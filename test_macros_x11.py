#!/usr/bin/env python3
"""Real-X11 macro acceptance checks.  Run under Xvfb or a live X11 session.

The suite uses Xlib's XTEST extension for pointer/keyboard input, the shipped
MacroRecorder/MacroPlayer through StudioService, and pynput listeners only as
observers of what the X server actually delivered.  It does not use fake sinks
for the round trip or fake hotkey dispatch.
"""

import tempfile
import time
import unittest
from pathlib import Path

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)

try:
    from Xlib import display, X, XK
    from Xlib.ext import xtest
except Exception:  # pragma: no cover - platform without Xlib
    display = X = XK = xtest = None

try:
    from pynput import keyboard, mouse
except Exception:  # pragma: no cover - platform without pynput
    keyboard = mouse = None

import wxb_macros as macros
import wxb_studio_service as svc

print(wxb_test_isolation.assert_isolated("macros-x11"))


def wait_until(predicate, timeout=2.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


class XTest:
    """Minimal real X server input driver using XTEST."""

    def __init__(self):
        self.display = display.Display()
        if self.display.query_extension("XTEST") is None:
            raise unittest.SkipTest("X server has no XTEST extension")
        screen = self.display.screen()
        self.width = screen.width_in_pixels
        self.height = screen.height_in_pixels
        self._codes = {}

    def close(self):
        try:
            self.display.close()
        except Exception:
            pass

    def code(self, name):
        if name not in self._codes:
            keysym = XK.string_to_keysym(name)
            code = self.display.keysym_to_keycode(keysym)
            if not code:
                raise AssertionError("X11 has no keycode for %s" % name)
            self._codes[name] = code
        return self._codes[name]

    def key(self, name, pressed):
        xtest.fake_input(self.display,
                         X.KeyPress if pressed else X.KeyRelease,
                         self.code(name))
        self.display.sync()

    def chord(self, names):
        for name in names:
            self.key(name, True)
            time.sleep(0.04)
        time.sleep(0.05)
        for name in reversed(names):
            self.key(name, False)
            time.sleep(0.04)
        time.sleep(0.20)

    def move(self, x, y):
        xtest.fake_input(self.display, X.MotionNotify, x=int(x), y=int(y))
        self.display.sync()

    def button(self, number, pressed):
        xtest.fake_input(self.display,
                         X.ButtonPress if pressed else X.ButtonRelease,
                         int(number))
        self.display.sync()

    def scroll(self, down=True):
        button = 5 if down else 4
        self.button(button, True)
        self.button(button, False)

    def pointer(self):
        pointer = self.display.screen().root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)


class Observer:
    def __init__(self):
        self.events = []
        self._mouse = None
        self._keyboard = None

    def start(self):
        self._mouse = mouse.Listener(
            on_move=lambda x, y: self.events.append(("move", int(x), int(y))),
            on_click=lambda x, y, button, pressed: self.events.append(
                ("button", int(x), int(y), button.name, bool(pressed))),
            on_scroll=lambda x, y, dx, dy: self.events.append(
                ("scroll", int(x), int(y), int(dx), int(dy))))
        self._keyboard = keyboard.Listener(
            on_press=lambda key: self.events.append(
                ("keydown", getattr(key, "char", None) or str(key))),
            on_release=lambda key: self.events.append(
                ("keyup", getattr(key, "char", None) or str(key))))
        self._mouse.daemon = True
        self._keyboard.daemon = True
        self._mouse.start()
        self._keyboard.start()
        time.sleep(0.35)

    def clear(self):
        self.events[:] = []

    def stop(self):
        for listener in (self._mouse, self._keyboard):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        time.sleep(0.2)


