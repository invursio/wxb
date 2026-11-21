#!/usr/bin/env python3
"""Focused checks for the WXB Studio service.

Run with:  python3 test_studio.py
No display is required: the service takes a sink factory.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)
import wxb_studio_service as svc

print(wxb_test_isolation.assert_isolated("studio"))


class FakeSink:
    def __init__(self):
        self.clicks = 0
        self.pressed = False
        self.keys = []
        self.released = 0
        self._lock = threading.Lock()

    def press_mouse(self, button):
        with self._lock:
            self.pressed = True

    def release_mouse(self, button=None):
        with self._lock:
            if self.pressed and button is not None:
                self.clicks += 1
            self.pressed = False
            self.released += 1

    def tap_key(self, token):
        with self._lock:
            self.keys.append(str(token))
        return True

    def release_keys(self):
        pass

    def release_all(self):
        self.release_mouse(None)


class SettingsTests(unittest.TestCase):
    def test_socd_defaults_are_neutral_when_a_profile_never_sets_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text(json.dumps({"clicker": {"cps": 31}}),
                            encoding="utf-8")
            loaded = svc.load_settings(path)
        self.assertEqual(loaded["socd"], {
            "mode_h": "Neutral", "mode_v": "Neutral"})
        state = svc.engine.SocdEngine()
        self.assertEqual(state.modes, {"horizontal": "Neutral",
                                      "vertical": "Neutral"})
        self.assertEqual(state.press("keyboard", 30), [("down", 30)])
        self.assertEqual(state.press("keyboard", 32), [("up", 30)])
        self.assertEqual(state.press("keyboard", 17), [("down", 17)])
        self.assertEqual(state.press("keyboard", 31), [("up", 17)])
        self.assertFalse(any(state.snapshot()["out"].values()))

    def test_socd_modes_apply_independently_per_axis_and_partial_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text(json.dumps({
                "socd": {"mode_h": "Last win"},
            }), encoding="utf-8")
            horizontal_only = svc.load_settings(path)
            path.write_text(json.dumps({
                "socd": {"mode_v": "Last win"},
            }), encoding="utf-8")
            vertical_only = svc.load_settings(path)
        self.assertEqual(horizontal_only["socd"], {
            "mode_h": "Last win", "mode_v": "Neutral"})
        self.assertEqual(vertical_only["socd"], {
            "mode_h": "Neutral", "mode_v": "Last win"})

        # A=LEFT(30), D=RIGHT(32), W=UP(17), S=DOWN(31). Horizontal uses
        # last-input priority while vertical stays neutral for both holds.
        state = svc.engine.SocdEngine(modes={
            "horizontal": "Last win", "vertical": "Neutral"})
        self.assertEqual(state.press("keyboard", 30), [("down", 30)])
        self.assertEqual(state.press("keyboard", 32),
                         [("up", 30), ("down", 32)])
        self.assertEqual(state.press("keyboard", 17), [("down", 17)])
        self.assertEqual(state.press("keyboard", 31), [("up", 17)])
        self.assertTrue(state.snapshot()["out"]["RIGHT"])
        self.assertFalse(state.snapshot()["out"]["UP"])
        self.assertFalse(state.snapshot()["out"]["DOWN"])
        # Switching only the vertical axis must resolve only that axis and
        # leave horizontal's last-input-priority output untouched.
        self.assertEqual(state.set_mode("vertical", "Last win"),
                         [("down", 31)])
        self.assertTrue(state.snapshot()["out"]["RIGHT"])
        self.assertEqual(state.set_mode("horizontal", "Neutral"),
                         [("up", 32)])
        self.assertTrue(state.snapshot()["out"]["DOWN"])

    def test_defaults_are_merged_without_dropping_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text(json.dumps({"accent": "sage",
                                        "clicker": {"cps": 31}}),
                            encoding="utf-8")
            got = svc.load_settings(path)
            self.assertEqual(got["accent"], "sage")
            self.assertEqual(got["clicker"]["cps"], 31)
            self.assertEqual(got["clicker"]["duty"], 50)
            self.assertIn("rotation", got)
            self.assertEqual(got["socd"]["mode_h"], "Neutral")

    def test_bad_json_and_bad_accent_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text("{not json", encoding="utf-8")
            got = svc.load_settings(path)
            self.assertEqual(got["accent"], "cyan")
            self.assertIs(got["onboarded"], False)

    def test_save_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            svc.save_settings({"version": 1, "accent": "rose"}, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            self.assertEqual(json.loads(path.read_text())["accent"], "rose")

    def test_legacy_rose_accent_migrates_to_the_new_signal_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text(json.dumps({"accent": "rose"}), encoding="utf-8")
            self.assertEqual(svc.load_settings(path)["accent"], "cyan")
            path.write_text(json.dumps({"accent": "rose",
                                        "appearance_revision": 2}),
                            encoding="utf-8")
            self.assertEqual(svc.load_settings(path)["accent"], "rose")

    def test_clicker_validation(self):
        self.assertEqual(svc.validate_clicker(12, 50, "left")[1], "")
        self.assertEqual(svc.validate_clicker(12, 50, "middle")[1], "")
        for cps, duty, button in ((0, 50, "left"), (1001, 50, "left"),
                                  (12, 0, "left"), (12, 100, "left"),
                                  (12, 50, "thumb"), ("x", 50, "left")):
            _, err = svc.validate_clicker(cps, duty, button)
            self.assertTrue(err, (cps, duty, button))

    def test_rotation_validation(self):
        good, err = svc.validate_rotation({"slot_a": 1, "slot_b": 2,
                                           "delay_a": 10, "delay_b": 20,
                                           "mode": "toggle", "max_rot": 3})
        self.assertFalse(err)
        self.assertEqual(good["key_a"], "1")
        self.assertEqual(good["delay_a"], 0.01)
        for bad in ({"slot_a": 11}, {"slot_b": -1}, {"delay_a": -1},
                    {"mode": "other"}):
            values = {"slot_a": 1, "slot_b": 2, "delay_a": 10,
                      "delay_b": 20, "mode": "toggle", "max_rot": 0}
            values.update(bad)
            _, error = svc.validate_rotation(values)
            self.assertTrue(error, bad)


class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.sink = FakeSink()
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

    def test_clicker_starts_counts_and_stops(self):
        ok, message = self.service.start_clicker(50, 50, "left")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: self.sink.clicks >= 2))
        self.assertTrue(self.service.ac_running)
        self.service.stop_clicker()
        self.assertFalse(self.service.ac_running)
        self.assertGreaterEqual(self.sink.clicks, 2)
        self.assertFalse(self.sink.pressed)

    def test_clicker_refuses_a_second_worker(self):
        self.service.start_clicker(20, 50, "left")
        first = self.service._ac_thread
        ok, _ = self.service.start_clicker(20, 50, "left")
        self.assertTrue(ok)
        self.assertIs(self.service._ac_thread, first)
        self.service.stop_clicker()

    def test_rotation_uses_injected_sink_and_stops(self):
        self.assertFalse(self.service.macro_enabled)
        refused, message = self.service.start_rotation(
            {"slot_a": 1, "slot_b": 2, "delay_a": 5, "delay_b": 5})
        self.assertFalse(refused)
        self.assertIn("Enable the rotation macro", message)
        self.assertTrue(self.service.set_macro_enabled(True)[0])
        ok, message = self.service.start_rotation(
            {"slot_a": 1, "slot_b": 2, "delay_a": 5, "delay_b": 5,
             "mode": "toggle", "max_rot": 2})
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: not self.service.rot_running))
        self.assertGreaterEqual(len(self.sink.keys), 4)
        self.assertEqual(self.sink.keys[:4], ["1", "2", "1", "2"])
        self.service.stop_rotation()

    def test_socd_modes_are_engine_labels(self):
        modes = {"horizontal": "Last win", "vertical": "Priority"}
        state = svc.engine.SocdEngine(modes=modes)
        self.assertEqual(state.modes["horizontal"], "Last win")
        self.assertEqual(state.modes["vertical"], "Priority")

    def test_stop_all_releases_everything(self):
        self.service.start_clicker(50, 50, "left")
        self.assertTrue(self.wait_for(lambda: self.sink.clicks > 0))
        self.service.stop_all()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.sink.pressed)

    def test_snapshot_shape(self):
        snap = self.service.snapshot()
        for key in ("clicker", "rotation", "socd", "sober", "alts"):
            self.assertIn(key, snap)
        self.assertFalse(snap["clicker"]["running"])



class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.service = svc.StudioService(path=self.path,
                                         sink_factory=FakeSink)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_named_configs_round_trip(self):
        self.service.settings["clicker"].update({"cps": 987.65, "duty": 12.5})
        ok, message = self.service.save_config("My setup")
        self.assertTrue(ok, message)
        self.assertEqual(self.service.list_configs(), ["My setup"])
        self.service.settings["clicker"]["cps"] = 20
        ok, message = self.service.load_config("My setup")
        self.assertTrue(ok, message)
        self.assertAlmostEqual(self.service.settings["clicker"]["cps"],
                               987.65, places=2)
        self.assertTrue((self.path.parent / "configs" /
                         "My setup.json").is_file())
        self.assertFalse(self.service.save_config("../escape")[0])

    def test_a_damaged_named_config_never_replaces_live_settings(self):
        self.service.settings["clicker"]["cps"] = 321
        config_dir = self.path.parent / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "Broken.json").write_text('{"clicker": [', encoding="utf-8")
        ok, message = self.service.load_config("Broken")
        self.assertFalse(ok)
        self.assertIn("Could not read config", message)
        self.assertEqual(self.service.settings["clicker"]["cps"], 321)
        (config_dir / "Array.json").write_text("[]", encoding="utf-8")
        ok, message = self.service.load_config("Array")
        self.assertFalse(ok)
        self.assertIn("settings object", message)
        self.assertEqual(self.service.settings["clicker"]["cps"], 321)

    def test_hotkeys_are_configurable_and_validated(self):
        ok, message = self.service.set_hotkeys({
            "toggle_clicker": "Ctrl+Shift+F8",
            "toggle_rotation": "Alt+1",
            "toggle_socd": "",
            "stop_all": "F10",
        })
        self.assertTrue(ok, message)
        self.assertEqual(self.service.settings["hotkeys"]["toggle_clicker"],
                         "ctrl+shift+f8")
        self.assertEqual(self.service.settings["hotkeys"]["toggle_rotation"],
                         "alt+1")
        self.assertEqual(self.service.settings["hotkeys"]["toggle_socd"], "")
        ok, message = self.service.set_hotkeys({
            "toggle_clicker": "Alt+1",
            "toggle_rotation": "Alt+1",
            "toggle_socd": "F8",
            "stop_all": "F9",
        })
        self.assertFalse(ok)
        self.assertIn("both use", message)
        self.assertEqual(svc.normalize_hotkey("Ctrl + Shift + F8"),
                         "ctrl+shift+f8")
        self.assertEqual(svc.pynput_hotkey("Ctrl + Shift + F8"),
                         "<ctrl>+<shift>+<f8>")
        self.assertEqual(svc.normalize_hotkey("Ctrl + F20"), "ctrl+f20")
        self.assertEqual(svc.normalize_hotkey("Ctrl + F21"), "")
    def test_clicker_mode_quick_play_and_config_rename(self):
        self.assertEqual(self.service.settings["clicker"]["mode"], "toggle")
        self.service.settings["clicker"]["mode"] = "hold"
        self.service.save()
        got = svc.normalize_settings(self.service.settings)
        self.assertEqual(got["clicker"]["mode"], "hold")
        self.assertEqual(self.service.parse_place("123456"), ("123456", ""))
        self.assertEqual(
            self.service.parse_place(
                "https://www.roblox.com/games/123456/Place"),
            ("123456", ""))
        self.assertEqual(
            self.service.parse_place(
                "roblox://placeId=123456&gameInstanceId=abc-1"),
            ("123456", "abc-1"))
        self.assertIsNone(self.service.parse_place("not a place"))
        self.service.remember_place("123456", "abc-1")
        self.assertEqual(self.service.settings["places"][0]["id"], "123456")
        ok, _ = self.service.save_config("rename me")
        self.assertTrue(ok)
        ok, message = self.service.rename_config("rename me", "renamed")
        self.assertTrue(ok, message)
        self.assertIn("renamed", self.service.list_configs())
        self.assertNotIn("rename me", self.service.list_configs())

    def test_clicker_accepts_full_fractional_range(self):
        good, error = svc.validate_clicker(0.01, 99.99, "left")
        self.assertTrue(good, error)
        self.assertEqual(good["cps"], 0.01)
        self.assertEqual(good["duty"], 99.99)
        for cps, duty in ((0, 50), (1000.01, 50), (10, 0), (10, 100)):
            self.assertTrue(svc.validate_clicker(cps, duty, "left")[1])


class LaunchTests(unittest.TestCase):
    def test_launch_guard_refuses_overlapping_sober_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "s.json",
                                        auto_start=False)
            try:
                service._sober_launching = True
                ok, message = service.launch_sober()
                self.assertTrue(ok)
                self.assertIn("already starting", message)
                self.assertIsNone(service._sober_thread)
                self.assertFalse(service.ensure_sober()[0])
            finally:
                service.shutdown()

    def test_stop_all_never_kills_the_sober_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "s.json")
            with mock.patch.object(service, "stop_sober") as stop_sober:
                service.stop_all()
            stop_sober.assert_not_called()


    def test_sober_command_exposes_tmp_for_flatpak_x11(self):
        launcher = svc.lution_launcher
        self.assertIsNotNone(launcher)
        with mock.patch.object(launcher, "BWRAP", "/usr/bin/bwrap"), \
             mock.patch.object(launcher, "userns_available", return_value=True), \
             mock.patch.object(launcher, "sober_is_running", return_value=False):
            args = launcher.build_sober_command()
        self.assertIn("--filesystem=/tmp", args)

    def test_gamescope_default_is_named_off_and_opt_in(self):
        self.assertFalse(svc.GAMESCOPE_DEFAULT)
        self.assertIs(svc.DEFAULT_SETTINGS["gamemode"]["gamescope"],
                      svc.GAMESCOPE_DEFAULT)
        self.assertFalse(
            svc.normalize_settings({})["gamemode"]["gamescope"])
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            try:
                self.assertFalse(service.launch_effects()["gamescope"])
                self.assertTrue(
                    service.set_gamemode_effect("gamescope", True))
                self.assertTrue(service.launch_effects()["gamescope"])
                service.save()
                self.assertTrue(
                    svc.load_settings(service.path)["gamemode"]["gamescope"])
                self.assertTrue(
                    service.set_gamemode_effect("gamescope", False))
                self.assertFalse(service.launch_effects()["gamescope"])
            finally:
                service.shutdown()

    def test_switches_default_off_and_round_trip(self):
        got = svc.normalize_settings({
            "gamemode": {"mangohud": True, "gamemode": "yes",
                         "gamescope": True, "bogus": True},
        })
        self.assertEqual(got["gamemode"], {
            "mangohud": True, "gamemode": False, "gamescope": True,
        })
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            try:
                self.assertEqual(service.launch_effects(), {
                    "mangohud": False, "gamemode": False,
                    "gamescope": False,
                })
                self.assertTrue(service.set_gamemode_effect("gamemode", True))
                self.assertTrue(service.set_gamemode_effect("mangohud", True))
                service.save()
                reloaded = svc.load_settings(service.path)
                self.assertEqual(reloaded["gamemode"], {
                    "mangohud": True, "gamemode": True,
                    "gamescope": False,
                })
                self.assertFalse(service.set_gamemode_effect("bogus", True))
            finally:
                service.shutdown()

    def test_active_effects_are_false_when_the_tool_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            try:
                service.settings["gamemode"] = {
                    "mangohud": True, "gamemode": True, "gamescope": True}
                status = {
                    "mangohud": {"available": False, "detail": "missing"},
                    "gamemode": {"available": True, "detail": "ready"},
                    "gamescope": {"available": False, "detail": "missing"},
                }
                with mock.patch.object(service, "gamemode_status",
                                       return_value=status):
                    self.assertEqual(service.active_launch_effects(), {
                        "mangohud": False, "gamemode": True,
                        "gamescope": False,
                    })
            finally:
                service.shutdown()

    def test_launch_command_adds_mangohud_only_when_enabled(self):
        launcher = svc.lution_launcher
        self.assertIsNotNone(launcher)
        with mock.patch.object(launcher, "BWRAP", "/usr/bin/bwrap"), \
             mock.patch.object(launcher, "userns_available", return_value=True), \
             mock.patch.object(launcher, "sober_is_running", return_value=False):
            plain = launcher.build_sober_command()
            hud = launcher.build_sober_command(mangohud=True)
        self.assertNotIn("--env=MANGOHUD=1", plain)
        self.assertIn("--env=MANGOHUD=1", hud)

    def test_launcher_wraps_gamescope_only_when_enabled(self):
        launcher = svc.lution_launcher
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gamescope"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ,
                                 {"WXB_GAMESCOPE": str(fake)}, clear=False), \
                 mock.patch.object(launcher, "build_sober_command",
                                   return_value=["flatpak", "run", "app"]), \
                 mock.patch.object(launcher, "find_gamemode_library",
                                   return_value="/tmp/libgamemodeauto.so.0"), \
                 mock.patch.object(launcher.subprocess, "Popen") as popen:
                launcher.launch(gamescope=True, gamemode=True)
                args = popen.call_args.args[0]
                env = popen.call_args.kwargs["env"]
                self.assertEqual(args[:3], [
                    "/bin/sh", "-c", 'unset LD_PRELOAD; exec "$@"'])
                self.assertEqual(args[4:6], [str(fake), "--"])
                self.assertEqual(args[6:], ["flatpak", "run", "app"])
                self.assertTrue(env["LD_PRELOAD"].startswith(
                    "/tmp/libgamemodeauto.so.0"))

                launcher.launch()
                args = popen.call_args.args[0]
                env = popen.call_args.kwargs["env"]
                self.assertEqual(args, ["flatpak", "run", "app"])
                self.assertIsNone(env)

    def test_launcher_uses_the_real_flatpak_gamescope_extension(self):
        """The extension is a real compositor, not a host wrapper.

        Its loader and hard-coded extension path are essential: invoking the
        ELF with the host loader only produces a library error.  This locks in
        the bubblewrap prefix that supplies the matching runtime, the real
        Xwayland, and the private X11 socket used by the nested game.
        """
        launcher = svc.lution_launcher
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext = root / "ext"
            runtime = root / "runtime"
            binary = ext / "bin/gamescope-brokey"
            xwayland = ext / "bin/Xwayland"
            loader = runtime / "files/lib64/ld-linux-x86-64.so.2"
            for path in (binary, xwayland, loader):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
                path.chmod(0o755)
            (runtime / "files/lib/x86_64-linux-gnu").mkdir(parents=True)
            mount = root / "mount"
            mount.mkdir()
            helpers = root / "helpers"
            x11 = root / "x11"
            x11.mkdir()
            socket = root / "X0"
            socket.touch()
            extension = {
                "extension": ext,
                "runtime": runtime / "files",
                "binary": binary,
                "xwayland": xwayland,
                "loader": loader,
                "library_path": (ext / "lib", runtime / "files/lib/x86_64-linux-gnu"),
                "branch": "26.08",
            }
            with mock.patch.object(launcher, "BWRAP", "/usr/bin/bwrap"), \
                 mock.patch.object(launcher, "_GAMESCOPE_MOUNT", mount), \
                 mock.patch.object(launcher, "find_gamescope",
                                   return_value=str(binary)), \
                 mock.patch.object(launcher, "_gamescope_extension",
                                   return_value=extension), \
                 mock.patch.object(launcher, "_extension_launch_dir",
                                   return_value=(helpers, x11, socket)):
                args = launcher._wrap_gamescope(
                    ["flatpak", "run", "app"])
        self.assertEqual(args[0], "/usr/bin/bwrap")
        self.assertIn("--library-path", args)
        self.assertIn(str(binary), args)
        self.assertIn("--ro-bind", args)
        self.assertEqual(args[-4:], ["--", "flatpak", "run", "app"])

    def test_service_passes_each_enabled_switch_to_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            fake_proc = mock.Mock(pid=4321)
            effects = {"mangohud": True, "gamemode": True,
                       "gamescope": True}
            try:
                with mock.patch.object(type(service), "sober_running",
                                       new_callable=mock.PropertyMock,
                                       return_value=False), \
                     mock.patch.object(service, "active_launch_effects",
                                       return_value=effects), \
                     mock.patch.object(svc.lution_sober, "ensure_sober",
                                       return_value=(True, "")), \
                     mock.patch.object(svc.lution_launcher, "launch_plain",
                                       return_value=fake_proc) as launch:
                    self.assertTrue(service.launch_sober(plain=True)[0])
                    service._sober_thread.join(2)
                launch.assert_called_once()
                called = launch.call_args.kwargs
                self.assertTrue(called["mangohud"])
                self.assertTrue(called["gamemode"])
                self.assertTrue(called["gamescope"])
                self.assertEqual(service._sober_error, "")
            finally:
                service.shutdown()

    def test_elevated_wxb_launches_game_as_owner(self):
        with mock.patch.object(svc.os, "geteuid", return_value=0), \
             mock.patch.dict(svc.os.environ, {"WXB_OWNER_UID": "1000"}, clear=False), \
             mock.patch.object(svc.pwd, "getpwuid",
                               return_value=mock.Mock(pw_gid=1000)), \
             mock.patch.object(svc.os, "setgroups") as setgroups, \
             mock.patch.object(svc.os, "setgid") as setgid, \
             mock.patch.object(svc.os, "setuid") as setuid:
            drop = svc._owner_launch_preexec()
            self.assertIsNotNone(drop)
            drop()
        setgroups.assert_called_once_with([])
        setgid.assert_called_once_with(1000)
        setuid.assert_called_once_with(1000)

    def test_unprivileged_wxb_does_not_attempt_to_setuid(self):
        with mock.patch.object(svc.os, "geteuid", return_value=1000):
            self.assertIsNone(svc._owner_launch_preexec())

    def test_alt_options_persist_and_cap_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alts.json"
            service = svc.StudioService(path=path)
            self.assertEqual(service.settings["alts"]["fps_cap"],
                             svc.ALT_FPS_DEFAULT)
            self.assertTrue(service.settings["alts"]["auto_tile"])
            self.assertEqual(service.settings["alts"]["tiling_style"],
                             "columns")
            service._alt_manager = mock.Mock()
            service._alt_manager.live_launches.return_value = []
            ok, message = service.set_alt_options(
                auto_tile=False, fps_cap=30, low_graphics=False,
                cpu_quota=40, tiling_style="rows")
            self.assertTrue(ok, message)
            self.assertFalse(service.settings["alts"]["auto_tile"])
            self.assertFalse(service.settings["alts"]["low_graphics"])
            self.assertEqual(service.settings["alts"]["cpu_quota"], 40)
            self.assertEqual(service.settings["alts"]["tiling_style"], "rows")
            service._alt_manager.set_cpu_quota.assert_called_once_with(40)
            saved = svc.load_settings(path)["alts"]
            self.assertEqual(saved["fps_cap"], 30)
            self.assertFalse(saved["auto_tile"])
            self.assertFalse(saved["low_graphics"])
            self.assertEqual(saved["cpu_quota"], 40)
            self.assertEqual(saved["tiling_style"], "rows")
            self.assertFalse(service.set_alt_options(fps_cap=-1)[0])
            self.assertFalse(service.set_alt_options(fps_cap="inf")[0])
            self.assertFalse(service.set_alt_options(cpu_quota=9999)[0])
            self.assertFalse(service.set_alt_options(cpu_quota="inf")[0])
            self.assertFalse(service.set_alt_options(tiling_style="diagonal")[0])
            self.assertFalse(service.settings["alts"]["auto_tile"])
            service.shutdown()

    def test_tile_alts_uses_the_saved_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_manager = mock.Mock()
            service._alt_manager.tile.return_value = (True, "Tiled")
            service.settings["alts"]["tiling_style"] = "focus-top"
            self.assertEqual(service.tile_alts(), (True, "Tiled"))
            service._alt_manager.tile.assert_called_once_with(style="focus-top")

    def test_cycle_tiling_hotkey_advances_and_tiles_each_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_manager = mock.Mock()
            service._alt_manager.tile.return_value = (True, "Tiled")
            service.settings["alts"]["tiling_style"] = "columns"
            expected = [key for key, _title in svc.ALT_TILING_CHOICES]
            expected = expected[1:] + expected[:1]
            seen = []
            for style in expected:
                ok, message = service.cycle_alt_tiling()
                self.assertTrue(ok, message)
                seen.append(service.settings["alts"]["tiling_style"])
                service._alt_manager.tile.assert_called_with(style=style)
            self.assertEqual(seen, expected)
            self.assertEqual(service.settings["alts"]["tiling_style"], "columns")
            self.assertEqual(service.hotkey_mapping()["cycle_tiling"],
                             "ctrl+shift+f10")
            ok, message = service.handle_hotkey("cycle_tiling")
            self.assertTrue(ok, message)
            self.assertEqual(service.settings["alts"]["tiling_style"], "rows")
            self.assertEqual(svc.HOTKEY_ACTIONS["cycle_tiling"],
                             "cycle_alt_tiling")
            service._alt_manager.tile.assert_called_with(style="rows")

    def test_launch_alt_writes_the_hard_cap_before_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service.settings["alts"].update({"fps_cap": 24,
                                             "auto_tile": False})
            service._alt_install = object()
            service._alt_manager = mock.Mock()
            with mock.patch.object(svc.roalt, "prepare_profile") as prepare:
                ok, message = service.launch_alt("Alt1")
            self.assertTrue(ok, message)
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.kwargs["cap"], 24)
            service._alt_manager.launch.assert_called_once_with("Alt1")

    def test_launch_alt_fails_closed_when_cap_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_install = object()
            service._alt_manager = mock.Mock()
            with mock.patch.object(
                    svc.roalt, "prepare_profile",
                    side_effect=svc.roalt.ProfileFlagsError("flags are read-only")):
                ok, message = service.launch_alt("Alt1")
            self.assertFalse(ok)
            self.assertIn("flags are read-only", message)
            service._alt_manager.launch.assert_not_called()

    def test_launch_alt_reads_the_cap_from_the_profile_at_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp,
                   "XDG_DATA_HOME": os.path.join(tmp, "data")}
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service.settings["alts"].update({"fps_cap": 24,
                                             "auto_tile": False})
            service._alt_install = SimpleNamespace(kind="native")
            service._alt_env = lambda: env
            service._alt_manager = mock.Mock()
            seen = {}

            def launch(profile):
                path = Path(tmp) / "data" / "cordial" / "profiles" / profile \
                    / "flags.json"
                seen.update(json.loads(path.read_text(encoding="utf-8")))
                return object()

            service._alt_manager.launch.side_effect = launch
            ok, message = service.launch_alt("Alt1")
            self.assertTrue(ok, message)
            self.assertEqual(seen["DFIntTaskSchedulerTargetFps"], 24)
            self.assertEqual(seen["CordialFrameRateLimit"], 24)

    def test_add_and_delete_alt_profile_updates_the_real_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp,
                   "XDG_DATA_HOME": os.path.join(tmp, "data"),
                   "XDG_CONFIG_HOME": os.path.join(tmp, "config")}
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_install = SimpleNamespace(kind="native")
            service._alt_env = lambda: env
            service._alt_manager = mock.Mock()
            service._alt_manager.running.return_value = []
            ok, message = service.add_alt_profile("Fresh")
            self.assertTrue(ok, message)
            path = (Path(tmp) / "data" / "cordial" / "profiles" / "Fresh")
            self.assertTrue(path.is_dir())
            self.assertIn("Fresh", service.snapshot()["alts"]["profiles"])
            ok, message = service.delete_alt_profile("Fresh")
            self.assertTrue(ok, message)
            self.assertFalse(path.exists())
            self.assertNotIn("Fresh", service.snapshot()["alts"]["profiles"])

    def test_delete_alt_profile_refuses_a_running_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp,
                   "XDG_DATA_HOME": os.path.join(tmp, "data")}
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_install = SimpleNamespace(kind="native")
            service._alt_env = lambda: env
            svc.roalt.create_profile("Busy", env=env)
            service._alt_manager = mock.Mock()
            service._alt_manager.running.return_value = [
                {"profile": "Busy", "running": True}]
            ok, message = service.delete_alt_profile("Busy")
            self.assertFalse(ok)
            self.assertIn("Stop Busy", message)
            self.assertTrue((Path(tmp) / "data" / "cordial" / "profiles" /
                             "Busy").is_dir())

    def test_launch_alt_explains_that_detection_is_still_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "alts.json")
            service._alt_thread = mock.Mock()
            service._alt_thread.is_alive.return_value = True
            ok, message = service.launch_alt("Alt1")
            self.assertFalse(ok)
            self.assertIn("still being detected", message)


class InputLabServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.service = svc.StudioService(path=self.path,
                                         sink_factory=FakeSink,
                                         auto_start=False)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def test_defaults_and_settings_are_bounded(self):
        snap = self.service.input_lab_snapshot()
        self.assertEqual(snap["settings"]["timeline"], False)
        self.assertEqual(snap["settings"]["visualizer"]["keys"], False)
        ok, message = self.service.set_input_lab_settings(
            timeline=True, deadzone=9, color="not-a-colour")
        self.assertTrue(ok, message)
        got = self.service.input_lab_snapshot()["settings"]
        self.assertTrue(got["timeline"])
        self.assertEqual(got["deadzone"], svc.input_lab.DEADZONE_MAX)
        self.assertEqual(got["visualizer"]["color"], "#9fcaff")
        self.assertFalse(self.service._visualizer.running)

    def test_profile_round_trip_is_private_and_has_no_raw_events(self):
        mappings = [{"source": "Space", "target": "Enter"},
                    {"source": "Tab", "target": "F1"}]
        ok, message = self.service.save_input_lab_profile(
            "Tutorial", mappings, mode="hold")
        self.assertTrue(ok, message)
        path = self.service.input_lab_profile_path("Tutorial")
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["mappings"], mappings)
        self.assertNotIn("events", payload)
        self.assertNotIn("raw", payload)
        self.assertEqual(self.service.list_input_lab_profiles(), ["Tutorial"])
        ok, message = self.service.load_input_lab_profile("Tutorial")
        self.assertTrue(ok, message)
        snap = self.service.input_lab_snapshot()
        self.assertEqual(snap["profile"]["mode"], "hold")
        self.assertEqual(snap["settings"]["active_profile"], "Tutorial")
        self.assertTrue(self.service.delete_input_lab_profile("Tutorial")[0])
        self.assertEqual(self.service.list_input_lab_profiles(), [])
        self.assertEqual(self.service.settings["input_lab"]["active_profile"],
                         "")

    def test_symlinked_profile_is_refused(self):
        directory = self.service.input_lab_profile_dir()
        directory.mkdir(parents=True, exist_ok=True)
        real = directory / "real.json"
        real.write_text(json.dumps({"name": "Real", "mappings": [
            {"source": "Space", "target": "Enter"}]}),
            encoding="utf-8")
        (directory / "Linked.json").symlink_to(real)
        ok, message = self.service.load_input_lab_profile("Linked")
        self.assertFalse(ok)
        self.assertIn("symlink", message.lower())

    def test_safe_reset_stops_inputs_and_disables_remaps(self):
        self.service.save_input_lab_profile(
            "Tutorial", [{"source": "Space", "target": "Enter"}])
        self.service.load_input_lab_profile("Tutorial")
        self.assertTrue(self.service.start_clicker(20, 20, "left")[0])
        self.assertTrue(self.service.ac_running)
        fake_controller = mock.Mock(running=True)
        fake_visualizer = mock.Mock()
        self.service._controller = fake_controller
        self.service._visualizer = fake_visualizer
        ok, message = self.service.safe_input_reset()
        self.assertTrue(ok, message)
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.service.socd_running)
        self.assertEqual(self.service.settings["input_lab"]["active_profile"],
                         "")
        fake_controller.stop.assert_called_once()
        fake_visualizer.stop.assert_called_once()
        self.assertIn("released", message.lower())

    def test_controller_test_refuses_without_readable_input(self):
        with mock.patch.object(
                self.service, "input_lab_capabilities",
                return_value={"controller": False,
                              "reason": "No readable /dev/input devices"}):
            ok, message = self.service.start_controller_test()
        self.assertFalse(ok)
        self.assertIn("readable", message.lower())
        self.assertEqual(self.service.input_lab_snapshot()["capture"], "off")
        self.assertIn("readable", self.service.input_lab_snapshot()["error"])

    def test_controller_test_start_stop_uses_read_only_monitor(self):
        monitor = mock.Mock()
        monitor.running = True
        monitor.start.return_value = (True, "Controller test started.")
        monitor.snapshot.return_value = {
            "running": True, "connected": True, "name": "Test pad",
            "buttons": ["A"], "axes": {"LX": {"value": 0.4}}}
        with mock.patch.object(
                self.service, "input_lab_capabilities",
                return_value={"controller": True}), \
                mock.patch.object(svc.input_lab, "ControllerMonitor",
                                  return_value=monitor):
            ok, message = self.service.start_controller_test()
            self.assertTrue(ok, message)
            snap = self.service.input_lab_snapshot()
            self.assertEqual(snap["capture"], "controller")
            self.assertEqual(snap["controller"]["name"], "Test pad")
            self.service.stop_controller_test()
        monitor.stop.assert_called_once()
        self.assertEqual(self.service.input_lab_snapshot()["capture"], "off")

    def test_visualizer_starts_only_for_enabled_switches_and_reports_error(self):
        visualizer = mock.Mock()
        visualizer.start.return_value = (True, "Visualizer capture started.")
        visualizer.snapshot.return_value = {
            "running": True, "keys": True, "clicks": False,
            "error": ""}
        self.service._visualizer = visualizer
        ok, message = self.service.set_input_lab_settings(
            keys=True, clicks=False)
        self.assertTrue(ok, message)
        visualizer.start.assert_called_once_with(True, False)
        self.assertEqual(self.service.input_lab_snapshot()["capture"],
                         "visualizer")
        visualizer.start.return_value = (False, "No X11 display")
        ok, message = self.service.set_input_lab_settings(
            keys=True, clicks=False)
        self.assertFalse(ok)
        self.assertIn("x11", message.lower())
        self.assertFalse(
            self.service.settings["input_lab"]["visualizer"]["keys"])



class SettingsHardeningTests(unittest.TestCase):
    def load_raw(self, raw):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text(raw, encoding="utf-8")
            return svc.load_settings(path)

    def test_missing_empty_truncated_and_non_object_files_are_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(svc.load_settings(missing), svc.DEFAULT_SETTINGS)
            for raw in ("", "   ", "{not json", "[]", "null", '"text"'):
                got = self.load_raw(raw)
                self.assertEqual(got["clicker"], svc.DEFAULT_SETTINGS["clicker"])
                self.assertEqual(got["rotation"], svc.DEFAULT_SETTINGS["rotation"])
                self.assertFalse(got["onboarded"])

    def test_out_of_range_values_are_clamped_not_propagated(self):
        raw = {
            "clicker": {"cps": 9999, "duty": -4, "button": "thumb"},
            "rotation": {"slot_a": -2, "slot_b": 42, "delay_a": -1,
                         "delay_b": 10 ** 12, "mode": "HOLD", "max_rot": -9},
            "socd": {"mode_h": "last", "mode_v": "nonsense"},
            "alts": {"cpu_quota": 10 ** 9},
        }
        got = svc.normalize_settings(raw)
        self.assertEqual(got["clicker"], {
            "cps": 1000.0, "duty": 0.01, "button": "left",
            "mode": "toggle", "duration": 0, "pointer_mode": "follow",
            "jitter": 0, "fixed_x": 0, "fixed_y": 0, "failsafe": True,
            "failsafe_corner": "top-left", "pause_on_lock": True,
            "rate_mode": "exact", "cps_max": 1000.0,
            "start_delay_ms": 0, "stop_after_clicks": 0,
            "timing_precision": "balanced",
            "failsafe_margin": 2, "app_filter_mode": "off",
            "app_blacklist": [], "app_whitelist": [],
        })
        self.assertEqual(got["rotation"], {
            "slot_a": 0, "slot_b": 10, "delay_a": 0, "delay_b": 5000,
            "mode": "hold", "max_rot": 0,
        })
        self.assertEqual(got["socd"], {
            "mode_h": "Last win", "mode_v": "Neutral"})
        self.assertEqual(got["alts"]["cpu_quota"], 1600)

    def test_legacy_deck_shape_starts_clean(self):
        # A file from the removed deck: old keys, old units, no studio
        # sections.  It must not accidentally seed today's engines.
        raw = json.dumps({
            "theme": "Nocturne", "theme_choice": "Noir",
            "ac_cps": 999, "ac_cdc": 0, "ac_button": "Right",
            "m_slot_a": 99, "m_slot_b": -1, "m_delay_a": 9000,
            "macro_enabled": True, "maxrot": "9999",
            "hotkeys": {"ac": ["F6"]}, "live": {"ac_cps": 999},
            "onboarded": True,
        })
        got = self.load_raw(raw)
        self.assertEqual(got["clicker"], svc.DEFAULT_SETTINGS["clicker"])
        self.assertEqual(got["rotation"], svc.DEFAULT_SETTINGS["rotation"])
        self.assertEqual(got["socd"], svc.DEFAULT_SETTINGS["socd"])
        self.assertFalse(got["onboarded"])

    def test_malformed_sections_and_non_finite_numbers_become_defaults(self):
        raw = json.dumps({
            "clicker": ["not", "a", "mapping"],
            "rotation": "nope",
            "socd": {"mode_h": ["Last win"], "mode_v": None},
            "alts": {"cpu_quota": float("nan")},
            "onboarded": "yes",
            "accent": "not-accent",
        }, allow_nan=True)
        got = self.load_raw(raw)
        self.assertEqual(got["clicker"], svc.DEFAULT_SETTINGS["clicker"])
        self.assertEqual(got["rotation"], svc.DEFAULT_SETTINGS["rotation"])
        self.assertEqual(got["socd"]["mode_h"], "Neutral")
        self.assertEqual(got["socd"]["mode_v"], "Neutral")
        self.assertEqual(got["alts"]["cpu_quota"], 0)
        self.assertIs(got["onboarded"], False)
        self.assertEqual(got["accent"], "cyan")

    def test_service_constructs_from_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "studio.json"
            path.write_text('{"clicker": NaN, "rotation": []', encoding="utf-8")
            service = svc.StudioService(path=path, sink_factory=FakeSink)
            try:
                self.assertEqual(service.settings["clicker"]["cps"], 12)
                self.assertIn("clicker", service.snapshot())
            finally:
                service.shutdown()


class LifecycleHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.sink = FakeSink()
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

    def test_repeated_clicker_start_stop_cycles(self):
        for _ in range(5):
            ok, message = self.service.start_clicker(100, 50, "left")
            self.assertTrue(ok, message)
            self.assertTrue(self.wait_for(lambda: self.sink.clicks > 0))
            self.service.stop_clicker()
            self.assertFalse(self.service.ac_running)
        self.assertGreaterEqual(self.sink.clicks, 5)

    def test_stopping_idle_engines_is_safe_and_idempotent(self):
        for _ in range(3):
            self.service.stop_clicker()
            self.service.stop_rotation()
            self.service.stop_socd()
            self.service.stop_all()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.service.rot_running)
        self.assertFalse(self.service.socd_running)

    def test_hotkey_f6_repeats_and_f9_stops_everything(self):
        ok, message = self.service.handle_hotkey("F6")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: self.service.ac_running))
        ok, message = self.service.handle_hotkey("f6")
        self.assertTrue(ok, message)
        self.assertFalse(self.service.ac_running)
        self.service.handle_hotkey("f6")
        self.service.handle_hotkey("f9")
        self.service.handle_hotkey("f9")
        self.assertFalse(self.service.ac_running)
        ok, message = self.service.handle_hotkey("not-a-key")
        self.assertFalse(ok)
        self.assertIn("Unknown", message)

    def test_hotkey_f7_and_f8_are_repeatable(self):
        refused, message = self.service.handle_hotkey("f7")
        self.assertFalse(refused)
        self.assertIn("Enable the rotation macro", message)
        self.assertTrue(self.service.set_macro_enabled(True)[0])
        ok, message = self.service.handle_hotkey("f7")
        self.assertTrue(ok, message)
        self.service.handle_hotkey("f7")
        self.assertFalse(self.service.rot_running)
        # F8 may be unavailable without /dev/uinput; it must answer, not raise.
        ok, message = self.service.handle_hotkey("f8")
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(message, str)
        self.service.stop_socd()

    def test_shutdown_with_running_clicker_releases_and_joins(self):
        self.assertTrue(self.service.start_clicker(100, 50, "left")[0])
        self.assertTrue(self.wait_for(lambda: self.service.ac_running))
        self.service.shutdown()
        self.assertFalse(self.service.ac_running)
        self.assertFalse(self.sink.pressed)
        self.assertFalse(any(t.name == "wxb-studio-clicks" and t.is_alive()
                             for t in threading.enumerate()))

    def test_macro_master_switch_stops_and_gates_the_rotation(self):
        self.assertFalse(self.service.settings["macro_enabled"])
        self.assertFalse(self.service.snapshot()["rotation"]["enabled"])
        self.assertTrue(self.service.set_macro_enabled(True)[0])
        self.assertTrue(self.service.settings["macro_enabled"])
        self.assertTrue(self.service.snapshot()["rotation"]["enabled"])
        self.assertTrue(self.service.start_rotation(
            {"slot_a": 1, "slot_b": 2, "delay_a": 5, "delay_b": 5})[0])
        self.assertTrue(self.wait_for(lambda: self.service.rot_running))
        self.assertTrue(self.service.set_macro_enabled(False)[0])
        self.assertFalse(self.service.rot_running)
        self.assertFalse(self.service.settings["macro_enabled"])

    def test_hold_mode_starts_on_press_and_stops_on_release(self):
        self.service.settings["clicker"]["mode"] = "hold"
        self.service.settings["macro_enabled"] = True
        self.service.settings["rotation"]["mode"] = "hold"
        self.service.save()
        self.service._hotkey_combo_keys = {
            "toggle_clicker": {"f6"}, "toggle_rotation": {"f7"}}
        self.service._hotkey_activated("toggle_clicker")
        self.assertTrue(self.wait_for(lambda: self.service.ac_running))
        self.assertIn("toggle_clicker", self.service._hold_actions)
        self.service._hotkey_released("f6")
        self.assertFalse(self.service.ac_running)
        self.assertNotIn("toggle_clicker", self.service._hold_actions)
        self.service._hotkey_activated("toggle_rotation")
        self.assertTrue(self.wait_for(lambda: self.service.rot_running))
        self.service._hotkey_released("f7")
        self.assertFalse(self.service.rot_running)

    def test_esc_is_a_reserved_emergency_stop(self):
        wire = self.service.hotkey_wire_mapping()
        self.assertIn("<esc>", wire)
        self.assertIn("<f9>", wire)
        self.assertTrue(self.service.start_clicker(100, 50, "left")[0])
        self.assertTrue(self.wait_for(lambda: self.service.ac_running))
        ok, message = self.service.handle_hotkey("esc")
        self.assertTrue(ok, message)
        self.assertFalse(self.service.ac_running)
        self.assertTrue(self.service.settings["macro_enabled"] is False)
        ok, message = self.service.set_hotkeys({
            "toggle_clicker": "F6", "toggle_rotation": "F7",
            "toggle_socd": "F8", "stop_all": "Esc",
        })
        self.assertFalse(ok)
        self.assertIn("reserved", message)

    def test_shutdown_is_safe_when_idle_and_repeated(self):
        self.service.shutdown()
        self.service.shutdown()
        self.assertFalse(self.service.ac_running)


class LutionCapabilityTests(unittest.TestCase):
    """The old Lution visuals, flags, Linux settings and deploy controls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "studio.json"
        self.service = svc.StudioService(path=self.path, auto_start=False)

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def not_running(self):
        return mock.patch.object(
            type(self.service), "sober_running",
            new_callable=mock.PropertyMock, return_value=False)

    def wait_for_sober(self):
        for _ in range(200):
            if not self.service._sober_maintenance:
                return
            time.sleep(0.005)
        self.fail("Sober deployment thread did not finish")

    def test_font_status_apply_restore_and_reapply(self):
        fonts = mock.Mock()
        fonts.status.return_value = {
            "apk": True, "apk_fonts": 75, "active": 75,
            "installed": "font.ttf", "overlay": "/tmp/overlay",
        }
        fonts.apply_font.return_value = ["a.ttf", "b.ttf"]
        fonts.restore_fonts.return_value = 3
        fonts.reapply_fonts.return_value = 75
        with mock.patch.object(svc, "lution_fonts", fonts):
            status = self.service.fonts_status()
            self.assertTrue(status["ok"])
            self.assertEqual(status["apk_fonts"], 75)
            self.assertEqual(self.service.apply_font("/tmp/font.ttf"),
                             (True, "Font applied to 2 Roblox faces."))
            self.assertTrue(self.service.restore_fonts()[0])
            self.assertTrue(self.service.reapply_font()[0])
        fonts.apply_font.side_effect = FileNotFoundError("no APK")
        with mock.patch.object(svc, "lution_fonts", fonts):
            ok, message = self.service.apply_font("/tmp/font.ttf")
        self.assertFalse(ok)
        self.assertIn("no APK", message)

    def test_cursor_status_apply_restore_and_refusals(self):
        cursors = mock.Mock()
        cursors.STYLES = ("Cross", "Heart")
        cursors.COLORS = {"Pink": ("#f00", "#fff")}
        cursors.SIZES = (16, 32)
        cursors.DEFAULT_STYLE = "Cross"
        cursors.DEFAULT_COLOR = "Pink"
        cursors.DEFAULT_SIZE = 32
        cursors.ONLINE_STYLES = (
            {"name": "Classic pointer", "member": "pointer_a",
             "anchor": "top"},
        )
        cursors.PACK_NAME = "Kenney Cursor Pack"
        cursors.PACK_URL = "https://example.invalid/cursors.zip"
        cursors.PACK_LICENSE = "CC0 1.0"
        cursors.PACK_CREDIT = "Kenney (kenney.nl)"
        cursors.status.return_value = {
            "active": 3, "assets": 3, "style": "Heart",
            "color": "Pink", "size": 32, "pillow": True,
        }
        cursors.apply.return_value = {"ok": True}
        cursors.apply_online.return_value = {
            "ok": True, "cursor": {"style": "Classic pointer",
                                    "mode": "online"}}
        cursors.restore.return_value = {"ok": True, "removed": 3}
        with mock.patch.object(svc, "lution_cursors", cursors):
            options = self.service.cursor_options()
            self.assertEqual(options["styles"], ["Cross", "Heart"])
            self.assertEqual(options["online"][0]["name"], "Classic pointer")
            self.assertEqual(options["pack"]["license"], "CC0 1.0")
            self.assertTrue(self.service.cursors_status()["ok"])
            self.assertTrue(self.service.apply_cursor("Heart", "Pink", 32)[0])
            self.assertTrue(self.service.apply_online_cursor(
                "Classic pointer", 32)[0])
            self.assertTrue(self.service.restore_cursors()[0])
        cursors.apply.return_value = {"ok": False, "error": "Pillow missing"}
        with mock.patch.object(svc, "lution_cursors", cursors):
            ok, message = self.service.apply_cursor("Heart", "Pink", 32)
        self.assertFalse(ok)
        self.assertIn("Pillow", message)
        cursors.apply_online.return_value = {
            "ok": False, "error": "could not download"}
        with mock.patch.object(svc, "lution_cursors", cursors):
            ok, message = self.service.apply_online_cursor(
                "Classic pointer", 32)
        self.assertFalse(ok)
        self.assertIn("download", message)

    def test_fps_cap_round_trip_and_missing_settings_refusal(self):
        fps = mock.Mock()
        fps.load_framerate_cap.return_value = "144"
        with mock.patch.object(svc, "lution_fps", fps), self.not_running():
            status = self.service.fps_status()
            self.assertEqual(status["fps"], 144)
            self.assertTrue(status["available"])
            self.assertTrue(self.service.set_fps_cap(240)[0])
            fps.save_framerate_cap.assert_called_once_with(240)
        fps.load_framerate_cap.return_value = None
        with mock.patch.object(svc, "lution_fps", fps):
            status = self.service.fps_status()
        self.assertFalse(status["available"])
        self.assertIn("Launch it once", status["error"])
        fps.save_framerate_cap.side_effect = FileNotFoundError("missing")
        with mock.patch.object(svc, "lution_fps", fps), self.not_running():
            ok, message = self.service.set_fps_cap(144)
        self.assertFalse(ok)
        self.assertIn("Launch it once", message)

    def test_fast_flags_validate_text_clean_and_refuse_while_running(self):
        flags = mock.Mock()
        flags.allowlist.return_value = {"FFlagGood"}
        flags.get_fflags.return_value = {"FFlagGood": 3}
        flags.parse_value.side_effect = lambda raw: int(raw) if raw.strip().isdigit() else raw.strip()
        flags.clean.return_value = ({"FFlagGood": 3}, ["BadFlag"])
        with mock.patch.object(svc, "lution_fflags", flags), self.not_running():
            self.assertTrue(self.service.set_lution_flags(
                {"FFlagGood": 5})[0])
            flags.save_fflags.assert_called_with({"FFlagGood": 5})
            ok, message = self.service.set_lution_flags({"BadFlag": 1})
            self.assertFalse(ok)
            self.assertIn("Not allowlisted", message)
            self.assertTrue(self.service.set_lution_flags_text(
                "FFlagGood = 7")[0])
            self.assertTrue(self.service.lution_flags_text()[0])
            self.assertTrue(self.service.clean_lution_flags()[0])
        with mock.patch.object(type(self.service), "sober_running",
                               new_callable=mock.PropertyMock,
                               return_value=True):
            ok, message = self.service.set_lution_flags({"FFlagGood": 1})
        self.assertFalse(ok)
        self.assertIn("Close Sober", message)

    def test_flag_profiles_save_load_delete_and_refuse_unknown_flags(self):
        flags = mock.Mock()
        flags.allowlist.return_value = {"FFlagGood"}
        flags.get_fflags.return_value = {"FFlagGood": 3}
        flags.save_fflags.return_value = None
        with mock.patch.object(svc, "lution_fflags", flags), self.not_running():
            self.assertTrue(self.service.save_flag_profile("Daily")[0])
            self.assertEqual(self.service.list_flag_profiles(), ["Daily"])
            flags.get_fflags.return_value = {"FFlagGood": 9}
            self.assertTrue(self.service.save_flag_profile("Daily")[0])
            flags.get_fflags.return_value = {"FFlagGood": 1}
            self.assertTrue(self.service.load_flag_profile("Daily")[0])
            flags.save_fflags.assert_called_with({"FFlagGood": 9})
            self.assertTrue(self.service.delete_flag_profile("Daily")[0])
            self.assertEqual(self.service.list_flag_profiles(), [])
            self.assertFalse(self.service.load_flag_profile("missing")[0])
            self.assertFalse(self.service.save_flag_profile("../escape")[0])

    def test_environment_validation_and_text_round_trip(self):
        env = mock.Mock()
        env.load_vars.return_value = {"MANGOHUD": "1"}
        env.valid_key.side_effect = lambda key: bool(key) and " " not in key and "=" not in key
        env.save_vars.side_effect = lambda data: data
        with mock.patch.object(svc, "lution_envvars", env):
            self.assertTrue(self.service.lution_env()["ok"])
            self.assertEqual(self.service.lution_env_text()[1], "MANGOHUD = 1")
            self.assertTrue(self.service.save_lution_env({"MANGOHUD": "1"})[0])
            ok, message = self.service.save_lution_env({"BAD KEY": "1"})
            self.assertFalse(ok)
            self.assertIn("Invalid", message)
            self.assertTrue(self.service.save_lution_env_text("A = 2")[0])
            env.save_vars.assert_called_with({"A": "2"})
            self.assertFalse(self.service.save_lution_env_text("NoEquals")[0])

    def test_runtime_status_and_explicit_sober_deployment(self):
        sober = mock.Mock()
        sober.is_installed.return_value = True
        sober.get_version.return_value = "1.7.0"
        sober.ensure_sober.return_value = (True, "already up to date")
        launcher = mock.Mock()
        launcher.crash_state.return_value = {"count": 2}
        with mock.patch.object(svc, "lution_sober", sober), \
                mock.patch.object(svc, "lution_launcher", launcher), \
                mock.patch.object(svc.shutil, "which", return_value="/usr/bin/flatpak"), \
                self.not_running():
            data = self.service.lution_runtime(refresh=True)
            self.assertEqual(data["version"], "1.7.0")
            self.assertEqual(data["crashes"], 2)
            self.assertTrue(data["flatpak"])
            ok, message = self.service.ensure_sober()
            self.assertTrue(ok, message)
            self.wait_for_sober()
            sober.ensure_sober.assert_called_once()
            self.assertEqual(self.service._sober_error, "")
        with mock.patch.object(svc.shutil, "which", return_value=None):
            ok, message = self.service.ensure_sober()
        self.assertFalse(ok)
        self.assertIn("Flatpak", message)

    def test_playtime_is_exposed_as_a_local_total(self):
        playtime = mock.Mock()
        playtime.scan.return_value = ({"123": 60}, 125, 2)
        playtime.format_duration.return_value = "2m"
        with mock.patch.object(svc, "lution_playtime", playtime):
            data = self.service.lution_playtime()
        self.assertTrue(data["ok"])
        self.assertEqual(data["seconds"], 125)
        self.assertEqual(data["sessions"], 2)
        self.assertEqual(data["label"], "2m")


