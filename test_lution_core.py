#!/usr/bin/env python3
"""
Lution core tests
=================
The vendored Lution engine, through the exact interface WXB's Game tab uses:
`python3 -m lution_core.cli <op>` printing one JSON object. Every test runs
against a throwaway HOME, so it never touches the real Sober config, fflags,
environment variables or playtime cache.

No display, no root, no network, and it never launches Sober.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from unittest import mock

import wxb_test_isolation

print(wxb_test_isolation.assert_isolated("core"))

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s   %s" % (name, detail))


def run(op, payload=None, home=None, args=(), extra_env=None):
    env = os.environ.copy()
    env["HOME"] = home
    env["PYTHONPATH"] = ROOT
    if extra_env:
        env.update(extra_env)
    cmd = [PY, "-m", "lution_core.cli", op] + list(args)
    proc = subprocess.run(cmd, cwd=ROOT, env=env,
                          input="" if payload is None else json.dumps(payload),
                          capture_output=True, text=True, timeout=180)
    lines = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
    if not lines:
        return {"ok": False, "error": "no stdout", "_exit": proc.returncode,
                "_stderr": (proc.stderr or "")[-300:]}
    try:
        data = json.loads(lines[-1])
    except ValueError:
        return {"ok": False, "error": "bad json: %r" % lines[-1][:120],
                "_exit": proc.returncode}
    data["_exit"] = proc.returncode
    return data


def main():
    home = tempfile.mkdtemp(prefix="wxb-lution-test-")
    try:
        print("\n[lution core: status]")
        status = run("status", home=home)
        check("status returns JSON and exits 0",
              status.get("ok") is True and status.get("_exit") == 0,
              status.get("error") or status.get("_stderr"))
        check("status reports flatpak state",
              isinstance(status.get("installed"), bool),
              status.get("installed"))
        check("status carries the fflag allowlist",
              len(status.get("allowlist") or []) >= 10,
              len(status.get("allowlist") or []))
        check("status carries fflags, env and playtime",
              isinstance(status.get("fflags"), dict)
              and isinstance(status.get("env"), dict)
              and isinstance(status.get("playtime"), dict),
              (status.get("fflags"), status.get("env")))
        check("a fresh HOME has no playtime",
              (status.get("playtime") or {}).get("seconds") == 0,
              status.get("playtime"))
        check("playtime carries a per-place map",
              isinstance((status.get("playtime") or {}).get("places"), dict),
              status.get("playtime"))
        quick = run("playtime", home=home)
        check("the playtime op returns the same shape",
              quick.get("ok") is True
              and isinstance((quick.get("playtime") or {}).get("places"),
                             dict),
              quick)

        print("\n[lution core: fast flags]")
        allowed = (status.get("allowlist") or ["DFIntTextureQualityOverride"])[0]
        saved = run("fflags-set", {allowed: 3, "NotARealFlag": 1}, home=home)
        check("fflags-set keeps allowlisted flags",
              saved.get("ok") is True and saved.get("saved") == 1,
              saved)
        check("fflags-set drops non-allowlisted ones",
              saved.get("removed") == ["NotARealFlag"], saved.get("removed"))
        got = run("fflags-get", home=home)
        check("fflags round-trip through the config file",
              (got.get("fflags") or {}).get(allowed) == 3,
              got.get("fflags"))

        # a flag written behind the allowlist's back is cleaned up
        cfg = os.path.join(home, ".local/share/Lution/sober_config.json")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w") as fh:
            json.dump({"fflags": {allowed: 3, "MadeUp": True}}, fh)
        cleaned = run("fflags-clean", home=home)
        check("fflags-clean removes unknown flags",
              cleaned.get("removed") == ["MadeUp"] and cleaned.get("kept") == 1,
              cleaned)
        check("fflags-clean keeps the allowlisted one",
              (cleaned.get("fflags") or {}).get(allowed) == 3,
              cleaned.get("fflags"))

        print("\n[lution core: environment]")
        envs = run("env-set", {"DFIntTaskSchedulerTargetFps": "60",
                               "BAD KEY": "x", "": "y"}, home=home)
        check("env-set drops invalid keys",
              envs.get("env") == {"DFIntTaskSchedulerTargetFps": "60"},
              envs.get("env"))
        check("env round-trips",
              run("env-get", home=home).get("env")
              == {"DFIntTaskSchedulerTargetFps": "60"},
              run("env-get", home=home).get("env"))

        print("\n[lution core: framerate]")
        fps = run("fps-get", home=home)
        check("fps-get reports a missing settings file without crashing",
              fps.get("ok") is True and fps.get("fps") is None, fps)
        bad = run("fps-set", home=home, args=("not-a-number",))
        check("fps-set rejects a non-number",
              bad.get("ok") is False and "whole number" in (bad.get("error") or ""),
              bad)

        print("\n[lution core: named configs]")
        # A named config is restored by one op: pointer, flags, environment
        # and framerate cap. The settings XML exists here so the cap has
        # somewhere to land (a fresh HOME has no GlobalBasicSettings).
        xml_dir = os.path.join(home, ".var/app/org.vinegarhq.Sober/data/sober",
                               "appData")
        os.makedirs(xml_dir, exist_ok=True)
        with open(os.path.join(xml_dir, "GlobalBasicSettings_13.xml"),
                  "w") as fh:
            fh.write("<roblox><Item><int name=\"FramerateCap\">240"
                     "</int></Item></roblox>")
        applied = run("config-apply", {
            "fflags": {allowed: 7, "Nope": True},
            "env": {"A": "1"},
            "fps": "144",
            "cursor": {"style": "Spark", "color": "Mint", "size": 24},
        }, home=home)
        check("config-apply restores the whole setup in one op",
              applied.get("ok") is True and applied.get("_exit") == 0,
              applied.get("error") or applied.get("_stderr"))
        check("config-apply filters the flag set",
              applied.get("fflags") == {allowed: 7}
              and applied.get("removed") == ["Nope"], applied)
        check("config-apply writes the environment",
              applied.get("env") == {"A": "1"}, applied.get("env"))
        check("config-apply writes the framerate cap",
              applied.get("fps") == 144, applied.get("fps"))
        check("config-apply writes the pointer",
              (applied.get("cursor") or {}).get("style") == "Spark",
              applied.get("cursor"))
        check("the restored cap really landed",
              run("fps-get", home=home).get("fps") == "144",
              run("fps-get", home=home))
        check("the restored flags really landed",
              (run("fflags-get", home=home).get("fflags") or {})
              == {allowed: 7}, run("fflags-get", home=home).get("fflags"))
        check("the restored pointer really landed",
              run("cursors-status", home=home)["cursor"]["style"] == "Spark")

        # A live client must not see its flag table or settings change under
        # it; the environment and pointer are only read at the next launch.
        probe = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "from lution_core import cli, launcher, fflags\n"
            "launcher.sober_is_running = lambda: True\n"
            "before = fflags.get_fflags()\n"
            "out = cli.op_config_apply({'fflags': {'%s': 5},"
            " 'env': {'A': '9'}, 'fps': '30'})\n"
            "out['unchanged'] = fflags.get_fflags() == before\n"
            "print(json.dumps(out))\n" % (ROOT, allowed))
        busy_out = subprocess.run(
            [PY, "-c", probe], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, HOME=home, PYTHONPATH=ROOT))
        try:
            busy = json.loads((busy_out.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            busy = {"raw": (busy_out.stdout or "")[-200:]}
        check("a running client's flags are left alone",
              busy.get("unchanged") is True
              and "flags" in (busy.get("skipped") or []), busy)
        check("a running client's cap is left alone",
              "framerate cap" in (busy.get("skipped") or []), busy)
        check("a running client still gets the environment",
              busy.get("env") == {"A": "9"}, busy.get("env"))
        check("the busy note says why",
              any("running" in str(note) for note in busy.get("notes") or []),
              busy.get("notes"))
        check("config-apply refuses a non-object payload",
              run("config-apply", [1, 2], home=home).get("ok") is False)
        # leave the shared test HOME as the later cursor section expects it
        run("cursors-restore", home=home)

        print("\n[lution core: launch guards]")
        # never launch anything real: with no flatpak on PATH the engine must
        # refuse before it reaches bubblewrap, and say why.
        launched = run("launch", home=home, extra_env={"PATH": "/nonexistent"})
        check("launch refuses when flatpak is unavailable",
              launched.get("ok") is False
              and "not installed" in (launched.get("error") or ""),
              launched)

        print("\n[lution core: gamescope handoff scheduling]")
        # Exercise the real scheduling code against a stubbed window manager.
        # No Gamescope, Sober, Xwayland or display server is started here.
        from lution_core import launcher as llauncher

        class FakeProcess:
            alive = True

            def poll(self):
                return None if self.alive else 0

        def handoff_run(events, clock, process, state_hidden=True):
            def fake_tool(argv, timeout=3):
                events.append(("tool", tuple(argv), timeout))
                if argv[0].endswith("xdotool") and "search" in argv:
                    # The blocking search discovers the output after 1.4 s.
                    clock[0] += 1.4
                    return subprocess.CompletedProcess(argv, 0, "4242\n", "")
                if argv[0].endswith("xprop") and "WM_CLASS" in argv:
                    return subprocess.CompletedProcess(
                        argv, 0, 'WM_CLASS(STRING) = "ld-linux-x86-64.so.2"\n', "")
                if argv[0].endswith("xwininfo"):
                    return subprocess.CompletedProcess(
                        argv, 0, "Map State: IsViewable\n", "")
                if argv[0].endswith("xprop") and "_NET_WM_STATE" in argv:
                    return subprocess.CompletedProcess(
                        argv, 0,
                        "_NET_WM_STATE_HIDDEN\n" if state_hidden else "\n",
                        "")
                return subprocess.CompletedProcess(argv, 0, "", "")

            def fake_sleep(seconds):
                events.append(("sleep", float(seconds)))
                clock[0] += float(seconds)

            def fake_monotonic():
                return clock[0]

            with mock.patch.object(llauncher, "_run_window_tool",
                                   side_effect=fake_tool), \
                 mock.patch.object(llauncher.shutil, "which",
                                   side_effect=lambda name: "/usr/bin/" + name), \
                 mock.patch.object(llauncher.time, "monotonic",
                                   side_effect=fake_monotonic), \
                 mock.patch.object(llauncher.time, "sleep",
                                   side_effect=fake_sleep), \
                 mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
                result = llauncher._activate_gamescope_output(process)
            return result

        events, clock, process = [], [0.0], FakeProcess()
        handoff = handoff_run(events, clock, process)
        tools = [entry[1] for entry in events if entry[0] == "tool"]
        searches = [argv for argv in tools
                    if argv[0].endswith("xdotool") and "search" in argv]
        sleeps = [entry[1] for entry in events if entry[0] == "sleep"]
        removes = [argv for argv in tools
                   if argv[0].endswith("wmctrl") and "remove,hidden" in argv]
        maps = [argv for argv in tools
                if argv[0].endswith("xdotool") and "windowmap" in argv]
        check("gamescope handoff finds the output once",
              len(searches) == 1 and "--sync" in searches[0]
              and searches[0][-2:] == ("--name", "^(gamescope|Sober)$"),
              searches)
        check("gamescope handoff waits before exposing a transient window",
              handoff is True and sleeps == [1.0, 2.5], sleeps)
        check("gamescope handoff applies one late hidden-window repair",
              len(removes) == 2 and len(maps) >= 2, (removes, maps))
        # A client that exits during the delayed pass must not be focused.
        events2, clock2, process2 = [], [0.0], FakeProcess()
        def end_during_sleep(seconds):
            events2.append(("sleep", float(seconds)))
            clock2[0] += float(seconds)
            if float(seconds) >= 1.0:
                process2.alive = False
        # Re-run with a sleep hook that ends the process after discovery.
        def fake_tool2(argv, timeout=3):
            events2.append(("tool", tuple(argv), timeout))
            if argv[0].endswith("xdotool") and "search" in argv:
                clock2[0] += 1.4
                return subprocess.CompletedProcess(argv, 0, "4242\n", "")
            if argv[0].endswith("xprop") and "WM_CLASS" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, 'WM_CLASS(STRING) = "ld-linux-x86-64.so.2"\n', "")
            if argv[0].endswith("xwininfo"):
                return subprocess.CompletedProcess(
                    argv, 0, "Map State: IsViewable\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(llauncher, "_run_window_tool",
                               side_effect=fake_tool2), \
             mock.patch.object(llauncher.shutil, "which",
                               side_effect=lambda name: "/usr/bin/" + name), \
             mock.patch.object(llauncher.time, "monotonic",
                               side_effect=lambda: clock2[0]), \
             mock.patch.object(llauncher.time, "sleep",
                               side_effect=end_during_sleep), \
             mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            ended = llauncher._activate_gamescope_output(process2)
        check("gamescope handoff stops after the client exits",
              ended is False, ended)

        print("\n[lution core: launch does not hold the bridge open]")
        # The UI reads the CLI's stdout until EOF. If the launched game
        # inherits that pipe, the bridge only returns when the game exits -
        # so "Start game" would sit on STARTING and then report a timeout
        # although Sober is running. The child must get its own null streams.
        probe = (
            "import subprocess, sys\n"
            "sys.path.insert(0, %r)\n"
            "from lution_core import cli, launcher, sober\n"
            "sober.is_installed = lambda: True\n"
            "launcher.launch = lambda url=None, **kw: subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(4)'], **kw)\n"
            "print(cli.op_launch())\n" % ROOT)
        started = time.time()
        try:
            gone = subprocess.run([PY, "-c", probe], capture_output=True,
                                  text=True, timeout=10)
        except subprocess.TimeoutExpired:
            gone = None
        elapsed = time.time() - started
        check("launch hands back at once, not when the game exits",
              gone is not None and elapsed < 2.0
              and "'ok': True" in (gone.stdout or ""),
              "%.1fs, %r" % (elapsed, (gone.stdout if gone else None)))

        print("\n[lution core: flatpak timeouts]")
        # `_run` returns None when flatpak is missing or a call times out.
        # That is the offline path, not a crash: remote_commit used to read
        # .returncode straight off it.
        sys.path.insert(0, ROOT)
        from lution_core import sober as lsober
        real_run = lsober._run
        try:
            lsober._run = lambda *a, **k: None
            check("remote_commit treats a timeout as offline",
                  lsober.remote_commit() == "")
            check("installed_commit treats a timeout as offline",
                  lsober.installed_commit() == "")
            check("get_version treats a timeout as offline",
                  lsober.get_version() == "")
        finally:
            lsober._run = real_run

        print("\n[lution core: custom fonts]")
        # a synthetic APK under this HOME: two text faces, one emoji face and
        # one families/ face. Only the two text faces may be replaced.
        apk_dir = os.path.join(home, ".var/app/org.vinegarhq.Sober/data/sober",
                               "packages", "x86_64", "com.roblox.client")
        os.makedirs(apk_dir, exist_ok=True)
        with zipfile.ZipFile(os.path.join(apk_dir, "base.apk"), "w") as zf:
            zf.writestr("assets/content/fonts/Alpha.ttf", b"\x00\x01\x00\x00")
            zf.writestr("assets/content/fonts/Beta.otf", b"OTTO")
            zf.writestr("assets/content/fonts/families/Fam.ttf",
                        b"\x00\x01\x00\x00")
            zf.writestr("assets/content/fonts/RobloxEmoji.ttf",
                        b"\x00\x01\x00\x00")
        font = os.path.join(home, "MyFont.ttf")
        payload = b"\x00\x01\x00\x00" + b"F" * 64
        with open(font, "wb") as fh:
            fh.write(payload)

        st = run("fonts-status", home=home)
        check("fonts-status reads the APK", st.get("ok") is True
              and st["font"]["apk"] is True, st)
        check("fonts-status counts only text faces",
              st["font"]["apk_fonts"] == 2, st["font"])
        check("a clean install has no custom font",
              st["font"]["active"] == 0 and st["font"]["installed"] == "",
              st["font"])

        applied = run("fonts-apply", home=home, args=(font,))
        check("fonts-apply replaces every text face",
              applied.get("ok") is True and applied.get("replaced") == 2,
              applied)
        overlay = os.path.join(home, ".local/share/Lution/overlay/content/fonts")
        check("the overlay holds exactly those faces",
              sorted(os.listdir(overlay)) == ["Alpha.ttf", "Beta.otf"],
              os.listdir(overlay))
        with open(os.path.join(overlay, "Alpha.ttf"), "rb") as fh:
            check("the chosen bytes are what lands in the overlay",
                  fh.read() == payload)
        check("the emoji face is never touched",
              not os.path.exists(os.path.join(overlay, "RobloxEmoji.ttf")))
        check("the families/ assets are never touched",
              not os.path.exists(os.path.join(overlay, "families")))
        check("the font is kept for reapply",
              applied["font"]["installed"] == "font.ttf", applied["font"])
        check("status now reports it active",
              applied["font"]["active"] == 2, applied["font"])

        again = run("fonts-apply", home=home, args=(font,))
        check("applying twice is idempotent",
              again.get("replaced") == 2
              and sorted(os.listdir(overlay)) == ["Alpha.ttf", "Beta.otf"],
              again)

        otf = os.path.join(home, "Other.otf")
        with open(otf, "wb") as fh:
            fh.write(b"OTTO" + b"G" * 32)
        switched = run("fonts-apply", home=home, args=(otf,))
        check("only one kept copy survives a format change",
              switched.get("ok") is True
              and switched["font"]["installed"] == "font.otf",
              switched.get("font") if switched.get("ok") else switched)

        restored = run("fonts-restore", home=home)
        check("fonts-restore removes the overrides",
              restored.get("ok") is True and restored.get("removed") == 2,
              restored)
        check("the overlay is empty after a restore",
              os.listdir(overlay) == [], os.listdir(overlay))
        check("restore clears the kept copy too",
              restored["font"]["installed"] == ""
              and restored["font"]["active"] == 0, restored["font"])

        missing = run("fonts-apply", home=home,
                      args=(os.path.join(home, "nope.ttf"),))
        check("a missing font is refused with a reason",
              missing.get("ok") is False and "not found" in missing["error"],
              missing)
        fake = os.path.join(home, "not-a-font.ttf")
        with open(fake, "w") as fh:
            fh.write("hello")
        bad_font = run("fonts-apply", home=home, args=(fake,))
        check("a renamed non-font is refused",
              bad_font.get("ok") is False
              and "TrueType" in (bad_font.get("error") or ""), bad_font)
        wrong = os.path.join(home, "font.woff")
        with open(wrong, "w") as fh:
            fh.write("wOFF")
        bad_ext = run("fonts-apply", home=home, args=(wrong,))
        check("only .ttf and .otf are accepted",
              bad_ext.get("ok") is False
              and "only .ttf" in (bad_ext.get("error") or ""), bad_ext)
        check("apply without a path is an error",
              run("fonts-apply", home=home).get("ok") is False)

        empty_home = tempfile.mkdtemp(prefix="wxb-nofont-")
        try:
            noapk = run("fonts-apply", home=empty_home, args=(font,))
            check("applying with no APK explains what is missing",
                  noapk.get("ok") is False
                  and "APK" in (noapk.get("error") or ""), noapk)
            check("status without an APK is clean",
                  run("fonts-status", home=empty_home)["font"]["apk"] is False)
        finally:
            shutil.rmtree(empty_home, ignore_errors=True)

        # stress: odd names, URIs, a directory in the way, a bad overlay
        spaced = os.path.join(home, "My Font ü.ttf")
        with open(spaced, "wb") as fh:
            fh.write(b"\x00\x01\x00\x00" + b"S" * 32)
        spaced_ok = run("fonts-apply", home=home, args=(spaced,))
        check("a font path with spaces and unicode works",
              spaced_ok.get("ok") is True and spaced_ok.get("replaced") == 2,
              spaced_ok)
        uri = "file://" + urllib.parse.quote(spaced)
        check("a file:// URI is accepted",
              run("fonts-apply", home=home, args=(uri,)).get("ok") is True)

        run("fonts-restore", home=home)
        blocked = os.path.join(overlay, "Alpha.ttf")
        os.makedirs(blocked, exist_ok=True)
        cleared = run("fonts-apply", home=home, args=(spaced,))
        check("a directory in a face's place is replaced",
              cleared.get("ok") is True and os.path.isfile(blocked), cleared)

        big = os.path.join(home, "Big.ttf")
        with open(big, "wb") as fh:
            fh.write(b"\x00\x01\x00\x00")
            fh.truncate(13 * 1024 * 1024)
        too_big = run("fonts-apply", home=home, args=(big,))
        check("a font over the size cap is refused",
              too_big.get("ok") is False
              and "larger than" in (too_big.get("error") or ""), too_big)

        if os.geteuid() != 0:                     # root ignores the mode
            os.chmod(overlay, 0o500)
            try:
                ro = run("fonts-apply", home=home, args=(spaced,))
            finally:
                os.chmod(overlay, 0o700)
            check("a read-only overlay fails with a reason",
                  ro.get("ok") is False and bool(ro.get("error")), ro)
        run("fonts-restore", home=home)

        # fuzz: empty, upper-case suffix, symlink, and four applies at once
        empty = os.path.join(home, "Empty.ttf")
        open(empty, "wb").close()
        empty_res = run("fonts-apply", home=home, args=(empty,))
        check("an empty file is refused",
              empty_res.get("ok") is False
              and "empty" in (empty_res.get("error") or ""), empty_res)

        upper = os.path.join(home, "Loud.TTF")
        with open(upper, "wb") as fh:
            fh.write(b"\x00\x01\x00\x00" + b"U" * 16)
        upper_ok = run("fonts-apply", home=home, args=(upper,))
        check("an upper-case .TTF suffix works",
              upper_ok.get("ok") is True
              and upper_ok["font"]["installed"] == "font.ttf", upper_ok)

        link = os.path.join(home, "shortcut.ttf")
        os.symlink(upper, link)
        check("a symlinked font works",
              run("fonts-apply", home=home, args=(link,)).get("ok") is True)

        env = dict(os.environ, HOME=home, PYTHONPATH=ROOT)
        procs = [subprocess.Popen(
            [PY, "-m", "lution_core.cli", "fonts-apply", upper],
            cwd=ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [proc.communicate(timeout=60)[0] for proc in procs]
        good = 0
        for out in outputs:
            try:
                good += 1 if json.loads(out.strip().splitlines()[-1]).get("ok") else 0
            except (ValueError, IndexError):
                pass
        check("four simultaneous applies all succeed", good == 4, outputs)
        with open(os.path.join(overlay, "Alpha.ttf"), "rb") as fh:
            with open(upper, "rb") as src:
                check("the overlay holds a whole font after concurrent applies",
                      fh.read() == src.read())
        run("fonts-restore", home=home)

        # the overlay must never depend on the file the user picked: apply,
        # then delete that file, and every face has to stay intact
        picked_res = run("fonts-apply", home=home, args=(upper,))
        payload = b"\x00\x01\x00\x00" + b"U" * 16
        kept_dir = os.path.join(home, ".local/share/Lution/installed_font")
        os.remove(upper)
        check("deleting the picked file leaves the faces whole",
              picked_res.get("ok") is True
              and open(os.path.join(overlay, "Alpha.ttf"), "rb").read() == payload
              and open(os.path.join(overlay, "Beta.otf"), "rb").read() == payload
              and open(os.path.join(kept_dir, "font.ttf"), "rb").read() == payload,
              picked_res)

        # choosing a face that is already a destination: the old order unlinked
        # the destination (which was the source) before linking it
        inside = os.path.join(overlay, "Alpha.ttf")
        inside_res = run("fonts-apply", home=home, args=(inside,))
        check("a font picked from inside the overlay survives itself",
              inside_res.get("ok") is True
              and open(inside, "rb").read() == payload, inside_res)

        # a crash can leave the temp files behind: restore clears them
        stale = (os.path.join(overlay, "Alpha.ttf.wxb-tmp"),
                 os.path.join(kept_dir, ".font.wxb-tmp"))
        for path in stale:
            with open(path, "wb") as fh:
                fh.write(b"half a font")
        run("fonts-restore", home=home)
        check("restore clears interrupted temp files",
              not any(os.path.exists(p) for p in stale), stale)

        print("\n[lution core: custom cursor]")
        cursor = run("cursors-status", home=home)
        check("cursors-status starts clean",
              cursor.get("ok") is True and cursor["cursor"]["active"] == 0,
              cursor)
        applied = run("cursors-apply",
                      {"style": "Heart", "color": "Pink", "size": 40},
                      home=home)
        check("cursors-apply writes every pointer asset",
              applied.get("ok") is True
              and applied["cursor"]["active"] == 3,
              applied)
        texture = os.path.join(
            home, ".local/share/Lution/overlay/content/textures")
        pointers = [
            os.path.join(texture, "Cursors/KeyboardMouse/ArrowCursor.png"),
            os.path.join(texture, "Cursors/KeyboardMouse/ArrowFarCursor.png"),
            os.path.join(texture, "Cursors/KeyboardMouse/IBeamCursor.png"),
        ]
        check("the overlay holds the three mouse pointers",
              all(os.path.isfile(p) for p in pointers), pointers)
        with open(pointers[0], "rb") as fh:
            head = fh.read(8)
        check("the pointer is a real PNG", head == b"\x89PNG\r\n\x1a\n")
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image is not None:
            im = Image.open(pointers[0]).convert("RGBA")
            opaque = [p for p in im.getdata() if p[3] > 200]
            check("the pointer is drawn in the chosen pink",
                  im.size == (64, 64)
                  and any(p[0] > 230 and p[1] < 180 for p in opaque), im.size)
            check("the pointer is transparent around the shape",
                  im.getpixel((0, 0))[3] == 0, im.getpixel((0, 0)))
        check("the choice is remembered",
              run("cursors-status", home=home)["cursor"]["style"] == "Heart")
        check("the kept copy survives",
              os.path.isfile(os.path.join(
                  home, ".local/share/Lution/installed_cursors/cursor.png")))

        # Online cursors use a cached CC0 ZIP; this fixture exercises the
        # exact archive path without making the suite depend on a network.
        print("\n[lution core: online cursor pack]")
        installed = os.path.join(
            home, ".local/share/Lution/installed_cursors")
        os.makedirs(installed, exist_ok=True)
        from PIL import Image, ImageDraw
        fixture_png = os.path.join(installed, "fixture-pointer.png")
        icon = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.polygon(((2, 1), (28, 16), (16, 18), (9, 30)), fill="white")
        icon.save(fixture_png, "PNG")
        pack = os.path.join(installed, "kenney-cursor-pack.zip")
        with zipfile.ZipFile(pack, "w") as archive:
            archive.write(fixture_png,
                          "PNG/Outline/Default/pointer_a.png")
        online = run("cursors-online-apply",
                     {"name": "Classic pointer", "size": 32}, home=home)
        check("a cached online pack applies without network",
              online.get("ok") is True
              and online["cursor"]["mode"] == "online"
              and online["cursor"]["active"] == 3,
              online)
        check("the online source and licence are remembered",
              online["cursor"].get("source", "").startswith("https://")
              and online["cursor"].get("pack_cached") is True,
              online["cursor"])
        check("the online pointers are valid PNGs",
              all(os.path.isfile(p) for p in pointers)
              and open(pointers[0], "rb").read(8) == b"\x89PNG\r\n\x1a\n")
        check("an unknown online cursor is refused",
              run("cursors-online-apply", {"name": "No such cursor"},
                  home=home).get("ok") is False)
        restored_online = run("cursors-restore", home=home)
        check("restoring cursors keeps the downloaded pack cached",
              restored_online.get("ok") is True
              and os.path.isfile(pack),
              restored_online)

        # a font override is orthogonal to the cursor override
        font_dir = os.path.join(home, ".local/share/Lution/overlay/content/fonts")
        os.makedirs(font_dir, exist_ok=True)
        with open(os.path.join(font_dir, "Alpha.ttf"), "wb") as fh:
            fh.write(b"\x00\x01\x00\x00")
        run("cursors-apply", {"style": "Spark", "color": "White",
                              "size": 24}, home=home)
        check("a cursor change leaves fonts alone",
              os.path.isfile(os.path.join(font_dir, "Alpha.ttf")))
        check("a bad shape is refused",
              run("cursors-apply", {"style": "Nope"}, home=home)
              .get("ok") is False)
        check("a bad colour is refused",
              run("cursors-apply", {"color": "Nope"}, home=home)
              .get("ok") is False)
        check("an out-of-range size is refused",
              run("cursors-apply", {"size": 400}, home=home)
              .get("ok") is False)
        restored_cursor = run("cursors-restore", home=home)
        check("cursors-restore removes every pointer",
              restored_cursor.get("removed") == 3
              and restored_cursor["cursor"]["active"] == 0,
              restored_cursor)
        check("the kept cursor copy is gone",
              not os.path.isfile(os.path.join(
                  home, ".local/share/Lution/installed_cursors/cursor.png")))

        print("\n[lution core: crash state and flag cache]")
        cache_dir = os.path.join(
            home, ".var/app/org.vinegarhq.Sober/cache/sober/cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache = os.path.join(cache_dir, "flag_cache.dat")
        with open(cache, "wb") as fh:
            fh.write(b"stale flags")
        run("fflags-set", {allowed: 5}, home=home)
        check("saving flags drops Roblox's stale flag cache",
              not os.path.exists(cache))
        crash_dir = os.path.join(
            home, ".var/app/org.vinegarhq.Sober/data/sober/crashes")
        os.makedirs(crash_dir, exist_ok=True)
        with open(os.path.join(crash_dir, "a crash"), "wb") as fh:
            fh.write(b"dump")
        state = run("crashes", home=home)
        check("the crash op counts Sober's dumps",
              state.get("ok") is True and state.get("count") == 1,
              state)
        check("the crash op names the newest dump",
              state.get("latest_name") == "a crash"
              and state.get("latest") is not None, state)
        check("the crash op carries the log evidence",
              isinstance(state.get("context"), dict)
              and "summary" in state, state)
        check("a machine with no Sober logs says so instead of failing",
              state["context"]["ok"] is False
              and "no Sober logs" in (state["context"]["error"] or "")
              and state.get("summary") == "", state.get("context"))
        check("the running poll tolerates a missing flatpak",
              run("running", home=home,
                  extra_env={"PATH": "/nonexistent"}).get("running") is False)

        # the same evidence, from a log that a killed session left behind
        logs = os.path.join(home, ".var/app/org.vinegarhq.Sober/data/sober",
                            "sober_logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "2026-01-01_10-00-00.log"), "w") as fh:
            fh.write("info: Roblox: 2026-01-01T10:00:00.000Z,1.0,aa,6,Info "
                     "[FLog::Start] boot\n")
            fh.write("info: Roblox: 2026-01-01T10:02:00.000Z,120.0,aa,12 "
                     "[DFLog::X] something gave up\n")
        told = run("crashes", home=home)
        check("the crash op reads the killed session's log",
              told.get("context", {}).get("clean_exit") is False
              and told.get("context", {}).get("ran_for") == 120.0,
              told.get("context"))
        check("the summary is a sentence",
              "2m 00s" in (told.get("summary") or "")
              and "without a clean exit" in (told.get("summary") or ""),
              told.get("summary"))

        print("\n[lution core: argument parsing]")
        import wxb_engine as wxb
        parsed = wxb.parse_kv_lines("# c\nA = 1\nB=two=three\n\nnoequals\n")
        check("parse_kv_lines reads KEY=value and skips junk",
              parsed == {"A": "1", "B": "two=three"}, parsed)
        check("parse_flag_value types bool/int/float/string",
              (wxb.parse_flag_value("TRUE") is True
               and wxb.parse_flag_value("false") is False
               and wxb.parse_flag_value("240") == 240
               and wxb.parse_flag_value("1.5") == 1.5
               and wxb.parse_flag_value("hello") == "hello"))
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
