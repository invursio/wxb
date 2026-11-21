#!/usr/bin/env python3
"""Deterministic recorder checks: argv, lifecycle, security, service wiring.

These tests do not start a real capture process.  The real-X11 acceptance
suite lives in test_recorder_x11.py.
"""

import io
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)

import wxb_recorder as recorder
import wxb_studio_service as service

print(wxb_test_isolation.assert_isolated("recorder"))


def caps(**overrides):
    value = {
        "session": "x11", "x11": True, "xwayland": False, "wayland": False,
        "display": True, "ffmpeg": "/usr/bin/ffmpeg",
        "ffprobe": "/usr/bin/ffprobe", "x11grab": True, "pulse": True,
        "portal": False, "pipewire": False, "backend": "ffmpeg/x11grab",
        "record": True, "play": True, "hotkeys": True,
        "sources": ["screen", "window", "region"], "audio": True,
        "encoders": ["libx264"], "hardware_encoders": [], "reason": "",
    }
    value.update(overrides)
    return value


class SettingsTests(unittest.TestCase):
    def test_defaults_and_clamps(self):
        clean = recorder.normalize_settings({
            "source": "bogus", "fps": 1000, "quality": "nope",
            "region_w": -5, "region_h": "bad", "cursor": 0,
        })
        self.assertEqual(clean["source"], "screen")
        self.assertEqual(clean["fps"], 60)
        self.assertEqual(clean["quality"], "balanced")
        self.assertEqual(clean["region_w"], 0)
        self.assertEqual(clean["region_h"], 0)
        self.assertFalse(clean["cursor"])
        self.assertTrue(Path(clean["save_dir"]).is_absolute())

    def test_hex_window_id_is_converted_to_decimal(self):
        clean = recorder.normalize_settings({"window_id": "0x200030"})
        self.assertEqual(clean["window_id"], "2097200")
        bad = recorder.normalize_settings({"window_id": "0xNOTHEX"})
        self.assertEqual(bad["window_id"], "")

    def test_choice_limits(self):
        for value in recorder.SOURCE_CHOICES:
            self.assertEqual(recorder.normalize_settings({"source": value})["source"], value)
        for value in recorder.RESOLUTION_CHOICES:
            self.assertEqual(
                recorder.normalize_settings({"resolution": value})["resolution"], value)

    def test_wayland_portal_is_not_advertised_without_tested_capture(self):
        self.addCleanup(recorder.invalidate_capabilities)
        env = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
               "DISPLAY": ""}

        def which(name):
            return "/usr/bin/" + name if name in ("ffmpeg", "ffprobe",
                                                   "gst-launch-1.0", "gdbus") else None

        with mock.patch.object(recorder.shutil, "which", side_effect=which), \
                mock.patch.object(recorder, "_portal_has_screencast", return_value=True), \
                mock.patch.object(recorder, "_gst_element", return_value=True), \
                mock.patch.object(recorder, "_ffmpeg_has", return_value=True), \
                mock.patch.object(recorder, "_pulse_available", return_value=True), \
                mock.patch.object(recorder, "_ffmpeg_encoders",
                                  return_value={"libx264"}):
            caps = recorder.detect_capabilities(env, refresh=True)
        self.assertTrue(caps["wayland"])
        self.assertFalse(caps["record"])
        self.assertEqual(caps["sources"], [])
        self.assertIn("portal", caps["reason"].lower())


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(recorder, "_x11_screen_size",
                                    return_value=(1920, 1080))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **values):
        settings = {
            "source": "screen", "save_dir": str(self.root), "fps": 30,
            "quality": "balanced", "encoder": "cpu", "cursor": True,
        }
        settings.update(values)
        return recorder.build_x11_command(
            settings, caps(), self.root / "out.mkv", {"DISPLAY": ":99"})

    def test_screen_command_is_shell_free_and_never_overwrites(self):
        argv = self.build()
        self.assertEqual(argv[0], "/usr/bin/ffmpeg")
        self.assertIn("-y", argv)
        self.assertIn("-video_size", argv)
        self.assertIn("1920x1080", argv)
        self.assertIn(":99+0,0", argv)
        self.assertIn("-draw_mouse", argv)
        self.assertIn("1", argv)
        self.assertIn("libx264", argv)
        self.assertIn("veryfast", argv)
        self.assertIn("23", argv)
        self.assertNotIn("-window_id", argv)

    def test_region_command_uses_offset_and_rejects_bad_regions(self):
        argv = self.build(source="region", region_x=10, region_y=20,
                          region_w=640, region_h=480)
        self.assertIn("640x480", argv)
        self.assertIn(":99+10,20", argv)
        vf = argv[argv.index("-vf") + 1]
        self.assertIn("crop=trunc(iw/2)*2:trunc(ih/2)*2", vf)
        with self.assertRaises(recorder.RecorderError):
            self.build(source="region", region_w=0, region_h=480)
        with self.assertRaises(recorder.RecorderError):
            self.build(source="region", region_x=1900, region_w=100,
                       region_h=100)

    def test_window_uses_geometry_fallback_and_handles_odd_size(self):
        with mock.patch.object(recorder, "window_geometry",
                               return_value=(50, 60, 165, 52)):
            argv = self.build(source="window", window_id="123")
        self.assertIn("165x52", argv)
        self.assertIn(":99+50,60", argv)
        vf = argv[argv.index("-vf") + 1]
        self.assertIn("crop=trunc(iw/2)*2:trunc(ih/2)*2", vf)
        self.assertNotIn("-window_id", argv)

    def test_missing_or_offscreen_window_is_an_error(self):
        with self.assertRaises(recorder.RecorderError):
            self.build(source="window", window_id="")
        with mock.patch.object(recorder, "window_geometry",
                               return_value=(-1, 0, 100, 100)):
            with self.assertRaises(recorder.RecorderError):
                self.build(source="window", window_id="123")
        with mock.patch.object(recorder, "window_geometry",
                               return_value=(0, 0, 0, 0)):
            with self.assertRaises(recorder.RecorderError):
                self.build(source="window", window_id="123")

    def test_audio_maps_and_mixes(self):
        with mock.patch.object(recorder, "list_audio_devices",
                               return_value={"microphones": ["mic"],
                                              "desktop": ["monitor"]}):
            argv = self.build(mic=True, mic_device="mic", desktop_audio=True,
                              desktop_device="monitor")
        self.assertIn("-filter_complex", argv)
        self.assertIn("[1:a][2:a]amix=inputs=2:duration=longest[a]", argv)
        self.assertIn("-c:a", argv)
        self.assertIn("aac", argv)
        self.assertIn("monitor", argv)
        self.assertIn("mic", argv)
        self.assertIn("-vf", argv)

    def test_desktop_audio_without_monitor_is_an_error(self):
        with mock.patch.object(recorder, "list_audio_devices",
                               return_value={"microphones": [], "desktop": []}):
            with self.assertRaises(recorder.RecorderError):
                self.build(desktop_audio=True)

    def test_quality_and_resolution_change_load(self):
        low = self.build(quality="low", resolution="720p", fps=15)
        high = self.build(quality="high", resolution="1440p", fps=60)
        self.assertIn("ultrafast", low)
        self.assertIn("scale=-2:720:flags=fast_bilinear",
                      low[low.index("-vf") + 1])
        self.assertIn("medium", high)
        self.assertIn("scale=-2:1440:flags=fast_bilinear",
                      high[high.index("-vf") + 1])
        self.assertIn("60", high)

    def test_encoder_requires_detected_hardware(self):
        with self.assertRaises(recorder.RecorderError):
            self.build(encoder="vaapi")
        with self.assertRaises(recorder.RecorderError):
            self.build(encoder="nvenc")
        argv = self.build(encoder="cpu")
        self.assertIn("libx264", argv)
        vaapi_caps = caps(hardware_encoders=["vaapi"], encoders=["libx264", "vaapi"])
        with mock.patch.object(recorder, "list_audio_devices", return_value={}):
            argv = recorder.build_x11_command(
                {"source": "screen", "save_dir": str(self.root), "fps": 30,
                 "quality": "balanced", "encoder": "vaapi"},
                vaapi_caps, self.root / "vaapi.mkv", {"DISPLAY": ":99"})
        self.assertIn("-vaapi_device", argv)
        self.assertIn("h264_vaapi", argv)
        vf = argv[argv.index("-vf") + 1]
        self.assertIn("crop=trunc(iw/2)*2:trunc(ih/2)*2", vf)
        self.assertIn("hwupload", vf)


