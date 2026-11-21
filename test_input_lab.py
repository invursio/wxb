#!/usr/bin/env python3
"""Pure checks for the optional Input Lab, remaps, controllers and overlays."""

import unittest
from types import SimpleNamespace

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)
import wxb_input_lab as lab
import wxb_engine as engine

print(wxb_test_isolation.assert_isolated("input-lab"))


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_bounded(self):
        clean = lab.normalize_input_lab({})
        self.assertEqual(clean, lab.DEFAULT_INPUT_LAB)
        self.assertFalse(clean["visualizer"]["keys"])
        self.assertFalse(clean["visualizer"]["clicks"])
        dirty = lab.normalize_input_lab({
            "timeline": "yes", "deadzone": 4, "active_profile": "../bad",
            "visualizer": {"keys": True, "clicks": 1, "size": 900,
                           "color": "red", "opacity": -10},
        })
        self.assertFalse(dirty["timeline"])
        self.assertEqual(dirty["deadzone"], lab.DEADZONE_MAX)
        self.assertEqual(dirty["active_profile"], "")
        self.assertTrue(dirty["visualizer"]["keys"])
        self.assertFalse(dirty["visualizer"]["clicks"])
        self.assertEqual(dirty["visualizer"]["size"], lab.VISUALIZER_SIZE_MAX)
        self.assertEqual(dirty["visualizer"]["color"], "#9fcaff")
        self.assertEqual(dirty["visualizer"]["opacity"],
                         lab.VISUALIZER_OPACITY_MIN)

    def test_profile_validation_rejects_conflicts(self):
        good, error = lab.normalize_profile({
            "name": "Tutorial",
            "mappings": [{"source": "A", "target": "B"}],
        }, require_name=True)
        self.assertTrue(good, error)
        self.assertEqual(good["mode"], "hold")
        self.assertEqual(good["mappings"][0], {"source": "A", "target": "B"})

        for bad in (
            {"name": "x", "mappings": [{"source": "A", "target": "A"}]},
            {"name": "x", "mappings": [{"source": "A", "target": "D"}]},
            {"name": "x", "mappings": [
                {"source": "A", "target": "B"},
                {"source": "C", "target": "B"}]},
            {"name": "x", "mode": "toggle",
             "mappings": [{"source": "A", "target": "B"}]},
            {"name": "x", "mappings": [{"source": "A", "target": "B"},
                                      {"source": "B", "target": "C"}]},
            {"name": "x", "mappings": [{"source": "A", "target": "LOL"}]},
        ):
            self.assertFalse(lab.normalize_profile(bad)[0], bad)
        self.assertFalse(lab.normalize_profile({"name": "x"},
                                                require_name=True)[0])

    def test_profile_conflicts_against_wxb_hotkeys(self):
        profile = {"name": "x", "mappings": [
            {"source": "Space", "target": "F8"}]}
        conflicts = lab.profile_conflicts(
            profile, {"toggle_socd": "ctrl+shift+f8"})
        self.assertTrue(any("F8" in item for item in conflicts))
        self.assertTrue(any("Target" in item for item in conflicts))

    def test_capabilities_explain_every_missing_layer(self):
        caps = lab.detect_input_lab_capabilities(
            {"DISPLAY": "", "XDG_SESSION_TYPE": "wayland"},
            evdev=None, euid=1000,
            read_check=lambda: {"ok": False, "devices": 0})
        self.assertFalse(caps["remap"])
        self.assertFalse(caps["controller"])
        self.assertFalse(caps["visualizer"])
        self.assertIn("readable", caps["reason"])

        caps = lab.detect_input_lab_capabilities(
            {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"},
            evdev=object(), euid=0,
            read_check=lambda: {"ok": True, "devices": 4})
        self.assertFalse(caps["remap"])
        self.assertTrue(caps["root"])
        self.assertIn("root", caps["reason"])


class RemapTests(unittest.TestCase):
    def test_hold_mirrors_and_counts_multiple_keyboards(self):
        remap = lab.RemapEngine({"name": "x", "mappings": [
            {"source": "Space", "target": "Enter"}]})
        self.assertTrue(remap.handles(57))
        self.assertEqual(remap.press("kbd1", 57), [("down", 28)])
        self.assertEqual(remap.press("kbd2", 57), [])
        self.assertEqual(remap.release("kbd1", 57), [])
        self.assertEqual(remap.release("kbd2", 57), [("up", 28)])
        self.assertEqual(remap.release_all(), [])

    def test_toggle_releases_on_second_press_and_source_loss(self):
        remap = lab.RemapEngine({"name": "x", "mode": "toggle",
                                 "mappings": [
                                     {"source": "Space", "target": "Enter"}]})
        self.assertEqual(remap.press("kbd", 57), [("down", 28)])
        self.assertEqual(remap.release("kbd", 57), [])
        self.assertEqual(remap.press("kbd", 57), [("up", 28)])
        self.assertEqual(remap.press("kbd2", 57), [("down", 28)])
        self.assertEqual(remap.forget_source("kbd2"), [("up", 28)])

    def test_direction_output_is_translated_but_not_handled_as_raw_key(self):
        remap = lab.RemapEngine({"name": "x", "mappings": [
            {"source": "A", "target": "B"}]})
        self.assertEqual(remap.direction_output(30), 48)
        self.assertFalse(remap.handles(30))
        self.assertEqual(remap.press("kbd", 30), [])
        self.assertEqual(remap.release_all(), [])

    def test_forget_source_and_release_all_cover_every_held_target(self):
        remap = lab.RemapEngine({"name": "x", "mode": "hold",
                                 "mappings": [
                                     {"source": "Space", "target": "Enter"},
                                     {"source": "Tab", "target": "F1"}]})
        remap.press("kbd", 57)
        remap.press("kbd", 15)
        self.assertEqual(remap.release_all(), [("up", 28), ("up", 59)])
        self.assertEqual(remap.down_codes(), [])


class ControllerTests(unittest.TestCase):
    class Event:
        def __init__(self, kind, code, value, info=None):
            self.type, self.code, self.value = kind, code, value
            self.info = info

    def test_buttons_sticks_and_deadzone(self):
        monitor = lab.ControllerMonitor(evdev=object(), access_check=None)
        monitor._states["/dev/input/event9"] = {
            "name": "Fake Pad", "buttons": [], "axes": {}}
        monitor._handle("/dev/input/event9", self.Event(lab.EV_KEY, 304, 1))
        monitor._handle("/dev/input/event9", self.Event(
            lab.EV_ABS, 0, 200, SimpleNamespace(min=0, max=255)))
        snap = monitor.snapshot()
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["buttons"], ["A"])
        self.assertGreater(snap["axes"]["Left X"]["raw"], 0)
        monitor.set_deadzone(0.5)
        self.assertLess(monitor.snapshot()["axes"]["Left X"]["value"],
                        snap["axes"]["Left X"]["raw"])
        monitor.set_deadzone(0)
        self.assertGreater(monitor.snapshot()["axes"]["Left X"]["value"], 0)
        monitor._handle("/dev/input/event9", self.Event(lab.EV_KEY, 304, 0))
        self.assertFalse(monitor.snapshot()["buttons"])

    def test_disconnect_clears_state_and_reports_reconnect(self):
        monitor = lab.ControllerMonitor(evdev=object(), access_check=None)
        monitor._states["/dev/input/event9"] = {
            "name": "Fake Pad", "buttons": ["A"], "axes": {"Left X": 0.5}}
        monitor._drop("/dev/input/event9", "Controller disconnected")
        snap = monitor.snapshot()
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["buttons"], [])
        self.assertIn("disconnected", snap["error"])

    def test_controller_detection_uses_real_capabilities(self):
        class Device:
            name = "Pad"

            def capabilities(self):
                return {lab.EV_KEY: [304, 305], lab.EV_ABS: [0, 1]}

            def close(self):
                pass

        self.assertTrue(lab.ControllerMonitor._is_controller(Device()))
        Device.capabilities = lambda self: {lab.EV_KEY: [30],
                                            lab.EV_ABS: [0]}
        self.assertFalse(lab.ControllerMonitor._is_controller(Device()))


