# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Package-relative imports; the overlay layout and the APK font list are
# upstream. WXB adds validation, a size cap, hardlink placement and a kept
# copy that restore() also clears. See NOTICE.md next to this package.
"""Custom fonts for Sober.

Roblox reads its text faces from ``assets/content/fonts/`` inside the APK.
Sober overlays that directory from ``data/sober/asset_overlay/content``, and
Lution bind-mounts ``~/.local/share/Lution/overlay/content`` there at launch,
so a file written into ``overlay/content/fonts/<name>`` replaces that face for
the next session. Nothing inside the APK is touched: deleting the overlay
files gives the stock faces back.
"""

import os
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import paths

def overlay_font_dir():
    return paths.lution_overlay() / "content/fonts"


def installed_fonts_dir():
    return paths.lution_root() / "installed_font"

# Emoji faces are deliberately left alone: replacing them breaks emoji.
EMOJI_FONT_NAMES = ("RobloxEmoji.ttf", "TwemojiMozilla.ttf")
FONT_SUFFIXES = (".ttf", ".otf")
MAX_FONT_BYTES = 12 * 1024 * 1024

# A font file starts with an sfnt signature. Checking it stops a renamed
# image (or a truncated download) from being copied over every face.
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")


def _as_path(font_path):
    """A Path from a plain path or a file:// URI (what a drop sends)."""
    if isinstance(font_path, Path):
        return font_path
    text = str(font_path)
    if text.startswith("file:"):
        parsed = urlparse(text)
        return Path(unquote(parsed.path))
    return paths.expanduser(text)


def _lower_names():
    return {name.lower() for name in EMOJI_FONT_NAMES}


