#!/usr/bin/env python3
"""
autoswitcher test suite — run with:  python3 test_autoswitcher.py

Nothing real is ever touched: pynput's Controller is replaced by a recorder
and the 'keyboard' library by a stub, so no key is injected and no global
hook is installed. The CONFIG block is checked against the file itself.
"""

import sys
import threading
import time

import wxb_test_isolation

try:
    import autoswitcher as asw
except SystemExit as exc:                     # 'keyboard' not installed
    print("SKIP autoswitcher tests: %s" % exc)
    sys.exit(0)
except Exception as exc:                      # no /dev/input access, etc.
    print("SKIP autoswitcher tests: %s: %s" % (type(exc).__name__, exc))
    sys.exit(0)

print(wxb_test_isolation.assert_isolated("autoswitcher"))

PASSES = []
FAILS = []
CONFIG_NAMES = ("SLOT_A", "SLOT_B", "DELAY_A_MS", "DELAY_B_MS", "HOTKEY",
                "MODE", "MAX_ROT")


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, extra))


class FakeKeys:
    """Stands in for pynput's Controller; records presses and releases."""

    def __init__(self):
        self.log = []
        self.timed = []                       # monotonic time of each press
        self.held = set()                     # keys currently down

    def press(self, key):
        self.log.append("press:" + str(key))
        self.timed.append(time.monotonic())
        self.held.add(str(key))

    def release(self, key):
        self.log.append("release:" + str(key))
        self.held.discard(str(key))


class FakeKeyboard:
    """Stands in for the keyboard module; records hotkey registrations."""

    def __init__(self):
        self.hotkeys = []

    def add_hotkey(self, hotkey, callback, **kw):
        self.hotkeys.append((hotkey, callback,
                             bool(kw.get("trigger_on_release", False))))
        return lambda: None

    def wait(self, *_a, **_kw):
        return None


def with_config(**kw):
    """Run asw.validate() with the CONFIG globals temporarily replaced."""
    olds = {k: getattr(asw, k) for k in kw}
    for k, v in kw.items():
        setattr(asw, k, v)
    try:
        return asw.validate()
    finally:
        for k, v in olds.items():
            setattr(asw, k, v)


def config_error(**kw):
    """Return the error message for a bad CONFIG, or None if it was accepted."""
    try:
        with_config(**kw)
    except SystemExit as exc:
        return str(exc)
    return None


def test_config_block():
    print("\n[config block]")
    with open(asw.__file__) as fh:
        lines = fh.read().splitlines()
    head = lines[:8]
    check("the config block is exactly lines 1-8", len(head) == 8, head)
    for name in CONFIG_NAMES:
        check("%s is declared in lines 1-8" % name,
              any(line.split("=")[0].strip() == name for line in head), head)
    check("lines 1-8 hold no logic",
          not any(line.lstrip().startswith(
              ("import", "from", "def", "class", "if", "for", "while", "try"))
              for line in head), head)
    values = {}
    for line in head:
        code = line.split("#", 1)[0].strip()          # drop trailing comments
        if "=" in code:
            key, val = code.split("=", 1)
            values[key.strip()] = val.strip()
    check("lines 1-8 hold exactly the seven settings",
          sorted(values) == sorted(CONFIG_NAMES), values)
    check("every setting has a value",
          all(values.get(name) for name in CONFIG_NAMES), values)
    check("shipped defaults are the documented ones",
          with_config()["key_a"] == "1" and with_config()["key_b"] == "4"
          and with_config()["delay_a"] == 1.15
          and with_config()["delay_b"] == 0.1
          and with_config()["mode"] == "toggle"
          and with_config()["max_rot"] == 0, with_config())


def test_slot_key():
    print("\n[slot mapping]")
    check("slot 0 -> '0'", asw.slot_key(0) == "0")
    check("slot 1 -> '1'", asw.slot_key(1) == "1")
    check("slot 4 -> '4'", asw.slot_key(4) == "4")
    check("slot 9 -> '9'", asw.slot_key(9) == "9")
    check("slot 10 -> '0'", asw.slot_key(10) == "0")


