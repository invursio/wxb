#!/usr/bin/env python3
"""Macro format, recorder, player and StudioService integration tests."""

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)
import wxb_engine as engine
import wxb_macros as macros
import wxb_studio_service as svc

print(wxb_test_isolation.assert_isolated("macros"))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class OvershootClock(Clock):
    def sleep(self, seconds):
        self.now += max(0.0, seconds) + 11.0


class Sink:
    def __init__(self):
        self.calls = []
        self.released = 0
        self.fail = None

    def move_pointer(self, x, y):
        self._record("move", x, y)

    def press_mouse(self, button):
        self._record("down", button)

    def release_mouse(self, button=None):
        self._record("up", button)

    def scroll(self, dx, dy):
        self._record("scroll", dx, dy)

    def key_down(self, key):
        self._record("keydown", key)

    def key_up(self, key):
        self._record("keyup", key)

    def _record(self, *args):
        if self.fail:
            raise self.fail
        self.calls.append(args)

    def release_all(self):
        self.released += 1


def doc(*events):
    return macros.make_macro("Test", events)


class ValidationTests(unittest.TestCase):
    def test_capability_matrix_reports_x11_and_wayland_limits(self):
        with mock.patch.object(macros.importlib.util, "find_spec",
                               return_value=object()):
            x11 = macros.detect_capabilities(
                {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
            wayland = macros.detect_capabilities(
                {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
            none = macros.detect_capabilities({"XDG_SESSION_TYPE": "x11"})
        self.assertTrue(x11["record"] and x11["play"] and x11["hotkeys"])
        self.assertFalse(wayland["record"])
        self.assertTrue(wayland["xwayland"])
        self.assertTrue(wayland["play"])
        self.assertFalse(none["record"] or none["play"] or none["hotkeys"])

    def test_versioned_document_and_order(self):
        got = macros.validate_macro({
            "format": macros.MACRO_FORMAT, "version": 1, "name": "  order  ",
            "events": [
                {"at": 0.2, "type": "key", "key": "A", "pressed": False},
                {"at": 0.1, "type": "mouse_move", "x": 10, "y": 20},
            ],
        })
        self.assertEqual(got["name"], "order")
        self.assertEqual([item["at"] for item in got["events"]], [0.1, 0.2])
        self.assertEqual(got["duration"], 0.2)

    def test_bad_format_versions_and_types_are_refused(self):
        good = {"format": macros.MACRO_FORMAT, "version": 1, "events": []}
        for broken in (
                {**good, "format": "other"},
                {**good, "version": 2},
                {**good, "events": "no"},
                {**good, "events": [{"at": -1, "type": "key",
                                     "key": "A", "pressed": True}]},
                {**good, "events": [{"at": 0, "type": "mouse_move",
                                     "x": 1, "y": float("inf")}]},
                {**good, "events": [{"at": 0, "type": "key",
                                     "key": "NOT-A-REAL-KEY", "pressed": True}]},
                {**good, "events": [{"at": 0, "type": "mouse_button",
                                     "button": "side", "pressed": True}]}):
            with self.assertRaises(macros.MacroError):
                macros.validate_macro(broken)

    def test_recorder_captures_order_scroll_and_control_ignore(self):
        clock = Clock()
        rec = macros.MacroRecorder(ignore_keys={"A"}, clock=clock)
        rec._start = 0.0
        rec._on_move(4, 5)
        clock.now = 0.05
        rec._on_click(4, 5, SimpleNamespace(name="left"), True)
        clock.now = 0.1
        rec._on_scroll(4, 5, 0, -2)
        rec._on_press(engine.keyboard.KeyCode.from_char("a"))
        rec._on_release(engine.keyboard.KeyCode.from_char("b"))
        self.assertEqual([item["type"] for item in rec.events],
                         ["mouse_move", "mouse_button", "mouse_scroll", "key"])
        self.assertEqual(rec.events[0]["at"], 0.0)
        self.assertAlmostEqual(rec.events[1]["at"], 0.05)
        self.assertEqual(rec.events[3]["key"], "B")

    def test_recorder_strips_control_chords_but_retains_normal_keys(self):
        keyboard = engine.keyboard
        clock = Clock()
        rec = macros.MacroRecorder(
            control_combos=[{"CTRL", "ALT", "R"}, {"F6"}], clock=clock)
        rec._start = 0.0
        rec._on_press(keyboard.Key.ctrl_l)
        rec._on_press(keyboard.Key.alt_l)
        rec._on_press(keyboard.KeyCode.from_char("r"))
        rec._on_release(keyboard.KeyCode.from_char("r"))
        rec._on_release(keyboard.Key.alt_l)
        rec._on_release(keyboard.Key.ctrl_l)
        rec._on_press(keyboard.KeyCode.from_char("r"))
        rec._on_release(keyboard.KeyCode.from_char("r"))
        rec._on_press(keyboard.Key.f6)
        rec._on_release(keyboard.Key.f6)
        rec._on_press(keyboard.Key.ctrl_l)
        rec._on_press(keyboard.KeyCode.from_char("c"))
        rec._on_release(keyboard.KeyCode.from_char("c"))
        rec._on_release(keyboard.Key.ctrl_l)
        filtered = rec.stop()
        self.assertEqual([(event["key"], event["pressed"])
                          for event in filtered],
                         [("R", True), ("R", False),
                          ("CTRL", True), ("C", True),
                          ("C", False), ("CTRL", False)])

    def test_recorder_drops_orphan_control_releases_after_hotkey_stop(self):
        keyboard = engine.keyboard
        rec = macros.MacroRecorder(
            control_combos=[{"CTRL", "ALT", "M"}], clock=Clock())
        rec._start = 0.0
        # The hotkey listener consumes the M press and stops recording.  The
        # X11 listener can still deliver the chord's release events.
        rec._on_release(keyboard.KeyCode.from_char("m"))
        rec._on_release(keyboard.Key.alt_l)
        rec._on_release(keyboard.Key.ctrl_l)
        rec._append({"type": "mouse_move", "x": 7, "y": 8})
        self.assertEqual([event["type"] for event in rec.stop()],
                         ["mouse_move"])


class PlayerTests(unittest.TestCase):
    def test_order_loops_speed_and_release(self):
        sink = Sink()
        clock = Clock()
        player = macros.MacroPlayer(sink, clock=clock, sleep=clock.sleep)
        play_doc = doc(
            {"at": 0, "type": "mouse_move", "x": 1, "y": 2},
            {"at": 0.2, "type": "key", "key": "A", "pressed": True},
            {"at": 0.4, "type": "key", "key": "A", "pressed": False},
            {"at": 0.4, "type": "mouse_scroll", "dx": 0, "dy": -1},
        )
        count = player.play(play_doc, speed=2.0, loops=2)
        self.assertEqual(count, 2)
        self.assertEqual([call[0] for call in sink.calls],
                         ["move", "keydown", "keyup", "scroll"] * 2)
        self.assertEqual(sink.released, 2)

    def test_stop_and_session_lock_release_inputs(self):
        stop_sink = Sink()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(macros.MacroStopped):
            macros.MacroPlayer(stop_sink).play(
                doc({"at": 0.5, "type": "key", "key": "A", "pressed": True}),
                stop=stop)
        self.assertEqual(stop_sink.released, 1)

        locked_sink = Sink()
        with self.assertRaises(macros.MacroInterrupted):
            macros.MacroPlayer(locked_sink, session_guard=lambda: True).play(
                doc({"at": 0, "type": "key", "key": "A", "pressed": True}))
        self.assertEqual(locked_sink.released, 1)

    def test_operation_failure_releases_and_sleep_gap_stops(self):
        sink = Sink()
        sink.fail = RuntimeError("permission lost")
        with self.assertRaises(macros.MacroError):
            macros.MacroPlayer(sink).play(
                doc({"at": 0, "type": "mouse_move", "x": 1, "y": 2}))
        self.assertEqual(sink.released, 1)

        clock = OvershootClock()
        player = macros.MacroPlayer(Sink(), clock=clock, sleep=clock.sleep,
                                    session_guard=lambda: False)
        # A sleep that returns far later means the desktop was suspended.
        with self.assertRaises(macros.MacroInterrupted):
            player.play(doc({"at": 0.5, "type": "mouse_move", "x": 1, "y": 2}))

    def test_ten_thousand_step_stress(self):
        sink = Sink()
        clock = Clock()
        player = macros.MacroPlayer(sink, clock=clock, sleep=clock.sleep)
        play_doc = doc(*[{"at": 0, "type": "mouse_move", "x": i, "y": i}
                         for i in range(10000)])
        count = player.play(play_doc, speed=10.0)
        self.assertEqual(count, 1)
        self.assertEqual(len(sink.calls), 10000)
        self.assertEqual(sink.released, 1)


class StoreTests(unittest.TestCase):
    def test_traversal_names_and_targets_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = macros.MacroStore(Path(tmp) / "macros")
            for name in ("../escape", "a/b", "a\\b", "..", "", "x\x00y"):
                self.assertIsNone(store.path(name), name)
            ok, _message, _path = store.save("../escape", doc(
                {"at": 0, "type": "mouse_move", "x": 1, "y": 2}))
            self.assertFalse(ok)

    def test_private_atomic_store_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = macros.MacroStore(Path(tmp) / "macros")
            good = doc({"at": 0, "type": "mouse_move", "x": 1, "y": 2})
            ok, message, path = store.save("private", good)
            self.assertTrue(ok, message)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())
            loaded, error = store.load("private")
            self.assertFalse(error)
            self.assertEqual(loaded["events"], good["events"])

            bad = store.root / "bad.wxbmacro.json"
            bad.write_text('{"format": "wxb.macro", "version": 1, "events": [',
                           encoding="utf-8")
            loaded, error = store.load("bad")
            self.assertIsNone(loaded)
            self.assertIn("Could not read", error)

            link = store.root / "link.wxbmacro.json"
            os.symlink(path, link)
            loaded, error = store.load("link")
            self.assertIsNone(loaded)
            self.assertIn("symlink", error.lower())

    def test_import_export_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "macros"
            store = macros.MacroStore(root)
            good = doc({"at": 0, "type": "key", "key": "A", "pressed": True})
            source = Path(tmp) / "source.json"
            source.write_text(json.dumps(good), encoding="utf-8")
            ok, message = store.import_file(source)
            self.assertTrue(ok, message)
            self.assertIn("Test", store.list())
            target = Path(tmp) / "export.json"
            ok, message = store.export_file(good, target)
            self.assertTrue(ok, message)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("[]", encoding="utf-8")
            ok, message = store.import_file(corrupt)
            self.assertFalse(ok)
            self.assertIn("not a", message)


class MacroSink:
    def __init__(self):
        self.calls = []
        self.released = 0

    def move_pointer(self, x, y):
        self.calls.append(("move", x, y))

    def press_mouse(self, button):
        self.calls.append(("down", button))

    def release_mouse(self, button=None):
        self.calls.append(("up", button))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def key_down(self, key):
        self.calls.append(("keydown", key))

    def key_up(self, key):
        self.calls.append(("keyup", key))

    def release_all(self):
        self.released += 1

    def pointer_position(self):
        return 0, 0

    def release_keys(self):
        pass


class FakeRecorder:
    def __init__(self, events=None, **kwargs):
        self._events = events or []
        self.error = ""
        self.started = False
        self.stopped = False

    @property
    def event_count(self):
        return len(self._events)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return list(self._events)


class ServiceMacroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.store = macros.MacroStore(Path(self.tmp.name) / "macros")
        self.macro_sink = MacroSink()
        self.service = svc.StudioService(
            path=self.path, sink_factory=lambda: MacroSink(),
            macro_store=self.store,
            macro_sink_factory=lambda: self.macro_sink)
        self.service.macro_capabilities = lambda: {
            "session": "x11", "x11": True, "xwayland": False,
            "record": True, "play": True, "hotkeys": True, "reason": ""}
        self.service._session_locked = lambda: False
        self.recorder = FakeRecorder(events=[
            {"at": 0, "type": "mouse_move", "x": 1, "y": 2},
            {"at": 0.1, "type": "key", "key": "A", "pressed": True},
        ])
        self.service._macro_recorder_factory = lambda **kwargs: self.recorder

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def wait(self, predicate, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_record_to_private_file_and_open(self):
        ok, message = self.service.start_macro_recording("Recorded")
        self.assertTrue(ok, message)
        self.assertTrue(self.recorder.started)
        self.assertTrue(self.service.macro_running)
        ok, message = self.service.stop_macro_recording()
        self.assertTrue(ok, message)
        self.assertFalse(self.service.macro_running)
        target = self.store.path("Recorded")
        self.assertTrue(target.is_file())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.service.list_macro_steps()[1]["key"], "A")
        ok, message = self.service.load_macro("Recorded")
        self.assertTrue(ok, message)

    def test_playback_loops_and_stop_release(self):
        self.service._macro_doc = macros.make_macro("Play", [
            {"at": 0, "type": "key", "key": "A", "pressed": True},
            {"at": 0.01, "type": "key", "key": "A", "pressed": False},
        ])
        self.service.set_macro_settings(speed=10.0, loop_mode="count", repeat=2)
        ok, message = self.service.start_macro_playback()
        self.assertTrue(ok, message)
        self.assertTrue(self.wait(lambda: not self.service.macro_running))
        self.assertEqual(self.macro_sink.calls,
                         [("keydown", "A"), ("keyup", "A")] * 2)
        self.assertGreaterEqual(self.macro_sink.released, 1)
        self.assertFalse(self.service.macro_snapshot()["error"])

    def test_playback_stop_releases_and_f8_guards(self):
        self.service._macro_doc = macros.make_macro("Long", [
            {"at": 10, "type": "key", "key": "A", "pressed": True},
        ])
        ok, message = self.service.start_macro_playback()
        self.assertTrue(ok, message)
        self.assertTrue(self.service.macro_running)
        refused, message = self.service.start_clicker(20, 50, "left")
        self.assertFalse(refused)
        self.assertIn("macro", message.lower())
        ok, message = self.service.handle_hotkey("f8")
        self.assertTrue(ok)
        self.assertFalse(self.service.macro_running)
        self.assertGreaterEqual(self.macro_sink.released, 1)

    def test_corrupt_import_and_duplicate_hotkeys(self):
        corrupt = Path(self.tmp.name) / "corrupt.json"
        corrupt.write_text("not json", encoding="utf-8")
        ok, message = self.service.import_macro(corrupt)
        self.assertFalse(ok)
        self.assertIn("Could not read", message)
        values = dict(svc.HOTKEY_DEFAULTS)
        values["play_macro"] = values["record_macro"]
        ok, message = self.service.set_hotkeys(values)
        self.assertFalse(ok)
        self.assertIn("both use", message)

    def test_clicker_and_macro_are_mutually_exclusive(self):
        ok, message = self.service.start_clicker(20, 50, "left")
        self.assertTrue(ok, message)
        ok, message = self.service.start_macro_recording("Blocked")
        self.assertFalse(ok)
        self.assertIn("clicker", message.lower())
        self.service.stop_clicker(wait=True)

    def test_settings_and_snapshot_shape(self):
        ok, message = self.service.set_macro_settings(
            speed=2.5, loop_mode="count", repeat=4)
        self.assertTrue(ok, message)
        snap = self.service.snapshot()
        self.assertIn("macros", snap)
        self.assertEqual(snap["macros"]["speed"], 2.5)
        self.assertEqual(snap["macros"]["repeat"], 4)
        self.assertIn("record_macro", self.service.hotkey_mapping())

    def test_record_hotkey_toggles_and_saves(self):
        ok, message = self.service.handle_hotkey("record_macro")
        self.assertTrue(ok, message)
        self.assertEqual(self.service._macro_mode, "recording")
        ok, message = self.service.handle_hotkey("record_macro")
        self.assertTrue(ok, message)
        self.assertFalse(self.service.macro_running)
        self.assertTrue(self.store.path(self.service._macro_pending_name).is_file())

    def test_session_lock_stops_and_saves_recording(self):
        self.service._session_locked = lambda: True
        ok, message = self.service.start_macro_recording("Locked")
        self.assertTrue(ok, message)
        deadline = time.monotonic() + 3
        while (self.service.macro_running
               or "locked" not in self.service.macro_snapshot()["notice"].lower()) \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.service.macro_running)
        self.assertIn("locked", self.service.macro_snapshot()["notice"].lower())
        self.assertTrue(self.store.path("Locked").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
