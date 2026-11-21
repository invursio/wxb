"""Throwaway HOME and executable isolation guard for WXB tests.

Lution computes its paths with ``Path.home()`` at import time.  Test suites
must not read or write the real ``~/.local/Lution``, ``~/.var/app`` or
``~/.config/wxb`` trees, so this module redirects HOME and the XDG roots
before the service (or GTK window) is imported.

It also provides :func:`assert_isolated`: a guard that imports every Lution /
WXB path module, resolves their state paths, checks the XDG fallbacks and
``expanduser``, and starts a fresh Python subprocess with the test HOME.  If a
path points at the real desktop home, at a real Lution/WXB/Sober root, or
anywhere outside the throwaway HOME, the guard raises ``IsolationError``.

The test suite deliberately exercises both sides of that guard: one child
process runs with the redirect enabled and must pass; another child sets
``WXB_TEST_NO_REDIRECT=1`` and the real HOME and must be caught.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
from pathlib import Path

# Capture the real desktop home before redirecting it.  This is only used by
# the guard to recognise paths that escaped, never as a state location.
_IMPORT_HOME = Path(os.environ.get("HOME") or "~").expanduser()
REAL_HOME = _IMPORT_HOME
# Capture the dependency location before HOME is redirected.  This is a
# read-only import path, never a Lution/WXB state location.
IMPORT_USER_SITE = site.getusersitepackages()

TEST_HOME = Path(tempfile.mkdtemp(prefix="wxb-test-home-"))
REDIRECT_ENABLED = os.environ.get("WXB_TEST_NO_REDIRECT") != "1"
os.environ["WXB_TEST_HOME"] = str(TEST_HOME)

_XDG_DIRS = (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_STATE_HOME", ".local/state"),
)

if REDIRECT_ENABLED:
    os.environ["HOME"] = str(TEST_HOME)
    for _name, _suffix in _XDG_DIRS:
        os.environ[_name] = str(TEST_HOME / _suffix)


class IsolationError(RuntimeError):
    """Raised when a suite can resolve state outside its throwaway HOME."""


def cleanup():
    shutil.rmtree(TEST_HOME, ignore_errors=True)


atexit.register(cleanup)


def _resolved(value):
    try:
        return Path(str(value)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _within(path, root):
    path = _resolved(path)
    root = _resolved(root)
    if path is None or root is None:
        return False
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _real_state_roots():
    """The real desktop paths that must never be reached by a test."""
    return (
        REAL_HOME / ".local/Lution",
        REAL_HOME / ".local/share/Lution",
        REAL_HOME / ".config/wxb",
        REAL_HOME / ".local/state/wxb",
        REAL_HOME / ".cache/wxb",
        REAL_HOME / ".var/app/org.vinegarhq.Sober",
        REAL_HOME / ".local/share/applications/WXB.desktop",
        REAL_HOME / ".local/bin/wxb",
    )


def _add_path(out, label, value):
    if value is not None:
        out[label] = str(value)


def collect_paths():
    """Resolve every state-bearing path used by Lution/WXB.

    Importing these modules under the redirected HOME is intentional: the
    import-time constants are exactly what an escaping path would capture.
    No function here writes state; service objects are only constructed with
    an explicit throwaway settings path.
    """
    from lution_core import paths
    from lution_core import fonts, cursors, fps, envvars, fflags
    from lution_core import playtime, sober, soberlog, history, log
    import roblox_runtime as rr
    import wxb_studio_service as svc

    out = {}
    for label, value in (
            ("paths.sober_data", paths.SOBER_DATA),
            ("paths.sober_overlay", paths.SOBER_OVERLAY),
            ("paths.sober_config", paths.SOBER_CONFIG),
            ("paths.sober_apk_dir", paths.SOBER_APK_DIR),
            ("paths.sober_crashes", paths.SOBER_CRASHES),
            ("paths.sober_cache", paths.SOBER_CACHE),
            ("paths.flag_cache", paths.FLAG_CACHE),
            ("paths.lution_root", paths.LUTION_ROOT),
            ("paths.lution_overlay", paths.LUTION_OVERLAY),
            ("paths.lution_config", paths.LUTION_CONFIG),
            ("paths.state_dir", paths.STATE_DIR),
            ("fonts.overlay", fonts.OVERLAY_FONT_DIR),
            ("fonts.installed", fonts.INSTALLED_FONTS_DIR),
            ("cursors.overlay", cursors.OVERLAY_TEXTURES),
            ("cursors.kept_dir", cursors.KEPT_DIR),
            ("cursors.kept_file", cursors.KEPT_FILE),
            ("cursors.config", cursors.CONFIG_FILE),
            ("fps.xml", fps.FPS_XML),
            ("env.dir", envvars.CONFIG_DIR),
            ("env.file", envvars.ENV_FILE),
            ("flags.config", fflags.CONFIG_JSON),
            ("flags.sober_config", fflags.SOBER_CONFIG),
            ("flags.allowlist_cache", fflags.ALLOWLIST_CACHE),
            ("playtime.cache", playtime.CACHE_FILE),
            ("playtime.logs", playtime.SOBER_LOGS),
            ("sober.data", sober.SOBER_DATA_DIR),
            ("soberlog.logs", soberlog.LOG_DIR),
            ("soberlog.client_logs", soberlog.CLIENT_LOG_DIR),
            ("history.logs", history.SOBER_LOGS),
            ("history.cache_dir", history.CACHE_DIR),
            ("history.names_cache", history.NAMES_CACHE),
            ("log.file", log.LOG_FILE)):
        _add_path(out, label, value)
    for name, value in paths.FONT_DIRS.items():
        _add_path(out, "paths.font_dirs.%s" % name, value)

    # Settings/config and named flag profiles.  Both the default settings path
    # and an explicitly constructed service are checked.
    _add_path(out, "config.default_settings", svc.settings_path())
    default_service = svc.StudioService()
    _add_path(out, "config.default_service", default_service.path)
    _add_path(out, "config.default_clicker_stats", default_service.stats_path)
    service = svc.StudioService(path=TEST_HOME / "guard-studio.json")
    _add_path(out, "config.service", service.path)
    _add_path(out, "config.clicker_stats", service.stats_path)
    _add_path(out, "config.config_dir", service.config_dir())
    _add_path(out, "config.flag_profile_dir", service.flag_profile_dir())
    _add_path(out, "config.flag_profile",
              service._flag_profile_path("isolation-guard"))

    # The alt/runtime store and profile roots are WXB state too.
    _add_path(out, "runtime.store", rr.default_store_path())
    _add_path(out, "runtime.home", rr._cordial_home())
    for index, root in enumerate(rr.profile_roots()):
        _add_path(out, "runtime.profile_root.%d" % index, root)

    # Exercise the expanduser path explicitly; a file:// picker path must not
    # resolve against the real desktop home.
    _add_path(out, "expand.font", fonts._as_path("~/guard-font.ttf"))
    _add_path(out, "expand.service", svc._desktop_path("~/guard-path"))
    return out


def _env_errors(errors):
    for name, suffix in (("HOME", None),) + _XDG_DIRS:
        expected = TEST_HOME if suffix is None else TEST_HOME / suffix
        actual = os.environ.get(name)
        if not actual:
            errors.append("%s is unset; expected %s" % (name, expected))
        elif _resolved(actual) != _resolved(expected):
            errors.append("%s is %s, expected %s" % (name, actual, expected))
    if _resolved(os.path.expanduser("~")) != _resolved(TEST_HOME):
        errors.append("os.path.expanduser('~') is %s, expected %s"
                      % (os.path.expanduser("~"), TEST_HOME))


def _path_errors(label, value, errors, expected_home):
    path = _resolved(value)
    if path is None:
        errors.append("%s cannot be resolved: %r" % (label, value))
        return
    if not _within(path, expected_home):
        errors.append("%s is %s, outside test HOME %s"
                      % (label, path, expected_home))
    for real_root in _real_state_roots():
        if _within(path, real_root):
            errors.append("%s resolves into real state %s: %s"
                          % (label, real_root, path))


def _check_paths(paths, errors, expected_home):
    for label, value in paths.items():
        _path_errors(label, value, errors, expected_home)


# This child imports the modules directly (without importing this helper
# first) so it proves that fresh processes re-derive paths from the HOME and
# XDG values they inherit.  It does not write any state.
_PROBE_CODE = r'''
import json
import os
from pathlib import Path
from lution_core import paths, fonts, cursors, fps, envvars, fflags
from lution_core import playtime, sober, soberlog, history, log
import roblox_runtime as rr
import wxb_studio_service as svc

home = os.environ["WXB_TEST_HOME"]
service = svc.StudioService(path=os.path.join(home, "probe-studio.json"))
out = {
    "home": str(Path.home()),
    "expanduser": os.path.expanduser("~"),
    "config.settings_path": str(svc.settings_path()),
    "config.service": str(service.path),
    "config.config_dir": str(service.config_dir()),
    "config.flag_profile_dir": str(service.flag_profile_dir()),
    "config.flag_profile": str(service._flag_profile_path("probe-profile")),
    "paths.lution_root": str(paths.LUTION_ROOT),
    "paths.lution_overlay": str(paths.LUTION_OVERLAY),
    "paths.lution_config": str(paths.LUTION_CONFIG),
    "paths.state_dir": str(paths.STATE_DIR),
    "paths.sober_data": str(paths.SOBER_DATA),
    "paths.sober_overlay": str(paths.SOBER_OVERLAY),
    "paths.sober_config": str(paths.SOBER_CONFIG),
    "paths.sober_apk_dir": str(paths.SOBER_APK_DIR),
    "paths.sober_crashes": str(paths.SOBER_CRASHES),
    "paths.sober_cache": str(paths.SOBER_CACHE),
    "paths.flag_cache": str(paths.FLAG_CACHE),
    "fonts.overlay": str(fonts.OVERLAY_FONT_DIR),
    "fonts.installed": str(fonts.INSTALLED_FONTS_DIR),
    "cursors.overlay": str(cursors.OVERLAY_TEXTURES),
    "cursors.kept_dir": str(cursors.KEPT_DIR),
    "cursors.kept_file": str(cursors.KEPT_FILE),
    "cursors.config": str(cursors.CONFIG_FILE),
    "fps.xml": str(fps.FPS_XML),
    "env.dir": str(envvars.CONFIG_DIR),
    "env.file": str(envvars.ENV_FILE),
    "flags.config": str(fflags.CONFIG_JSON),
    "flags.allowlist_cache": str(fflags.ALLOWLIST_CACHE),
    "playtime.cache": str(playtime.CACHE_FILE),
    "sober.data": str(sober.SOBER_DATA_DIR),
    "soberlog.logs": str(soberlog.LOG_DIR),
    "soberlog.client_logs": str(soberlog.CLIENT_LOG_DIR),
    "history.logs": str(history.SOBER_LOGS),
    "history.cache_dir": str(history.CACHE_DIR),
    "history.names_cache": str(history.NAMES_CACHE),
    "runtime.store": str(rr.default_store_path()),
    "runtime.home": str(rr._cordial_home()),
    "expand.font": str(fonts._as_path("~/probe-font.ttf")),
    "expand.service": str(svc._desktop_path("~/probe-path")),
}
print("WXB_ISOLATION_PROBE " + json.dumps(out, sort_keys=True))
'''


def _probe_subprocess(errors):
    """Re-import Lution in a child with XDG unset to test HOME fallbacks."""
    env = os.environ.copy()
    env["HOME"] = str(TEST_HOME)
    env["WXB_TEST_HOME"] = str(TEST_HOME)
    for name, _suffix in _XDG_DIRS:
        env.pop(name, None)
    env.pop("WXB_TEST_NO_REDIRECT", None)
    app_dir = Path(__file__).resolve().parent
    search = [str(app_dir)]
    if IMPORT_USER_SITE and Path(IMPORT_USER_SITE).is_dir():
        search.append(IMPORT_USER_SITE)
    if env.get("PYTHONPATH"):
        search.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(search)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE],
            cwd=str(app_dir), env=env, capture_output=True, text=True,
            timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append("subprocess probe could not run: %s" % exc)
        return
    marker = "WXB_ISOLATION_PROBE "
    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(marker):
            try:
                payload = json.loads(line[len(marker):])
            except ValueError:
                payload = None
            break
    if proc.returncode != 0 or payload is None:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        errors.append("subprocess probe failed (exit %s)%s"
                      % (proc.returncode,
                         ": " + detail[-1] if detail else ""))
        return
    for key in ("home", "expanduser"):
        _path_errors("subprocess.%s" % key, payload.get(key, ""),
                     errors, TEST_HOME)
    for label, value in payload.items():
        if label in ("home", "expanduser"):
            continue
        _path_errors("subprocess.%s" % label, value, errors, TEST_HOME)


def assert_isolated(entry="suite"):
    """Fail loudly if any Lution/WXB state path escapes the test HOME.

    Returns a short report of the checked path count on success.  The report
    is intentionally simple so test logs can show that the guard actually
    walked every state category.
    """
    errors = []
    if not REDIRECT_ENABLED:
        errors.append("redirect disabled by WXB_TEST_NO_REDIRECT")
    if os.geteuid() == 0:
        errors.append("test isolation must run unprivileged, not as root")
    _env_errors(errors)
    if os.environ.get("WXB_TEST_HOME") != str(TEST_HOME):
        errors.append("WXB_TEST_HOME is %s, expected %s"
                      % (os.environ.get("WXB_TEST_HOME"), TEST_HOME))
    # The service's passwd/pw_dir lookup is only valid for the elevated owner
    # bridge.  Test processes are unprivileged, so that lookup must be
    # inactive; an active owner would be a path outside the test HOME.
    if os.geteuid() != 0:
        try:
            import wxb_studio_service as _guard_svc
            if _guard_svc._desktop_owner() is not None:
                errors.append("passwd owner lookup resolved outside test HOME")
        except Exception as exc:
            errors.append("passwd owner check failed: %s" % exc)
    try:
        paths = collect_paths()
    except Exception as exc:
        raise IsolationError(
            "isolation guard could not import state modules for %s: %s: %s"
            % (entry, type(exc).__name__, exc)) from exc
    _check_paths(paths, errors, TEST_HOME)
    _probe_subprocess(errors)
    if errors:
        raise IsolationError(
            "isolation guard failed for %s:\n  - %s"
            % (entry, "\n  - ".join(errors)))
    return "isolation guard passed for %s: %d state paths" % (entry, len(paths))


if __name__ == "__main__":
    print(assert_isolated("command-line"))