def test_validate():
    print("\n[config validation]")
    cfg = with_config(SLOT_A=1, SLOT_B=4, DELAY_A_MS=1150.0, DELAY_B_MS=100.0,
                      HOTKEY="f6", MODE="toggle", MAX_ROT=0)
    check("defaults validate", cfg["key_a"] == "1" and cfg["key_b"] == "4")
    check("delays are converted to seconds",
          cfg["delay_a"] == 1.15 and cfg["delay_b"] == 0.1, cfg)
    check("boundary slots accepted",
          config_error(SLOT_A=0, SLOT_B=10) is None)
    check("SLOT_A 11 rejected", config_error(SLOT_A=11) is not None)
    check("SLOT_B -1 rejected", config_error(SLOT_B=-1) is not None)
    check("non-int slot rejected", config_error(SLOT_A="1") is not None)
    check("float slot rejected", config_error(SLOT_A=1.5) is not None)
    check("negative delay rejected", config_error(DELAY_A_MS=-0.1) is not None)
    check("non-numeric delay rejected",
          config_error(DELAY_B_MS="fast") is not None)
    check("integer delays accepted (>= 0)",
          config_error(DELAY_A_MS=0, DELAY_B_MS=250) is None)
    check("nan delay rejected",
          config_error(DELAY_A_MS=float("nan")) is not None)
    check("infinite delay rejected",
          config_error(DELAY_B_MS=float("inf")) is not None)
    check("unknown MODE rejected", config_error(MODE="repeat") is not None)
    check("'hold' mode accepted", config_error(MODE="hold") is None)
    check("MODE is case-insensitive like WXB's",
          config_error(MODE="Hold") is None
          and with_config(MODE="HOLD")["mode"] == "hold")
    check("the cfg carries the mode lower-case",
          with_config(MODE="Toggle")["mode"] == "toggle")
    check("negative MAX_ROT rejected", config_error(MAX_ROT=-1) is not None)
    check("non-int MAX_ROT rejected", config_error(MAX_ROT="lots") is not None)
    check("empty HOTKEY rejected", config_error(HOTKEY="") is not None)
    check("non-string HOTKEY rejected", config_error(HOTKEY=6) is not None)
    check("error message names the setting",
          "SLOT_A" in (config_error(SLOT_A=42) or ""),
          config_error(SLOT_A=42))


def test_loop():
    print("\n[rotation]")
    real_keys = asw._keys
    try:
        # resolved cfgs carry delays in SECONDS (ms / 1000)
        fake = FakeKeys()
        asw._keys = fake
        cfg = dict(with_config(), delay_a=0.04, delay_b=0.02, max_rot=3)
        asw._stop.clear()
        t0 = time.monotonic()
        asw.loop(cfg)
        dt = time.monotonic() - t0
        check("taps slot A, then slot B",
              fake.log[:4] == ["press:1", "release:1", "press:4", "release:4"],
              fake.log[:4])
        check("MAX_ROT stops after exactly N rotations",
              fake.log.count("press:1") == 3 and fake.log.count("press:4") == 3,
              fake.log)
        check("a finite run returns instead of blocking", dt < 0.6,
              "%.3fs" % dt)
        check("presses and releases stay balanced",
              fake.log.count("press:1") == fake.log.count("release:1") and
              fake.log.count("press:4") == fake.log.count("release:4"))
        check("the run reports how many rotations it completed",
              asw.loop(cfg, threading.Event()) == 3)

        # A run that ends by itself must not set any stop event: the old code
        # set the shared one, which could cancel the run started right after
        # it (the new run cleared the event, then the old one set it again).
        own = threading.Event()
        asw.loop(cfg, own)
        check("a finite run leaves its own stop event clear",
              not own.is_set())
        check("a finite run leaves the module's live event clear",
              not asw._stop.is_set())

        # delays are actually waited out
        fake = FakeKeys()
        asw._keys = fake
        cfg = dict(with_config(), delay_a=0.06, delay_b=0.02, max_rot=2)
        asw._stop.clear()
        asw.loop(cfg)
        ts = fake.timed
        check("two rotations press four keys", len(ts) == 4, len(ts))
        check("DELAY_A_MS waited out (~60 ms)", 0.045 < ts[1] - ts[0] < 0.10,
              "%.3f" % (ts[1] - ts[0]))
        check("DELAY_B_MS waited out (~20 ms)", 0.008 < ts[2] - ts[1] < 0.06,
              "%.3f" % (ts[2] - ts[1]))
        check("the pattern repeats", fake.log.count("press:1") == 2, fake.log)

        # an infinite rotation still stops instantly on the event
        fake = FakeKeys()
        asw._keys = fake
        cfg = dict(with_config(), delay_a=5.0, delay_b=5.0, max_rot=0)
        asw._stop.clear()
        th = threading.Thread(target=asw.loop, args=(cfg,), daemon=True)
        t0 = time.monotonic()
        th.start()
        time.sleep(0.08)
        asw._stop.set()
        th.join(timeout=1.5)
        dt = time.monotonic() - t0
        check("stops inside a long delay, not after it",
              not th.is_alive() and dt < 0.4, "%.3fs" % dt)
        check("no slot B press after a mid-delay stop",
              fake.log.count("press:4") == 0, fake.log)
    finally:
        asw._stop.set()
        asw._keys = real_keys


