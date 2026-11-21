#!/usr/bin/env python3
"""WXB engine core.

Input engines and small contract helpers only: click bursts, slot rotation,
the SOCD state machine/pipeline and the input sink. The GTK studio imports
this module; it contains no UI toolkit or window code.
"""


import math
import os
import shutil
import random
import selectors
import subprocess
import threading
import time
from collections import deque

# Cordial multi-client support is optional: Sober remains the main path, and
# the app still starts if this module is removed.
try:
    import roblox_runtime as roalt
except Exception:  # pragma: no cover - only when the optional file is missing
    roalt = None


try:
    from pynput import mouse, keyboard
except ImportError:  # pragma: no cover
    raise SystemExit("pynput is required:  pip install pynput")

from wxb_input_lab import (  # noqa: E402
    RemapEngine, code_name, timeline_entry,
)

# =========================================================
# SMALL HELPERS
# =========================================================
def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def hexmix(c1, c2, t):
    """Blend two #rrggbb colours; t=0 -> c1, t=1 -> c2."""
    t = clamp(t, 0.0, 1.0)
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))






def snap_value(v, lo, hi, step):
    """Clamp + step-snap a slider value, returning int when it is integral.

    A non-finite entry ("nan", "inf" — both are accepted by float()) snaps
    to the low end instead of poisoning every later calculation.
    """
    v = float(v)
    v = float(lo) if not math.isfinite(v) else clamp(v, float(lo), float(hi))
    if step:
        v = float(lo) + round((v - float(lo)) / float(step)) * float(step)
        v = clamp(v, float(lo), float(hi))
    return int(v) if float(v).is_integer() else round(v, 4)


def fmt_num(v):
    return str(int(v)) if float(v).is_integer() else ("%.4g" % v)


def parse_kv_lines(text):
    """`KEY = value` lines (the Game tab's editors) -> dict of raw strings.

    Blank lines and # / // comments are ignored, as is anything without an
    equals sign; the first '=' splits, so values may contain '='.
    """
    out = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def parse_flag_value(raw):
    """A typed flag value: bool, int, float, or the raw string."""
    text = str(raw).strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# =========================================================
# KEY HANDLING
# =========================================================
KEY_ALIASES = {"CONTROL": "CTRL", "OPTION": "ALT", "META": "CMD",
               "COMMAND": "CMD", "SUPER": "CMD", "WINDOWS": "CMD",
               "ESCAPE": "ESC", "RETURN": "ENTER"}

NAMED_TOKENS = {
    "space": keyboard.Key.space, "tab": keyboard.Key.tab,
    "enter": keyboard.Key.enter, "escape": keyboard.Key.esc,
    "backspace": keyboard.Key.backspace,
    "shift": keyboard.Key.shift, "ctrl": keyboard.Key.ctrl,
    "alt": keyboard.Key.alt,
}
NAMED_TOKENS.update({("f%d" % i): getattr(keyboard.Key, "f%d" % i)
                     for i in range(1, 13)})


def normalize_key(key):
    """Normalise a pynput key into a stable uppercase token (SHIFT, A, F6 …)."""
    if isinstance(key, keyboard.Key):
        name = key.name.upper()
        if name.endswith(("_L", "_R")):
            name = name[:-2]
        return KEY_ALIASES.get(name, name)
    ch = getattr(key, "char", None)
    if ch:
        return ch.upper()
    return str(key).replace("'", "").upper()


def parse_key_token(token):
    """Turn a mapping string ('1', 'F5', 'space') into something pynput can send."""
    if token is None:
        return None
    t = str(token).strip()
    if not t:
        return None
    low = t.lower()
    if low in NAMED_TOKENS:
        return NAMED_TOKENS[low]
    if len(t) == 1:
        return keyboard.KeyCode.from_char(t.lower())
    return None


def combo_text(combo):
    return "+".join(sorted(combo)) if combo else "NONE"


def slot_key(slot):
    """Hotbar slot number -> key token (slot 10 uses the '0' key)."""
    return "0" if int(slot) == 10 else str(int(slot))


# =========================================================
# INPUT SINK  (real input, or a fake one in tests)
# =========================================================
class CommandSink:
    """Minimal pointer fallback for sessions where pynput cannot inject.

    Wayland compositors frequently reject pynput/XTest injection.  When the
    user has installed ``ydotool`` (preferred on Wayland) or ``xdotool``, a
    click can still be delivered by the external tool.  These tools do not
    expose a portable press/release split, so the whole click happens on
    release; hold time is therefore best-effort here.  Failures are raised so
    the service can show the exact permission/tool error instead of silently
    doing nothing.
    """

    def __init__(self):
        self._tool = shutil.which("ydotool") or shutil.which("xdotool")
        if not self._tool:
            raise RuntimeError(
                "No pointer backend is available. Install pynput, or install "
                "ydotool (Wayland) / xdotool (X11) and run its daemon as your "
                "user; sudo is not required.")
        self._pressed = None
        self.clicks = 0

    def _run(self, *args):
        try:
            result = subprocess.run(
                [self._tool, *args], capture_output=True, text=True,
                timeout=2.0, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("%s could not inject a click: %s"
                               % (os.path.basename(self._tool), exc)) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "%s could not inject a click%s. On Wayland, start the "
                "ydotoold daemon as your user and check /dev/uinput access."
                % (os.path.basename(self._tool),
                   (": " + detail[:180]) if detail else ""))

    def press_mouse(self, button):
        # ydotool/xdotool both offer a complete click; defer until release.
        self._pressed = button if button in ("left", "right", "middle") \
            else "left"

    def release_mouse(self, button=None):
        if self._pressed is None:
            return
        btn = self._pressed
        self._pressed = None
        if self._tool.endswith("ydotool"):
            self._run("click", {"left": "0x40", "middle": "0x20",
                                "right": "0x80"}[btn])
        else:
            self._run("click", {"left": "1", "middle": "2",
                                "right": "3"}[btn])
        self.clicks += 1

    def pointer_position(self):
        if self._tool.endswith("xdotool"):
            try:
                result = subprocess.run(
                    [self._tool, "getmouselocation", "--shell"],
                    capture_output=True, text=True, timeout=1.0, check=False)
                if result.returncode == 0:
                    values = {}
                    for line in result.stdout.splitlines():
                        if "=" in line:
                            key, value = line.split("=", 1)
                            values[key] = value
                    return int(values["X"]), int(values["Y"])
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
        return None

    def move_pointer(self, x, y):
        if self._tool.endswith("xdotool"):
            self._run("mousemove", "--sync", str(int(x)), str(int(y)))
        else:
            self._run("mousemove", "--absolute", str(int(x)), str(int(y)))

    def tap_key(self, token):
        raise RuntimeError(
            "The command backend cannot tap keyboard keys; use the X11 "
            "backend or run the rotation through autoswitcher.py.")

    def release_keys(self):
        pass

    def release_all(self):
        self.release_mouse()


