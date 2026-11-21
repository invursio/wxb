#!/usr/bin/env python3
"""WXB Roblox runtime tests — no Cordial, no display, no real game.

Run: python3 test_roblox_runtime.py
"""

import os
import json
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import wxb_test_isolation
import roblox_runtime as rr

print(wxb_test_isolation.assert_isolated("runtime"))

FAILS = []
PASSES = []


def check(name, cond, extra=""):
    if cond:
        PASSES.append(name)
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s %s" % (name, extra))


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:
        print("       wrong exception: %r" % other)
        return False
    return False


def fake_bin(folder, name, body):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


CORDIAL = """#!%s
import os, sys, time
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write("\\x1f".join(sys.argv[1:]) + "\\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "1.0")))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
""" % sys.executable

FLATPAK = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_FLATPAK_LOG"
if [ "$1" = "info" ]; then exit 0; fi
exit 0
"""


def test_validation():
    print("\n[validation]")
    for good in ("P1", "couch-left", "A_b-9"):
        check("profile %r accepted" % good,
              rr.validate_profile(good) == good)
    for bad in ("", ".Name", "has space", "-P1", "a" * 65, "café", None):
        check("profile %r refused" % (bad,),
              raises(rr.InvalidProfile, rr.validate_profile, bad))
    check("place id digits", rr.validate_place_id(1818) == "1818")
    for bad in ("", "18x", "-1", "1 2"):
        check("place %r refused" % bad,
              raises(rr.InvalidPlace, rr.validate_place_id, bad))
    job = "1234abcd-12ab-34cd-56ef-1234567890ab"
    check("job uuid accepted", rr.validate_job_id(job) == job)
    check("job bad refused",
          raises(rr.InvalidJob, rr.validate_job_id, "1234"))


def test_argv():
    print("\n[argv]")
    native = rr.CordialInstall("native", ("/usr/bin/cordial",))
    check("launch argv exact",
          native.launch_argv("P1", 1818) ==
          ["/usr/bin/cordial", "--profile", "P1", "--play",
           "--place-id", "1818"])
    check("fullscreen appended",
          native.launch_argv("P1", 1818, fullscreen=True)[-1] == "--fullscreen")
    check("settings sorted",
          native.launch_argv("P1", settings={"b": 2, "a": 1})[-4:] ==
          ["--set", "a=1", "--set", "b=2"])
    check("bad setting refused",
          raises(rr.RobloxRuntimeError, native.launch_argv, "P1",
                 settings={"bad key": 1}))
    flat = rr.CordialInstall("flatpak", ("/usr/bin/flatpak", "run",
                                          rr.APP_ID))
    check("flatpak prefix exact",
          flat.launch_argv("P1", 42)[:4] ==
          ["/usr/bin/flatpak", "run", rr.APP_ID, "--profile"])
    job = "1234abcd-12ab-34cd-56ef-1234567890ab"
    check("same-server url exact",
          rr.same_server_url(1818, job) ==
          "roblox://experiences/start?placeId=1818&gameInstanceId=" + job)
    check("same-server refuses without runtime",
          raises(rr.SameServerUnavailable, native.same_server_argv,
                 "P1", 1818, job))
    run = rr.CordialInstall("native", ("/usr/bin/cordial",),
                            runtime=("cordial-run", "--lib-dir", "/x"))
    check("same-server argv exact",
          run.same_server_argv("P1", 1818, job)[-4:] ==
          ["--profile", "P1", "--join-url",
           "roblox://experiences/start?placeId=1818&gameInstanceId=" + job])


def test_nightcap_argv():
    print("\n[nightcap argv]")
    night = rr.NightcapInstall("native", ("/usr/bin/nightcap",))
    check("nightcap is single-session", not night.supports_many)
    check("nightcap place argv",
          night.launch_argv("ignored", 1818) ==
          ["/usr/bin/nightcap", "--launch-uri",
           "roblox://experiences/start?placeId=1818"],
          night.launch_argv("ignored", 1818))
    check("nightcap home has no flags",
          night.launch_argv("ignored") == ["/usr/bin/nightcap"])
    job = "1234abcd-12ab-34cd-56ef-1234567890ab"
    check("nightcap same-server argv",
          night.same_server_argv("ignored", 1818, job)[-2:] ==
          ["--launch-uri",
           "roblox://experiences/start?placeId=1818&gameInstanceId=" + job])


def test_discover():
    print("\n[discover]")
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin(tmp, "cordial", "#!/bin/sh\nexit 0\n")
        got = rr.discover({"PATH": tmp, "HOME": tmp})
        check("native discovered", got.kind == "native", got)
        check("native path", got.command == (os.path.join(tmp, "cordial"),),
              got.command)
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "flatpak.log")
        fake_bin(tmp, "flatpak", FLATPAK)
        got = rr.discover({"PATH": tmp, "HOME": tmp,
                           "FAKE_FLATPAK_LOG": log})
        check("flatpak discovered", got.kind == "flatpak", got)
        check("flatpak app id", got.command[-1] == rr.APP_ID, got.command)
        check("flatpak info queried",
              "info " + rr.APP_ID in open(log).read())
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        check("missing Cordial refused",
              raises(rr.CordialNotFound, rr.discover,
                     {"PATH": empty, "HOME": tmp}))
    with tempfile.TemporaryDirectory() as tmp:
        path = fake_bin(tmp, "my-cordial", "#!/bin/sh\nexit 0\n")
        got = rr.discover({"PATH": tmp, "HOME": tmp, "WXB_CORDIAL": path,
                           "WXB_CORDIAL_RUN": "cordial-run --lib-dir /x"})
        check("override used", got.command == (path,), got.command)
        check("runtime split", got.runtime ==
              ("cordial-run", "--lib-dir", "/x"), got.runtime)
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin(tmp, "nightcap", "#!/bin/sh\nexit 0\n")
        got = rr.discover_nightcap({"PATH": tmp, "HOME": tmp})
        check("nightcap native discovered", got.kind == "native", got)
        check("nightcap command", got.command ==
              (os.path.join(tmp, "nightcap"),), got.command)
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin(tmp, "mocktail", "#!/bin/sh\necho 'Mocktail 1.0'\n")
        check("upstream mocktail refused",
              raises(rr.NightcapNotFound, rr.discover_nightcap,
                     {"PATH": tmp, "HOME": tmp}))
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin(tmp, "cordial", "#!/bin/sh\nexit 0\n")
        fake_bin(tmp, "nightcap", "#!/bin/sh\nexit 0\n")
        found = rr.discover_backends({"PATH": tmp, "HOME": tmp})
        check("both backends discovered",
              found["cordial"] is not None and found["nightcap"] is not None,
              found)


def test_profile_import():
    print("\n[profile import]")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "cordial", "profiles")
        os.makedirs(root)
        for name in ("Alice", "Alt_2", "Alt-3"):
            os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, "Notes"), "w") as fh:
            fh.write("not a profile")
        for name in (".Hidden", "bad name"):
            os.makedirs(os.path.join(root, name))
        got = rr.discover_profiles(env={"CORDIAL_PROFILE_ROOT": root})
        check("profile dirs discovered", got == ["Alice", "Alt-3", "Alt_2"],
              got)
        check("files and invalid names skipped", ".Hidden" not in got, got)
        path = os.path.join(tmp, "wxb", "roblox.json")
        store = rr.ProfileStore(path)
        store.add("Alice", "Main")
        added = store.sync(got + ["Alice", "bad name"])
        check("only new profiles imported", added == ["Alt-3", "Alt_2"], added)
        check("existing label preserved", store.get("Alice").label == "Main")
        check("no duplicate import", store.sync(got) == [])
        check("import does not alter existing order",
              store.names() == ["Alice", "Alt-3", "Alt_2"], store.names())

    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        root = os.path.join(home, ".var", "app", rr.APP_ID, "data",
                            "cordial", "profiles")
        os.makedirs(os.path.join(root, "FlatpakAlt"))
        got = rr.discover_profiles(env={"HOME": home})
        check("flatpak profile root discovered", got == ["FlatpakAlt"], got)

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "profiles")
        os.makedirs(root)
        got = rr.discover_profiles(env={"HOME": tmp, "XDG_DATA_HOME": tmp})
        check("missing profile root is empty", got == [], got)


def test_profile_crud():
    print("\n[profile crud]")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"HOME": os.path.join(tmp, "home"),
               "XDG_DATA_HOME": os.path.join(tmp, "data")}
        root = os.path.join(env["XDG_DATA_HOME"], "cordial", "profiles")
        path = rr.create_profile("Fresh", env=env)
        check("create uses the native profile root",
              path == os.path.join(root, "Fresh"), path)
        check("create makes a private directory",
              stat.S_IMODE(os.stat(path).st_mode) == 0o700,
              oct(stat.S_IMODE(os.stat(path).st_mode)))
        check("find locates the created profile",
              rr.find_profile_dir("Fresh", env=env) == path)
        check("duplicate create is refused",
              raises(rr.ProfileExists, rr.create_profile, "Fresh", env=env))
        check("invalid create is refused",
              raises(rr.InvalidProfile, rr.create_profile, "bad name", env=env))
        check("delete removes the directory",
              rr.delete_profile("Fresh", env=env) == path
              and not os.path.exists(path))
        check("missing delete is refused",
              raises(rr.ProfileNotFound, rr.delete_profile, "Fresh", env=env))

        target = os.path.join(tmp, "outside")
        os.makedirs(target)
        os.symlink(target, os.path.join(root, "Linked"))
        check("symlinked profile delete is refused",
              raises(rr.ProfileFlagsError, rr.delete_profile, "Linked",
                     env=env) and os.path.isdir(target))

def test_store():
    print("\n[store]")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wxb", "roblox.json")
        store = rr.ProfileStore(path)
        check("missing store is empty", store.load() == [])
        store.add("P1", "Main")
        store.add("P2", "Alt")
        check("names round-trip", store.names() == ["P1", "P2"], store.names())
        check("label kept", store.get("P1").label == "Main")
        check("duplicate refused",
              raises(rr.ProfileExists, store.add, "P1"))
        check("invalid refused",
              raises(rr.InvalidProfile, store.add, ".Name"))
        check("file is private",
              stat.S_IMODE(os.stat(path).st_mode) == 0o600,
              oct(stat.S_IMODE(os.stat(path).st_mode)))
        check("reload sees both", rr.ProfileStore(path).names() ==
              ["P1", "P2"])
        check("remove works", store.remove("P2") and store.names() == ["P1"])
        with open(path, "w") as fh:
            fh.write("{")
        check("corrupt store refused",
              raises(rr.StoreError, rr.ProfileStore(path).load))


def test_alt_flags():
    print("\n[alt flags]")
    flags = rr.performance_flags(15, low_graphics=True)
    check("hard cap sets the engine target",
          flags.get("DFIntTaskSchedulerTargetFps") == 15, flags)
    check("hard cap sets Cordial's row",
          flags.get("CordialFrameRateLimit") == 15, flags)
    check("low graphics is a real preset",
          flags.get("DFIntTextureQualityOverride") == 0
          and flags.get("FIntDebugForceMSAASamples") == 0, flags)
    with tempfile.TemporaryDirectory() as tmp:
        env = {"HOME": tmp, "XDG_DATA_HOME": os.path.join(tmp, "data")}
        path = rr.prepare_profile("Alt1", env=env, cap=15,
                                  low_graphics=False)
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        check("prepare_profile writes the cap before any spawn",
              written.get("DFIntTaskSchedulerTargetFps") == 15
              and written.get("CordialFrameRateLimit") == 15, written)
        path = rr.prepare_profile("Alt1", env=env, cap=0,
                                  low_graphics=False)
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        check("cap 0 removes the engine target and asks Cordial for display",
              "DFIntTaskSchedulerTargetFps" not in written
              and written.get("CordialFrameRateLimit") == "display", written)
        path = rr.prepare_profile("Alt1", env=env, cap=15,
                                  low_graphics=True)
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        check("low graphics writes its managed preset",
              written.get("DFIntTextureQualityOverride") == 0
              and written.get("FFlagDisablePostFx") is True, written)
        path = rr.prepare_profile("Alt1", env=env, cap=15,
                                  low_graphics=False)
        with open(path, encoding="utf-8") as fh:
            written = json.load(fh)
        check("low graphics off removes its preset and keeps the cap",
              "DFIntTextureQualityOverride" not in written
              and "FFlagDisablePostFx" not in written
              and written.get("DFIntTaskSchedulerTargetFps") == 15, written)


def wait_lines(path, count, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [line for line in fh if line.strip()]
            if len(lines) >= count:
                return [line.rstrip("\n").split("\x1f") for line in lines]
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    return []


def test_manager():
    print("\n[manager]")
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "calls.log")
        cordial = fake_bin(tmp, "cordial", CORDIAL)
        env = dict(os.environ)
        env.update({"FAKE_LOG": log, "FAKE_SLEEP": "1.5"})
        install = rr.CordialInstall("native", (cordial,))
        mgr = rr.LaunchManager(install, env=env)
        mgr_q = rr.LaunchManager(install, env=env, cpu_quota=40)
        check("cpu quota is kept per manager", mgr_q.cpu_quota == 40)
        mgr_q.set_cpu_quota(0)
        check("cpu quota can be turned off", mgr_q.cpu_quota == 0)
        mgr_q.set_cpu_quota(9999)
        check("cpu quota is clamped to the supported range",
              mgr_q.cpu_quota == 1600, mgr_q.cpu_quota)
        try:
            launch = mgr.launch("P1", 1818)
            lines = wait_lines(log, 1)
            check("launch wrote argv", lines and lines[0] ==
                  ["--profile", "P1", "--play", "--place-id", "1818"], lines)
            check("process is tracked", launch.running() and
                  len(mgr.running()) == 1)
            check("pid is real", launch.pid > 0)
            check("duplicate profile refused",
                  raises(rr.ProfileBusy, mgr.launch, "P1", 1818))
            old_pid = launch.pid
            restarted = mgr.restart("P1")
            wait_lines(log, 2)
            check("restart replaces the process",
                  restarted.pid != old_pid and restarted.running(),
                  (old_pid, restarted.pid))
            check("restart keeps the place and mode",
                  restarted.place_id == "1818"
                  and restarted.fullscreen is False,
                  (restarted.place_id, restarted.fullscreen))
        finally:
            check("stop reports it stopped", mgr.stop("P1"))
        check("nothing running after stop", mgr.running() == [])
        check("stop again is false", mgr.stop("P1") is False)
        check("restart of an unknown profile is refused",
              raises(rr.LaunchFailed, mgr.restart, "ghost"))
        try:
            a = mgr.launch("P1", 42)
            b = mgr.launch("P2", 42)
            wait_lines(log, 2)
            check("two profiles run", len(mgr.running()) == 2)
            check("status has exit wording",
                  all("running" in s["exit"] for s in mgr.statuses()))
            restarted_all = mgr.restart_all()
            wait_lines(log, 4)
            check("restart all restarts both running clients",
                  len(restarted_all) == 2
                  and all(launch.running() for launch in restarted_all),
                  [launch.pid for launch in restarted_all])
            check("restart all keeps both profiles",
                  sorted(s["profile"] for s in mgr.running())
                  == ["P1", "P2"], mgr.running())
        finally:
            check("stop all stops both", mgr.stop_all() == 2)
        check("all gone", mgr.running() == [])
        check("a and b are separate", a.pid != b.pid)
        short_env = dict(env)
        short_env["FAKE_SLEEP"] = "0.2"
        short = rr.LaunchManager(install, env=short_env)
        short_launch = short.launch("P3", 42)
        deadline = time.monotonic() + 3.0
        while short_launch.running() and time.monotonic() < deadline:
            time.sleep(0.02)
        check("clear finished forgets a naturally exited session",
              short.clear_finished() == 1 and short.statuses() == [])
        check("clear finished is empty when already clear",
              short.clear_finished() == 0)


def test_tiling_geometry():
    print("\n[tiling]")
    area = (0, 0, 1920, 1080)
    one = rr.tile_geometries(1, area)
    check("one window fills the work area", one == [(0, 0, 1920, 1080)], one)
    two = rr.tile_geometries(2, area)
    check("two windows are exact halves", two == [(0, 0, 960, 1080),
                                                   (960, 0, 960, 1080)], two)
    three = rr.tile_geometries(3, area)
    check("three windows have gapless boundaries",
          [cell[0] for cell in three] == [0, 640, 1280]
          and [cell[2] for cell in three] == [640, 640, 640], three)
    odd = rr.tile_geometries(7, area)
    check("odd division preserves every pixel",
          sum(cell[2] for cell in odd) == 1920
          and all(odd[i][0] + odd[i][2] == odd[i + 1][0]
                  for i in range(len(odd) - 1)), odd)
    stacked = rr.tile_geometries(3, area, "rows")
    check("stack on top uses exact full-width heights",
          stacked == [(0, 0, 1920, 360), (0, 360, 1920, 360),
                      (0, 720, 1920, 360)], stacked)
    grid = rr.tile_geometries(3, area, "grid")
    check("grid fills the short final row",
          grid == [(0, 0, 960, 540), (960, 0, 960, 540),
                   (0, 540, 1920, 540)], grid)
    grid4 = rr.tile_geometries(4, area, "grid")
    check("four-up grid is gapless",
          grid4 == [(0, 0, 960, 540), (960, 0, 960, 540),
                    (0, 540, 960, 540), (960, 540, 960, 540)], grid4)
    focus = rr.tile_geometries(3, area, "focus-left")
    check("focus left gives the first client 60 percent",
          focus == [(0, 0, 1152, 1080), (1152, 0, 768, 540),
                    (1152, 540, 768, 540)], focus)
    focus_top = rr.tile_geometries(3, area, "focus-top")
    check("focus top gives the first client 60 percent",
          focus_top == [(0, 0, 1920, 648), (0, 648, 960, 432),
                        (960, 648, 960, 432)], focus_top)
    check("stack alias maps to rows",
          rr.tile_geometries(2, area, "stack") ==
          [(0, 0, 1920, 540), (0, 540, 1920, 540)])
    check("unknown layout falls back to side by side",
          rr.tile_geometries(2, area, "diagonal") == two)


def test_window_tiler():
    print("\n[window tiler]")
    calls = []
    window_lines = ["0x001 0 101 0 0 800 600 Cordial.Cordial host Alt1",
                    "0x002 0 102 0 0 800 600 Cordial.Cordial host Alt2"]

    class Done:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    frame_top = {"value": 0}

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "-root":
            return Done("_NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1080\n")
        if argv[1] == "-lpGx":
            return Done("\n".join(window_lines) + "\n")
        if argv[1] == "-id" and frame_top["value"]:
            return Done("_NET_FRAME_EXTENTS(CARDINAL) = 0, 0, %d, 0\n"
                        % frame_top["value"])
        return Done()

    old_tree = rr.descendant_pids
    rr.descendant_pids = lambda pid: [int(pid)]
    try:
        tiler = rr.WindowTiler(runner=runner,
                               which=lambda name: "/usr/bin/" + name)
        launches = [SimpleNamespace(pid=101, profile="Alt1",
                                    running=lambda: True),
                    SimpleNamespace(pid=102, profile="Alt2",
                                    running=lambda: True)]
        ok, message = tiler.tile(launches)
        check("tiler succeeds for two PID-matched windows", ok, message)
        check("each launch owns a distinct window",
              launches[0].window_id != launches[1].window_id,
              (launches[0].window_id, launches[1].window_id))
        moves = [call[-1] for call in calls
                 if len(call) > 4 and call[-2] == "-e"]
        check("tiler emits exact half widths",
              moves == ["0,0,0,960,1080", "0,960,0,960,1080"], moves)
        check("tiler clears maximized and fullscreen state",
              any("fullscreen" in " ".join(call) for call in calls), calls)
        # Flatpak can expose a window without _NET_WM_PID.  The class/title
        # fallback is limited to Cordial/Roblox surfaces, never arbitrary
        # windows, and still uses distinct windows once each.
        window_lines[:] = [
            "0x003 0 0 0 0 800 600 Cordial.Cordial host Alt1",
            "0x004 0 0 0 0 800 600 Cordial.Cordial host Alt2",
        ]
        calls.clear()
        ok, message = tiler.tile(launches)
        check("tiler falls back to Cordial windows without PIDs",
              ok, message)
        check("fallback keeps per-launch window ids distinct",
              launches[0].window_id != launches[1].window_id,
              (launches[0].window_id, launches[1].window_id))
        moves = [call[-1] for call in calls
                 if len(call) > 4 and call[-2] == "-e"]
        check("fallback tiles each distinct window",
              moves == ["0,0,0,960,1080", "0,960,0,960,1080"], moves)
        calls.clear()
        ok, message = tiler.tile(launches, style="rows")
        moves = [call[-1] for call in calls
                 if len(call) > 4 and call[-2] == "-e"]
        check("tiler applies the selected stack layout",
              ok and moves == ["0,0,0,1920,540", "0,0,540,1920,540"],
              (message, moves))
        # The real X11 WM applies wmctrl width/height to the client area, so a
        # 32px titlebar must be removed from every requested vertical cell.
        frame_top["value"] = 32
        calls.clear()
        ok, message = tiler.tile(launches, style="rows")
        moves = [call[-1] for call in calls
                 if len(call) > 4 and call[-2] == "-e"]
        check("tiler subtracts the X11 frame from vertical cells",
              ok and moves == ["0,0,0,1920,508", "0,0,540,1920,508"],
              (message, moves))
    finally:
        rr.descendant_pids = old_tree


def test_stagger_and_rollback():
    print("\n[stagger/rollback]")
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "calls.log")
        cordial = fake_bin(tmp, "cordial", CORDIAL)
        env = dict(os.environ)
        env.update({"FAKE_LOG": log, "FAKE_SLEEP": "0.6"})
        install = rr.CordialInstall("native", (cordial,))
        calls = []
        mgr = rr.LaunchManager(install, env=env,
                               sleep=lambda s: calls.append(s))
        try:
            mgr.launch_many(["P1", "P2", "P3"], 42, stagger=2.5)
            check("stagger between starts only", calls == [2.5, 2.5], calls)
        finally:
            mgr.stop_all()
        real = subprocess.Popen
        count = {"n": 0}

        def flaky(*a, **kw):
            count["n"] += 1
            if count["n"] == 2:
                raise OSError("boom")
            return real(*a, **kw)

        mgr2 = rr.LaunchManager(install, env=env, popen=flaky)
        check("second launch failure raises",
              raises(rr.LaunchFailed, mgr2.launch_many,
                     ["P1", "P2"], 42, stagger=0))
        check("rollback stopped the first", mgr2.running() == [])


def test_nightcap_manager():
    print("\n[nightcap manager]")
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "calls.log")
        binary = fake_bin(tmp, "nightcap", CORDIAL)
        env = dict(os.environ)
        env.update({"FAKE_LOG": log, "FAKE_SLEEP": "0.8"})
        install = rr.NightcapInstall("native", (binary,))
        mgr = rr.LaunchManager(install, env=env)
        try:
            launch = mgr.launch("nightcap", 1818)
            lines = wait_lines(log, 1)
            check("nightcap launch wrote URI",
                  lines and lines[0] == [
                      "--launch-uri",
                      "roblox://experiences/start?placeId=1818"], lines)
            check("nightcap process tracked", launch.running())
            check("nightcap second session refused",
                  raises(rr.SingleSessionOnly, mgr.launch,
                         "other", 1818))
        finally:
            mgr.stop_all()
        mgr2 = rr.LaunchManager(install, env=env)
        check("nightcap roster launch refused",
              raises(rr.SingleSessionOnly, mgr2.launch_many,
                     ["a", "b"], 1818, stagger=0))


def test_process_tree():
    print("\n[process tree]")
    try:
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(1)"])
    except OSError as exc:
        print("  SKIP no subprocess (%s)" % exc)
        return
    try:
        pids = rr.descendant_pids(proc.pid)
        check("tree includes launcher", proc.pid in pids, pids)
        stats = rr.process_tree_stats(proc.pid)
        check("tree has RSS", (stats.get("rss_mb") or 0) > 0, stats)
        time.sleep(0.2)
        again = rr.process_tree_stats(proc.pid)
        check("tree reports CPU after a second sample",
              again.get("cpu_percent") is not None
              and again["cpu_percent"] >= 0, again)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    install = rr.CordialInstall("native", ("/usr/bin/cordial",))
    mgr = rr.LaunchManager(install, ionice=True)
    wrapped = mgr._wrap("P1", ["cordial"])
    if rr.shutil.which("ionice"):
        check("ionice class is applied",
              "-c2" in wrapped and "-n7" in wrapped, wrapped)
    quota_mgr = rr.LaunchManager(install, cpu_quota=25)
    quota_mgr._quota_checked = True       # do not spawn systemd in this test
    wrapped = quota_mgr._wrap("P1", ["cordial"])
    check("native quota uses a transient systemd scope",
          any("systemd-run" in str(part) for part in wrapped)
          and "CPUQuota=25%" in wrapped, wrapped)
    flat = rr.CordialInstall("flatpak", ("flatpak", "run", rr.APP_ID))
    flat_mgr = rr.LaunchManager(flat, cpu_quota=25)
    wrapped = flat_mgr._wrap("P1", ["flatpak", "run", rr.APP_ID])
    check("flatpak quota is not wrapped in an outer scope",
          "systemd-run" not in wrapped, wrapped)


def test_privilege_prefix():
    print("\n[privilege]")
    install = rr.CordialInstall("native", ("/usr/bin/cordial",))
    # Force the root branch deterministically without needing root: monkeypatch.
    old = rr.os.geteuid
    rr.os.geteuid = lambda: 0
    try:
        mgr = rr.LaunchManager(install, drop_uid=1000, drop_gid=1000)
        prefix = mgr._privilege_prefix()
        check("setpriv drops uid and gid",
              prefix[:2] == [rr.shutil.which("setpriv"), "--reuid=1000"],
              prefix)
        check("setpriv ends with --", prefix[-1] == "--")
    finally:
        rr.os.geteuid = old


def test_exit_words():
    print("\n[exit]")
    check("normal", rr.describe_exit(0) == "game ended normally")
    check("busy profile", "already open" in rr.describe_exit(3))
    check("signal", "signal 15" in rr.describe_exit(-15))
    check("running", rr.describe_exit(None) == "running")


def main():
    test_validation()
    test_argv()
    test_nightcap_argv()
    test_discover()
    test_profile_import()
    test_profile_crud()
    test_store()
    test_alt_flags()
    test_manager()
    test_tiling_geometry()
    test_window_tiler()
    test_stagger_and_rollback()
    test_nightcap_manager()
    test_process_tree()
    test_privilege_prefix()
    test_exit_words()
    print("\n%d passed, %d failed" % (len(PASSES), len(FAILS)))
    if FAILS:
        for name in FAILS:
            print("  failed: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