class OutputSecurityTests(unittest.TestCase):
    def test_unique_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with mock.patch.object(recorder.time, "strftime",
                                   return_value="WXB-TEST"):
                first = recorder._unique_output(root)
                first.write_bytes(b"existing")
                second = recorder._unique_output(root)
            self.assertEqual(second.name, "WXB-TEST-2.mkv")
            self.assertEqual(first.read_bytes(), b"existing")

    def test_symlinked_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(recorder.RecorderError):
                recorder._unique_output(link)

    def test_file_is_not_a_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "file"
            target.write_text("x")
            with self.assertRaises(recorder.RecorderError):
                recorder._unique_output(target)


class FakeProcess:
    def __init__(self):
        self.pid = 4321
        self.returncode = None
        self.signals = []
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig in (signal.SIGINT, signal.SIGTERM):
            self.returncode = 0 if sig == signal.SIGINT else -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.settings = {
            "source": "screen", "save_dir": str(self.root), "fps": 30,
            "quality": "balanced", "encoder": "cpu",
            "mic": False, "desktop_audio": False,
        }
        self.proc = None
        self.command = []

    def factory(self, command, **_kwargs):
        self.command = command
        self.proc = FakeProcess()
        return self.proc

    def make(self, make_file=True, **kwargs):
        def popen(command, **_kwargs):
            proc = self.factory(command)
            if make_file:
                Path(command[-1]).write_bytes(b"fake mkv")
            return proc
        return recorder.Recorder(
            lambda: self.settings, capabilities_getter=lambda: caps(),
            popen_factory=popen, probe_factory=lambda *_a: {
                "duration": 1.0, "width": 1920, "height": 1080, "fps": 30},
            **kwargs)

    def wait_idle(self, rec, timeout=3.0):
        deadline = time.monotonic() + timeout
        while rec.snapshot()["running"] and time.monotonic() < deadline:
            time.sleep(0.02)
        return not rec.snapshot()["running"]

    def test_start_pause_resume_stop_and_finalize(self):
        rec = self.make()
        self.addCleanup(rec.shutdown)
        ok, message = rec.start()
        self.assertTrue(ok, message)
        self.assertTrue(rec.snapshot()["recording"])
        self.assertIn("-y", self.command)
        self.assertTrue(rec.pause()[0])
        self.assertEqual(rec.snapshot()["state"], "paused")
        self.assertIn(signal.SIGSTOP, self.proc.signals)
        self.assertTrue(rec.resume()[0])
        self.assertIn(signal.SIGCONT, self.proc.signals)
        ok, message = rec.stop(wait=True)
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_idle(rec))
        output = self.root / Path(self.command[-1]).name
        self.assertTrue(output.exists())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIn(signal.SIGINT, self.proc.signals)
        self.assertEqual(rec.snapshot()["state"], "idle")
        self.assertEqual(rec.snapshot()["probe"]["width"], 1920)

    def test_no_output_is_reported_not_hidden(self):
        rec = self.make(make_file=False)
        self.addCleanup(rec.shutdown)
        self.assertTrue(rec.start()[0])
        ok, message = rec.stop(wait=True)
        self.assertFalse(ok)
        self.assertIn("usable file", message)
        self.assertEqual(rec.snapshot()["state"], "error")

    def test_start_refuses_when_no_capability(self):
        rec = recorder.Recorder(lambda: self.settings,
                                capabilities_getter=lambda: caps(record=False,
                                                                 reason="no"),
                                popen_factory=lambda *_a, **_k: self.fail("spawned"))
        self.addCleanup(rec.shutdown)
        ok, message = rec.start()
        self.assertFalse(ok)
        self.assertEqual(message, "no")

    def test_disk_guard_blocks_start(self):
        rec = self.make()
        self.addCleanup(rec.shutdown)
        with mock.patch.object(recorder, "_free_bytes", return_value=0):
            ok, message = rec.start()
        self.assertFalse(ok)
        self.assertIn("disk space", message)
        self.assertEqual(rec.snapshot()["state"], "error")

    def test_session_lock_finalizes_the_file(self):
        locked = [False]
        rec = self.make(session_guard=lambda: locked[0])
        self.addCleanup(rec.shutdown)
        self.assertTrue(rec.start()[0])
        locked[0] = True
        self.assertTrue(self.wait_idle(rec, timeout=3.0))
        snapshot = rec.snapshot()
        self.assertIn("desktop locked", snapshot["notice"])
        self.assertEqual(snapshot["state"], "idle")

    def test_shutdown_stops_a_live_capture(self):
        rec = self.make()
        self.assertTrue(rec.start()[0])
        rec.shutdown()
        self.assertFalse(rec.snapshot()["running"])
        self.assertIn(signal.SIGINT, self.proc.signals)

    def test_unexpected_exit_is_visible(self):
        rec = self.make()
        self.addCleanup(rec.shutdown)
        self.assertTrue(rec.start()[0])
        self.proc.returncode = 1
        self.assertTrue(self.wait_idle(rec, timeout=3.0))
        self.assertIn("stopped unexpectedly", rec.snapshot()["error"])

    def test_spawn_failure_is_visible_and_cleans_the_reserved_file(self):
        rec = recorder.Recorder(
            lambda: self.settings, capabilities_getter=lambda: caps(),
            popen_factory=lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("permission denied")))
        self.addCleanup(rec.shutdown)
        ok, message = rec.start()
        self.assertFalse(ok)
        self.assertIn("permission denied", message)
        self.assertFalse(any(self.root.glob("*.mkv")))