class InputSink:
    """Owns the OS input controllers so the engines stay testable."""

    def __init__(self):
        self._mouse = mouse.Controller()
        self._kbd = keyboard.Controller()
        self._held_keys = set()

    @staticmethod
    def _btn(name):
        buttons = {"left": mouse.Button.left, "right": mouse.Button.right,
                   "middle": mouse.Button.middle}
        if name not in buttons:
            raise ValueError("Unsupported mouse button: %s" % name)
        return buttons[name]

    def press_mouse(self, button):
        self._mouse.press(self._btn(button))

    def release_mouse(self, button=None):
        """Release one button, or all three when no button is named."""
        failures = []
        for name in (("left", "right", "middle") if button is None
                     else (button,)):
            try:
                self._mouse.release(self._btn(name))
            except Exception as exc:
                failures.append("%s button: %s" % (name, exc))
        if failures:
            raise RuntimeError("Could not release the %s. The button may "
                               "still be held; stop the clicker and check "
                               "input permissions." % "; ".join(failures))

    def tap_key(self, token):
        code = parse_key_token(token)
        if code is None:
            return False
        try:
            self._kbd.press(code)
        except Exception as exc:
            raise RuntimeError("Could not press key %s: %s"
                               % (token, exc)) from exc
        self._held_keys.add(code)
        release_error = None
        try:
            time.sleep(0.008)
        finally:
            try:
                self._kbd.release(code)
            except Exception as exc:
                release_error = exc
            self._held_keys.discard(code)
        if release_error is not None:
            raise RuntimeError("Could not release key %s: %s"
                               % (token, release_error)) from release_error
        return True

    def key_down(self, token):
        """Hold a macro key until key_up/release_all; never leave it down."""
        code = parse_key_token(token)
        if code is None:
            raise RuntimeError("Unsupported macro key: %s" % token)
        if code in self._held_keys:
            return
        try:
            self._kbd.press(code)
        except Exception as exc:
            raise RuntimeError("Could not press macro key %s: %s"
                               % (token, exc)) from exc
        self._held_keys.add(code)

    def key_up(self, token):
        code = parse_key_token(token)
        if code is None:
            raise RuntimeError("Unsupported macro key: %s" % token)
        try:
            self._kbd.release(code)
        except Exception as exc:
            raise RuntimeError("Could not release macro key %s: %s"
                               % (token, exc)) from exc
        self._held_keys.discard(code)

    def scroll(self, dx, dy):
        try:
            self._mouse.scroll(int(dx), int(dy))
        except Exception as exc:
            raise RuntimeError("Could not scroll the pointer: %s" % exc) from exc

    def release_keys(self):
        """Release only the keys this sink is holding, never the mouse.

        The rotation and the clicker can run at the same time; a stop must
        undo what that engine did, not what the other one is doing.
        """
        failures = []
        for code in list(self._held_keys):
            try:
                self._kbd.release(code)
            except Exception as exc:
                failures.append("key %s: %s" % (code, exc))
        self._held_keys.clear()
        if failures:
            raise RuntimeError("Could not release held keys: %s"
                               % "; ".join(failures))

    def release_all(self):
        """Everything this sink can hold: keys and mouse buttons (ESC)."""
        failures = []
        for label, cleanup in (("keys", self.release_keys),
                               ("mouse", self.release_mouse)):
            try:
                cleanup()
            except Exception as exc:
                failures.append("%s: %s" % (label, exc))
        if failures:
            raise RuntimeError("; ".join(failures))

    def pointer_position(self):
        """Current pointer position, or None when the backend cannot report it."""
        try:
            x, y = self._mouse.position
            return int(x), int(y)
        except Exception:
            return None

    def move_pointer(self, x, y):
        """Move the pointer, raising a readable error when the backend refuses."""
        try:
            self._mouse.position = (int(x), int(y))
        except Exception as exc:
            raise RuntimeError(
                "The pointer backend cannot move the cursor (%s). On Wayland, "
                "install ydotool and start ydotoold as your user; do not use "
                "sudo for the clicker." % exc) from exc


_sink = None


def sink_default():
    """Lazily created real sink (keeps the module importable headless)."""
    global _sink
    if _sink is None:
        try:
            _sink = InputSink()
        except Exception as exc:
            # A missing X display or a Wayland compositor that refuses XTest
            # should not make the whole app fatal.  CommandSink raises a
            # precise error at start-up if there is no fallback tool either.
            try:
                _sink = CommandSink()
            except Exception as fallback:
                raise RuntimeError("%s %s" % (exc, fallback)) from fallback
    return _sink


def release_sink():
    """Release everything held, without creating a sink that never existed.

    Stop paths used to call sink_default(), which connects pynput even when
    nothing was ever pressed; a failed connection there could mask the stop.
    """
    sk = _sink
    if sk is None:
        return
    sk.release_all()


def screen_bounds():
    """Best-effort primary-monitor size for the corner failsafe.

    Headless test runs and Wayland sessions without an accessible monitor
    simply return None; the failsafe is skipped rather than guessing.
    """
    try:
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        monitor = display.get_monitors().get_item(0) if display else None
        geom = monitor.get_geometry() if monitor is not None else None
        if geom is not None and geom.width > 0 and geom.height > 0:
            return int(geom.width), int(geom.height)
    except Exception:
        pass
    return None


def release_keys():
    """Release held keys only; the clicker's mouse button is not ours."""
    sk = _sink
    if sk is None:
        return
    sk.release_keys()


def release_mouse(button=None):
    """Release the mouse only; a rotation's key tap is not ours."""
    sk = _sink
    if sk is None:
        return
    sk.release_mouse(button)


# =========================================================
# SOCD CLEANER
# =========================================================
# Simultaneous Opposite Cardinal Directions: Left+Right or Up+Down held at
# once. Games disagree on what that should mean, so the cleaner decides per
# axis and sends only the resolved direction. Two halves, deliberately
# separate:
#
#   SocdEngine   pure state machine — takes (source, keycode) presses and
#                returns ("down"/"up", keycode) output. No I/O, no evdev, so
#                every rule is unit-tested without touching a device.
#   SocdPipeline Linux only — exclusive grabs on physical keyboards, one
#                uinput keyboard emitting the cleaned stream. Everything it
#                does to hardware is undone in one finally block.
#
# The output key is the physical key that won, not a canonical one: a game
# bound to WASD still sees W/A/S/D, and one bound to the arrows still sees the
# arrows. WASD and arrows are unified only for the decision.
SOCD_LEFT, SOCD_RIGHT = "LEFT", "RIGHT"
SOCD_UP, SOCD_DOWN = "UP", "DOWN"
SOCD_DIRECTIONS = (SOCD_LEFT, SOCD_RIGHT, SOCD_UP, SOCD_DOWN)
SOCD_AXIS_DIRS = {"horizontal": (SOCD_LEFT, SOCD_RIGHT),
                  "vertical": (SOCD_UP, SOCD_DOWN)}