class VisualizerTests(unittest.TestCase):
    class FakeKeyboard:
        created = []

        def __init__(self, on_press=None, on_release=None):
            self.on_press = on_press
            self.stopped = False
            self.created.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    class FakeMouse:
        created = []

        def __init__(self, on_click=None):
            self.on_click = on_click
            self.stopped = False
            self.created.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    def setUp(self):
        type(self).FakeKeyboard.created.clear()
        type(self).FakeMouse.created.clear()
        self.keys = []
        self.clicks = []
        self.capture = lab.VisualizerCapture(
            on_key=self.keys.append,
            on_click=lambda label, x, y: self.clicks.append((label, x, y)),
            keyboard_factory=self.FakeKeyboard,
            mouse_factory=self.FakeMouse,
            environ={"DISPLAY": ":99", "XDG_SESSION_TYPE": "x11"})

    def test_listeners_are_created_only_for_enabled_switches(self):
        ok, message = self.capture.start(keys=True, clicks=False)
        self.assertTrue(ok, message)
        self.assertEqual(len(self.FakeKeyboard.created), 1)
        self.assertEqual(len(self.FakeMouse.created), 0)
        self.capture._keyboard.on_press(SimpleNamespace(char="a"))
        self.assertEqual(self.keys, ["A"])
        self.capture.stop()
        self.assertTrue(self.FakeKeyboard.created[0].stopped)

    def test_click_callback_has_no_text_or_persistence(self):
        ok, message = self.capture.start(keys=False, clicks=True)
        self.assertTrue(ok, message)
        self.capture._mouse.on_click(
            12, 34, SimpleNamespace(name="left"), True)
        self.assertEqual(self.clicks, [("Left click", 12, 34)])
        self.capture._mouse.on_click(
            12, 34, SimpleNamespace(name="left"), False)
        self.assertEqual(len(self.clicks), 1)
        self.assertNotIn("text", self.capture.snapshot())

    def test_key_formatting_never_builds_a_sequence(self):
        self.assertEqual(lab.format_key(SimpleNamespace(char="q")), "Q")
        self.assertEqual(lab.format_key(SimpleNamespace(char=" ")), "Space")
        self.assertEqual(lab.format_key(
            SimpleNamespace(name="ctrl_l")), "Ctrl")
        self.assertEqual(lab.format_key(
            SimpleNamespace(name="f8")), "F8")