class LutionOwnerBridgeTests(unittest.TestCase):
    """Elevated WXB must not own any file the desktop user needs."""

    def wait_for_sober(self):
        for _ in range(200):
            if not self.service._sober_maintenance:
                return
            time.sleep(0.005)
        self.fail("Sober deployment thread did not finish")

    def test_test_suite_is_isolated_from_real_lution_state(self):
        from lution_core import paths

        root = Path(os.environ["WXB_TEST_HOME"]).resolve()
        self.assertEqual(Path.home().resolve(), root)
        self.assertTrue(str(paths.LUTION_ROOT).startswith(str(root)))
        self.assertNotEqual(paths.LUTION_ROOT, Path("/home/user/.local/Lution"))

    def test_elevated_bridge_runs_as_owner_with_owner_home(self):
        fake = SimpleNamespace(
            returncode=0, stdout='{"ok": true, "running": true}\n',
            stderr="")
        owner = (1000, 1001, "desk", "/home/desk")
        with mock.patch.object(svc.os, "geteuid", return_value=0), \
                mock.patch.object(svc, "_desktop_owner", return_value=owner), \
                mock.patch.object(svc.os, "getgrouplist",
                                  return_value=[1000, 1001]), \
                mock.patch.object(svc.subprocess, "run",
                                  return_value=fake) as run:
            data = svc.run_lution_cli("running")
        self.assertTrue(data["ok"])
        self.assertTrue(data["running"])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["user"], 1000)
        self.assertEqual(kwargs["group"], 1001)
        self.assertEqual(kwargs["extra_groups"], [1000, 1001])
        self.assertEqual(kwargs["env"]["HOME"], "/home/desk")
        self.assertEqual(kwargs["env"]["WXB_HOME"], "/home/desk")
        self.assertEqual(kwargs["env"]["XDG_CONFIG_HOME"],
                         "/home/desk/.config")
        self.assertEqual(kwargs["env"]["XDG_DATA_HOME"],
                         "/home/desk/.local/share")
        self.assertNotIn("preexec_fn", kwargs)
        self.assertEqual(run.call_args.args[0][:4],
                         [sys.executable, "-m", "lution_core.cli", "running"])

    def test_elevated_cordial_env_uses_the_owners_session_bus(self):
        owner = (1000, 1001, "desk", "/home/desk")
        with mock.patch.object(svc.Path, "is_dir", return_value=True), \
                mock.patch.object(svc.Path, "exists", return_value=True):
            env = svc._lution_bridge_env(owner)
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"],
                         "unix:path=/run/user/1000/bus")

    def test_elevated_bridge_refuses_without_a_desktop_owner(self):
        with mock.patch.object(svc.os, "geteuid", return_value=0), \
                mock.patch.object(svc, "_desktop_owner", return_value=None), \
                mock.patch.object(svc.subprocess, "run") as run:
            data = svc.run_lution_cli("status")
        self.assertFalse(data["ok"])
        self.assertIn("root-owned", data["error"])
        run.assert_not_called()

    def test_unelevated_bridge_runs_normally_without_uid_kwargs(self):
        fake = SimpleNamespace(
            returncode=0, stdout='{"ok": true, "running": false}\n',
            stderr="")
        with mock.patch.object(svc.os, "geteuid", return_value=1000), \
                mock.patch.object(svc.subprocess, "run",
                                  return_value=fake) as run:
            data = svc.run_lution_cli("status")
        self.assertTrue(data["ok"])
        kwargs = run.call_args.kwargs
        for key in ("user", "group", "extra_groups", "preexec_fn"):
            self.assertNotIn(key, kwargs)
        self.assertEqual(kwargs["env"]["HOME"], os.environ["HOME"])

    def test_elevated_visual_flags_and_environment_use_the_bridge(self):
        calls = []

        def fake(op, payload=None, args=(), timeout=45):
            calls.append((op, payload, args))
            return {
                "fonts-status": {"ok": True,
                                 "font": {"apk": True, "active": 2}},
                "fonts-apply": {"ok": True, "replaced": 2},
                "fonts-reapply": {"ok": True, "reapplied": 2},
                "cursors-status": {"ok": True,
                                   "cursor": {"active": 3}},
                "cursors-apply": {"ok": True},
                "cursors-online-apply": {
                    "ok": True, "cursor": {"style": "Classic pointer"}},
                "fps-get": {"ok": True, "fps": 144},
                "fps-set": {"ok": True},
                "fflags-get": {"ok": True, "fflags": {"Good": 1},
                               "allowlist": ["Good"]},
                "fflags-set": {"ok": True, "removed": []},
                "env-get": {"ok": True, "env": {"A": "1"}},
                "env-set": {"ok": True},
                "status": {"ok": True, "installed": True,
                           "version": "1.8.0", "running": False,
                           "crashes": {"count": 2}},
                "running": {"ok": True, "running": False},
                "playtime": {"ok": True,
                             "playtime": {"seconds": 60, "sessions": 1,
                                          "text": "1m"}},
            }.get(op, {"ok": True})

        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            try:
                with mock.patch.object(service, "_elevated", return_value=True), \
                        mock.patch.object(svc, "_desktop_owner",
                                          return_value=(1000, 1000, "desk",
                                                        "/home/desk")), \
                        mock.patch.object(svc, "run_lution_cli",
                                          side_effect=fake):
                    self.assertTrue(service.fonts_status()["ok"])
                    self.assertTrue(service.apply_font("/tmp/f.ttf")[0])
                    self.assertTrue(service.apply_font("~/picked.ttf")[0])
                    self.assertTrue(service.reapply_font()[0])
                    self.assertTrue(service.cursors_status()["ok"])
                    self.assertTrue(service.apply_cursor("Heart", "Pink", 32)[0])
                    self.assertTrue(service.apply_online_cursor(
                        "Classic pointer", 32)[0])
                    self.assertEqual(service.fps_status()["fps"], 144)
                    self.assertTrue(service.set_fps_cap(240)[0])
                    self.assertTrue(service.lution_flags()["ok"])
                    self.assertTrue(service.set_lution_flags({"Good": 7})[0])
                    self.assertTrue(service.lution_env()["ok"])
                    self.assertTrue(service.save_lution_env({"A": "1"})[0])
                    self.assertEqual(service.lution_playtime()["seconds"], 60)
                    self.assertTrue(service.lution_runtime(refresh=True)["installed"])
                    self.assertIsNone(service.flag_allowlist_age())
            finally:
                service.shutdown()
        font_args = [call[2][0] for call in calls if call[0] == "fonts-apply"]
        self.assertIn("/home/desk/picked.ttf", font_args)
        ops = [call[0] for call in calls]
        for expected in ("fonts-status", "fonts-apply", "fonts-reapply",
                         "cursors-status", "cursors-apply",
                         "cursors-online-apply", "fps-get",
                         "fps-set", "fflags-get", "fflags-set", "env-get",
                         "env-set", "playtime", "status"):
            self.assertIn(expected, ops)

    def test_elevated_deployment_stop_and_launch_use_the_bridge(self):
        calls = []

        def fake(op, payload=None, args=(), timeout=45):
            calls.append((op, payload, args))
            return {"ok": True, "message": "already up to date",
                    "running": False}

        with tempfile.TemporaryDirectory() as tmp:
            service = svc.StudioService(path=Path(tmp) / "studio.json",
                                        auto_start=False)
            self.service = service
            try:
                service._sober_running_cache = False
                service._sober_running_checked = time.monotonic()
                with mock.patch.object(service, "_elevated", return_value=True), \
                        mock.patch.object(svc, "run_lution_cli",
                                          side_effect=fake), \
                        mock.patch.object(svc.shutil, "which",
                                          return_value="/usr/bin/flatpak"):
                    self.assertTrue(service.ensure_sober()[0])
                    self.wait_for_sober()
                    service._sober_running_cache = False
                    service._sober_running_checked = time.monotonic()
                    self.assertTrue(service.launch_sober()[0])
                    service._sober_thread.join(2)
                    service.stop_sober()
            finally:
                service.shutdown()
        ops = [call[0] for call in calls]
        self.assertIn("ensure", ops)
        self.assertIn("launch", ops)
        self.assertIn("stop", ops)
        self.assertEqual(service._sober_error, "")