SOCD_AXIS = {SOCD_LEFT: "horizontal", SOCD_RIGHT: "horizontal",
             SOCD_UP: "vertical", SOCD_DOWN: "vertical"}
SOCD_OPPOSITE = {SOCD_LEFT: SOCD_RIGHT, SOCD_RIGHT: SOCD_LEFT,
                 SOCD_UP: SOCD_DOWN, SOCD_DOWN: SOCD_UP}

# Linux evdev key codes (the same numbers on every keyboard). Keeping them as
# plain ints keeps the engine testable without the evdev package installed.
SOCD_KEY_CODES = {
    30: SOCD_LEFT, 105: SOCD_LEFT,       # A, LeftArrow
    32: SOCD_RIGHT, 106: SOCD_RIGHT,     # D, RightArrow
    17: SOCD_UP, 103: SOCD_UP,           # W, UpArrow
    31: SOCD_DOWN, 108: SOCD_DOWN,       # S, DownArrow
}
SOCD_CODE_NAMES = {30: "A", 105: "Left", 32: "D", 106: "Right",
                   17: "W", 103: "Up", 31: "S", 108: "Down"}
SOCD_NEUTRAL, SOCD_LAST, SOCD_FIRST = "Neutral", "Last win", "First win"
SOCD_PRIORITY = "Priority"
SOCD_MODES = (SOCD_NEUTRAL, SOCD_LAST, SOCD_FIRST, SOCD_PRIORITY)
# fixed priority: Up wins on the vertical axis, Right on the horizontal one
SOCD_PRIORITY_WINNER = {"horizontal": SOCD_RIGHT, "vertical": SOCD_UP}

SOCD_PRESETS = (
    ("Hitbox standard", {"horizontal": SOCD_NEUTRAL,
                         "vertical": SOCD_PRIORITY}),
    ("True neutral", {"horizontal": SOCD_NEUTRAL,
                      "vertical": SOCD_NEUTRAL}),
    ("Last win", {"horizontal": SOCD_LAST, "vertical": SOCD_LAST}),
    ("First win", {"horizontal": SOCD_FIRST, "vertical": SOCD_FIRST}),
)
SOCD_PRESET_MODES = dict(SOCD_PRESETS)


def socd_direction(code):
    """Logical direction for an evdev key code, or None."""
    return SOCD_KEY_CODES.get(int(code))


def socd_preset_modes(name):
    """A fresh modes dict for a preset name (None if there is no such
    preset). Copies, so a caller can never edit the table by accident."""
    modes = SOCD_PRESET_MODES.get(name)
    return dict(modes) if modes else None


class SocdEngine:
    """The whole SOCD decision, with no I/O at all.

    Holds are tracked per (source, keycode): the same logical direction can be
    held by several physical keys and several keyboards at once. Every press
    gets a monotonic sequence number, which is what Last/First compare. Each
    press/release returns the output changes needed, as a list of
    ("down"/"up", keycode) pairs in the order they must be sent.

    The keycode sent is the winning physical key's own code, so the game keeps
    the binding it already had.
    """

    def __init__(self, modes=None, conflicts=0):
        self.modes = {axis: SOCD_NEUTRAL for axis in SOCD_AXIS_DIRS}
        if modes:
            for axis, mode in modes.items():
                if axis in self.modes and mode in SOCD_MODES:
                    self.modes[axis] = mode
        self._seq = 0
        self._held = {}         # (source, code) -> (seq, direction)
        self._sent = {d: None for d in SOCD_DIRECTIONS}
        self._conflict = {axis: False for axis in SOCD_AXIS_DIRS}
        self.conflicts = int(conflicts)

    # -- input -------------------------------------------------------------
    def press(self, source, code):
        """A physical key went down. Returns output ops ( [] when it is not a
        direction, or when the key was already held — autorepeat)."""
        direction = SOCD_KEY_CODES.get(int(code))
        if direction is None:
            return []
        key = (source, int(code))
        if key in self._held:
            return []                       # repeat or duplicate press
        self._seq += 1
        self._held[key] = (self._seq, direction)
        return self._resolve(SOCD_AXIS[direction])

    def release(self, source, code):
        entry = self._held.pop((source, int(code)), None)
        if entry is None:
            return []
        return self._resolve(SOCD_AXIS[entry[1]])

    def forget_source(self, source):
        """A keyboard disappeared: drop everything it held, then re-resolve.

        Releasing the key is not enough to promise nothing sticks — the
        direction that was being sent *for* it must be re-decided too.
        """
        gone = [k for k in self._held if k[0] == source]
        if not gone:
            return []
        axes = sorted({SOCD_AXIS[self._held[k][1]] for k in gone})
        for k in gone:
            del self._held[k]
        ops = []
        for axis in axes:
            ops.extend(self._resolve(axis))
        return ops

    def release_all(self):
        """Release every virtual key this engine is holding (stop/error)."""
        ops = [("up", code) for code in self._sent.values()
               if code is not None]
        self._sent = {d: None for d in SOCD_DIRECTIONS}
        self._held.clear()
        self._conflict = {axis: False for axis in SOCD_AXIS_DIRS}
        return ops

    def output_codes(self):
        """The physical codes currently down on the virtual keyboard.

        The pipeline uses this after rebuilding its uinput device: the new
        device starts with every key up, so anything still held has to be
        pressed again or the release would arrive from nowhere.
        """
        return [code for code in self._sent.values() if code is not None]

    # -- modes -------------------------------------------------------------
    def set_mode(self, axis, mode):
        if axis not in self.modes or mode not in SOCD_MODES:
            return []
        if self.modes[axis] == mode:
            return []
        self.modes[axis] = mode
        return self._resolve(axis)

    def set_modes(self, modes):
        ops = []
        for axis in SOCD_AXIS_DIRS:
            mode = modes.get(axis)
            if mode in SOCD_MODES and mode != self.modes[axis]:
                self.modes[axis] = mode
                ops.extend(self._resolve(axis))
        return ops

    def apply_preset(self, name):
        modes = socd_preset_modes(name)
        return self.set_modes(modes) if modes else []

    # -- state -------------------------------------------------------------
    def _held_in(self, axis):
        held = {d: [] for d in SOCD_AXIS_DIRS[axis]}
        for (src, code), (seq, direction) in self._held.items():
            if SOCD_AXIS[direction] == axis:
                held[direction].append((seq, code, src))
        for entries in held.values():
            entries.sort()
        return held

    def _winner(self, axis, held):
        """(direction, code) that should be down on this axis, or None."""
        a, b = SOCD_AXIS_DIRS[axis]
        ha, hb = held[a], held[b]
        if not ha and not hb:
            return None
        if not ha:
            return (b, hb[0][1])
        if not hb:
            return (a, ha[0][1])
        mode = self.modes[axis]
        if mode == SOCD_NEUTRAL:
            return None                     # opposites cancel while both hold
        if mode == SOCD_LAST:               # newest press wins, key to key
            return ((a, ha[-1][1]) if ha[-1][0] > hb[-1][0]
                    else (b, hb[-1][1]))
        if mode == SOCD_FIRST:              # oldest press wins until released
            return ((a, ha[0][1]) if ha[0][0] < hb[0][0]
                    else (b, hb[0][1]))
        win = SOCD_PRIORITY_WINNER[axis]    # fixed priority, order-independent
        return (win, held[win][0][1])

    def _resolve(self, axis):
        """Emit the difference between what is sent and what should be."""
        held = self._held_in(axis)
        a, b = SOCD_AXIS_DIRS[axis]
        conflict = bool(held[a]) and bool(held[b])
        if conflict and not self._conflict[axis]:
            self.conflicts += 1             # one per conflict episode
        self._conflict[axis] = conflict
        winner = self._winner(axis, held)
        winner_dir, winner_code = winner if winner else (None, None)
        # every release goes before every press: the output never has both
        # directions of an axis down at the same moment, not even for one event
        ups, downs = [], []
        for direction in (a, b):
            want = winner_code if direction == winner_dir else None
            have = self._sent[direction]
            if have == want:
                continue
            if have is not None:
                ups.append(("up", have))
                self._sent[direction] = None
            if want is not None:
                downs.append(("down", want))
                self._sent[direction] = want
        return ups + downs

    def snapshot(self):
        """What the visualizer draws: held, sent, suppressed, counters."""
        phys = {d: False for d in SOCD_DIRECTIONS}
        codes = {d: [] for d in SOCD_DIRECTIONS}
        for _key, (seq, direction) in sorted(self._held.items(),
                                             key=lambda kv: kv[1][0]):
            phys[direction] = True
            codes[direction].append(SOCD_CODE_NAMES.get(
                _key[1], str(_key[1])))
        out = {d: self._sent[d] is not None for d in SOCD_DIRECTIONS}
        return {"phys": phys, "codes": codes, "out": out,
                "suppressed": {d: phys[d] and not out[d]
                               for d in SOCD_DIRECTIONS},
                "conflicts": self.conflicts,
                "conflict": dict(self._conflict),
                "modes": dict(self.modes)}


