# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# builds the command that actually starts sober

import os
import json
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import paths
from . import envvars
from . import log

_BWRAP_FALLBACKS = (
    "/usr/bin/bwrap",
    "/bin/bwrap",
    "/usr/local/bin/bwrap",
    "/usr/libexec/bwrap",
    "/usr/libexec/flatpak-bwrap",
    "/usr/lib/flatpak/bwrap",
)

_GAMEMODE_LIBRARY_FALLBACKS = (
    "/usr/lib/x86_64-linux-gnu/libgamemodeauto.so.0",
    "/usr/lib/x86_64-linux-gnu/libgamemodeauto.so",
    "/usr/lib64/libgamemodeauto.so.0",
    "/usr/lib/libgamemodeauto.so.0",
    "/usr/local/lib/libgamemodeauto.so.0",
)

_GAMESCOPE_FALLBACKS = (
    "/usr/bin/gamescope",
    "/usr/local/bin/gamescope",
    "/usr/games/gamescope",
)

_GAMESCOPE_EXTENSION_ID = "org.freedesktop.Platform.VulkanLayer.gamescope"
_GAMESCOPE_RUNTIME_ID = "org.freedesktop.Platform"
_GAMESCOPE_MOUNT = Path("/usr/lib/extensions/vulkan/gamescope")


def find_bwrap():
    override = os.environ.get("FLATPAK_BWRAP")
    if override and os.access(override, os.X_OK):
        return override

    found = shutil.which("bwrap")
    if found:
        return found

    flatpak = shutil.which("flatpak")
    if flatpak:
        sibling = Path(flatpak).parent / "bwrap"
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)

    for path in _BWRAP_FALLBACKS:
        if os.access(path, os.X_OK):
            return path

    return None


def find_gamemode_library():
    """Path to GameMode's auto client, or None when it is not installed.

    ``gamemoderun`` is a shell wrapper around this library.  Loading the auto
    client into the launcher gives GameMode the same lifetime as the game
    without needing the wrapper to be present on PATH.
    """
    override = os.environ.get("WXB_GAMEMODE_LIB")
    if override and os.access(override, os.R_OK):
        return override
    for path in _GAMEMODE_LIBRARY_FALLBACKS:
        if os.access(path, os.R_OK):
            return path
    for directory in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib",
                      "/usr/local/lib"):
        try:
            matches = sorted(Path(directory).glob("libgamemodeauto.so*"))
        except OSError:
            matches = []
        if matches:
            return str(matches[0])
    return None


def _flatpak_deploy_dirs(app_id, *, user=True):
    """Every deployed branch for one Flatpak runtime/extension.

    Flatpak keeps user installations below XDG_DATA_HOME and system ones
    below /var/lib/flatpak.  Reading the deploy tree directly lets an
    elevated WXB find the desktop user's extension without asking that user's
    session bus, and makes status checks work when ``flatpak`` is on PATH but
    its root/user selection is ambiguous.
    """
    roots = []
    if user:
        home = paths.lution_home()
        roots.append(Path(home) / ".local/share/flatpak")
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg and not os.environ.get("WXB_HOME"):
            roots.append(Path(xdg) / "flatpak")
        custom = os.environ.get("FLATPAK_USER_DIR")
        if custom:
            roots.append(Path(custom))
    else:
        roots.append(Path("/var/lib/flatpak"))

    found = []
    for root in roots:
        branch_root = root / "runtime" / app_id / "x86_64"
        try:
            branches = sorted((p for p in branch_root.iterdir()
                               if p.is_dir()), reverse=True)
        except OSError:
            continue
        for branch in branches:
            # A normal install exposes active/files.  A directly-addressed
            # commit (as seen for the system runtime) has files beside its
            # deployment metadata instead.
            candidates = [branch / "active" / "files", branch / "files"]
            try:
                candidates += [child / "files" for child in branch.iterdir()
                               if child.is_dir()]
            except OSError:
                pass
            for files in candidates:
                if files.is_dir():
                    found.append((branch, files))
                    break
    return found