def validate_source(font_path):
    """Check a user-picked file. Returns (Path, None) or (None, reason)."""
    path = _as_path(font_path)
    if not path.exists():
        return None, "font file not found: %s" % path
    if path.is_dir():
        return None, "that is a folder, not a font file"
    if path.suffix.lower() not in FONT_SUFFIXES:
        return None, "only .ttf and .otf files can be used"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        return None, "cannot read %s: %s" % (path.name, exc)
    if size <= 0:
        return None, "%s is empty" % path.name
    if size > MAX_FONT_BYTES:
        return None, "%s is larger than %d MB" % (
            path.name, MAX_FONT_BYTES // (1024 * 1024))
    if head not in _FONT_MAGIC:
        return None, "%s does not look like a TrueType/OpenType font" % path.name
    return path, None


def apk_font_names():
    """Font file names Roblox ships, excluding emoji faces and families/."""
    apk = paths.find_base_apk()
    if apk is None:
        return []
    skip = _lower_names()
    names = []
    try:
        with zipfile.ZipFile(apk) as zf:
            for entry in zf.namelist():
                if not entry.startswith("assets/content/fonts/"):
                    continue
                if entry.startswith("assets/content/fonts/families/"):
                    continue
                name = Path(entry).name
                if not name or Path(name).suffix.lower() not in FONT_SUFFIXES:
                    continue
                if name.lower() in skip:
                    continue
                names.append(name)
    except (OSError, zipfile.BadZipFile):
        return []
    # a zip can list the same entry twice; each face must be written once
    return list(dict.fromkeys(names))


def _overlay_fonts():
    overlay_dir = overlay_font_dir()
    if not overlay_dir.exists():
        return []
    skip = _lower_names()
    return [p for p in overlay_dir.iterdir()
            if p.is_file() and p.name.lower() not in skip]


def installed_font():
    """The kept copy (`installed_font/font.<ext>`), or None."""
    installed_dir = installed_fonts_dir()
    if not installed_dir.exists():
        return None
    kept = [p for p in installed_dir.glob("font.*")
            if p.is_file() and p.suffix.lower() in FONT_SUFFIXES]
    return sorted(kept, key=lambda p: p.stat().st_mtime)[-1] if kept else None


def status():
    """What the UI needs to describe the current state, without writing."""
    overlay_dir = overlay_font_dir()
    names = apk_font_names()
    overlay = _overlay_fonts()
    wanted = set(names)
    active = [p for p in overlay if p.name in wanted]
    kept = installed_font()
    return {
        "apk": paths.find_base_apk() is not None,
        "apk_fonts": len(names),
        "active": len(active),
        "installed": kept.name if kept else "",
        "overlay": str(overlay_dir),
    }


def _unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def _tmp_for(path):
    """A temp name only this process can own.

    Two font operations can run at once (the UI guards itself, but two
    windows, or the WXB bridge and a terminal, do not share a lock), and a
    shared temp name means one finishes the rename out from under the other.
    """
    return path.with_name("%s.%d.wxb-tmp" % (path.name, os.getpid()))


def _clean_stale(directory):
    """Drop temp files left by a crash, but never one still being written."""
    cutoff = time.time() - 3600
    for path in directory.glob("*.wxb-tmp"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _place(source, dest):
    """Put `source` at `dest`; a hardlink keeps 75 faces at one copy's size."""
    try:
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)         # nothing sane creates this, but clear it
        elif dest.exists() or dest.is_symlink():
            dest.unlink()
    except OSError:
        pass
    try:
        os.link(source, dest)
        return
    except OSError:
        pass                    # different filesystem: a copy still works
    # write beside it and rename: a torn copy must never be visible to the
    # mounted overlay (or left behind by a full disk)
    tmp = _tmp_for(dest)
    shutil.copyfile(source, tmp)
    os.replace(tmp, dest)


def apply_font(font_path):
    """Replace every Roblox text face with `font_path`. Returns the names.

    Raises ValueError for a bad file and FileNotFoundError when the APK (and
    so the font list) cannot be read.
    """
    source, error = validate_source(font_path)
    if error:
        raise ValueError(error)
    names = apk_font_names()
    if not names:
        raise FileNotFoundError(
            "Could not read the font list from the Roblox APK. "
            "Install Sober and let it run once, then try again.")
    overlay_dir = overlay_font_dir()
    installed_dir = installed_fonts_dir()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    installed_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale(overlay_dir)
    _clean_stale(installed_dir)

    # Keep one private copy first and link every face from it. The overlay
    # then never depends on the file the user picked - it can be moved,
    # edited or deleted afterwards - and a picked file can never be one of
    # the destinations (which the old link-from-source order deleted).
    kept = installed_dir / ("font" + source.suffix.lower())
    tmp = _tmp_for(installed_dir / ".font")
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, kept)
    except OSError:
        _unlink(tmp)
        raise
    for old in installed_dir.glob("font.*"):
        if old != kept:
            _unlink(old)
    for name in names:
        _place(kept, overlay_dir / name)
    return names


def restore_fonts():
    """Delete the overrides and the kept copy; returns how many were removed.

    The kept copy goes too: otherwise the next APK update would silently put
    the custom font back, which is not what "restore" promises.
    """
    overlay_dir = overlay_font_dir()
    installed_dir = installed_fonts_dir()
    removed = 0
    skip = _lower_names()
    if overlay_dir.exists():
        for path in list(overlay_dir.iterdir()):
            if path.name.lower() in skip:
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
            except OSError:
                pass
    if installed_dir.exists():
        for path in list(installed_dir.glob("font.*")):
            try:
                path.unlink()
            except OSError:
                pass
        for stale in installed_dir.glob("*.wxb-tmp"):
            _unlink(stale)
    return removed


def reapply_fonts():
    """Re-apply the kept font after Sober replaced its APK.

    Returns the number of faces rewritten (0 when there is nothing to do).
    """
    kept = installed_font()
    if kept is None:
        return 0
    names = apk_font_names()
    if not names:
        return 0
    overlay_dir = overlay_font_dir()
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        _place(kept, overlay_dir / name)
    return len(names)


def __getattr__(name):
    """Keep the historical directory names as dynamic attributes."""
    if name == "OVERLAY_FONT_DIR":
        return overlay_font_dir()
    if name == "INSTALLED_FONTS_DIR":
        return installed_fonts_dir()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