SOCD_VIRTUAL_NAME = "WXB SOCD virtual keyboard"
EV_SYN, EV_KEY = 0, 1
UINPUT_PATH = "/dev/uinput"
SOCD_UDEV_PATH = "/etc/udev/rules.d/99-uinput.rules"
SOCD_UDEV_RULE = ('KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", '
                  'MODE="0660"')
# every key code a keyboard can send: passthrough needs the virtual device to
# be able to carry anything, not just the four directions
SOCD_UINPUT_KEYS = list(range(1, 768))


def load_evdev():
    """The evdev module, or None (it is an optional dependency)."""
    try:
        import evdev
        return evdev
    except Exception:
        return None


def socd_permission(uinput=UINPUT_PATH, input_dir="/dev/input",
                    access=os.access, exists=os.path.exists,
                    listdir=os.listdir):
    """Can this process grab keyboards and create a virtual one?

    Returns a dict with ok, the two individual answers, the exact commands to
    fix what is missing, and a ready-to-show text. Pure checks, injectable, so
    both the granted and the denied path are testable.
    """
    writes_uinput = bool(exists(uinput) and access(uinput, os.W_OK))
    nodes = []
    try:
        if exists(input_dir):
            nodes = [n for n in listdir(input_dir) if n.startswith("event")]
    except OSError:
        nodes = []
    readable = [n for n in nodes
                if access(os.path.join(input_dir, n), os.R_OK)]
    reads_input = bool(readable)
    ok = reads_input and writes_uinput

    commands = []
    if not reads_input:
        commands.append("sudo usermod -aG input $USER")
    if not writes_uinput:
        commands.append("sudo modprobe uinput")
        commands.append("sudo tee %s >/dev/null <<'EOF'" % SOCD_UDEV_PATH)
        commands.append(SOCD_UDEV_RULE)
        commands.append("EOF")
        commands.append("sudo udevadm control --reload-rules && "
                        "sudo udevadm trigger")
    if not ok:
        text = ("Input access needed — the cleaner cannot grab keyboards or "
                "create its virtual keyboard yet. Run these once:\n  "
                + "\n  ".join(commands)
                + "\nThen log out and back in so the input group applies.")
    else:
        text = ("Input access ready · %d readable input device%s · "
                "/dev/uinput writable." %
                (len(readable), "" if len(readable) == 1 else "s"))
    missing = []
    if not reads_input:
        missing.append("input group")
    if not writes_uinput:
        missing.append("uinput")
    return {"ok": ok, "reads_input": reads_input,
            "writes_uinput": writes_uinput, "devices": len(readable),
            "missing": " + ".join(missing),
            "commands": commands, "text": text}


