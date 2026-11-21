#!/usr/bin/env python3
"""TinyTask-style macro recording and playback for WXB Studio.

The module is deliberately independent of GTK.  A macro is a versioned JSON
document containing timestamped mouse/keyboard events.  Recording uses
pynput's X11 listeners; playback uses an injected sink so the timing, loop,
stop and release behavior can be tested without a desktop session.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

MACRO_FORMAT = "wxb.macro"
MACRO_VERSION = 1
MACRO_SUFFIX = ".wxbmacro.json"
MAX_EVENTS = 100000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DURATION = 24 * 60 * 60.0
SLEEP_GAP = 10.0
EVENT_TYPES = ("mouse_move", "mouse_button", "mouse_scroll", "key")
BUTTONS = ("left", "right", "middle")
SPEED_MIN = 0.1
SPEED_MAX = 10.0
LOOP_MODES = ("once", "count", "continuous")
LOOP_COUNT_MAX = 100000


class MacroError(ValueError):
    """A user-facing macro validation/playback error."""


class MacroStopped(Exception):
    """Playback was stopped by the user; not an error."""


class MacroInterrupted(MacroError):
    """Playback stopped because the session became unsafe."""


def _key_helpers():
    """Return engine key helpers without making module import fatal."""
    try:
        import wxb_engine as engine
        return engine.normalize_key, engine.parse_key_token
    except Exception:
        return None, None


def normalize_key_token(key: Any) -> str:
    """Convert a pynput key to the stable token stored in a macro."""
    normalize, parse = _key_helpers()
    if normalize is None:
        return ""
    try:
        token = normalize(key)
    except Exception:
        return ""
    if not isinstance(token, str) or not token or len(token) > 40:
        return ""
    try:
        if parse(token) is None:
            return ""
    except Exception:
        return ""
    return token


def _finite(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MacroError("%s must be a number." % field)
    if not math.isfinite(number):
        raise MacroError("%s must be finite." % field)
    return number


def _event_number(event: Mapping[str, Any], field: str) -> float:
    return _finite(event.get(field), field)


def _event_int(event: Mapping[str, Any], field: str) -> int:
    number = _event_number(event, field)
    if number != int(number):
        raise MacroError("%s must be a whole number." % field)
    return int(number)


def _clean_event(raw: Any) -> dict:
    if not isinstance(raw, Mapping):
        raise MacroError("Every macro step must be an object.")
    kind = str(raw.get("type") or "")
    if kind not in EVENT_TYPES:
        raise MacroError("Unknown macro step type.")
    at = _event_number(raw, "at")
    if at < 0 or at > MAX_DURATION:
        raise MacroError("Macro step time is out of range.")
    event: dict[str, Any] = {"at": round(at, 6), "type": kind}
    if kind == "mouse_move":
        event["x"] = _event_int(raw, "x")
        event["y"] = _event_int(raw, "y")
        if abs(event["x"]) > 100000 or abs(event["y"]) > 100000:
            raise MacroError("Mouse coordinate is out of range.")
    elif kind == "mouse_button":
        button = str(raw.get("button") or "")
        if button not in BUTTONS:
            raise MacroError("Mouse button must be left, right or middle.")
        if not isinstance(raw.get("pressed"), bool):
            raise MacroError("Mouse button state must be true or false.")
        event["button"] = button
        event["pressed"] = raw["pressed"]
    elif kind == "mouse_scroll":
        event["dx"] = _event_int(raw, "dx")
        event["dy"] = _event_int(raw, "dy")
        if abs(event["dx"]) > 10000 or abs(event["dy"]) > 10000:
            raise MacroError("Scroll amount is out of range.")
    else:
        token = raw.get("key")
        if not isinstance(token, str) or not token:
            raise MacroError("Keyboard steps need a key.")
        _, parse = _key_helpers()
        try:
            supported = parse(token) is not None
        except Exception:
            supported = False
        if not supported:
            raise MacroError("Unsupported keyboard key in macro.")
        if not isinstance(raw.get("pressed"), bool):
            raise MacroError("Keyboard state must be true or false.")
        event["key"] = token
        event["pressed"] = raw["pressed"]
    return event


def validate_macro(raw: Any) -> dict:
    """Validate and normalize a versioned macro document.

    The returned document contains only known keys and sanitized event fields.
    Invalid data raises :class:`MacroError`; it is never partially applied.
    """
    if not isinstance(raw, Mapping):
        raise MacroError("That file is not a macro object.")
    if raw.get("format") != MACRO_FORMAT:
        raise MacroError("That is not a WXB macro file.")
    version = raw.get("version")
    if version != MACRO_VERSION:
        raise MacroError("Unsupported macro version.")
    events = raw.get("events")
    if not isinstance(events, list):
        raise MacroError("Macro events must be a list.")
    if len(events) > MAX_EVENTS:
        raise MacroError("Macro has too many steps (limit %d)." % MAX_EVENTS)
    cleaned = [_clean_event(item) for item in events]
    cleaned.sort(key=lambda item: item["at"])
    name = raw.get("name", "Untitled macro")
    if not isinstance(name, str):
        name = "Untitled macro"
    name = name.strip()[:80] or "Untitled macro"
    created = raw.get("created", "")
    if not isinstance(created, str):
        created = ""
    duration = cleaned[-1]["at"] if cleaned else 0.0
    return {
        "format": MACRO_FORMAT,
        "version": MACRO_VERSION,
        "name": name,
        "created": created[:64],
        "events": cleaned,
        "duration": round(duration, 6),
    }


def make_macro(name: str, events: Iterable[Mapping[str, Any]]) -> dict:
    """Build a validated macro document from freshly recorded events."""
    return validate_macro({
        "format": MACRO_FORMAT,
        "version": MACRO_VERSION,
        "name": str(name or "Untitled macro")[:80],
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "events": list(events),
    })


def macro_summary(doc: Mapping[str, Any] | None) -> dict:
    events = list((doc or {}).get("events") or [])
    duration = float((doc or {}).get("duration") or 0.0)
    return {
        "name": str((doc or {}).get("name") or "Untitled macro"),
        "steps": len(events),
        "duration": max(0.0, duration),
        "events": events,
    }


def detect_capabilities(environ: Mapping[str, str] | None = None) -> dict:
    """Describe what the current Linux session can actually record/play.

    pynput uses X11 XTest/XTEST listeners.  Native Wayland does not expose the
    equivalent global capture/injection APIs; XWayland may allow X11 clients
    only.  We disable recording on Wayland and report the limitation rather
    than pretending xdotool/ydotool/wtype can record.
    """
    env = dict(os.environ if environ is None else environ)
    display = bool(str(env.get("DISPLAY") or "").strip())
    session = str(env.get("XDG_SESSION_TYPE") or "").strip().lower()
    wayland = session == "wayland" or bool(str(env.get("WAYLAND_DISPLAY") or "").strip())
    pynput_ok = importlib.util.find_spec("pynput") is not None
    if not pynput_ok:
        return {"session": session or "unknown", "x11": False, "xwayland": False,
                "record": False, "play": False, "hotkeys": False,
                "reason": "pynput is not installed; install requirements.txt."}
    if not display:
        return {"session": session or "unknown", "x11": False, "xwayland": False,
                "record": False, "play": False, "hotkeys": False,
                "reason": "No X11 DISPLAY is available. Native Wayland global "
                          "recording/injection is not supported."}
    if wayland:
        return {"session": "wayland", "x11": False, "xwayland": True,
                "record": False, "play": bool(display), "hotkeys": bool(display),
                "reason": "XWayland is available for X11 windows, but global "
                          "recording is unavailable on Wayland. Record in an X11 "
                          "session; playback is limited to XWayland windows."}
    return {"session": session or "x11", "x11": True, "xwayland": False,
            "record": True, "play": True, "hotkeys": True, "reason": ""}


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if not name or name in (".", "..") or len(name) > 64:
        return ""
    if "/" in name or "\\" in name or "\x00" in name or any(
            ord(char) < 32 for char in name):
        return ""
    return name


def default_macro_root() -> Path:
    xdg = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "autoclicker" / "macros"


class MacroStore:
    """Private local macro storage and validated import/export."""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root) if root else default_macro_root()

    def path(self, name: Any) -> Path | None:
        clean = clean_name(name)
        return self.root / (clean + MACRO_SUFFIX) if clean else None

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise MacroError("Refusing a symlinked macro directory.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def list(self) -> list[str]:
        try:
            if self.root.is_symlink():
                return []
            return sorted(path.name[:-len(MACRO_SUFFIX)]
                          for path in self.root.glob("*" + MACRO_SUFFIX)
                          if path.is_file() and not path.is_symlink())
        except OSError:
            return []

    def _read_document(self, path: Path) -> dict:
        if path.is_symlink():
            raise MacroError("Refusing a symlinked macro file.")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MacroError("Could not read macro: %s" % exc)
        if size > MAX_FILE_BYTES:
            raise MacroError("Macro file is too large.")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None
                    raw = json.load(handle)
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, ValueError) as exc:
            raise MacroError("Could not read macro: %s" % exc)
        return validate_macro(raw)

    def load(self, name: Any) -> tuple[dict | None, str]:
        target = self.path(name)
        if target is None:
            return None, "Give this macro a name."
        try:
            return self._read_document(target), ""
        except MacroError as exc:
            return None, str(exc)

    def load_path(self, path: Any) -> tuple[dict | None, str]:
        try:
            return self._read_document(Path(path).expanduser()), ""
        except MacroError as exc:
            return None, str(exc)

    def _write_document(self, target: Path, doc: Mapping[str, Any]) -> None:
        payload = json.dumps(validate_macro(doc), indent=2, sort_keys=True) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_suffix(target.suffix + ".tmp")
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

    def save(self, name: Any, doc: Mapping[str, Any],
             overwrite: bool = True) -> tuple[bool, str, Path | None]:
        target = self.path(name)
        if target is None:
            return False, "Give this macro a name.", None
        if not overwrite and target.exists():
            return False, "A macro with that name already exists.", target
        try:
            self._ensure_root()
            if target.parent.is_symlink():
                raise MacroError("Refusing a symlinked macro directory.")
            self._write_document(target, doc)
        except (OSError, MacroError) as exc:
            return False, "Could not save macro: %s" % exc, target
        return True, "Saved macro “%s”." % clean_name(name), target

    def import_file(self, source: Any, name: str = "") -> tuple[bool, str]:
        doc, error = self.load_path(source)
        if error:
            return False, error
        chosen = clean_name(name) or clean_name(doc.get("name"))
        if not chosen:
            chosen = "Imported macro"
        ok, message, _path = self.save(chosen, doc, overwrite=False)
        return ok, message

    def export_file(self, doc: Mapping[str, Any], target: Any) -> tuple[bool, str]:
        path = Path(target).expanduser()
        try:
            if path.is_symlink():
                return False, "Refusing to overwrite a symlinked export."
            self._write_document(path, doc)
        except (OSError, MacroError) as exc:
            return False, "Could not export macro: %s" % exc
        return True, "Exported macro to %s." % path


class MacroRecorder:
    """Record global X11 mouse/keyboard events with relative timing."""

    def __init__(self, ignore_keys: Iterable[str] = (),
                 control_combos: Iterable[Iterable[str]] = (),
                 clock: Callable = time.monotonic,
                 max_events: int = MAX_EVENTS,
                 on_error: Callable[[str], None] | None = None) -> None:
        self.ignore_keys = {str(key).upper() for key in ignore_keys}
        self.control_combos = [
            {str(token).upper() for token in combo if str(token).strip()}
            for combo in control_combos]
        self.control_combos = [combo for combo in self.control_combos if combo]
        self._clock = clock
        self._max_events = max(1, int(max_events))
        self._on_error = on_error
        self._events: list[dict] = []
        self._lock = threading.RLock()
        self._start = 0.0
        self._stopping = False
        self._error = ""
        self._mouse_listener = None
        self._keyboard_listener = None

    @property
    def running(self) -> bool:
        listeners = (self._mouse_listener, self._keyboard_listener)
        return any(item is not None and getattr(item, "running", False)
                   for item in listeners)

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._events)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> None:
        if self.running:
            raise MacroError("Macro recording is already running.")
        try:
            from pynput import keyboard, mouse
        except Exception as exc:
            raise MacroError("pynput is unavailable: %s" % exc)
        self._start = self._clock()
        self._stopping = False
        self._error = ""
        try:
            self._mouse_listener = mouse.Listener(
                on_move=self._on_move, on_click=self._on_click,
                on_scroll=self._on_scroll)
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release)
            self._mouse_listener.start()
            self._keyboard_listener.start()
        except Exception as exc:
            self.stop()
            raise MacroError("Could not start recording: %s" % exc)

    def _fail(self, message: str) -> None:
        self._error = str(message)
        self._stopping = True
        for listener in (self._mouse_listener, self._keyboard_listener):
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
        if self._on_error is not None:
            try:
                self._on_error(self._error)
            except Exception:
                pass

    def _append(self, event: dict) -> None:
        with self._lock:
            if self._stopping:
                return
            if len(self._events) >= self._max_events:
                self._fail("Macro reached the %d-step limit." % self._max_events)
                return
            elapsed = self._clock() - self._start
            if elapsed > MAX_DURATION:
                self._fail("Macro exceeded the 24-hour recording limit.")
                return
            event["at"] = round(max(0.0, elapsed), 6)
            self._events.append(event)

    def _on_move(self, x, y) -> None:
        self._append({"type": "mouse_move", "x": int(x), "y": int(y)})

    def _on_click(self, _x, _y, button, pressed) -> None:
        name = getattr(button, "name", "")
        if name not in BUTTONS:
            return
        self._append({"type": "mouse_button", "button": name,
                      "pressed": bool(pressed)})

    def _on_scroll(self, _x, _y, dx, dy) -> None:
        self._append({"type": "mouse_scroll", "dx": int(dx), "dy": int(dy)})

    def _key(self, key) -> str:
        token = normalize_key_token(key)
        return token if token and token.upper() not in self.ignore_keys else ""

    def _on_press(self, key) -> None:
        token = self._key(key)
        if token:
            self._append({"type": "key", "key": token, "pressed": True})

    def _on_release(self, key) -> None:
        token = self._key(key)
        if token:
            self._append({"type": "key", "key": token, "pressed": False})

    def _strip_control_chords(self, events: list[dict]) -> list[dict]:
        """Remove recorded key sequences that are WXB control shortcuts.

        A trigger key such as plain R is still recordable. Only a full chord
        (for example Ctrl+Alt+R), plus the key events in that held sequence, is
        discarded. Mouse/scroll timing is left untouched.
        """
        if not self.control_combos:
            return events
        held: set[str] = set()
        sequence: list[int] = []
        removed: set[int] = set()
        suppressed = False
        for index, event in enumerate(events):
            if event.get("type") != "key":
                continue
            token = str(event.get("key") or "").upper()
            if not token:
                continue
            if event.get("pressed"):
                held.add(token)
                sequence.append(index)
                if suppressed or any(combo <= held
                                     for combo in self.control_combos):
                    suppressed = True
                    removed.update(sequence)
            else:
                held.discard(token)
                if suppressed:
                    removed.add(index)
                if not held:
                    suppressed = False
                    sequence = []
                else:
                    sequence.append(index)
        if not removed:
            return events
        return [event for index, event in enumerate(events)
                if index not in removed]

    def _strip_unmatched_control_keys(self, events: list[dict]) -> list[dict]:
        """Drop control-key releases left when the stop chord consumed the press.

        Recording can begin or end in the middle of the shortcut that started
        or stopped it.  A matched pair is a legitimate recorded control-key
        use and is kept; an orphan press/release made only of control tokens
        cannot be replayed safely and is removed.
        """
        control_tokens = set().union(*self.control_combos) if self.control_combos else set()
        if not control_tokens:
            return events
        pending: dict[str, list[int]] = {}
        removed: set[int] = set()
        for index, event in enumerate(events):
            if event.get("type") != "key":
                continue
            token = str(event.get("key") or "").upper()
            if token not in control_tokens:
                continue
            if event.get("pressed"):
                pending.setdefault(token, []).append(index)
            elif pending.get(token):
                pending[token].pop()
            else:
                removed.add(index)
        for indices in pending.values():
            removed.update(indices)
        if not removed:
            return events
        return [event for index, event in enumerate(events)
                if index not in removed]

    def stop(self) -> list[dict]:
        self._stopping = True
        listeners = (self._mouse_listener, self._keyboard_listener)
        self._mouse_listener = None
        self._keyboard_listener = None
        for listener in listeners:
            try:
                if listener is not None:
                    listener.stop()
            except Exception:
                pass
            try:
                if listener is not None and listener is not threading.current_thread():
                    listener.join(timeout=0.5)
            except Exception:
                pass
        with self._lock:
            clean = self._strip_control_chords(self._events)
            return copy.deepcopy(self._strip_unmatched_control_keys(clean))


class MacroPlayer:
    """Timed macro playback with explicit stop/session/release behavior."""

    def __init__(self, sink: Any, clock: Callable = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 session_guard: Callable[[], bool] | None = None,
                 on_status: Callable[[int, int], None] | None = None) -> None:
        self.sink = sink
        self._clock = clock
        self._sleep = sleep
        self._session_guard = session_guard
        self._on_status = on_status
        self._last_index = -1
        self._last_loop = 0

    def _guard(self) -> None:
        if self._session_guard is not None:
            try:
                locked = bool(self._session_guard())
            except Exception as exc:
                raise MacroInterrupted("Could not check the desktop session: %s"
                                       % exc)
            if locked:
                raise MacroInterrupted("Desktop session locked; macro stopped.")

    def _wait_until(self, target: float, stop: threading.Event) -> None:
        while True:
            self._guard()
            if stop.is_set():
                raise MacroStopped()
            now = self._clock()
            if now >= target:
                if now - target > SLEEP_GAP:
                    raise MacroInterrupted(
                        "The desktop was suspended or the clock jumped; macro stopped.")
                return
            slice_start = now
            self._sleep(min(0.05, target - now))
            after = self._clock()
            if after - slice_start > SLEEP_GAP:
                raise MacroInterrupted(
                    "The desktop was suspended or the clock jumped; macro stopped.")

    def _apply(self, event: Mapping[str, Any]) -> None:
        kind = event["type"]
        try:
            if kind == "mouse_move":
                self.sink.move_pointer(event["x"], event["y"])
            elif kind == "mouse_button":
                if event["pressed"]:
                    self.sink.press_mouse(event["button"])
                else:
                    self.sink.release_mouse(event["button"])
            elif kind == "mouse_scroll":
                self.sink.scroll(event["dx"], event["dy"])
            elif kind == "key":
                if event["pressed"]:
                    self.sink.key_down(event["key"])
                else:
                    self.sink.key_up(event["key"])
            else:  # pragma: no cover - validation prevents this
                raise MacroError("Unknown macro step.")
        except (MacroStopped, MacroInterrupted):
            raise
        except Exception as exc:
            raise MacroError("Macro input injection failed: %s" % exc) from exc

    def play(self, doc: Mapping[str, Any], speed: float = 1.0,
             loops: int = 1, stop: threading.Event | None = None) -> int:
        """Play a validated document; 0 loops means continuous."""
        speed = float(speed)
        if not math.isfinite(speed) or not SPEED_MIN <= speed <= SPEED_MAX:
            raise MacroError("Playback speed must be between %.1f and %.1f."
                             % (SPEED_MIN, SPEED_MAX))
        loops = int(loops)
        if loops < 0 or loops > LOOP_COUNT_MAX:
            raise MacroError("Loop count is out of range.")
        stop = stop or threading.Event()
        events = list(doc.get("events") or [])
        if not events:
            return 0
        completed = 0
        primary = None
        try:
            while loops == 0 or completed < loops:
                loop_start = self._clock()
                for index, event in enumerate(events):
                    self._wait_until(loop_start + event["at"] / speed, stop)
                    self._guard()
                    if stop.is_set():
                        raise MacroStopped()
                    self._apply(event)
                    self._last_index = index
                    if self._on_status is not None:
                        try:
                            self._on_status(index + 1, completed + 1)
                        except Exception:
                            pass
                completed += 1
                self._last_loop = completed
                # A loop can end with a key or button still held. Release at
                # each boundary so a slow loop or a stop cannot leave input
                # stuck down while the next loop waits.
                if loops == 0 or completed < loops:
                    self.sink.release_all()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self.sink.release_all()
            except Exception as release_error:
                if primary is None:
                    raise MacroError("Could not release macro inputs: %s"
                                     % release_error)
                raise MacroError("%s; release also failed: %s"
                                 % (primary, release_error)) from primary
        return completed
