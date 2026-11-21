#!/usr/bin/env python3
"""Regression and stress checks for the hardened auto-clicker.

Run with:  python3 test_clicker_hardening.py
No display is required; every click goes to an injected fake sink.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)
import wxb_engine as engine
import wxb_studio_service as svc

print(wxb_test_isolation.assert_isolated("clicker-hardening"))


class Sink:
    """A pointer sink that can count, move, and fail on demand."""

    def __init__(self):
        self.clicks = 0
        self.pressed = False
        self.press_at = 0.0
        self.hold_times = []
        self.buttons = []
        self.moves = []
        self.pos = (100, 100)
        self.fail_press = None
        self.fail_release = None
        self.fail_position = None
        self.release_delay = 0.0
        self._lock = threading.Lock()

    def press_mouse(self, button):
        if self.fail_press:
            raise self.fail_press
        with self._lock:
            self.pressed = True
            self.press_at = time.monotonic()
            self.buttons.append(button)

    def release_mouse(self, button=None):
        if self.fail_release:
            raise self.fail_release
        with self._lock:
            if self.pressed and button is not None:
                self.clicks += 1
                self.hold_times.append(time.monotonic() - self.press_at)
            self.pressed = False

    def pointer_position(self):
        if self.fail_position:
            raise self.fail_position
        return self.pos

    def move_pointer(self, x, y):
        self.moves.append((x, y))
        self.pos = (x, y)

    def release_all(self):
        if self.release_delay:
            time.sleep(self.release_delay)
        self.release_mouse()


class ClickerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.sink = Sink()
        self.service = svc.StudioService(path=self.path,
                                         sink_factory=lambda: self.sink)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def wait_for(self, predicate, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False


class CharacterizationTests(ClickerTestCase):
    """These pin today's public clicker contract before the hardening."""

    def test_existing_cps_duty_toggle_contract(self):
        cfg, error = svc.validate_clicker(80, 50, "left")
        self.assertFalse(error)
        for key in ("cps", "duty", "button"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["cps"], 80.0)
        self.assertEqual(cfg["duty"], 50.0)

        ok, message = self.service.start_clicker(80, 50, "left")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: self.sink.clicks >= 2))
        self.assertTrue(self.service.ac_running)
        self.service.toggle_clicker()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.sink.pressed)
        self.assertGreaterEqual(self.sink.clicks, 2)
        # Mode remains a settings concern, not a click-loop concern.
        self.assertEqual(self.service.settings["clicker"]["mode"], "toggle")

    def test_existing_stop_all_releases_the_pointer(self):
        self.assertTrue(self.service.start_clicker(100, 50, "left")[0])
        self.assertTrue(self.wait_for(lambda: self.sink.clicks > 0))
        self.service.stop_all()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.sink.pressed)


class NewClickerFeatureTests(ClickerTestCase):
    def test_range_delay_stop_count_and_middle_button(self):
        started = time.monotonic()
        done = engine.click_burst(
            100, 50, "middle", threading.Event(), cps_max=200, clicks=6,
            sink=self.sink, start_delay_ms=80,
            timing_precision="precise", failsafe_corner="off")
        self.assertEqual(done, 6)
        self.assertGreaterEqual(time.monotonic() - started, 0.08)
        self.assertEqual(self.sink.buttons, ["middle"] * 6)
        self.assertFalse(self.sink.pressed)

    def test_service_uses_range_count_and_stops(self):
        self.service.settings["clicker"].update({
            "rate_mode": "range", "cps_max": 150, "cps": 100,
            "start_delay_ms": 10, "stop_after_clicks": 8,
            "timing_precision": "precise",
            "failsafe": False,
        })
        ok, message = self.service.start_clicker()
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: not self.service.ac_running))
        self.assertEqual(self.sink.clicks, 8)
        snap = self.service.snapshot()["clicker"]
        self.assertEqual(snap["clicks"], 8)

    def test_app_filter_blocks_blacklisted_foreground(self):
        self.service.settings["clicker"].update({
            "cps": 100, "duty": 50, "app_filter_mode": "blacklist",
            "app_blacklist": ["sober"], "failsafe": False,
        })
        with mock.patch.object(svc, "foreground_app_name",
                               return_value="sober"):
            ok, message = self.service.start_clicker()
            self.assertTrue(ok, message)
            self.assertTrue(self.wait_for(
                lambda: self.service.snapshot()["clicker"]["blocked"]))
            self.assertEqual(self.sink.clicks, 0)
            self.service.stop_clicker()

    def test_mouse_hotkey_is_normalized_and_runs_hold_mode(self):
        self.assertEqual(svc.normalize_hotkey("Mouse4"), "mouse4")
        self.assertEqual(svc.pynput_hotkey("mouse4"), "")
        self.service.settings["clicker"]["mode"] = "hold"
        with mock.patch.object(self.service, "start_clicker",
                               return_value=(True, "")) as start, \
                mock.patch.object(self.service, "stop_clicker") as stop:
            self.service._mouse_hotkey_event("toggle_clicker", "mouse4", True)
            start.assert_called_once_with()
            self.assertIn("toggle_clicker", self.service._hold_actions)
            self.service._mouse_hotkey_event("toggle_clicker", "mouse4", False)
            stop.assert_called_once_with()