class SocdPipeline:
    """evdev grab -> SocdEngine -> one uinput keyboard.

    A single reader thread owns every device and the virtual keyboard; new
    keyboards are picked up by a cheap rescan, removed ones are released.
    Every exit path — stop, read error, device unplug, a failed write — runs
    through _finish(), which releases all virtual keys, ungrabs every device
    and closes the ring. A key cannot be left held by this class.
    """

    def __init__(self, engine=None, on_change=None, evdev=None,
                 uinput_factory=None, access_check=socd_permission,
                 rescan=1.0, tick=0.1, remap=None, timeline=False,
                 on_event=None):
        self.engine = engine if engine is not None else SocdEngine()
        self.on_change = on_change
        self._evdev = evdev if evdev is not None else load_evdev()
        self._factory = uinput_factory
        self.access_check = access_check
        self.rescan = float(rescan)
        self.tick = float(tick)
        self._remap = (remap if isinstance(remap, RemapEngine)
                       else (RemapEngine(remap) if remap else None))
        self._timeline_enabled = bool(timeline)
        self._timeline = deque(maxlen=12)
        self._on_event = on_event
        self.running = False
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        self._sel = None
        self._devices = {}         # path -> device
        self._ignored = set()      # paths that cannot send a direction
        self._grabbed = []
        self._out = None
        self._out_path = None
        self._pass_held = {}       # code -> set(device paths)
        self._last_scan = 0.0
        self._io = threading.RLock()
        # A uinput failure is recoverable: the reader rebuilds the virtual
        # keyboard and re-sends whatever is still held. Losing input access
        # for one write must not silently switch the whole cleaner off.
        self._closing = False
        self._out_retry_at = 0.0
        self.output_error = None
        self.stopped = False

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self.running:
            return True, "cleaning"
        if self._evdev is None:
            self.error = ("python3-evdev is not installed — "
                          "sudo apt install python3-evdev")
            return False, self.error
        if self.access_check is not None:
            perm = self.access_check()
            if not perm.get("ok"):
                self.error = perm.get("text") or "input access denied"
                return False, self.error
        self.error = None
        self.output_error = None
        self.stopped = False
        self._closing = False
        self._out_retry_at = 0.0
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run, name="wxb-socd",
                                        daemon=True)
        self._thread.start()
        return True, "cleaning"

    def stop(self):
        self.stopped = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def snapshot(self):
        # the reader thread mutates the engine's holds under this same lock;
        # without it, a snapshot taken from the UI thread mid-event could
        # iterate a changing dict and raise
        with self._io:
            snap = self.engine.snapshot()
            snap.update({"running": self.running,
                         "keyboards": len(self._devices),
                         "error": self.error,
                         "timeline": list(self._timeline),
                         "remap": (self._remap.snapshot()
                                   if self._remap is not None else None)})
        return snap

    def _record(self, kind, code, direction="", output=""):
        if not self._timeline_enabled:
            return
        event = timeline_entry(kind, code, direction, output)
        with self._io:
            self._timeline.append(event)
        if self._on_event is not None:
            try:
                self._on_event(dict(event))
            except Exception:
                pass

    # -- thread ------------------------------------------------------------
    def _run(self):
        try:
            self._open_output()
            self._sel = selectors.DefaultSelector()
            self._scan(force=True)
            while not self._stop.is_set():
                self._select()
                self._scan()
        except Exception as exc:            # permission, ENODEV, write error
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self._finish()

    def _open_output(self):
        self._out_path = None
        factory = self._factory
        if factory is None:
            factory = self._evdev.UInput
        out = factory({EV_KEY: list(SOCD_UINPUT_KEYS)},
                      name=SOCD_VIRTUAL_NAME)
        self._out = out
        try:
            node = getattr(out, "device", None)
            # evdev's InputDevice calls it .path; older versions used .fn
            self._out_path = (getattr(node, "path", None)
                              or getattr(node, "fn", None))
        except Exception:
            self._out_path = None

    def _close_output(self):
        out, self._out = self._out, None
        self._out_path = None
        if out is not None:
            try:
                out.close()
            except Exception:
                pass

    def _ensure_output(self):
        """Recreate the virtual keyboard after a write failed, with backoff."""
        if self._out is not None:
            return True
        if self._closing or self._stop.is_set():
            return False
        now = time.monotonic()
        if now < self._out_retry_at:
            return False
        self._out_retry_at = now + 1.0
        try:
            self._open_output()
        except Exception as exc:
            self.output_error = "%s: %s" % (type(exc).__name__, exc)
            self._out = None
            self._out_path = None
            return False
        self.output_error = None
        self._resync_output()
        return True

    def _resync_output(self):
        """Press again whatever the old device had down.

        The replacement keyboard is brand new, so every key is up on it.
        Without this, a direction held across an output failure would stay
        pressed in the game until its release event, which the new device
        would never send because it never sent the press.
        """
        try:
            codes = list(self.engine.output_codes())
        except Exception:
            codes = []
        for code in codes:
            self._write(self._direction_code(code), 1)
        for code in list(self._pass_held):
            self._write(code, 1)
        if self._remap is not None:
            for code in self._remap.down_codes():
                self._write(code, 1)

    def _is_keyboard(self, dev):
        name = (getattr(dev, "name", "") or "")
        if name.lower().startswith("wxb socd") or name == SOCD_VIRTUAL_NAME:
            return False                    # never grab our own output
        path = getattr(dev, "path", getattr(dev, "fn", None))
        if self._out_path and path == self._out_path:
            return False
        try:
            caps = dev.capabilities()
        except Exception:
            return False
        keys = caps.get(EV_KEY, ())
        return any(int(code) in SOCD_KEY_CODES for code in keys)

    def _scan(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_scan < self.rescan:
            return
        self._last_scan = now
        try:
            # writable=False on purpose: evdev's default only lists nodes that
            # are writable too, and grabbing needs read access alone
            try:
                paths = list(self._evdev.list_devices(writable=False))
            except TypeError:            # older evdev has no such option
                paths = list(self._evdev.list_devices())
        except Exception:
            # A transient listing failure is not an unplug. Treating it as
            # one used to release every keyboard, so a single hiccup made the
            # cleaner look like it had switched itself off until the next
            # rescan. Keep the grabs we already hold and try again.
            return
        # a device that cannot send a direction is not reopened on every
        # scan; a path that disappears is forgotten, so a replug is retried
        for path in list(self._ignored):
            if path not in paths:
                self._ignored.discard(path)
        created = False
        for path in paths:
            if path in self._devices or path in self._ignored:
                continue
            if self._out_path and path == self._out_path:
                continue
            try:
                dev = self._evdev.InputDevice(path)
            except (OSError, PermissionError):
                continue
            if not self._is_keyboard(dev):
                self._ignored.add(path)
                self._close(dev)
                continue
            try:
                dev.grab()
            except Exception:
                self._close(dev)            # in use by another grabber
                continue
            self._devices[path] = dev
            self._grabbed.append(path)
            try:
                self._sel.register(dev.fd, selectors.EVENT_READ, path)
            except Exception:
                self._devices.pop(path, None)
                self._grabbed.remove(path)
                self._close(dev, ungrab=True)
                continue
            created = True
            self._seed_held(path, dev)
        for path in list(self._devices):
            if path not in paths:
                self._drop(path)            # unplugged
        if created:
            self._notify()

    def _select(self):
        if self._sel is None:
            return
        try:
            ready = self._sel.select(timeout=self.tick)
        except (OSError, ValueError):
            # a vanished fd can make select fail until the next rescan drops
            # the device; without this pause that would spin a whole second
            time.sleep(min(self.tick, 0.05))
            return
        for key, _mask in ready:
            path = key.data
            dev = self._devices.get(path)
            if dev is None:
                continue
            self._read_dev(path, dev)

    def _read_dev(self, path, dev):
        for _ in range(64):
            try:
                event = dev.read_one()
            except BlockingIOError:
                return
            except OSError:
                self._drop(path)            # unplugged mid-read
                return
            if event is None:
                return
            self._handle(path, event)

    # -- events ------------------------------------------------------------
    def _handle(self, path, event):
        if getattr(event, "type", None) != EV_KEY:
            return
        value = int(event.value)
        code = int(event.code)
        if value == 2:
            return                          # autorepeat suppressed
        if value not in (0, 1):
            return
        with self._io:
            if code in SOCD_KEY_CODES:
                direction = SOCD_KEY_CODES.get(code) or ""
                ops = (self.engine.press(path, code) if value == 1
                       else self.engine.release(path, code))
                if ops:
                    self._emit(ops)
                output = "suppressed"
                for kind, out_code in ops:
                    if kind == "down":
                        output = (SOCD_KEY_CODES.get(out_code)
                                  or code_name(out_code))
                self._record("down" if value == 1 else "up", code,
                             direction, output)
                return
            if self._remap is not None and self._remap.handles(code):
                ops = (self._remap.press(path, code) if value == 1
                       else self._remap.release(path, code))
                if ops:
                    self._write_ops(ops)
                    self._notify()
                return
            # everything that is not a direction passes through untouched.
            # A release for a key we never forwarded (held before the cleaner
            # started, or a duplicate release) must leave no empty entry
            # behind: _finish would otherwise send a stray up for it.
            if value == 1:
                heads = self._pass_held.setdefault(code, set())
                if path in heads:
                    return                  # duplicate press
                heads.add(path)
                self._write(code, 1)
            else:
                heads = self._pass_held.get(code)
                if not heads or path not in heads:
                    return
                heads.discard(path)
                if not heads:
                    self._pass_held.pop(code, None)
                    self._write(code, 0)

    def _seed_held(self, path, dev):
        """Carry the keys that were already down when the grab began.

        Their presses happened before the grab, so the cleaner never saw
        them. Unless it presses them on the virtual keyboard, their releases
        will be the first event it sees, and a game or the desktop keeps the
        key held forever (the F8 that starts the cleaner was the worst case:
        X never saw its release and kept repeating it).
        """
        try:
            active = [int(code) for code in (dev.active_keys() or ())]
        except Exception:
            return
        for code in active:
            if code in SOCD_KEY_CODES:
                ops = self.engine.press(path, code)
                if ops:
                    self._write_ops(ops, direction=True)
            elif self._remap is not None and self._remap.handles(code):
                self._write_ops(self._remap.press(path, code))
            else:
                heads = self._pass_held.setdefault(code, set())
                if path in heads:
                    continue
                heads.add(path)
                self._write(code, 1)

    def _write(self, code, value):
        """Send one key event, rebuilding the virtual keyboard if it failed.

        A closed or removed uinput node used to raise out of the reader
        thread and finish the whole pipeline — the cleaner switched off for
        no reason the user could see. Now the event is retried once on a
        fresh device and the reader keeps running.
        """
        for _attempt in (0, 1):
            if self._out is None and not self._ensure_output():
                return False
            try:
                self._out.write(EV_KEY, int(code), 1 if value else 0)
                self._out.syn()
                return True
            except Exception as exc:
                self.output_error = "%s: %s" % (type(exc).__name__, exc)
                self._close_output()
        return False

    def _emit(self, ops):
        self._write_ops(ops, direction=True)
        self._notify()

    def _drop(self, path):
        dev = self._devices.pop(path, None)
        self._grabbed = [p for p in self._grabbed if p != path]
        if dev is None:
            return
        if self._sel is not None:
            try:
                self._sel.unregister(dev.fd)
            except Exception:
                pass
        self._close(dev, ungrab=True)
        with self._io:
            for code in list(self._pass_held):
                heads = self._pass_held[code]
                heads.discard(path)
                if not heads:
                    self._pass_held.pop(code, None)
                    self._write(code, 0)
            ops = self.engine.forget_source(path)
            if ops:
                self._write_ops(ops, direction=True)
            if self._remap is not None:
                remap_ops = self._remap.forget_source(path)
                if remap_ops:
                    self._write_ops(remap_ops)
        self._notify()

    def _direction_code(self, code):
        if self._remap is None:
            return code
        return self._remap.direction_output(code) or code

    def _write_ops(self, ops, direction=False):
        for kind, code in ops:
            if direction:
                code = self._direction_code(code)
            self._write(code, 1 if kind == "down" else 0)

    # -- modes while running ----------------------------------------------
    def set_mode(self, axis, mode):
        with self._io:
            ops = self.engine.set_mode(axis, mode)
            if ops:
                self._write_ops(ops, direction=True)
        self._notify()

    def set_modes(self, modes):
        with self._io:
            ops = self.engine.set_modes(modes)
            if ops:
                self._write_ops(ops, direction=True)
        self._notify()

    def set_timeline(self, enabled):
        """Turn the bounded direction timeline on/off while running."""
        with self._io:
            self._timeline_enabled = bool(enabled)
            if not self._timeline_enabled:
                self._timeline.clear()
        self._notify()

    # -- teardown ----------------------------------------------------------
    def _close(self, dev, ungrab=False):
        if ungrab:
            try:
                dev.ungrab()
            except Exception:
                pass
        try:
            dev.close()
        except Exception:
            pass

    def _finish(self):
        """Release every key, let go of every keyboard, close the ring.

        Runs on the reader thread and is safe to call after a failed start: it
        never assumes anything exists.
        """
        self._closing = True
        try:
            with self._io:
                if self._out is not None:
                    self._release_all_inputs()
        except Exception:
            pass
        for path in list(self._devices):
            dev = self._devices.pop(path)
            if self._sel is not None:
                try:
                    self._sel.unregister(dev.fd)
                except Exception:
                    pass
            self._close(dev, ungrab=True)
        self._grabbed = []
        if self._sel is not None:
            try:
                self._sel.close()
            except Exception:
                pass
            self._sel = None
        self._close_output()
        self.running = False
        self._thread = None
        self._notify()

    def release_now(self):
        """Release virtual keys without closing the capture ring.

        Safe Reset calls this before stopping the pipeline so a held direction
        or a remapped target gets an up event even if the reader is between
        events.  The pipeline may keep running; a later release is ignored by
        the engine/remap state that was cleared here.
        """
        with self._io:
            self._release_all_inputs()
        self._notify()

    def _release_all_inputs(self):
        for _kind, code in self.engine.release_all():
            self._write(self._direction_code(code), 0)
        for code in list(self._pass_held):
            self._write(code, 0)
        self._pass_held.clear()
        if self._remap is not None:
            for _kind, code in self._remap.release_all():
                self._write(code, 0)

    def _notify(self):
        if self.on_change is None:
            return
        try:
            self.on_change(self.snapshot())
        except Exception:
            pass


# =========================================================
# ENGINES  (low CPU: event-aware sleeps, monotonic scheduling)
# =========================================================
def sleep_stop(stop, seconds):
    """Sleep up to `seconds`, waking early if `stop` is set. False = stopped.

    time.sleep is the cheapest wait this app has: measured on this machine it
    costs 17-23 us of CPU per call at 0.5-1 ms against 21-29 us for
    select.select and 28-40 us for Event.wait, and it keeps that edge on long
    waits too (56 us vs 73 us vs 114 us at 25 ms). So long waits are polled in
    10 ms slices once every 10 ms rather than handed to an event, which also
    keeps the abort granularity at one slice.
    """
    if seconds <= 0:
        return not stop.is_set()
    if seconds <= 0.004:                      # fast path: no slicing overhead
        time.sleep(seconds)
        return not stop.is_set()
    end = time.monotonic() + seconds
    while True:
        if stop.is_set():
            return False
        rem = end - time.monotonic()
        if rem <= 0:
            return True
        time.sleep(0.01 if rem > 0.01 else rem)


def _sleep_until(stop, deadline, precision="balanced"):
    """Wait until a monotonic deadline using the requested timing precision."""
    precision = str(precision or "balanced").strip().lower()
    if precision not in ("efficient", "balanced", "precise"):
        precision = "balanced"
    while True:
        if stop.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if precision == "precise" and remaining <= 0.0015:
            # The final millisecond belongs to the scheduler, not to a
            # sleep slice that can overshoot the click deadline.
            time.sleep(0)
            continue
        slice_seconds = 0.02 if precision == "efficient" else 0.008
        if precision == "precise":
            slice_seconds = min(slice_seconds, max(0.0005, remaining - 0.001))
        if not sleep_stop(stop, min(remaining, slice_seconds)):
            return False


def _report_click_error(callback, message):
    if callback is None:
        return
    try:
        callback(str(message))
    except Exception:
        pass


def _failsafe_hit(position, bounds, corner, margin=2):
    if corner == "off" or not position or not bounds:
        return False
    try:
        x, y = int(position[0]), int(position[1])
        width, height = int(bounds[0]), int(bounds[1])
    except (TypeError, ValueError, IndexError):
        return False
    if width <= 0 or height <= 0:
        return False
    left = x <= margin
    right = x >= width - 1 - margin
    top = y <= margin
    bottom = y >= height - 1 - margin
    return {"top-left": left and top, "top-right": right and top,
            "bottom-left": left and bottom,
            "bottom-right": right and bottom}.get(corner, False)


def click_burst(cps, cdc, button, stop, duration_ms=None, clicks=None, sink=None,
                *, cps_max=None, start_delay_ms=0, on_error=None, on_pause=None,
                pointer_mode="follow", fixed_position=None, jitter=0, bounds=None,
                failsafe_corner="off", failsafe_margin=2, session_guard=None,
                timing_precision="balanced", click_guard=None):
    """Press/release `button` at `cps` with `cdc` duty (% held).

    Runs until `stop` is set, for `duration_ms`, or for `clicks` clicks.
    Returns the number of completed clicks. Never busy-waits.
    """
    try:
        sk = sink if sink is not None else sink_default()
    except Exception as exc:
        _report_click_error(on_error, "Could not open a pointer backend: %s" % exc)
        return 0
    if sk is None:                      # defensive: never crash the worker
        _report_click_error(on_error, "No pointer backend is available.")
        return 0
    low_cps = clamp(cps, 0.01, 1000.0)
    high_cps = (clamp(cps_max, 0.01, 1000.0)
                if cps_max is not None else low_cps)
    if high_cps < low_cps:
        low_cps, high_cps = high_cps, low_cps
    precision = str(timing_precision or "balanced").strip().lower()
    if precision not in ("efficient", "balanced", "precise"):
        precision = "balanced"
    try:
        delay_seconds = max(0.0, float(start_delay_ms or 0) / 1000.0)
    except (TypeError, ValueError):
        delay_seconds = 0.0
    if delay_seconds and not _sleep_until(
            stop, time.monotonic() + delay_seconds, precision):
        return 0
    started_at = time.monotonic()
    deadline = (None if duration_ms is None
                else started_at + duration_ms / 1000.0)
    # CLOCK_MONOTONIC stops during suspend, so use the wall clock only to
    # notice a long resume/sleep hole.  A normal low-CPS wait is allowed for
    # four periods before this is treated as a resume rather than a wait.
    last_wall = time.time()
    first_period = 1.0 / (random.uniform(low_cps, high_cps)
                          if high_cps > low_cps else low_cps)
    sleep_gap = max(10.0, first_period * 4.0)
    mode = str(pointer_mode or "follow").strip().lower()
    if not callable(getattr(sk, "pointer_position", None)):
        # Test sinks and extremely old custom sinks cannot report a position;
        # do not let the optional failsafe break a working backend.
        failsafe_corner = "off"
        if mode in ("fixed", "jitter"):
            mode = "follow"
    elif failsafe_corner != "off" and not bounds:
        _report_click_error(
            on_error, "Failsafe is unavailable because the monitor bounds "
            "could not be read; clicking continues without it.")
        failsafe_corner = "off"
    try:
        jitter_px = max(0, int(jitter or 0))
    except (TypeError, ValueError):
        jitter_px = 0
    paused = False

    def set_paused(value):
        nonlocal paused
        if paused == value:
            return
        paused = value
        if on_pause is not None:
            try:
                on_pause(value)
            except Exception:
                pass

    try:
        failsafe_margin = max(1, int(failsafe_margin))
    except (TypeError, ValueError):
        failsafe_margin = 2
    done = 0
    next_due = started_at
    pressed = False
    try:
        while not stop.is_set():
            if clicks is not None and done >= clicks:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            now_wall = time.time()
            if now_wall - last_wall > sleep_gap:
                _report_click_error(
                    on_error, "Clicker stopped after a long system sleep or "
                    "clock jump; press Start when you are ready.")
                break
            last_wall = now_wall
            if session_guard is not None:
                try:
                    locked = bool(session_guard())
                except Exception:
                    locked = False
                if locked:
                    set_paused(True)
                    if not sleep_stop(stop, 0.2):
                        break
                    continue
                set_paused(False)
            period = 1.0 / (random.uniform(low_cps, high_cps)
                            if high_cps > low_cps else low_cps)
            down = period * clamp(cdc / 100.0, 0.0001, 0.9999)
            if click_guard is not None:
                try:
                    allowed = bool(click_guard())
                except Exception as exc:
                    _report_click_error(
                        on_error, "The app filter could not check the "
                        "foreground window: %s" % exc)
                    break
                if not allowed:
                    next_due = max(next_due, time.monotonic())
                    if not _sleep_until(
                            stop, time.monotonic() + min(max(period, 0.05),
                                                         0.25), precision):
                        break
                    continue
            if failsafe_corner != "off":
                pos = None
                try:
                    pos = sk.pointer_position()
                except Exception as exc:
                    _report_click_error(
                        on_error, "The pointer backend stopped reporting its "
                        "position: %s" % exc)
                    break
                if pos is None:
                    _report_click_error(
                        on_error, "Failsafe is unavailable because this pointer "
                        "backend does not expose a global cursor position; "
                        "clicking continues without it.")
                    failsafe_corner = "off"
                    continue
                if _failsafe_hit(pos, bounds, failsafe_corner,
                                 margin=failsafe_margin):
                    _report_click_error(
                        on_error, "Failsafe corner triggered; clicking stopped.")
                    break
            if mode in ("fixed", "jitter"):
                base = fixed_position
                if base is None:
                    try:
                        base = sk.pointer_position()
                    except Exception as exc:
                        _report_click_error(
                            on_error, "Could not read the pointer position: %s" % exc)
                        break
                if base is None:
                    _report_click_error(
                        on_error, "The pointer backend cannot follow a fixed point.")
                    break
                x, y = int(base[0]), int(base[1])
                if mode == "jitter" and jitter_px:
                    x += random.randint(-jitter_px, jitter_px)
                    y += random.randint(-jitter_px, jitter_px)
                try:
                    sk.move_pointer(x, y)
                except Exception as exc:
                    _report_click_error(
                        on_error, "Could not move the pointer: %s" % exc)
                    break
            try:
                sk.press_mouse(button)
                pressed = True
            except Exception as exc:
                _report_click_error(
                    on_error, "The pointer backend could not press the mouse "
                    "button: %s" % exc)
                break                       # sink died: stop, never spin
            held = False
            release_failed = False
            try:
                held = sleep_stop(stop, down)
            finally:
                # a stop, a sink error or a keyboard interrupt between the
                # press and this line must still let the button go
                if pressed:
                    try:
                        sk.release_mouse(button)
                    except Exception as exc:
                        release_failed = True
                        _report_click_error(
                            on_error, "The pointer backend could not release "
                            "the mouse button: %s" % exc)
                    if not release_failed:
                        pressed = False
            if release_failed:
                break
            if not held:
                break
            done += 1
            # Fell behind: resync instead of firing a catch-up burst, so a
            # slow interval cannot cause a clicking spike.
            next_due = max(next_due + period, time.monotonic())
            if not _sleep_until(stop, next_due, precision):
                break
    finally:
        set_paused(False)
        if pressed:                     # only ever release what we pressed
            try:
                sk.release_mouse(button)
            except Exception as exc:
                _report_click_error(
                    on_error, "The mouse button could not be released: %s" % exc)
    return done


def build_rotation(slot_a, slot_b, delay_a_ms, delay_b_ms, mode, max_rot):
    """Validate raw user values for the rotation macro.

    Returns (cfg, None) on success or (None, "reason") so the UI can show
    exactly what is wrong. Slots must be ints 0-10, delays numbers >= 0,
    max rotations an int >= 0 (0 = infinite).

    The `_ms` arguments are what the sliders show; the returned cfg uses the
    same shape as autoswitcher's: delays in **seconds**, mode lower-case.
    Both rotation engines share that contract, so a cfg can be read the same
    way whichever one runs it.
    """
    try:
        sa = int(str(slot_a).strip())
        sb = int(str(slot_b).strip())
    except (TypeError, ValueError):
        return None, "slots must be whole numbers"
    if not 0 <= sa <= 10:
        return None, "slot A must be 0-10"
    if not 0 <= sb <= 10:
        return None, "slot B must be 0-10"
    try:
        da = float(delay_a_ms)
        db = float(delay_b_ms)
    except (TypeError, ValueError):
        return None, "delays must be numbers"
    if not (math.isfinite(da) and math.isfinite(db)):
        return None, "delays must be finite numbers"
    if da < 0 or db < 0:
        return None, "delays must be >= 0"
    if str(mode).lower() not in ("toggle", "hold"):
        return None, "mode must be toggle or hold"
    try:
        mr = int(str(max_rot).strip())
    except (TypeError, ValueError):
        return None, "max rotations must be a whole number"
    if mr < 0:
        return None, "max rotations must be >= 0"
    return {"slot_a": sa, "slot_b": sb, "key_a": slot_key(sa),
            "key_b": slot_key(sb), "delay_a": da / 1000.0,
            "delay_b": db / 1000.0,
            "mode": "hold" if str(mode).lower() == "hold" else "toggle",
            "max_rot": mr}, None


def run_rotation(cfg, stop, sink=None):
    """The rotation switcher, repeated until `stop` or MAX_ROT rotations:

        tap key(SLOT_A) -> wait DELAY_A -> tap key(SLOT_B) -> wait DELAY_B

    Shared contract with autoswitcher.loop: delays are in seconds; a
    rotation counts only after its closing delay; a stop aborts during the
    current wait, before the next tap; presses and releases always balance.

    Only key taps are sent, so nothing ends up held and there is no frozen
    "one click" state to recover from; the closing release_keys() is just a
    safety net and deliberately never touches the mouse, which the clicker
    may be holding at the same time. A tap that fails (a dead controller or a
    backend that cannot tap) raises a visible error instead of spinning on a
    keyboard that does nothing.
    """
    sk = sink if sink is not None else sink_default()
    if sk is None:
        return 0
    key_a, key_b = cfg["key_a"], cfg["key_b"]
    delay_a, delay_b = cfg["delay_a"], cfg["delay_b"]
    done = 0
    try:
        while not stop.is_set():
            if not sk.tap_key(key_a):
                raise RuntimeError("Could not tap rotation key %s." % key_a)
            if stop.wait(delay_a):
                break
            if not sk.tap_key(key_b):
                raise RuntimeError("Could not tap rotation key %s." % key_b)
            if stop.wait(delay_b):
                break
            done += 1
            if cfg["max_rot"] and done >= cfg["max_rot"]:
                break
    finally:
        # a tap that raises mid-key must still leave nothing held; a release
        # failure is allowed to surface so the service can show it
        sk.release_keys()
    return done


class HotkeyWatcher:
    """Edge-triggered hotkey matcher: fires once per press, re-arms on release."""

    def __init__(self, combo=None):
        self.combo = frozenset(combo or ())
        self._armed = False

    def set_combo(self, combo):
        self.combo = frozenset(combo or ())
        self._armed = False

    def reset(self):
        self._armed = False

    def update(self, pressed):
        now = bool(self.combo) and self.combo.issubset(pressed)
        fire = now and not self._armed
        self._armed = now
        return fire


# =========================================================
# COLOUR MATH  (also what the palette audit checks)
# =========================================================










# =========================================================
# ICON GLYPHS  (drawn vectors — no image assets)
# =========================================================
CURSOR_STYLES = ("Cross", "Dot", "Heart", "Paw", "Spark", "Arrow")
CURSOR_COLORS = {
    "Pink": "#ff8fbd",
    "White": "#fffdfd",
    "Black": "#1b1b1b",
    "Mint": "#8fe8cf",
    "Lavender": "#c7b3ff",
}