class LutionIntegrationTests(unittest.TestCase):
    """Real vendored Lution modules under a throwaway HOME.

    Only the external Flatpak and bubblewrap executables are replaced; the
    font, cursor, FPS, flag, environment and deployment code is the real
    ``lution_core`` implementation, reached through StudioService.
    """

    def setUp(self):
        self.fake = tempfile.TemporaryDirectory(prefix="wxb-lution-fake-")
        self.fake_dir = Path(self.fake.name)
        self.home = Path(os.environ["WXB_TEST_HOME"])
        self.service = svc.StudioService(
            path=self.fake_dir / "studio.json", auto_start=False)
        # The fake executables are the only external boundary.  The scripts
        # speak just enough Flatpak to exercise install, update, status and
        # launch without touching the real user's Flatpak session.
        self.fake_flatpak = self.fake_dir / "flatpak"
        # Keep a log so the test can prove the real ensure_sober() took
        # both the install and update branches instead of assuming it did.
        self.fake_flatpak.write_text("""#!/bin/sh
printf '%s\\n' \"$*\" >> \"$HOME/.fake-flatpak-calls\"
case \"$1\" in
  list)
    [ -f \"$HOME/.fake-sober-installed\" ] && echo org.vinegarhq.Sober
    exit 0 ;;
  remotes)
    echo flathub
    exit 0 ;;
  info)
    echo 'Version: 1.8.0'
    if [ -f \"$HOME/.fake-sober-installed\" ]; then
      echo \"Commit: $(cat \"$HOME/.fake-sober-installed\")\"
    fi
    exit 0 ;;
  remote-info)
    echo 'Commit: remote-commit'
    exit 0 ;;
  install)
    echo install-commit > \"$HOME/.fake-sober-installed\"
    echo 'install complete'
    exit 0 ;;
  update)
    echo remote-commit > \"$HOME/.fake-sober-installed\"
    echo 'update complete'
    exit 0 ;;
  ps)
    exit 0 ;;
  kill)
    exit 0 ;;
  *)
    exit 0 ;;
esac
""")
        self.fake_flatpak.chmod(0o755)
        self.fake_bwrap = self.fake_dir / "bwrap"
        self.fake_bwrap.write_text("""#!/bin/sh
printf '%s\\n' \"$@\" > \"$HOME/.fake-bwrap-args\"
""")
        self.fake_bwrap.chmod(0o755)
        self.path_patch = mock.patch.dict(
            os.environ, {"PATH": str(self.fake_dir) + os.pathsep +
                         os.environ.get("PATH", "")})
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.service.shutdown()
        try:
            svc.lution_fonts.restore_fonts()
        except Exception:
            pass
        try:
            svc.lution_cursors.restore()
        except Exception:
            pass
        for path in (svc.lution_launcher.paths.LUTION_ROOT,
                     svc.lution_launcher.paths.STATE_DIR):
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(self.home / ".var/app/org.vinegarhq.Sober",
                      ignore_errors=True)
        for name in (".fake-sober-installed", ".fake-flatpak-calls",
                     ".fake-bwrap-args"):
            try:
                (self.home / name).unlink()
            except OSError:
                pass
        self.fake.cleanup()

    def wait_for_sober_thread(self):
        for _ in range(200):
            if not self.service._sober_maintenance:
                return
            time.sleep(0.005)
        self.fail("Sober deployment thread did not finish")

    def test_real_modules_round_trip_and_deploy(self):
        paths = svc.lution_launcher.paths
        # The test process must be reading and writing only its throwaway
        # HOME; otherwise a failed isolation guard could mutate real state.
        self.assertEqual(Path.home().resolve(), self.home.resolve())
        self.assertNotEqual(Path.home().resolve(),
                            Path(wxb_test_isolation.REAL_HOME).resolve())
        self.assertTrue(str(paths.LUTION_ROOT).startswith(str(self.home)))
        self.assertFalse(str(paths.LUTION_ROOT).startswith(
            str(wxb_test_isolation.REAL_HOME)))
        # Minimal Sober APK: fonts.status reads its face list, and no real
        # Sober install is needed for the font and cursor paths.
        apk = paths.SOBER_APK_DIR / "base.apk"
        apk.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(apk, "w") as zf:
            zf.writestr("assets/content/fonts/Roboto-Regular.ttf", b"x")
            zf.writestr("assets/content/fonts/BuilderSans-Bold.otf", b"x")
            zf.writestr("assets/content/fonts/RobloxEmoji.ttf", b"x")
        font = self.home / "source.ttf"
        font.write_bytes(b"\x00\x01\x00\x00" + b"integration" * 4)

        self.assertEqual(self.service.apply_font(font),
                         (True, "Font applied to 2 Roblox faces."))
        self.assertEqual(self.service.fonts_status()["active"], 2)
        self.assertTrue((paths.LUTION_ROOT / "installed_font/font.ttf").is_file())
        overlay_font = (paths.LUTION_OVERLAY / "content/fonts/Roboto-Regular.ttf")
        self.assertTrue(overlay_font.is_file())
        self.assertEqual(self.service.reapply_font()[0], True)
        self.assertTrue(overlay_font.is_file())
        self.assertTrue(self.service.restore_fonts()[0])
        self.assertFalse((paths.LUTION_ROOT / "installed_font/font.ttf").exists())
        self.assertFalse(overlay_font.exists())
        self.assertEqual(self.service.fonts_status()["active"], 0)

        options = self.service.cursor_options()
        self.assertEqual(options["styles"], list(svc.lution_cursors.STYLES))
        self.assertEqual(options["sizes"], list(svc.lution_cursors.SIZES))
        self.assertIn("Heart", options["styles"])
        self.assertTrue(self.service.apply_cursor("Heart", "Mint", 40)[0])
        cursor = self.service.cursors_status()
        self.assertEqual((cursor["style"], cursor["color"], cursor["size"]),
                         ("Heart", "Mint", 40))
        self.assertEqual(cursor["active"], 3)
        cursor_files = [svc.lution_cursors.OVERLAY_TEXTURES / name
                        for name in svc.lution_cursors.ASSETS]
        self.assertTrue(all(path.is_file() for path in cursor_files))
        self.assertTrue(svc.lution_cursors.KEPT_FILE.is_file())
        self.assertTrue(svc.lution_cursors.CONFIG_FILE.is_file())
        self.assertTrue(self.service.restore_cursors()[0])
        self.assertFalse(any(path.exists() for path in cursor_files))
        self.assertFalse(svc.lution_cursors.KEPT_FILE.exists())
        self.assertFalse(svc.lution_cursors.CONFIG_FILE.exists())
        self.assertEqual(self.service.cursors_status()["active"], 0)

        xml = svc.lution_fps.FPS_XML
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_text('<roblox><Item><int name="FramerateCap">60</int>'
                       '</Item></roblox>')
        self.assertEqual(self.service.set_fps_cap(144),
                         (True, "Framerate cap set to 144."))
        self.assertEqual(self.service.fps_status()["fps"], 144)
        self.assertEqual(svc.lution_fps.load_framerate_cap(), "144")
        self.assertIn('<int name="FramerateCap">144</int>', xml.read_text())

        # A real Sober install always has this file; its presence is what
        # makes the launcher bind Lution's flag table over Sober's config.
        paths.SOBER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        paths.SOBER_CONFIG.write_text('{"fflags": {}}\n')

        allowed = "DFIntTextureQualityOverride"
        self.assertIn(allowed, self.service.flag_allowlist())
        self.assertTrue(self.service.set_lution_flags({allowed: 2})[0])
        self.assertEqual(self.service.lution_flags()["flags"], {allowed: 2})
        flag_config = json.loads(paths.LUTION_CONFIG.read_text())
        self.assertEqual(flag_config["fflags"], {allowed: 2})
        self.assertIn(allowed, svc.lution_fflags.allowlist())

        self.assertTrue(self.service.save_lution_env(
            {"WXB_INTEGRATION": "1"})[0])
        self.assertTrue(self.service.save_lution_env_text(
            "WXB_INTEGRATION = 1\nWXB_TEXT = 2")[0])
        self.assertEqual(self.service.lution_env()["env"],
                         {"WXB_INTEGRATION": "1", "WXB_TEXT": "2"})
        self.assertEqual(self.service.lution_env_text(),
                         (True, "WXB_INTEGRATION = 1\nWXB_TEXT = 2"))
        env_file = svc.lution_envvars.ENV_FILE
        self.assertTrue(env_file.is_file())
        self.assertEqual(json.loads(env_file.read_text()),
                         {"WXB_INTEGRATION": "1", "WXB_TEXT": "2"})

        # Explicit install/update through the real sober.ensure_sober().
        self.assertFalse(self.service.lution_runtime(refresh=True)["installed"])
        self.assertTrue(self.service.ensure_sober()[0])
        self.wait_for_sober_thread()
        marker = self.home / ".fake-sober-installed"
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text().strip(), "install-commit")
        calls = (self.home / ".fake-flatpak-calls").read_text()
        self.assertIn("install -y flathub org.vinegarhq.Sober", calls)
        self.assertEqual(self.service._sober_error, "")
        runtime = self.service.lution_runtime(refresh=True)
        self.assertTrue(runtime["installed"])
        self.assertEqual(runtime["version"], "1.8.0")

        # The fake remote advertises a different commit, so this pass must
        # go through the real update branch and replace the installed one.
        self.assertTrue(self.service.ensure_sober()[0])
        self.wait_for_sober_thread()
        self.assertEqual(marker.read_text().strip(), "remote-commit")
        calls = (self.home / ".fake-flatpak-calls").read_text()
        self.assertIn("update -y org.vinegarhq.Sober", calls)
        self.assertEqual(self.service._sober_error, "")
        self.assertEqual(self.service.lution_runtime(refresh=True)["version"],
                         "1.8.0")

        self.assertTrue(self.service.ensure_sober()[0])
        self.wait_for_sober_thread()
        self.assertIn("up to date", self.service._sober_detail)

        # Real launcher.build_sober_command + launch, with only the external
        # bubblewrap executable replaced.  userns_available() and the rest of
        # the launcher logic are the real vendored implementation.
        with mock.patch.object(svc.lution_launcher, "BWRAP",
                               str(self.fake_bwrap)):
            self.assertTrue(svc.lution_launcher.userns_available())
            # The namespace probe wrote its own argv to the shared fake log;
            # clear it so the assertions below describe the launch itself.
            (self.home / ".fake-bwrap-args").unlink(missing_ok=True)
            self.assertTrue(self.service.launch_sober()[0])
            self.service._sober_thread.join(2)
        self.assertEqual(self.service._sober_error, "")
        args_file = self.home / ".fake-bwrap-args"
        for _ in range(200):
            if (args_file.exists()
                    and "--filesystem=/tmp" in args_file.read_text()):
                break
            time.sleep(0.005)
        self.assertTrue(args_file.exists())
        args = args_file.read_text().splitlines()
        self.assertIn("--filesystem=/tmp", args)
        self.assertIn("--env=WXB_INTEGRATION=1", args)
        self.assertIn(str(paths.LUTION_CONFIG), args)
        self.assertIn(str(paths.LUTION_OVERLAY / "content"), args)
        launched = self.service._sober
        self.assertIsNotNone(launched)
        launched.wait(timeout=2)

        env_file = svc.lution_envvars.ENV_FILE
        self.assertTrue(env_file.is_file())
        env_file.unlink()
        self.assertFalse(env_file.exists())
        self.assertEqual(self.service.lution_env()["env"], {})

    def test_elevated_real_modules_keep_desktop_owner_paths(self):
        """A root-style process must still write only the desktop home.

        The harness imports every real Lution module while HOME points at a
        root-like throwaway directory, before WXB_HOME is set.  It then uses
        the explicit override for all writes.  On this machine the harness is
        also run inside ``unshare -Ur`` when available, so its euid is 0; the
        parent checks the artifacts by path and by their on-disk uid/gid.
        Only the fake Flatpak executable is supplied on PATH.
        """
        with tempfile.TemporaryDirectory(prefix="wxb-elevated-") as tmp:
            base = Path(tmp)
            root_home = base / "root-home"
            desktop_home = base / "desktop-home"
            root_home.mkdir()
            desktop_home.mkdir()
            sobot = desktop_home / ".var/app/org.vinegarhq.Sober"
            apk = (sobot / "data/sober/packages/x86_64/"
                   "com.roblox.client/base.apk")
            apk.parent.mkdir(parents=True)
            with zipfile.ZipFile(apk, "w") as zf:
                zf.writestr("assets/content/fonts/Roboto-Regular.ttf", b"x")
                zf.writestr("assets/content/fonts/BuilderSans-Bold.otf", b"x")
            (desktop_home / "source.ttf").write_bytes(
                b"\x00\x01\x00\x00" + b"elevated" * 4)
            fps_xml = (sobot / "data/sober/appData/"
                       "GlobalBasicSettings_13.xml")
            fps_xml.parent.mkdir(parents=True)
            fps_xml.write_text('<roblox><Item>'
                               '<int name="FramerateCap">60</int>'
                               '</Item></roblox>')

            harness = base / "elevated_harness.py"
            harness.write_text(textwrap.dedent(r'''
                import json
                import os
                import sys
                from pathlib import Path

                root = Path(os.environ["ROOT_HOME"])
                desktop = Path(os.environ["DESKTOP_HOME"])
                # Import before the explicit override is installed.  The
                # modules must not freeze Path.home()/root's HOME here.
                from lution_core import paths, fonts, cursors, fps, fflags
                from lution_core import envvars, sober

                assert paths.lution_home() == root, (paths.lution_home(), root)
                os.environ["WXB_HOME"] = str(desktop)
                assert paths.lution_home() == desktop, paths.lution_home()

                if len(sys.argv) > 1 and sys.argv[1] == "restore":
                    paths_to_remove = {
                        "font_kept": paths.lution_root() /
                                     "installed_font/font.ttf",
                        "font_overlay": paths.lution_overlay() /
                                        "content/fonts/Roboto-Regular.ttf",
                        "cursor_arrow": cursors.overlay_textures() /
                                        cursors.ASSETS[0],
                        "cursor_far": cursors.overlay_textures() /
                                      cursors.ASSETS[1],
                        "cursor_ibeam": cursors.overlay_textures() /
                                        cursors.ASSETS[2],
                        "cursor_json": cursors.config_file(),
                        "cursor_kept": cursors.kept_file(),
                    }
                    fonts.restore_fonts()
                    cursors.restore()
                    print("ELEVATED_RESULT " + json.dumps({
                        "mode": "restore",
                        "removed": {name: not path.exists()
                                    for name, path in paths_to_remove.items()},
                    }))
                    raise SystemExit(0)

                names = fonts.apply_font(desktop / "source.ttf")
                cursor = cursors.apply("Heart", "Mint", 40)
                fps.save_framerate_cap(144)
                allowed = "DFIntTextureQualityOverride"
                fflags.save_fflags({allowed: 2})
                envvars.save_vars({"WXB_ELEVATED": "1"})
                installed = sober.ensure_sober()
                updated = sober.ensure_sober()
                up_to_date = sober.ensure_sober()

                def record(path):
                    st = path.stat()
                    return {"path": str(path), "uid": st.st_uid,
                            "gid": st.st_gid, "size": st.st_size}

                artifacts = {
                    "font_kept": paths.lution_root() / "installed_font/font.ttf",
                    "font_overlay": paths.lution_overlay() /
                                    "content/fonts/Roboto-Regular.ttf",
                    "cursor_arrow": cursors.overlay_textures() /
                                    cursors.ASSETS[0],
                    "cursor_far": cursors.overlay_textures() /
                                  cursors.ASSETS[1],
                    "cursor_ibeam": cursors.overlay_textures() /
                                    cursors.ASSETS[2],
                    "cursor_json": cursors.config_file(),
                    "cursor_kept": cursors.kept_file(),
                    "sober_config": paths.lution_config(),
                    "fps_xml": fps.fps_xml(),
                    "env_json": envvars.env_file(),
                }
                result = {
                    "euid": os.geteuid(), "uid": os.getuid(),
                    "gid": os.getgid(), "desktop_home": str(desktop),
                    "lution_home": str(paths.lution_home()),
                    "font_names": names, "cursor": cursor,
                    "deploy_install": installed, "deploy_update": updated,
                    "deploy_up_to_date": up_to_date,
                    "artifacts": {name: record(path)
                                  for name, path in artifacts.items()},
                    "flatpak_marker": str(desktop /
                                          ".fake-sober-installed"),
                    "root_candidates": [
                        str(root / ".local/share/Lution"),
                        str(root / ".local/Lution"),
                        str(root / ".var/app/org.vinegarhq.Sober"),
                    ],
                }
                print("ELEVATED_RESULT " + json.dumps(result))
            '''), encoding="utf-8")

            child_env = os.environ.copy()
            child_env.update({
                "HOME": str(root_home),
                "ROOT_HOME": str(root_home),
                "DESKTOP_HOME": str(desktop_home),
                "PYTHONPATH": str(svc.APP_DIR),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": str(self.fake_dir) + os.pathsep +
                        os.environ.get("PATH", ""),
            })
            child_env.pop("WXB_HOME", None)
            child_env.pop("WXB_LUTION_HOME", None)
            child_env.pop("WXB_OWNER_UID", None)
            child_env.pop("PKEXEC_UID", None)
            child_env.pop("SUDO_UID", None)
            unshare = shutil.which("unshare")
            def run_harness(mode=None):
                cmd = [sys.executable, str(harness)]
                if mode:
                    cmd.append(mode)
                if unshare:
                    cmd = [unshare, "-Ur", "--propagation", "private"] + cmd
                return subprocess.run(cmd, cwd=str(svc.APP_DIR), env=child_env,
                                  capture_output=True, text=True, timeout=120)
            proc = run_harness()
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("ELEVATED_RESULT ", proc.stdout)
            data = json.loads(proc.stdout.split("ELEVATED_RESULT ", 1)[1]
                              .splitlines()[0])

            self.assertEqual(Path(data["lution_home"]).resolve(),
                             desktop_home.resolve())
            if unshare:
                self.assertEqual(data["euid"], 0)
            artifacts = data["artifacts"]
            for name in ("font_kept", "font_overlay", "cursor_arrow",
                         "cursor_far", "cursor_ibeam", "cursor_json",
                         "cursor_kept", "sober_config", "fps_xml",
                         "env_json"):
                self.assertIn(name, artifacts)
                item = artifacts[name]
                path = Path(item["path"])
                self.assertTrue(path.is_file(), name)
                self.assertTrue(str(path).startswith(str(desktop_home)), name)
                actual = path.stat()
                self.assertNotEqual(actual.st_uid, 0, name)
                self.assertEqual(actual.st_uid, os.getuid(), name)
                self.assertEqual(actual.st_gid, os.getgid(), name)
                self.assertNotIn("/root/", str(path), name)
            self.assertEqual(set(data["font_names"]),
                             {"BuilderSans-Bold.otf", "Roboto-Regular.ttf"})
            self.assertTrue(data["cursor"]["ok"])
            self.assertTrue(data["deploy_install"][0])
            self.assertTrue(data["deploy_update"][0])
            self.assertTrue(data["deploy_up_to_date"][0])
            self.assertIn("up to date", data["deploy_up_to_date"][1])
            marker = Path(data["flatpak_marker"])
            self.assertTrue(marker.is_file())
            self.assertTrue(str(marker).startswith(str(desktop_home)))
            self.assertEqual(marker.stat().st_uid, os.getuid())
            self.assertEqual(marker.stat().st_gid, os.getgid())
            for path in map(Path, data["root_candidates"]):
                self.assertFalse(path.exists(), path)
            restored = run_harness("restore")
            self.assertEqual(restored.returncode, 0,
                             restored.stdout + restored.stderr)
            restore_data = json.loads(
                restored.stdout.split("ELEVATED_RESULT ", 1)[1]
                .splitlines()[0])
            self.assertEqual(restore_data["removed"], {
                "font_kept": True, "font_overlay": True,
                "cursor_arrow": True, "cursor_far": True,
                "cursor_ibeam": True, "cursor_json": True,
                "cursor_kept": True})