class SpamAndValidationTests(ClickerTestCase):
    def test_hotkey_spam_and_toggle_spam_never_duplicate_workers(self):
        for _ in range(24):
            self.service.handle_hotkey("f6")
        self.assertFalse(self.service.ac_running)
        for _ in range(24):
            self.service.toggle_clicker(200, 50, "left")
        self.assertFalse(self.service.ac_running)
        self.assertFalse(any(t.name == "wxb-studio-clicks" and t.is_alive()
                             for t in threading.enumerate()))

    def test_invalid_input_is_rejected_without_mutating_good_settings(self):
        before = dict(self.service.settings["clicker"])
        for args in ((0, 50, "left"), (1001, 50, "left"),
                     (10, 0, "left"), (10, 100, "left"),
                     (10, 50, "thumb")):
            ok, message = self.service.start_clicker(*args)
            self.assertFalse(ok)
            self.assertTrue(message)
        for kwargs in ({"duration": -1}, {"duration": 999999},
                       {"pointer_mode": "magic"}, {"jitter": -1},
                       {"jitter": 9999}, {"fixed_x": 99999},
                       {"failsafe_corner": "middle"}):
            cfg, error = svc.validate_clicker(10, 50, "left", **kwargs)
            self.assertIsNone(cfg)
            self.assertTrue(error)
        self.assertEqual(self.service.settings["clicker"], before)

    def test_corrupt_settings_and_profiles_are_refused(self):
        self.path.write_text("{not json", encoding="utf-8")
        loaded = svc.load_settings(self.path)
        self.assertEqual(loaded["clicker"], svc.DEFAULT_SETTINGS["clicker"])

        ok, message = self.service.save_clicker_profile("desk")
        self.assertTrue(ok, message)
        target = self.service.clicker_profile_path("desk")
        self.assertTrue(target.is_file())
        self.assertEqual(target.parent.name, "autoclicker")
        old_cps = self.service.settings["clicker"]["cps"]
        self.service.settings["clicker"]["cps"] = 77
        self.assertTrue(self.service.load_clicker_profile("desk")[0])
        self.assertEqual(self.service.settings["clicker"]["cps"], old_cps)

        target.write_text("", encoding="utf-8")
        ok, message = self.service.load_clicker_profile("desk")
        self.assertFalse(ok)
        self.assertIn("Could not read", message)
        target.write_text("{}", encoding="utf-8")
        ok, message = self.service.load_clicker_profile("desk")
        self.assertFalse(ok)
        self.assertIn("empty or invalid", message)
        self.assertFalse(self.service.save_clicker_profile("../escape")[0])

    def test_profile_and_lock_permissions_are_private(self):
        ok, message = self.service.save_clicker_profile("private")
        self.assertTrue(ok, message)
        profile = self.service.clicker_profile_path("private")
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(profile.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.service.start_clicker(20, 50, "left")[0])
        lock = self.path.parent / "clicker.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.service.stop_clicker()

    def test_symlinked_profile_is_not_read_or_listed(self):
        self.service.clicker_profile_dir().mkdir(parents=True, exist_ok=True)
        real = self.service.clicker_profile_dir() / "real.json"
        real.write_text('{"clicker": {"cps": 33}}', encoding="utf-8")
        link = self.service.clicker_profile_path("link")
        os.symlink(real, link)
        ok, message = self.service.load_clicker_profile("link")
        self.assertFalse(ok)
        self.assertIn("symlink", message)
        self.assertNotIn("link", self.service.list_clicker_profiles())