def _gamescope_extension():
    """The real Gamescope ELF and its matching freedesktop runtime.

    The Flathub ``org.freedesktop.Platform.VulkanLayer.gamescope`` extension
    is a real Gamescope build, not a stub.  It is linked against the runtime
    of the same branch, so it cannot simply be executed with the host loader.
    Return both paths so the launcher can run it through that runtime's
    loader and expose it at the extension's hard-coded mount path.
    """
    for _branch, ext_files in _flatpak_deploy_dirs(_GAMESCOPE_EXTENSION_ID):
        manifest_path = ext_files / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            manifest = {}
        runtime_id = str(manifest.get("runtime") or _GAMESCOPE_RUNTIME_ID)
        runtime_version = str(manifest.get("runtime-version") or "")
        binary = ext_files / "bin/gamescope-brokey"
        xwayland = ext_files / "bin/Xwayland"
        if not (binary.is_file() and xwayland.is_file()
                and os.access(binary, os.X_OK)):
            continue

        runtime_files = None
        for user_install in (True, False):
            for _branch2, candidate in _flatpak_deploy_dirs(
                    runtime_id, user=user_install):
                if runtime_version and _branch2.name != runtime_version:
                    continue
                runtime_files = candidate
                break
            if runtime_files is not None:
                break
        if runtime_files is None:
            continue
        loader = runtime_files / "lib64/ld-linux-x86-64.so.2"
        runtime_lib = runtime_files / "lib/x86_64-linux-gnu"
        if not (loader.is_file() and runtime_lib.is_dir()):
            continue
        return {
            "extension": ext_files,
            "runtime": runtime_files,
            "binary": binary,
            "xwayland": xwayland,
            "loader": loader,
            "library_path": (ext_files / "lib", runtime_lib),
            "branch": runtime_version,
        }
    return None


def find_gamescope():
    """A real Gamescope executable, host package or Flatpak extension.

    A host package is preferred because it uses the host system's Xwayland and
    libraries directly.  When the distro has no candidate, the installed
    Flathub extension supplies the genuine compositor; ``_wrap_gamescope``
    supplies the matching runtime and mount environment for it.

    ``WXB_GAMESCOPE`` only overrides the executable path (used by tests and
    custom installs); it never enables Gamescope.  The Studio setting decides
    that, and defaults to off.
    """
    override = os.environ.get("WXB_GAMESCOPE")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("gamescope")
    if found:
        return found
    for path in _GAMESCOPE_FALLBACKS:
        if os.access(path, os.X_OK):
            return path
    extension = _gamescope_extension()
    if extension:
        return str(extension["binary"])
    return None


def gamescope_status():
    """Return ``(executable, human detail)`` for the GameMode page."""
    override = os.environ.get("WXB_GAMESCOPE")
    if override and os.access(override, os.X_OK):
        return override, "Gamescope override is ready"
    found = shutil.which("gamescope")
    if not found:
        found = next((path for path in _GAMESCOPE_FALLBACKS
                      if os.access(path, os.X_OK)), None)
    if found:
        return found, "Host Gamescope ready"
    extension = _gamescope_extension()
    if extension:
        return (str(extension["binary"]),
                "Flatpak Gamescope extension ready (%s)" % extension["branch"])
    return None, "Gamescope is not installed"


def _x11_display_name():
    """The local X socket name for the current DISPLAY (``:0`` -> ``X0``)."""
    display = os.environ.get("DISPLAY", "")
    if not display.startswith(":"):
        return ""
    number = display[1:].split(".", 1)[0]
    return "X" + number if number.isdigit() else ""


def _write_executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o755)


def _extension_launch_dir(extension):
    """Create loader helpers and a private X11 socket directory.

    The extension's own launcher assumes Flatpak already mounted it at
    /usr/lib/extensions/vulkan/gamescope and that its runtime's libraries are
    the default.  Outside that sandbox we recreate exactly those two things
    for the real compositor: a launcher script for the runtime-linked
    Xwayland and reaper children, and a writable /tmp/.X11-unix that also
    contains the host display socket.
    """
    uid = os.geteuid()
    if uid == 0:
        raw = (os.environ.get("WXB_OWNER_UID")
               or os.environ.get("PKEXEC_UID")
               or os.environ.get("SUDO_UID") or "0")
        uid = int(raw) if str(raw).isdigit() else 0
    base = Path(tempfile.gettempdir()) / ("wxb-gamescope-%s" % uid)
    bin_dir = base / "bin"
    x11_dir = base / "x11"
    for directory in (base, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o755)
        except OSError:
            pass
    x11_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(x11_dir, 0o1777)
    except OSError:
        pass

    loader = shlex.quote(str(extension["loader"]))
    library_path = shlex.quote(":".join(str(p) for p in extension["library_path"]))
    for name, source in (("Xwayland", extension["xwayland"]),
                         ("gamescopereaper",
                          extension["extension"] / "bin/gamescopereaper")):
        if not source.is_file():
            continue
        _write_executable(
            bin_dir / name,
            "#!/bin/sh\nexec %s --library-path %s %s \"$@\"\n"
            % (loader, library_path, shlex.quote(str(source))),
        )

    socket_name = _x11_display_name()
    host_socket = Path("/tmp/.X11-unix") / socket_name if socket_name else None
    if host_socket is not None and host_socket.exists():
        target = x11_dir / socket_name
        if not target.exists():
            target.touch()
        try:
            os.chmod(target, 0o666)
        except OSError:
            pass
    return bin_dir, x11_dir, host_socket