class PrivacyTests(unittest.TestCase):
    def test_timeline_is_direction_metadata_only(self):
        event = lab.timeline_entry("down", 30, "LEFT", "LEFT")
        self.assertEqual(event["key"], "A")
        self.assertEqual(event["direction"], "LEFT")
        self.assertNotIn("text", event)
        self.assertNotIn("typed", event)


class PipelineTests(unittest.TestCase):
    class UInput:
        def __init__(self):
            self.events = []

        def write(self, etype, code, value):
            self.events.append((etype, int(code), int(value)))

        def syn(self):
            pass

        def close(self):
            pass

    class Device:
        fd = 99

        def close(self):
            pass

        def ungrab(self):
            pass

    def pipeline(self, profile=None):
        pipe = engine.SocdPipeline(
            evdev=object(), access_check=None, remap=profile,
            timeline=True, rescan=999, tick=0.01)
        pipe._out = self.UInput()
        return pipe

    def event(self, code, value=1):
        return SimpleNamespace(type=lab.EV_KEY, code=code, value=value)

    def test_direction_remap_and_timeline(self):
        pipe = self.pipeline({"name": "x", "mappings": [
            {"source": "A", "target": "B"}]})
        pipe._handle("kbd", self.event(30, 1))
        pipe._handle("kbd", self.event(32, 1))
        pipe._handle("kbd", self.event(30, 0))
        codes = [(code, value) for _etype, code, value in pipe._out.events]
        self.assertIn((48, 1), codes)
        self.assertIn((48, 0), codes)
        timeline = pipe.snapshot()["timeline"]
        self.assertEqual([item["key"] for item in timeline], ["A", "D", "A"])
        self.assertTrue(all("text" not in item for item in timeline))

    def test_non_direction_remap_releases_on_reset_and_unplug(self):
        pipe = self.pipeline({"name": "x", "mappings": [
            {"source": "Space", "target": "Enter"}]})
        pipe._handle("kbd", self.event(57, 1))
        self.assertEqual(pipe._out.events[-1], (lab.EV_KEY, 28, 1))
        pipe.release_now()
        self.assertEqual(pipe._out.events[-1], (lab.EV_KEY, 28, 0))
        pipe._handle("kbd", self.event(57, 1))
        pipe._devices["kbd"] = self.Device()
        pipe._drop("kbd")
        self.assertEqual(pipe._out.events[-1], (lab.EV_KEY, 28, 0))
        self.assertEqual(pipe._remap.down_codes(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