class FailurePathTests(ClickerTestCase):
    def test_release_failure_is_reported(self):
        self.sink.fail_release = RuntimeError("release denied")
        ok, message = self.service.start_clicker(50, 50, "left")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: not self.service.ac_running))
        snap = self.service.snapshot()["clicker"]
        self.assertIn("release", snap["error"].lower())

    def test_input_sink_release_raises_instead_of_swallowing(self):
        class BadMouse:
            def release(self, _button):
                raise RuntimeError("permission denied")

        sink = object.__new__(engine.InputSink)
        sink._mouse = BadMouse()
        with self.assertRaises(RuntimeError) as raised:
            sink.release_mouse("left")
        self.assertIn("permission denied", str(raised.exception))

    def test_input_sink_key_press_failure_raises(self):
        class BadKeyboard:
            def press(self, _code):
                raise RuntimeError("keyboard denied")

        sink = object.__new__(engine.InputSink)
        sink._kbd = BadKeyboard()
        sink._held_keys = set()
        with self.assertRaises(RuntimeError) as raised:
            sink.tap_key("1")
        self.assertIn("keyboard denied", str(raised.exception))

    def test_rotation_tap_failure_is_reported_and_releases(self):
        class BadKeyboard:
            def __init__(self):
                self.releases = 0

            def tap_key(self, _token):
                raise RuntimeError("keyboard denied")

            def release_keys(self):
                self.releases += 1

        cfg, error = engine.build_rotation(1, 2, 10, 10, "toggle", 1)
        self.assertIsNone(error)
        sink = BadKeyboard()
        with self.assertRaises(RuntimeError) as raised:
            engine.run_rotation(cfg, threading.Event(), sink=sink)
        self.assertIn("keyboard denied", str(raised.exception))
        self.assertEqual(sink.releases, 1)

    def test_stop_release_failure_is_reported(self):
        self.assertTrue(self.service.start_clicker(50, 50, "left")[0])
        self.assertTrue(self.wait_for(lambda: self.service.click_count() >= 1))
        self.sink.fail_release = RuntimeError("release blocked")
        self.service.stop_clicker(wait=True)
        snap = self.service.snapshot()["clicker"]
        self.assertIn("release", snap["error"].lower())

    def test_hotkey_release_failure_is_reported(self):
        self.service._hold_actions.add("toggle_clicker")
        self.service._hotkey_combo_keys["toggle_clicker"] = {"F6"}
        original = self.service.stop_clicker

        def broken_stop():
            raise RuntimeError("hotkey release blocked")

        self.service.stop_clicker = broken_stop
        try:
            self.service._hotkey_released("F6")
        finally:
            self.service.stop_clicker = original
        snap = self.service.snapshot()["clicker"]
        self.assertIn("hotkey release failed", snap["error"].lower())

    def test_permission_loss_is_reported_and_button_released(self):
        self.sink.fail_press = PermissionError("permission denied by compositor")
        ok, message = self.service.start_clicker(50, 50, "left")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: not self.service.ac_running))
        snap = self.service.snapshot()["clicker"]
        self.assertFalse(snap["running"])
        self.assertIn("could not press", snap["error"].lower())
        self.assertIn("permission", snap["error"].lower())
        self.assertFalse(self.sink.pressed)

    def test_display_disconnect_is_reported_and_released(self):
        self.sink.fail_position = RuntimeError("X display disconnected")
        with mock.patch.object(engine, "screen_bounds", return_value=(1000, 800)):
            ok, message = self.service.start_clicker(50, 50, "left")
            self.assertTrue(ok, message)
            self.assertTrue(self.wait_for(lambda: not self.service.ac_running))
        snap = self.service.snapshot()["clicker"]
        self.assertIn("stopped reporting", snap["error"].lower())
        self.assertFalse(self.sink.pressed)

    def test_failsafe_corner_stops_before_clicking(self):
        self.sink.pos = (0, 0)
        with mock.patch.object(engine, "screen_bounds", return_value=(1000, 800)):
            ok, message = self.service.start_clicker(100, 50, "left")
            self.assertTrue(ok, message)
            self.assertTrue(self.wait_for(lambda: not self.service.ac_running))
        snap = self.service.snapshot()["clicker"]
        self.assertIn("failsafe", snap["error"].lower())
        self.assertEqual(self.sink.clicks, 0)

    def test_stop_clicker_is_nonblocking_for_the_ui(self):
        self.sink.release_delay = 0.4
        self.assertTrue(self.service.start_clicker(1000, 50, "left")[0])
        self.assertTrue(self.wait_for(lambda: self.sink.clicks > 0))
        started = time.monotonic()
        self.service.stop_clicker(wait=False)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertTrue(self.wait_for(lambda: not self.service.ac_running, 1.5))
        self.assertFalse(self.sink.pressed)

    def test_shutdown_then_relaunch_starts_idle_with_saved_settings(self):
        self.service.settings["clicker"]["cps"] = 44
        self.service.save()
        self.assertTrue(self.service.start_clicker(44, 50, "left")[0])
        self.service.shutdown()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.sink.pressed)
        relaunched = svc.StudioService(path=self.path,
                                       sink_factory=lambda: self.sink)
        self.addCleanup(relaunched.shutdown)
        self.assertFalse(relaunched.ac_running)
        self.assertEqual(relaunched.settings["clicker"]["cps"], 44.0)

    def test_single_clicker_lock_across_service_instances(self):
        second = svc.StudioService(path=self.path, sink_factory=Sink)
        self.addCleanup(second.shutdown)
        self.assertTrue(self.service.start_clicker(20, 50, "left")[0])
        ok, message = second.start_clicker(20, 50, "left")
        self.assertFalse(ok)
        self.assertIn("already clicking", message.lower())
        self.service.stop_clicker()
        ok, message = second.start_clicker(20, 50, "left")
        self.assertTrue(ok, message)
        second.stop_clicker()

    def test_killed_process_releases_clicker_lock_on_relaunch(self):
        project = str(Path(__file__).resolve().parent)
        config = str(self.path)
        child_code = """
import sys, time
sys.path.insert(0, %r)
import wxb_studio_service as svc
class Sink:
    def __init__(self): self.clicks = 0; self.pressed = False
    def press_mouse(self, button): self.pressed = True
    def release_mouse(self, button=None):
        if self.pressed and button is not None: self.clicks += 1
        self.pressed = False
    def release_all(self): self.release_mouse()
service = svc.StudioService(path=%r, sink_factory=Sink)
ok, message = service.start_clicker(20, 50, "left")
print("READY" if ok else "FAIL " + message, flush=True)
time.sleep(10)
""" % (project, config)
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        proc = subprocess.Popen([sys.executable, "-c", child_code],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                env=child_env)
        try:
            self.assertIn("READY", proc.stdout.readline().strip())
            proc.kill()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
        relaunched = svc.StudioService(path=self.path, sink_factory=Sink)
        self.addCleanup(relaunched.shutdown)
        ok, message = relaunched.start_clicker(20, 50, "left")
        self.assertTrue(ok, message)
        relaunched.stop_clicker()


