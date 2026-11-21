#!/usr/bin/env python3
"""WXB Studio service layer.

The GTK shell only talks to this module.  It owns settings, the three input
engines and the optional game/alt launchers, so every control can be tested
without constructing a window.  The input engines are the long-running,
battle-tested implementations from :mod:`wxb_engine`; this file adds clean
lifecycle, validation and change notifications around them.
"""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import pwd
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping

APP_DIR = Path(__file__).resolve().parent
STUDIO_VERSION = "4.2"
CLICKER_STATS_VERSION = 1
CLICKER_STATS_MAX = 10 ** 12

# Gamescope is an opt-in launch effect: it adds a nested compositor, so the
# shipped default stays off.  A settings value or the GameMode switch layers
# on top of this one default; no environment override changes this setting.
GAMESCOPE_DEFAULT = False

try:
    import wxb_engine as engine
except Exception:                                        # pragma: no cover
    engine = None

try:
    import wxb_macros as macros
except Exception:                                        # pragma: no cover
    macros = None

try:
    import wxb_recorder as screen_recorder
except Exception:                                        # pragma: no cover
    screen_recorder = None

import wxb_input_lab as input_lab

try:
    import roblox_runtime as roalt
except Exception:                                        # pragma: no cover
    roalt = None

try:
    from lution_core import launcher as lution_launcher
except Exception:                                        # pragma: no cover
    lution_launcher = None

try:
    from lution_core import sober as lution_sober
except Exception:                                        # pragma: no cover
    lution_sober = None

try:
    from lution_core import fflags as lution_fflags
except Exception:                                        # pragma: no cover
    lution_fflags = None

try:
    from lution_core import fonts as lution_fonts
except Exception:                                        # pragma: no cover
    lution_fonts = None

try:
    from lution_core import cursors as lution_cursors
except Exception:                                        # pragma: no cover
    lution_cursors = None

try:
    from lution_core import fps as lution_fps
except Exception:                                        # pragma: no cover
    lution_fps = None

try:
    from lution_core import envvars as lution_envvars
except Exception:                                        # pragma: no cover
    lution_envvars = None

try:
    from lution_core import playtime as lution_playtime
except Exception:                                        # pragma: no cover
    lution_playtime = None


CLICKER_CPS_MIN = 0.01
CLICKER_CPS_MAX = 1000.0
CLICKER_DUTY_MIN = 0.01
CLICKER_DUTY_MAX = 99.99
CLICKER_DURATION_MAX = 86400.0          # 24 h; 0 means infinite
CLICKER_JITTER_MAX = 500
CLICKER_POINTER_MODES = ("follow", "fixed", "jitter")
CLICKER_FAILSAFE_CORNERS = ("top-left", "top-right", "bottom-left",
                            "bottom-right", "off")
CLICKER_RATE_MODES = ("exact", "range")
CLICKER_TIMING_PRECISIONS = ("efficient", "balanced", "precise")
CLICKER_APP_FILTER_MODES = ("off", "blacklist", "whitelist")
CLICKER_START_DELAY_MAX = 60000          # milliseconds
CLICKER_STOP_CLICKS_MAX = 10000000
CLICKER_FAILSAFE_MARGIN_MAX = 10000
MOUSE_HOTKEY_BUTTONS = {
    "mouseleft": "left", "mouseright": "right", "mousemiddle": "middle",
    "mouse4": "button8", "mouse5": "button9",
}
HOTKEY_DEFAULTS = {
    "toggle_clicker": "f6",
    "toggle_rotation": "f7",
    "toggle_socd": "f8",
    "stop_all": "f9",
    "cycle_tiling": "ctrl+shift+f10",
    "record_macro": "ctrl+alt+r",
    "play_macro": "ctrl+alt+p",
    "stop_macro": "ctrl+alt+s",
    "stop_recording": "ctrl+alt+x",
    "toggle_recording": "ctrl+alt+v",
    "toggle_privacy": "ctrl+alt+h",
}
HOTKEY_LABELS = {
    "toggle_clicker": "Start / stop clicking",
    "toggle_rotation": "Start / stop rotation",
    "toggle_socd": "Start / stop SOCD",
    "stop_all": "Stop everything",
    "cycle_tiling": "Cycle Cordial tiling",
    "record_macro": "Record / stop macro",
    "play_macro": "Play / stop macro",
    "stop_macro": "Stop macro only",
    "stop_recording": "Stop screen recording",
    "toggle_recording": "Start / stop screen recording",
    "toggle_privacy": "Hide / show interface values",
}
HOTKEY_ACTIONS = {
    "toggle_clicker": "toggle_clicker",
    "toggle_rotation": "toggle_rotation",
    "toggle_socd": "toggle_socd",
    "stop_all": "stop_all",
    "cycle_tiling": "cycle_alt_tiling",
    "record_macro": "toggle_macro_recording",
    "play_macro": "toggle_macro_playback",
    "stop_macro": "stop_macro",
    "stop_recording": "stop_recording",
    "toggle_recording": "toggle_recording",
    "toggle_privacy": "toggle_privacy",
}
LEGACY_HOTKEY_ALIASES = {
    "f6": "toggle_clicker", "f7": "toggle_rotation",
    "f8": "toggle_socd", "f9": "stop_all", "esc": "stop_all",
}
ALT_FPS_DEFAULT = 15

ALT_TILING_CHOICES = (
    ("columns", "Side by side"),
    ("rows", "Stack on top"),
    ("grid", "Grid"),
    ("focus-left", "Focus left"),
    ("focus-top", "Focus top"),
)
ALT_TILING_LABELS = dict(ALT_TILING_CHOICES)

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 1,
    "onboarded": False,
    "appearance": "dark",
    "accent": "cyan",
    "monochrome": False,
    "appearance_revision": 2,
    "opacity": 92,
    "clicker": {
        "cps": 12, "duty": 50, "button": "left", "mode": "toggle",
        "duration": 0, "pointer_mode": "follow", "jitter": 0,
        "fixed_x": 0, "fixed_y": 0, "failsafe": True,
        "failsafe_corner": "top-left", "pause_on_lock": True,
        "rate_mode": "exact", "cps_max": 14, "start_delay_ms": 0,
        "stop_after_clicks": 0, "timing_precision": "balanced",
        "failsafe_margin": 2,
        "app_filter_mode": "off", "app_blacklist": [], "app_whitelist": [],
    },
    "rotation": {
        "slot_a": 1,
        "slot_b": 2,
        "delay_a": 140,
        "delay_b": 140,
        "mode": "toggle",
        "max_rot": 0,
    },
    "socd": {"mode_h": "Neutral", "mode_v": "Neutral"},
    "input_lab": copy.deepcopy(input_lab.DEFAULT_INPUT_LAB),
    "macros": {"speed": 1.0, "loop_mode": "once", "repeat": 1,
               "selected": ""},
    "recorder": {
        "source": "screen", "region_x": 0, "region_y": 0,
        "region_w": 0, "region_h": 0, "window_id": "",
        "resolution": "source", "fps": 30, "quality": "balanced",
        "encoder": "auto", "container": "mkv", "cursor": True,
        "save_dir": (str(screen_recorder.default_save_dir())
                     if screen_recorder is not None else ""),
        "mic": False, "mic_device": "", "desktop_audio": False,
        "desktop_device": "",
    },
    "alts": {"cpu_quota": 0, "auto_tile": True,
             "fps_cap": ALT_FPS_DEFAULT, "low_graphics": True,
             "tiling_style": "columns"},
    "gamemode": {"mangohud": False, "gamemode": False,
                  "gamescope": GAMESCOPE_DEFAULT},
    "hotkeys": dict(HOTKEY_DEFAULTS),
    "macro_enabled": False,
    "stop_on_close": False,
    "launch_mode": "player",
    "places": [],
    "nav_compact": False,
    "always_on_top": False,
    "window": {"x": None, "y": None, "width": 1180, "height": 780,
               "maximized": False},
}

ACCENTS = {
    "cyan": "#9fcaff",
    "rose": "#e7a6b4",
    "apricot": "#eab48e",
    "sage": "#a8c3a1",
    "lilac": "#b9abd9",
    "ice": "#9dc5d6",
}


