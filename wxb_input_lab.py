#!/usr/bin/env python3
"""Input Lab support for WXB Studio.

This module is deliberately toolkit-free.  It owns validated Input Lab
settings, keyboard remap rules, conflict checks, the optional read-only
controller monitor and the opt-in keystroke/click capture used by the
visualizers.  GTK presentation lives in ``wxb_overlay`` and ``wxb_studio``.

Privacy rules:

* Raw key/mouse events are never written to disk or logged.
* Only mappings and visual preferences are persisted.
* The SOCD timeline contains direction names only, not typed text.
* Listeners exist only while their explicit visualizer switch is enabled.
"""

from __future__ import annotations

import importlib.util
import os
import re
import selectors
import threading
import time
from typing import Any, Callable, Mapping

# Linux input-event-codes.  Keeping these as plain ints means the module is
# importable and testable on machines without the evdev package installed.
INPUT_KEY_CHOICES = (
    ("A", 30), ("B", 48), ("C", 46), ("D", 32), ("E", 18), ("F", 33),
    ("G", 34), ("H", 35), ("I", 23), ("J", 36), ("K", 37), ("L", 38),
    ("M", 50), ("N", 49), ("O", 24), ("P", 25), ("Q", 16), ("R", 19),
    ("S", 31), ("T", 20), ("U", 22), ("V", 47), ("W", 17), ("X", 45),
    ("Y", 21), ("Z", 44),
    ("1", 2), ("2", 3), ("3", 4), ("4", 5), ("5", 6), ("6", 7),
    ("7", 8), ("8", 9), ("9", 10), ("0", 11),
    ("Left", 105), ("Right", 106), ("Up", 103), ("Down", 108),
    ("Space", 57), ("Enter", 28), ("Tab", 15), ("Esc", 1),
    ("Backspace", 14), ("Delete", 111), ("Insert", 110),
    ("Home", 102), ("End", 107), ("Page up", 104), ("Page down", 109),
    ("Shift", 42), ("Ctrl", 29), ("Alt", 56), ("Super", 125),
    ("F1", 59), ("F2", 60), ("F3", 61), ("F4", 62), ("F5", 63),
    ("F6", 64), ("F7", 65), ("F8", 66), ("F9", 67), ("F10", 68),
    ("F11", 87), ("F12", 88),
)
INPUT_KEY_CODES = {name: code for name, code in INPUT_KEY_CHOICES}
INPUT_KEY_NAMES = {code: name for name, code in INPUT_KEY_CHOICES}
INPUT_KEY_CODES_LC = {name.lower(): code for name, code in INPUT_KEY_CHOICES}
DIRECTION_CODES = frozenset((30, 105, 32, 106, 17, 103, 31, 108))
DIRECTION_NAMES = {30: "LEFT", 105: "LEFT", 32: "RIGHT", 106: "RIGHT",
                   17: "UP", 103: "UP", 31: "DOWN", 108: "DOWN"}
MOUSE_BUTTON_NAMES = {272: "Left", 273: "Right", 274: "Middle",
                      275: "Back", 276: "Forward"}
CONTROLLER_BUTTON_NAMES = {
    304: "A", 305: "B", 307: "X", 308: "Y",
    310: "LB", 311: "RB", 312: "LT", 313: "RT",
    314: "Back", 315: "Start", 316: "Guide",
    317: "LS", 318: "RS", 544: "D-pad up", 545: "D-pad down",
    546: "D-pad left", 547: "D-pad right",
}
CONTROLLER_AXES = {
    0: "Left X", 1: "Left Y", 3: "Right X", 4: "Right Y",
    2: "Left trigger", 5: "Right trigger",
    16: "D-pad X", 17: "D-pad Y",
}
STICK_AXES = frozenset(("Left X", "Left Y", "Right X", "Right Y"))
TRIGGER_AXES = frozenset(("Left trigger", "Right trigger"))
CONTROLLER_BUTTONS = frozenset(CONTROLLER_BUTTON_NAMES)
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3