def test_start_stop_toggle():
    print("\n[start / stop / toggle]")
    real_keys, real_a, real_b = asw._keys, asw.DELAY_A_MS, asw.DELAY_B_MS
    try:
        fake = FakeKeys()
        asw._keys = fake
        asw.DELAY_A_MS, asw.DELAY_B_MS = 25.0, 25.0
        asw._stop.clear()
        check("start() begins a rotation", asw.start() is True)
        check("a second start() is a no-op", asw.start() is False)
        time.sleep(0.15)
        check("the rotation thread is alive", asw._running is True)
        check("keys are being pressed", fake.log.count("press:1") > 0, fake.log[:2])
        check("toggle() stops a running rotation",
              asw.toggle() is None and asw._running is False)
        asw.toggle()
        check("toggle() starts it again", asw._running is True)
        check("stop() reports that it stopped something", asw.stop() is True)
        check("stop() leaves nothing running",
              asw._running is False and asw._thread is None)
        check("a second stop() is a no-op", asw.stop() is False)
        time.sleep(0.08)
        check("presses and releases stay balanced",
              fake.log.count("press:1") == fake.log.count("release:1") and
              fake.log.count("press:4") == fake.log.count("release:4"), fake.log)
    finally:
        asw.stop()
        asw.DELAY_A_MS, asw.DELAY_B_MS = real_a, real_b
        asw._keys = real_keys