def portable_mode() -> bool:
    """Whether WXB should keep its settings beside the application.

    A portable copy is selected explicitly with ``WXB_PORTABLE=1`` or by a
    ``portable.flag`` file next to the launcher.  This keeps a USB-stick or
    extracted build self-contained without changing the normal XDG path.
    """
    value = os.environ.get("WXB_PORTABLE", "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return (APP_DIR / "portable.flag").is_file()


def settings_path() -> Path:
    if portable_mode():
        return APP_DIR / "wxb-studio-data" / "studio.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "wxb" / "studio.json"


def _normal_settings_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "wxb" / "studio.json"


def _migrate_portable_settings(target: Path) -> bool:
    """Seed a first portable launch from the normal settings file.

    Portable mode used to start with a blank/default tree because it pointed
    at a new path.  Copying the existing XDG config on first launch keeps the
    user's clicker settings, appearance and window state intact.  The copy is
    private and atomic; a failure is left for the UI warning rather than
    overwriting either file.
    """
    if target.exists():
        return False
    source = _normal_settings_path()
    if not source.is_file() or source == target:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(source.read_bytes())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _desktop_owner():
    """The desktop user WXB must use for Lution-owned files.

    WXB itself is often elevated for /dev/input and /dev/uinput.  Lution and
    Flatpak state belongs to the logged-in user, not root, so every bridge
    call runs as that user and gets that user's HOME.  Returns
    ``(uid, gid, name, home)`` or ``None`` when we are not elevated.
    """
    if os.geteuid() != 0:
        return None
    raw = (os.environ.get("WXB_OWNER_UID")
           or os.environ.get("PKEXEC_UID")
           or os.environ.get("SUDO_UID"))
    uid = int(raw) if raw and str(raw).isdigit() else 0
    home = os.environ.get("WXB_HOME") or ""
    if not uid and home:
        try:
            uid = os.stat(home).st_uid
        except OSError:
            uid = 0
    if not uid or uid == 0:
        return None
    try:
        record = pwd.getpwuid(uid)
    except KeyError:
        return None
    home = home or record.pw_dir
    if not home:
        return None
    return uid, record.pw_gid, record.pw_name, home


def _lution_bridge_env(owner=None):
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    app_dir = str(APP_DIR)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (app_dir + os.pathsep + current
                         if current else app_dir)
    if owner is None:
        return env
    _uid, _gid, name, home = owner
    env["HOME"] = home
    env["WXB_HOME"] = home
    env["USER"] = name
    env["LOGNAME"] = name
    # A root shell may carry root's X authority or XDG roots.  The desktop
    # owner's paths are the only ones that have a session to talk to.
    xauth = env.get("XAUTHORITY", "")
    if not xauth or xauth.startswith("/root"):
        candidate = Path(home) / ".Xauthority"
        if candidate.exists():
            env["XAUTHORITY"] = str(candidate)
    defaults = {
        "XDG_CONFIG_HOME": ".config",
        "XDG_DATA_HOME": ".local/share",
        "XDG_CACHE_HOME": ".cache",
        "XDG_STATE_HOME": ".local/state",
    }
    for key, suffix in defaults.items():
        # Even a convincing inherited XDG root may point at root's cache or
        # config.  The desktop owner's roots are canonical for this bridge.
        env[key] = str(Path(home) / suffix)
    # A root WXB can inherit root's display but not root's session bus.  Flatpak
    # and Cordial need the desktop user's bus and runtime directory, otherwise
    # `flatpak run` starts and immediately exits with no window.  Use the
    # owner's runtime path whenever it exists, even if the root shell supplied
    # a value that points at /run/user/0.
    runtime = Path("/run/user") / str(_uid)
    try:
        runtime_exists = runtime.is_dir()
    except OSError:
        runtime_exists = False
    if runtime_exists:
        env["XDG_RUNTIME_DIR"] = str(runtime)
        bus = runtime / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(bus)
    return env


def run_lution_cli(op: str, payload: Any = None, args: tuple = (), *,
                   timeout: float = 45.0) -> dict:
    """Run one vendored Lution bridge operation as the desktop user.

    The CLI is intentionally a subprocess: it is the boundary that prevents
    an elevated WXB from creating root-owned files in the user's Lution,
    Sober or Flatpak state.  The same call path is used by the live UI and by
    the test suite (which points HOME at a throwaway directory).
    """
    euid = os.geteuid()
    owner = _desktop_owner()
    if euid == 0 and owner is None:
        return {"ok": False,
                "error": "WXB is elevated but the desktop user could not "
                         "be identified; refusing to write root-owned "
                         "Lution state."}
    cmd = [sys.executable, "-m", "lution_core.cli", str(op)]
    cmd += [str(arg) for arg in args]
    input_text = "" if payload is None else json.dumps(payload)
    kwargs = {
        "cwd": str(APP_DIR),
        "env": _lution_bridge_env(owner),
        "input": input_text,
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if owner is not None:
        uid, gid, name, _home = owner
        try:
            groups = os.getgrouplist(name, gid)
        except (OSError, KeyError):
            groups = [gid]
        kwargs.update({"user": uid, "group": gid, "extra_groups": groups})
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Lution bridge timed out (%s)." % op}
    except OSError as exc:
        return {"ok": False, "error": "Lution bridge failed: %s" % exc}
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            data.setdefault("ok", proc.returncode == 0)
            return data
    detail = (proc.stderr or "").strip().splitlines()
    return {"ok": False,
            "error": "Lution bridge returned no JSON%s" % (
                ": " + detail[-1] if detail else ".")}


def _claim_owner(path: Path) -> None:
    """Hand a file written by elevated WXB back to the desktop user."""
    owner = _desktop_owner()
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def _desktop_path(value: Any) -> str:
    """Expand ``~`` against the desktop owner, never against root's HOME."""
    text = str(value or "")
    if text.startswith("~") and (len(text) == 1 or text[1] in ("/", "\\")):
        owner = _desktop_owner()
        if owner is not None:
            return owner[3] + text[1:]
    return os.path.expanduser(text)


def _valid_env_name(key: Any) -> bool:
    name = str(key or "").strip()
    return bool(name) and "=" not in name and not any(
        char.isspace() for char in name)


def _owner_launch_preexec():
    """Drop a game child from root to the desktop owner, when elevated.

    WXB runs as root for uinput/input-group access, but Flatpak and Sober must
    run as the logged-in user: root's Flatpak session has no /run/flatpak,
    which is exactly the bwrap lock-file failure that blocked launches.
    """
    if os.geteuid() != 0:
        return None
    owner = os.environ.get("WXB_OWNER_UID") or os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    uid = int(owner) if owner and str(owner).isdigit() else 0
    if not uid:
        home = os.environ.get("WXB_HOME", "")
        try:
            uid = os.stat(home).st_uid if home else 0
        except OSError:
            uid = 0
    if not uid or uid == 0:
        return None
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        return None

    def drop():
        try:
            os.setgroups([])
        except OSError:
            pass
        os.setgid(gid)
        os.setuid(uid)

    return drop


# Keys a removed deck wrote.  If the file has these and no studio sections,
# treat it as a foreign/legacy file and start from defaults instead of
# guessing at its old meaning.
LEGACY_SETTINGS_KEYS = frozenset({
    "theme", "theme_choice", "ac_cps", "ac_cdc", "ac_button", "ac_mode",
    "m_slot_a", "m_slot_b", "m_delay_a", "m_delay_b", "m_mode", "maxrot",
    "macro_enabled", "hotkeys", "live", "rail_compact", "glass_alpha",
    "cursor_style", "cursor_color", "cursor_size", "font", "opacity",
})

SOCD_MODE_ALIASES = {
    "neutral": "Neutral",
    "last win": "Last win", "last": "Last win",
    "first win": "First win", "first": "First win",
    "priority": "Priority",
}
def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _legacy_shape(raw: Mapping[str, Any]) -> bool:
    if any(key in raw for key in ("clicker", "rotation", "socd", "alts")):
        return False
    return any(key in raw for key in LEGACY_SETTINGS_KEYS)


def _number(value: Any, default: float, low: float, high: float,
            *, integer: bool = False) -> float | int:
    """Clamp one stored number.  Booleans and non-finite values are not
    numbers here; the UI must never receive NaN or an unbounded spin value."""
    if isinstance(value, bool):
        return int(default) if integer else default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default) if integer else default
    if not math.isfinite(number):
        return int(default) if integer else default
    number = max(low, min(high, number))
    return int(round(number)) if integer else number


def _optional_coord(value: Any) -> int | None:
    if value is None:
        return None
    return int(_number(value, 0, -32000, 32000, integer=True))


def _choice(value: Any, choices: Mapping[str, str] | tuple[str, ...],
            default: str) -> str:
    if not isinstance(value, str):
        return default
    needle = value.strip().lower()
    if isinstance(choices, Mapping):
        return choices.get(needle, default)
    for choice in choices:
        if choice.lower() == needle:
            return choice
    return default


def normalize_app_list(value: Any) -> list[str]:
    """Normalise process names from a list or a comma/semicolon field."""
    if isinstance(value, str):
        raw_items = value.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = []
    result: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            continue
        name = item.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
        if name.endswith(".exe"):
            name = name[:-4]
        name = name.strip()[:64]
        if name and name not in result:
            result.append(name)
        if len(result) >= 64:
            break
    return result


_FOREGROUND_CACHE_LOCK = threading.Lock()
_FOREGROUND_CACHE: tuple[float, str] = (0.0, "")


def _process_name_from_pid(pid: Any) -> str:
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid_i <= 0:
        return ""
    try:
        name = Path("/proc/%d/comm" % pid_i).read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        name = ""
    if not name:
        try:
            name = os.path.basename(
                os.readlink("/proc/%d/exe" % pid_i))
        except OSError:
            name = ""
    return normalize_app_list([name])[0] if name else ""


def _query_foreground_app_name() -> str:
    """Read the active X11 window's process name without optional packages."""
    window_id = 0
    pid = 0
    wm_class = ""
    try:
        from Xlib import X, display  # type: ignore
        display_handle = display.Display()
        try:
            root = display_handle.screen().root
            active = root.get_full_property(
                display_handle.intern_atom("_NET_ACTIVE_WINDOW"),
                X.AnyPropertyType)
            if active is not None and len(active.value):
                window_id = int(active.value[0])
            if window_id:
                window = display_handle.create_resource_object("window",
                                                              window_id)
                pid_prop = window.get_full_property(
                    display_handle.intern_atom("_NET_WM_PID"),
                    X.AnyPropertyType)
                if pid_prop is not None and len(pid_prop.value):
                    pid = int(pid_prop.value[0])
                class_prop = window.get_full_property(
                    display_handle.intern_atom("WM_CLASS"),
                    X.AnyPropertyType)
                if class_prop is not None:
                    raw_class = class_prop.value
                    if isinstance(raw_class, bytes):
                        parts = [p for p in raw_class.split(b"\0") if p]
                        if parts:
                            wm_class = parts[-1].decode("utf-8", "replace")
        finally:
            display_handle.close()
    except Exception:
        window_id = 0
    if not pid and not wm_class:
        try:
            if shutil.which("xdotool") and shutil.which("xprop"):
                active = subprocess.run(
                    ["xdotool", "getactivewindow"], capture_output=True,
                    text=True, timeout=0.6, check=False)
                raw_id = active.stdout.strip()
                if raw_id.isdigit():
                    window_id = int(raw_id)
                    props = subprocess.run(
                        ["xprop", "-id", raw_id, "_NET_WM_PID", "WM_CLASS"],
                        capture_output=True, text=True, timeout=0.6,
                        check=False)
                    for line in props.stdout.splitlines():
                        if line.startswith("_NET_WM_PID"):
                            digits = "".join(ch for ch in line if ch.isdigit())
                            if digits:
                                pid = int(digits)
                        elif line.startswith("WM_CLASS"):
                            values = line.split("=", 1)[-1].split(",")
                            if values:
                                wm_class = values[-1].strip().strip('"')
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    process_name = _process_name_from_pid(pid)
    if process_name:
        return process_name
    if wm_class:
        names = normalize_app_list([wm_class])
        return names[0] if names else ""
    return ""


def foreground_app_name(refresh: bool = False) -> str:
    """Cached active-window process name for the optional app filter."""
    global _FOREGROUND_CACHE
    now = time.monotonic()
    with _FOREGROUND_CACHE_LOCK:
        checked, cached = _FOREGROUND_CACHE
        if not refresh and now - checked < 0.15:
            return cached
    name = _query_foreground_app_name()
    with _FOREGROUND_CACHE_LOCK:
        _FOREGROUND_CACHE = (time.monotonic(), name)
    return name


def portable_status() -> dict:
    """Describe the active settings store and whether migration is pending."""
    enabled = portable_mode()
    target = settings_path()
    normal = _normal_settings_path()
    warning = ""
    if enabled and not target.is_file() and normal.is_file():
        warning = ("Portable mode could not copy your normal settings; "
                   "this session is starting with defaults.")
    return {"enabled": enabled, "path": target,
            "normal_path": normal, "exists": target.is_file(),
            "warning": warning}


def _xlib_available() -> bool:
    try:
        from Xlib import display
        return hasattr(display, "Display")
    except Exception:
        return False


def x11_capabilities() -> dict:
    """What the current display/tool set can support, without probing a window."""
    display = bool(os.environ.get("DISPLAY"))
    xlib = _xlib_available()
    xdotool = bool(shutil.which("xdotool"))
    xprop = bool(shutil.which("xprop"))
    wmctrl = bool(shutil.which("wmctrl"))
    return {
        "display": display,
        "xlib": xlib,
        "xdotool": xdotool,
        "xprop": xprop,
        "wmctrl": wmctrl,
        "foreground": display and (xlib or (xdotool and xprop)),
        "geometry": display and (xlib or xdotool),
        "above": display and (xlib or wmctrl),
        "foreground_backend": ("xlib" if xlib else
                              "xdotool+xprop" if xdotool and xprop else ""),
        "geometry_backend": ("xlib" if xlib else
                             "xdotool" if xdotool else ""),
        "above_backend": ("wmctrl" if wmctrl else
                          "xlib" if xlib else ""),
    }


def x11_window_geometry(window_id: Any) -> tuple[int, int, int, int] | None:
    """Return root coordinates and size for an X11 window, or None."""
    try:
        from Xlib import display
        handle = display.Display()
        try:
            root = handle.screen().root
            window = handle.create_resource_object("window", int(window_id))
            coords = root.translate_coords(window, 0, 0)
            geometry = window.get_geometry()
            return (int(coords.x), int(coords.y),
                    int(geometry.width), int(geometry.height))
        finally:
            handle.close()
    except Exception:
        return None


def x11_configure_window(window_id: Any, x: int, y: int,
                         width: int, height: int) -> bool:
    """Move/resize a window without requiring xdotool."""
    try:
        from Xlib import display
        handle = display.Display()
        try:
            window = handle.create_resource_object("window", int(window_id))
            window.configure(x=int(x), y=int(y), width=max(1, int(width)),
                             height=max(1, int(height)))
            handle.flush()
            return True
        finally:
            handle.close()
    except Exception:
        return False


def x11_set_window_above(window_id: Any, enabled: bool) -> bool:
    """Set EWMH _NET_WM_STATE_ABOVE without requiring wmctrl."""
    try:
        from Xlib import X, display, protocol
        handle = display.Display()
        try:
            window = handle.create_resource_object("window", int(window_id))
            root = window.query_tree().root
            state = handle.intern_atom("_NET_WM_STATE")
            above = handle.intern_atom("_NET_WM_STATE_ABOVE")
            action = 1 if enabled else 0
            event = protocol.event.ClientMessage(
                window=window, client_type=state,
                data=(32, [action, above, 0, 1, 0]))
            root.send_event(
                event, event_mask=(X.SubstructureRedirectMask
                                   | X.SubstructureNotifyMask))
            handle.flush()
            return True
        finally:
            handle.close()
    except Exception:
        return False


def normalize_hotkey(value: Any) -> str:
    """Canonical hotkey text (``ctrl+shift+f8``); empty means disabled.

    The editor accepts the common forms people actually type.  Pynput's
    wire format is produced separately by :func:`pynput_hotkey`.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip().lower().replace("<", "").replace(">", "")
    raw = raw.replace(" ", "")
    if not raw or raw in ("none", "off"):
        return ""
    aliases = {
        "control": "ctrl", "option": "alt", "meta": "cmd", "super": "cmd",
        "win": "cmd", "windows": "cmd", "escape": "esc", "return": "enter",
        "page_up": "pageup", "page_down": "pagedown",
        "mouse1": "mouseleft", "mouse2": "mousemiddle", "mouse3": "mouseright",
        "mousebutton4": "mouse4", "mousebutton5": "mouse5",
    }
    modifiers = {"ctrl", "shift", "alt", "cmd"}
    special = {
        "space", "tab", "enter", "esc", "backspace", "delete", "insert",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
    }
    tokens = [part for part in raw.split("+") if part]
    if not tokens:
        return ""
    mods: list[str] = []
    key = ""
    for token in tokens:
        token = aliases.get(token, token)
        if token in modifiers:
            if token not in mods:
                mods.append(token)
        elif key:
            return ""
        else:
            key = token
    if not key:
        return ""
    # pynput's Key enum exposes F1-F20 on this backend; F21-F24 normalize to
    # names it cannot parse, which used to make a saved binding fail only when
    # the listener started.  Reject them at the editor boundary instead.
    is_fkey = (len(key) >= 2 and key[0] == "f" and key[1:].isdigit()
               and 1 <= int(key[1:]) <= 20)
    mouse_keys = {"mouseleft", "mouseright", "mousemiddle", "mouse4", "mouse5"}
    if key in mouse_keys and mods:
        return ""
    if not (is_fkey or key in special or key in mouse_keys
            or (len(key) == 1 and key.isalnum())):
        return ""
    order = [mod for mod in ("ctrl", "shift", "alt", "cmd") if mod in mods]
    return "+".join(order + [key])


def pynput_hotkey(value: Any) -> str:
    """Translate canonical hotkey text to pynput GlobalHotKeys syntax."""
    canonical = normalize_hotkey(value)
    if not canonical or canonical.startswith("mouse"):
        return ""
    out = []
    for token in canonical.split("+"):
        if token in ("ctrl", "shift", "alt", "cmd"):
            out.append("<%s>" % token)
        elif len(token) == 1:
            out.append(token)
        else:
            out.append("<%s>" % token)
    return "+".join(out)


def validate_hotkeys(values: Mapping[str, Any]) -> tuple[dict | None, str]:
    """Normalise every action binding and reject collisions."""
    normalized: dict[str, str] = {}
    seen: dict[str, str] = {}
    for action, default in HOTKEY_DEFAULTS.items():
        raw = values.get(action, default)
        canonical = normalize_hotkey(raw)
        if raw not in (None, "") and not canonical:
            return None, "Invalid keybinding for %s." % HOTKEY_LABELS[action]
        if canonical == "esc":
            return None, "Esc is reserved as the emergency stop."
        if canonical:
            if canonical in seen:
                return None, ("%s and %s both use %s." %
                              (HOTKEY_LABELS[seen[canonical]],
                               HOTKEY_LABELS[action], canonical))
            seen[canonical] = action
        normalized[action] = canonical
    return normalized, ""


def normalize_settings(raw: Any) -> dict:
    """Return a complete, clamped studio settings tree.

    This is the trust boundary for disk data.  Missing/truncated/legacy files
    become defaults; individual out-of-range values become the nearest safe
    value; a malformed nested section becomes its default section.  Nothing in
    the returned tree can raise when a GTK control or engine reads it.
    """
    source = dict(raw) if isinstance(raw, Mapping) else {}
    if _legacy_shape(source):
        source = {}

    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if isinstance(source.get("onboarded"), bool):
        settings["onboarded"] = source["onboarded"]
    if source.get("appearance") == "dark":
        settings["appearance"] = "dark"
    if isinstance(source.get("accent"), str):
        accent = source["accent"].strip().lower()
        settings["accent"] = accent if accent in ACCENTS else "cyan"
    if isinstance(source.get("monochrome"), bool):
        settings["monochrome"] = source["monochrome"]
    settings["opacity"] = _number(source.get("opacity"), 92, 20, 100,
                                  integer=True)
    if isinstance(source.get("stop_on_close"), bool):
        settings["stop_on_close"] = source["stop_on_close"]
    # The rotation is a macro, so it gets a master switch like the old deck.
    # It starts disabled and is opt-in, never silently on.
    if isinstance(source.get("macro_enabled"), bool):
        settings["macro_enabled"] = source["macro_enabled"]

    clicker = _mapping(source.get("clicker"))
    clicker_cps = _number(clicker.get("cps"), 12, CLICKER_CPS_MIN,
                          CLICKER_CPS_MAX)
    clicker_cps_max = _number(clicker.get("cps_max"), 14, CLICKER_CPS_MIN,
                              CLICKER_CPS_MAX)
    if clicker_cps_max < clicker_cps:
        clicker_cps_max = clicker_cps
    settings["clicker"] = {
        "cps": clicker_cps,
        "duty": _number(clicker.get("duty"), 50, CLICKER_DUTY_MIN,
                        CLICKER_DUTY_MAX),
        "button": _choice(clicker.get("button"),
                          ("left", "right", "middle"), "left"),
        "mode": _choice(clicker.get("mode"), ("toggle", "hold"), "toggle"),
        "duration": _number(clicker.get("duration"), 0, 0,
                            CLICKER_DURATION_MAX),
        "pointer_mode": _choice(clicker.get("pointer_mode"),
                                CLICKER_POINTER_MODES, "follow"),
        "jitter": _number(clicker.get("jitter"), 0, 0, CLICKER_JITTER_MAX,
                          integer=True),
        "fixed_x": _number(clicker.get("fixed_x"), 0, -10000, 10000,
                           integer=True),
        "fixed_y": _number(clicker.get("fixed_y"), 0, -10000, 10000,
                           integer=True),
        "failsafe": (clicker.get("failsafe")
                     if isinstance(clicker.get("failsafe"), bool) else True),
        "failsafe_corner": _choice(clicker.get("failsafe_corner"),
                                   CLICKER_FAILSAFE_CORNERS, "top-left"),
        "pause_on_lock": (clicker.get("pause_on_lock")
                          if isinstance(clicker.get("pause_on_lock"), bool)
                          else True),
        "rate_mode": _choice(clicker.get("rate_mode"), CLICKER_RATE_MODES,
                             "exact"),
        "cps_max": clicker_cps_max,
        "start_delay_ms": _number(clicker.get("start_delay_ms"), 0, 0,
                                  CLICKER_START_DELAY_MAX, integer=True),
        "stop_after_clicks": _number(clicker.get("stop_after_clicks"), 0, 0,
                                     CLICKER_STOP_CLICKS_MAX, integer=True),
        "timing_precision": _choice(clicker.get("timing_precision"),
                                    CLICKER_TIMING_PRECISIONS, "balanced"),
        "failsafe_margin": _number(clicker.get("failsafe_margin"), 2, 1,
                                   CLICKER_FAILSAFE_MARGIN_MAX, integer=True),
        "app_filter_mode": _choice(clicker.get("app_filter_mode"),
                                   CLICKER_APP_FILTER_MODES, "off"),
        "app_blacklist": normalize_app_list(clicker.get("app_blacklist")),
        "app_whitelist": normalize_app_list(clicker.get("app_whitelist")),
    }

    rotation = _mapping(source.get("rotation"))
    settings["rotation"] = {
        "slot_a": _number(rotation.get("slot_a"), 1, 0, 10, integer=True),
        "slot_b": _number(rotation.get("slot_b"), 2, 0, 10, integer=True),
        "delay_a": _number(rotation.get("delay_a"), 140, 0, 5000,
                          integer=True),
        "delay_b": _number(rotation.get("delay_b"), 140, 0, 5000,
                          integer=True),
        "mode": _choice(rotation.get("mode"), ("toggle", "hold"), "toggle"),
        "max_rot": _number(rotation.get("max_rot"), 0, 0, 999, integer=True),
    }

    socd = _mapping(source.get("socd"))
    settings["socd"] = {
        "mode_h": _choice(socd.get("mode_h", "Neutral"),
                          SOCD_MODE_ALIASES, "Neutral"),
        "mode_v": _choice(socd.get("mode_v", "Neutral"),
                          SOCD_MODE_ALIASES, "Neutral"),
    }
    settings["input_lab"] = input_lab.normalize_input_lab(
        source.get("input_lab"))

    alts = _mapping(source.get("alts"))
    settings["alts"] = {
        "cpu_quota": _number(alts.get("cpu_quota"), 0, 0, 1600,
                             integer=True),
        "auto_tile": (alts.get("auto_tile")
                      if isinstance(alts.get("auto_tile"), bool) else True),
        "fps_cap": _number(alts.get("fps_cap"), ALT_FPS_DEFAULT, 0, 1000000,
                           integer=True),
        "low_graphics": (alts.get("low_graphics")
                         if isinstance(alts.get("low_graphics"), bool) else True),
        "tiling_style": _choice(alts.get("tiling_style"),
                                tuple(ALT_TILING_LABELS), "columns"),
    }

    launch_effects = _mapping(source.get("gamemode"))
    settings["gamemode"] = {}
    for key in ("mangohud", "gamemode", "gamescope"):
        default = GAMESCOPE_DEFAULT if key == "gamescope" else False
        settings["gamemode"][key] = (
            launch_effects.get(key)
            if isinstance(launch_effects.get(key), bool) else default)

    macro_settings = _mapping(source.get("macros"))
    settings["macros"] = {
        "speed": _number(macro_settings.get("speed"), 1.0, 0.1, 10.0),
        "loop_mode": _choice(macro_settings.get("loop_mode"),
                             ("once", "count", "continuous"), "once"),
        "repeat": _number(macro_settings.get("repeat"), 1, 1, 100000,
                          integer=True),
        "selected": (str(macro_settings.get("selected") or "").strip()[:64]),
    }

    recorder_settings = _mapping(source.get("recorder"))
    if screen_recorder is not None:
        settings["recorder"] = screen_recorder.normalize_settings(
            recorder_settings)
    else:
        settings["recorder"] = copy.deepcopy(DEFAULT_SETTINGS["recorder"])

    settings["launch_mode"] = _choice(source.get("launch_mode"),
                                      ("player", "plain"), "player")
    places = []
    raw_places = source.get("places")
    if isinstance(raw_places, list):
        for item in raw_places:
            if not isinstance(item, Mapping):
                continue
            place_id = str(item.get("id") or "").strip()
            if not place_id.isdigit():
                continue
            places.append({
                "id": place_id,
                "server": str(item.get("server") or "").strip()[:64],
                "name": str(item.get("name") or "").strip()[:80],
                "fav": bool(item.get("fav")),
                "ts": _number(item.get("ts"), 0, 0, 10 ** 12),
            })
    settings["places"] = places[:80]
    settings["nav_compact"] = (source.get("nav_compact")
                               if isinstance(source.get("nav_compact"), bool)
                               else False)
    settings["always_on_top"] = (
        source.get("always_on_top")
        if isinstance(source.get("always_on_top"), bool) else False)
    window = _mapping(source.get("window"))
    settings["window"] = {
        "x": _optional_coord(window.get("x")),
        "y": _optional_coord(window.get("y")),
        "width": _number(window.get("width"), 1180, 900, 10000,
                          integer=True),
        "height": _number(window.get("height"), 780, 620, 10000,
                           integer=True),
        "maximized": (window.get("maximized")
                      if isinstance(window.get("maximized"), bool) else False),
    }

    hotkeys = _mapping(source.get("hotkeys"))
    settings["hotkeys"] = {}
    for action, default in HOTKEY_DEFAULTS.items():
        raw = hotkeys.get(action, default)
        normalized = normalize_hotkey(raw)
        if normalized and normalized != "esc":
            settings["hotkeys"][action] = normalized
        elif isinstance(raw, str) and raw.strip().lower() in ("", "none", "off"):
            settings["hotkeys"][action] = ""
        else:
            settings["hotkeys"][action] = default
    return settings


def load_settings(path: str | os.PathLike | None = None) -> dict:
    target = Path(path) if path else settings_path()
    if portable_mode() and target == settings_path():
        _migrate_portable_settings(target)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    settings = normalize_settings(raw)
    # The rose token was the old default.  Move files that predate the navy
    # glass redesign to its cyan signal once, without overriding a colour the
    # user chose after this revision was written.
    if (isinstance(raw, Mapping)
            and raw.get("accent") == "rose"
            and "appearance_revision" not in raw):
        settings["accent"] = "cyan"
    return settings


def save_settings(data: Mapping[str, Any], path: str | os.PathLike | None = None) -> Path:
    target = Path(path) if path else settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(normalize_settings(data), indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return target


def clicker_stats_path(settings_file: str | os.PathLike | None = None) -> Path:
    """Session totals live beside the settings file they belong to."""
    target = Path(settings_file) if settings_file else settings_path()
    return target.with_name("clicker-stats.json")


def normalize_clicker_stats(raw: Any) -> dict:
    """Bound persisted counters so a damaged file cannot poison the UI."""
    source = raw if isinstance(raw, Mapping) else {}
    stats = {"version": CLICKER_STATS_VERSION}
    for key in ("total_clicks", "sessions", "last_session_clicks",
                "last_session_seconds", "best_session_clicks"):
        stats[key] = int(_number(source.get(key), 0, 0, CLICKER_STATS_MAX,
                                 integer=True))
    return stats


def load_clicker_stats(path: str | os.PathLike) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    return normalize_clicker_stats(raw)


def save_clicker_stats(stats: Mapping[str, Any], path: str | os.PathLike) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(normalize_clicker_stats(stats),
                         indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    _claim_owner(target)
    return target


class CountingSink(engine.InputSink if engine else object):
    """Input sink that also counts completed clicks for the dashboard."""

    def __init__(self) -> None:
        super().__init__()
        self.clicks = 0
        self._pressed = False
        self._lock = threading.Lock()

    def press_mouse(self, button: str) -> None:
        super().press_mouse(button)
        with self._lock:
            self._pressed = True

    def release_mouse(self, button: str | None = None) -> None:
        super().release_mouse(button)
        with self._lock:
            if self._pressed and button is not None:
                self.clicks += 1
                self._pressed = False


def validate_clicker(cps: Any, duty: Any, button: Any, *,
                     duration: Any = 0, pointer_mode: Any = "follow",
                     jitter: Any = 0, fixed_x: Any = 0, fixed_y: Any = 0,
                     failsafe: Any = True,
                     failsafe_corner: Any = "top-left",
                     pause_on_lock: Any = True, rate_mode: Any = "exact",
                     cps_max: Any = None, start_delay_ms: Any = 0,
                     stop_after_clicks: Any = 0,
                     timing_precision: Any = "balanced",
                     failsafe_margin: Any = 2,
                     app_filter_mode: Any = "off",
                     app_blacklist: Any = None,
                     app_whitelist: Any = None) -> tuple[dict | None, str]:
    try:
        cps_f = float(cps)
    except (TypeError, ValueError):
        return None, "Clicks per second must be a number."
    if not (math.isfinite(cps_f)
            and CLICKER_CPS_MIN <= cps_f <= CLICKER_CPS_MAX):
        return None, ("Clicks per second must stay between %g and %g."
                      % (CLICKER_CPS_MIN, CLICKER_CPS_MAX))
    try:
        duty_f = float(duty)
    except (TypeError, ValueError):
        return None, "Hold time must be a number."
    if not (math.isfinite(duty_f)
            and CLICKER_DUTY_MIN <= duty_f <= CLICKER_DUTY_MAX):
        return None, ("Hold time must stay between %g%% and %g%%."
                      % (CLICKER_DUTY_MIN, CLICKER_DUTY_MAX))
    if button not in ("left", "right", "middle"):
        return None, "Choose the left, right or middle mouse button."
    try:
        duration_f = float(duration)
    except (TypeError, ValueError):
        return None, "Duration must be a number of seconds (0 = infinite)."
    if not (math.isfinite(duration_f)
            and 0 <= duration_f <= CLICKER_DURATION_MAX):
        return None, ("Duration must stay between 0 and %g seconds "
                      "(0 = infinite)." % CLICKER_DURATION_MAX)
    mode = str(pointer_mode or "follow").strip().lower()
    if mode not in CLICKER_POINTER_MODES:
        return None, "Choose follow, fixed or jitter pointer mode."
    try:
        jitter_i = int(jitter)
    except (TypeError, ValueError):
        return None, "Jitter must be a whole number of pixels."
    if not 0 <= jitter_i <= CLICKER_JITTER_MAX:
        return None, "Jitter must stay between 0 and %d pixels." % CLICKER_JITTER_MAX
    try:
        fixed_x_i = int(fixed_x)
        fixed_y_i = int(fixed_y)
    except (TypeError, ValueError):
        return None, "Fixed coordinates must be whole numbers."
    if not (-10000 <= fixed_x_i <= 10000 and -10000 <= fixed_y_i <= 10000):
        return None, "Fixed coordinates must stay between -10000 and 10000."
    corner = str(failsafe_corner or "top-left").strip().lower()
    if corner not in CLICKER_FAILSAFE_CORNERS:
        return None, "Choose a valid failsafe corner (or off)."
    rate = str(rate_mode or "exact").strip().lower()
    if rate not in CLICKER_RATE_MODES:
        return None, "Choose exact CPS or a CPS range."
    try:
        cps_max_f = float(cps_f if cps_max is None else cps_max)
    except (TypeError, ValueError):
        return None, "Maximum CPS must be a number."
    if not (math.isfinite(cps_max_f)
            and CLICKER_CPS_MIN <= cps_max_f <= CLICKER_CPS_MAX):
        return None, ("Maximum CPS must stay between %g and %g."
                      % (CLICKER_CPS_MIN, CLICKER_CPS_MAX))
    if cps_max_f < cps_f:
        if rate == "range":
            return None, "Maximum CPS must not be lower than the minimum CPS."
        cps_max_f = cps_f
    try:
        delay_i = int(start_delay_ms)
    except (TypeError, ValueError):
        return None, "Start delay must be a whole number of milliseconds."
    if not 0 <= delay_i <= CLICKER_START_DELAY_MAX:
        return None, ("Start delay must stay between 0 and %d milliseconds."
                      % CLICKER_START_DELAY_MAX)
    try:
        stop_clicks_i = int(stop_after_clicks)
    except (TypeError, ValueError):
        return None, "Stop-after clicks must be a whole number (0 = off)."
    if not 0 <= stop_clicks_i <= CLICKER_STOP_CLICKS_MAX:
        return None, ("Stop-after clicks must stay between 0 and %d "
                      "(0 = never)." % CLICKER_STOP_CLICKS_MAX)
    precision = str(timing_precision or "balanced").strip().lower()
    if precision not in CLICKER_TIMING_PRECISIONS:
        return None, "Choose efficient, balanced or precise timing."
    try:
        margin_i = int(failsafe_margin)
    except (TypeError, ValueError):
        return None, "Failsafe size must be a whole number of pixels."
    if not 1 <= margin_i <= CLICKER_FAILSAFE_MARGIN_MAX:
        return None, ("Failsafe size must stay between 1 and %d pixels."
                      % CLICKER_FAILSAFE_MARGIN_MAX)
    filter_mode = str(app_filter_mode or "off").strip().lower()
    if filter_mode not in CLICKER_APP_FILTER_MODES:
        return None, "Choose off, blacklist or whitelist app filtering."
    return {"cps": cps_f, "duty": duty_f, "button": button,
            "duration": duration_f, "pointer_mode": mode,
            "jitter": jitter_i, "fixed_x": fixed_x_i, "fixed_y": fixed_y_i,
            "failsafe": bool(failsafe), "failsafe_corner": corner,
            "pause_on_lock": bool(pause_on_lock), "rate_mode": rate,
            "cps_max": cps_max_f, "start_delay_ms": delay_i,
            "stop_after_clicks": stop_clicks_i,
            "timing_precision": precision,
            "failsafe_margin": margin_i, "app_filter_mode": filter_mode,
            "app_blacklist": normalize_app_list(app_blacklist),
            "app_whitelist": normalize_app_list(app_whitelist)}, ""


def validate_rotation(values: Mapping[str, Any]) -> tuple[dict | None, str]:
    if engine is None:
        return None, "The input engine is unavailable."
    return engine.build_rotation(
        values.get("slot_a", 1),
        values.get("slot_b", 2),
        values.get("delay_a", 140),
        values.get("delay_b", 140),
        values.get("mode", "toggle"),
        values.get("max_rot", 0),
    )


class StudioService:
    """Lifecycle owner for the studio.  Safe to drive from the GTK thread."""

    def __init__(self, path: str | os.PathLike | None = None,
                 sink_factory: Callable[[], Any] | None = None,
                 emit: Callable[[], None] | None = None,
                 auto_start: bool = False,
                 macro_recorder_factory: Callable[..., Any] | None = None,
                 macro_store: Any = None,
                 macro_sink_factory: Callable[[], Any] | None = None,
                 recorder_factory: Callable[[], Any] | None = None) -> None:
        self.path = Path(path) if path else settings_path()
        self.stats_path = clicker_stats_path(self.path)
        self.settings = load_settings(self.path)
        self._clicker_stats = load_clicker_stats(self.stats_path)
        self._privacy_toggle_callback: Callable[[], Any] | None = None
        self._sink_factory = sink_factory
        self._emit_cb = emit
        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._ac_stop: threading.Event | None = None
        self._ac_thread: threading.Thread | None = None
        self._ac_sink: CountingSink | None = None
        self._ac_lock = None
        self._ac_started = 0.0
        self._ac_start_delay = 0.0
        self._ac_error = ""
        self._ac_paused = False
        self._ac_blocked = ""
        self._ac_last_elapsed = 0.0
        self._ac_last_clicks = 0
        self._session_locked_cache = False
        self._session_locked_checked = 0.0
        self._rot_stop: threading.Event | None = None
        self._rot_thread: threading.Thread | None = None
        self._rot_sink = None
        self._rot_started = 0.0
        self._rot_done = 0
        self._rot_error = ""
        self._socd = None
        self._socd_error = ""
        self._input_lab_profile = None
        self._input_lab_profile_error = ""
        self._input_lab_error = ""
        self._input_lab_caps_cache = None
        self._input_lab_caps_checked = 0.0
        self._controller = None
        self._controller_error = ""
        self._visualizer = input_lab.VisualizerCapture(
            on_key=self._visualizer_key,
            on_click=self._visualizer_click)
        self._visualizer_listeners: list[Callable] = []
        self._visualizer_error = ""
        self._sober = None
        self._sober_error = ""
        self._sober_started = 0.0
        self._sober_maintenance = False
        self._sober_launching = False
        self._sober_detail = ""
        self._sober_running_cache = None
        self._sober_running_checked = 0.0
        self._lution_runtime_cache = None
        self._lution_runtime_dirty = True
        self._lution_allowlist_cache = None
        self._gamemode_status_cache = None
        self._gamemode_status_checked = 0.0
        self._alt_install = None
        self._alt_manager = None
        self._alt_error = ""
        self._alt_scanning = False
        self._alt_scanned = False
        self._alt_thread: threading.Thread | None = None
        self._alt_tile_thread: threading.Thread | None = None
        self._alt_tile_generation = 0
        self._sober_thread: threading.Thread | None = None
        self._profiles: list[str] = []
        self._recent: list[str] = []
        self._hotkeys = None
        self._mouse_hotkeys = None
        self._hotkeys_enabled = False
        self.hotkey_error = ""
        self._hold_actions: set[str] = set()
        self._hotkey_combo_keys: dict[str, set] = {}
        self._macro_recorder = None
        self._macro_recorder_factory = macro_recorder_factory
        self._macro_sink_factory = macro_sink_factory
        self._macro_player = None
        self._macro_thread: threading.Thread | None = None
        self._macro_sink = None
        self._macro_stop: threading.Event | None = None
        self._macro_mode = ""
        self._macro_error = ""
        self._macro_notice = ""
        self._macro_doc: dict | None = None
        self._macro_pending_name = ""
        self._macro_started = 0.0
        self._macro_step = 0
        self._macro_loop_done = 0
        self._macro_last_emit = 0.0
        self._macro_watch_stop: threading.Event | None = None
        self._macro_watch_thread: threading.Thread | None = None
        if macro_store is not None:
            self._macro_store = macro_store
        elif macros is not None:
            self._macro_store = macros.MacroStore(
                self.clicker_profile_dir() / "macros")
        else:
            self._macro_store = None
        if screen_recorder is not None:
            self._recorder = (recorder_factory() if recorder_factory is not None
                              else screen_recorder.Recorder(
                                  settings_getter=lambda: self.settings["recorder"],
                                  session_guard=self._session_locked,
                                  emit=self._emit))
        else:
            self._recorder = None

    @staticmethod
    def _elevated() -> bool:
        return os.geteuid() == 0

    def _alt_env(self):
        """Environment for Cordial itself, owned by the desktop user."""
        owner = _desktop_owner()
        return _lution_bridge_env(owner) if owner is not None else None

    def _alt_store(self):
        """The profile name index, always written for the desktop owner."""
        owner = _desktop_owner()
        env = self._alt_env()
        return roalt.ProfileStore(
            path=roalt.default_store_path(env),
            owner_uid=owner[0] if owner is not None else None)

    def _lution_cli(self, op: str, payload: Any = None,
                    args: tuple = (), *, timeout: float = 45.0) -> dict:
        return run_lution_cli(op, payload=payload, args=args,
                              timeout=timeout)

    # -- listeners ---------------------------------------------------------
    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _emit(self) -> None:
        if self._emit_cb is not None:
            try:
                self._emit_cb()
                return
            except Exception:
                traceback.print_exc()
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                traceback.print_exc()

    # -- global hotkeys ----------------------------------------------------
    def set_privacy_toggle(self, callback: Callable[[], Any] | None) -> None:
        """Let the window receive the Hide UI action from the global listener."""
        self._privacy_toggle_callback = callback

    def toggle_privacy(self) -> tuple[bool, str]:
        """Toggle the window's display-only privacy mask from a hotkey."""
        callback = self._privacy_toggle_callback
        if callback is None:
            return False, "Hide UI is only available while the window is open."
        try:
            result = callback()
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1])
        return True, "Interface values toggled."

    def hotkey_mapping(self) -> dict:
        """Return the current action -> canonical key map."""
        raw = _mapping(self.settings.get("hotkeys"))
        mapping = {}
        for action, default in HOTKEY_DEFAULTS.items():
            mapping[action] = normalize_hotkey(raw.get(action, default))
        return mapping

    def hotkey_wire_mapping(self) -> dict:
        """Pynput bindings for the current settings, plus reserved ESC."""
        mapping = {}
        for action, canonical in self.hotkey_mapping().items():
            wire = pynput_hotkey(canonical)
            if wire:
                mapping[wire] = (lambda a=action: self._hotkey_activated(a))
        # ESC is reserved in the old deck and remains a real emergency stop
        # even if the user rebinds Stop everything to another key.
        mapping.setdefault("<esc>",
                           lambda: self.handle_hotkey("stop_all"))
        return mapping

    def _hotkey_activated(self, action: str) -> None:
        """A global combo was pressed.  Hold modes start on the press."""
        if action == "toggle_clicker" and self.settings["clicker"].get(
                "mode") == "hold":
            if not self.ac_running:
                ok, _ = self.start_clicker()
            else:
                ok = True
            if ok:
                self._hold_actions.add(action)
            return
        if action == "toggle_rotation" and self.settings["rotation"].get(
                "mode") == "hold":
            if not self.macro_enabled:
                return
            if not self.rot_running:
                ok, _ = self.start_rotation()
            else:
                ok = True
            if ok:
                self._hold_actions.add(action)
            return
        self.handle_hotkey(action)

    def _hotkey_released(self, canonical_key) -> None:
        """Release of any key in a held combo stops that held engine."""
        try:
            for action in tuple(self._hold_actions):
                combo = self._hotkey_combo_keys.get(action, set())
                if canonical_key not in combo:
                    continue
                self._hold_actions.discard(action)
                if action == "toggle_clicker":
                    self.stop_clicker()
                elif action == "toggle_rotation":
                    self.stop_rotation()
        except Exception as exc:
            self._ac_error = "Hotkey release failed: %s" % exc
            traceback.print_exc()
            self._emit()

    def _mouse_hotkey_event(self, action: str, canonical: str,
                            pressed: bool) -> None:
        """Bridge a global mouse button to the same press/release path."""
        token = "mouse:" + canonical
        if pressed:
            if action in self._hold_actions:
                return
            self._hotkey_activated(action)
            if self.settings["clicker"].get("mode") == "hold" \
                    and action == "toggle_clicker":
                self._hotkey_combo_keys[action] = {token}
            elif self.settings["rotation"].get("mode") == "hold" \
                    and action == "toggle_rotation":
                self._hotkey_combo_keys[action] = {token}
            return
        self._hotkey_released(token)

    def start_hotkeys(self) -> tuple[bool, str]:
        """Bind the user's (or default) global hotkeys.

        Any combination of ctrl/shift/alt/cmd plus F1–F20, letters, digits or
        named keys is accepted.  Empty bindings are ignored.  ESC is always
        present as the emergency stop.  Hold-mode clicker/rotation bindings
        stop when any key in their combination is released.
        """
        if self._hotkeys is not None or self._mouse_hotkeys is not None:
            return True, "Hotkeys are already listening."
        self._hotkeys_enabled = True
        try:
            _, conflict = validate_hotkeys(self.settings.get("hotkeys", {}))
            if conflict:
                self._hotkeys_enabled = False
                self.hotkey_error = conflict
                return False, conflict
            from pynput import keyboard
            mapping = self.hotkey_wire_mapping()
            mouse_bindings = {
                canonical: action
                for action, canonical in self.hotkey_mapping().items()
                if canonical.startswith("mouse")
            }
            if not mapping and not mouse_bindings:
                self._hotkeys_enabled = False
                self.hotkey_error = "No global hotkeys are configured."
                return False, self.hotkey_error
            combo_keys = {}
            for action, canonical in self.hotkey_mapping().items():
                wire = pynput_hotkey(canonical)
                if wire:
                    combo_keys[action] = set(keyboard.HotKey.parse(wire))
            self._hotkey_combo_keys = combo_keys
            self._hold_actions.clear()

            class HoldAwareHotKeys(keyboard.GlobalHotKeys):
                def _on_release(self, key, injected):
                    if not injected:
                        self_service._hotkey_released(self.canonical(key))
                    super()._on_release(key, injected)

            self_service = self
            listener = HoldAwareHotKeys(mapping)
            listener.daemon = True
            listener.start()
            # pynput's start() returns once the thread exists, but XRecord may
            # not be enabled yet.  Do not report a live listener until it is
            # actually ready, otherwise a hotkey pressed immediately after
            # startup is silently missed.
            deadline = time.monotonic() + 2.0
            while (not getattr(listener, "_ready", False)
                   and listener.is_alive()
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            if not getattr(listener, "_ready", False):
                listener.stop()
                self._hotkeys_enabled = False
                self.hotkey_error = ("Global hotkeys could not listen to "
                                     "this X server.")
                return False, self.hotkey_error
            self._hotkeys = listener
            mouse_listener = None
            if mouse_bindings:
                from pynput import mouse
                by_button = {}
                for canonical, action in mouse_bindings.items():
                    attr = MOUSE_HOTKEY_BUTTONS.get(canonical)
                    button = getattr(mouse.Button, attr, None) if attr else None
                    if button is not None:
                        by_button[button] = (canonical, action)

                def on_click(_x, _y, button, pressed):
                    binding = by_button.get(button)
                    if binding is None:
                        return
                    canonical, action = binding
                    self._mouse_hotkey_event(action, canonical, bool(pressed))

                mouse_listener = mouse.Listener(on_click=on_click)
                mouse_listener.daemon = True
                mouse_listener.start()
                # As with XRecord above, thread liveness is not readiness:
                # a button pressed before the listener hooks the display is
                # lost.  Wait (bounded) for pynput to mark it ready.
                mouse_deadline = time.monotonic() + 2.0
                while (not getattr(mouse_listener, "_ready", False)
                       and mouse_listener.is_alive()
                       and time.monotonic() < mouse_deadline):
                    time.sleep(0.01)
                if (not getattr(mouse_listener, "_ready", False)
                        or not mouse_listener.is_alive()):
                    listener.stop()
                    self._hotkeys = None
                    self._hotkeys_enabled = False
                    self.hotkey_error = "Mouse hotkeys could not listen to this X server."
                    return False, self.hotkey_error
                self._mouse_hotkeys = mouse_listener
            self.hotkey_error = ""
            return True, "Hotkeys listening."
        except Exception as exc:
            self._hotkeys_enabled = False
            listener, self._hotkeys = self._hotkeys, None
            mouse_listener, self._mouse_hotkeys = self._mouse_hotkeys, None
            for active in (listener, mouse_listener):
                if active is not None:
                    try:
                        active.stop()
                    except Exception:
                        pass
            self.hotkey_error = "%s: %s" % (type(exc).__name__, exc)
            return False, self.hotkey_error

    HOTKEY_ACTIONS = dict(HOTKEY_ACTIONS)

    def handle_hotkey(self, key: str) -> tuple[bool, str]:
        """One dispatch point for actions, so order and repetition are testable
        without a physical keyboard or a live pynput listener."""
        needle = str(key).strip().lower()
        action = LEGACY_HOTKEY_ALIASES.get(needle) or HOTKEY_ACTIONS.get(needle)
        if action is None:
            return False, "Unknown hotkey."
        # F8 is SOCD's normal toggle.  While the clicker is running it is a
        # fixed panic stop even if SOCD was rebound, so the safety key works
        # without giving up SOCD when idle.
        if needle == "f8" and self.ac_running:
            self.stop_clicker()
            return True, "Clicker panic-stopped (F8)."
        if (needle == "f8" and self._recorder is not None
                and self._recorder.recording):
            self.stop_recording(wait=True)
            return True, "Recording panic-stopped (F8)."
        if needle == "f8" and self.macro_running:
            self.stop_macro(wait=True)
            return True, "Macro panic-stopped (F8)."
        try:
            result = getattr(self, action)()
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        if isinstance(result, tuple):
            return result
        return True, "Everything stopped."

    def set_hotkeys(self, values: Mapping[str, Any]) -> tuple[bool, str]:
        """Validate, persist and (if running) reload the global hotkeys."""
        normalized, error = validate_hotkeys(values)
        if error:
            return False, error
        self.settings["hotkeys"] = dict(normalized)
        self.save()
        if self._hotkeys is not None or self._hotkeys_enabled:
            self.stop_hotkeys()
            self._hotkeys_enabled = True
            ok, message = self.start_hotkeys()
            if not ok:
                return False, message
        self._emit()
        return True, "Keybindings saved."

    def restart_hotkeys(self) -> tuple[bool, str]:
        if not self._hotkeys_enabled:
            return True, "Global hotkeys are disabled."
        self.stop_hotkeys()
        self._hotkeys_enabled = True
        return self.start_hotkeys()

    def stop_hotkeys(self) -> None:
        listener, self._hotkeys = self._hotkeys, None
        mouse_listener, self._mouse_hotkeys = self._mouse_hotkeys, None
        self._hold_actions.clear()
        self._hotkey_combo_keys = {}
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        if mouse_listener is not None:
            try:
                mouse_listener.stop()
            except Exception:
                pass

    # -- settings ----------------------------------------------------------
    def save(self) -> None:
        target = save_settings(self.settings, self.path)
        # WXB normally runs elevated for uinput, but its settings are a user
        # artifact.  Hand a root-written file back so a later unprivileged
        # launch can read it instead of silently falling back to defaults.
        _claim_owner(target)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.settings[key] = value
        if save:
            self.save()
        self._emit()

    # -- launch effects ----------------------------------------------------
    GAMEMODE_KEYS = ("mangohud", "gamemode", "gamescope")

    def launch_effects(self) -> dict:
        """The three saved launch switches, independent of availability."""
        saved = _mapping(self.settings.get("gamemode"))
        defaults = DEFAULT_SETTINGS["gamemode"]
        return {key: bool(saved.get(key, defaults[key]))
                for key in self.GAMEMODE_KEYS}

    def set_gamemode_effect(self, key: str, enabled: Any) -> bool:
        if key not in self.GAMEMODE_KEYS:
            return False
        effects = self.launch_effects()
        effects[key] = bool(enabled)
        self.settings["gamemode"] = effects
        self.save()
        self._emit()
        return True

    def gamemode_status(self, refresh: bool = False) -> dict:
        """Installed capabilities plus the saved switch state.

        Detection touches the Flatpak CLI, so the capability result is cached
        for a short while; switch state is always read live.  This keeps the
        one-second page refresh cheap without making a toggle feel stale.
        """
        now = time.monotonic()
        if (self._gamemode_status_cache is None or refresh
                or now - self._gamemode_status_checked > 20.0):
            flatpak = shutil.which("flatpak")
            mangohud = False
            if flatpak:
                try:
                    listed = subprocess.run(
                        [flatpak, "list", "--runtime",
                         "--columns=application"],
                        capture_output=True, text=True, timeout=5)
                    mangohud = (listed.returncode == 0
                                and "org.freedesktop.Platform.VulkanLayer."
                                    "MangoHud" in listed.stdout)
                except (OSError, subprocess.SubprocessError):
                    mangohud = False
            gamemode_lib = (lution_launcher.find_gamemode_library()
                            if lution_launcher is not None else None)
            gamemode_daemon = bool(shutil.which("gamemoded")
                                   or Path("/usr/bin/gamemoded").exists())
            gamescope = None
            gamescope_detail = "Gamescope is not installed"
            if lution_launcher is not None:
                if hasattr(lution_launcher, "gamescope_status"):
                    gamescope, gamescope_detail = lution_launcher.gamescope_status()
                else:
                    gamescope = lution_launcher.find_gamescope()
                    gamescope_detail = ("Host Gamescope ready" if gamescope
                                        else "Host gamescope is not installed")
            self._gamemode_status_cache = {
                "mangohud": {
                    "available": bool(mangohud),
                    "detail": ("Flatpak Vulkan layer ready" if mangohud
                               else "MangoHud Flatpak layer not found"),
                },
                "gamemode": {
                    "available": bool(gamemode_lib and gamemode_daemon),
                    "detail": ("gamemoded and client library ready"
                               if gamemode_lib and gamemode_daemon
                               else "Linux GameMode is not installed"),
                },
                "gamescope": {
                    "available": bool(gamescope),
                    "detail": gamescope_detail,
                },
            }
            self._gamemode_status_checked = now
        result = copy.deepcopy(self._gamemode_status_cache)
        effects = self.launch_effects()
        for key in self.GAMEMODE_KEYS:
            result[key]["enabled"] = effects[key]
        return result

    def active_launch_effects(self) -> dict:
        """Switches to hand to the launcher, including availability guards."""
        effects = self.launch_effects()
        status = self.gamemode_status()
        return {
            key: bool(effects[key] and status[key]["available"])
            for key in self.GAMEMODE_KEYS
        }

    # -- named configs -----------------------------------------------------
    def config_dir(self) -> Path:
        return self.path.parent / "configs"

    @staticmethod
    def clean_config_name(name: Any) -> str:
        if not isinstance(name, str):
            return ""
        name = name.strip()
        if not name or name in (".", ".."):
            return ""
        if "/" in name or "\\" in name or "\x00" in name:
            return ""
        return name[:64]

    def _config_path(self, name: Any) -> Path | None:
        clean = self.clean_config_name(name)
        return self.config_dir() / (clean + ".json") if clean else None

    def list_configs(self) -> list[str]:
        directory = self.config_dir()
        try:
            return sorted(p.stem for p in directory.glob("*.json")
                          if p.is_file())
        except OSError:
            return []

    def save_config(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self._config_path(clean)
        if target is None:
            return False, "Give this config a name."
        try:
            save_settings(self.settings, target)
        except OSError as exc:
            return False, "Could not save config: %s" % exc
        self._emit()
        return True, "Saved config “%s”." % clean

    def load_config(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self._config_path(clean)
        if target is None:
            return False, "Choose a config to load."
        if not target.is_file():
            return False, "That config no longer exists."
        # A named config is a user artifact, not the primary settings file.
        # Refuse a damaged one instead of silently replacing the live setup
        # with defaults and then saving those defaults over it.
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return False, "Could not read config: %s" % exc
        if not isinstance(raw, dict):
            return False, "That config is not a settings object."
        previous = self.settings
        self.settings = normalize_settings(raw)
        try:
            self.save()
        except OSError as exc:
            self.settings = previous
            return False, "Could not apply config: %s" % exc
        if self._hotkeys is not None or self._hotkeys_enabled:
            self._hotkeys_enabled = True
            self.stop_hotkeys()
            self.start_hotkeys()
        self._emit()
        return True, "Loaded config “%s”." % clean

    def delete_config(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self._config_path(clean)
        if target is None or not target.is_file():
            return False, "That config no longer exists."
        try:
            target.unlink()
        except OSError as exc:
            return False, "Could not remove config: %s" % exc
        self._emit()
        return True, "Removed config “%s”." % clean

    def rename_config(self, old: Any, new: Any) -> tuple[bool, str]:
        source = self._config_path(old)
        target = self._config_path(new)
        clean = self.clean_config_name(new)
        if source is None or not source.is_file():
            return False, "That config no longer exists."
        if target is None:
            return False, "Give this config a name."
        if target.exists() and target != source:
            return False, "A config with that name already exists."
        try:
            source.replace(target)
        except OSError as exc:
            return False, "Could not rename config: %s" % exc
        self._emit()
        return True, "Renamed config to “%s”." % clean

    # -- clicker profiles -------------------------------------------------
    def clicker_profile_dir(self) -> Path:
        """The requested shared store: ~/.config/autoclicker/*.json."""
        root = Path(os.environ.get("XDG_CONFIG_HOME")
                    or Path.home() / ".config")
        return root / "autoclicker"

    def clicker_profile_path(self, name: Any) -> Path | None:
        clean = self.clean_config_name(name)
        return (self.clicker_profile_dir() / (clean + ".json")
                if clean else None)

    def list_clicker_profiles(self) -> list[str]:
        try:
            if self.clicker_profile_dir().is_symlink():
                return []
            return sorted(p.stem for p in self.clicker_profile_dir().glob("*.json")
                          if p.is_file() and not p.is_symlink())
        except OSError:
            return []

    def save_clicker_profile(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self.clicker_profile_path(clean)
        if target is None:
            return False, "Give this clicker profile a name."
        try:
            if target.parent.is_symlink():
                return False, ("Refusing to write through a symlinked "
                               "autoclicker directory.")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            tmp = target.with_suffix(target.suffix + ".tmp")
            payload = {"version": 1,
                       "clicker": dict(self.settings["clicker"])}
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = None
                    handle.write(json.dumps(payload, indent=2,
                                            sort_keys=True) + "\n")
            finally:
                if fd is not None:
                    os.close(fd)
            os.replace(tmp, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            return False, "Could not save clicker profile: %s" % exc
        self._emit()
        return True, "Saved clicker profile “%s”." % clean

    def load_clicker_profile(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self.clicker_profile_path(clean)
        if target is None:
            return False, "Choose a clicker profile to load."
        if target.parent.is_symlink():
            return False, "Refusing to read a symlinked autoclicker directory."
        if target.is_symlink():
            return False, "Refusing to read a symlinked clicker profile."
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None
                    raw = json.load(handle)
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            return False, "Could not read clicker profile: %s" % exc
        if not isinstance(raw, dict) or not raw:
            return False, "That clicker profile is empty or invalid."
        section = raw.get("clicker", raw)
        if not isinstance(section, dict) or not section:
            return False, "That clicker profile has no clicker settings."
        clean_cfg = normalize_settings({"clicker": section})["clicker"]
        self.settings["clicker"] = clean_cfg
        self.save()
        self._emit()
        return True, "Loaded clicker profile “%s”." % clean

    def delete_clicker_profile(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self.clicker_profile_path(clean)
        if target is None or not target.is_file():
            return False, "That clicker profile no longer exists."
        if target.parent.is_symlink():
            return False, "Refusing to delete through a symlinked autoclicker directory."
        try:
            target.unlink()
        except OSError as exc:
            return False, "Could not remove clicker profile: %s" % exc
        self._emit()
        return True, "Removed clicker profile “%s”." % clean

    # -- macros ------------------------------------------------------------
    def macro_capabilities(self) -> dict:
        if macros is None:
            return {"session": "unknown", "x11": False, "xwayland": False,
                    "record": False, "play": False, "hotkeys": False,
                    "reason": "The macro module is unavailable."}
        return macros.detect_capabilities()

    @property
    def macro_running(self) -> bool:
        return self._macro_mode in ("recording", "playing") or bool(
            self._macro_thread and self._macro_thread.is_alive())

    def macro_snapshot(self) -> dict:
        doc = self._macro_doc or {}
        events = doc.get("events") or []
        if self._macro_mode == "recording" and self._macro_recorder is not None:
            steps = int(getattr(self._macro_recorder, "event_count", 0))
            duration = max(0.0, time.monotonic() - self._macro_started) \
                if self._macro_started else 0.0
        else:
            steps = len(events)
            duration = float(doc.get("duration") or 0.0)
        return {
            "recording": self._macro_mode == "recording",
            "playing": self._macro_mode == "playing",
            "running": self.macro_running,
            "steps": steps,
            "duration": duration,
            "name": (self._macro_pending_name if self._macro_mode == "recording"
                     else str(doc.get("name") or "")),
            "file": str(self.settings["macros"].get("selected") or ""),
            "error": self._macro_error,
            "notice": self._macro_notice,
            "step": int(self._macro_step),
            "loop_done": int(self._macro_loop_done),
            "speed": float(self.settings["macros"]["speed"]),
            "loop_mode": str(self.settings["macros"]["loop_mode"]),
            "repeat": int(self.settings["macros"]["repeat"]),
            "files": self.list_macro_files(),
            "capabilities": self.macro_capabilities(),
        }

    def list_macro_files(self) -> list[str]:
        if self._macro_store is None:
            return []
        try:
            return self._macro_store.list()
        except Exception:
            return []

    def list_macro_steps(self) -> list[dict]:
        return [dict(event) for event in (self._macro_doc or {}).get("events") or []]

    def set_macro_settings(self, speed: Any = None, loop_mode: Any = None,
                           repeat: Any = None) -> tuple[bool, str]:
        current = dict(self.settings["macros"])
        if speed is not None:
            try:
                value = float(speed)
            except (TypeError, ValueError):
                return False, "Playback speed must be a number."
            if not math.isfinite(value) or not 0.1 <= value <= 10.0:
                return False, "Playback speed must be between 0.1x and 10x."
            current["speed"] = value
        if loop_mode is not None:
            if loop_mode not in ("once", "count", "continuous"):
                return False, "Choose once, a repeat count, or continuous."
            current["loop_mode"] = loop_mode
        if repeat is not None:
            try:
                count = int(repeat)
            except (TypeError, ValueError):
                return False, "Repeat count must be a whole number."
            if count < 1 or count > 100000:
                return False, "Repeat count must be between 1 and 100000."
            current["repeat"] = count
        self.settings["macros"] = current
        self.save()
        self._emit()
        return True, "Macro playback settings saved."

    def _macro_control_combos(self) -> list[set[str]]:
        combos: list[set[str]] = []
        for action in HOTKEY_DEFAULTS:
            canonical = str(self.hotkey_mapping().get(action) or "")
            combo = {token.upper() for token in canonical.split("+") if token}
            if combo:
                combos.append(combo)
        combos.append({"ESC"})
        return combos

    def _stop_macro_watch(self) -> None:
        event = self._macro_watch_stop
        self._macro_watch_stop = None
        self._macro_watch_thread = None
        if event is not None:
            event.set()

    def _start_macro_watch(self, recorder: Any) -> None:
        self._stop_macro_watch()
        stop = threading.Event()
        self._macro_watch_stop = stop

        def watch() -> None:
            last = time.monotonic()
            started = last
            while not stop.wait(0.5):
                if (self._macro_recorder is not recorder
                        or self._macro_mode != "recording"):
                    return
                now = time.monotonic()
                reason = ""
                if self._session_locked():
                    reason = "Recording stopped because the desktop locked."
                elif now - last > macros.SLEEP_GAP:
                    reason = ("Recording stopped because the desktop was "
                              "suspended or the clock jumped.")
                elif getattr(recorder, "error", ""):
                    reason = "Recording stopped: %s" % recorder.error
                elif (now - started > 1.0
                      and not getattr(recorder, "running", True)):
                    reason = "Recording stopped because an input listener exited."
                last = now
                if not reason:
                    continue
                ok, _message = self.stop_macro_recording()
                if ok:
                    self._macro_notice = reason
                    self._macro_error = ""
                self._emit()
                return

        thread = threading.Thread(target=watch, name="wxb-macro-watch",
                                  daemon=True)
        self._macro_watch_thread = thread
        thread.start()

    def _macro_record_error(self, message: str) -> None:
        self._macro_error = str(message)
        if self._macro_mode == "recording":
            self.stop_macro_recording()
            self._macro_error = str(message)
        else:
            self._macro_mode = ""
        self._emit()

    def start_macro_recording(self, name: Any = "") -> tuple[bool, str]:
        if macros is None or self._macro_store is None:
            return False, "The macro module is unavailable."
        if self.macro_running:
            return False, "Stop the current macro first."
        if self.ac_running or self.rot_running or self.socd_running:
            return False, "Stop the clicker, rotation and SOCD before recording."
        caps = self.macro_capabilities()
        if not caps.get("record"):
            return False, caps.get("reason") or "Macro recording is unavailable."
        clean = macros.clean_name(name) or time.strftime("Macro %Y-%m-%d %H:%M:%S")
        factory = self._macro_recorder_factory or macros.MacroRecorder
        try:
            recorder = factory(ignore_keys=set(),
                               control_combos=self._macro_control_combos(),
                               on_error=self._macro_record_error)
            recorder.start()
        except Exception as exc:
            self._macro_error = "Could not start recording: %s" % exc
            self._emit()
            return False, self._macro_error
        self._macro_recorder = recorder
        self._macro_pending_name = clean
        self._macro_doc = None
        self._macro_mode = "recording"
        self._macro_error = ""
        self._macro_notice = "Recording. Use the stop hotkey or Stop recording."
        self._macro_started = time.monotonic()
        self._macro_step = 0
        self._macro_loop_done = 0
        self._emit()
        self._start_macro_watch(recorder)
        return True, "Recording macro “%s”." % clean

    def stop_macro_recording(self, save: bool = True) -> tuple[bool, str]:
        recorder = self._macro_recorder
        if self._macro_mode != "recording" or recorder is None:
            return False, "No macro is being recorded."
        self._macro_recorder = None
        self._macro_mode = ""
        self._stop_macro_watch()
        try:
            events = recorder.stop()
        except Exception as exc:
            self._macro_error = "Could not stop recording: %s" % exc
            self._emit()
            return False, self._macro_error
        name = self._macro_pending_name or "Untitled macro"
        if not events:
            message = "Recording stopped with no steps."
            self._macro_notice = message
            self._emit()
            return True, message
        try:
            doc = macros.make_macro(name, events)
        except Exception as exc:
            self._macro_error = "Recorded macro is invalid: %s" % exc
            self._emit()
            return False, self._macro_error
        if save:
            ok, message, _path = self._macro_store.save(name, doc)
            if not ok:
                self._macro_error = message
                self._emit()
                return False, message
            self.settings["macros"]["selected"] = name
            self.save()
        self._macro_doc = doc
        self._macro_notice = "Recorded %d steps." % len(doc["events"])
        if recorder.error:
            self._macro_error = recorder.error
        self._emit()
        return True, self._macro_notice

    def toggle_macro_recording(self, name: Any = "") -> tuple[bool, str]:
        if self._macro_mode == "recording":
            return self.stop_macro_recording()
        if self._macro_mode == "playing" or self.macro_running:
            return False, "Stop macro playback first."
        return self.start_macro_recording(name)

    def load_macro(self, name: Any) -> tuple[bool, str]:
        if self._macro_store is None:
            return False, "The macro module is unavailable."
        if self.macro_running:
            return False, "Stop the macro before opening another file."
        doc, error = self._macro_store.load(name)
        if error:
            self._macro_error = error
            self._emit()
            return False, error
        self._macro_doc = doc
        self.settings["macros"]["selected"] = str(name)
        self.save()
        self._macro_error = ""
        self._macro_notice = "Opened %d macro steps." % len(doc["events"])
        self._emit()
        return True, self._macro_notice

    def import_macro(self, path: Any) -> tuple[bool, str]:
        if macros is None or self._macro_store is None:
            return False, "The macro module is unavailable."
        if self.macro_running:
            return False, "Stop the macro before importing another file."
        doc, error = self._macro_store.load_path(path)
        if error:
            self._macro_error = error
            self._emit()
            return False, error
        name = macros.clean_name(doc.get("name")) or "Imported macro"
        ok, message, _target = self._macro_store.save(name, doc,
                                                       overwrite=False)
        if not ok:
            self._macro_error = message
            self._emit()
            return False, message
        self._macro_doc = doc
        self.settings["macros"]["selected"] = name
        self.save()
        self._macro_error = ""
        self._macro_notice = "Imported macro “%s” (%d steps)." % (
            name, len(doc["events"]))
        self._emit()
        return True, self._macro_notice

    def save_macro(self, name: Any = "") -> tuple[bool, str]:
        if macros is None or self._macro_store is None:
            return False, "The macro module is unavailable."
        if self._macro_doc is None:
            return False, "Record or open a macro first."
        clean = macros.clean_name(name) or macros.clean_name(
            self._macro_doc.get("name"))
        if not clean:
            return False, "Give this macro a name."
        doc = copy.deepcopy(self._macro_doc)
        doc["name"] = clean
        try:
            doc = macros.validate_macro(doc)
        except Exception as exc:
            self._macro_error = "Could not validate macro: %s" % exc
            self._emit()
            return False, self._macro_error
        ok, message, _path = self._macro_store.save(clean, doc)
        if not ok:
            self._macro_error = message
            self._emit()
            return False, message
        self._macro_doc = doc
        self.settings["macros"]["selected"] = clean
        self.save()
        self._macro_error = ""
        self._macro_notice = message
        self._emit()
        return True, message

    def export_macro(self, path: Any) -> tuple[bool, str]:
        if macros is None or self._macro_store is None:
            return False, "The macro module is unavailable."
        if self._macro_doc is None:
            return False, "Record or open a macro first."
        ok, message = self._macro_store.export_file(self._macro_doc, path)
        self._macro_error = "" if ok else message
        self._macro_notice = message if ok else self._macro_notice
        self._emit()
        return ok, message

    def delete_macro(self, name: Any) -> tuple[bool, str]:
        if macros is None or self._macro_store is None:
            return False, "The macro module is unavailable."
        if self.macro_running:
            return False, "Stop the macro before deleting a file."
        target = self._macro_store.path(name)
        if target is None or not target.is_file():
            return False, "That macro no longer exists."
        try:
            target.unlink()
        except OSError as exc:
            return False, "Could not remove macro: %s" % exc
        if self.settings["macros"].get("selected") == name:
            self.settings["macros"]["selected"] = ""
            self._macro_doc = None
            self.save()
        self._macro_notice = "Removed macro “%s”." % name
        self._emit()
        return True, self._macro_notice

    def replace_macro_step(self, index: Any, event: Mapping[str, Any]) -> tuple[bool, str]:
        if macros is None or self._macro_doc is None:
            return False, "Record or open a macro first."
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return False, "Choose a macro step."
        doc = copy.deepcopy(self._macro_doc)
        events = doc.get("events") or []
        if idx < 0 or idx >= len(events):
            return False, "That macro step no longer exists."
        events[idx] = dict(event)
        try:
            self._macro_doc = macros.validate_macro(doc)
        except Exception as exc:
            return False, "Invalid macro step: %s" % exc
        self._macro_notice = "Step %d updated; save to keep it." % (idx + 1)
        self._emit()
        return True, self._macro_notice

    def delete_macro_step(self, index: Any) -> tuple[bool, str]:
        if macros is None or self._macro_doc is None:
            return False, "Record or open a macro first."
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return False, "Choose a macro step."
        doc = copy.deepcopy(self._macro_doc)
        events = doc.get("events") or []
        if idx < 0 or idx >= len(events):
            return False, "That macro step no longer exists."
        del events[idx]
        try:
            self._macro_doc = macros.validate_macro(doc)
        except Exception as exc:
            return False, "Could not delete macro step: %s" % exc
        self._macro_notice = "Step deleted; save to keep it."
        self._emit()
        return True, self._macro_notice

    def start_macro_playback(self) -> tuple[bool, str]:
        if macros is None:
            return False, "The macro module is unavailable."
        if self.macro_running:
            return False, "Stop the current macro first."
        if self.ac_running or self.rot_running or self.socd_running:
            return False, "Stop the clicker, rotation and SOCD before playback."
        if self._macro_doc is None or not self._macro_doc.get("events"):
            return False, "Record or open a macro first."
        caps = self.macro_capabilities()
        if not caps.get("play"):
            return False, caps.get("reason") or "Macro playback is unavailable."
        try:
            sink = (self._macro_sink_factory() if self._macro_sink_factory
                    else engine.InputSink())
        except Exception as exc:
            self._macro_error = "Could not open macro output: %s" % exc
            self._emit()
            return False, self._macro_error
        mode = self.settings["macros"]["loop_mode"]
        loops = 1 if mode == "once" else (
            0 if mode == "continuous" else int(self.settings["macros"]["repeat"]))
        speed = float(self.settings["macros"]["speed"])
        doc = copy.deepcopy(self._macro_doc)
        stop = threading.Event()
        self._macro_sink = sink
        self._macro_stop = stop
        self._macro_mode = "playing"
        self._macro_error = ""
        self._macro_notice = "Playing %d steps at %gx." % (
            len(doc["events"]), speed)
        self._macro_started = time.monotonic()
        self._macro_step = 0
        self._macro_loop_done = 0
        self._macro_last_emit = 0.0

        def progress(index: int, loop: int) -> None:
            self._macro_step = int(index)
            self._macro_loop_done = int(loop)
            now = time.monotonic()
            if now - self._macro_last_emit >= 0.1:
                self._macro_last_emit = now
                self._emit()

        def worker() -> None:
            player = None
            error = ""
            try:
                player = macros.MacroPlayer(
                    sink, session_guard=self._session_locked,
                    on_status=progress)
                self._macro_player = player
                done = player.play(doc, speed=speed, loops=loops, stop=stop)
                self._macro_notice = "Played %d loop%s." % (
                    done, "" if done == 1 else "s")
            except macros.MacroStopped:
                self._macro_notice = "Macro playback stopped."
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
                self._macro_error = error
            finally:
                try:
                    sink.release_all()
                except Exception as exc:
                    if not error:
                        error = "Could not release macro inputs: %s" % exc
                        self._macro_error = error
                with self._lock:
                    if self._macro_stop is stop:
                        self._macro_stop = None
                        self._macro_sink = None
                        self._macro_thread = None
                        self._macro_mode = ""
                        self._macro_player = None
                self._emit()

        thread = threading.Thread(target=worker, name="wxb-studio-macro",
                                  daemon=True)
        self._macro_thread = thread
        thread.start()
        self._emit()
        return True, self._macro_notice

    def toggle_macro_playback(self) -> tuple[bool, str]:
        if self._macro_mode == "playing":
            self.stop_macro()
            return True, "Macro playback stopped."
        return self.start_macro_playback()

    def stop_macro(self, wait: bool = False) -> tuple[bool, str]:
        if self._macro_mode == "recording":
            return self.stop_macro_recording()
        stop = self._macro_stop
        thread = self._macro_thread
        if stop is None and (thread is None or not thread.is_alive()):
            return False, "No macro is running."
        if stop is not None:
            stop.set()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        sink = self._macro_sink
        if sink is not None:
            try:
                sink.release_all()
            except Exception as exc:
                self._macro_error = "Could not release macro inputs: %s" % exc
                self._emit()
        self._macro_notice = "Macro playback stopped."
        self._emit()
        return True, self._macro_notice

    # -- screen recorder ---------------------------------------------------
    def recorder_capabilities(self) -> dict:
        if self._recorder is None or screen_recorder is None:
            return {"session": "unknown", "record": False, "audio": False,
                    "sources": [], "encoders": [],
                    "reason": "The screen recorder module is unavailable."}
        try:
            return self._recorder.capabilities()
        except Exception as exc:
            return {"session": "unknown", "record": False, "audio": False,
                    "sources": [], "encoders": [],
                    "reason": "Could not inspect screen capture: %s" % exc}

    def recorder_snapshot(self) -> dict:
        if self._recorder is None:
            return {"available": False, "running": False, "recording": False,
                    "paused": False, "starting": False, "elapsed": 0.0,
                    "output": "", "error": "Screen recording is unavailable.",
                    "notice": "", "capabilities": self.recorder_capabilities(),
                    "settings": copy.deepcopy(self.settings["recorder"]),
                    "audio_devices": {"microphones": [], "desktop": []}}
        try:
            return self._recorder.snapshot()
        except Exception as exc:
            return {"available": False, "running": False, "recording": False,
                    "paused": False, "starting": False, "elapsed": 0.0,
                    "output": "", "error": str(exc), "notice": "",
                    "capabilities": self.recorder_capabilities(),
                    "settings": copy.deepcopy(self.settings["recorder"]),
                    "audio_devices": {"microphones": [], "desktop": []}}

    def recorder_devices(self, refresh: bool = False) -> dict:
        if screen_recorder is None:
            return {"microphones": [], "desktop": []}
        try:
            return screen_recorder.list_audio_devices(refresh=refresh)
        except Exception:
            return {"microphones": [], "desktop": []}

    def set_recorder_settings(self, **values: Any) -> tuple[bool, str]:
        """Persist capture options; never mutate an active capture."""
        if self._recorder is not None and self._recorder.recording:
            return False, "Stop the recording before changing capture settings."
        if screen_recorder is None:
            return False, "Screen recording is unavailable."
        current = copy.deepcopy(self.settings["recorder"])
        current.update({key: value for key, value in values.items()
                        if key in current or key == "window_id"})
        clean = screen_recorder.normalize_settings(current)
        self.settings["recorder"] = clean
        try:
            self.save()
        except OSError as exc:
            return False, "Could not save recorder settings: %s" % exc
        self._emit()
        return True, "Recorder settings saved."

    def start_recording(self) -> tuple[bool, str]:
        if self._recorder is None:
            return False, "Screen recording is unavailable."
        try:
            ok, message = self._recorder.start()
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        self._emit()
        return ok, message

    def stop_recording(self, wait: bool = False) -> tuple[bool, str]:
        if self._recorder is None:
            return False, "Screen recording is unavailable."
        try:
            ok, message = self._recorder.stop(wait=wait)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        self._emit()
        return ok, message

    def pause_recording(self) -> tuple[bool, str]:
        if self._recorder is None:
            return False, "Screen recording is unavailable."
        ok, message = self._recorder.pause()
        self._emit()
        return ok, message

    def resume_recording(self) -> tuple[bool, str]:
        if self._recorder is None:
            return False, "Screen recording is unavailable."
        ok, message = self._recorder.resume()
        self._emit()
        return ok, message

    def toggle_recording(self) -> tuple[bool, str]:
        """One global action for both starting and finishing a capture."""
        snap = self.recorder_snapshot()
        if snap.get("running") or snap.get("starting"):
            return self.stop_recording(wait=False)
        return self.start_recording()

    def recorder_active_window(self) -> str:
        if screen_recorder is None:
            return ""
        try:
            return screen_recorder.active_window_id()
        except Exception:
            return ""

    def set_onboarded(self, value: bool = True) -> None:
        self.settings["onboarded"] = bool(value)
        self.save()
        self._emit()

    def set_accent(self, accent: str) -> None:
        if accent in ACCENTS:
            self.settings["accent"] = accent
            self.save()
            self._emit()

    # -- clicker -----------------------------------------------------------
    @property
    def ac_running(self) -> bool:
        return bool(self._ac_thread and self._ac_thread.is_alive())

    def _app_filter_decision(self) -> tuple[bool, str]:
        """Apply the optional foreground-app gate, fail closed."""
        clicker = self.settings["clicker"]
        mode = str(clicker.get("app_filter_mode", "off")).lower()
        if mode not in ("blacklist", "whitelist"):
            return True, ""
        name = foreground_app_name()
        if not name:
            return False, "foreground app unavailable"
        if mode == "whitelist":
            allowed = set(normalize_app_list(
                clicker.get("app_whitelist", [])))
            if not allowed:
                return False, "whitelist is empty"
            return (name in allowed, "" if name in allowed else name)
        blocked = set(normalize_app_list(clicker.get("app_blacklist", [])))
        return (name not in blocked, name if name in blocked else "")

    def _app_filter_guard(self) -> bool:
        allowed, label = self._app_filter_decision()
        if allowed:
            if self._ac_blocked:
                self._ac_blocked = ""
                self._emit()
            return True
        if label != self._ac_blocked:
            self._ac_blocked = label
            self._emit()
        return False

    def start_clicker(self, cps: Any = None, duty: Any = None,
                      button: Any = None) -> tuple[bool, str]:
        if engine is None:
            return False, "The input engine is unavailable."
        if self.macro_running:
            return False, "Stop the macro before starting the clicker."
        current = self.settings["clicker"]
        requested_cps = current["cps"] if cps is None else cps
        requested_max = current.get("cps_max")
        try:
            requested_max = max(float(requested_max), float(requested_cps))
        except (TypeError, ValueError):
            pass
        cfg, error = validate_clicker(
            requested_cps,
            current["duty"] if duty is None else duty,
            current["button"] if button is None else button,
            duration=current.get("duration", 0),
            pointer_mode=current.get("pointer_mode", "follow"),
            jitter=current.get("jitter", 0),
            fixed_x=current.get("fixed_x", 0),
            fixed_y=current.get("fixed_y", 0),
            failsafe=current.get("failsafe", True),
            failsafe_corner=current.get("failsafe_corner", "top-left"),
            pause_on_lock=current.get("pause_on_lock", True),
            rate_mode=current.get("rate_mode", "exact"),
            cps_max=requested_max,
            start_delay_ms=current.get("start_delay_ms", 0),
            stop_after_clicks=current.get("stop_after_clicks", 0),
            timing_precision=current.get("timing_precision", "balanced"),
            failsafe_margin=current.get("failsafe_margin", 2),
            app_filter_mode=current.get("app_filter_mode", "off"),
            app_blacklist=current.get("app_blacklist", []),
            app_whitelist=current.get("app_whitelist", []),
        )
        if error:
            return False, error
        if cfg["app_filter_mode"] != "off" and not foreground_app_name(
                refresh=True):
            return False, ("The app filter is on, but the foreground app "
                           "cannot be identified on this display. Turn the "
                           "filter off or start WXB in an X11 session.")
        with self._lock:
            if self.ac_running:
                return True, "Already clicking."
            locked, lock_message = self._acquire_clicker_lock()
            if not locked:
                self._ac_error = lock_message
                self._emit()
                return False, lock_message
            lock_handle = self._ac_lock
            self.settings["clicker"].update(cfg)
            try:
                self.save()
            except OSError as exc:
                if self._ac_lock is lock_handle:
                    self._release_clicker_lock()
                self._ac_error = "Could not save clicker settings: %s" % exc
                self._emit()
                return False, self._ac_error
            try:
                if self._sink_factory:
                    sink = self._sink_factory()
                elif os.environ.get("WXB_CLICK_BACKEND", "").strip().lower() in (
                        "command", "ydotool", "xdotool"):
                    sink = engine.CommandSink()
                else:
                    try:
                        sink = CountingSink()
                    except Exception as primary:
                        # Native Wayland or a missing X display can reject
                        # pynput at construction.  Use the command backend if
                        # the user installed one, and keep both reasons.
                        try:
                            sink = engine.CommandSink()
                        except Exception as fallback:
                            raise RuntimeError("%s %s" % (primary, fallback))
            except Exception as exc:
                if self._ac_lock is lock_handle:
                    self._release_clicker_lock()
                self._ac_error = "Could not open a pointer backend: %s" % exc
                self._emit()
                return False, self._ac_error
            self._ac_sink = sink
            stop = threading.Event()
            self._ac_stop = stop
            self._ac_started = time.monotonic()
            self._ac_start_delay = cfg["start_delay_ms"] / 1000.0
            self._ac_error = ""
            self._ac_paused = False
            self._ac_blocked = ""
            self._ac_last_elapsed = 0.0
            self._ac_last_clicks = 0
            # Query the monitor before the worker starts.  GDK is not thread
            # safe, and the click loop must never call into it.
            bounds = engine.screen_bounds()

            def on_error(message: str) -> None:
                self._ac_error = str(message)
                self._emit()

            def on_pause(paused: bool) -> None:
                self._ac_paused = bool(paused)
                self._emit()

            def worker() -> None:
                try:
                    engine.click_burst(cfg["cps"], cfg["duty"],
                                       cfg["button"], stop, sink=sink,
                                       duration_ms=(cfg["duration"] * 1000.0
                                                    if cfg["duration"] else None),
                                       cps_max=(cfg["cps_max"]
                                                if cfg["rate_mode"] == "range"
                                                else None),
                                       start_delay_ms=cfg["start_delay_ms"],
                                       clicks=(cfg["stop_after_clicks"] or None),
                                       on_error=on_error, on_pause=on_pause,
                                       pointer_mode=cfg["pointer_mode"],
                                       fixed_position=(cfg["fixed_x"],
                                                       cfg["fixed_y"]),
                                       jitter=cfg["jitter"],
                                       bounds=bounds,
                                       failsafe_corner=(
                                           cfg["failsafe_corner"]
                                           if cfg["failsafe"] else "off"),
                                       failsafe_margin=cfg["failsafe_margin"],
                                       timing_precision=cfg["timing_precision"],
                                       click_guard=(self._app_filter_guard
                                                    if cfg["app_filter_mode"] != "off"
                                                    else None),
                                       session_guard=(self._session_locked
                                                      if cfg["pause_on_lock"]
                                                      else None))
                except Exception as exc:
                    self._ac_error = "%s: %s" % (type(exc).__name__, exc)
                    traceback.print_exc()
                finally:
                    self._ac_paused = False
                    self._ac_blocked = ""
                    try:
                        sink.release_mouse(cfg["button"])
                    except Exception as exc:
                        if not self._ac_error:
                            self._ac_error = ("Could not release the mouse "
                                              "button: %s" % exc)
                        self._emit()
                    self._ac_last_elapsed = max(
                        0.0, time.monotonic() - self._ac_started
                        - self._ac_start_delay)
                    try:
                        self._ac_last_clicks = self.click_count()
                    except Exception:
                        self._ac_last_clicks = 0
                    self._record_clicker_session(self._ac_last_clicks,
                                                 self._ac_last_elapsed)
                    with self._lock:
                        if self._ac_stop is stop:
                            self._ac_thread = None
                            self._ac_stop = None
                        if self._ac_lock is lock_handle:
                            self._release_clicker_lock()
                    self._emit()

            thread = threading.Thread(target=worker, name="wxb-studio-clicks",
                                      daemon=True)
            self._ac_thread = thread
            thread.start()
        self._emit()
        if cfg["rate_mode"] == "range" and cfg["cps_max"] != cfg["cps"]:
            return True, ("Clicking between %g and %g CPS."
                          % (cfg["cps"], cfg["cps_max"]))
        return True, "Clicking at %g CPS." % cfg["cps"]

    def _session_locked(self) -> bool:
        """Whether the desktop session reports LockedHint=yes.

        ``loginctl`` is queried at most once a second.  A missing session or
        command is treated as unlocked: the clicker must keep working on
        minimal desktops, while a confirmed lock pauses it instead of typing
        into a lock screen.
        """
        now = time.monotonic()
        if now - self._session_locked_checked < 1.0:
            return bool(self._session_locked_cache)
        self._session_locked_checked = now
        session = os.environ.get("XDG_SESSION_ID", "").strip()
        if not session:
            self._session_locked_cache = False
            return False
        try:
            result = subprocess.run(
                ["loginctl", "show-session", session,
                 "-p", "LockedHint", "--value"],
                capture_output=True, text=True, timeout=1.0, check=False)
            locked = (result.returncode == 0
                      and result.stdout.strip().lower() in ("yes", "true", "1"))
        except (OSError, subprocess.SubprocessError):
            locked = False
        self._session_locked_cache = bool(locked)
        return self._session_locked_cache

    def _acquire_clicker_lock(self) -> tuple[bool, str]:
        """One clicker per config, even without a working D-Bus unique app.

        Gio's application id is the normal single-instance guard.  This
        advisory file lock closes the gap when the session bus is unavailable
        or a second launch uses WXB_STANDALONE.  The kernel releases it when
        the process exits, so a killed instance never leaves a stale lock.
        """
        if self._ac_lock is not None:
            return False, "Another WXB clicker is already running."
        try:
            if self.path.parent.is_symlink():
                return False, ("Refusing to lock through a symlinked config "
                               "directory.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = self.path.parent / "clicker.lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(target, flags, 0o600)
            # An elevated WXB must not leave a root-only lock that blocks the
            # same user's next unprivileged launch.
            _claim_owner(target)
            try:
                os.fchmod(fd, 0o600)
                handle = os.fdopen(fd, "a+")
                fd = None
            finally:
                if fd is not None:
                    os.close(fd)
        except OSError as exc:
            return False, "Could not open the clicker lock: %s" % exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False, ("Another WXB instance is already clicking with "
                           "this config.")
        self._ac_lock = handle
        return True, ""

    def _release_clicker_lock(self) -> None:
        handle, self._ac_lock = self._ac_lock, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    def stop_clicker(self, wait: bool = True) -> None:
        self._hold_actions.discard("toggle_clicker")
        with self._lock:
            stop, thread = self._ac_stop, self._ac_thread
            self._ac_stop = None
        if stop is not None:
            stop.set()
        if (wait and thread is not None
                and thread is not threading.current_thread()):
            thread.join(timeout=1.0)
        # The worker releases the button in its finally block.  A UI stop
        # must not wait for a blocking backend; only shutdown/stop-all wait
        # and then force a release as a belt-and-braces cleanup.
        sink = self._ac_sink
        if wait and sink is not None:
            try:
                sink.release_all()
            except Exception as exc:
                self._ac_error = ("Could not release the pointer/keyboard: %s"
                                  % exc)
                self._emit()
        self._ac_paused = False
        self._ac_blocked = ""
        self._emit()

    def toggle_clicker(self, cps: Any = None, duty: Any = None,
                       button: Any = None) -> tuple[bool, str]:
        if self.ac_running:
            self.stop_clicker()
            return True, "Clicker stopped."
        return self.start_clicker(cps, duty, button)

    def click_count(self) -> int:
        sink = self._ac_sink
        try:
            return int(getattr(sink, "clicks", 0) or 0) if sink is not None else 0
        except (TypeError, ValueError):
            return 0

    def clicker_stats_snapshot(self) -> dict:
        with self._lock:
            return dict(self._clicker_stats)

    def _record_clicker_session(self, clicks: Any, seconds: Any) -> None:
        """Persist lifetime totals once per completed clicker run."""
        try:
            count = max(0, int(clicks))
            elapsed = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        if count <= 0:
            return
        with self._lock:
            stats = dict(self._clicker_stats)
            stats["total_clicks"] = min(
                CLICKER_STATS_MAX, int(stats.get("total_clicks", 0)) + count)
            stats["sessions"] = min(
                CLICKER_STATS_MAX, int(stats.get("sessions", 0)) + 1)
            stats["last_session_clicks"] = count
            stats["last_session_seconds"] = round(elapsed, 3)
            stats["best_session_clicks"] = max(
                int(stats.get("best_session_clicks", 0)), count)
            self._clicker_stats = stats
        try:
            save_clicker_stats(stats, self.stats_path)
        except OSError as exc:
            if not self._ac_error:
                self._ac_error = "Clicker stopped, but stats could not be saved: %s" % exc

    def pointer_position(self) -> tuple[int, int] | None:
        """Read-only cursor position for the sidebar; never starts an engine."""
        try:
            sink = self._ac_sink
            if sink is None:
                if self._sink_factory:
                    sink = self._sink_factory()
                elif engine is not None:
                    sink = engine.sink_default()
            position = sink.pointer_position() if sink is not None else None
            if position:
                return int(position[0]), int(position[1])
        except Exception:
            return None
        return None

    def capture_clicker_point(self) -> tuple[bool, str]:
        """Read the current pointer position for fixed/jitter mode."""
        try:
            sink = self._ac_sink
            if sink is None:
                sink = (self._sink_factory() if self._sink_factory
                        else CountingSink())
            position = sink.pointer_position()
        except Exception as exc:
            return False, "Could not read the pointer position: %s" % exc
        if not position:
            return False, ("The pointer backend cannot report a position. "
                           "Type fixed coordinates or install ydotool/xdotool.")
        self.settings["clicker"]["fixed_x"] = int(position[0])
        self.settings["clicker"]["fixed_y"] = int(position[1])
        self.save()
        self._emit()
        return True, "Fixed point set to %d, %d." % (int(position[0]),
                                                      int(position[1]))

    # -- rotation ----------------------------------------------------------
    @property
    def rot_running(self) -> bool:
        return bool(self._rot_thread and self._rot_thread.is_alive())

    @property
    def macro_enabled(self) -> bool:
        """Whether the rotation master switch is on."""
        return bool(self.settings.get("macro_enabled", False))

    def set_macro_enabled(self, enabled: bool) -> tuple[bool, str]:
        """The rotation master switch.  Off also stops a live rotation."""
        on = bool(enabled)
        self.settings["macro_enabled"] = on
        if not on:
            self._hold_actions.discard("toggle_rotation")
            self.stop_rotation()
        self.save()
        self._emit()
        return True, ("Rotation enabled." if on
                      else "Rotation disabled and stopped.")

    def start_rotation(self, values: Mapping[str, Any] | None = None) -> tuple[bool, str]:
        if engine is None:
            return False, "The input engine is unavailable."
        if self.macro_running:
            return False, "Stop the macro before starting the rotation."
        if not self.macro_enabled:
            return False, "Enable the rotation macro first."
        raw = dict(self.settings["rotation"])
        if values:
            raw.update({k: v for k, v in values.items() if v is not None})
        cfg, error = validate_rotation(raw)
        if error:
            return False, error
        with self._lock:
            if self.rot_running:
                return True, "Already rotating."
            self.settings["rotation"] = {
                "slot_a": raw["slot_a"], "slot_b": raw["slot_b"],
                "delay_a": raw["delay_a"], "delay_b": raw["delay_b"],
                "mode": raw["mode"], "max_rot": raw["max_rot"],
            }
            self.save()
            stop = threading.Event()
            sink = self._sink_factory() if self._sink_factory else None
            self._rot_sink = sink
            self._rot_stop = stop
            self._rot_started = time.monotonic()
            self._rot_done = 0
            self._rot_error = ""

            def worker() -> None:
                try:
                    done = engine.run_rotation(cfg, stop, sink=sink)
                    self._rot_done = int(done or 0)
                except Exception as exc:
                    self._rot_error = "%s: %s" % (type(exc).__name__, exc)
                    traceback.print_exc()
                finally:
                    try:
                        engine.release_keys()
                    except Exception as exc:
                        if not self._rot_error:
                            self._rot_error = ("Could not release rotation "
                                               "keys: %s" % exc)
                        self._emit()
                    with self._lock:
                        if self._rot_stop is stop:
                            self._rot_thread = None
                            self._rot_stop = None
                            self._rot_sink = None
                    self._emit()

            thread = threading.Thread(target=worker, name="wxb-studio-rotation",
                                      daemon=True)
            self._rot_thread = thread
            thread.start()
        self._emit()
        return True, "Rotation started."

    def stop_rotation(self) -> None:
        self._hold_actions.discard("toggle_rotation")
        with self._lock:
            stop, thread = self._rot_stop, self._rot_thread
            self._rot_stop = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.8)
        try:
            if self._rot_sink is not None:
                self._rot_sink.release_keys()
            else:
                engine.release_keys()
        except Exception as exc:
            self._rot_error = "Could not release rotation keys: %s" % exc
            self._emit()
        self._emit()

    def toggle_rotation(self, values: Mapping[str, Any] | None = None) -> tuple[bool, str]:
        if self.rot_running:
            self.stop_rotation()
            return True, "Rotation stopped."
        return self.start_rotation(values)

    # -- SOCD --------------------------------------------------------------
    @property
    def socd_running(self) -> bool:
        return bool(self._socd is not None and getattr(self._socd, "running", False))

    def socd_access(self) -> dict:
        if engine is None:
            return {"ok": False, "text": "The input engine is unavailable.",
                    "missing": "engine", "commands": []}
        try:
            return engine.socd_permission()
        except Exception as exc:
            return {"ok": False, "text": "%s: %s" % (type(exc).__name__, exc),
                    "missing": "input", "commands": []}

    def socd_snapshot(self) -> dict:
        access = self.socd_access()
        if self._socd is None:
            return {"running": False, "keyboards": 0, "conflicts": 0,
                    "error": self._socd_error, "phys": {}, "out": {},
                    "access": access}
        try:
            snap = self._socd.snapshot()
            snap["access"] = access
            return snap
        except Exception as exc:
            return {"running": False, "keyboards": 0, "conflicts": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "phys": {}, "out": {}, "access": access}

    def set_socd_modes(self, modes: Mapping[str, Any]) -> tuple[bool, str]:
        socd = self.settings["socd"]
        socd["mode_h"] = _choice(
            modes.get("horizontal", modes.get("mode_h")),
            SOCD_MODE_ALIASES, socd.get("mode_h", "Neutral"))
        socd["mode_v"] = _choice(
            modes.get("vertical", modes.get("mode_v")),
            SOCD_MODE_ALIASES, socd.get("mode_v", "Neutral"))
        if self._socd is not None:
            try:
                self._socd.engine.set_modes({
                    "horizontal": socd["mode_h"],
                    "vertical": socd["mode_v"],
                })
            except Exception as exc:
                return False, "Could not update the cleaner: %s" % exc
        self.save()
        self._emit()
        return True, "SOCD modes updated."

    def start_socd(self, modes: Mapping[str, str] | None = None) -> tuple[bool, str]:
        if engine is None:
            return False, "The input engine is unavailable."
        if self.macro_running:
            return False, "Stop the macro before starting SOCD."
        if self.socd_running:
            return True, "SOCD cleaner is already running."
        modes = modes or {
            "horizontal": self.settings["socd"].get("mode_h", "Neutral"),
            "vertical": self.settings["socd"].get("mode_v", "Neutral"),
        }
        profile, profile_error = self._active_input_lab_profile()
        if profile_error:
            self._socd_error = profile_error
            self._emit()
            return False, profile_error
        if profile is not None and not self.input_lab_capabilities().get("remap"):
            message = ("Input Lab remapping needs readable /dev/input and "
                       "writable /dev/uinput, and is disabled while root.")
            self._socd_error = message
            self._emit()
            return False, message
        try:
            state = engine.SocdEngine(modes=modes)
            pipeline = engine.SocdPipeline(
                engine=state, on_change=lambda: self._emit(),
                remap=profile,
                timeline=bool(self.settings["input_lab"]["timeline"]))
            ok, message = pipeline.start()
        except Exception as exc:
            message = "%s: %s" % (type(exc).__name__, exc)
            ok = False
        if ok:
            self._socd = pipeline
            self._socd_error = ""
            self.settings["socd"] = {
                "mode_h": modes.get("horizontal", "Neutral"),
                "mode_v": modes.get("vertical", "Neutral"),
            }
            self.save()
            self._emit()
            return True, "SOCD cleaner is running."
        self._socd_error = message
        self._emit()
        return False, message

    def stop_socd(self) -> None:
        pipeline, self._socd = self._socd, None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception as exc:
                self._socd_error = "Could not stop SOCD: %s" % exc
                traceback.print_exc()
        self._emit()

    def toggle_socd(self) -> tuple[bool, str]:
        if self.socd_running:
            self.stop_socd()
            return True, "SOCD cleaner stopped."
        return self.start_socd()

    # -- Input Lab ----------------------------------------------------------
    def input_lab_profile_dir(self) -> Path:
        return self.path.parent / "input-lab-profiles"

    def input_lab_profile_path(self, name: Any) -> Path | None:
        clean = self.clean_config_name(name)
        return self.input_lab_profile_dir() / (clean + ".json") if clean else None

    def list_input_lab_profiles(self) -> list[str]:
        root = self.input_lab_profile_dir()
        try:
            if root.is_symlink():
                return []
            return sorted(path.stem for path in root.glob("*.json")
                          if path.is_file() and not path.is_symlink())
        except OSError:
            return []

    def _read_input_lab_profile(self, name: Any) -> tuple[dict | None, str]:
        path = self.input_lab_profile_path(name)
        if path is None:
            return None, "Choose a remap profile."
        if path.parent.is_symlink():
            return None, "Refusing to read a symlinked Input Lab directory."
        if path.is_symlink():
            return None, "Refusing to read a symlinked remap profile."
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None
                    raw = json.load(handle)
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            return None, "Could not read remap profile: %s" % exc
        profile, error = input_lab.normalize_profile(raw, require_name=True)
        if profile is None:
            return None, error or "That remap profile is invalid."
        return profile, ""

    def _active_input_lab_profile(self) -> tuple[dict | None, str]:
        name = self.settings["input_lab"].get("active_profile", "")
        if not name:
            self._input_lab_profile = None
            self._input_lab_profile_error = ""
            return None, ""
        if (self._input_lab_profile is not None
                and self._input_lab_profile.get("name") == name):
            return self._input_lab_profile, self._input_lab_profile_error
        profile, error = self._read_input_lab_profile(name)
        self._input_lab_profile = profile
        self._input_lab_profile_error = error
        return profile, error

    def save_input_lab_profile(self, name: Any, mappings: Any,
                               mode: Any = "hold") -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self.input_lab_profile_path(clean)
        if target is None:
            return False, "Give this remap profile a name without slashes."
        profile, error = input_lab.normalize_profile({
            "name": clean, "mode": mode, "mappings": mappings,
        }, require_name=True)
        if profile is None:
            return False, error
        try:
            if target.parent.is_symlink():
                return False, "Refusing to write through a symlinked Input Lab directory."
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            tmp = target.with_suffix(target.suffix + ".tmp")
            payload = json.dumps(profile, indent=2, sort_keys=True) + "\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = None
                    handle.write(payload)
            finally:
                if fd is not None:
                    os.close(fd)
            os.replace(tmp, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            return False, "Could not save remap profile: %s" % exc
        self._emit()
        return True, "Saved remap profile “%s”." % clean

    def load_input_lab_profile(self, name: Any) -> tuple[bool, str]:
        profile, error = self._read_input_lab_profile(name)
        if profile is None:
            self._input_lab_profile_error = error
            self._emit()
            return False, error
        self._input_lab_profile = profile
        self._input_lab_profile_error = ""
        self.settings["input_lab"]["active_profile"] = profile["name"]
        self.save()
        self._emit()
        suffix = (" It applies the next time the cleaner starts."
                  if self.socd_running else "")
        return True, "Loaded remap profile “%s”.%s" % (profile["name"], suffix)

    def delete_input_lab_profile(self, name: Any) -> tuple[bool, str]:
        clean = self.clean_config_name(name)
        target = self.input_lab_profile_path(clean)
        if target is None or not target.is_file():
            return False, "That remap profile no longer exists."
        if target.parent.is_symlink() or target.is_symlink():
            return False, "Refusing to delete a symlinked remap profile."
        try:
            target.unlink()
        except OSError as exc:
            return False, "Could not remove remap profile: %s" % exc
        if self.settings["input_lab"].get("active_profile") == clean:
            self.settings["input_lab"]["active_profile"] = ""
            self._input_lab_profile = None
            self.save()
        self._emit()
        return True, "Removed remap profile “%s”." % clean

    def input_lab_conflicts(self, external: bool = False) -> list[str]:
        profile, error = self._active_input_lab_profile()
        if error:
            return [error]
        if profile is None:
            return []
        conflicts = input_lab.profile_conflicts(profile, self.hotkey_mapping())
        if external:
            conflicts.extend(input_lab.external_key_conflicts(
                profile, os.environ))
        return conflicts

    def input_lab_capabilities(self, refresh: bool = False) -> dict:
        now = time.monotonic()
        if (not refresh and self._input_lab_caps_cache is not None
                and now - self._input_lab_caps_checked < 5.0):
            return dict(self._input_lab_caps_cache)
        data = input_lab.detect_input_lab_capabilities()
        self._input_lab_caps_cache = dict(data)
        self._input_lab_caps_checked = now
        return data

    def input_lab_snapshot(self) -> dict:
        profile, error = self._active_input_lab_profile()
        controller = (self._controller.snapshot()
                      if self._controller is not None else None)
        visualizer = self._visualizer.snapshot()
        if self.socd_running:
            capture = "socd"
        elif controller and controller.get("running"):
            capture = "controller"
        elif visualizer.get("running"):
            capture = "visualizer"
        else:
            capture = "off"
        return {
            "settings": copy.deepcopy(self.settings["input_lab"]),
            "capabilities": self.input_lab_capabilities(),
            "profile": profile,
            "profile_error": error or self._input_lab_profile_error,
            "conflicts": (input_lab.profile_conflicts(
                profile, self.hotkey_mapping()) if profile else []),
            "controller": controller,
            "visualizer": visualizer,
            "capture": capture,
            "error": self._input_lab_error or self._controller_error
                     or self._visualizer_error,
        }

    def start_controller_test(self) -> tuple[bool, str]:
        caps = self.input_lab_capabilities(refresh=True)
        if not caps.get("controller"):
            message = (caps.get("reason") or
                       "Controller testing needs readable /dev/input.")
            self._controller_error = message
            self._input_lab_error = message
            self._emit()
            return False, message
        if self._controller is not None and self._controller.running:
            return True, "Controller test is already running."
        monitor = input_lab.ControllerMonitor(
            deadzone=self.settings["input_lab"]["deadzone"],
            on_change=lambda _snap: self._emit())
        ok, message = monitor.start()
        if not ok:
            self._controller_error = message
            self._input_lab_error = message
            self._emit()
            return False, message
        self._controller = monitor
        self._controller_error = ""
        self._input_lab_error = ""
        self._emit()
        return True, message

    def stop_controller_test(self) -> None:
        monitor, self._controller = self._controller, None
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as exc:
                self._controller_error = "Could not stop controller test: %s" % exc
        self._emit()

    def set_input_lab_settings(self, **values: Any) -> tuple[bool, str]:
        current = copy.deepcopy(self.settings["input_lab"])
        if "timeline" in values:
            current["timeline"] = values["timeline"]
        if "deadzone" in values:
            current["deadzone"] = values["deadzone"]
        visual = values.get("visualizer")
        if isinstance(visual, Mapping):
            current["visualizer"].update(visual)
        for key in ("keys", "clicks", "size", "color", "opacity"):
            if key in values:
                current["visualizer"][key] = values[key]
        clean = input_lab.normalize_input_lab(current)
        self.settings["input_lab"] = clean
        self.save()
        if self._controller is not None:
            self._controller.set_deadzone(clean["deadzone"])
        if self.socd_running and hasattr(self._socd, "set_timeline"):
            self._socd.set_timeline(clean["timeline"])
        self._apply_visualizer()
        self._emit()
        if self._visualizer_error:
            return False, self._visualizer_error
        return True, "Input Lab settings saved."

    def add_visualizer_listener(self, callback: Callable) -> Callable:
        self._visualizer_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._visualizer_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _visualizer_key(self, label: str) -> None:
        for callback in list(self._visualizer_listeners):
            try:
                callback("key", label, 0, 0)
            except Exception:
                traceback.print_exc()

    def _visualizer_click(self, label: str, x: int, y: int) -> None:
        for callback in list(self._visualizer_listeners):
            try:
                callback("click", label, x, y)
            except Exception:
                traceback.print_exc()

    def _apply_visualizer(self) -> None:
        visual = self.settings["input_lab"]["visualizer"]
        if not (visual["keys"] or visual["clicks"]):
            self._visualizer.stop()
            self._visualizer_error = ""
            return
        ok, message = self._visualizer.start(visual["keys"], visual["clicks"])
        if not ok:
            self._visualizer_error = message
            self.settings["input_lab"]["visualizer"]["keys"] = False
            self.settings["input_lab"]["visualizer"]["clicks"] = False
            self.save()
        else:
            self._visualizer_error = ""

    def safe_input_reset(self) -> tuple[bool, str]:
        for stop in (lambda: self.stop_macro(wait=True), self.stop_clicker,
                     self.stop_rotation, self.stop_socd):
            try:
                stop()
            except Exception:
                traceback.print_exc()
        self.stop_controller_test()
        self._visualizer.stop()
        self.settings["input_lab"]["active_profile"] = ""
        self._input_lab_profile = None
        self._input_lab_profile_error = ""
        self._input_lab_error = ""
        self._visualizer_error = ""
        self.save()
        self._emit()
        return True, "Inputs released and remaps disabled."

    # -- Sober -------------------------------------------------------------
    @property
    def sober_running(self) -> bool:
        proc = self._sober
        if proc is not None and proc.poll() is None:
            return True
        # Both the elevated owner bridge and `flatpak ps` can take longer than
        # a frame.  The window snapshot asks every second, so keep one short
        # shared answer instead of spawning a helper for every tick.  Launch
        # and stop invalidate this cache explicitly.
        now = time.monotonic()
        if (self._sober_running_cache is not None
                and now - self._sober_running_checked < 3.0):
            return bool(self._sober_running_cache)
        if self._elevated():
            data = self._lution_cli("running", timeout=15)
            running = bool(data.get("ok") and data.get("running"))
        else:
            # flatpak can reparent/detach the game after the bwrap wrapper
            # exits; ask the Flatpak session rather than trusting only the
            # Popen handle.
            try:
                running = bool(lution_launcher is not None
                               and lution_launcher.sober_is_running())
            except Exception:
                running = False
        self._sober_running_cache = running
        self._sober_running_checked = now
        return running

    # -- Lution runtime / deployment --------------------------------------
    def lution_runtime(self, refresh: bool = False) -> dict:
        """Installed version, Flatpak presence and crash count.

        Reading this runs the Flatpak CLI, so it is cached; refresh is an
        explicit action after install/update or when the window refreshes a
        state it just changed. Playtime is a separate log scan.
        """
        if self._lution_runtime_cache is not None and not refresh:
            return dict(self._lution_runtime_cache)
        if self._elevated():
            data = self._lution_cli("status", timeout=60)
            installed = bool(data.get("installed"))
            crashes_data = data.get("crashes")
            crashes = (int(crashes_data.get("count", 0))
                       if isinstance(crashes_data, dict) else 0)
            running = bool(data.get("running"))
            result = {
                "ok": bool(data.get("ok")),
                "installed": installed,
                "version": str(data.get("version") or ""),
                "running": running,
                "flatpak": bool(shutil.which("flatpak")),
                "crashes": crashes,
                "error": str(data.get("error") or ""),
            }
            self._sober_running_cache = running
            self._sober_running_checked = time.monotonic()
            self._lution_runtime_cache = dict(result)
            self._lution_runtime_dirty = False
            return result
        flatpak = bool(shutil.which("flatpak"))
        installed = False
        version = ""
        crashes = 0
        error = ""
        if lution_sober is None:
            error = "The bundled Lution core is unavailable."
        else:
            try:
                installed = bool(lution_sober.is_installed())
            except Exception as exc:
                error = "%s: %s" % (type(exc).__name__, exc)
            if installed:
                try:
                    version = str(lution_sober.get_version() or "")
                except Exception as exc:
                    error = "%s: %s" % (type(exc).__name__, exc)
        if lution_launcher is not None:
            try:
                crashes = int((lution_launcher.crash_state() or {}).get("count", 0))
            except Exception:
                crashes = 0
        try:
            running = bool(self.sober_running)
        except Exception:
            running = False
        data = {
            "ok": not bool(error),
            "installed": installed,
            "version": version,
            "running": running,
            "flatpak": flatpak,
            "crashes": crashes,
            "error": error,
        }
        self._lution_runtime_cache = dict(data)
        self._lution_runtime_dirty = False
        return data

    def ensure_sober(self) -> tuple[bool, str]:
        """Install or update Sober as an explicit deployment action."""
        if self._sober_launching:
            return False, "Sober is already starting; wait for it to open."
        if self._elevated():
            if not shutil.which("flatpak"):
                return False, "Flatpak is not installed. Install Flatpak and retry."
            if self.sober_running:
                return False, "Close Sober before installing or updating it."
            with self._lock:
                if self._sober_maintenance:
                    return True, "Sober install/update is already running."
                self._sober_maintenance = True
                self._sober_error = ""
                self._sober_detail = "Installing or updating Sober…"

            def elevated_worker() -> None:
                result = self._lution_cli("ensure", timeout=600)
                ok = bool(result.get("ok"))
                detail = str(result.get("message") or result.get("error")
                             or "")
                with self._lock:
                    self._sober_maintenance = False
                    self._sober_detail = detail
                    self._sober_error = ("" if ok else
                                         "Sober install/update failed: %s"
                                         % detail)
                    self._lution_runtime_cache = None
                    self._lution_runtime_dirty = True
                self._emit()

            thread = threading.Thread(target=elevated_worker,
                                      name="wxb-sober-deploy", daemon=True)
            self._sober_thread = thread
            thread.start()
            self._emit()
            return True, "Installing or updating Sober…"
        if lution_sober is None:
            return False, "The bundled Lution core is unavailable."
        if not shutil.which("flatpak"):
            return False, "Flatpak is not installed. Install Flatpak and retry."
        if self.sober_running:
            return False, "Close Sober before installing or updating it."
        with self._lock:
            if self._sober_maintenance:
                return True, "Sober install/update is already running."
            self._sober_maintenance = True
            self._sober_error = ""
            self._sober_detail = "Installing or updating Sober…"

        def worker() -> None:
            detail = ""
            try:
                def progress(line):
                    nonlocal detail
                    detail = str(line or "").strip() or detail

                ok, detail = lution_sober.ensure_sober(progress)
                detail = str(detail or "")
            except Exception as exc:
                ok = False
                detail = "%s: %s" % (type(exc).__name__, exc)
            with self._lock:
                self._sober_maintenance = False
                self._sober_detail = detail
                self._sober_error = ("" if ok else
                                     "Sober install/update failed: %s" % detail)
                self._lution_runtime_cache = None
                self._lution_runtime_dirty = True
            self._emit()

        thread = threading.Thread(target=worker, name="wxb-sober-deploy",
                                  daemon=True)
        self._sober_thread = thread
        thread.start()
        self._emit()
        return True, "Installing or updating Sober…"

    def lution_playtime(self) -> dict:
        if self._elevated():
            data = self._lution_cli("playtime", timeout=60)
            play = data.get("playtime") if isinstance(data, dict) else None
            if not data.get("ok") or not isinstance(play, dict):
                return {"ok": False, "seconds": 0, "sessions": 0,
                        "error": data.get("error", "Playtime is unavailable.")}
            seconds = int(play.get("seconds") or 0)
            return {
                "ok": True,
                "seconds": seconds,
                "sessions": int(play.get("sessions") or 0),
                "places": len(play.get("places") or {}),
                "label": str(play.get("text") or "%ds" % seconds),
            }
        if lution_playtime is None:
            return {"ok": False, "seconds": 0, "sessions": 0,
                    "error": "The bundled Lution core is unavailable."}
        try:
            places, seconds, sessions = lution_playtime.scan()
            seconds = int(seconds or 0)
            return {
                "ok": True,
                "seconds": seconds,
                "sessions": int(sessions or 0),
                "places": len(places or {}),
                "label": lution_playtime.format_duration(seconds),
            }
        except Exception as exc:
            return {"ok": False, "seconds": 0, "sessions": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    # -- Lution visuals ----------------------------------------------------
    def fonts_status(self) -> dict:
        if self._elevated():
            data = self._lution_cli("fonts-status", timeout=60)
            font = data.get("font")
            if not data.get("ok") or not isinstance(font, dict):
                return {"ok": False,
                        "error": data.get("error", "Font status is unavailable.")}
            result = dict(font)
            result["ok"] = True
            return result
        if lution_fonts is None:
            return {"ok": False, "error": "The bundled Lution core is unavailable."}
        try:
            data = dict(lution_fonts.status())
            data["ok"] = True
            return data
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    def apply_font(self, path: Any) -> tuple[bool, str]:
        if self._elevated():
            value = str(path)
            if not value.startswith("file:"):
                value = os.path.abspath(_desktop_path(value))
            data = self._lution_cli("fonts-apply", args=(value,), timeout=120)
            if not data.get("ok"):
                return False, data.get("error", "Could not apply that font.")
            return True, "Font applied to %d Roblox faces." % int(
                data.get("replaced") or 0)
        if lution_fonts is None:
            return False, "The bundled Lution core is unavailable."
        try:
            names = lution_fonts.apply_font(path)
            return True, "Font applied to %d Roblox faces." % len(names)
        except (FileNotFoundError, ValueError) as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "Could not apply that font: %s" % exc

    def restore_fonts(self) -> tuple[bool, str]:
        if self._elevated():
            data = self._lution_cli("fonts-restore", timeout=60)
            if not data.get("ok"):
                return False, data.get("error", "Could not restore fonts.")
            removed = int(data.get("removed") or 0)
            return True, ("Restored Roblox's fonts." if removed else
                          "Roblox's fonts were already stock.")
        if lution_fonts is None:
            return False, "The bundled Lution core is unavailable."
        try:
            removed = int(lution_fonts.restore_fonts() or 0)
            return True, ("Restored Roblox's fonts." if removed else
                          "Roblox's fonts were already stock.")
        except Exception as exc:
            return False, "Could not restore fonts: %s" % exc

    def reapply_font(self) -> tuple[bool, str]:
        if self._elevated():
            data = self._lution_cli("fonts-reapply", timeout=120)
            if not data.get("ok"):
                return False, data.get("error", "Could not reapply the font.")
            count = int(data.get("reapplied") or 0)
            if count:
                return True, "Custom font reapplied to %d faces." % count
            return True, "No kept font or Roblox face list to reapply."
        if lution_fonts is None:
            return False, "The bundled Lution core is unavailable."
        try:
            count = int(lution_fonts.reapply_fonts() or 0)
            if count:
                return True, "Custom font reapplied to %d faces." % count
            return True, "No kept font or Roblox face list to reapply."
        except Exception as exc:
            return False, "Could not reapply the font: %s" % exc

    def cursor_options(self) -> dict:
        if lution_cursors is None:
            return {"ok": False, "styles": [], "colors": [], "sizes": [],
                    "defaults": {}, "error": "The bundled Lution core is unavailable."}
        online = getattr(lution_cursors, "ONLINE_STYLES", ())
        if not isinstance(online, (list, tuple)):
            online = ()
        pack = {
            "name": getattr(lution_cursors, "PACK_NAME", ""),
            "url": getattr(lution_cursors, "PACK_URL", ""),
            "license": getattr(lution_cursors, "PACK_LICENSE", ""),
            "credit": getattr(lution_cursors, "PACK_CREDIT", ""),
        }
        return {
            "ok": True,
            "styles": list(lution_cursors.STYLES),
            "colors": list(lution_cursors.COLORS),
            "sizes": list(lution_cursors.SIZES),
            "online": [dict(item) for item in online
                       if isinstance(item, dict)],
            "pack": pack,
            "defaults": {
                "style": lution_cursors.DEFAULT_STYLE,
                "color": lution_cursors.DEFAULT_COLOR,
                "size": lution_cursors.DEFAULT_SIZE,
            },
        }

    def cursors_status(self) -> dict:
        if self._elevated():
            data = self._lution_cli("cursors-status", timeout=60)
            cursor = data.get("cursor")
            if not data.get("ok") or not isinstance(cursor, dict):
                return {"ok": False,
                        "error": data.get("error", "Cursor status is unavailable.")}
            result = dict(cursor)
            result["ok"] = True
            return result
        if lution_cursors is None:
            return {"ok": False, "error": "The bundled Lution core is unavailable."}
        try:
            data = dict(lution_cursors.status())
            data["ok"] = True
            return data
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    def apply_cursor(self, style: Any, color: Any, size: Any
                     ) -> tuple[bool, str]:
        if self._elevated():
            data = self._lution_cli("cursors-apply", payload={
                "style": style, "color": color, "size": size}, timeout=120)
            if not data.get("ok"):
                return False, data.get("error", "Cursor renderer failed.")
            return True, "Cursor applied for the next Sober launch."
        if lution_cursors is None:
            return False, "The bundled Lution core is unavailable."
        try:
            result = lution_cursors.apply(style, color, size)
        except Exception as exc:
            return False, "Could not apply that cursor: %s" % exc
        if not isinstance(result, dict) or not result.get("ok"):
            return False, (result.get("error") if isinstance(result, dict)
                           else "Cursor renderer failed.")
        return True, "Cursor applied for the next Sober launch."

    def apply_online_cursor(self, name: Any, size: Any) -> tuple[bool, str]:
        """Download (once) and apply a cursor from the CC0 online pack."""
        if self._elevated():
            data = self._lution_cli("cursors-online-apply", payload={
                "name": name, "size": size}, timeout=120)
            if not data.get("ok"):
                return False, data.get("error", "Cursor download failed.")
            style = (data.get("cursor") or {}).get("style") or name
            return True, "%s applied for the next Sober launch." % style
        if lution_cursors is None:
            return False, "The bundled Lution core is unavailable."
        try:
            result = lution_cursors.apply_online(name, size)
        except Exception as exc:
            return False, "Could not apply that cursor: %s" % exc
        if not isinstance(result, dict) or not result.get("ok"):
            return False, (result.get("error") if isinstance(result, dict)
                           else "Cursor download failed.")
        style = (result.get("cursor") or {}).get("style") or name
        return True, "%s applied for the next Sober launch." % style

    def restore_cursors(self) -> tuple[bool, str]:
        if self._elevated():
            data = self._lution_cli("cursors-restore", timeout=60)
            if not data.get("ok"):
                return False, data.get("error", "Cursor restore failed.")
            return True, "Roblox's stock cursors restored."
        if lution_cursors is None:
            return False, "The bundled Lution core is unavailable."
        try:
            result = lution_cursors.restore()
        except Exception as exc:
            return False, "Could not restore cursors: %s" % exc
        if not isinstance(result, dict) or not result.get("ok"):
            return False, "Cursor restore failed."
        return True, "Roblox's stock cursors restored."

    def fps_status(self) -> dict:
        if self._elevated():
            data = self._lution_cli("fps-get", timeout=60)
            if not data.get("ok"):
                return {"ok": False, "fps": None, "available": False,
                        "running": bool(self.sober_running),
                        "error": data.get("error", "Framerate cap is unavailable.")}
            value = data.get("fps")
            if value is None:
                return {"ok": True, "fps": None, "available": False,
                        "running": bool(self.sober_running),
                        "error": "Sober has not written its settings file yet. "
                                 "Launch it once, then set a cap."}
            try:
                return {"ok": True, "fps": int(str(value).strip()),
                        "available": True,
                        "running": bool(self.sober_running)}
            except (TypeError, ValueError):
                return {"ok": True, "fps": None, "available": False,
                        "running": bool(self.sober_running),
                        "error": "Sober's framerate cap is not a whole number."}
        if lution_fps is None:
            return {"ok": False, "fps": None,
                    "error": "The bundled Lution core is unavailable."}
        try:
            value = lution_fps.load_framerate_cap()
        except Exception as exc:
            return {"ok": False, "fps": None, "available": False,
                    "running": bool(self.sober_running),
                    "error": "Could not read Sober's settings: %s" % exc}
        if value is None:
            return {"ok": True, "fps": None, "available": False,
                    "running": bool(self.sober_running),
                    "error": "Sober has not written its settings file yet. "
                             "Launch it once, then set a cap."}
        try:
            return {"ok": True, "fps": int(str(value).strip()),
                    "available": True, "running": bool(self.sober_running)}
        except (TypeError, ValueError):
            return {"ok": True, "fps": None, "available": False,
                    "running": bool(self.sober_running),
                    "error": "Sober's framerate cap is not a whole number."}

    def set_fps_cap(self, value: Any) -> tuple[bool, str]:
        try:
            cap = int(str(value).strip())
        except (TypeError, ValueError):
            return False, "Framerate cap must be a whole number."
        if not 0 <= cap <= 1000000:
            return False, "Framerate cap must stay between 0 and 1000000."
        if self._elevated():
            data = self._lution_cli("fps-set", args=(str(cap),), timeout=60)
            if not data.get("ok"):
                return False, data.get("error", "Could not save the framerate cap.")
            return True, "Framerate cap set to %d." % cap
        if lution_fps is None:
            return False, "The bundled Lution core is unavailable."
        if self.sober_running:
            return False, "Close Sober before changing its framerate cap."
        try:
            lution_fps.save_framerate_cap(cap)
        except FileNotFoundError:
            return False, ("Sober has not written its settings file yet. "
                           "Launch it once, then set a cap.")
        except Exception as exc:
            return False, "Could not save the framerate cap: %s" % exc
        return True, "Framerate cap set to %d." % cap

    # -- Lution fast flags -------------------------------------------------
    def lution_flags(self) -> dict:
        """The flags Lution will bind into Sober at the next launch."""
        if self._elevated():
            data = self._lution_cli("fflags-get", timeout=60)
            if not data.get("ok"):
                return {"ok": False, "flags": {},
                        "error": data.get("error", "Lution is unavailable.")}
            allowed = data.get("allowlist")
            if isinstance(allowed, list):
                self._lution_allowlist_cache = set(allowed)
            flags = data.get("fflags")
            return {"ok": True,
                    "flags": flags if isinstance(flags, dict) else {}}
        if lution_fflags is None:
            return {"ok": False, "flags": {}, "error": "Lution is unavailable."}
        try:
            flags = lution_fflags.get_fflags()
            return {"ok": True, "flags": flags if isinstance(flags, dict) else {}}
        except Exception as exc:
            return {"ok": False, "flags": {}, "error": str(exc)}

    def flag_allowlist(self) -> set[str]:
        if self._elevated():
            if self._lution_allowlist_cache is not None:
                return set(self._lution_allowlist_cache)
            data = self._lution_cli("fflags-get", timeout=60)
            allowed = data.get("allowlist")
            if isinstance(allowed, list):
                self._lution_allowlist_cache = set(allowed)
                return set(allowed)
            return set()
        if lution_fflags is None:
            return set()
        try:
            return set(lution_fflags.allowlist())
        except Exception:
            return set()

    def flag_allowlist_age(self):
        if self._elevated():
            # The allowlist is refreshed by the owner bridge; root has no
            # meaningful age for it and must not read root state.
            return None
        if lution_fflags is None:
            return None
        try:
            return lution_fflags.allowlist_age()
        except Exception:
            return None

    def _clean_lution_flags(self, flags: Mapping[str, Any]
                            ) -> tuple[dict | None, str]:
        allowed = self.flag_allowlist()
        if not allowed:
            return None, "The fast-flag allowlist is unavailable."
        clean: dict[str, Any] = {}
        bad: list[str] = []
        for key, value in flags.items():
            name = str(key or "").strip()
            if not name:
                continue
            if name not in allowed:
                bad.append(name)
                continue
            if isinstance(value, (dict, list, tuple)) or value is None:
                return None, "%s needs a true/false, number or text value." % name
            clean[name] = value
        if bad:
            return None, ("Not allowlisted: %s" % ", ".join(sorted(bad)[:8]))
        return clean, ""

    def set_lution_flags(self, flags: Mapping[str, Any]) -> tuple[bool, str]:
        if self._elevated():
            clean, error = self._clean_lution_flags(flags)
            if error:
                return False, error
            data = self._lution_cli("fflags-set", payload=clean, timeout=60)
            if not data.get("ok"):
                return False, data.get("error",
                                      "Could not save Lution flags.")
            removed = data.get("removed") or []
            if removed:
                return False, ("Some flags were refused: %s" %
                               ", ".join(map(str, removed[:8])))
            return True, "Lution flags saved."
        if lution_fflags is None:
            return False, "Lution is unavailable."
        if self.sober_running:
            return False, "Close Sober before changing its fast flags."
        clean, error = self._clean_lution_flags(flags)
        if error:
            return False, error
        try:
            lution_fflags.save_fflags(clean)
            return True, "Lution flags saved."
        except Exception as exc:
            return False, "Could not save Lution flags: %s" % exc

    def lution_flags_text(self) -> tuple[bool, str]:
        data = self.lution_flags()
        if not data.get("ok"):
            return False, data.get("error", "Lution is unavailable.")
        lines = []
        for name in sorted(data["flags"]):
            value = data["flags"][name]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value)
            lines.append("%s = %s" % (name, rendered))
        return True, "\n".join(lines)

    def set_lution_flags_text(self, text: Any) -> tuple[bool, str]:
        if lution_fflags is None:
            return False, "Lution is unavailable."
        if len(str(text or "")) > 256 * 1024:
            return False, "That flag table is too large."
        flags: dict[str, Any] = {}
        for number, raw in enumerate(str(text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                return False, "Line %d needs NAME = value." % number
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if not name:
                return False, "Line %d has no flag name." % number
            flags[name] = lution_fflags.parse_value(raw_value)
        if not flags:
            return False, "Add at least one NAME = value flag."
        return self.set_lution_flags(flags)

    def clean_lution_flags(self) -> tuple[bool, str]:
        if self._elevated():
            data = self._lution_cli("fflags-clean", timeout=60)
            if not data.get("ok"):
                return False, data.get("error",
                                      "Could not clean Lution flags.")
            removed = data.get("removed") or []
            return True, ("Removed %d unknown flag%s." %
                          (len(removed), "" if len(removed) == 1 else "s"))
        if lution_fflags is None:
            return False, "Lution is unavailable."
        if self.sober_running:
            return False, "Close Sober before changing its fast flags."
        try:
            current = lution_fflags.get_fflags()
            clean, _bad = lution_fflags.clean(current)
            lution_fflags.save_fflags(clean)
        except Exception as exc:
            return False, "Could not clean Lution flags: %s" % exc
        removed = len(current) - len(clean)
        return True, ("Removed %d unknown flag%s." %
                      (removed, "" if removed == 1 else "s"))

    def flag_profile_dir(self) -> Path:
        return self.path.parent / "flag-profiles"

    def _flag_profile_path(self, name: Any) -> Path | None:
        clean = self.clean_config_name(name)
        if not clean:
            return None
        return self.flag_profile_dir() / (clean + ".json")

    def list_flag_profiles(self) -> list[str]:
        directory = self.flag_profile_dir()
        if not directory.is_dir():
            return []
        names = []
        for path in sorted(directory.glob("*.json")):
            clean = self.clean_config_name(path.stem)
            if clean and path.is_file():
                names.append(clean)
        return names

    def save_flag_profile(self, name: Any) -> tuple[bool, str]:
        path = self._flag_profile_path(name)
        if path is None:
            return False, "Give the profile a name without slashes."
        data = self.lution_flags()
        if not data.get("ok"):
            return False, data.get("error", "Lution is unavailable.")
        clean, error = self._clean_lution_flags(data["flags"])
        if error:
            return False, error
        if not clean:
            return False, "Add at least one fast flag before saving a profile."
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(json.dumps({"flags": clean,
                                        "saved": int(time.time())}, indent=2)
                            + "\n")
            os.replace(temp, path)
            _claim_owner(path)
        except OSError as exc:
            return False, "Could not save that profile: %s" % exc
        return True, "Flag profile “%s” saved." % path.stem

    def load_flag_profile(self, name: Any) -> tuple[bool, str]:
        path = self._flag_profile_path(name)
        if path is None or not path.is_file():
            return False, "That flag profile does not exist."
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            return False, "Could not read that profile: %s" % exc
        flags = data.get("flags") if isinstance(data, dict) else None
        if not isinstance(flags, dict):
            return False, "That flag profile is malformed."
        ok, message = self.set_lution_flags(flags)
        if ok:
            message = "Flag profile “%s” loaded." % path.stem
        return ok, message

    def delete_flag_profile(self, name: Any) -> tuple[bool, str]:
        path = self._flag_profile_path(name)
        if path is None or not path.is_file():
            return False, "That flag profile does not exist."
        try:
            path.unlink()
        except OSError as exc:
            return False, "Could not delete that profile: %s" % exc
        return True, "Flag profile deleted."

    # -- Lution environment (Linux launch settings) -----------------------
    def lution_env(self) -> dict:
        if self._elevated():
            data = self._lution_cli("env-get", timeout=60)
            env = data.get("env")
            if not data.get("ok") or not isinstance(env, dict):
                return {"ok": False, "env": {},
                        "error": data.get("error", "Environment is unavailable.")}
            return {"ok": True, "env": env}
        if lution_envvars is None:
            return {"ok": False, "env": {},
                    "error": "The bundled Lution core is unavailable."}
        try:
            env = lution_envvars.load_vars()
            return {"ok": True, "env": env if isinstance(env, dict) else {}}
        except Exception as exc:
            return {"ok": False, "env": {},
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    def lution_env_text(self) -> tuple[bool, str]:
        data = self.lution_env()
        if not data.get("ok"):
            return False, data.get("error", "Lution is unavailable.")
        return True, "\n".join("%s = %s" % (key, value)
                               for key, value in sorted(data["env"].items()))

    def save_lution_env(self, env: Mapping[str, Any]) -> tuple[bool, str]:
        clean: dict[str, str] = {}
        invalid = []
        for key, value in env.items():
            name = str(key or "").strip()
            if not _valid_env_name(name):
                invalid.append(name or "(empty)")
                continue
            clean[name] = str(value if value is not None else "").strip()
        if invalid:
            return False, "Invalid environment variable: %s" % ", ".join(invalid[:6])
        if self._elevated():
            data = self._lution_cli("env-set", payload=clean, timeout=60)
            if not data.get("ok"):
                return False, data.get("error",
                                      "Could not save environment variables.")
            return True, "Environment saved for the next launch."
        if lution_envvars is None:
            return False, "The bundled Lution core is unavailable."
        try:
            lution_envvars.save_vars(clean)
        except Exception as exc:
            return False, "Could not save environment variables: %s" % exc
        return True, "Environment saved for the next launch."

    def save_lution_env_text(self, text: Any) -> tuple[bool, str]:
        if len(str(text or "")) > 64 * 1024:
            return False, "That environment list is too large."
        env: dict[str, str] = {}
        for number, raw in enumerate(str(text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                return False, "Line %d needs NAME = value." % number
            key, value = line.split("=", 1)
            key = key.strip()
            if not _valid_env_name(key):
                return False, "Line %d has an invalid variable name." % number
            env[key] = value.strip()
        return self.save_lution_env(env)

    def launch_sober(self, plain: bool = False, url: str | None = None
                     ) -> tuple[bool, str]:
        with self._lock:
            if self._sober_launching:
                return True, "Sober is already starting."
            if self._sober_maintenance:
                return False, "Sober is being installed or updated; wait for that to finish."
            self._sober_launching = True
        effects = self.active_launch_effects()
        if self._elevated():
            if self.sober_running:
                self._sober_launching = False
                return True, "Sober is already running."
            self._sober_error = ""

            def elevated_worker() -> None:
                try:
                    installed = self._lution_cli("ensure", timeout=600)
                    if not installed.get("ok"):
                        detail = (installed.get("message")
                                  or installed.get("error") or "unknown error")
                        self._sober_error = "Sober install/update failed: %s" % detail
                        return
                    launch_args = []
                    if plain:
                        launch_args.append("--plain")
                    if url:
                        launch_args.append(url)
                    for key, flag in (("mangohud", "--mangohud"),
                                      ("gamemode", "--gamemode"),
                                      ("gamescope", "--gamescope")):
                        if effects.get(key):
                            launch_args.append(flag)
                    launched = self._lution_cli("launch", args=tuple(launch_args),
                                                timeout=60)
                    if not launched.get("ok"):
                        self._sober_error = str(
                            launched.get("error") or "Could not start Sober.")
                        return
                    self._sober = None
                    self._sober_started = time.monotonic()
                    self._sober_running_cache = True
                    self._sober_running_checked = time.monotonic()
                except Exception as exc:
                    self._sober_error = "%s: %s" % (type(exc).__name__, exc)
                finally:
                    with self._lock:
                        self._sober_launching = False
                    self._emit()

            thread = threading.Thread(target=elevated_worker,
                                      name="wxb-studio-sober", daemon=True)
            self._sober_thread = thread
            thread.start()
            self._emit()
            return True, "Starting Sober…"
        if lution_launcher is None:
            self._sober_launching = False
            return False, "The Sober launcher is unavailable."
        if self.sober_running:
            self._sober_launching = False
            return True, "Sober is already running."
        self._sober_error = ""

        def worker() -> None:
            try:
                # First launch on a clean machine: install the Flatpak, then
                # use the Lution launcher so its fflag config is bind-mounted.
                if lution_sober is not None:
                    installed, detail = lution_sober.ensure_sober()
                    if not installed:
                        self._sober_error = "Sober install/update failed: %s" % detail
                        self._emit()
                        return
                fn = lution_launcher.launch_plain if plain else lution_launcher.launch
                # WXB is often elevated for input access.  A game (and
                # Flatpak's /run/flatpak runtime) belongs to the desktop
                # user, not root: drop privileges for the child only.
                proc = fn(url, preexec_fn=_owner_launch_preexec(),
                          mangohud=effects["mangohud"],
                          gamemode=effects["gamemode"],
                          gamescope=effects["gamescope"])
                self._sober = proc
                self._sober_started = time.monotonic()
            except Exception as exc:
                self._sober_error = str(exc)
            finally:
                with self._lock:
                    self._sober_launching = False
                self._emit()

        thread = threading.Thread(target=worker, name="wxb-studio-sober",
                                  daemon=True)
        self._sober_thread = thread
        thread.start()
        self._emit()
        return True, "Starting Sober…"

    @staticmethod
    def parse_place(value: Any) -> tuple[str, str] | None:
        """Return (place id, optional server id) from an old-build-style URL."""
        import re
        text = str(value or "").strip()
        if not text:
            return None
        place = re.search(r"(?:placeId=|/games/)(\d+)", text)
        if not place:
            place = re.match(r"^(\d{3,20})$", text)
        if not place:
            return None
        place_id = place.group(1)
        server = re.search(r"(?:gameInstanceId=|serverId=)([A-Za-z0-9-]+)", text)
        return place_id, (server.group(1) if server else "")

    def remember_place(self, place_id: Any, server: str = "") -> None:
        pid = str(place_id or "").strip()
        if not pid.isdigit():
            return
        places = [dict(p) for p in self.settings.get("places", [])
                  if str(p.get("id")) != pid]
        places.insert(0, {"id": pid, "server": str(server or ""),
                          "name": "", "fav": False, "ts": time.time()})
        self.settings["places"] = places[:80]
        self.save()

    def stop_sober(self) -> tuple[bool, str]:
        if self._elevated():
            self._sober = None
            self._sober_running_cache = False
            self._sober_running_checked = time.monotonic()
            data = self._lution_cli("stop", timeout=30)
            ok = bool(data.get("ok"))
            self._sober_error = "" if ok else str(
                data.get("error") or "Could not stop Sober.")
            self._lution_runtime_cache = None
            self._lution_runtime_dirty = True
            self._emit()
            return ok, ("Stopping Sober…" if ok else self._sober_error)
        proc, self._sober = self._sober, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            # Do not make the UI wait on a stale Flatpak session helper.
            subprocess.Popen(["flatpak", "kill", "org.vinegarhq.Sober"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass
        self._sober_running_cache = False
        self._sober_running_checked = time.monotonic()
        self._lution_runtime_cache = None
        self._lution_runtime_dirty = True
        self._emit()
        return True, "Stopping Sober…"

    # -- alts --------------------------------------------------------------
    def scan_alts(self) -> None:
        if roalt is None or self._alt_scanning:
            return
        self._alt_scanning = True
        self._alt_error = ""

        def worker() -> None:
            try:
                owner = _desktop_owner()
                alt_env = self._alt_env()
                install = roalt.discover(alt_env)
                found = roalt.discover_profiles(install, alt_env)
                store = self._alt_store()
                store.sync(found)
                with self._lock:
                    self._alt_install = install
                    self._profiles = store.names()
                    self._alt_manager = roalt.LaunchManager(
                        install,
                        env=alt_env,
                        drop_uid=owner[0] if owner is not None else None,
                        drop_gid=owner[1] if owner is not None else None,
                        cpu_quota=self.settings["alts"]["cpu_quota"])
                    self._alt_scanned = True
            except Exception as exc:
                self._alt_error = str(exc)
                self._alt_scanned = True
            finally:
                self._alt_scanning = False
                with self._lock:
                    if self._alt_thread is threading.current_thread():
                        self._alt_thread = None
            self._emit()

        thread = threading.Thread(target=worker, name="wxb-studio-alts",
                                  daemon=True)
        self._alt_thread = thread
        thread.start()
        self._emit()

    def launch_alt(self, profile: str) -> tuple[bool, str]:
        if self._alt_manager is None:
            if self._alt_scanning or (self._alt_thread is not None
                                      and self._alt_thread.is_alive()):
                return False, "Cordial is still being detected. Try again in a moment."
            return False, self._alt_error or "Cordial was not found."
        try:
            if roalt is None:
                return False, "Cordial profile support is unavailable."
            owner = _desktop_owner()
            opts = self.settings["alts"]
            # Fail closed: an alt must have its hard frame cap written into
            # its own Cordial flags before a game is allowed to start.
            roalt.prepare_profile(
                profile, self._alt_install, env=self._alt_env(),
                cap=opts["fps_cap"], low_graphics=opts["low_graphics"],
                owner_uid=owner[0] if owner is not None else None,
                owner_gid=owner[1] if owner is not None else None)
            self._alt_manager.launch(profile)
            self._recent = [profile] + [x for x in self._recent if x != profile]
            self._schedule_alt_tile()
            self._emit()
            return True, "Launching %s." % profile
        except Exception as exc:
            self._emit()
            return False, str(exc)

    def add_alt_profile(self, profile: str) -> tuple[bool, str]:
        """Create a real Cordial profile directory from the WXB roster."""
        if roalt is None:
            return False, "Cordial profile support is unavailable."
        try:
            name = roalt.validate_profile(profile)
        except Exception as exc:
            return False, str(exc)
        env = self._alt_env()
        install = self._alt_install
        if install is None:
            try:
                install = roalt.discover(env)
            except Exception as exc:
                return False, str(exc)
        owner = _desktop_owner()
        try:
            roalt.create_profile(
                name, install, env=env,
                owner_uid=owner[0] if owner is not None else None,
                owner_gid=owner[1] if owner is not None else None)
            try:
                store = self._alt_store()
                store.sync([name])
                names = store.names()
            except Exception:
                # Do not leave a profile without an index entry behind if the
                # store is unwritable.  The directory is empty and removable.
                try:
                    roalt.delete_profile(name, install, env=env)
                except Exception:
                    pass
                raise
        except Exception as exc:
            return False, str(exc)
        with self._lock:
            self._alt_install = install
            self._profiles = names
        self._alt_error = ""
        if self._alt_manager is None:
            # Adding from the empty state can race the first background scan.
            # Once the directory exists, run detection again so Launch has a
            # manager instead of reporting Cordial as missing.
            self.scan_alts()
        self._emit()
        return True, "Profile %s added. Launch it to sign in." % name

    def delete_alt_profile(self, profile: str) -> tuple[bool, str]:
        """Delete a stopped Cordial profile, including its local account data."""
        if roalt is None:
            return False, "Cordial profile support is unavailable."
        try:
            name = roalt.validate_profile(profile)
        except Exception as exc:
            return False, str(exc)
        running = {item.get("profile") for item in self.running_alts()
                   if item.get("running")}
        if name in running:
            return False, "Stop %s before deleting its profile." % name
        env = self._alt_env()
        try:
            roalt.delete_profile(name, self._alt_install, env=env)
        except Exception as exc:
            return False, str(exc)
        try:
            store = self._alt_store()
            store.remove(name)
        except Exception:
            # The directory is already gone.  The next scan will not revive
            # it; leaving a stale label is less harmful than hiding a failed
            # deletion behind a store-side error.
            pass
        with self._lock:
            self._profiles = [item for item in self._profiles if item != name]
            self._recent = [item for item in self._recent if item != name]
        self._alt_error = ""
        self._emit()
        return True, "Profile %s deleted." % name

    def set_alt_options(self, *, auto_tile: Any = None,
                        fps_cap: Any = None,
                        low_graphics: Any = None,
                        cpu_quota: Any = None,
                        tiling_style: Any = None) -> tuple[bool, str]:
        """Persist the Cordial cap and tiling options.

        The cap, low-graphics flags and CPU quota take effect on the next
        launch; a running client cannot be retuned without restarting it.
        Tiling is a layout choice, so it can be changed while clients run.
        """
        opts = self.settings["alts"]
        # Validate the whole update before touching the live mapping.  A
        # rejected layout must not leave a half-applied auto-tile or cap
        # value behind in memory while the file still has the old one.
        next_opts = dict(opts)
        if auto_tile is not None:
            next_opts["auto_tile"] = bool(auto_tile)
        if tiling_style is not None:
            style = str(tiling_style).strip().lower().replace("_", "-")
            aliases = {
                "side-by-side": "columns", "stacked": "rows",
                "stack": "rows", "focus": "focus-left", "top": "focus-top",
            }
            style = aliases.get(style, style)
            if style not in ALT_TILING_LABELS:
                return False, "Choose a known Cordial tiling style."
            next_opts["tiling_style"] = style
        if low_graphics is not None:
            next_opts["low_graphics"] = bool(low_graphics)
        if cpu_quota is not None:
            try:
                quota = int(float(str(cpu_quota).strip()))
            except (TypeError, ValueError, OverflowError):
                return False, "CPU quota must be a whole number."
            if not 0 <= quota <= 1600:
                return False, "CPU quota must stay between 0 and 1600%."
            next_opts["cpu_quota"] = quota
        if fps_cap is not None:
            try:
                cap = int(float(str(fps_cap).strip()))
            except (TypeError, ValueError, OverflowError):
                return False, "Alt FPS cap must be a whole number."
            if not 0 <= cap <= 1000000:
                return False, "Alt FPS cap must stay between 0 and 1000000."
            next_opts["fps_cap"] = cap
        previous = dict(opts)
        opts.update(next_opts)
        try:
            self.save()
        except OSError as exc:
            opts.clear()
            opts.update(previous)
            return False, "Could not save Cordial options: %s" % exc
        if self._alt_manager is not None:
            try:
                self._alt_manager.set_cpu_quota(opts["cpu_quota"])
            except Exception:
                traceback.print_exc()
        if opts["auto_tile"]:
            self._schedule_alt_tile()
        self._emit()
        return True, ("Alt FPS cap %d; %s; low graphics %s; CPU quota %s; "
                      "auto-tile %s." %
                      (opts["fps_cap"],
                       ALT_TILING_LABELS.get(opts["tiling_style"],
                                             "Side by side").lower(),
                       "on" if opts["low_graphics"] else "off",
                       ("%d%%" % opts["cpu_quota"]
                        if opts["cpu_quota"] else "off"),
                       "on" if opts["auto_tile"] else "off"))

    def tile_alts(self) -> tuple[bool, str]:
        if self._alt_manager is None:
            return False, self._alt_error or "Cordial was not found."
        try:
            style = self.settings["alts"].get("tiling_style", "columns")
            return self._alt_manager.tile(style=style)
        except Exception as exc:
            return False, "Could not tile Cordial windows: %s" % exc

    def cycle_alt_tiling(self) -> tuple[bool, str]:
        """Advance the Cordial layout and immediately apply it.

        This is the action behind the global tiling hotkey.  It deliberately
        tiles every press instead of only changing the saved preference, so a
        user pressing the shortcut with live clients gets an observable
        rearrangement even when auto-tile is off.
        """
        styles = [key for key, _title in ALT_TILING_CHOICES]
        current = self.settings["alts"].get("tiling_style", "columns")
        try:
            index = styles.index(current)
        except ValueError:
            index = -1
        style = styles[(index + 1) % len(styles)]
        self.settings["alts"]["tiling_style"] = style
        self.save()
        ok, message = self.tile_alts()
        self._emit()
        label = ALT_TILING_LABELS.get(style, style)
        if ok:
            return True, "%s · %s" % (label, message)
        return False, "%s selected, but %s" % (label, message)

    def _schedule_alt_tile(self) -> None:
        if (self._alt_manager is None
                or not self.settings["alts"].get("auto_tile")):
            return
        self._alt_tile_generation += 1
        generation = self._alt_tile_generation
        manager = self._alt_manager

        def worker() -> None:
            # Cordial's first-run/build-store initialization can take much
            # longer than one scheduler beat.  Keep waiting until every live
            # launch has its own mapped window; merely sleeping for a fixed
            # timeout would still leave one alt untitled indefinitely.
            for _ in range(120):
                if (generation != self._alt_tile_generation
                        or manager is not self._alt_manager
                        or not self.settings["alts"].get("auto_tile")):
                    return
                if not manager.live_launches():
                    return
                try:
                    manager.tile(
                        style=self.settings["alts"].get("tiling_style",
                                                         "columns"))
                except Exception:
                    traceback.print_exc()
                if manager.all_windows_mapped():
                    return
                time.sleep(0.5)

        thread = threading.Thread(target=worker,
                                  name="wxb-studio-alt-tile", daemon=True)
        self._alt_tile_thread = thread
        thread.start()

    def stop_alt(self, profile: str) -> bool:
        if self._alt_manager is None:
            return False
        ok = self._alt_manager.stop(profile)
        self._emit()
        return ok

    def running_alts(self) -> list[dict]:
        if self._alt_manager is None:
            return []
        try:
            return self._alt_manager.running()
        except Exception:
            return []

    # -- aggregate ---------------------------------------------------------
    def emergency_stop(self) -> None:
        """ESC's action: stop every engine, never the Sober session."""
        self.stop_all()

    def stop_all(self) -> None:
        self._hold_actions.clear()
        try:
            self.stop_recording(wait=True)
        except Exception:
            pass
        self.stop_macro(wait=True)
        self.stop_clicker()
        self.stop_rotation()
        self.stop_socd()
        # "Stop everything" means WXB's input engines, not the game the user
        # is playing. Roblox must stay open when F9 stops the clicker or when
        # WXB itself is closed.
        if self._alt_manager is not None:
            try:
                self._alt_manager.stop_all()
            except Exception:
                traceback.print_exc()
        self._emit()

    def snapshot(self) -> dict:
        running = self.ac_running
        elapsed = (max(0.0, time.monotonic() - self._ac_started)
                   if running else self._ac_last_elapsed)
        if running:
            elapsed = max(0.0, elapsed - self._ac_start_delay)
        clicks = self.click_count() if running else self._ac_last_clicks
        return {
            "clicker": {
                "running": running,
                "clicks": clicks,
                "elapsed": elapsed,
                "error": self._ac_error,
                "paused": bool(self._ac_paused),
                "blocked": self._ac_blocked,
                "stats": self.clicker_stats_snapshot(),
            },
            "rotation": {
                "enabled": self.macro_enabled,
                "running": self.rot_running,
                "done": self._rot_done,
                "elapsed": (max(0.0, time.monotonic() - self._rot_started)
                            if self.rot_running else 0.0),
                "error": self._rot_error,
            },
            "socd": self.socd_snapshot(),
            "input_lab": self.input_lab_snapshot(),
            "sober": {
                "running": self.sober_running,
                "error": self._sober_error,
                "maintenance": self._sober_maintenance,
                "detail": self._sober_detail,
                "runtime_dirty": self._lution_runtime_dirty,
            },
            "alts": {
                "scanning": self._alt_scanning,
                "scanned": self._alt_scanned,
                "error": self._alt_error,
                "profiles": list(self._profiles),
                "running": self.running_alts(),
                "auto_tile": bool(self.settings["alts"]["auto_tile"]),
                "fps_cap": int(self.settings["alts"]["fps_cap"]),
                "low_graphics": bool(self.settings["alts"]["low_graphics"]),
                "cpu_quota": int(self.settings["alts"]["cpu_quota"]),
                "tiling_style": self.settings["alts"]["tiling_style"],
                "backend": (getattr(self._alt_install, "label", "")
                            if self._alt_install is not None else ""),
            },
            "macros": self.macro_snapshot(),
            "recorder": self.recorder_snapshot(),
        }

    def shutdown(self) -> None:
        try:
            self.stop_hotkeys()
            self.stop_all()
            self.stop_controller_test()
            self._visualizer.stop()
            self._alt_tile_generation += 1
            for thread in (self._alt_thread, self._sober_thread):
                if (thread is not None and thread.is_alive()
                        and thread is not threading.current_thread()):
                    thread.join(timeout=1.0)
            if (self._alt_tile_thread is not None
                    and self._alt_tile_thread.is_alive()
                    and self._alt_tile_thread is not threading.current_thread()):
                self._alt_tile_thread.join(timeout=1.0)
        finally:
            self.save()


_app: StudioService | None = None


def service() -> StudioService:
    global _app
    if _app is None:
        _app = StudioService()
    return _app