DEFAULT_INPUT_LAB = {
    "timeline": False,
    "deadzone": 0.12,
    "active_profile": "",
    "visualizer": {
        "keys": False,
        "clicks": False,
        "size": 100,
        "color": "#9fcaff",
        "opacity": 80,
    },
}
VISUALIZER_SIZE_MIN, VISUALIZER_SIZE_MAX = 60, 160
VISUALIZER_OPACITY_MIN, VISUALIZER_OPACITY_MAX = 20, 100
DEADZONE_MIN, DEADZONE_MAX = 0.0, 0.50
MAX_MAPPINGS = 64
PROFILE_VERSION = 1
PROFILE_MODE_HOLD, PROFILE_MODE_TOGGLE = "hold", "toggle"
PROFILE_MODES = (PROFILE_MODE_HOLD, PROFILE_MODE_TOGGLE)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SAFE_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,64}$")


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _number(value: Any, default: float, low: float, high: float,
            integer: bool = False) -> float | int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number != number:                 # NaN
        number = float(default)
    number = max(low, min(high, number))
    return int(round(number)) if integer else round(number, 3)


def normalize_input_lab(raw: Any) -> dict:
    """Return the complete, bounded Input Lab setting document."""
    source = _mapping(raw)
    visual = _mapping(source.get("visualizer"))
    color = str(visual.get("color") or "").strip()
    if not _COLOR_RE.match(color):
        color = DEFAULT_INPUT_LAB["visualizer"]["color"]
    return {
        "timeline": _bool(source.get("timeline"), False),
        "deadzone": _number(source.get("deadzone"),
                            DEFAULT_INPUT_LAB["deadzone"],
                            DEADZONE_MIN, DEADZONE_MAX),
        "active_profile": clean_profile_name(source.get("active_profile")),
        "visualizer": {
            "keys": _bool(visual.get("keys"), False),
            "clicks": _bool(visual.get("clicks"), False),
            "size": _number(visual.get("size"),
                            DEFAULT_INPUT_LAB["visualizer"]["size"],
                            VISUALIZER_SIZE_MIN, VISUALIZER_SIZE_MAX,
                            integer=True),
            "color": color.lower(),
            "opacity": _number(visual.get("opacity"),
                               DEFAULT_INPUT_LAB["visualizer"]["opacity"],
                               VISUALIZER_OPACITY_MIN,
                               VISUALIZER_OPACITY_MAX, integer=True),
        },
    }