class RealMacroX11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if display is None or xtest is None or keyboard is None:
            raise unittest.SkipTest("Xlib, XTEST or pynput is unavailable")
        try:
            cls.x = XTest()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest("No usable X11 display: %s" % exc)
        cls.x.move(20, 20)

    @classmethod
    def tearDownClass(cls):
        cls.x.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.x.move(20, 20)
        self.service = svc.StudioService(
            path=self.path / "studio.json",
            macro_store=macros.MacroStore(self.path / "macros"))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.service.stop_hotkeys)

    def snapshot(self):
        return self.service.macro_snapshot()

    def assert_no_control_events(self, doc):
        controls = {"CTRL", "ALT", "SHIFT", "M", "N", "B"}
        seen = [event.get("key") for event in doc["events"]
                if event.get("type") == "key"]
        self.assertFalse(set(seen) & controls,
                         "control-hotkey events leaked into the macro: %r" % seen)

    def test_real_xtest_capture_and_playback_round_trip(self):
        self.assertTrue(self.service.macro_capabilities()["record"])
        ok, message = self.service.start_macro_recording("X11 Round Trip")
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(
            lambda: (self.service._macro_recorder is not None
                     and self.service._macro_recorder.running), 2.0),
            "real recorder listeners did not start")
        time.sleep(0.25)

        self.x.move(240, 260)
        time.sleep(0.12)
        self.x.button(1, True)
        time.sleep(0.08)
        self.x.button(1, False)
        time.sleep(0.08)
        self.x.button(3, True)
        time.sleep(0.08)
        self.x.button(3, False)
        time.sleep(0.08)
        self.x.scroll()
        time.sleep(0.10)
        self.x.key("a", True)
        time.sleep(0.12)
        self.x.key("a", False)
        time.sleep(0.15)

        ok, message = self.service.stop_macro_recording()
        self.assertTrue(ok, message)
        doc = self.service._macro_doc
        self.assertIsNotNone(doc)
        events = doc["events"]
        self.assertGreaterEqual(len(events), 7, events)
        self.assertGreater(doc["duration"], 0.3)

        moves = [event for event in events if event["type"] == "mouse_move"]
        self.assertTrue(any((event["x"], event["y"]) == (240, 260)
                            for event in moves), events)
        buttons = [(event["button"], event["pressed"])
                   for event in events if event["type"] == "mouse_button"]
        self.assertEqual(buttons, [("left", True), ("left", False),
                                   ("right", True), ("right", False)],
                         events)
        scrolls = [event for event in events
                   if event["type"] == "mouse_scroll"]
        self.assertTrue(scrolls and any(event["dy"] for event in scrolls),
                        events)
        keys = [(event["key"], event["pressed"])
                for event in events if event["type"] == "key"]
        self.assertEqual(keys, [("A", True), ("A", False)], events)
        self.assert_no_control_events(doc)

        def index_of(predicate):
            return next(index for index, event in enumerate(events)
                        if predicate(event))

        order = [
            index_of(lambda event: event["type"] == "mouse_move"
                     and (event["x"], event["y"]) == (240, 260)),
            index_of(lambda event: event["type"] == "mouse_button"
                     and event["button"] == "left" and event["pressed"]),
            index_of(lambda event: event["type"] == "mouse_button"
                     and event["button"] == "left" and not event["pressed"]),
            index_of(lambda event: event["type"] == "mouse_button"
                     and event["button"] == "right" and event["pressed"]),
            index_of(lambda event: event["type"] == "mouse_button"
                     and event["button"] == "right" and not event["pressed"]),
            index_of(lambda event: event["type"] == "mouse_scroll"),
            index_of(lambda event: event["type"] == "key"
                     and event["pressed"]),
            index_of(lambda event: event["type"] == "key"
                     and not event["pressed"]),
        ]
        self.assertEqual(order, sorted(order), events)

        key_down = next(event for event in events
                        if event["type"] == "key" and event["pressed"])
        key_up = next(event for event in events
                      if event["type"] == "key" and not event["pressed"])
        held = key_up["at"] - key_down["at"]
        self.assertGreaterEqual(held, 0.05, events)
        self.assertLess(held, 0.6, events)
        self.assertTrue(all(event["at"] >= 0 for event in events))
        self.assertEqual(sorted(event["at"] for event in events),
                         [event["at"] for event in events])

        saved = self.service._macro_store.path(doc["name"])
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

        observer = Observer()
        observer.start()
        observer.clear()
        try:
            ok, message = self.service.set_macro_settings(
                speed=2.0, loop_mode="count", repeat=2)
            self.assertTrue(ok, message)
            started = time.monotonic()
            ok, message = self.service.start_macro_playback()
            self.assertTrue(ok, message)
            self.assertTrue(wait_until(
                lambda: not self.service.macro_running, 5.0),
                "playback did not finish: %r" % (self.snapshot(),))
            elapsed = time.monotonic() - started
            time.sleep(0.2)
        finally:
            observer.stop()

        snap = self.snapshot()
        self.assertEqual(snap["loop_done"], 2, snap)
        self.assertEqual(snap["speed"], 2.0, snap)
        self.assertIn("2 loops", snap["notice"], snap)
        self.assertFalse(snap["error"], snap)

        events_seen = list(observer.events)
        buttons_seen = [event for event in events_seen
                        if event[0] == "button"]
        keys_seen = [event for event in events_seen if event[0] == "keydown"]
        scrolls_seen = [event for event in events_seen if event[0] == "scroll"]
        moves_seen = [event for event in events_seen
                      if event[0] == "move" and event[1:3] == (240, 260)]
        self.assertEqual(len(keys_seen), 2, events_seen)
        self.assertEqual(len([event for event in buttons_seen
                              if event[3] == "left" and event[4]]), 2,
                         events_seen)
        self.assertEqual(len([event for event in buttons_seen
                              if event[3] == "right" and event[4]]), 2,
                         events_seen)
        self.assertGreaterEqual(len(scrolls_seen), 2, events_seen)
        self.assertGreaterEqual(len(moves_seen), 2, events_seen)
        self.assertEqual(self.x.pointer(), (240, 260))
        self.assertLess(
            elapsed, max(0.5, doc["duration"] * 1.5),
            "2x playback took %.3fs for a %.3fs macro; speed may not be "
            "applied (2 loops expected about one macro duration): %r"
            % (elapsed, doc["duration"], snap))

    def test_live_configurable_hotkeys_record_play_and_stop(self):
        values = dict(self.service.settings["hotkeys"])
        values.update(record_macro="ctrl+alt+m",
                      play_macro="ctrl+alt+n",
                      stop_macro="ctrl+alt+b")
        ok, message = self.service.set_hotkeys(values)
        self.assertTrue(ok, message)
        ok, message = self.service.start_hotkeys()
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(
            lambda: (self.service._hotkeys is not None
                     and self.service._hotkeys.running), 2.0),
            "global hotkey listener did not start: %s"
            % self.service.hotkey_error)

        self.x.chord(["Control_L", "Alt_L", "m"])
        self.assertTrue(wait_until(
            lambda: self.snapshot()["recording"], 2.0),
            "record hotkey did not start recording: %r"
            % self.snapshot())
        time.sleep(0.2)
        self.x.move(77, 88)
        time.sleep(0.15)
        self.x.key("a", True)
        time.sleep(0.10)
        self.x.key("a", False)
        time.sleep(1.0)

        self.x.chord(["Control_L", "Alt_L", "b"])
        self.assertTrue(wait_until(
            lambda: (not self.snapshot()["recording"]
                     and self.service._macro_doc is not None), 3.0),
            "stop hotkey did not stop recording: %r" % self.snapshot())
        doc = self.service._macro_doc
        self.assertIsNotNone(doc, self.snapshot())
        self.assertGreaterEqual(doc["duration"], 0.8, doc)
        self.assertTrue(any(event["type"] == "mouse_move"
                            for event in doc["events"]), doc)
        self.assert_no_control_events(doc)

        # Play through the live configured hotkey.  The recorded tail wait
        # leaves enough time for the stop hotkey to interrupt this run.
        self.x.chord(["Control_L", "Alt_L", "n"])
        self.assertTrue(wait_until(
            lambda: self.snapshot()["playing"], 1.5),
            "play hotkey did not start playback: %r" % self.snapshot())
        self.x.chord(["Control_L", "Alt_L", "b"])
        self.assertTrue(wait_until(
            lambda: not self.snapshot()["running"], 2.0),
            "stop hotkey did not interrupt playback: %r" % self.snapshot())
        self.assertIn("stopped", self.snapshot()["notice"].lower(),
                      self.snapshot())

    def test_conflicting_hotkeys_are_refused(self):
        values = dict(self.service.settings["hotkeys"])
        values.update(record_macro="ctrl+alt+m",
                      play_macro="ctrl+alt+n",
                      stop_macro="ctrl+alt+b")
        ok, message = self.service.set_hotkeys(values)
        self.assertTrue(ok, message)
        duplicate = dict(values)
        duplicate["play_macro"] = duplicate["record_macro"]
        ok, message = self.service.set_hotkeys(duplicate)
        self.assertFalse(ok)
        self.assertIn("both use", message)
        self.assertEqual(self.service.settings["hotkeys"]["play_macro"],
                         "ctrl+alt+n")

        # Bypass set_hotkeys to prove start_hotkeys itself refuses a bad tree;
        # it must not create a listener that silently double-binds one chord.
        self.service.stop_hotkeys()
        self.service.settings["hotkeys"]["play_macro"] = (
            self.service.settings["hotkeys"]["record_macro"])
        ok, message = self.service.start_hotkeys()
        self.assertFalse(ok)
        self.assertIn("both use", message)
        self.assertIsNone(self.service._hotkeys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
