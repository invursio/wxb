#!/usr/bin/env python3
"""Opt-in real-machine checks for the GameMode tab.

These checks touch the host's installed Flatpak extensions and GameMode
daemon, so they are deliberately not part of the default suite.  Run them
only when you want to verify the real launch pieces on this machine:

    WXB_RUN_GAMEMODE_INTEGRATION=1 python3 test_gamemode_integration.py

Nothing here launches Sober or puts a game window on the desktop.  The
GameMode check starts a throwaway Python process with the real
libgamemodeauto client, proves gamemoded registers it, then stops it and
waits for GameMode to become inactive again.  Each tool is detected on its
own, so a missing Flatpak, MangoHud, GameMode or Gamescope produces a clean
skip rather than a failure on a machine that does not have that feature.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lution_core import launcher  # noqa: E402

OPT_IN = "WXB_RUN_GAMEMODE_INTEGRATION"
ENABLED = os.environ.get(OPT_IN) == "1"


def _run(argv, timeout=15):
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


def _wait_for(check, timeout=6.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


@functools.lru_cache(maxsize=None)
def _flatpak_runtime_has(needle):
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    try:
        result = _run([flatpak, "list", "--runtime",
                       "--columns=application"])
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and needle in result.stdout


@unittest.skipUnless(
    ENABLED,
    "real GameMode integration is opt-in; set %s=1" % OPT_IN)
class GameModeIntegrationTests(unittest.TestCase):
    """Real tools, with a per-component skip when the host lacks one."""

    def test_flatpak_has_the_real_sober_app(self):
        flatpak = shutil.which("flatpak")
        if not flatpak:
            self.skipTest("Flatpak is not installed")
        try:
            result = _run([flatpak, "info", "org.vinegarhq.Sober"])
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest("Flatpak could not be queried: %s" % exc)
        if result.returncode != 0:
            self.skipTest("Sober is not installed in this Flatpak install")
        self.assertIn("org.vinegarhq.Sober", result.stdout)

    def test_mangohud_layer_is_really_deployed(self):
        if not _flatpak_runtime_has("VulkanLayer.MangoHud"):
            self.skipTest("the MangoHud Flatpak Vulkan layer is not installed")

        deployments = (
            launcher._flatpak_deploy_dirs(
                "org.freedesktop.Platform.VulkanLayer.MangoHud", user=True)
            + launcher._flatpak_deploy_dirs(
                "org.freedesktop.Platform.VulkanLayer.MangoHud", user=False)
        )
        layer = next((files for _branch, files in deployments
                      if (files / "bin/mangohud").is_file()
                      and (files / "share/vulkan/implicit_layer.d/"
                           "MangoHud.x86_64.json").is_file()), None)
        if layer is None:
            self.skipTest("MangoHud's deployed layer files were not found")
        self.assertTrue(os.access(layer / "bin/mangohud", os.R_OK))

    def test_mangohud_flag_is_added_to_the_real_flatpak_command(self):
        if not _flatpak_runtime_has("VulkanLayer.MangoHud"):
            self.skipTest("the MangoHud Flatpak Vulkan layer is not installed")
        if not launcher.BWRAP:
            self.skipTest("bubblewrap is not installed")

        with tempfile.TemporaryDirectory(prefix="wxb-mangohud-") as tmp:
            with mock.patch.dict(os.environ, {"WXB_HOME": tmp}):
                try:
                    args = launcher.build_sober_command(mangohud=True)
                except launcher.LaunchError as exc:
                    self.skipTest("real launch command unavailable: %s" % exc)
        self.assertIn("--env=MANGOHUD=1", args)

    def test_gamemode_registers_and_releases_a_real_client(self):
        library = launcher.find_gamemode_library()
        if not library:
            self.skipTest("libgamemodeauto is not installed")
        daemon = shutil.which("gamemoded") or (
            "/usr/bin/gamemoded" if Path("/usr/bin/gamemoded").exists()
            else None)
        if not daemon:
            self.skipTest("gamemoded is not installed")

        def status(pid=None):
            argv = [daemon, "--status"]
            if pid is not None:
                argv.append(str(pid))
            try:
                result = _run(argv, timeout=5)
            except (OSError, subprocess.SubprocessError):
                return ""
            return (result.stdout or "").strip().lower()

        def active(text):
            # "active" is a substring of "inactive"; require both tokens so a
            # released client cannot be mistaken for a registered one.
            return "active" in text and "inactive" not in text

        current = status()
        if not current:
            self.skipTest("gamemoded is not running or cannot report status")
        if active(current):
            self.skipTest("GameMode is already active; close the game first")
        env = launcher.gamemode_env(os.environ.copy())
        self.assertIn(library, env.get("LD_PRELOAD", "").split(":"))

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(8)"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            activated = _wait_for(lambda: active(status(process.pid)))
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        released = _wait_for(lambda: "inactive" in status(), timeout=7)
        self.assertTrue(activated,
                        "gamemoded did not activate the real GameMode client")
        self.assertTrue(
            released,
            "gamemoded did not release GameMode after the client exited")

    def test_gamescope_is_a_runnable_real_compositor(self):
        executable, detail = launcher.gamescope_status()
        if not executable:
            self.skipTest("Gamescope is not installed")
        extension = launcher._gamescope_extension()
        if extension:
            command = [
                str(extension["loader"]),
                "--library-path",
                ":".join(str(path) for path in extension["library_path"]),
                str(extension["binary"]),
                "--version",
            ]
        else:
            command = [executable, "--version"]
        try:
            result = _run(command, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest("Gamescope could not be started: %s" % exc)
        self.assertEqual(result.returncode, 0, (result.stderr or result.stdout)[-400:])
        self.assertIn("gamescope", (result.stdout + result.stderr).lower(),
                      detail)

    def test_service_reports_all_three_real_components(self):
        if not os.environ.get("DISPLAY"):
            self.skipTest("the Studio service needs an X11 display")
        try:
            from wxb_studio_service import StudioService
        except (ImportError, OSError, SystemExit) as exc:
            self.skipTest("Studio service is unavailable: %s" % exc)
        with tempfile.TemporaryDirectory(prefix="wxb-gamemode-service-") as tmp:
            service = StudioService(path=Path(tmp) / "studio.json",
                                    auto_start=False)
            try:
                status = service.gamemode_status(refresh=True)
            finally:
                service.shutdown()
        missing = [key for key, item in status.items()
                   if not item.get("available")]
        if missing:
            self.skipTest("real tools unavailable: %s" % ", ".join(missing))
        self.assertEqual(set(status), {"mangohud", "gamemode", "gamescope"})


if __name__ == "__main__":
    unittest.main()