class FakeRecorder:
    recording_state = False
    paused_state = False

    def __init__(self):
        self.settings = None
        self.stopped = []
        self.started = False

    @property
    def recording(self):
        return self.recording_state

    def start(self):
        self.started = True
        self.recording_state = True
        return True, "started"

    def stop(self, wait=False):
        self.stopped.append(wait)
        self.recording_state = False
        return True, "stopped"

    def pause(self):
        self.paused_state = True
        return True, "paused"

    def resume(self):
        self.paused_state = False
        return True, "resumed"

    def capabilities(self):
        return caps()

    def snapshot(self):
        return {"available": True, "recording": self.recording_state,
                "starting": False, "paused": self.paused_state,
                "running": self.recording_state, "state": "recording",
                "elapsed": 0.0, "output": "", "error": "", "notice": "",
                "probe": {}, "settings": {}, "capabilities": caps(),
                "audio_devices": {"microphones": [], "desktop": []},
                "limit": ""}


class ServiceWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeRecorder()
        self.service = service.StudioService(
            path=Path(self.tmp.name) / "studio.json",
            recorder_factory=lambda: self.fake)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.shutdown)

    def test_defaults_and_stop_hotkey_are_wired(self):
        self.assertEqual(self.service.settings["hotkeys"]["stop_recording"],
                         "ctrl+alt+x")
        self.assertEqual(self.service.settings["hotkeys"]["toggle_recording"],
                         "ctrl+alt+v")
        self.assertEqual(self.service.settings["recorder"]["source"], "screen")
        self.assertTrue(self.service.start_recording()[0])
        self.assertTrue(self.service.snapshot()["recorder"]["running"])
        self.assertFalse(self.service.set_recorder_settings(fps=60)[0])
        self.assertTrue(self.service.stop_recording(wait=True)[0])

    def test_toggle_recording_hotkey_starts_and_finishes_capture(self):
        self.assertTrue(self.service.handle_hotkey("toggle_recording")[0])
        self.assertTrue(self.service.recorder_snapshot()["running"])
        self.assertTrue(self.service.handle_hotkey("toggle_recording")[0])
        self.assertFalse(self.service.recorder_snapshot()["running"])
        self.assertTrue(self.fake.stopped)

    def test_stop_all_and_hotkey_stop_the_recorder(self):
        self.assertTrue(self.service.start_recording()[0])
        self.assertTrue(self.service.handle_hotkey("stop_recording")[0])
        self.assertFalse(self.service.recorder_snapshot()["running"])
        self.assertTrue(self.service.start_recording()[0])
        self.service.stop_all()
        self.assertIn(True, self.fake.stopped)


if __name__ == "__main__":
    unittest.main(verbosity=2)