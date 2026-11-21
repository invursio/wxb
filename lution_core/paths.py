# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# where everything lives
#
# WXB runs elevated for input access.  The Lution root must still be the
# desktop user's home, not root's, and an explicit WXB_HOME override must win
# even when HOME was already set when this module was imported.

import os
import pwd
import shutil
import zipfile
from pathlib import Path

from . import log

SOBER_APP_ID = "org.vinegarhq.Sober"
OVERLAY_ROOTS = ("content", "ExtraContent", "PlatformContent")

_HOME_OVERRIDE = None


def set_home(path):
    """Set an explicit Lution home for this process (None clears it)."""
    global _HOME_OVERRIDE
    _HOME_OVERRIDE = Path(path).expanduser() if path else None


def clear_home():
    set_home(None)


def lution_home():
    """The invoking desktop user's home, resolved on every call.

    ``set_home()`` wins for embedded/test callers, then the explicit
    ``WXB_LUTION_HOME``/``WXB_HOME`` environment override, then ``Path.home()``.
    """
    if _HOME_OVERRIDE is not None:
        return _HOME_OVERRIDE
    raw = (os.environ.get("WXB_LUTION_HOME")
           or os.environ.get("WXB_HOME"))
    if raw:
        return Path(raw).expanduser()
    if os.geteuid() == 0:
        # An elevated WXB may be started by pkexec/sudo without WXB_HOME.
        # The invoking desktop account is still the owner of Lution state.
        raw_uid = (os.environ.get("WXB_OWNER_UID")
                   or os.environ.get("PKEXEC_UID")
                   or os.environ.get("SUDO_UID"))
        if raw_uid and str(raw_uid).isdigit() and int(raw_uid) != 0:
            try:
                return Path(pwd.getpwuid(int(raw_uid)).pw_dir)
            except KeyError:
                pass
    return Path.home()


def expanduser(value):
    """Expand a leading ~ against the desktop home, not root's HOME."""
    text = str(value or "")
    if text == "~":
        return lution_home()
    if text.startswith("~/") or text.startswith("~\\"):
        return lution_home() / text[2:]
    return Path(text).expanduser()


def sober_app_dir():
    return lution_home() / ".var/app" / SOBER_APP_ID


def sober_data():
    return sober_app_dir() / "data/sober"


def sober_overlay():
    return sober_data() / "asset_overlay"


def sober_config():
    return sober_app_dir() / "config/sober/config.json"


def sober_apk_dir():
    return sober_data() / "packages/x86_64/com.roblox.client"


def sober_crashes():
    return sober_data() / "crashes"


def sober_cache():
    return sober_app_dir() / "cache/sober/cache"


def flag_cache():
    return sober_cache() / "flag_cache.dat"


def lution_root():
    return lution_home() / ".local/share/Lution"


def lution_overlay():
    return lution_root() / "overlay"


def lution_config():
    return lution_root() / "sober_config.json"


def state_dir():
    return lution_home() / ".local/Lution"


def font_dirs():
    root = lution_root()
    return {
        "font": root / "installed_font",
        "emoji": root / "installed_emoji",
        "cursors": root / "installed_cursors",
        "sounds": root / "installed_sounds",
    }


def lution_overlay_dir(name):
    return lution_overlay() / name


def sober_overlay_dir(name):
    return sober_overlay() / name


def ensure_lution_dirs():
    for name in OVERLAY_ROOTS:
        (lution_overlay() / name).mkdir(parents=True, exist_ok=True)
    lution_root().mkdir(parents=True, exist_ok=True)


def apk_has_font_assets(apk_path):
    try:
        with zipfile.ZipFile(apk_path) as zf:
            for entry in zf.namelist():
                if entry.startswith("assets/content/fonts/"):
                    return True
    except (OSError, zipfile.BadZipFile):
        pass
    return False


def find_base_apk():
    apk_dir = sober_apk_dir()
    if not apk_dir.exists():
        return None

    apks = list(apk_dir.glob("*.apk"))
    if not apks:
        return None

    for apk in apks:
        if apk.name == "base.apk":
            return apk

    with_assets = [a for a in apks if apk_has_font_assets(a)]
    pool = with_assets or apks
    return max(pool, key=lambda p: p.stat().st_size)


def _zip_dir(src, arc_root, zf):
    for fp in src.rglob("*"):
        if fp.is_file():
            zf.write(fp, arc_root + "/" + str(fp.relative_to(src)))


def migrate_legacy_overlay():
    ensure_lution_dirs()

    pending = []
    for name in OVERLAY_ROOTS:
        src = sober_overlay() / name
        if not src.exists():
            continue
        if any(p.is_file() for p in src.rglob("*")):
            pending.append((name, src))

    if not pending:
        return []

    backup_zip = lution_root() / "pre_migration_overlay.zip"
    with zipfile.ZipFile(backup_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, src in pending:
            _zip_dir(src, "asset_overlay/" + name, zf)
    log.info(f"Backed up existing overlay to {backup_zip}")

    migrated = []
    for name, src in pending:
        dest = lution_overlay() / name
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src)
        migrated.append(name)
        log.info(f"Migrated asset_overlay/{name} into lution")

    return migrated


def cleanup_legacy_config():
    return not sober_config().exists()


def __getattr__(name):
    """Keep the historical path names as per-call dynamic attributes."""
    getters = {
        "SOBER_DATA": sober_data,
        "SOBER_OVERLAY": sober_overlay,
        "SOBER_CONFIG": sober_config,
        "SOBER_APK_DIR": sober_apk_dir,
        "SOBER_CRASHES": sober_crashes,
        "SOBER_CACHE": sober_cache,
        "FLAG_CACHE": flag_cache,
        "LUTION_ROOT": lution_root,
        "LUTION_OVERLAY": lution_overlay,
        "LUTION_CONFIG": lution_config,
        "STATE_DIR": state_dir,
        "FONT_DIRS": font_dirs,
    }
    try:
        return getters[name]()
    except KeyError:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