def test_lifecycle():
    print("\n[run lifecycle]")
    real_keys = asw._keys
    real_a, real_b = asw.DELAY_A_MS, asw.DELAY_B_MS

    class FlakyKeys(FakeKeys):
        """A controller that dies on the Nth press."""

        def __init__(self, fail_on=1):
            super().__init__()
            self.calls = 0
            self.fail_on = fail_on

        def press(self, key):
            self.calls += 1
            if self.calls >= self.fail_on:
                raise RuntimeError("controller gone")
            super().press(key)

    try:
        asw.stop()
        asw.DELAY_A_MS, asw.DELAY_B_MS = 10.0, 10.0

        # a controller that dies mid-run must not leave the module thinking a
        # rotation is still going - that used to make every later start() a
        # silent no-op until a stop() was pressed
        asw._keys = FlakyKeys(fail_on=2)
        check("start() begins even with a flaky controller", asw.start() is True)
        deadline = time.time() + 1.5
        while asw._running and time.time() < deadline:
            time.sleep(0.01)
        check("a crashed rotation clears its running state",
              asw._running is False)
        check("a crashed rotation forgets its thread", asw._thread is None)
        check("a crashed rotation leaves no key down",
              asw._keys.held == set(), asw._keys.held)

        asw._keys = FakeKeys()
        check("the next start() is not blocked by the crash",
              asw.start() is True)
        time.sleep(0.08)
        check("and it really runs", asw._running is True)
        asw.stop()

        # a run that finishes by itself must not cancel the run started after
        # it: the new run owns a different stop event
        asw.stop()
        asw.DELAY_A_MS, asw.DELAY_B_MS = 300.0, 300.0
        cfg = dict(with_config(), delay_a=0.01, delay_b=0.01, max_rot=1)
        asw.loop(cfg, threading.Event())
        check("starting right after a finite run succeeds", asw.start() is True)
        time.sleep(0.12)
        check("the new run is not cancelled by the old one finishing",
              asw._running is True)
        check("the new run keeps tapping",
              asw._keys.log.count("press:1") > 0)
        asw.stop()
        check("a stopped run clears the state",
              asw._running is False and asw._thread is None)

        # a bad CONFIG discovered at start() is reported, not raised into the
        # hotkey callback thread
        old_mode = asw.MODE
        asw.MODE = "repeat"
        try:
            check("a bad CONFIG at start() is refused, not raised",
                  asw.start() is False)
        finally:
            asw.MODE = old_mode

        # starting twice, one right after the other, never stacks threads
        asw._keys = FakeKeys()
        asw.DELAY_A_MS, asw.DELAY_B_MS = 100.0, 100.0
        check("the first start wins", asw.start() is True)
        check("a second start while running is refused", asw.start() is False)
        threads = [t for t in threading.enumerate()
                   if t.name == "autoswitcher-rotation"]
        check("exactly one rotation thread exists", len(threads) == 1, threads)
        asw.stop()
    finally:
        asw.stop()
        asw.DELAY_A_MS, asw.DELAY_B_MS = real_a, real_b
        asw._keys = real_keys


def test_bind():
    print("\n[hotkey binding]")
    real_kb = asw.keyboard
    real_mode, real_hotkey = asw.MODE, asw.HOTKEY
    try:
        fake = FakeKeyboard()
        asw.keyboard = fake
        asw.MODE, asw.HOTKEY = "toggle", "f6"
        asw.bind()
        check("toggle mode binds one press handler on HOTKEY",
              fake.hotkeys == [("f6", asw.toggle, False)],
              [(h[0], h[1].__name__, h[2]) for h in fake.hotkeys])

        fake.hotkeys = []
        asw.HOTKEY = "ctrl+shift+f8"
        asw.bind()
        check("a modifier combo binds exactly as written",
              fake.hotkeys[0][0] == "ctrl+shift+f8", fake.hotkeys)

        fake.hotkeys = []
        asw.HOTKEY = "alt+1"
        asw.bind()
        check("alt+digit binds exactly as written",
              fake.hotkeys[0][0] == "alt+1", fake.hotkeys)

        fake.hotkeys = []
        asw.HOTKEY = "z"
        asw.bind()
        check("a single letter binds exactly as written",
              fake.hotkeys[0][0] == "z", fake.hotkeys)

        fake.hotkeys = []
        asw.MODE, asw.HOTKEY = "hold", "f8"
        asw.bind()
        check("hold mode binds press->start and release->stop",
              [(h[0], h[1].__name__, h[2]) for h in fake.hotkeys] ==
              [("f8", "start", False), ("f8", "stop", True)],
              [(h[0], h[1].__name__, h[2]) for h in fake.hotkeys])

        fake.hotkeys = []
        asw.MODE = "toggle"
        asw.bind()
        check("changing HOTKEY alone re-binds without touching bind()",
              fake.hotkeys[0][0] == "f8", fake.hotkeys)
    finally:
        asw.MODE, asw.HOTKEY = real_mode, real_hotkey
        asw.keyboard = real_kb


def main():
    test_config_block()
    test_slot_key()
    test_validate()
    test_loop()
    test_start_stop_toggle()
    test_lifecycle()
    test_bind()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for name in FAILS:
            print("  failed: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
