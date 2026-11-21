#!/usr/bin/env python3
"""Headless smoke and interaction checks for the GTK studio.

Run under a display:  xvfb-run -a python3 test_studio_ui.py
"""

import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest import mock
from pathlib import Path

os.environ.setdefault("WXB_STANDALONE", "1")
# Keep the headless session free of the Xvfb DRI3/EGL warning; this is the
# software renderer, not a suppression inside the application.
os.environ.setdefault("GDK_DEBUG", "gl-disable")
os.environ.setdefault("GSK_RENDERER", "cairo")

import wxb_test_isolation  # noqa: E402,F401
import wxb_macros as macros  # noqa: E402

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk, Pango  # noqa: E402

import wxb_studio as studio  # noqa: E402
from wxb_studio import OnboardingWindow, WxbApplication, WxbWindow  # noqa: E402
from wxb_studio_service import (  # noqa: E402
    ALT_FPS_DEFAULT, StudioService, load_settings,
)

print(wxb_test_isolation.assert_isolated("ui"))


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

    def pointer_position(self):
        return 123, 456

    def move_pointer(self, x, y):
        self.position = (x, y)

    def release_keys(self):
        pass

    def release_all(self):
        self.release_mouse(None)


class FakeMacroSink(FakeSink):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.released = 0

    def move_pointer(self, x, y):
        self.calls.append(("move", x, y))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def key_down(self, key):
        self.calls.append(("keydown", key))

    def key_up(self, key):
        self.calls.append(("keyup", key))

    def release_all(self):
        self.released += 1


class FakeUiRecorder:
    """In-memory recorder so tab-switch tests never spawn FFmpeg."""

    def __init__(self, service):
        self.service = service
        self.recording = False
        self.paused = False
        self.started = 0
        self.stopped = []

    def capabilities(self):
        return {"session": "x11", "x11": True, "xwayland": False,
                "wayland": False, "display": True, "x11grab": True,
                "pulse": True, "backend": "ffmpeg/x11grab", "record": True,
                "play": True, "hotkeys": True,
                "sources": ["screen", "window", "region"],
                "audio": True, "encoders": ["libx264"],
                "hardware_encoders": [], "reason": ""}

    def snapshot(self):
        return {"available": True, "recording": self.recording,
                "starting": False, "paused": self.paused,
                "running": self.recording, "state": "recording" if self.recording else "idle",
                "elapsed": 1.0 if self.recording else 0.0, "output": "",
                "error": "", "notice": "", "probe": {},
                "settings": self.service.settings["recorder"],
                "capabilities": self.capabilities(),
                "audio_devices": {"microphones": [], "desktop": []}}

    def start(self):
        self.started += 1
        self.recording = True
        self.paused = False
        return True, "fake recording started"

    def stop(self, wait=False):
        self.stopped.append(wait)
        self.recording = False
        self.paused = False
        return True, "fake recording stopped"

    def pause(self):
        self.paused = True
        return True, "fake paused"

    def resume(self):
        self.paused = False
        return True, "fake resumed"


def pump(seconds=0.15):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)


def fake_gamemode_status(service, unavailable=()):
    """UI-suite detection stub; no Flatpak or gamemoded subprocess."""
    effects = service.launch_effects()
    return {key: {
        "available": key not in unavailable,
        "detail": ("test fixture" if key not in unavailable
                   else "Host %s is not installed" % key),
        "enabled": effects[key],
    } for key in service.GAMEMODE_KEYS}


def patch_gamemode_status(service, unavailable=()):
    return mock.patch.object(
        service, "gamemode_status",
        side_effect=lambda refresh=False:
        fake_gamemode_status(service, unavailable))


@contextmanager
def gamemode_page(window, unavailable=()):
    """Open GameMode with detection stubbed, then restore all page state."""
    previous = window.service.launch_effects()
    seen = window._sober_running_seen
    window._sober_running_seen = False
    with patch_gamemode_status(window.service, unavailable):
        try:
            window.show_page("gamemode")
            pump(0.08)
            yield
        finally:
            for key, enabled in previous.items():
                window.service.set_gamemode_effect(key, enabled)
            window._sober_running_seen = seen
            window._refresh_gamemode_page(running=False)
            window.show_page("overview")
            pump(0.05)


