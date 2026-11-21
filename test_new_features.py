#!/usr/bin/env python3
"""End-to-end checks for the new Studio clicker features.

Run under a display:

    xvfb-run -a python3 test_new_features.py

The service tests use a fake pointer sink; the UI tests build a real GTK
window and re-open its persisted config in a second window.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WXB_STANDALONE", "1")
os.environ.setdefault("GDK_DEBUG", "gl-disable")
os.environ.setdefault("GSK_RENDERER", "cairo")

import wxb_test_isolation  # noqa: E402,F401

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Gtk  # noqa: E402

import wxb_studio as studio  # noqa: E402
import wxb_studio_service as svc  # noqa: E402
from wxb_studio import WxbApplication, WxbWindow  # noqa: E402
from wxb_studio_service import StudioService  # noqa: E402

print(wxb_test_isolation.assert_isolated("new-features"))


def pump(seconds: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(0.02)
    return False


class RecordingSink:
    def __init__(self):
        self.pressed = False
        self.pressed_button = None
        self.clicks = 0
        self.click_times = []
        self.buttons = []

    def press_mouse(self, button):
        self.pressed = True
        self.pressed_button = button

    def release_mouse(self, button=None):
        if self.pressed and button is not None:
            self.clicks += 1
            self.click_times.append(time.monotonic())
            self.buttons.append(button)
        self.pressed = False

    def pointer_position(self):
        return 100, 100

    def move_pointer(self, _x, _y):
        pass

    def tap_key(self, _token):
        return True

    def release_keys(self):
        pass

    def release_all(self):
        self.pressed = False


class ServiceFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = RecordingSink()
        self.service = StudioService(
            path=Path(self.tmp.name) / "studio.json",
            sink_factory=lambda: self.sink,
        )
        self.service.settings["clicker"].update({
            "cps": 60, "duty": 20, "failsafe": False,
            "app_filter_mode": "off",
        })

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_delay_cap_and_middle_button(self):
        self.service.settings["clicker"].update({
            "cps": 80, "duty": 10, "button": "middle",
            "rate_mode": "exact", "cps_max": 80,
            "start_delay_ms": 300, "stop_after_clicks": 10,
        })
        started = time.monotonic()
        ok, message = self.service.start_clicker()
        self.assertTrue(ok, message)
        self.assertTrue(wait_for(lambda: not self.service.ac_running))
        self.assertEqual(self.sink.clicks, 10)
        self.assertEqual(self.sink.buttons, ["middle"] * 10)
        self.assertGreaterEqual(self.sink.click_times[0] - started, 0.26)

    def test_app_filter_allows_whitelisted_foreground(self):
        self.service.settings["clicker"].update({
            "app_filter_mode": "whitelist",
            "app_whitelist": ["sober"],
            "cps": 100, "duty": 10, "stop_after_clicks": 3,
        })
        with mock.patch.object(svc, "foreground_app_name",
                               return_value="sober"):
            ok, message = self.service.start_clicker()
            self.assertTrue(ok, message)
            self.assertTrue(wait_for(lambda: not self.service.ac_running))
        self.assertEqual(self.sink.clicks, 3)

    def test_app_filter_blocks_blacklisted_foreground(self):
        self.service.settings["clicker"].update({
            "app_filter_mode": "blacklist",
            "app_blacklist": ["sober"],
            "cps": 100, "duty": 10,
        })
        with mock.patch.object(svc, "foreground_app_name",
                               return_value="sober"):
            ok, message = self.service.start_clicker()
            self.assertTrue(ok, message)
            self.assertTrue(wait_for(
                lambda: self.service.snapshot()["clicker"]["blocked"]))
            self.assertEqual(self.sink.clicks, 0)
            self.service.stop_clicker()

    def test_missing_foreground_detection_is_an_explicit_start_error(self):
        self.service.settings["clicker"].update({
            "app_filter_mode": "blacklist", "app_blacklist": ["sober"]})
        with mock.patch.object(svc, "foreground_app_name", return_value=""):
            ok, message = self.service.start_clicker()
        self.assertFalse(ok)
        self.assertIn("foreground", message.lower())
        self.assertFalse(self.service.ac_running)

    def test_portable_first_launch_migrates_normal_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            config = root / "config" / "wxb" / "studio.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                '{"accent": "sage", "clicker": {"cps": 33}, '
                '"window": {"x": 44, "y": 55}}', encoding="utf-8")
            env = {"WXB_PORTABLE": "1", "XDG_CONFIG_HOME": str(root / "config")}
            with mock.patch.object(svc, "APP_DIR", app_dir), \
                    mock.patch.dict(os.environ, env):
                target = app_dir / "wxb-studio-data" / "studio.json"
                self.assertFalse(target.exists())
                loaded = svc.load_settings()
                self.assertEqual(loaded["accent"], "sage")
                self.assertEqual(loaded["clicker"]["cps"], 33)
                self.assertTrue(target.is_file())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                service = StudioService(path=target)
                try:
                    self.assertEqual(service.settings["accent"], "sage")
                    self.assertEqual(service.settings["window"]["y"], 55)
                finally:
                    service.shutdown()
                status = svc.portable_status()
                self.assertTrue(status["enabled"])
                self.assertEqual(status["warning"], "")

    def test_failed_portable_copy_reports_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config" / "wxb" / "studio.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"clicker": {"cps": 33}}', encoding="utf-8")
            env = {"WXB_PORTABLE": "1", "XDG_CONFIG_HOME": str(root / "config")}
            with mock.patch.object(svc, "APP_DIR", root / "app"), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(svc, "_migrate_portable_settings",
                                      return_value=False):
                svc.load_settings()
                status = svc.portable_status()
                self.assertTrue(status["enabled"])
                self.assertIn("could not copy", status["warning"])


def walk_widgets(widget):
    yield widget
    child = widget.get_first_child() if hasattr(widget, "get_first_child") \
        else None
    while child is not None:
        yield from walk_widgets(child)
        child = child.get_next_sibling()


def button_labels(root):
    return {widget.get_label() for widget in walk_widgets(root)
            if isinstance(widget, Gtk.Button)}


class UiFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = WxbApplication(
            config_path=Path(cls.tmp.name) / "studio.json",
            show_onboarding=False, auto_scan=False, hotkeys=False)
        cls.app.register(None)
        cls.app.activate()
        pump(0.3)
        cls.win = cls.app.window
        assert cls.win is not None, "activate did not build a window"

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.app.quit()
        cls.tmp.cleanup()

    def test_every_page_renders(self):
        for key, _label, _icon, _slot in WxbWindow.PAGES:
            self.win.show_page(key)
            pump(0.08)
            self.assertEqual(self.win._page_key, key)
            scroll = self.win._page_scrolls.get(key)
            self.assertIsNotNone(scroll, key)
            self.assertTrue(scroll.get_mapped(), key)

    def test_advanced_and_hotkey_dialog_render(self):
        window = self.win
        window.show_page("automation")
        window.ui["ac_advanced"].set_expanded(True)
        pump(0.15)
        for key in ("ac_cps_max", "ac_start_delay", "ac_stop_clicks",
                    "ac_precision", "ac_failsafe_margin",
                    "ac_filter_mode", "ac_blacklist", "ac_whitelist"):
            self.assertTrue(window.ui[key].get_visible(), key)

        for action, label in (("toggle_clicker", "Mouse 4"),
                              ("toggle_rotation", "Mouse 5"),
                              ("toggle_socd", "Middle")):
            window._start_hotkey_capture(action)
            pump(0.1)
            capture = window._capture_window
            self.assertIsNotNone(capture)
            labels = button_labels(capture.get_content())
            self.assertIn("Mouse 4", labels)
            self.assertIn("Mouse 5", labels)
            self.assertIn(label, labels)
            chosen = next(
                widget for widget in walk_widgets(capture.get_content())
                if isinstance(widget, Gtk.Button)
                and widget.get_label() == label)
            chosen.emit("clicked")
            pump(0.1)
            self.assertIsNone(window._capture_window)
        self.assertEqual(window.service.settings["hotkeys"]["toggle_clicker"],
                         "mouse4")
        self.assertEqual(window.service.settings["hotkeys"]["toggle_rotation"],
                         "mouse5")
        self.assertEqual(window.service.settings["hotkeys"]["toggle_socd"],
                         "mousemiddle")

    def test_clicker_controls_survive_reload_and_reopen(self):
        window = self.win
        window.show_page("automation")
        window.ui["ac_cps"].set_value(42)
        window.ui["ac_duty"].set_value(30)
        window.ui["ac_button_choice"].set_selected(2)
        window.ui["ac_rate_mode"].set_selected(1)
        window.ui["ac_cps_max"].set_value(500)
        window.ui["ac_start_delay"].set_value(250)
        window.ui["ac_stop_clicks"].set_value(50)
        window.ui["ac_precision"].set_selected(2)
        window.ui["ac_failsafe_margin"].set_value(20)
        window.ui["ac_filter_mode"].set_selected(1)
        window.ui["ac_blacklist"].set_text("sober, discord")
        window.ui["ac_whitelist"].set_text("roblox")
        window.ui["always_on_top"].set_active(True)
        pump(0.05)
        window._sync_clicker_controls()
        window._sync_clicker_extras()
        window.service.save()
        expected = {
            "cps": 42.0, "duty": 30.0, "button": "middle",
            "rate_mode": "range", "cps_max": 500.0,
            "start_delay_ms": 250, "stop_after_clicks": 50,
            "timing_precision": "precise",
            "failsafe_margin": 20, "app_filter_mode": "blacklist",
            "app_blacklist": ["sober", "discord"],
            "app_whitelist": ["roblox"],
        }
        for key, value in expected.items():
            self.assertEqual(window.service.settings["clicker"][key], value,
                             key)

        reloaded = StudioService(path=window.service.path)
        second = WxbWindow(self.app, reloaded, startup_page="automation",
                           show_onboarding=False, auto_scan=False)
        try:
            second.present()
            pump(0.25)
            self.assertEqual(second.ui["ac_cps"].get_value(), 42.0)
            self.assertEqual(second.ui["ac_duty"].get_value(), 30.0)
            self.assertEqual(second.ui["ac_button_choice"].get_selected(), 2)
            self.assertEqual(second.ui["ac_rate_mode"].get_selected(), 1)
            self.assertEqual(second.ui["ac_cps_max"].get_value(), 500.0)
            self.assertEqual(second.ui["ac_start_delay"].get_value(), 250)
            self.assertEqual(second.ui["ac_stop_clicks"].get_value(), 50)
            self.assertEqual(second.ui["ac_precision"].get_selected(), 2)
            self.assertEqual(second.ui["ac_failsafe_margin"].get_value(), 20)
            self.assertEqual(second.ui["ac_filter_mode"].get_selected(), 1)
            self.assertEqual(second.ui["ac_blacklist"].get_text(),
                             "sober, discord")
            self.assertEqual(second.ui["ac_whitelist"].get_text(), "roblox")
            self.assertTrue(second.ui["always_on_top"].get_active())
        finally:
            second.close()
            pump(0.1)
        window.ui["always_on_top"].set_active(False)
        window.service.save()

    def test_capability_hints_degrade_visibly(self):
        window = self.win
        caps = {
            "display": False, "xlib": False, "xdotool": False,
            "xprop": False, "wmctrl": False, "foreground": False,
            "geometry": False, "above": False,
            "foreground_backend": "", "geometry_backend": "",
            "above_backend": "",
        }
        with mock.patch.object(studio, "x11_capabilities", return_value=caps), \
                mock.patch.object(window, "_display_is_x11", return_value=False):
            window._refresh_capability_hints()
            self.assertIn("unavailable", window.ui["ac_filter_hint"].get_text())
            self.assertFalse(window.ui["ac_blacklist"].get_sensitive())
            self.assertTrue(window.ui["ac_filter_mode"].get_sensitive())
            self.assertIn("does not expose X11",
                          window.ui["always_on_top_hint"].get_text())
            self.assertIn("not remembered",
                          window.ui["window_geometry_hint"].get_text())
            window.ui["always_on_top"].set_active(True)
            pump(0.02)
            self.assertFalse(window.ui["always_on_top"].get_active())
            self.assertFalse(window.service.settings["always_on_top"])
        window._refresh_capability_hints()
        self.assertTrue(window.ui["ac_blacklist"].get_sensitive())

    def test_portable_warning_is_visible_in_settings(self):
        warning = "Portable settings could not be copied."
        with mock.patch.object(studio, "portable_status", return_value={
                "enabled": True, "warning": warning}):
            self.win._refresh_capability_hints()
        self.assertIn(warning, self.win.ui["portable_hint"].get_text())

    def test_recorder_mp4_format_persists_from_the_ui(self):
        window = self.win
        window.show_page("recorder")
        pump(0.1)
        window.ui["rec_container"].set_selected(1)
        pump(0.1)
        self.assertEqual(
            window.service.settings["recorder"]["container"], "mp4")
        reloaded = StudioService(path=window.service.path)
        try:
            self.assertEqual(reloaded.settings["recorder"]["container"],
                             "mp4")
            snap = reloaded.recorder_snapshot()
            self.assertEqual(snap["settings"]["container"], "mp4")
            self.assertIn("MP4", snap["limit"])
        finally:
            reloaded.shutdown()
        window.ui["rec_container"].set_selected(0)
        pump(0.1)

    def test_window_geometry_and_above_use_xlib_without_tools(self):
        window = self.win
        xid = window._x11_window_id()
        self.assertIsNotNone(xid)
        # GTK may still have an allocation queued when this test starts and a
        # single XConfigureWindow can be overwritten by it.  The app restores
        # geometry 250 ms after map, once allocation has settled; do the same
        # here by reapplying until the server reports the requested frame.
        def configure(target):
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                studio.x11_configure_window(xid, *target)
                if studio.x11_window_geometry(xid) == target:
                    return True
                pump(0.05)
            return False

        self.assertTrue(configure((140, 150, 1110, 742)),
                        studio.x11_window_geometry(xid))
        with mock.patch.object(studio.shutil, "which", return_value=None):
            window._save_window_geometry()
            saved = window.service.settings["window"]
            self.assertEqual((saved["x"], saved["y"]), (140, 150))
            self.assertEqual((saved["width"], saved["height"]),
                             (1110, 742))
            self.assertTrue(window._apply_always_on_top(True))
            self.assertTrue(window._apply_always_on_top(False))

            window.service.settings["window"].update({
                "maximized": False, "x": 160, "y": 170,
                "width": 1110, "height": 742,
            })
            deadline = time.monotonic() + 3.0
            geometry = None
            while time.monotonic() < deadline:
                window._window_state_restored = False
                window._restore_window_state()
                geometry = studio.x11_window_geometry(xid)
                if geometry is not None and abs(geometry[0] - 160) <= 2 \
                        and abs(geometry[1] - 170) <= 2:
                    break
                pump(0.05)
            self.assertIsNotNone(geometry)
            self.assertLessEqual(abs(geometry[0] - 160), 2)
            self.assertLessEqual(abs(geometry[1] - 170), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
