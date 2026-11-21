# CONFIG — lines 1-8, edit these values only; there is no logic in this block.
SLOT_A = 1           # int 0-10  · presses key "1"  (slot 10 presses the "0" key)
SLOT_B = 4           # int 0-10  · presses key "4"
DELAY_A_MS = 1150.0  # float >= 0 · pause after key A, in milliseconds
DELAY_B_MS = 100.0   # float >= 0 · pause after key B, in milliseconds
HOTKEY = "f6"        # any key or combo: "z" "1" "f6" "ctrl+shift+f8" "alt+1"
MODE = "toggle"      # "toggle" press on / press off · "hold" run while held
MAX_ROT = 0          # 0 = infinite loop · else stop after this many rotations

# ---------------------------------------------------------------------------
# autoswitcher — minimal hotbar rotation switcher (Linux Mint / X11)
#
#   run:      sudo python3 autoswitcher.py
#   why sudo: the 'keyboard' library reads /dev/input to catch the global
#             hotkey. Once-only alternative, then log out and back in:
#                 sudo usermod -aG input $USER
#   install:  pip install keyboard pynput
#   stop it:  press HOTKEY again (toggle) · release it (hold) · Ctrl+C here
#
# Rotation: key(SLOT_A) -> DELAY_A_MS -> key(SLOT_B) -> DELAY_B_MS -> repeat.
# Only key taps are sent; the closing release of both keys is a safety net,
# not normal operation. Delays wait on an event (no polling, no busy CPU).
#
# WXB's Macro tab runs the same contract in-process (wxb.py: run_rotation):
# cfg delays in seconds, mode lower-case, a rotation counts after its closing
# delay, and a stop aborts inside the current delay before the next tap.
# This file is still standalone on purpose - it is not a plugin loader.
# ---------------------------------------------------------------------------

import math
import threading

try:
    import keyboard                      # global hotkey capture
except ImportError:
    raise SystemExit("autoswitcher needs 'keyboard':  pip install keyboard")

from pynput.keyboard import Controller   # input simulation

_keys = Controller()

# One run at a time, each with its own stop event. The event is never reused
# and a run that ends by itself never sets it: a rotation that hits MAX_ROT
# must not be able to cancel the rotation started right after it.
_stop = threading.Event()                # the live run's own event
_lock = threading.Lock()                 # guards _stop / _running / _thread
_running = False
_thread = None


def slot_key(slot):
    """Hotbar slot number -> key to press (slot 10 uses the '0' key)."""
    return "0" if int(slot) == 10 else str(int(slot))


