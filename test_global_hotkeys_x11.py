#!/usr/bin/env python3
"""Real X11 proof for the global hotkeys.

This is intentionally separate from the headless suite: pynput's global
listener and xdotool both require a live X11 display.  It uses unique
Ctrl+Shift+F6..F9 bindings so it cannot toggle another WXB instance's
default F6..F9 bindings.

Run under Xvfb or a live X11 display with xdotool installed:
  timeout --kill-after=10s 120s env GDK_DEBUG=gl-disable \
    GSK_RENDERER=cairo xvfb-run -a python3 test_global_hotkeys_x11.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import wxb_test_isolation  # noqa: E402,F401
from wxb_studio_service import StudioService

print(wxb_test_isolation.assert_isolated("global-hotkeys"))


class FakeSink:
    def __init__(self):
        self.clicks = 0
        self.pressed = False

    def press_mouse(self, button):
        self.pressed = True

    def release_mouse(self, button=None):
        if self.pressed and button is not None:
            self.clicks += 1
        self.pressed = False

    def tap_key(self, token):
        return True

    def release_keys(self):
        pass

    def release_all(self):
        self.release_mouse(None)


def wait_for(predicate, timeout=2.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"),
                     "needs a live X11 DISPLAY and xdotool")
class RealX11HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = StudioService(
            path=Path(self.tmp.name) / "hotkeys.json",
            sink_factory=FakeSink,
        )
        self.service.settings["clicker"].update(
            {"cps": 5, "duty": 20, "button": "left", "mode": "toggle"})
        self.service.settings["rotation"].update(
            {"delay_a": 20, "delay_b": 20, "max_rot": 0, "mode": "toggle"})
        self.service.settings["hotkeys"] = {
            "toggle_clicker": "ctrl+shift+f6",
            "toggle_rotation": "ctrl+shift+f7",
            "toggle_socd": "ctrl+shift+f8",
            "stop_all": "ctrl+shift+f9",
        }
        self.service.settings["macro_enabled"] = True
        self.service.save()
        ok, message = self.service.start_hotkeys()
        self.assertTrue(ok, message)
        # start_hotkeys() must not report success until XRecord is ready.
        # If this flag is false here, the product's readiness guarantee has
        # regressed and an immediately following chord would be lost.
        listener = self.service._hotkeys
        self.assertIsNotNone(listener)
        self.assertTrue(getattr(listener, "_ready", False),
                        "start_hotkeys returned before XRecord was ready")

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def key(self, combo):
        result = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", combo],
            capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0,
                         result.stderr or result.stdout)

    def test_hold_mode_stops_when_the_combo_is_released(self):
        self.service.settings["clicker"]["mode"] = "hold"
        self.service.save()
        down = subprocess.run(["xdotool", "keydown", "ctrl+shift+F6"],
                              capture_output=True, text=True, timeout=8)
        self.assertEqual(down.returncode, 0, down.stderr or down.stdout)
        self.assertTrue(wait_for(lambda: self.service.ac_running),
                        "holding Ctrl+Shift+F6 did not start the clicker")
        up = subprocess.run(["xdotool", "keyup", "ctrl+shift+F6"],
                            capture_output=True, text=True, timeout=8)
        self.assertEqual(up.returncode, 0, up.stderr or up.stdout)
        self.assertTrue(wait_for(lambda: not self.service.ac_running),
                        "releasing the combo did not stop the clicker")

    def test_reserved_escape_stops_the_clicker_globally(self):
        self.key("ctrl+shift+F6")
        self.assertTrue(wait_for(lambda: self.service.ac_running),
                        "Ctrl+Shift+F6 did not start the clicker")
        self.key("Escape")
        self.assertTrue(wait_for(lambda: not self.service.ac_running),
                        "Escape did not stop the clicker")

    def test_mouse4_and_mouse5_global_hotkeys_start_the_clicker(self):
        for binding, x_button in (("mouse4", "8"), ("mouse5", "9")):
            self.service.stop_hotkeys()
            self.service.settings["hotkeys"]["toggle_clicker"] = binding
            self.service.save()
            ok, message = self.service.start_hotkeys()
            self.assertTrue(ok, message)
            result = subprocess.run(["xdotool", "click", x_button],
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0,
                             result.stderr or result.stdout)
            self.assertTrue(wait_for(lambda: self.service.ac_running),
                            "%s did not start the clicker" % binding)
            self.service.stop_clicker()
            self.assertTrue(wait_for(lambda: not self.service.ac_running))

    def test_global_bindings_are_delivered_without_focus(self):
        # The terminal/browser keeps focus; the listener must still see these.
        self.key("ctrl+shift+F6")
        self.assertTrue(wait_for(lambda: self.service.ac_running),
                        "Ctrl+Shift+F6 did not start the clicker")
        self.key("ctrl+shift+F6")
        self.assertTrue(wait_for(lambda: not self.service.ac_running),
                        "Ctrl+Shift+F6 did not stop the clicker")

        self.key("ctrl+shift+F7")
        self.assertTrue(wait_for(lambda: self.service.rot_running),
                        "Ctrl+Shift+F7 did not start the rotation")
        self.key("ctrl+shift+F7")
        self.assertTrue(wait_for(lambda: not self.service.rot_running),
                        "Ctrl+Shift+F7 did not stop the rotation")

        calls = []
        self.service.toggle_socd = lambda: (calls.append("socd") or
                                            (True, "test socd"))
        self.key("ctrl+shift+F8")
        self.assertTrue(wait_for(lambda: bool(calls)),
                        "Ctrl+Shift+F8 did not dispatch the SOCD action")

        self.key("ctrl+shift+F6")
        self.assertTrue(wait_for(lambda: self.service.ac_running),
                        "Ctrl+Shift+F6 did not start the clicker before stop-all")
        self.key("ctrl+shift+F9")
        self.assertTrue(wait_for(lambda: not self.service.ac_running),
                        "Ctrl+Shift+F9 did not stop the clicker")


if __name__ == "__main__":
    unittest.main(
        testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