class StudioUiTests(unittest.TestCase):
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
        # The shared clicker test must not fire real pointer events at whatever
        # happens to be under the Xvfb cursor (it could press Launch Sober).
        cls.win.service._sink_factory = FakeSink

    @classmethod
    def tearDownClass(cls):
        if cls.win is not None:
            cls.win.close()
        cls.app.quit()
        cls.tmp.cleanup()

    def test_locks_socd_presets_and_opacity(self):
        self.win.show_page("socd")
        self.win.ui["ac_lock"].set_active(True)
        self.assertFalse(self.win.ui["ac_cps"].get_sensitive())
        self.win.ui["ac_lock"].set_active(False)
        self.assertTrue(self.win.ui["ac_cps"].get_sensitive())
        self.win._apply_socd_preset("Last win")
        self.assertEqual(self.win.service.settings["socd"]["mode_h"],
                         "Last win")
        self.assertEqual(self.win.service.settings["socd"]["mode_v"],
                         "Last win")
        self.win.show_page("settings")
        self.win.ui["opacity"].set_value(55)
        pump(0.02)
        self.assertEqual(self.win.service.settings["opacity"], 55)
        self.assertAlmostEqual(self.win.get_opacity(), 0.55, places=2)
        self.win.ui["opacity"].set_value(92)
        pump(0.02)

    def test_socd_horizontal_and_vertical_modes_can_differ(self):
        window = self.win
        window.show_page("socd")
        window._apply_socd_preset("True neutral")
        self.assertEqual(window.service.settings["socd"], {
            "mode_h": "Neutral", "mode_v": "Neutral"})
        modes = ["Neutral", "Last win", "First win", "Priority"]
        window.ui["socd_h"].set_selected(modes.index("Last win"))
        window.ui["socd_v"].set_selected(modes.index("Neutral"))
        window._sync_socd_controls()
        self.assertEqual(window.service.settings["socd"], {
            "mode_h": "Last win", "mode_v": "Neutral"})
        window.ui["socd_h"].set_selected(modes.index("Neutral"))
        window.ui["socd_v"].set_selected(modes.index("Last win"))
        window._sync_socd_controls()
        self.assertEqual(window.service.settings["socd"], {
            "mode_h": "Neutral", "mode_v": "Last win"})
        # Restore the shared test window's prior default behavior.
        window._apply_socd_preset("True neutral")
        self.assertEqual(window.service.settings["socd"], {
            "mode_h": "Neutral", "mode_v": "Neutral"})

    def test_input_lab_is_one_optional_expander_inside_socd(self):
        window = self.win
        window.show_page("socd")
        pump(0.03)
        self.assertEqual(sum(1 for page in window.PAGES
                             if page[0] == "socd"), 1)
        fold = window.ui["lab_fold"]
        self.assertIsInstance(fold, Gtk.Expander)
        title = fold.get_label_widget().get_first_child()
        self.assertEqual(title.get_text(), "Input Lab")
        self.assertFalse(fold.get_expanded())
        self.assertEqual(window.ui["lab_capture_badge"].get_text(),
                         "CAPTURE · OFF")
        self.assertTrue(window.ui["lab_capture_badge"].get_mapped())
        self.assertIn("competition", window.ui["lab_notice"].get_text())
        self.assertTrue(window.ui["lab_notice"].get_mapped())
        fold.set_expanded(True)
        pump(0.15)
        self.assertTrue(window.ui["lab_capture"].get_mapped())
        self.assertTrue(window.ui["lab_live"]["LEFT"][0].get_mapped())
        self.assertEqual(window.ui["lab_capture"].get_text(),
                         "CAPTURE · OFF")
        self.assertIn("remap", window.ui["lab_caps"].get_text().lower())
        self.assertTrue(window.ui["socd_start"].get_mapped())
        fold.set_expanded(False)

    def test_input_lab_profile_save_load_and_safe_reset(self):
        window = self.win
        window.show_page("socd")
        window.ui["lab_fold"].set_expanded(True)
        pump(0.08)
        name = "UI Input Lab Profile"
        window.ui["lab_profile_name"].set_text(name)
        window._lab_clear_mappings()
        window._lab_add_mapping()
        self.assertEqual(len(window._lab_mappings), 1)
        window._lab_save_profile()
        self.assertIn(name, window.service.list_input_lab_profiles())
        window._refresh_lab_profiles()
        self.assertIn(name, window._lab_profile_names)
        window.ui["lab_profile_choice"].set_selected(
            window._lab_profile_names.index(name))
        window._lab_load_profile()
        self.assertEqual(
            window.service.settings["input_lab"]["active_profile"], name)
        self.assertTrue(window.service.input_lab_snapshot()["profile"])
        window._lab_safe_reset()
        self.assertEqual(
            window.service.settings["input_lab"]["active_profile"], "")
        window._refresh_lab_profiles()
        if name in window._lab_profile_names:
            window.ui["lab_profile_choice"].set_selected(
                window._lab_profile_names.index(name))
            window._lab_delete_profile()
        self.assertNotIn(name, window.service.list_input_lab_profiles())
        window.ui["lab_fold"].set_expanded(False)

    def test_input_lab_visualizer_switches_are_independent(self):
        window = self.win
        fake = mock.Mock()
        fake.error = ""
        fake.click_through = True
        with mock.patch.object(studio, "InputOverlayWindow", return_value=fake):
            with mock.patch.object(
                    window.service, "set_input_lab_settings",
                    return_value=(True, "Input Lab settings saved.")) as setter:
                window.ui["lab_keys_switch"].set_active(True)
                pump(0.03)
                view = window.service.settings["input_lab"]["visualizer"]
                view["keys"] = True
                view["clicks"] = False
                window._sync_overlay()
                self.assertTrue(setter.called)
                fake.apply_settings.assert_called()
                fake.set_enabled.assert_called_with(True, False)
                window._show_visual_event("key", "A", 0, 0)
                fake.show_key.assert_called_with("A")
                window.ui["lab_clicks_switch"].set_active(True)
                pump(0.03)
                view["clicks"] = True
                window._sync_overlay()
                fake.set_enabled.assert_called_with(True, True)
                window._show_visual_event("click", "left", 10, 12)
                fake.show_click.assert_called_with("left", 10, 12)
        view["keys"] = False
        view["clicks"] = False
        window._sync_overlay()

    def test_overlay_class_bounds_appearance_without_capturing_text(self):
        overlay = studio.InputOverlayWindow()
        try:
            overlay.apply_settings({
                "keys": True, "clicks": True, "size": 999,
                "color": "not-a-colour", "opacity": 1})
            settings = overlay._settings
            self.assertEqual(settings["size"], 160)
            self.assertEqual(settings["opacity"], 20)
            self.assertEqual(settings["color"], "#9fcaff")
            self.assertFalse(overlay.get_visible())
            self.assertEqual(overlay._keycap.get_text(), "")
        finally:
            overlay.destroy()

    def test_window_and_pages_exist(self):
        self.win.show_page("overview")
        self.assertEqual(self.win.stack.get_visible_child_name(), "overview")
        with patch_gamemode_status(self.win.service):
            for key in ("overview", "automation", "rotation", "socd",
                        "macros", "recorder", "alts", "settings",
                        "gamemode"):
                self.win.show_page(key)
                pump(0.02)
                self.assertEqual(self.win.stack.get_visible_child_name(), key)
                self.assertTrue(self.win._nav[key].has_css_class("active"))
            self.win.show_page("overview")

    def test_gamemode_page_switches_are_persisted_and_conditional(self):
        window = self.win
        with gamemode_page(window):
            self.assertEqual(window.stack.get_visible_child_name(),
                             "gamemode")
            for key in ("mangohud", "gamemode", "gamescope"):
                widget = window.ui["gm_%s_switch" % key]
                self.assertTrue(widget.get_mapped(), key)
                self.assertEqual(widget.get_active(),
                                 window.service.launch_effects()[key])
            window._refresh_gamemode_page(running=False)
            for key in ("mangohud", "gamemode", "gamescope"):
                window.ui["gm_%s_switch" % key].set_active(True)
            self.assertEqual(window.service.launch_effects(), {
                "mangohud": True, "gamemode": True, "gamescope": True})
            self.assertIn("MangoHud", window.ui["gm_summary"].get_text())
            self.assertIn("Linux GameMode",
                          window.ui["gm_summary"].get_text())
            self.assertIn("Gamescope", window.ui["gm_summary"].get_text())
            for key in ("mangohud", "gamemode", "gamescope"):
                window.ui["gm_%s_switch" % key].set_active(False)
            self.assertEqual(window.service.launch_effects(), {
                "mangohud": False, "gamemode": False,
                "gamescope": False})
            self.assertIn("No launch effects",
                          window.ui["gm_summary"].get_text())

    def test_gamemode_saved_unavailable_switch_can_turn_off(self):
        window = self.win
        with gamemode_page(window, {"gamescope"}):
            window.service.set_gamemode_effect("gamescope", True)
            window._refresh_gamemode_page(running=False)
            switch = window.ui["gm_gamescope_switch"]
            self.assertTrue(switch.get_active())
            self.assertTrue(switch.get_sensitive())
            self.assertIn("Unavailable",
                          window.ui["gm_gamescope_state"].get_text())
            switch.set_active(False)
            self.assertFalse(window.service.launch_effects()["gamescope"])
            self.assertFalse(switch.get_sensitive())

    def test_recorder_open_folder_creates_and_opens_user_folder(self):
        window = self.win
        target = Path(self.tmp.name) / "recordings-open-test"
        window.show_page("recorder")
        window.ui["rec_dir"].set_text(str(target))
        with mock.patch("wxb_studio._launch_desktop_uri",
                        return_value=True) as opened:
            window._open_recording_folder()
        self.assertTrue(target.is_dir())
        opened.assert_called_once()
        self.assertEqual(opened.call_args.args[0], target.resolve().as_uri())

    def test_file_manager_launch_strips_appimage_environment(self):
        polluted = {key: "/tmp/.mount_Freebuff/leak"
                    for key in studio._DESKTOP_CHILD_STRIP}
        polluted.update({
            "PATH": "/tmp/.mount_Freebuff:/usr/bin:/bin",
            "XDG_DATA_DIRS":
                "/tmp/.mount_Freebuff/usr/share:./share:/usr/share",
            "DISPLAY": ":0",
            "APPDIR": "/tmp/.mount_Freebuff",
        })
        with mock.patch.dict(os.environ, polluted, clear=True), \
             mock.patch("wxb_studio.shutil.which",
                        return_value="/usr/bin/xdg-open") as which, \
             mock.patch("wxb_studio.subprocess.Popen") as popen:
            self.assertTrue(studio._launch_desktop_uri("file:///tmp/example"))
        which.assert_called_once_with(
            "xdg-open", path="/usr/local/bin:/usr/bin:/bin")
        child_env = popen.call_args.kwargs["env"]
        for key in studio._DESKTOP_CHILD_STRIP:
            self.assertNotIn(key, child_env)
        self.assertEqual(child_env["PATH"], "/usr/bin:/bin")
        self.assertEqual(child_env["XDG_DATA_DIRS"], "/usr/share")
        self.assertEqual(child_env["DISPLAY"], ":0")
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/xdg-open", "file:///tmp/example"])

    def test_recorder_page_controls_and_tab_switch_keeps_recording(self):
        window = self.win
        old = window.service._recorder
        fake = FakeUiRecorder(window.service)
        window.service._recorder = fake
        try:
            window.show_page("recorder")
            pump(0.12)
            self.assertEqual(window.stack.get_visible_child_name(), "recorder")
            for key in ("rec_status", "rec_source", "rec_resolution",
                        "rec_fps", "rec_quality", "rec_encoder", "rec_cursor",
                        "rec_dir", "rec_browse", "rec_open", "rec_legacy_note",
                        "rec_legacy_open", "rec_start", "rec_pause",
                        "rec_stop", "rec_mic", "rec_desktop_audio",
                        "rec_mic_device", "rec_desktop_device", "rec_hotkey",
                        "rec_privacy", "rec_perf", "rec_error"):
                self.assertIn(key, window.ui)
            self.assertIn("Ctrl+Alt+X", window.ui["rec_hotkey"].get_text())
            window.ui["rec_start"].emit("clicked")
            pump(0.08)
            self.assertEqual(fake.started, 1)
            self.assertTrue(fake.recording)
            self.assertIn(window.ui["rec_status"].get_text(),
                          ("RECORDING", "PAUSED"))
            window.show_page("automation")
            pump(0.08)
            self.assertTrue(fake.recording,
                            "switching tabs stopped the recorder")
            window.show_page("recorder")
            pump(0.08)
            window.ui["rec_pause"].emit("clicked")
            pump(0.05)
            self.assertTrue(fake.paused)
            self.assertEqual(window.ui["rec_pause"]._wxb_text, "Resume")
            window.ui["rec_stop"].emit("clicked")
            pump(0.08)
            self.assertFalse(fake.recording)
            self.assertEqual(fake.stopped, [False])
        finally:
            window.service._recorder = old
            window.show_page("overview")
            pump(0.05)

    def test_macros_page_controls_and_playback_survives_tab_switch(self):
        window = self.win
        window.show_page("macros")
        pump(0.08)
        self.assertEqual(window.stack.get_visible_child_name(), "macros")
        for key in ("macro_status", "macro_record", "macro_play",
                    "macro_stop", "macro_speed", "macro_loop",
                    "macro_choice", "macro_steps", "macro_step_delay"):
            self.assertIn(key, window.ui)
        self.assertFalse(window.ui["macro_stop"].get_sensitive())
        service = window.service
        sink = FakeMacroSink()
        old_caps = service.macro_capabilities
        old_sink = service._macro_sink_factory
        old_guard = service._session_locked
        try:
            service.macro_capabilities = lambda: {
                "session": "x11", "x11": True, "xwayland": False,
                "record": True, "play": True, "hotkeys": True, "reason": ""}
            service._macro_sink_factory = lambda: sink
            service._session_locked = lambda: False
            service._macro_doc = macros.make_macro("UI", [
                {"at": 4.0, "type": "key", "key": "A", "pressed": True},
                {"at": 4.1, "type": "key", "key": "A", "pressed": False},
            ])
            ok, message = service.start_macro_playback()
            self.assertTrue(ok, message)
            self.assertTrue(service.macro_running)
            window.show_page("automation")
            pump(0.03)
            self.assertTrue(service.macro_running,
                            "switching tabs stopped macro playback: %r"
                            % (service.macro_snapshot(),))
            window.show_page("macros")
            pump(0.05)
            self.assertIn(window.ui["macro_status"].get_text(),
                          ("PLAYING", "READY"))
            deadline = time.monotonic() + 6
            while service.macro_running and time.monotonic() < deadline:
                pump(0.02)
            self.assertFalse(service.macro_running)
            self.assertEqual(sink.calls,
                             [("keydown", "A"), ("keyup", "A")])
            self.assertGreaterEqual(sink.released, 1)
        finally:
            service.stop_macro(wait=True)
            service.macro_capabilities = old_caps
            service._macro_sink_factory = old_sink
            service._session_locked = old_guard
            window.show_page("overview")
            pump(0.03)

    def test_real_x11_recorder_listener_lifecycle_when_available(self):
        caps = macros.detect_capabilities()
        if not caps.get("record"):
            self.skipTest("no X11 recording support in this session")
        recorder = macros.MacroRecorder(ignore_keys=set())
        try:
            recorder.start()
            pump(0.15)
            self.assertTrue(recorder.running)
            events = recorder.stop()
            self.assertIsInstance(events, list)
            self.assertFalse(recorder.running)
        finally:
            recorder.stop()

    def test_closed_palette_never_captures_the_pointer(self):
        self.win.close_command()
        pump(0.05)
        self.assertFalse(self.win.cmd_revealer.get_can_target())
        button = self.win.search_btn
        point = Graphene.Point().init(button.get_width() / 2,
                                      button.get_height() / 2)
        ok, point = button.compute_point(self.win, point)
        self.assertTrue(ok)
        hit = self.win.pick(point.x, point.y, Gtk.PickFlags.DEFAULT)
        self.assertIsNotNone(hit)
        self.assertNotIsInstance(hit, Gtk.Revealer,
                                 "closed palette is swallowing pointer input")
        node = hit
        while node is not None and node is not self.win:
            if node is button:
                break
            node = node.get_parent()
        self.assertIs(node, button,
                      "pointer did not reach the search button")
        self.win.open_command()
        pump(0.05)
        self.assertTrue(self.win.cmd_revealer.get_can_target())
        self.win.close_command()
        pump(0.05)
        self.assertFalse(self.win.cmd_revealer.get_can_target())

    def test_ctrl_q_requests_a_clean_close(self):
        with mock.patch.object(self.win, "close") as close:
            handled = self.win._on_key(
                None, Gdk.KEY_q, 0, Gdk.ModifierType.CONTROL_MASK)
            self.assertTrue(handled)
            close.assert_called_once()
        self.assertFalse(self.win._on_key(None, Gdk.KEY_q, 0, 0))

    def test_command_palette_filters_and_activates(self):
        self.win.open_command()
        pump(0.02)
        self.assertTrue(self.win.cmd_revealer.get_reveal_child())
        self.win.cmd_entry.set_text("cordial")
        self.win._fill_command()
        pump(0.02)
        rows = [self.win.cmd_list.get_row_at_index(i)
                for i in range(12)]
        rows = [r for r in rows if r is not None]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        title = (row.get_child().get_first_child().get_next_sibling()
                 .get_first_child().get_text())
        self.assertEqual(title, "Cordial")
        self.win._activate_command(self.win.cmd_list, row)
        pump(0.02)
        self.assertFalse(self.win.cmd_revealer.get_reveal_child())
        self.assertEqual(self.win.stack.get_visible_child_name(), "alts")

    def test_clicker_button_updates_engine_and_releases(self):
        self.win.show_page("automation")
        self.win.ui["ac_cps"].set_value(40)
        self.win.ui["ac_duty"].set_value(50)
        self.win._toggle_clicker()
        pump(0.15)
        self.assertTrue(self.win.service.ac_running)
        self.win._toggle_clicker()
        pump(0.05)
        self.assertFalse(self.win.service.ac_running)
        self.assertFalse(self.win.ui["ac_start"].get_child() is None)
        self.assertIn("Start clicking",
                      self.win.ui["ac_start"]._wxb_text)

    def test_exact_clicker_fields_are_editable_and_clamped(self):
        self.win.show_page("automation")
        for key, expected in (("ac_cps_value", 123.45),
                              ("ac_duty_value", 45.67)):
            entry = self.win.ui[key]
            self.assertIsInstance(entry, Gtk.Entry)
            self.assertTrue(entry.get_has_frame())
            self.assertTrue(entry.has_css_class("wxb-entry"))
            self.assertTrue(entry.has_css_class("wxb-value-entry"))
            entry.set_text(str(expected))
            entry.emit("activate")
            pump(0.03)
        self.assertAlmostEqual(self.win.ui["ac_cps"].get_value(), 123.45,
                               places=2)
        self.assertAlmostEqual(self.win.ui["ac_duty"].get_value(), 45.67,
                               places=2)
        self.assertAlmostEqual(
            self.win.service.settings["clicker"]["cps"], 123.45, places=2)
        self.assertAlmostEqual(
            self.win.service.settings["clicker"]["duty"], 45.67, places=2)
        entry = self.win.ui["ac_cps_value"]
        entry.set_text("not a number")
        entry.emit("activate")
        pump(0.02)
        self.assertAlmostEqual(self.win.ui["ac_cps"].get_value(), 123.45,
                               places=2)
        entry.set_text("1500")
        entry.emit("activate")
        pump(0.02)
        self.assertEqual(self.win.ui["ac_cps"].get_value(), 1000.0)
        self.win.ui["ac_cps"].set_value(12)
        self.win.ui["ac_duty"].set_value(50)

    def test_wide_clicker_and_named_config_round_trip(self):
        self.win.show_page("automation")
        self.win.ui["ac_cps"].set_value(987.65)
        self.win.ui["ac_duty"].set_value(12.5)
        pump(0.03)
        self.assertAlmostEqual(
            self.win.service.settings["clicker"]["cps"], 987.65, places=2)
        self.assertAlmostEqual(
            self.win.service.settings["clicker"]["duty"], 12.5, places=2)
        self.win.show_page("settings")
        self.win.ui["config_name"].set_text("UI test config")
        self.win._save_config()
        pump(0.03)
        self.assertIn("UI test config", self.win.service.list_configs())
        self.win.ui["ac_cps"].set_value(20)
        self.win._load_config()
        pump(0.03)
        self.assertAlmostEqual(
            self.win.service.settings["clicker"]["cps"], 987.65, places=2)
        self.win.service.delete_config("UI test config")

    def test_keybindings_are_editable_and_validated(self):
        self.win.show_page("settings")
        entries = self.win.ui["hotkey_entries"]
        entries["toggle_clicker"].set_text("Ctrl+Shift+F8")
        entries["toggle_rotation"].set_text("Alt+1")
        entries["toggle_socd"].set_text("")
        entries["stop_all"].set_text("F10")
        self.win._apply_hotkeys()
        hotkeys = self.win.service.settings["hotkeys"]
        self.assertEqual(hotkeys["toggle_clicker"], "ctrl+shift+f8")
        self.assertEqual(hotkeys["toggle_rotation"], "alt+1")
        self.assertEqual(hotkeys["toggle_socd"], "")
        self.assertEqual(hotkeys["stop_all"], "f10")
        entries["toggle_clicker"].set_text("Alt+1")
        self.win._apply_hotkeys()
        self.assertTrue(self.win.ui["hotkey_error"].get_visible())
        self.assertEqual(
            self.win.service.settings["hotkeys"]["toggle_clicker"],
            "ctrl+shift+f8")

    def test_privacy_masks_values_and_restores_them_without_editing(self):
        self.win.show_page("automation")
        self.win.ui["ac_cps"].set_value(77.5)
        self.win.ui["ac_duty"].set_value(33)
        pump(0.03)
        self.win.show_page("settings")
        self.win.ui["opacity"].set_value(66)
        pump(0.03)
        before = (self.win.service.settings["clicker"]["cps"],
                  self.win.service.settings["clicker"]["duty"],
                  self.win.service.settings["opacity"],
                  dict(self.win.service.settings["hotkeys"]))
        self.assertFalse(self.win.privacy)
        self.assertIsNotNone(self.win.privacy_btn)
        self.win.set_privacy(True)
        self.assertTrue(self.win.privacy)
        self.assertEqual(self.win.privacy_btn._wxb_text, "Hidden")
        self.assertEqual(self.win.ui["ac_cps_value"].get_text(),
                         studio.PRIVACY_MASK)
        self.assertFalse(self.win.ui["ac_cps_value"].get_sensitive())
        self.assertFalse(self.win.ui["ac_cps"].get_sensitive())
        self.assertEqual(self.win.ui["opacity_value"].get_text(),
                         studio.PRIVACY_MASK)
        self.assertFalse(self.win.ui["opacity"].get_sensitive())
        self.assertFalse(self.win.ui["hotkey_apply"].get_sensitive())
        self.assertTrue(self.win.ui["ac_start"].get_sensitive())
        self.assertEqual(self.win.ui["socd_conflicts"].get_text(),
                         studio.PRIVACY_MASK + " conflicts")
        self.assertEqual(self.win.ui["rot_a_hint"].get_text(),
                         studio.PRIVACY_MASK_KEY)
        self.assertEqual(self.win.ui["ac_hotkey_hint"].get_text(),
                         "Hotkey · " + studio.PRIVACY_MASK)
        self.assertEqual(self.win.ui["rot_da"].get_text(), "")
        self.assertEqual(self.win.ui["rot_da"].get_value(), 140)
        for entry in self.win.ui["hotkey_entries"].values():
            self.assertEqual(entry.get_text(), studio.PRIVACY_MASK)
            self.assertFalse(entry.get_sensitive())
        self.win.show_page("overview")
        self.assertEqual(self.win.ui["ac_card_value"].get_text(),
                         studio.PRIVACY_MASK)
        self.assertEqual(self.win.metric_clicks.get_text(),
                         studio.PRIVACY_MASK)
        self.assertEqual(before, (
            self.win.service.settings["clicker"]["cps"],
            self.win.service.settings["clicker"]["duty"],
            self.win.service.settings["opacity"],
            dict(self.win.service.settings["hotkeys"])))
        self.win.set_privacy(False)
        self.assertFalse(self.win.privacy)
        self.assertEqual(self.win.privacy_btn._wxb_text, "Hide UI")
        self.assertEqual(self.win.ui["ac_cps_value"].get_text(), "77.5")
        self.assertTrue(self.win.ui["ac_cps_value"].get_sensitive())
        self.assertTrue(self.win.ui["ac_cps"].get_sensitive())
        self.win.show_page("settings")
        self.assertEqual(self.win.ui["opacity_value"].get_text(), "66%")
        self.assertTrue(self.win.ui["hotkey_apply"].get_sensitive())
        self.assertEqual(self.win.ui["rot_a_hint"].get_text(), "key 1")
        self.assertNotEqual(self.win.ui["rot_da"].get_text(), "")
        self.assertNotEqual(self.win.ui["hotkey_entries"]["toggle_clicker"]
                            .get_text(), studio.PRIVACY_MASK)
        # Return the shared test window to defaults.
        self.win.ui["ac_cps"].set_value(12)
        self.win.ui["ac_duty"].set_value(50)
        self.win.ui["opacity"].set_value(92)
        pump(0.03)

    def test_rotation_master_switch_starts_off_and_gates_controls(self):
        self.win.show_page("rotation")
        self.win.service.set_macro_enabled(False)
        self.win.ui["rot_enabled"].set_active(False)
        pump(0.03)
        self.assertFalse(self.win.service.macro_enabled)
        self.assertFalse(self.win.ui["rot_start"].get_sensitive())
        self.assertFalse(self.win.ui["rot_a"].get_sensitive())
        self.assertEqual(self.win.ui["rot_status"].get_text(), "DISABLED")
        self.win.ui["rot_enabled"].set_active(True)
        pump(0.03)
        self.assertTrue(self.win.service.macro_enabled)
        self.assertTrue(self.win.ui["rot_start"].get_sensitive())
        self.assertTrue(self.win.ui["rot_a"].get_sensitive())
        self.win.ui["rot_enabled"].set_active(False)
        pump(0.03)
        self.assertFalse(self.win.service.macro_enabled)
        self.assertFalse(self.win.ui["rot_start"].get_sensitive())

    def test_escape_is_emergency_stop_but_closes_the_palette_first(self):
        with mock.patch.object(self.win.service, "stop_all") as stop_all:
            self.assertTrue(self.win._on_key(None, Gdk.KEY_Escape, 0, 0))
            stop_all.assert_called_once_with()
        self.win.open_command()
        pump(0.02)
        with mock.patch.object(self.win.service, "stop_all") as stop_all:
            self.assertTrue(self.win._on_key(None, Gdk.KEY_Escape, 0, 0))
            stop_all.assert_not_called()
        self.assertFalse(self.win.cmd_revealer.get_reveal_child())

    def test_slot_hints_map_ten_to_zero(self):
        self.win.show_page("rotation")
        self.win.ui["rot_a"].set_selected(10)
        pump(0.03)
        self.assertEqual(self.win.ui["rot_a_hint"].get_text(), "key 0")
        self.win.ui["rot_a"].set_selected(1)
        pump(0.03)
        self.assertEqual(self.win.ui["rot_a_hint"].get_text(), "key 1")

    def test_hotkey_capture_reset_and_event_translation(self):
        self.assertEqual(
            studio.hotkey_from_event(
                Gdk.KEY_F8, Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.SHIFT_MASK),
            "ctrl+shift+f8")
        self.assertEqual(studio.hotkey_from_event(Gdk.KEY_Control_L, 0), "")
        self.win.show_page("settings")
        self.win._start_hotkey_capture("toggle_clicker")
        pump(0.05)
        dialog = self.win._capture_window
        self.assertIsNotNone(dialog)
        dialog._on_key(None, Gdk.KEY_F8, 0, Gdk.ModifierType.CONTROL_MASK)
        pump(0.05)
        self.assertEqual(
            self.win.ui["hotkey_entries"]["toggle_clicker"].get_text(),
            "Ctrl+F8")
        self.assertEqual(
            self.win.service.settings["hotkeys"]["toggle_clicker"],
            "ctrl+f8")
        self.win._reset_hotkey("toggle_clicker")
        pump(0.03)
        self.assertEqual(
            self.win.service.settings["hotkeys"]["toggle_clicker"], "f6")
        self.win._reset_hotkeys()
        pump(0.03)

    def test_second_activation_raises_the_existing_window(self):
        with mock.patch.object(self.win, "present") as present:
            self.app.do_activate()
        present.assert_called_once_with()

    def test_stop_all_is_hidden_when_idle(self):
        self.win.show_page("overview")
        self.win._stop_all()
        pump(0.02)
        self.assertFalse(self.win.stop_btn.get_visible())

    def test_close_with_stop_on_close_keeps_sober_running(self):
        service = StudioService(path=Path(self.tmp.name) / "close-probe.json",
                                auto_start=False)
        service.settings["stop_on_close"] = True
        service.save()
        probe = WxbWindow(self.app, service, show_onboarding=False,
                          auto_scan=False)
        self.addCleanup(service.shutdown)
        self.addCleanup(probe.close)
        with mock.patch.object(service, "stop_sober") as stop_sober:
            probe._on_close()
            service.shutdown()
        stop_sober.assert_not_called()

    def test_settings_exposes_stop_on_close(self):
        self.win.show_page("settings")
        pump(0.03)
        switch = self.win.ui["stop_on_close"]
        self.assertIsInstance(switch, Gtk.Switch)
        switch.set_active(True)
        pump(0.02)
        self.assertTrue(self.win.service.settings["stop_on_close"])
        saved = load_settings(self.win.service.path)
        self.assertTrue(saved["stop_on_close"])
        switch.set_active(False)
        pump(0.02)
        self.assertFalse(self.win.service.settings["stop_on_close"])

    def test_applying_settings_updates_navigation_close_and_cordial_controls(self):
        service = self.win.service
        previous = {
            "nav_compact": service.settings["nav_compact"],
            "stop_on_close": service.settings["stop_on_close"],
            "alts": dict(service.settings["alts"]),
        }
        self.addCleanup(lambda: service.settings["alts"].update(previous["alts"]))
        try:
            service.settings["nav_compact"] = True
            service.settings["stop_on_close"] = True
            service.settings["alts"].update({
                "auto_tile": False, "fps_cap": 45, "low_graphics": False,
                "cpu_quota": 60, "tiling_style": "focus-top",
            })
            self.win._apply_settings_to_controls()
            pump(0.03)
            self.assertTrue(self.win.ui["nav_compact"].get_active())
            self.assertTrue(self.win.ui["stop_on_close"].get_active())
            self.assertFalse(self.win.ui["alt_auto_tile"].get_active())
            self.assertEqual(self.win.ui["alt_fps_cap"].get_value(), 45)
            self.assertFalse(self.win.ui["alt_low_graphics"].get_active())
            self.assertEqual(self.win.ui["alt_cpu_quota"].get_value(), 60)
            self.assertEqual(self.win.ui["alt_tiling_style"].get_selected(), 4)
        finally:
            service.settings["nav_compact"] = previous["nav_compact"]
            service.settings["stop_on_close"] = previous["stop_on_close"]
            service.settings["alts"].update(previous["alts"])
            self.win._apply_settings_to_controls()
            pump(0.02)

    def test_responsive_reflow_tracks_width(self):
        # The grids reflow independently of a window manager.  Keep this
        # deterministic; the real 900px layout is exercised by preview shots.
        self.win._reflow(900)
        for grid in self.win._reflow_groups:
            self.assertEqual(grid._cols, min(2, grid._max_cols))
        self.win._reflow(700)
        for grid in self.win._reflow_groups:
            self.assertEqual(grid._cols, 1)
        self.win._reflow(1180)
        for grid in self.win._reflow_groups:
            self.assertEqual(grid._cols, grid._max_cols)

    def test_onboarding_steps_and_completion(self):
        done = []
        win = OnboardingWindow(self.win, "#e7a6b4", lambda: done.append(True))
        win.present()
        pump(0.05)
        self.assertEqual(win.stack.get_visible_child_name(), "step-0")
        win._next()
        self.assertEqual(win.stack.get_visible_child_name(), "step-1")
        win._back()
        self.assertEqual(win.stack.get_visible_child_name(), "step-0")
        win._next(); win._next()
        self.assertEqual(win.stack.get_visible_child_name(), "step-2")
        win._finish()
        pump(0.05)
        self.assertEqual(done, [True])

    def test_accent_provider_reloads(self):
        self.app.apply_accent("#a8c3a1")
        self.app.apply_accent("#9dc5d6")
        self.assertEqual(self.app._accent, "#9dc5d6")

    def test_monochrome_runtime_switch_and_restore(self):
        window = self.win
        app = self.app
        settings = window.service.settings
        original_mono = bool(settings.get("monochrome", False))
        original_accent = settings.get("accent", "cyan")
        normal_accent = studio.ACCENTS.get(original_accent, "#9fcaff")
        try:
            # Begin in the normal theme so this test also covers colour
            # restoration after leaving mono at runtime.
            settings["monochrome"] = False
            window._set_mono_switch(False)
            window._apply_monochrome_controls(False)
            self.assertTrue(app.apply_appearance())
            before_provider = app._provider
            self.assertFalse(app._monochrome)
            self.assertEqual(app._accent, normal_accent)

            window.show_page("settings")
            window.ui["monochrome"].set_active(True)
            pump(0.03)
            self.assertTrue(window.ui["monochrome"].get_active())
            self.assertTrue(settings["monochrome"])
            self.assertTrue(app._monochrome)
            self.assertEqual(app._accent, "#ffffff")
            self.assertIsNot(app._provider, before_provider)
            self.assertEqual(
                load_settings(window.service.path)["monochrome"], True)
            self.assertFalse(window.ui["accent_buttons"][0].get_sensitive())

            mono_provider = app._provider
            window.ui["monochrome"].set_active(False)
            pump(0.03)
            self.assertFalse(settings["monochrome"])
            self.assertFalse(app._monochrome)
            self.assertEqual(app._accent, normal_accent)
            self.assertIsNot(app._provider, mono_provider)
            self.assertEqual(
                load_settings(window.service.path)["monochrome"], False)
            self.assertTrue(window.ui["accent_buttons"][0].get_sensitive())
        finally:
            settings["monochrome"] = original_mono
            window._set_mono_switch(original_mono)
            window._apply_monochrome_controls(original_mono)
            window.service.save()
            app.apply_appearance()

    def test_first_allocation_at_900_is_already_narrow(self):
        service = StudioService(path=Path(self.tmp.name) / "probe.json",
                                auto_start=False)
        probe = WxbWindow(self.app, service, show_onboarding=False,
                          auto_scan=False)
        self.addCleanup(service.shutdown)
        self.addCleanup(probe.close)
        probe.set_default_size(900, 700)
        probe.present()
        pump(0.25)
        # X11 client geometry can be a few pixels inside the requested
        # decorated window; the contract is that it is already below the old
        # 1122px floor, not that the decoration math is exact.
        self.assertLessEqual(probe.get_width(), 900)
        self.assertGreaterEqual(probe.get_width(), 850)
        self.assertLessEqual(probe.measure(Gtk.Orientation.HORIZONTAL, -1)[0],
                             900)
        self.assertTrue(probe.split.get_collapsed())
        self.assertFalse(probe.split.get_show_sidebar())
        self.assertTrue(probe.side_btn.get_visible())
        for flow in probe._reflow_groups:
            self.assertEqual(flow.get_max_children_per_line(),
                             min(2, flow._max_cols))

    def test_responsive_watchdog_survives_preallocation(self):
        # A width of zero before the first map must not remove the repeating
        # 120 ms watchdog; that was the startup race at 900px.
        with mock.patch.object(self.win, "get_width", return_value=0):
            self.assertTrue(self.win._responsive())

    def children(self, box):
        out = []
        child = box.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    def test_alts_empty_single_hundreds_and_long_unicode_names(self):
        def render(profiles):
            self.win._alt_sig = None
            # Drain any service refresh that was queued by an earlier test
            # before injecting this synthetic profile set; otherwise its
            # empty snapshot can land after the injection and clear it.
            pump(0.02)
            self.win._refresh_alts({
                "profiles": profiles, "running": [], "scanning": False,
                "error": "", "backend": "Cordial",
            })
            return (len(self.children(self.win.ui["alts_list"])),
                    len(self.children(self.win.ui["alts_empty"])))

        self.assertEqual(render([]), (0, 1))
        self.assertEqual(render(["Solo"]), (1, 0))
        names = ["A very long Cordial profile name " + "x" * 120,
                 "🎮 unicode / 日本語 / مرحبا"]
        names += ["Profile %03d 🚀" % i for i in range(250)]
        count, empty = render(names)
        self.assertEqual((count, empty), (252, 0))
        row = self.win._alt_row("A" * 500 + " 🚀", False)
        name_label = (row.get_first_child().get_next_sibling()
                      .get_first_child())
        self.assertEqual(name_label.get_ellipsize(), Pango.EllipsizeMode.END)
        self.assertEqual(name_label.get_text(), "A" * 500 + " 🚀")

    def test_cordial_page_is_compact_and_exposes_tiling_styles(self):
        self.win.show_page("alts")
        pump(0.03)
        self.assertIsInstance(self.win.ui["alts_list"], Gtk.FlowBox)
        self.assertEqual(self.win.ui["alts_list"].get_max_children_per_line(),
                         2)
        self.assertFalse(self.win.ui["alts_quick_fold"].get_expanded())
        self.assertFalse(self.win.ui["alts_advanced_fold"].get_expanded())
        self.assertIsInstance(self.win.ui["alt_tiling_style"], Gtk.DropDown)
        self.assertIsInstance(self.win.ui["alt_low_graphics"], Gtk.Switch)
        self.assertIsInstance(self.win.ui["alt_cpu_quota"], Gtk.SpinButton)
        self.assertEqual(len(studio.ALT_TILING_CHOICES), 5)
        def save_options(**kwargs):
            self.win.service.settings["alts"].update(kwargs)
            return True, "ok"

        with mock.patch.object(self.win.service, "set_alt_options",
                               side_effect=save_options) as save:
            self.win.ui["alt_tiling_style"].set_selected(1)
            pump(0.02)
            self.assertEqual(save.call_args.kwargs["tiling_style"], "rows")
            self.win.ui["alt_low_graphics"].set_active(False)
            pump(0.02)
            self.assertFalse(save.call_args.kwargs["low_graphics"])
            self.win.ui["alt_cpu_quota"].set_value(40)
            pump(0.02)
            self.assertEqual(save.call_args.kwargs["cpu_quota"], 40)
        self.win.service.settings["alts"].update({
            "auto_tile": True, "fps_cap": ALT_FPS_DEFAULT,
            "low_graphics": True, "cpu_quota": 0,
            "tiling_style": "columns",
        })
        self.win._sync_alt_options(self.win.service.settings["alts"])

    def test_cordial_profile_add_and_delete_are_wired(self):
        self.win.show_page("alts")
        pump(0.03)
        self.assertIsInstance(self.win.ui["alt_profile_name"], Gtk.Entry)
        self.assertIsInstance(self.win.ui["alt_profile_add"], Gtk.Button)
        self.win.ui["alt_profile_name"].set_text("Fresh")
        with mock.patch.object(self.win.service, "add_alt_profile",
                               return_value=(True, "Profile Fresh added.")) as add:
            self.win.ui["alt_profile_add"].emit("clicked")
        add.assert_called_once_with("Fresh")
        self.assertEqual(self.win.ui["alt_profile_name"].get_text(), "")
        self.assertFalse(self.win.ui["alt_profile_error"].get_visible())

        with mock.patch.object(self.win.service, "delete_alt_profile",
                               return_value=(True, "Profile Alt1 deleted.")) as remove:
            self.win._delete_alt_now("Alt1")
        remove.assert_called_once_with("Alt1")

        row = self.win._alt_row("Alt1", False)
        actions = row.get_last_child()
        delete = actions.get_last_child()
        self.assertIn("Delete", delete.get_tooltip_text())
        with mock.patch.object(self.win.service, "launch_alt",
                               return_value=(True, "Launching Alt1.")) as launch:
            actions.get_first_child().emit("clicked")
        launch.assert_called_once_with("Alt1")
        running_row = self.win._alt_row("Alt2", True)
        self.assertFalse(running_row.get_last_child().get_last_child()
                         .get_sensitive())

    def test_hotkey_driven_tiling_style_syncs_the_dropdown(self):
        self.win.show_page("alts")
        pump(0.03)
        self.win.service.settings["alts"]["tiling_style"] = "grid"
        self.win._sync_alt_tiling({"tiling_style": "grid"})
        self.assertEqual(self.win.ui["alt_tiling_style"].get_selected(), 2)
        self.assertIn("grid",
                      self.win.ui["alt_options_status"].get_text().lower())
        self.win.service.settings["alts"]["tiling_style"] = "columns"
        self.win._sync_alt_tiling({"tiling_style": "columns"})
        self.assertEqual(self.win.ui["alt_tiling_style"].get_selected(), 0)

    def test_lution_controls_are_wired_to_the_service(self):
        win = self.win
        font_status = {"ok": True, "apk": True, "apk_fonts": 75,
                       "active": 0, "installed": ""}
        cursor_status = {"ok": True, "active": 0, "assets": 3,
                         "style": "Cross", "color": "Pink", "size": 32,
                         "pillow": True}
        fps_status = {"ok": True, "fps": 144, "available": True,
                      "running": False}
        win.show_page("settings")
        with mock.patch.object(win.service, "fonts_status",
                               return_value=font_status), \
                mock.patch.object(win.service, "cursors_status",
                                  return_value=cursor_status), \
                mock.patch.object(win.service, "fps_status",
                                  return_value=fps_status), \
                mock.patch.object(win.service, "apply_font",
                                  return_value=(True, "font ok")) as apply_font, \
                mock.patch.object(win.service, "restore_fonts",
                                  return_value=(True, "font restored")) as restore_font, \
                mock.patch.object(win.service, "reapply_font",
                                  return_value=(True, "font reapplied")) as reapply_font, \
                mock.patch.object(win.service, "apply_cursor",
                                  return_value=(True, "cursor ok")) as apply_cursor, \
                mock.patch.object(win.service, "restore_cursors",
                                  return_value=(True, "cursors restored")) as restore_cursor, \
                mock.patch.object(win.service, "apply_online_cursor",
                                  return_value=(True, "online ok")) as apply_online, \
                mock.patch.object(win.service, "set_fps_cap",
                                  return_value=(True, "cap ok")) as set_fps:
            win._apply_font_path("/tmp/font.ttf")
            apply_font.assert_called_once_with("/tmp/font.ttf")
            win.ui["font_restore"].emit("clicked")
            restore_font.assert_called_once_with()
            win.ui["font_reapply"].emit("clicked")
            reapply_font.assert_called_once_with()
            win.ui["cursor_style"].set_selected(0)
            win.ui["cursor_color"].set_selected(0)
            win.ui["cursor_size"].set_value(32)
            win.ui["cursor_apply"].emit("clicked")
            apply_cursor.assert_called_once()
            win.ui["cursor_restore"].emit("clicked")
            restore_cursor.assert_called_once_with()
            self.assertGreater(len(win._cursor_online), 1)
            win.ui["cursor_online"].set_selected(1)
            win.ui["cursor_download"].emit("clicked")
            apply_online.assert_called_once_with("Flat pointer", 32)
            win.ui["fps_value"].set_value(120)
            win.ui["fps_apply"].emit("clicked")
            set_fps.assert_called_once_with(120)

        win.show_page("alts")
        flags = {"ok": True, "flags": {"FFlagGood": 1}}
        with mock.patch.object(win.service, "lution_flags",
                               return_value=flags), \
                mock.patch.object(win.service, "flag_allowlist",
                                  return_value={"FFlagGood"}), \
                mock.patch.object(win.service, "lution_flags_text",
                                  return_value=(True, "FFlagGood = 1")), \
                mock.patch.object(win.service, "set_lution_flags_text",
                                  return_value=(True, "flags ok")) as set_flags, \
                mock.patch.object(win.service, "clean_lution_flags",
                                  return_value=(True, "cleaned")) as clean_flags, \
                mock.patch.object(win.service, "list_flag_profiles",
                                  return_value=["Daily"]), \
                mock.patch.object(win.service, "save_flag_profile",
                                  return_value=(True, "profile saved")) as save_profile:
            win._set_flag_text("FFlagGood = 2")
            win.ui["flags_save"].emit("clicked")
            self.assertEqual(set_flags.call_args.args[0], "FFlagGood = 2")
            win.ui["flags_clean"].emit("clicked")
            clean_flags.assert_called_once_with()
            win.ui["flag_profile_name"].set_text("Daily")
            win.ui["flag_profile_save"].emit("clicked")
            save_profile.assert_called_once_with("Daily")

        with mock.patch.object(win.service, "lution_env",
                               return_value={"ok": True,
                                             "env": {"A": "1"}}), \
                mock.patch.object(win.service, "lution_env_text",
                                  return_value=(True, "A = 1")), \
                mock.patch.object(win.service, "save_lution_env_text",
                                  return_value=(True, "env ok")) as save_env:
            win.ui["env_view"].get_buffer().set_text("A = 2")
            win.ui["env_save"].emit("clicked")
            self.assertEqual(save_env.call_args.args[0], "A = 2")

        with mock.patch.object(win.service, "ensure_sober",
                               return_value=(True, "installing")) as ensure:
            win.ui["sober_ensure"].emit("clicked")
            ensure.assert_called_once_with()
        self.assertIn("Installing", win.ui["sober_status"].get_text())
        with mock.patch.object(type(win.service), "sober_running",
                               new_callable=mock.PropertyMock,
                               return_value=True), \
                mock.patch.object(win.service, "stop_sober",
                                  return_value=(True, "Stopping Sober…")) as stop:
            win._stop_sober()
            stop.assert_called_once_with()
        with mock.patch.object(type(win.service), "sober_running",
                               new_callable=mock.PropertyMock,
                               return_value=False), \
                mock.patch.object(win.service, "stop_sober") as idle_stop:
            win.ui["sober_stop"].emit("clicked")
            idle_stop.assert_not_called()

    def _assert_widgets_inside(self, window, page_key, widgets):
        window.show_page(page_key)
        pump(0.12)
        page = window.stack.get_visible_child()
        self.assertGreater(page.get_width(), 0, page_key)
        for name, widget in widgets:
            self.assertTrue(widget.get_mapped(), "%s %s is not mapped" %
                            (page_key, name))
            self.assertGreater(widget.get_width(), 0,
                               "%s %s has zero width" % (page_key, name))
            self.assertGreater(widget.get_height(), 0,
                               "%s %s has zero height" % (page_key, name))
            ok, rect = widget.compute_bounds(page)
            self.assertTrue(ok, "%s %s has no bounds" % (page_key, name))
            self.assertGreaterEqual(rect.get_x(), -0.5,
                                    "%s %s clipped left" % (page_key, name))
            self.assertLessEqual(rect.get_x() + rect.get_width(),
                                 page.get_width() + 0.5,
                                 "%s %s clipped right" % (page_key, name))

    def _assert_no_sibling_overlap(self, window, page_key, widgets):
        page = window.stack.get_visible_child()
        rects = []
        for name, widget in widgets:
            ok, rect = widget.compute_bounds(page)
            self.assertTrue(ok, "%s %s has no bounds" % (page_key, name))
            rects.append((name, (rect.get_x(), rect.get_y(),
                                 rect.get_width(), rect.get_height())))
        for index, (name, (x, y, w, h)) in enumerate(rects):
            for other, (ox, oy, ow, oh) in rects[index + 1:]:
                overlap = (x < ox + ow - 0.5 and ox < x + w - 0.5 and
                           y < oy + oh - 0.5 and oy < y + h - 0.5)
                self.assertFalse(overlap, "%s %s overlaps %s" %
                                 (page_key, name, other))

    def _assert_added_rows_are_rendered(self, window):
        privacy = window.privacy_btn
        self.assertTrue(privacy.get_mapped())
        self.assertGreater(privacy.get_width(), 0)
        self.assertGreater(privacy.get_height(), 0)
        self._assert_widgets_inside(window, "automation", [
            ("clicker hold mode", window.ui["ac_mode"]),
        ])
        self._assert_widgets_inside(window, "rotation", [
            ("macro master switch", window.ui["rot_enabled"]),
            ("rotation hold mode", window.ui["rot_mode"]),
        ])
        self._assert_widgets_inside(window, "socd", [
            ("socd start", window.ui["socd_start"]),
        ])
        self._assert_widgets_inside(window, "macros", [
            ("record", window.ui["macro_record"]),
            ("play", window.ui["macro_play"]),
            ("stop", window.ui["macro_stop"]),
            ("speed", window.ui["macro_speed"]),
            ("loop", window.ui["macro_loop"]),
            ("file choice", window.ui["macro_choice"]),
            ("steps", window.ui["macro_steps"]),
            ("step delay", window.ui["macro_step_delay"]),
        ])
        self._assert_widgets_inside(window, "recorder", [
            ("recorder source", window.ui["rec_source"]),
            ("recorder resolution", window.ui["rec_resolution"]),
            ("recorder fps", window.ui["rec_fps"]),
            ("recorder quality", window.ui["rec_quality"]),
            ("recorder encoder", window.ui["rec_encoder"]),
            ("recorder folder", window.ui["rec_dir"]),
            ("recorder open folder", window.ui["rec_open"]),
            ("recorder start", window.ui["rec_start"]),
            ("recorder pause", window.ui["rec_pause"]),
            ("recorder stop", window.ui["rec_stop"]),
        ])
        window.ui["rot_enabled"].set_active(True)
        pump(0.03)
        window.ui["rot_enabled"].set_active(False)
        pump(0.03)
        self._assert_widgets_inside(window, "settings", [
            ("stop on close", window.ui["stop_on_close"]),
            ("font status", window.ui["font_status"]),
            ("font choose", window.ui["font_choose"]),
            ("font restore", window.ui["font_restore"]),
            ("font reapply", window.ui["font_reapply"]),
            ("cursor style", window.ui["cursor_style"]),
            ("cursor colour", window.ui["cursor_color"]),
            ("cursor size", window.ui["cursor_size"]),
            ("cursor apply", window.ui["cursor_apply"]),
            ("cursor restore", window.ui["cursor_restore"]),
            ("cursor online", window.ui["cursor_online"]),
            ("cursor download", window.ui["cursor_download"]),
            ("cursor online hint", window.ui["cursor_online_hint"]),
            ("fps value", window.ui["fps_value"]),
            ("fps apply", window.ui["fps_apply"]),
            ("hotkey clicker", window.ui["hotkey_entries"]["toggle_clicker"]),
            ("hotkey stop", window.ui["hotkey_entries"]["stop_all"]),
            ("hotkey set", window.ui["hotkey_set_buttons"]["toggle_clicker"]),
        ])
        self._assert_no_sibling_overlap(window, "settings", [
            ("font choose", window.ui["font_choose"]),
            ("font restore", window.ui["font_restore"]),
            ("font reapply", window.ui["font_reapply"]),
            ("cursor apply", window.ui["cursor_apply"]),
            ("cursor restore", window.ui["cursor_restore"]),
            ("cursor online", window.ui["cursor_online"]),
            ("cursor download", window.ui["cursor_download"]),
            ("fps value", window.ui["fps_value"]),
            ("fps apply", window.ui["fps_apply"]),
        ])

        window.ui["alts_advanced_fold"].set_expanded(True)
        window.ui["flags_expander"].set_expanded(True)
        window.ui["env_expander"].set_expanded(True)
        pump(0.05)
        self._assert_widgets_inside(window, "alts", [
            ("profile name", window.ui["alt_profile_name"]),
            ("profile add", window.ui["alt_profile_add"]),
            ("low graphics", window.ui["alt_low_graphics"]),
            ("cpu quota", window.ui["alt_cpu_quota"]),
            ("sober status", window.ui["sober_status"]),
            ("sober ensure", window.ui["sober_ensure"]),
            ("sober stop", window.ui["sober_stop"]),
            ("flags expander", window.ui["flags_expander"]),
            ("flags view", window.ui["flags_view"]),
            ("flags save", window.ui["flags_save"]),
            ("flag profiles", window.ui["flag_profile_choice"]),
            ("env expander", window.ui["env_expander"]),
            ("env view", window.ui["env_view"]),
            ("env save", window.ui["env_save"]),
        ])
        self._assert_no_sibling_overlap(window, "alts", [
            ("sober ensure", window.ui["sober_ensure"]),
            ("sober stop", window.ui["sober_stop"]),
            ("sober refresh", window.ui["sober_refresh"]),
            ("flags save", window.ui["flags_save"]),
            ("flags clean", window.ui["flags_clean"]),
            ("env save", window.ui["env_save"]),
            ("env reload", window.ui["env_reload"]),
        ])
        window.ui["alts_advanced_fold"].set_expanded(False)
        pump(0.03)

    def test_added_rows_render_at_default_and_900_width(self):
        self._assert_added_rows_are_rendered(self.win)
        service = StudioService(path=Path(self.tmp.name) / "geometry-900.json",
                                auto_start=False)
        probe = WxbWindow(self.app, service, show_onboarding=False,
                          auto_scan=False)
        self.addCleanup(service.shutdown)
        self.addCleanup(probe.close)
        probe.set_default_size(900, 700)
        probe.present()
        pump(0.3)
        self.assertLessEqual(probe.get_width(), 900)
        self.assertGreaterEqual(probe.get_width(), 850)
        self._assert_added_rows_are_rendered(probe)

    def test_clicker_advanced_controls_and_profile_round_trip(self):
        """Pointer, duration, failsafe and profile controls stay usable."""
        window = self.win
        window.show_page("automation")
        window.ui["ac_advanced"].set_expanded(True)
        pump(0.12)
        self._assert_widgets_inside(window, "automation", [
            ("duration", window.ui["ac_duration"]),
            ("pointer", window.ui["ac_pointer"]),
            ("jitter", window.ui["ac_jitter"]),
            ("corner", window.ui["ac_corner"]),
            ("failsafe", window.ui["ac_failsafe"]),
            ("pause lock", window.ui["ac_pause_lock"]),
            ("set point", window.ui["ac_set_point"]),
            ("profile name", window.ui["ac_profile_name"]),
            ("profile choice", window.ui["ac_profile_choice"]),
            ("profile save", window.ui["ac_profile_save"]),
            ("profile load", window.ui["ac_profile_load"]),
            ("profile delete", window.ui["ac_profile_delete"]),
        ])
        window.ui["ac_duration"].set_value(2.5)
        window.ui["ac_pointer"].set_selected(1)
        window.ui["ac_jitter"].set_value(7)
        window.ui["ac_pause_lock"].set_active(False)
        window._capture_clicker_point()
        pump(0.04)
        clicker = window.service.settings["clicker"]
        self.assertEqual(clicker["duration"], 2.5)
        self.assertEqual(clicker["pointer_mode"], "fixed")
        self.assertEqual(clicker["jitter"], 7)
        self.assertFalse(clicker["pause_on_lock"])
        self.assertEqual((clicker["fixed_x"], clicker["fixed_y"]),
                         (123, 456))

        window.ui["ac_profile_name"].set_text("ui-round-trip")
        window._save_clicker_profile()
        window.ui["ac_cps"].set_value(33)
        window._sync_clicker_controls()
        window._load_clicker_profile()
        self.assertEqual(window.service.settings["clicker"]["cps"], 12.0)
        self.assertEqual(window.service.settings["clicker"]["duration"], 2.5)
        window._delete_clicker_profile()
        window.ui["ac_advanced"].set_expanded(False)
        window.ui["ac_duration"].set_value(0)
        window.ui["ac_pointer"].set_selected(0)
        window.ui["ac_jitter"].set_value(0)
        window.ui["ac_pause_lock"].set_active(True)
        window.ui["ac_cps"].set_value(12)
        window.ui["ac_duty"].set_value(50)
        window._sync_clicker_controls()
        pump(0.03)

    def test_rapid_page_palette_and_onboarding_toggles(self):
        for _ in range(8):
            for key in ("overview", "automation", "rotation", "socd",
                        "macros", "alts", "settings"):
                self.win.show_page(key)
            self.win.open_command()
            self.win.close_command()
        pump(0.05)
        self.assertFalse(self.win.cmd_revealer.get_reveal_child())
        for _ in range(3):
            self.win._show_onboarding()
            pump(0.05)
            self.assertIsNotNone(self.win._onboard)
            self.win._onboard.close()
            pump(0.05)
        self.assertIsNone(self.win._onboard)

    def test_missing_and_malformed_css_cannot_stop_the_shell(self):
        old_path = studio.CSS_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                missing = Path(tmp) / "missing.css"
                studio.CSS_PATH = missing
                self.assertFalse(self.app.apply_accent("#e7a6b4"))
                bad = Path(tmp) / "bad.css"
                bad.write_text("window { this-is-not-a-property: ;",
                               encoding="utf-8")
                studio.CSS_PATH = bad
                self.assertFalse(self.app.apply_accent("#e7a6b4"))
            studio.CSS_PATH = old_path
            self.assertTrue(self.app.apply_accent("#e7a6b4"))
        finally:
            studio.CSS_PATH = old_path
            self.app.apply_accent("#e7a6b4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