def validate():
    """Check the CONFIG block, returning the resolved settings.

    SLOT_A / SLOT_B must be ints 0-10, DELAY_A_MS / DELAY_B_MS must be
    numbers >= 0, MODE must be "toggle" or "hold" in any case, MAX_ROT must
    be an int >= 0.  Anything else raises SystemExit with the reason.

    The resolved cfg is the same shape WXB's build_rotation() returns:
    delays in seconds, mode lower-case.
    """
    for name, value in (("SLOT_A", SLOT_A), ("SLOT_B", SLOT_B)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SystemExit("CONFIG error: %s must be an int 0-10 (got %r)"
                             % (name, value))
        if not 0 <= value <= 10:
            raise SystemExit("CONFIG error: %s must be 0-10 (got %r)"
                             % (name, value))
    for name, value in (("DELAY_A_MS", DELAY_A_MS), ("DELAY_B_MS", DELAY_B_MS)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SystemExit("CONFIG error: %s must be a number >= 0 (got %r)"
                             % (name, value))
        # nan/inf pass a bare >= 0 test but would kill the wait inside loop()
        if not math.isfinite(float(value)):
            raise SystemExit("CONFIG error: %s must be a finite number "
                             "(got %r)" % (name, value))
        if float(value) < 0:
            raise SystemExit("CONFIG error: %s must be >= 0 (got %r)"
                             % (name, value))
    # WXB accepts "Toggle"/"Hold"; both files agree on lower-case in the cfg
    mode = str(MODE).strip().lower()
    if mode not in ("toggle", "hold"):
        raise SystemExit("CONFIG error: MODE must be 'toggle' or 'hold' "
                         "(got %r)" % (MODE,))
    if isinstance(MAX_ROT, bool) or not isinstance(MAX_ROT, int) or MAX_ROT < 0:
        raise SystemExit("CONFIG error: MAX_ROT must be an int >= 0 (got %r)"
                         % (MAX_ROT,))
    if not isinstance(HOTKEY, str) or not HOTKEY.strip():
        raise SystemExit("CONFIG error: HOTKEY must be a key name or combo "
                         "(got %r)" % (HOTKEY,))
    return {"key_a": slot_key(SLOT_A), "key_b": slot_key(SLOT_B),
            "delay_a": float(DELAY_A_MS) / 1000.0,
            "delay_b": float(DELAY_B_MS) / 1000.0,
            "max_rot": MAX_ROT, "mode": mode, "hotkey": HOTKEY}


def _release(key):
    """Let one key go, swallowing a dead controller."""
    try:
        _keys.release(key)
        return True
    except Exception:
        return False


def _tap(key, held):
    """Press and release one key; False when the controller refused.

    `held` tracks the keys that are actually down, so the run's closing
    cleanup releases exactly those and never fires a blind release.
    """
    try:
        _keys.press(key)
        held.add(key)
        _keys.release(key)
        held.discard(key)
        return True
    except Exception as exc:
        print("[autoswitcher] key %s failed: %s" % (key, exc))
        return False


def _finish(stop):
    """Clear the live-run state, unless a newer run already owns it."""
    global _running, _thread
    with _lock:
        if stop is not _stop:            # a newer run took over meanwhile
            return
        _running = False
        _thread = None


def loop(cfg, stop=None):
    """The rotation itself, until `stop` is set or MAX_ROT is reached.

    Shared contract with wxb.run_rotation: delays are in seconds, a rotation
    counts after its closing delay, a stop aborts inside the current delay
    before the next tap, and presses/releases always balance.

    `stop` belongs to this run. This function never sets it, so a finite run
    cannot put out the event of the run that replaced it.
    """
    stop = _stop if stop is None else stop
    key_a, key_b = cfg["key_a"], cfg["key_b"]
    delay_a, delay_b = cfg["delay_a"], cfg["delay_b"]
    held = set()
    done = 0
    try:
        while not stop.is_set():
            if not _tap(key_a, held):
                break
            if stop.wait(delay_a):      # event wait: instant stop, zero CPU
                break
            if not _tap(key_b, held):
                break
            if stop.wait(delay_b):
                break
            done += 1
            if cfg["max_rot"] and done >= cfg["max_rot"]:
                break
    finally:
        # only a key this run actually pressed can still be down
        for key in list(held):
            _release(key)
        _finish(stop)
    print("[autoswitcher] stopped after %d rotation(s)" % done)
    return done


def start(*_):
    """Start the rotation. Idempotent; safe to call from the hotkey thread."""
    global _running, _thread, _stop
    with _lock:
        if _running:
            return False
        stale, stale_stop = _thread, _stop
    if stale is not None and stale.is_alive():
        # a run that was stopped a moment ago is still leaving: never stack a
        # second rotation on top of it
        stale_stop.set()
        stale.join(timeout=0.5)
        if stale.is_alive():
            print("[autoswitcher] the previous rotation is still finishing - "
                  "not starting another")
            return False
    try:
        cfg = validate()
    except SystemExit as exc:
        # start() can be called straight from the hotkey thread; a bad CONFIG
        # must be reported there, not kill the callback that reads the keys
        print("[autoswitcher] %s" % exc)
        return False
    event = threading.Event()
    thread = threading.Thread(target=loop, args=(cfg, event), daemon=True,
                              name="autoswitcher-rotation")
    with _lock:
        if _running:                    # two starts raced: the first wins
            return False
        _stop = event
        _running = True
        _thread = thread
    thread.start()
    print("[autoswitcher] rotation started  (%s -> %dms -> %s -> %dms)"
          % (cfg["key_a"], round(cfg["delay_a"] * 1000),
             cfg["key_b"], round(cfg["delay_b"] * 1000)))
    return True


def stop(*_):
    """Stop the live rotation and wait for its loop to leave (idempotent)."""
    global _running, _thread
    with _lock:
        was = _running
        event, thread = _stop, _thread
        _running = False
    if was:
        event.set()
    if thread is not None and thread.is_alive() \
            and thread is not threading.current_thread():
        thread.join(timeout=0.5)
    with _lock:
        # only forget a thread that is really gone: start() joins a lingering
        # one, and refuses to stack another rotation while it is still alive
        if thread is None or not thread.is_alive():
            _thread = None
    if was:
        print("[autoswitcher] rotation stopped")
    return was


def toggle(*_):
    """Toggle mode: one hotkey press starts the rotation, the next stops it."""
    if _running:
        stop()
    else:
        start()


def bind():
    """Attach HOTKEY — read here at bind time, so any key or combo works.

    Toggle: the hotkey press flip-flops start/stop.
    Hold:   the hotkey press starts, and its release stops — regardless of
            which key of the combo is released last.
    """
    # MODE is validated case-insensitively, so bind must agree with validate
    if str(MODE).strip().lower() == "hold":
        keyboard.add_hotkey(HOTKEY, start)
        keyboard.add_hotkey(HOTKEY, stop, trigger_on_release=True)
    else:
        keyboard.add_hotkey(HOTKEY, toggle)
    return HOTKEY


if __name__ == "__main__":
    # Defaults come straight from the CONFIG block at the top of this file:
    #   SLOT_A = 1, SLOT_B = 4, DELAY_A_MS = 1150.0, DELAY_B_MS = 100.0,
    #   HOTKEY = "f6", MODE = "toggle", MAX_ROT = 0
    cfg = validate()
    print("autoswitcher  |  key %s -> %dms -> key %s -> %dms  |  %s"
          % (cfg["key_a"], round(cfg["delay_a"] * 1000), cfg["key_b"],
             round(cfg["delay_b"] * 1000),
             "infinite rotations" if not cfg["max_rot"]
             else "%d rotation(s)" % cfg["max_rot"]))
    print("mode=%s  hotkey=%s  (Ctrl+C to quit)" % (cfg["mode"], cfg["hotkey"]))
    try:
        bind()
    except ValueError as exc:
        raise SystemExit("CONFIG error: HOTKEY=%r is not something 'keyboard' "
                         "understands (%s)\n"
                         "Examples: \"f6\", \"z\", \"1\", \"ctrl+shift+f8\", "
                         "\"alt+1\"" % (HOTKEY, exc))
    except Exception as exc:
        raise SystemExit(
            "could not grab the global hotkey (%s)\n"
            "On Linux 'keyboard' reads /dev/input, so it needs root:\n"
            "    sudo python3 autoswitcher.py\n"
            "or add yourself to the input group once, then log out and in:\n"
            "    sudo usermod -aG input $USER" % exc)
    try:
        keyboard.wait()                       # block until Ctrl+C
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        print("autoswitcher: bye")
