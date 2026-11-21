#!/usr/bin/env python3
"""Real-X11 screen recorder acceptance checks.

Run under Xvfb or a live X11 session:
  timeout --kill-after=10s 120s xvfb-run -a python3 test_recorder_x11.py

These tests spawn the real FFmpeg capture process through StudioService.
They prove a finalized file exists, is 0600, contains decodable video, and
that repeated stops never overwrite an earlier recording.  Wayland portal
capture is intentionally not claimed by this build; see the capability check.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import wxb_test_isolation  # noqa: F401  (HOME must be redirected first)

import wxb_recorder as recorder
import wxb_studio_service as service

print(wxb_test_isolation.assert_isolated("recorder-x11"))


def wait_until(predicate, timeout=5.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("ffmpeg")
                     and shutil.which("ffprobe"),
                     "needs X11 DISPLAY and FFmpeg/ffprobe")
class RealRecorderX11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        caps = recorder.detect_capabilities(refresh=True)
        if not caps.get("record") or not caps.get("x11"):
            raise unittest.SkipTest(caps.get("reason") or "no X11 recorder")
        cls.caps = caps

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = service.StudioService(
            path=self.root / "studio.json")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.shutdown)
        if self.service._recorder is not None:
            self.service._recorder._session_guard = lambda: False
        self.service.set_recorder_settings(
            source="region", region_x=0, region_y=0, region_w=320,
            region_h=240, resolution="source", fps=15, quality="low",
            encoder="cpu", cursor=False, save_dir=str(self.root),
            mic=False, desktop_audio=False)

    def snapshot(self):
        return self.service.recorder_snapshot()

    def start_and_stop(self, seconds=1.2):
        ok, message = self.service.start_recording()
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(
            lambda: self.snapshot()["recording"] or self.snapshot()["error"], 3.0),
            self.snapshot())
        # FFmpeg opens the Matroska file asynchronously after Popen returns.
        # Let it write the first frames before asking for a finalize, otherwise
        # an immediate SIGINT can legitimately leave an empty file.
        wait_until(lambda: (Path(self.snapshot()["output"]).exists()
                            and Path(self.snapshot()["output"]).stat().st_size > 0),
                   timeout=3.0)
        time.sleep(seconds)
        ok, message = self.service.stop_recording(wait=True)
        self.assertTrue(ok, "%s / %r" % (message, self.snapshot()))
        self.assertTrue(wait_until(lambda: not self.snapshot()["running"], 12.0),
                        self.snapshot())
        return Path(self.snapshot()["output"])

    def test_capture_stop_and_playback(self):
        output = self.start_and_stop()
        self.assertTrue(output.exists(), output)
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        probe = recorder.probe_output(output, self.caps)
        self.assertEqual(probe.get("width"), 320)
        self.assertEqual(probe.get("height"), 240)
        self.assertGreater(probe.get("duration", 0), 0)
        result = subprocess.run(
            [self.caps["ffmpeg"], "-v", "error", "-i", str(output),
             "-f", "null", "-"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip(), "")

    def test_repeated_start_stop_uses_unique_files(self):
        outputs = [self.start_and_stop(0.7) for _ in range(3)]
        self.assertEqual(len({str(path) for path in outputs}), 3)
        for output in outputs:
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_pause_resume_finalizes(self):
        ok, message = self.service.start_recording()
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(lambda: self.snapshot()["recording"], 3.0))
        time.sleep(0.6)
        self.assertTrue(self.service.pause_recording()[0])
        self.assertTrue(self.snapshot()["paused"])
        time.sleep(0.5)
        self.assertTrue(self.service.resume_recording()[0])
        time.sleep(0.6)
        self.assertTrue(self.service.stop_recording(wait=True)[0])
        self.assertTrue(wait_until(lambda: not self.snapshot()["running"], 12.0))
        output = Path(self.snapshot()["output"])
        probe = recorder.probe_output(output, self.caps)
        self.assertGreater(probe.get("duration", 0), 0)
        self.assertLess(probe.get("duration", 0), 3.0)

    def test_real_window_capture_uses_geometry(self):
        xmessage = shutil.which("xmessage")
        xwininfo = shutil.which("xwininfo")
        if not xmessage or not xwininfo:
            self.skipTest("xmessage/xwininfo unavailable")
        process = subprocess.Popen(
            [xmessage, "-name", "wxb-rec-window", "-buttons", "OK:0",
             "-timeout", "20", "WXB recorder window"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop_xmessage():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

        self.addCleanup(stop_xmessage)
        window_id = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not window_id:
            result = subprocess.run([xwininfo, "-root", "-tree"],
                                    capture_output=True, text=True, timeout=3)
            match = re.search(r"^\s*(0x[0-9a-fA-F]+)\s+\"wxb-rec-window\"",
                              result.stdout, re.MULTILINE)
            if match:
                window_id = match.group(1)
                break
            time.sleep(0.05)
        if not window_id:
            self.skipTest("xmessage window was not mapped")
        clean = recorder.normalize_settings({"window_id": window_id})
        x, y, width, height = recorder.window_geometry(clean["window_id"])
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.service.set_recorder_settings(source="window",
                                           window_id=window_id)
        output = self.start_and_stop(0.8)
        self.assertTrue(output.exists())
        probe = recorder.probe_output(output, self.caps)
        self.assertGreater(probe.get("width", 0), 0)
        self.assertGreater(probe.get("height", 0), 0)

    def test_desktop_audio_is_muxed_when_monitor_available(self):
        if not self.caps.get("audio"):
            self.skipTest("Pulse/PipeWire audio is unavailable")
        devices = recorder.list_audio_devices(refresh=True)["desktop"]
        if not devices:
            self.skipTest("no desktop monitor source")
        self.service.set_recorder_settings(desktop_audio=True,
                                           desktop_device=devices[0])
        output = self.start_and_stop(1.0)
        result = subprocess.run(
            [self.caps["ffprobe"], "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
             str(output)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "recording has no audio stream")

    def test_capabilities_do_not_misadvertise_wayland(self):
        # This is a capability-shape check, not a claim that a real Wayland
        # portal exists in this Xvfb process.
        self.assertFalse(self.caps.get("wayland"))
        self.assertTrue(self.caps.get("x11"))
        self.assertIn("screen", self.caps.get("sources", []))
        self.assertNotIn("wayland", self.caps.get("backend", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)