class IsolationGuardTests(unittest.TestCase):
    """Prove the redirect guard both passes and catches a deliberate bypass."""

    def run_guard_child(self, code, env):
        child_env = env.copy()
        paths = [str(svc.APP_DIR)]
        if (wxb_test_isolation.IMPORT_USER_SITE
                and Path(wxb_test_isolation.IMPORT_USER_SITE).is_dir()):
            paths.append(wxb_test_isolation.IMPORT_USER_SITE)
        if child_env.get("PYTHONPATH"):
            paths.append(child_env["PYTHONPATH"])
        child_env["PYTHONPATH"] = os.pathsep.join(paths)
        return subprocess.run(
            [sys.executable, "-c", code], cwd=str(svc.APP_DIR),
            env=child_env, capture_output=True, text=True, timeout=90)

    def test_guard_passes_with_redirect_in_place(self):
        proc = self.run_guard_child(
            "import wxb_test_isolation as iso\n"
            "print(iso.assert_isolated('subprocess-pass'))\n",
            os.environ.copy())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("isolation guard passed for subprocess-pass", proc.stdout)

    def test_guard_fails_when_redirect_is_bypassed(self):
        env = os.environ.copy()
        env["WXB_TEST_NO_REDIRECT"] = "1"
        env["HOME"] = str(wxb_test_isolation.REAL_HOME)
        for name, suffix in (("XDG_CONFIG_HOME", ".config"),
                             ("XDG_DATA_HOME", ".local/share"),
                             ("XDG_CACHE_HOME", ".cache"),
                             ("XDG_STATE_HOME", ".local/state")):
            env[name] = str(wxb_test_isolation.REAL_HOME / suffix)
        env.pop("WXB_TEST_HOME", None)
        proc = self.run_guard_child(
            "import wxb_test_isolation as iso\n"
            "try:\n"
            "    iso.assert_isolated('subprocess-bypass')\n"
            "except iso.IsolationError as exc:\n"
            "    print('ISOLATION_GUARD_CAUGHT:', exc)\n"
            "    raise SystemExit(3)\n"
            "print('ISOLATION_GUARD_UNEXPECTED_PASS')\n",
            env)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("ISOLATION_GUARD_CAUGHT", proc.stdout)
        self.assertIn("outside test HOME", proc.stdout)
        self.assertIn("real state", proc.stdout)


class AssetsTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent

    def desktop(self, name):
        values = {}
        for line in (self.ROOT / name).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def test_assets_and_both_desktop_entries_point_at_real_files(self):
        self.assertTrue((self.ROOT / "wxb_studio.css").is_file())
        css = (self.ROOT / "wxb_studio.css").read_text(encoding="utf-8")
        self.assertIn("@define-color wxb_accent", css)
        icon = self.ROOT / "wxb-studio.svg"
        self.assertTrue(icon.is_file())
        for name in ("WXB.desktop", "AutoSwitcher.desktop"):
            entry = self.desktop(name)
            exec_path = entry["Exec"].split()[0]
            if exec_path.startswith("/"):
                self.assertTrue(Path(exec_path).is_file(), (name, exec_path))
            if entry.get("Path", "").startswith("/"):
                self.assertTrue(Path(entry["Path"]).is_dir(), (name, "Path"))
            if entry.get("Icon", "").startswith("/"):
                self.assertTrue(Path(entry["Icon"]).is_file(), (name, "Icon"))
        wxb = self.desktop("WXB.desktop")
        self.assertEqual(wxb.get("StartupWMClass"), "wxb")
        self.assertEqual(Path(wxb["Icon"]).resolve(), icon.resolve())

    def test_retired_ui_assets_and_tests_are_absent(self):
        for name in ("wxb.png", "_shot_ui.png", "WXB_full_source.txt",
                     "wxb_cursor.xbm", "art", "trim.py", "old",
                     "test_wxb.py", "test_deck_ui.py", "test_close_option.py"):
            self.assertFalse((self.ROOT / name).exists(), name)


if __name__ == "__main__":
    unittest.main(
        testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