class FeatureTests(ClickerTestCase):
    def test_duration_fixed_position_and_jitter(self):
        self.service.settings["clicker"].update(
            {"duration": 0.12, "pointer_mode": "fixed",
             "fixed_x": 42, "fixed_y": 43, "failsafe": False})
        ok, message = self.service.start_clicker(100, 50, "left")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: not self.service.ac_running, 2.0))
        self.assertGreater(self.sink.clicks, 0)
        self.assertTrue(self.sink.moves)
        self.assertTrue(all(move == (42, 43) for move in self.sink.moves))

        self.sink.moves.clear()
        stop = threading.Event()
        done = engine.click_burst(
            100, 50, "left", stop, duration_ms=80, sink=self.sink,
            pointer_mode="jitter", fixed_position=(200, 210), jitter=5,
            failsafe_corner="off")
        self.assertGreater(done, 0)
        self.assertTrue(self.sink.moves)
        for x, y in self.sink.moves:
            self.assertLessEqual(abs(x - 200), 5)
            self.assertLessEqual(abs(y - 210), 5)

    def test_session_lock_pauses_then_resumes(self):
        locked = threading.Event()
        locked.set()
        sink = Sink()
        stop = threading.Event()
        errors = []
        worker = threading.Thread(
            target=lambda: engine.click_burst(
                100, 50, "left", stop, sink=sink, on_error=errors.append,
                failsafe_corner="off", session_guard=locked.is_set),
            daemon=True)
        worker.start()
        time.sleep(0.15)
        self.assertEqual(sink.clicks, 0)
        locked.clear()
        self.assertTrue(self._wait(lambda: sink.clicks > 0, 1.0))
        stop.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_sleep_gap_stops_instead_of_clicking_into_a_resumed_desktop(self):
        sink = Sink()
        errors = []
        stop = threading.Event()
        # last_wall, first loop, then a wall-clock jump of 20 seconds.
        with mock.patch("wxb_engine.time.time",
                        side_effect=[1000.0, 1000.1, 1020.0]):
            done = engine.click_burst(100, 50, "left", stop, sink=sink,
                                      on_error=errors.append,
                                      failsafe_corner="off")
        self.assertEqual(done, 1)
        self.assertTrue(errors)
        self.assertIn("sleep", errors[0].lower())
        self.assertFalse(sink.pressed)

    @staticmethod
    def _wait(predicate, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_f8_is_a_panic_stop_only_while_clicking(self):
        self.assertTrue(self.service.start_clicker(100, 50, "left")[0])
        ok, message = self.service.handle_hotkey("f8")
        self.assertTrue(ok)
        self.assertIn("panic", message.lower())
        self.assertFalse(self.service.ac_running)
        # With the clicker idle, F8 keeps its original SOCD meaning.
        ok, message = self.service.handle_hotkey("f8")
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(message, str)

    def test_10k_click_stress(self):
        sink = Sink()
        stop = threading.Event()
        started = time.monotonic()
        done = engine.click_burst(1000, 50, "left", stop, clicks=10000,
                                  sink=sink, failsafe_corner="off")
        elapsed = time.monotonic() - started
        self.assertEqual(done, 10000)
        self.assertEqual(sink.clicks, 10000)
        self.assertFalse(sink.pressed)
        self.assertLess(elapsed, 15.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