def clean_profile_name(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_NAME_RE.match(text) else ""


def canonical_key(value: Any) -> str:
    """Canonical display token for a key name, or ``""`` when unsupported."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text in INPUT_KEY_CODES:
        return text
    return INPUT_KEY_NAMES.get(INPUT_KEY_CODES_LC.get(text.lower(), 0), "")


def key_code(value: Any) -> int | None:
    token = canonical_key(value)
    return INPUT_KEY_CODES.get(token) if token else None


def code_name(code: Any) -> str:
    try:
        return INPUT_KEY_NAMES.get(int(code), str(int(code)))
    except (TypeError, ValueError):
        return ""


def _normalise_mappings(raw_mappings: Any, mode: str) -> tuple[list | None, str]:
    if raw_mappings is None:
        raw_mappings = []
    if not isinstance(raw_mappings, (list, tuple)):
        return None, "Mappings must be a list."
    if len(raw_mappings) > MAX_MAPPINGS:
        return None, "A profile may contain at most %d mappings." % MAX_MAPPINGS
    mappings = []
    seen_sources = set()
    seen_targets = set()
    for item in raw_mappings:
        if not isinstance(item, Mapping):
            return None, "Every mapping needs a source and target."
        source = canonical_key(item.get("source"))
        target = canonical_key(item.get("target"))
        if not source or not target:
            return None, "Use a supported key name for every mapping."
        if source == target:
            return None, "A key cannot be mapped to itself (%s)." % source
        if source in seen_sources:
            return None, "Source key %s is used more than once." % source
        target_code = INPUT_KEY_CODES.get(target)
        if target_code in DIRECTION_CODES:
            return None, ("Target %s is owned by the SOCD resolver; choose a "
                          "non-direction key." % target)
        if target in seen_targets:
            return None, "Target key %s is used more than once." % target
        if mode == PROFILE_MODE_TOGGLE and INPUT_KEY_CODES.get(source) in DIRECTION_CODES:
            return None, "Toggle mode does not support direction-key sources."
        seen_sources.add(source)
        seen_targets.add(target)
        mappings.append({"source": source, "target": target})
    for item in mappings:
        if item["target"] in seen_sources:
            return None, ("Target %s is also a source key; cascading remaps "
                          "are not supported." % item["target"])
    return mappings, ""


def normalize_profile(raw: Any, require_name: bool = False) -> tuple[dict | None, str]:
    """Validate and normalise one remap profile document."""
    source = _mapping(raw)
    name = clean_profile_name(source.get("name"))
    if require_name and not name:
        return None, "Give this remap profile a name."
    mode = str(source.get("mode") or PROFILE_MODE_HOLD).strip().lower()
    if mode not in PROFILE_MODES:
        return None, "Choose hold or toggle remap mode."
    mappings, error = _normalise_mappings(source.get("mappings"), mode)
    if mappings is None:
        return None, error
    if not mappings:
        return None, "Add at least one mapping."
    return {
        "version": PROFILE_VERSION,
        "name": name,
        "mode": mode,
        "mappings": mappings,
    }, ""


def _hotkey_tokens(hotkeys: Any) -> set[str]:
    tokens = set()
    values = hotkeys.values() if isinstance(hotkeys, Mapping) else hotkeys or ()
    for value in values:
        for part in str(value or "").lower().split("+"):
            token = canonical_key(part)
            if token:
                tokens.add(token)
    return tokens


def profile_conflicts(profile: Mapping, hotkeys: Any = None) -> list[str]:
    """Conflicts detectable inside WXB; does not touch external clients."""
    conflicts = []
    canonical, error = normalize_profile(profile)
    if canonical is None:
        return [error]
    hot_tokens = _hotkey_tokens(hotkeys)
    for item in canonical["mappings"]:
        if item["source"] in hot_tokens:
            conflicts.append(
                "Source %s is a WXB hotkey; the hotkey may not fire while "
                "the cleaner owns the keyboard." % item["source"])
        if item["target"] in hot_tokens:
            conflicts.append(
                "Target %s is a WXB hotkey; the game may miss it while the "
                "global listener is active." % item["target"])
    return conflicts


def external_key_conflicts(profile: Mapping, environ: Mapping | None = None,
                           display_factory: Callable | None = None) -> list[str]:
    """Best-effort X11 passive-grab probe for already-grabbed target keys.

    This intentionally never runs unless the UI asks for a conflict check.
    It is unavailable on Wayland and when python-xlib is absent.
    """
    env = dict(os.environ if environ is None else environ)
    if not env.get("DISPLAY"):
        return []
    if (str(env.get("XDG_SESSION_TYPE") or "").lower() == "wayland"
            or env.get("WAYLAND_DISPLAY")):
        return []
    canonical, _error = normalize_profile(profile)
    if canonical is None:
        return []
    factory = display_factory
    keysym = None
    any_modifier = 1 << 15                 # X.AnyModifier when Xlib is present
    async_mode = 1                         # X.GrabModeAsync
    if factory is None:
        if importlib.util.find_spec("Xlib") is None:
            return []
        try:
            from Xlib import X, XK, display as xdisplay
        except Exception:
            return []
        factory = xdisplay.Display
        keysym = XK.string_to_keysym
        any_modifier = X.AnyModifier
        async_mode = X.GrabModeAsync

    conflicts = []
    display = None
    try:
        display = factory()
        root = display.screen().root
        for item in canonical["mappings"]:
            token = item["target"]
            try:
                sym = keysym(token.lower().replace(" ", ""))
                code = display.keysym_to_keycode(sym) if sym else 0
                if not code:
                    continue
                try:
                    root.grab_key(code, any_modifier, True, async_mode,
                                  async_mode)
                    display.sync()
                except Exception:
                    conflicts.append(token)
                finally:
                    try:
                        root.ungrab_key(code, any_modifier)
                        display.sync()
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        return []
    finally:
        if display is not None:
            try:
                display.close()
            except Exception:
                pass
    return conflicts


def read_permission(input_dir: str = "/dev/input", access=os.access,
                    exists=os.path.exists, listdir=os.listdir) -> dict:
    """Read-only input-node access, separate from uinput access."""
    nodes = []
    try:
        if exists(input_dir):
            nodes = [name for name in listdir(input_dir)
                     if name.startswith("event")]
    except OSError:
        nodes = []
    readable = [name for name in nodes
                if access(os.path.join(input_dir, name), os.R_OK)]
    return {
        "ok": bool(readable),
        "devices": len(readable),
        "total": len(nodes),
        "text": ("Readable input devices: %d." % len(readable)
                 if readable else
                 "Input access missing — add your user to the input group "
                 "and log back in. WXB does not run sudo for you."),
    }


def load_evdev():
    try:
        import evdev
        return evdev
    except Exception:
        return None


def detect_input_lab_capabilities(environ: Mapping | None = None,
                                  evdev: Any = None,
                                  euid: int | None = None,
                                  read_check: Callable | None = None) -> dict:
    """Describe what Input Lab can do without changing any permission."""
    env = dict(os.environ if environ is None else environ)
    session = str(env.get("XDG_SESSION_TYPE") or "").strip().lower()
    display = bool(str(env.get("DISPLAY") or "").strip())
    wayland = (session == "wayland"
               or bool(str(env.get("WAYLAND_DISPLAY") or "").strip()))
    module = evdev if evdev is not None else load_evdev()
    root = (os.geteuid() if euid is None else int(euid)) == 0
    readable = (read_check() if read_check is not None else read_permission())
    read_ok = bool(readable.get("ok"))
    uinput = bool(os.path.exists("/dev/uinput")
                  and os.access("/dev/uinput", os.W_OK))
    pynput_ok = importlib.util.find_spec("pynput") is not None
    xlib_ok = importlib.util.find_spec("Xlib") is not None
    visual = bool(display and not wayland and pynput_ok and not root)
    remap = bool(module is not None and read_ok and uinput and not root)
    controller = bool(module is not None and read_ok and not root)
    reasons = []
    if root:
        reasons.append("Input Lab is disabled while WXB runs as root; "
                       "restart unprivileged.")
    if module is None:
        reasons.append("python3-evdev is not installed.")
    if not read_ok:
        reasons.append("No readable /dev/input devices.")
    if not uinput:
        reasons.append("/dev/uinput is not writable; keyboard remapping is "
                       "disabled.")
    if not display:
        reasons.append("No X11 DISPLAY; global visualizers are unavailable.")
    if wayland:
        reasons.append("Native Wayland does not expose global key/mouse "
                       "capture to this app.")
    if not pynput_ok:
        reasons.append("pynput is not installed; visualizers are unavailable.")
    return {
        "session": "wayland" if wayland else ("x11" if display else "unknown"),
        "x11": bool(display and not wayland),
        "wayland": wayland,
        "read_input": read_ok,
        "read_devices": int(readable.get("devices") or 0),
        "uinput": uinput,
        "remap": remap,
        "controller": controller,
        "visualizer": visual,
        "external_conflicts": bool(visual and xlib_ok),
        "root": root,
        "reason": " ".join(reasons),
    }


class RemapEngine:
    """Pure keyboard remap state.

    ``hold`` mirrors a source key while it is down.  ``toggle`` turns a
    source press into a target toggle.  Direction sources are handled by
    ``direction_output`` because the SOCD resolver owns their press/release
    decisions; toggle is not allowed for them.
    """

    def __init__(self, profile: Mapping, mode: str | None = None) -> None:
        canonical, error = normalize_profile(profile)
        if canonical is None:
            raise ValueError(error)
        if mode is not None:
            canonical["mode"] = str(mode).strip().lower()
            if canonical["mode"] not in PROFILE_MODES:
                raise ValueError("Choose hold or toggle remap mode.")
            if (canonical["mode"] == PROFILE_MODE_TOGGLE
                    and any(INPUT_KEY_CODES.get(item["source"])
                            in DIRECTION_CODES
                            for item in canonical["mappings"])):
                raise ValueError("Toggle mode does not support direction-key "
                                 "sources.")
        self.profile = canonical
        self.mode = canonical["mode"]
        self.by_source = {item["source"]: item["target"]
                          for item in canonical["mappings"]}
        self._hold: dict[tuple[str, int], str] = {}
        self._hold_counts: dict[str, int] = {}
        self._pressed: set[tuple[str, int]] = set()
        self._toggled: dict[str, str] = {}   # target token -> source path
        self._direction = {INPUT_KEY_CODES[src]: INPUT_KEY_CODES[tgt]
                           for src, tgt in self.by_source.items()
                           if INPUT_KEY_CODES[src] in DIRECTION_CODES}
        self._error = ""

    @property
    def error(self) -> str:
        return self._error

    def handles(self, code: Any) -> bool:
        try:
            number = int(code)
        except (TypeError, ValueError):
            return False
        return number not in DIRECTION_CODES and number in INPUT_KEY_NAMES

    def direction_output(self, code: Any) -> int | None:
        try:
            return self._direction.get(int(code))
        except (TypeError, ValueError):
            return None

    def press(self, source: str, code: Any) -> list[tuple[str, int]]:
        try:
            number = int(code)
        except (TypeError, ValueError):
            return []
        token = INPUT_KEY_NAMES.get(number)
        if token not in self.by_source or number in DIRECTION_CODES:
            return []
        key = (str(source), number)
        if key in self._pressed:
            return []
        self._pressed.add(key)
        target = INPUT_KEY_CODES[self.by_source[token]]
        if self.mode == PROFILE_MODE_TOGGLE:
            if target in self._toggled:
                self._toggled.pop(target, None)
                return [("up", target)]
            self._toggled[target] = str(source)
            return [("down", target)]
        self._hold[key] = self.by_source[token]
        count = self._hold_counts.get(self.by_source[token], 0)
        self._hold_counts[self.by_source[token]] = count + 1
        return [("down", target)] if count == 0 else []

    def release(self, source: str, code: Any) -> list[tuple[str, int]]:
        try:
            number = int(code)
        except (TypeError, ValueError):
            return []
        key = (str(source), number)
        self._pressed.discard(key)
        if self.mode != PROFILE_MODE_HOLD:
            return []
        target = self._hold.pop(key, None)
        if target is None:
            return []
        count = self._hold_counts.get(target, 1) - 1
        if count > 0:
            self._hold_counts[target] = count
            return []
        self._hold_counts.pop(target, None)
        return [("up", INPUT_KEY_CODES[target])]

    def forget_source(self, source: str) -> list[tuple[str, int]]:
        path = str(source)
        ops = []
        for key in [key for key in self._hold if key[0] == path]:
            target = self._hold.pop(key, None)
            if target:
                count = self._hold_counts.get(target, 1) - 1
                if count > 0:
                    self._hold_counts[target] = count
                else:
                    self._hold_counts.pop(target, None)
                    ops.append(("up", INPUT_KEY_CODES[target]))
        for key in [key for key in self._pressed if key[0] == path]:
            self._pressed.discard(key)
        for target in [target for target, owner in self._toggled.items()
                       if owner == path]:
            self._toggled.pop(target, None)
            ops.append(("up", target))
        return ops

    def release_all(self) -> list[tuple[str, int]]:
        codes = {INPUT_KEY_CODES[target]
                 for target in self._hold.values()}
        codes.update(self._toggled)
        self._hold.clear()
        self._hold_counts.clear()
        self._pressed.clear()
        self._toggled.clear()
        return [("up", code) for code in sorted(codes)]

    def down_codes(self) -> list[int]:
        codes = {INPUT_KEY_CODES[target] for target in self._hold.values()}
        codes.update(self._toggled)
        return sorted(codes)

    def snapshot(self) -> dict:
        down = {code_name(code) for code in self.down_codes()}
        return {
            "mode": self.mode,
            "mappings": len(self.by_source),
            "down": sorted(name for name in down if name),
            "error": self._error,
        }


class ControllerMonitor:
    """Read-only evdev gamepad monitor.  Never grabs or injects events."""

    def __init__(self, evdev: Any = None, on_change: Callable | None = None,
                 deadzone: float = DEFAULT_INPUT_LAB["deadzone"],
                 access_check: Callable | None = None, rescan: float = 1.0,
                 tick: float = 0.1) -> None:
        self._evdev = evdev if evdev is not None else load_evdev()
        self.on_change = on_change
        self.deadzone = float(deadzone)
        self.access_check = access_check
        self.rescan = float(rescan)
        self.tick = float(tick)
        self.running = False
        self.error = ""
        self._thread = None
        self._stop = threading.Event()
        self._sel = None
        self._devices: dict[str, Any] = {}
        self._states: dict[str, dict] = {}
        self._last_scan = 0.0
        self._io = threading.RLock()

    def start(self):
        if self.running:
            return True, "Controller test is already running."
        if self._evdev is None:
            self.error = ("python3-evdev is not installed; controller "
                          "testing is unavailable.")
            return False, self.error
        if self.access_check is not None:
            perm = self.access_check()
            if not perm.get("ok"):
                self.error = perm.get("text") or "Input access denied."
                return False, self.error
        self.error = ""
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run,
                                        name="wxb-controller", daemon=True)
        self._thread.start()
        return True, "Controller test started."

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._io:
            for dev in list(self._devices.values()):
                self._close(dev)
            self._devices.clear()
            self._states.clear()
            self.running = False
            self._thread = None
        self._notify()

    def set_deadzone(self, value: Any):
        self.deadzone = _number(value, self.deadzone, DEADZONE_MIN,
                                DEADZONE_MAX)
        self._notify()

    def snapshot(self) -> dict:
        with self._io:
            primary = next(iter(self._states.values()), None)
            names = [state.get("name", "Controller")
                     for state in self._states.values()]
            if primary is None:
                return {"running": self.running, "connected": False,
                        "name": "", "names": names, "buttons": [],
                        "axes": {}, "deadzone": self.deadzone,
                        "error": self.error}
            state = self._copy_state(primary)
            state.update({"running": self.running, "connected": True,
                          "names": names, "deadzone": self.deadzone,
                          "error": self.error})
            return state

    def _copy_state(self, state: Mapping) -> dict:
        axes = {}
        for name, raw in (state.get("axes") or {}).items():
            value = _deadzone_value(raw, self.deadzone) if name in STICK_AXES else raw
            axes[name] = {"raw": round(float(raw), 3),
                          "value": round(float(value), 3)}
        return {"name": state.get("name", "Controller"),
                "buttons": list(state.get("buttons") or []),
                "axes": axes}

    def _run(self):
        try:
            self._sel = selectors.DefaultSelector()
            self._scan(force=True)
            while not self._stop.is_set():
                try:
                    ready = self._sel.select(timeout=self.tick)
                except (OSError, ValueError):
                    ready = []
                for key, _mask in ready:
                    dev = self._devices.get(key.data)
                    if dev is not None:
                        self._read(key.data, dev)
                self._scan()
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self._finish()

    def _scan(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_scan < self.rescan:
            return
        self._last_scan = now
        try:
            try:
                paths = list(self._evdev.list_devices(writable=False))
            except TypeError:
                paths = list(self._evdev.list_devices())
        except Exception:
            return
        for path in list(self._devices):
            if path not in paths:
                self._drop(path, "Controller disconnected; waiting for it "
                                 "to reconnect.")
        for path in paths:
            if path in self._devices:
                continue
            try:
                dev = self._evdev.InputDevice(path)
            except (OSError, PermissionError):
                continue
            if not self._is_controller(dev):
                self._close(dev)
                continue
            try:
                self._sel.register(dev.fd, selectors.EVENT_READ, path)
            except Exception:
                self._close(dev)
                continue
            self._devices[path] = dev
            self._states[path] = {"name": getattr(dev, "name", "Controller"),
                                  "buttons": [], "axes": {}}
            self.error = ""
            self._seed(dev, self._states[path])
            self._notify()

    @staticmethod
    def _is_controller(dev: Any) -> bool:
        try:
            caps = dev.capabilities()
        except Exception:
            return False
        keys = set(int(code) for code in caps.get(EV_KEY, ()))
        axes = set(int(code) for code in caps.get(EV_ABS, ()))
        return bool(keys & CONTROLLER_BUTTONS and
                    axes & set(CONTROLLER_AXES))

    def _seed(self, dev: Any, state: dict):
        try:
            active = {int(code) for code in (dev.active_keys() or ())}
        except Exception:
            active = set()
        state["buttons"] = sorted(CONTROLLER_BUTTON_NAMES[code]
                                  for code in active
                                  if code in CONTROLLER_BUTTON_NAMES)

    def _read(self, path: str, dev: Any):
        for _ in range(64):
            try:
                event = dev.read_one()
            except BlockingIOError:
                return
            except OSError:
                self._drop(path, "Controller disconnected; waiting for it "
                                 "to reconnect.")
                return
            if event is None:
                return
            self._handle(path, event)

    def _handle(self, path: str, event: Any):
        state = self._states.get(path)
        if state is None:
            return
        etype = getattr(event, "type", None)
        code = int(getattr(event, "code", -1))
        value = int(getattr(event, "value", 0))
        changed = False
        if etype == EV_KEY and code in CONTROLLER_BUTTON_NAMES:
            label = CONTROLLER_BUTTON_NAMES[code]
            buttons = set(state["buttons"])
            if value:
                buttons.add(label)
            else:
                buttons.discard(label)
            state["buttons"] = sorted(buttons)
            changed = True
        elif etype == EV_ABS and code in CONTROLLER_AXES:
            label = CONTROLLER_AXES[code]
            raw = _abs_normalized(value, getattr(event, "info", None),
                                  trigger=label in TRIGGER_AXES)
            state["axes"][label] = raw
            changed = True
        if changed:
            self._notify()

    def _drop(self, path: str, error: str = ""):
        dev = self._devices.pop(path, None)
        self._states.pop(path, None)
        if self._sel is not None:
            try:
                self._sel.unregister(dev.fd)
            except Exception:
                pass
        if dev is not None:
            self._close(dev)
        if error:
            self.error = error
        self._notify()

    @staticmethod
    def _close(dev: Any):
        try:
            dev.close()
        except Exception:
            pass

    def _finish(self):
        for dev in list(self._devices.values()):
            self._close(dev)
        self._devices.clear()
        self._states.clear()
        if self._sel is not None:
            try:
                self._sel.close()
            except Exception:
                pass
            self._sel = None
        self.running = False
        self._thread = None
        self._notify()

    def _notify(self):
        if self.on_change is None:
            return
        try:
            self.on_change(self.snapshot())
        except Exception:
            pass


def _abs_normalized(value: int, info: Any, trigger: bool = False) -> float:
    low = int(getattr(info, "min", 0) or 0)
    high = int(getattr(info, "max", 255) or 255)
    if high <= low:
        return 0.0
    if trigger:
        return max(0.0, min(1.0, (value - low) / float(high - low)))
    return max(-1.0, min(1.0, 2.0 * (value - low) / float(high - low) - 1.0))


def _deadzone_value(value: float, deadzone: float) -> float:
    if abs(value) <= deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return max(-1.0, min(1.0, (abs(value) - deadzone) /
                        max(0.0001, 1.0 - deadzone))) * sign


class VisualizerCapture:
    """Opt-in global key/click capture for the two visualizers.

    It consumes pynput's event callbacks and never keeps a text buffer.  The
    callback receives only a display token ("A", "Ctrl", "Left click") and,
    for clicks, the pointer coordinates.
    """

    def __init__(self, on_key: Callable | None = None,
                 on_click: Callable | None = None,
                 keyboard_factory: Callable | None = None,
                 mouse_factory: Callable | None = None,
                 environ: Mapping | None = None) -> None:
        self.on_key = on_key
        self.on_click = on_click
        self._keyboard_factory = keyboard_factory
        self._mouse_factory = mouse_factory
        self._environ = dict(os.environ if environ is None else environ)
        self._keyboard = None
        self._mouse = None
        self.keys = False
        self.clicks = False
        self.running = False
        self.error = ""

    def start(self, keys: bool = False, clicks: bool = False):
        self.stop()
        self.keys, self.clicks = bool(keys), bool(clicks)
        if not (self.keys or self.clicks):
            self.error = ""
            return True, "Visualizers off."
        caps = detect_input_lab_capabilities(self._environ)
        if not caps.get("visualizer"):
            self.error = (caps.get("reason") or
                          "Global visualizer capture is unavailable.")
            self.keys = self.clicks = False
            return False, self.error
        try:
            if self._keyboard_factory is None or self._mouse_factory is None:
                from pynput import keyboard, mouse
                keyboard_factory = self._keyboard_factory or keyboard.Listener
                mouse_factory = self._mouse_factory or mouse.Listener
            else:
                keyboard_factory = self._keyboard_factory
                mouse_factory = self._mouse_factory
            if self.keys:
                self._keyboard = keyboard_factory(on_press=self._key_press)
                self._keyboard.start()
            if self.clicks:
                self._mouse = mouse_factory(on_click=self._mouse_click)
                self._mouse.start()
        except Exception as exc:
            self.stop()
            self.error = "Could not start visualizer capture: %s" % exc
            return False, self.error
        self.running = True
        self.error = ""
        return True, "Visualizer capture started."

    def stop(self):
        for listener in (self._keyboard, self._mouse):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._keyboard = self._mouse = None
        self.keys = self.clicks = False
        self.running = False

    def snapshot(self) -> dict:
        return {"running": self.running, "keys": self.keys,
                "clicks": self.clicks, "error": self.error,
                "session": detect_input_lab_capabilities(self._environ).get(
                    "session", "unknown")}

    def _key_press(self, key: Any):
        label = format_key(key)
        if label and self.on_key is not None:
            try:
                self.on_key(label)
            except Exception:
                pass

    def _mouse_click(self, x: Any, y: Any, button: Any, pressed: bool):
        if not pressed or self.on_click is None:
            return
        try:
            label = "Left click"
            name = getattr(button, "name", "")
            if name == "right":
                label = "Right click"
            elif name == "middle":
                label = "Middle click"
            self.on_click(label, int(x), int(y))
        except Exception:
            pass


def format_key(key: Any) -> str:
    """Display one key identifier; never returns typed text sequences."""
    char = getattr(key, "char", None)
    if char and len(char) == 1:
        return "Space" if char.isspace() else char.upper()
    name = str(getattr(key, "name", "") or "").strip().lower()
    if not name:
        return ""
    aliases = {"ctrl_l": "Ctrl", "ctrl_r": "Ctrl", "shift_l": "Shift",
               "shift_r": "Shift", "alt_l": "Alt", "alt_r": "Alt",
               "cmd_l": "Super", "cmd_r": "Super", "esc": "Esc",
               "return": "Enter", "space": "Space"}
    if name in aliases:
        return aliases[name]
    if name.startswith("f") and name[1:].isdigit():
        return name.upper()
    return name.replace("_", " ").title()


def timeline_entry(kind: str, code: Any, direction: str = "",
                   output: str = "") -> dict:
    """A bounded, non-text Input Lab event."""
    return {"at": round(time.monotonic(), 3),
            "kind": "down" if kind == "down" else "up",
            "key": code_name(code),
            "direction": str(direction or ""),
            "out": str(output or "")}