def _gamescope_backend_args():
    """Choose a display backend for this session.

    Gamescope's auto backend selects headless on an X11 desktop, which leaves
    the compositor with no visible output.  SDL opens a nested host window so
    the Gamescope frame is actually shown; Wayland sessions keep their native
    backend.  Force composition in the nested SDL backend: WSI's direct
    scanout never reaches the host window, so the client would otherwise
    create swapchains while the visible window stays transparent.
    """
    if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return ["--backend", "sdl", "--force-composition"]
    return []


def _gamescope_runtime_socket():
    """The Wayland socket Gamescope creates for WSI clients."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        uid = os.geteuid()
        if uid == 0:
            raw = (os.environ.get("WXB_OWNER_UID")
                   or os.environ.get("PKEXEC_UID")
                   or os.environ.get("SUDO_UID") or "0")
            uid = int(raw) if str(raw).isdigit() else 0
        runtime = "/run/user/%s" % uid
    return Path(runtime) / "gamescope-0"


def _wrap_extension_gamescope(extension, args):
    if not BWRAP:
        raise LaunchError("Gamescope needs bubblewrap (bwrap), which was not found.")
    if not _GAMESCOPE_MOUNT.is_dir():
        raise LaunchError(
            "The Gamescope Flatpak extension is installed, but the standard "
            "mount point %s is missing. Create it once with: "
            "sudo mkdir -p %s" % (_GAMESCOPE_MOUNT, _GAMESCOPE_MOUNT)
        )
    bin_dir, x11_dir, host_socket = _extension_launch_dir(extension)
    command = [BWRAP, "--bind", "/", "/", "--dev-bind", "/dev", "/dev",
               "--proc", "/proc"]
    if host_socket is not None and host_socket.exists():
        command += ["--bind", str(x11_dir), "/tmp/.X11-unix",
                    "--ro-bind", str(host_socket),
                    str(x11_dir / host_socket.name)]
    command += [
        "--ro-bind", str(extension["extension"]), str(_GAMESCOPE_MOUNT),
        "--ro-bind", str(bin_dir / "Xwayland"),
        str(_GAMESCOPE_MOUNT / "bin/Xwayland"),
        "--setenv", "PATH",
        "%s:%s:/usr/local/bin:/usr/bin:/bin" % (
            bin_dir, extension["extension"] / "bin"),
    ]
    runtime_lib = extension["runtime"] / "lib/x86_64-linux-gnu"
    spa = runtime_lib / "spa-0.2"
    pipewire = runtime_lib / "pipewire-0.3"
    if spa.is_dir():
        command += ["--setenv", "SPA_PLUGIN_DIR", str(spa)]
    if pipewire.is_dir():
        command += ["--setenv", "PIPEWIRE_MODULE_DIR", str(pipewire)]
    command += ["--", str(extension["loader"]),
                "--library-path",
                ":".join(str(p) for p in extension["library_path"]),
                str(extension["binary"])]
    command += _gamescope_backend_args()
    command += ["--"]
    return command + list(args)


def gamemode_env(base=None):
    """Child environment that asks GameMode to follow the launched game.

    Loading libgamemodeauto into the launcher process registers the game on
    startup and unregisters it when that process exits.  This is the same
    mechanism as ``gamemoderun`` and, unlike toggling a daemon globally, it
    leaves both states honest: off means no preload at all.
    """
    library = find_gamemode_library()
    if not library:
        return base
    child = dict(os.environ if base is None else base)
    current = [item for item in child.get("LD_PRELOAD", "").split(":")
               if item]
    if library not in current:
        current.insert(0, library)
    child["LD_PRELOAD"] = ":".join(current)
    return child


def _wrap_gamescope(args):
    exe = find_gamescope()
    if not exe:
        raise LaunchError(
            "Gamescope is not installed. Install the gamescope package, then "
            "turn the toggle on again."
        )
    extension = _gamescope_extension()
    if extension and Path(exe).resolve() == extension["binary"].resolve():
        return _wrap_extension_gamescope(extension, args)
    return [exe, "--"] + list(args)


def _wrap_gamemode(args):
    """Load GameMode in one short-lived shell, then clear it for children.

    bwrap and Flatpak both inherit the host environment.  Loading the client
    into every descendant registers the same launch several times and can
    leave a D-Bus teardown race in bwrap.  The shell registers once, drops the
    preload, and execs the real launcher, so only the process the daemon
    watches carries the client for the life of the game.
    """
    return ["/bin/sh", "-c", 'unset LD_PRELOAD; exec "$@"', "wxb-gamemode"] \
        + list(args)


def _run_window_tool(argv, timeout=3):
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _gamescope_output_window_id(xdotool, windows):
    """Validate the output-window candidates from one search result."""
    display = os.environ.get("DISPLAY", ":0")
    for window in str(windows or "").split():
        # xdotool's classname query is empty for the SDL window on some X
        # servers; xprop reads the property directly.
        props = _run_window_tool([
            "xprop", "-display", display, "-id", window, "WM_CLASS"])
        if props is None or "ld-linux" not in props.stdout:
            continue
        info = _run_window_tool([
            "xwininfo", "-display", display, "-id", window])
        if info is not None and "IsViewable" in info.stdout:
            return window
    return None


def _gamescope_output_window(xdotool):
    """Find the visible SDL output window without waking every client."""
    found = _run_window_tool(
        [xdotool, "search", "--onlyvisible", "--name",
         "^(gamescope|Sober)$"])
    if not found or found.returncode != 0:
        return None
    return _gamescope_output_window_id(xdotool, found.stdout)


def _wait_gamescope_output_window(xdotool, deadline, process=None):
    """Wait for the SDL output with blocking searches, not a poll storm.

    `xdotool search --sync` watches the X event stream until a matching
    window appears.  The old 0.7 s loop spawned a search (and often two
    validation helpers) every beat for up to 18 s.  This keeps that bounded
    wait in the background thread.  Five-second slices still notice a client
    that exited without waiting the full launch window; the short poll is
    only a fallback for an xdotool too old to understand --sync.
    """
    sync_supported = True
    while time.monotonic() < deadline:
        if process is not None:
            try:
                if process.poll() is not None:
                    return None
            except Exception:
                return None
        if sync_supported:
            remaining = deadline - time.monotonic()
            found = _run_window_tool(
                [xdotool, "search", "--sync", "--onlyvisible", "--name",
                 "^(gamescope|Sober)$"], timeout=min(5.0, remaining))
            if found is not None:
                if found.returncode != 0:
                    sync_supported = False
                    continue
                window = _gamescope_output_window_id(xdotool, found.stdout)
                if window:
                    return window
                # A stale match: check once more after a short beat.
                time.sleep(0.7)
                continue
            # subprocess timed out; loop to check whether the client exited.
            continue
        # Older xdotool: use the existing bounded fallback until deadline.
        window = _gamescope_output_window(xdotool)
        if window:
            return window
        time.sleep(0.7)
    return None


def _activate_gamescope_output(process):
    """Bring Gamescope's host window forward after its client maps it.

    The SDL backend creates an ordinary desktop window, and on some window
    managers it starts behind the browser or studio that launched the game.
    The compositor holds the real client frames, so a merely mapped window is
    not enough for the user to see them.  Find the visible Gamescope output
    and make one delayed visibility pass.  This stays best-effort: missing
    xdotool/wmctrl must never fail the game launch.
    """
    if not os.environ.get("DISPLAY") or process is None:
        return False
    xdotool = shutil.which("xdotool")
    wmctrl = shutil.which("wmctrl")
    if not xdotool and not wmctrl:
        return False
    deadline = time.monotonic() + 18
    window = None
    while time.monotonic() < deadline:
        try:
            if process.poll() is not None:
                return False
        except Exception:
            return False
        if xdotool:
            window = _wait_gamescope_output_window(xdotool, deadline, process)
            if window:
                break
        elif wmctrl:
            listed = _run_window_tool([wmctrl, "-lx"])
            if listed and listed.returncode == 0:
                for line in listed.stdout.splitlines():
                    fields = line.split(None, 4)
                    if len(fields) < 5:
                        continue
                    window, _desktop, klass, _host, title = fields
                    if (title.strip().lstrip("*") == "Sober"
                            and "ld-linux" in klass):
                        break
                else:
                    window = None
                if window:
                    break
        time.sleep(0.7)
    if not window:
        return False
    try:
        above = hex(int(window))
    except ValueError:
        above = window
    # Let SDL finish mapping/retitling; applying "above" to the transient
    # first window can make the final one hidden.
    time.sleep(1.0)
    try:
        if process.poll() is not None:
            return False
    except Exception:
        return False
    if wmctrl:
        _run_window_tool([wmctrl, "-i", "-r", above, "-b", "remove,hidden"])
        _run_window_tool([wmctrl, "-i", "-r", above, "-b", "add,above"])
    if xdotool:
        _run_window_tool([xdotool, "windowmap", window])
        _run_window_tool([xdotool, "windowraise", window])
        _run_window_tool([xdotool, "windowactivate", "--sync", window])
    elif wmctrl:
        _run_window_tool([wmctrl, "-i", "-a", window])
    # One cheap stabilization check for a late map/minimize.  Do not poll or
    # repeatedly focus: that is needless process churn for a bootstrap step.
    time.sleep(2.5)
    if wmctrl:
        state = _run_window_tool([
            "xprop", "-display", os.environ.get("DISPLAY", ":0"),
            "-id", window, "_NET_WM_STATE"])
        if state is not None and "HIDDEN" in state.stdout:
            _run_window_tool(
                [wmctrl, "-i", "-r", above, "-b", "remove,hidden"])
            _run_window_tool(
                [wmctrl, "-i", "-r", above, "-b", "add,above"])
            if xdotool:
                _run_window_tool([xdotool, "windowmap", window])
                _run_window_tool([xdotool, "windowraise", window])
    return True


def _focus_gamescope_output(process):
    if not os.environ.get("DISPLAY"):
        return
    threading.Thread(target=_activate_gamescope_output, args=(process,),
                     name="wxb-gamescope-focus", daemon=True).start()


class LaunchError(Exception):
    pass


BWRAP = find_bwrap()


def userns_available():
    if not BWRAP:
        return False
    try:
        result = subprocess.run(
            [BWRAP, "--bind", "/", "/", "--dev-bind", "/dev", "/dev",
             "--proc", "/proc", "--", "true"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def sober_is_running():
    try:
        result = subprocess.run(
            ["flatpak", "ps", "--columns=pid,application"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == paths.SOBER_APP_ID:
            # A stopped Flatpak leaves a stale row in some session helpers;
            # only a live PID means the game is actually running.
            if os.path.exists("/proc/" + fields[0]):
                return True
    return False


def crash_state(since=None):
    """Sober's own crash dumps: count, newest, and whether one is new.

    Sober writes one opaque file per abnormal exit. That is the only reliable
    way to tell a crash from the user simply closing the game, and it is what
    WXB's crash guard keys on. `since` is a POSIX timestamp; the newest dump
    is "new" when its mtime is greater.
    """
    entries = []
    try:
        for path in paths.SOBER_CRASHES.iterdir():
            if not path.is_file():
                continue
            try:
                entries.append((path.stat().st_mtime, path.name))
            except OSError:
                pass
    except OSError:
        pass
    entries.sort()
    latest = entries[-1] if entries else (None, "")
    new = bool(since is not None and latest[0] is not None
               and latest[0] > float(since))
    return {"ok": True, "count": len(entries),
            "latest": latest[0], "latest_name": latest[1], "new": new}


def seed_config():
    if paths.LUTION_CONFIG.exists():
        return
    paths.LUTION_ROOT.mkdir(parents=True, exist_ok=True)
    if paths.SOBER_CONFIG.exists():
        shutil.copyfile(paths.SOBER_CONFIG, paths.LUTION_CONFIG)
        log.info("Seeded lution config from sober's config.json")
    else:
        paths.LUTION_CONFIG.write_text('{\n    "fflags": {}\n}\n')
        log.info("Sober has no config.json yet, starting a fresh one")


def _overlay_args():
    args = []
    for name in paths.OVERLAY_ROOTS:
        lution_dir = paths.LUTION_OVERLAY / name
        lution_dir.mkdir(parents=True, exist_ok=True)
        args += ["--bind", str(lution_dir), str(paths.SOBER_OVERLAY / name)]
    return args


def invalidate_flag_cache():
    """Drop Roblox's cached flag table after the flags changed.

    Roblox writes ``flag_cache.dat`` with a signature over the values it last
    fetched. Saving a new config without removing it can leave the client
    reading a stale table from the previous launch, which is exactly the kind
    of mixed state that makes graphics flags crash later.
    """
    try:
        paths.FLAG_CACHE.unlink()
        return True
    except OSError:
        return False


def flag_cache_is_stale():
    """True when Roblox cached a flag table before the config last changed.

    ``save_fflags`` drops the cache itself, but a config written by Lution's
    CLI, by an older WXB, or by hand does not go through that path. Comparing
    the two mtimes is the cheap check that catches those, and it is the same
    mixed old/new table the save path guards against.
    """
    try:
        cache = paths.FLAG_CACHE.stat()
        config = paths.LUTION_CONFIG.stat()
    except OSError:
        return False
    return cache.st_mtime < config.st_mtime


def prepare_launch():
    """Drop a stale flag table before starting. True when it did."""
    if not flag_cache_is_stale():
        return False
    return invalidate_flag_cache()


def build_sober_command(url=None, mangohud=False, gamescope=False):
    if not BWRAP:
        raise LaunchError(
            "Could not find bubblewrap (bwrap). Flatpak depends on it, so this "
            "usually means the Flatpak install is incomplete - try reinstalling "
            "flatpak, or install the bubblewrap package."
        )
    if not userns_available():
        raise LaunchError(
            "Bubblewrap could not create a mount namespace here. Flatpak needs "
            "the same capability, so Sober itself would not start either - "
            "check that your kernel allows user namespaces."
        )
    if sober_is_running():
        raise LaunchError(
            "Sober is already running. Close it first, otherwise it keeps the "
            "files it was started with."
        )

    paths.migrate_legacy_overlay()
    seed_config()
    # A crash can leave Roblox's cached flag table describing the previous
    # config. Dropping it costs one refetch and rules out that mixed state.
    prepare_launch()

    args = [BWRAP,
            "--bind", "/", "/",
            "--dev-bind", "/dev", "/dev",
            "--proc", "/proc",
            "--chdir", "/"]
    args += _overlay_args()

    if paths.SOBER_CONFIG.exists():
        args += ["--bind", str(paths.LUTION_CONFIG), str(paths.SOBER_CONFIG)]

    args += ["--", "flatpak", "run", "--filesystem=/tmp"]
    if gamescope:
        # Gamescope's WSI layer connects to this socket.  Flatpak's private
        # /run/user tree does not expose it by default, so mount the socket
        # itself; without this the game falls back to X11 and presents black.
        args.append("--filesystem=%s" % _gamescope_runtime_socket())
    args += envvars.env_flatpak_args()
    if mangohud:
        # The MangoHud Vulkan layer is mounted into Sober by Flatpak.  This
        # per-launch flag is authoritative and disappears with the process;
        # it does not edit the user's Flatpak overrides.
        args.append("--env=MANGOHUD=1")
    args += [paths.SOBER_APP_ID]
    if url:
        args.append(url)
    return args


def launch(url=None, stdout=None, stderr=None, env=None, preexec_fn=None,
           mangohud=False, gamemode=False, gamescope=False):
    args = build_sober_command(url, mangohud=mangohud, gamescope=gamescope)
    if gamescope:
        args = _wrap_gamescope(args)
    if gamemode and find_gamemode_library():
        env = gamemode_env(env)
        args = _wrap_gamemode(args)
    log.info("Launching Sober with lution overlay")
    process = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=env,
                               preexec_fn=preexec_fn)
    if gamescope:
        _focus_gamescope_output(process)
    return process


def launch_plain(url=None, stdout=None, stderr=None, preexec_fn=None,
                 env=None, mangohud=False, gamemode=False, gamescope=False):
    args = ["flatpak", "run", "--filesystem=/tmp"]
    if gamescope:
        args.append("--filesystem=%s" % _gamescope_runtime_socket())
    args += envvars.env_flatpak_args()
    if mangohud:
        args.append("--env=MANGOHUD=1")
    args += [paths.SOBER_APP_ID]
    if url:
        args.append(url)
    if gamescope:
        args = _wrap_gamescope(args)
    if gamemode and find_gamemode_library():
        env = gamemode_env(env)
        args = _wrap_gamemode(args)
    log.info("launching normal sober")
    process = subprocess.Popen(args, stdout=stdout, stderr=stderr, env=env,
                               preexec_fn=preexec_fn)
    if gamescope:
        _focus_gamescope_output(process)
    return process
