#!/usr/bin/env python3
"""
WXB — Roblox on Linux runtime
=============================
The RoALT features that survive on Linux, kept in one removable module:

  * discover Cordial (native binary or Flatpak)
  * discover Nightcap as a single-session desktop runtime
  * remember named profiles (one account per profile)
  * launch one profile, or several, into an experience
  * stop every launched client, as a process group
  * build the engine's own specific-server deep link
  * drop root back to the desktop owner before starting a game
  * measure a whole client process tree, not only its launcher

What it deliberately does not pretend to do
-------------------------------------------
Cordial's supported shell CLI can launch N profiles into the same *place*:

    cordial --profile P1 --play --place-id 1818

but it has no flag for a specific server.  The engine's deep link accepts
``gameInstanceId`` and ``cordial-run`` accepts ``--join-url``; that lower
level runtime needs its build store, so this module only uses it when the
caller supplies a working ``WXB_CORDIAL_RUN`` prefix.  Otherwise
``launch_same_server`` raises with the exact alternative: pick the server in
each Cordial window's Servers list.  A dead button that silently joined the
wrong server would be worse than an honest refusal.

No third-party imports, no background threads, no polling.  The UI decides
when to call statuses(); this module only performs one syscall per process.

Nightcap is deliberately single-session: its current build takes a per-user
instance lock, and `--profile` is a Vulkan trace output, not an account
profile.  Cordial is the multi-account path.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

APP_ID = "io.github.luohoa97.Cordial"
NIGHTCAP_APP_ID = "io.github.CoderDayton.nightcap"
DEFAULT_STAGGER = 3.0
DEFAULT_FAST_STAGGER = 0.8

# Cordial profile names: letters, digits, '-' and '_'.  '.Name' (a guest) is
# refused by Cordial itself; the dot is refused here first.
PROFILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
PLACE_RE = re.compile(r"^[0-9]{1,20}$")
JOB_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SETTING_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

EXIT_MEANINGS = {
    0: "game ended normally",
    1: "could not start",
    2: "bad command line",
    3: "profile already open",
    4: "no Roblox build to run",
    5: "could not open a window",
}


class RobloxRuntimeError(Exception):
    """Base for every refusal or launch failure."""


class InvalidProfile(RobloxRuntimeError, ValueError):
    pass


class InvalidPlace(RobloxRuntimeError, ValueError):
    pass


class InvalidJob(RobloxRuntimeError, ValueError):
    pass


class CordialNotFound(RobloxRuntimeError):
    pass


class NightcapNotFound(RobloxRuntimeError):
    pass


class SingleSessionOnly(RobloxRuntimeError):
    pass


class ProfileBusy(RobloxRuntimeError):
    pass


class ProfileExists(RobloxRuntimeError):
    pass


class ProfileNotFound(RobloxRuntimeError):
    pass


class StoreError(RobloxRuntimeError):
    pass


class ProfileFlagsError(RobloxRuntimeError):
    """A profile's flags.json could not be read or written."""


class SameServerUnavailable(RobloxRuntimeError):
    pass


class LaunchFailed(RobloxRuntimeError):
    pass


class CpuQuotaUnavailable(RobloxRuntimeError):
    """A requested cgroup CPU quota could not be created on this host."""


def validate_profile(name: Any) -> str:
    """Return a clean profile name, or raise."""
    if not isinstance(name, str) or not PROFILE_RE.fullmatch(name):
        raise InvalidProfile(
            "Cordial profile names are 1-64 letters, digits, '-' or '_' "
            "('.Name' is refused by Cordial)")
    return name


def validate_place_id(place_id: Any) -> str:
    text = str(place_id).strip()
    if not PLACE_RE.fullmatch(text):
        raise InvalidPlace("place id must be digits only, e.g. 1818")
    return text


def validate_job_id(job_id: Any) -> str:
    text = str(job_id).strip()
    if not JOB_RE.fullmatch(text):
        raise InvalidJob("job id must be the server UUID, e.g. "
                         "1234abcd-12ab-34cd-56ef-1234567890ab")
    return text


def same_server_url(place_id: Any, job_id: Any) -> str:
    """The engine's own specific-server deep link."""
    query = urllib.parse.urlencode({
        "placeId": validate_place_id(place_id),
        "gameInstanceId": validate_job_id(job_id),
    })
    return "roblox://experiences/start?" + query


def place_url(place_id: Any) -> str:
    """The direct place form accepted by Cordial and Nightcap."""
    return "roblox://experiences/start?" + urllib.parse.urlencode({
        "placeId": validate_place_id(place_id)})


def describe_exit(code: Any) -> str:
    if code is None:
        return "running"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "exited"
    if code < 0:
        return "killed by signal %d" % -code
    return EXIT_MEANINGS.get(code, "exited with %d" % code)


@dataclass(frozen=True)
class CordialInstall:
    """How to invoke Cordial on this machine."""

    kind: str
    command: tuple[str, ...]
    runtime: tuple[str, ...] | None = None
    app_id: str = APP_ID

    def __post_init__(self):
        if self.kind not in ("native", "flatpak"):
            raise ValueError("kind must be native or flatpak")
        if not self.command:
            raise ValueError("empty Cordial command")

    @property
    def backend(self) -> str:
        return "cordial"

    @property
    def label(self) -> str:
        return "Cordial"

    @property
    def supports_many(self) -> bool:
        return True

    @property
    def same_server_available(self) -> bool:
        return self.runtime is not None

    def launch_argv(self, profile: Any, place_id: Any = None, *,
                    fullscreen: bool = False,
                    settings: Mapping[str, Any] | None = None) -> list[str]:
        args = ["--profile", validate_profile(profile), "--play"]
        if place_id is not None:
            args += ["--place-id", validate_place_id(place_id)]
        if fullscreen:
            args.append("--fullscreen")
        for key in sorted(settings or {}):
            if not SETTING_RE.fullmatch(str(key)):
                raise RobloxRuntimeError("bad Cordial setting name: %r" % key)
            text = str(settings[key])
            if any(ch in text for ch in "\r\n\0"):
                raise RobloxRuntimeError("bad Cordial setting value")
            args += ["--set", "%s=%s" % (key, text)]
        return [*self.command, *args]

    def same_server_argv(self, profile: Any, place_id: Any,
                         job_id: Any) -> list[str]:
        if self.runtime is None:
            raise SameServerUnavailable(
                "Cordial's supported shell CLI has no --join-url flag, so WXB "
                "cannot send a profile to one specific server. Set "
                "WXB_CORDIAL_RUN to a working 'cordial-run ...' prefix "
                "(including its --lib-dir/--apk), or join the server from the "
                "Servers list in each Cordial window.")
        url = same_server_url(place_id, job_id)
        return [*self.runtime, "--profile", validate_profile(profile),
                "--join-url", url]


@dataclass(frozen=True)
class NightcapInstall:
    """How to invoke Nightcap, a single-session desktop runtime."""

    kind: str
    command: tuple[str, ...]
    app_id: str = NIGHTCAP_APP_ID

    def __post_init__(self):
        if self.kind not in ("native", "flatpak"):
            raise ValueError("kind must be native or flatpak")
        if not self.command:
            raise ValueError("empty Nightcap command")

    @property
    def backend(self) -> str:
        return "nightcap"

    @property
    def label(self) -> str:
        return "Nightcap"

    @property
    def supports_many(self) -> bool:
        return False

    @property
    def same_server_available(self) -> bool:
        return True

    def launch_argv(self, profile: Any, place_id: Any = None, *,
                    fullscreen: bool = False,
                    settings: Mapping[str, Any] | None = None) -> list[str]:
        # Nightcap has one account and one window. `profile` is accepted so
        # the same LaunchManager can drive it; it is not a Nightcap flag.
        del profile, fullscreen, settings
        args: list[str] = []
        if place_id is not None:
            args += ["--launch-uri", place_url(place_id)]
        return [*self.command, *args]

    def same_server_argv(self, profile: Any, place_id: Any,
                         job_id: Any) -> list[str]:
        del profile
        return [*self.command, "--launch-uri",
                same_server_url(place_id, job_id)]


def _flatpak_has(flatpak: str, app_id: str,
                 env: Mapping[str, str]) -> bool:
    try:
        result = subprocess.run([flatpak, "info", app_id],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                env=dict(env), timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def discover(env: Mapping[str, str] | None = None) -> CordialInstall:
    """Find a native Cordial first, then a Flatpak app.

    ``WXB_CORDIAL`` may name a binary explicitly.  ``WXB_CORDIAL_RUN`` is
    optional and is parsed as a shell-like prefix for the low-level runtime.
    """
    env = dict(os.environ if env is None else env)
    path = env.get("PATH", os.defpath)

    override = env.get("WXB_CORDIAL")
    if override:
        if os.sep not in override:
            found = shutil.which(override, path=path)
            if not found:
                raise CordialNotFound("WXB_CORDIAL %r is not on PATH" % override)
            command, kind = (found,), "native"
        else:
            if not os.access(override, os.X_OK):
                raise CordialNotFound("WXB_CORDIAL %r is not executable"
                                      % override)
            command, kind = (override,), "native"
    else:
        native = shutil.which("cordial", path=path)
        if native:
            command, kind = (native,), "native"
        else:
            flatpak = shutil.which("flatpak", path=path)
            if flatpak and _flatpak_has(flatpak, APP_ID, env):
                command, kind = (flatpak, "run", APP_ID), "flatpak"
            else:
                raise CordialNotFound(
                    "Cordial is not installed: no 'cordial' on PATH and no "
                    "Flatpak app %s. Install Cordial, then press Recheck."
                    % APP_ID)

    runtime = None
    run_override = env.get("WXB_CORDIAL_RUN", "").strip()
    if run_override:
        runtime = tuple(shlex.split(run_override))
        if not runtime:
            raise CordialNotFound("WXB_CORDIAL_RUN is empty")
    return CordialInstall(kind=kind, command=command, runtime=runtime)


def _nightcap_version_ok(path: str, env: Mapping[str, str]) -> bool:
    """The distribution package installs `mocktail`; verify its fork."""
    try:
        done = subprocess.run([path, "--version"],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=dict(env), timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    text = ((done.stdout or "") + "\n" + (done.stderr or "")).lower()
    return "nightcap" in text


def discover_nightcap(env: Mapping[str, str] | None = None) -> NightcapInstall:
    """Find Nightcap, native or Flatpak, without starting a game.

    Nightcap distribution packages install `/usr/bin/mocktail`, so a bare
    `mocktail` is only accepted when its `--version` says Nightcap.  That
    keeps an upstream Mocktail install from being mistaken for this fork.
    """
    env = dict(os.environ if env is None else env)
    path = env.get("PATH", os.defpath)
    override = env.get("WXB_NIGHTCAP", "").strip()
    if override:
        if os.sep not in override:
            found = shutil.which(override, path=path)
            if not found:
                raise NightcapNotFound(
                    "WXB_NIGHTCAP %r is not on PATH" % override)
            command = found
        else:
            if not os.access(override, os.X_OK):
                raise NightcapNotFound(
                    "WXB_NIGHTCAP %r is not executable" % override)
            command = override
        return NightcapInstall("native", (command,))

    native = shutil.which("nightcap", path=path)
    if not native:
        candidate = shutil.which("mocktail", path=path)
        if candidate and _nightcap_version_ok(candidate, env):
            native = candidate
    if native:
        return NightcapInstall("native", (native,))

    flatpak = shutil.which("flatpak", path=path)
    if flatpak and _flatpak_has(flatpak, NIGHTCAP_APP_ID, env):
        return NightcapInstall("flatpak", (flatpak, "run", NIGHTCAP_APP_ID))
    raise NightcapNotFound(
        "Nightcap is not installed: no 'nightcap' on PATH and no Flatpak "
        "app %s. Install Nightcap, then press Recheck." % NIGHTCAP_APP_ID)


def discover_backends(env: Mapping[str, str] | None = None) -> dict:
    """Discover both runtimes in one lazy pass.

    Returns ``{"cordial": install|None, "nightcap": install|None,
    "errors": {backend: message}}``.  A missing runtime is data, not an
    exception, so the UI can offer the one that is installed.
    """
    env = dict(os.environ if env is None else env)
    out = {"cordial": None, "nightcap": None, "errors": {}}
    try:
        out["cordial"] = discover(env)
    except RobloxRuntimeError as exc:
        out["errors"]["cordial"] = str(exc)
    try:
        out["nightcap"] = discover_nightcap(env)
    except RobloxRuntimeError as exc:
        out["errors"]["nightcap"] = str(exc)
    return out


def discover_profiles(install=None, env: Mapping[str, str] | None = None) -> list[str]:
    """Profile directory names Cordial already has, without reading cookies.

    Cordial's profile root is ``$XDG_DATA_HOME/cordial/profiles``.  A Flatpak
    keeps its own data root under ``~/.var/app/<app-id>/data``.  The optional
    ``CORDIAL_PROFILE_ROOT`` override is honored, and only directory names
    that pass Cordial's own profile-name rule are returned.
    """
    env = dict(os.environ if env is None else env)
    home = env.get("HOME") or os.path.expanduser("~")
    roots: list[str] = []
    override = env.get("CORDIAL_PROFILE_ROOT", "").strip()
    if override:
        roots.append(override)
    else:
        data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        roots.append(os.path.join(data, "cordial", "profiles"))
        roots.append(os.path.join(home, ".var", "app", APP_ID, "data",
                                  "cordial", "profiles"))
    names: set[str] = set()
    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir and PROFILE_RE.fullmatch(entry.name):
                names.add(entry.name)
    return sorted(names)


def default_store_path(env: Mapping[str, str] | None = None) -> str:
    env = dict(os.environ if env is None else env)
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "wxb", "roblox.json")


def _children_of(pid: int) -> list[int]:
    try:
        with open("/proc/%d/task/%d/children" % (pid, pid),
                  encoding="utf-8") as fh:
            return [int(part) for part in fh.read().split() if part.isdigit()]
    except (OSError, ValueError):
        return []


def _pid_alive(pid: Any) -> bool:
    """True only while the PID exists and is not a zombie."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            fields = fh.read().split()
        return len(fields) > 2 and fields[2] != "Z"
    except OSError:
        return False


def descendant_pids(pid: Any) -> list[int]:
    """The launcher and every process below it, read on demand.

    Flatpak and the Roblox engine put the visible window below the process
    WXB starts, so focusing or tiling by the launcher PID alone misses it.
    """
    try:
        root = int(pid)
    except (TypeError, ValueError):
        return []
    if root <= 0:
        return []
    seen: list[int] = []
    todo = [root]
    while todo and len(seen) < 256:
        current = todo.pop()
        if current in seen:
            continue
        seen.append(current)
        todo.extend(child for child in _children_of(current)
                    if child not in seen)
    return seen


def _tile_edges(start: int, length: int, count: int) -> list[int]:
    """Exact shared boundaries for ``count`` cells inside one span."""
    count = min(max(0, count), length)
    if count <= 0:
        return []
    return [start + (length * index) // count for index in range(count + 1)]


def _row_cells(count: int, x: int, y: int, width: int,
               height: int) -> list[tuple[int, int, int, int]]:
    edges = _tile_edges(x, width, count)
    return [(edges[index], y, edges[index + 1] - edges[index], height)
            for index in range(len(edges) - 1)]


def _column_cells(count: int, x: int, y: int, width: int,
                  height: int) -> list[tuple[int, int, int, int]]:
    edges = _tile_edges(y, height, count)
    return [(x, edges[index], width, edges[index + 1] - edges[index])
            for index in range(len(edges) - 1)]


def tile_geometries(count: Any, work_area: tuple[int, int, int, int],
                    style: str = "columns"
                    ) -> list[tuple[int, int, int, int]]:
    """Split a work area into ``count`` exact, gapless cells.

    Boundaries are rounded independently so every pixel belongs to exactly
    one cell.  ``style`` selects the arrangement: side-by-side columns,
    stacked rows, a balanced grid, or a large focus pane beside/above the
    remaining clients.  Unknown styles fall back to columns so a stale
    settings file can never break launching.
    """
    try:
        count = int(count)
        x, y, width, height = (int(value) for value in work_area)
    except (TypeError, ValueError):
        return []
    if count <= 0 or width <= 0 or height <= 0:
        return []
    key = str(style or "columns").strip().lower().replace("_", "-")
    key = {"side-by-side": "columns", "stacked": "rows",
           "stack": "rows", "top": "focus-top"}.get(key, key)
    if count == 1:
        return [(x, y, width, height)]
    if key == "rows":
        count = min(count, height)
        return _column_cells(count, x, y, width, height)
    if key == "grid":
        count = min(count, width * height)
        cols = int(count ** 0.5)
        while cols * cols < count:
            cols += 1
        rows = (count + cols - 1) // cols
        y_edges = _tile_edges(y, height, rows)
        cells = []
        remaining = count
        for row in range(rows):
            in_row = min(cols, remaining)
            row_height = y_edges[row + 1] - y_edges[row]
            cells.extend(_row_cells(in_row, x, y_edges[row], width,
                                    row_height))
            remaining -= in_row
        return cells
    if key == "focus-left" and width >= 2:
        count = min(count, height)
        split = x + max(1, min(width - 1, (width * 3) // 5))
        cells = [(x, y, split - x, height)]
        cells.extend(_column_cells(count - 1, split, y, width - (split - x),
                                   height))
        return cells
    if key == "focus-top" and height >= 2:
        count = min(count, width)
        split = y + max(1, min(height - 1, (height * 3) // 5))
        cells = [(x, y, width, split - y)]
        cells.extend(_row_cells(count - 1, x, split, width, height - (split - y)))
        return cells
    count = min(count, width)
    return _row_cells(count, x, y, width, height)


class WindowTiler:
    """Arrange the visible X11 windows owned by WXB's Cordial launches.

    Cordial has no tiling flag.  The supported multi-account CLI only knows
    which profile a window belongs to, so WXB maps each tracked launch's
    process tree to its X11 window via ``_NET_WM_PID`` and uses the EWMH
    work area (not the full screen) for the row.  This is deliberately an
    X11 helper: a Wayland compositor does not allow a client to place another
    client's windows this way.
    """

    def __init__(self, *, runner=None, which=None, env=None) -> None:
        self._runner = runner or subprocess.run
        self._which = which or shutil.which
        self.env = dict(env) if env is not None else None
        self.wmctrl = self._which("wmctrl")
        self.xprop = self._which("xprop")
        self.xrandr = self._which("xrandr")

    def _run(self, args):
        try:
            return self._runner(args, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=3,
                                env=self.env)
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _command_failed(done) -> bool:
        return done is None or getattr(done, "returncode", 1) != 0

    def work_area(self):
        """The EWMH work area as ``(x, y, width, height)``, or None."""
        if self.xprop:
            done = self._run([self.xprop, "-root", "_NET_WORKAREA"])
            if not self._command_failed(done):
                match = re.search(
                    r"=\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)",
                    done.stdout or "")
                if match:
                    return tuple(int(value) for value in match.groups())
        if self.wmctrl:
            done = self._run([self.wmctrl, "-d"])
            if not self._command_failed(done):
                for line in (done.stdout or "").splitlines():
                    match = re.search(
                        r"WA:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", line)
                    if match:
                        x, y, width, height = (int(value)
                                               for value in match.groups())
                        return x, y, width, height
        if self.xrandr:
            done = self._run([self.xrandr, "--current"])
            if not self._command_failed(done):
                match = re.search(r"current\s+(\d+)\s*x\s*(\d+)",
                                  done.stdout or "")
                if match:
                    return 0, 0, int(match.group(1)), int(match.group(2))
        return None

    def _frame_extents(self, window_id: str):
        """``(left, right, top, bottom)`` decoration extents for one window."""
        if not self.xprop:
            return 0, 0, 0, 0
        done = self._run([self.xprop, "-id", window_id,
                          "_NET_FRAME_EXTENTS"])
        if self._command_failed(done):
            return 0, 0, 0, 0
        match = re.search(
            r"=\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)",
            done.stdout or "")
        if not match:
            return 0, 0, 0, 0
        return tuple(max(0, int(value)) for value in match.groups())

    def windows(self):
        """Visible top-level X11 windows with PID, geometry and title."""
        if not self.wmctrl:
            return None
        done = self._run([self.wmctrl, "-lpGx"])
        if self._command_failed(done):
            return None
        out = []
        for line in (done.stdout or "").splitlines():
            fields = line.split(None, 9)
            if len(fields) < 9 or not fields[0].startswith("0x"):
                continue
            try:
                desktop = int(fields[1])
                pid = int(fields[2])
                x, y, width, height = (int(value) for value in fields[3:7])
            except (TypeError, ValueError):
                continue
            if desktop < 0 or width <= 0 or height <= 0:
                continue
            out.append({
                "id": fields[0], "desktop": desktop, "pid": pid,
                "x": x, "y": y, "width": width, "height": height,
                "class": fields[7],
                "title": fields[9] if len(fields) > 9 else "",
            })
        return out

    @staticmethod
    def _window_for(launch, windows):
        try:
            pids = set(descendant_pids(launch.pid))
        except (TypeError, ValueError):
            return None
        candidates = [window for window in windows
                      if window["pid"] in pids]
        if not candidates:
            return None
        # A Flatpak client can briefly have more than one mapped surface;
        # the game window is the largest one, not a small helper popup.
        return max(candidates, key=lambda window:
                   window["width"] * window["height"])

    @staticmethod
    def _looks_like_client(window) -> bool:
        """Recognise a Cordial/Roblox surface when _NET_WM_PID is absent."""
        label = (str(window.get("class", "")) + " " +
                 str(window.get("title", ""))).lower()
        return "cordial" in label or "roblox" in label

    def tile(self, launches, style: str = "columns"):
        """Place each live launch in an exact, gapless work-area cell."""
        if not self.wmctrl:
            return False, "Auto-tile needs wmctrl on an X11 session."
        windows = self.windows()
        if windows is None:
            return False, "Could not read X11 window geometry."
        area = self.work_area()
        if area is None:
            return False, "Could not read the X11 work area."
        pairs = []
        used = set()
        for launch in launches:
            try:
                if not launch.running():
                    continue
            except Exception:
                continue
            window = None
            assigned = getattr(launch, "window_id", None)
            if assigned:
                window = next((item for item in windows
                               if item["id"] == assigned), None)
                if window is not None and window["id"] in used:
                    window = None
                if window is None:
                    # The surface disappeared; allow this launch to claim a
                    # replacement rather than considering it already tiled.
                    try:
                        launch.window_id = None
                    except AttributeError:
                        pass
            if window is None:
                window = self._window_for(launch, windows)
                if window is not None and window["id"] in used:
                    window = None
            if window is None:
                # Some XWayland/Flatpak surfaces do not publish a usable
                # _NET_WM_PID.  Fall back to the class/title Cordial gives its
                # game windows, never to an arbitrary window on the desktop.
                candidates = [item for item in windows
                              if item["id"] not in used
                              and self._looks_like_client(item)]
                if candidates:
                    window = sorted(candidates,
                                    key=lambda item: int(item["id"], 16))[0]
            if window is not None:
                used.add(window["id"])
                try:
                    launch.window_id = window["id"]
                except AttributeError:
                    pass
                pairs.append((launch, window))
        if not pairs:
            return False, "No Cordial windows are ready yet."
        cells = tile_geometries(len(pairs), area, style)
        failed = []
        for (_launch, window), (x, y, width, height) in zip(pairs, cells):
            # Removing the maximized/fullscreen state first is required by
            # several X11 WMs before they accept an absolute move-resize.
            self._run([self.wmctrl, "-i", "-r", window["id"], "-b",
                       "remove,fullscreen,maximized_vert,maximized_horz"])
            left, right, top, bottom = self._frame_extents(window["id"])
            # wmctrl treats width/height as the client size while x/y are the
            # outer frame position.  Subtract the decoration before moving,
            # otherwise vertical cells overlap by the titlebar height.
            client_width = width - left - right
            client_height = height - top - bottom
            if client_width <= 0 or client_height <= 0:
                failed.append(window["id"])
                continue
            done = self._run([
                self.wmctrl, "-i", "-r", window["id"], "-e",
                "0,%d,%d,%d,%d" % (x, y, client_width, client_height)])
            if self._command_failed(done):
                failed.append(window["id"])
        if failed:
            return False, "Could not move %d Cordial window(s)." % len(failed)
        labels = {"columns": "side by side", "rows": "stacked",
                  "grid": "in a grid", "focus-left": "with focus left",
                  "focus-top": "with focus on top"}
        key = str(style or "columns").strip().lower().replace("_", "-")
        key = {"side-by-side": "columns", "stacked": "rows",
               "stack": "rows", "top": "focus-top"}.get(key, key)
        return True, "Tiled %d Cordial window(s) %s." % (
            len(pairs), labels.get(key, "side by side"))


_CLK_TCK = 100.0
try:
    _CLK_TCK = float(os.sysconf("SC_CLK_TCK"))
except (AttributeError, OSError, ValueError):
    pass

# One sample per client tree, keyed by the root pid.  A percentage needs two
# readings; the first call after a launch therefore reports None.
_CPU_SAMPLES: dict[int, tuple[float, float]] = {}
_CPU_LOCK = threading.Lock()


def _cpu_ticks(pid: Any) -> float | None:
    """utime+stime for one process, in clock ticks, from /proc/<pid>/stat."""
    try:
        with open("/proc/%d/stat" % int(pid), encoding="utf-8") as fh:
            rest = fh.read().rsplit(")", 1)[1].split()
        return float(rest[11]) + float(rest[12])
    except (OSError, ValueError, IndexError):
        return None


def process_tree_stats(pid: Any) -> dict:
    """RSS, threads and CPU for the launcher plus its child windows.

    CPU is the whole tree's share of one core over the interval between two
    calls, so it can pass 100% for a multithreaded client.  None means there
    is not a second sample yet, not that the client is idle.
    """
    total_rss = 0.0
    total_threads = 0
    total_ticks = 0.0
    count = 0
    saw_ticks = False
    pids = descendant_pids(pid)
    for child in pids:
        try:
            with open("/proc/%d/status" % child, encoding="utf-8") as fh:
                saw_rss = False
                for line in fh:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            total_rss += int(parts[1]) / 1024.0
                            saw_rss = True
                    elif line.startswith("Threads:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            total_threads += int(parts[1])
                if saw_rss:
                    count += 1
        except (OSError, ValueError):
            continue
        ticks = _cpu_ticks(child)
        if ticks is not None:
            total_ticks += ticks
            saw_ticks = True

    cpu = None
    if saw_ticks:
        now = time.monotonic()
        try:
            key = int(pid)
        except (TypeError, ValueError):
            key = None
        if key is not None:
            with _CPU_LOCK:
                previous = _CPU_SAMPLES.get(key)
                _CPU_SAMPLES[key] = (now, total_ticks)
                if len(_CPU_SAMPLES) > 64:
                    cutoff = now - 60.0
                    for old, (stamp, _ticks) in list(_CPU_SAMPLES.items()):
                        if stamp < cutoff and old != key:
                            _CPU_SAMPLES.pop(old, None)
            if previous is not None:
                elapsed = now - previous[0]
                delta = total_ticks - previous[1]
                if elapsed > 0 and delta >= 0:
                    cpu = delta / _CLK_TCK / elapsed * 100.0

    return {"rss_mb": total_rss if count else None,
            "threads": total_threads or None, "processes": len(pids),
            "cpu_percent": cpu}


@dataclass(frozen=True)
class Profile:
    name: str
    label: str = ""
    account: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label,
                "account": self.account}


def _cordial_home(env: Mapping[str, str] | None = None) -> str:
    env = dict(os.environ if env is None else env)
    return env.get("HOME") or os.path.expanduser("~")


def profile_roots(install=None,
                  env: Mapping[str, str] | None = None) -> list[str]:
    """Cordial's profile roots, the install's own root first.

    Flatpak keeps them inside the sandbox's data dir; a native build uses
    XDG_DATA_HOME.  The other root is kept as a fallback so a roster that was
    imported before an install changed still resolves.
    """
    env = dict(os.environ if env is None else env)
    home = _cordial_home(env)
    data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    kind = getattr(install, "kind", None)
    if kind is None:
        kind = ("flatpak" if os.path.isdir(
            os.path.join(home, ".var", "app", APP_ID)) else "native")
    flatpak_root = os.path.join(home, ".var", "app", APP_ID, "data",
                                "cordial", "profiles")
    native_root = os.path.join(data, "cordial", "profiles")
    if kind == "flatpak":
        return [flatpak_root, native_root]
    return [native_root, flatpak_root]


def profile_dir(profile: Any, install=None,
                env: Mapping[str, str] | None = None,
                create: bool = False) -> str:
    """The directory Cordial uses for one profile."""
    name = validate_profile(profile)
    path = os.path.join(profile_roots(install, env)[0], name)
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise ProfileFlagsError(
                "cannot create the Cordial profile %s: %s" % (path, exc))
    return path


def find_profile_dir(profile: Any, install=None,
                     env: Mapping[str, str] | None = None) -> str | None:
    """Find an existing profile in either Cordial root, without creating it."""
    name = validate_profile(profile)
    for root in profile_roots(install, env):
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate):
            return candidate
    return None


def create_profile(profile: Any, install=None,
                   env: Mapping[str, str] | None = None,
                   owner_uid=None, owner_gid=None) -> str:
    """Create an empty Cordial profile directory for a new account.

    Cordial initializes the runtime data on its first launch.  Refusing an
    existing name in either root prevents WXB from hiding an account behind a
    duplicate path that Cordial would not read.
    """
    name = validate_profile(profile)
    roots = profile_roots(install, env)
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            raise ProfileExists("profile %r already exists" % name)
    path = os.path.join(roots[0], name)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ProfileFlagsError(
            "cannot create the Cordial profile %s: %s" % (path, exc)) from exc
    _claim_profile(path, owner_uid, owner_gid)
    return path


def delete_profile(profile: Any, install=None,
                   env: Mapping[str, str] | None = None) -> str:
    """Remove one Cordial profile directory and its local account data.

    The name is validated and the resolved directory must be a direct child
    of one of Cordial's profile roots.  Symlinks are refused rather than
    followed, so deleting a profile can never walk out of Cordial's data.
    """
    name = validate_profile(profile)
    paths = []
    for root in profile_roots(install, env):
        candidate = os.path.join(root, name)
        if not os.path.lexists(candidate):
            continue
        if os.path.islink(candidate):
            raise ProfileFlagsError(
                "refusing to delete symlinked Cordial profile %s" % candidate)
        resolved_root = os.path.realpath(root)
        resolved = os.path.realpath(candidate)
        if os.path.dirname(resolved) != resolved_root:
            raise ProfileFlagsError(
                "refusing to delete profile outside Cordial's data: %s"
                % candidate)
        paths.append(candidate)
    if not paths:
        raise ProfileNotFound("Cordial profile %r does not exist" % name)
    for path in paths:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise ProfileFlagsError(
                "cannot delete the Cordial profile %s: %s" % (path, exc)) from exc
    return paths[0]


def performance_flags(cap: Any = None, low_graphics: bool = True) -> dict:
    """FastFlags that cap the engine and cut an alt client's graphics load.

    ``DFIntTaskSchedulerTargetFps`` is the engine's own frame target;
    ``CordialFrameRateLimit`` is the Frame rate limit row.  Cordial reapplies
    both after Roblox refreshes its settings, so the cap is not just a
    request at startup.  ``cap=0`` asks Cordial for the display refresh and
    leaves the engine target alone.
    """
    flags = {}
    if cap not in (None, ""):
        cap = int(cap)
        if cap > 0:
            flags["DFIntTaskSchedulerTargetFps"] = cap
            flags["CordialFrameRateLimit"] = cap
        else:
            flags["CordialFrameRateLimit"] = "display"
    if low_graphics:
        # The cheapest set Roblox actually honours.  Texture quality runs
        # 0 (lowest) to 3 (highest); the old preset set 3, which was the
        # opposite of a memory saving.  MSAA 0 is off, not one sample.
        flags.update({
            "DFIntDebugFRMQualityLevelOverride": 1,
            "FIntDebugForceMSAASamples": 0,
            "FIntFRMMaxGrassDistance": 0,
            "FIntFRMMinGrassDistance": 0,
            "DFFlagTextureQualityOverrideEnabled": True,
            "DFIntTextureQualityOverride": 0,
            "FFlagDisablePostFx": True,
            "FIntTerrainArraySliceSize": 4,
            "DFIntCSGLevelOfDetailSwitchingDistance": 0,
            "DFIntCSGLevelOfDetailSwitchingDistanceL12": 0,
            "DFIntCSGLevelOfDetailSwitchingDistanceL23": 0,
            "DFIntCSGLevelOfDetailSwitchingDistanceL34": 0,
            "FIntRenderLocalLightUpdatesMax": 1,
            "FIntRenderLocalLightUpdatesMin": 1,
        })
    return flags


_LOW_GRAPHICS_FLAG_NAMES = tuple(
    name for name in performance_flags(0, low_graphics=True)
    if name not in performance_flags(0, low_graphics=False))


def _profile_owner(owner_uid=None, owner_gid=None):
    """Resolve the desktop owner when WXB is running elevated."""
    if owner_uid is None and os.geteuid() == 0:
        raw = (os.environ.get("WXB_OWNER_UID")
               or os.environ.get("PKEXEC_UID")
               or os.environ.get("SUDO_UID"))
        try:
            owner_uid = int(raw) if raw else None
        except (TypeError, ValueError):
            owner_uid = None
        if owner_uid is not None and owner_gid is None:
            try:
                owner_gid = pwd.getpwuid(owner_uid).pw_gid
            except KeyError:
                owner_gid = None
    return owner_uid, owner_gid


def _claim_profile(path: str, owner_uid=None, owner_gid=None) -> None:
    """Give a profile and its flags back to the non-root desktop user."""
    uid, gid = _profile_owner(owner_uid, owner_gid)
    if uid is None:
        return
    for target in (path, os.path.dirname(path)):
        try:
            st = os.stat(target)
            if st.st_uid == uid and (gid is None or st.st_gid == gid):
                continue
            os.chown(target, uid, -1 if gid is None else gid)
        except OSError as exc:
            raise ProfileFlagsError(
                "cannot give %s back to the desktop user: %s"
                % (target, exc)) from exc


def write_profile_flags(profile: Any, flags: Mapping[str, Any],
                        install=None,
                        env: Mapping[str, str] | None = None,
                        remove: tuple[str, ...] = (),
                        owner_uid=None, owner_gid=None) -> str:
    """Merge flags into ``profiles/<profile>/flags.json``, atomically.

    A flag the user set for another purpose is left alone; only the names WXB
    manages are replaced or removed.  An unreadable file is reported, never
    overwritten.
    """
    directory = profile_dir(profile, install, env, create=True)
    path = os.path.join(directory, "flags.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ProfileFlagsError("cannot read %s: %s" % (path, exc))
        if not isinstance(data, dict):
            raise ProfileFlagsError("%s is not a flag object" % path)
    for key in remove:
        data.pop(key, None)
    data.update({str(key): value for key, value in flags.items()})
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise ProfileFlagsError("cannot write %s: %s" % (path, exc))
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    _claim_profile(directory, owner_uid, owner_gid)
    _claim_profile(path, owner_uid, owner_gid)
    return path


def prepare_profile(profile: Any, install=None,
                    env: Mapping[str, str] | None = None,
                    cap: Any = 60,
                    low_graphics: bool = True,
                    owner_uid=None, owner_gid=None) -> str:
    """Create/refresh one Cordial profile for a capped alt launch."""
    flags = performance_flags(cap, low_graphics)
    remove = ["DFIntTaskSchedulerTargetFps"] if not cap else []
    if not low_graphics:
        # Turning the switch off must undo the preset, not merely stop
        # refreshing it.  Otherwise the old low-texture flags survive every
        # later launch and the control only works in one direction.
        remove.extend(_LOW_GRAPHICS_FLAG_NAMES)
    return write_profile_flags(profile, flags, install, env, remove=remove,
                               owner_uid=owner_uid, owner_gid=owner_gid)


class ProfileStore:
    """Small JSON store for profile names and labels.

    The file holds no cookies and no tokens: Cordial keeps those in the
    desktop keyring.  A root WXB passes the desktop owner uid so the file is
    chowned back instead of becoming root-only.
    """

    VERSION = 1

    def __init__(self, path: str | None = None, owner_uid: int | None = None):
        self.path = path or default_store_path()
        self.owner_uid = owner_uid
        self._profiles: list[Profile] | None = None

    def load(self) -> list[Profile]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            self._profiles = []
            return []
        except (OSError, ValueError) as exc:
            raise StoreError("cannot read %s: %s" % (self.path, exc)) from exc
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
            raise StoreError("%s is not a WXB Roblox profile store"
                             % self.path)
        items = raw.get("profiles")
        if not isinstance(items, list):
            raise StoreError("%s has no profiles list" % self.path)
        profiles = []
        for item in items:
            if not isinstance(item, dict):
                raise StoreError("bad profile entry in %s" % self.path)
            try:
                name = validate_profile(item.get("name"))
            except InvalidProfile as exc:
                raise StoreError("bad profile in %s: %s"
                                 % (self.path, exc)) from exc
            label = item.get("label", "")
            account = item.get("account", "")
            profiles.append(Profile(name=name,
                                    label=label if isinstance(label, str) else "",
                                    account=account if isinstance(account, str)
                                    else ""))
        self._profiles = profiles
        return list(profiles)

    def _ensure(self) -> list[Profile]:
        if self._profiles is None:
            self.load()
        return self._profiles or []

    def save(self, profiles: list[Profile] | None = None) -> None:
        if profiles is not None:
            self._profiles = list(profiles)
        data = {"version": self.VERSION,
                "profiles": [p.as_dict() for p in self._ensure()]}
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            raise StoreError("cannot create %s: %s" % (folder, exc)) from exc
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            if self.owner_uid is not None:
                try:
                    os.chown(folder, int(self.owner_uid), -1)
                    os.chown(self.path, int(self.owner_uid), -1)
                except OSError:
                    pass
        except OSError as exc:
            raise StoreError("cannot write %s: %s" % (self.path, exc)) from exc
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    def profiles(self) -> list[Profile]:
        return list(self._ensure())

    def names(self) -> list[str]:
        return [p.name for p in self._ensure()]

    def get(self, name: str) -> Profile | None:
        for profile in self._ensure():
            if profile.name == name:
                return profile
        return None

    def add(self, name: str, label: str = "", account: str = "") -> Profile:
        name = validate_profile(name)
        if any(p.name == name for p in self._ensure()):
            raise ProfileExists("profile %r already exists" % name)
        profile = Profile(name=name, label=label.strip(),
                          account=account.strip())
        self._profiles = self._ensure() + [profile]
        self.save()
        return profile

    def remove(self, name: str) -> bool:
        kept = [p for p in self._ensure() if p.name != name]
        if len(kept) == len(self._ensure()):
            return False
        self._profiles = kept
        self.save()
        return True

    def sync(self, names) -> list[str]:
        """Add profile names discovered on disk, keeping existing labels.

        Invalid Cordial names are skipped rather than making the whole
        import fail; WXB's own profile-name rule is the filter.  Nothing is
        written when there is nothing new.
        """
        existing = self._ensure()
        known = {profile.name for profile in existing}
        added: list[str] = []
        for raw in names or ():
            try:
                name = validate_profile(raw)
            except InvalidProfile:
                continue
            if name in known:
                continue
            known.add(name)
            existing.append(Profile(name=name))
            added.append(name)
        if added:
            self._profiles = existing
            self.save()
        return added


@dataclass
class Launch:
    profile: str
    argv: list[str]
    popen: Any
    started_at: float
    place_id: str | None = None
    job_id: str | None = None
    cpu_quota: int = 0
    fullscreen: bool = False
    # Set by WindowTiler when this launch is paired with its X11 surface.
    # It keeps a repeated tile pass from assigning one window to two profiles.
    window_id: str | None = None

    @property
    def pid(self) -> int:
        return int(self.popen.pid)

    def poll(self):
        return self.popen.poll()

    def running(self) -> bool:
        return self.poll() is None

    def status(self) -> dict:
        code = self.poll()
        return {
            "profile": self.profile,
            "pid": self.pid,
            "running": code is None,
            "exit_code": code,
            "exit": describe_exit(code),
            "argv": list(self.argv),
            "started_at": self.started_at,
            "place_id": self.place_id,
            "job_id": self.job_id,
            "cpu_quota": self.cpu_quota,
            "fullscreen": self.fullscreen,
            "window_id": self.window_id,
        }


class LaunchManager:
    """Own the child processes WXB started; nothing else is ever signalled."""

    def __init__(self, install, *, popen=subprocess.Popen,
                 sleep=time.sleep, clock=time.monotonic,
                 env: Mapping[str, str] | None = None,
                 drop_uid: int | None = None, drop_gid: int | None = None,
                 nice: bool = False, ionice: bool = False,
                 log_dir: str | None = None, cpu_quota: int = 0):
        self.install = install
        self.popen = popen
        self.sleep = sleep
        self.clock = clock
        self.env = dict(env) if env is not None else None
        self.drop_uid = drop_uid
        self.drop_gid = drop_gid
        self.nice = bool(nice)
        self.ionice = bool(ionice)
        self.log_dir = log_dir
        self._flatpak = getattr(install, "kind", "") == "flatpak"
        self.cpu_quota = 0
        self._quota_checked = False
        self._quota_error = ""
        self.set_cpu_quota(cpu_quota)
        self._launches: dict[str, Launch] = {}
        self._tiler = None
        self._lock = threading.RLock()

    @property
    def backend(self) -> str:
        return getattr(self.install, "backend", "cordial")

    @property
    def label(self) -> str:
        return getattr(self.install, "label", "Cordial")

    @property
    def supports_many(self) -> bool:
        return bool(getattr(self.install, "supports_many", True))

    @staticmethod
    def _clean_quota(value: Any) -> int:
        """A CPU quota as percent of one core: 0 is off, 1-1600 is a limit."""
        try:
            quota = int(value or 0)
        except (TypeError, ValueError):
            return 0
        if quota <= 0:
            return 0
        return max(1, min(1600, quota))

    def set_cpu_quota(self, value: Any) -> None:
        quota = self._clean_quota(value)
        if quota != self.cpu_quota:
            self._quota_checked = False
            self._quota_error = ""
        self.cpu_quota = quota

    def _quota_problem(self) -> str:
        """Why a systemd user scope cannot be made, or '' when it can.

        The preflight is cached: the first quota launch pays for it, later
        launches and every profile in the same roster reuse the answer.
        """
        if not self.cpu_quota:
            return ""
        if self._quota_checked:
            return self._quota_error
        self._quota_checked = True
        if os.geteuid() == 0:
            self._quota_error = (
                "CPU quota needs WXB to run as your desktop user, not root; "
                "launch without sudo or set the quota to 0.")
            return self._quota_error
        if self._flatpak:
            # Flatpak creates its own app-flatpak-...scope and moves the
            # client into it, so a wrapper scope would not contain the game.
            # The app scope is the one to set CPUQuota on after it appears.
            tool = shutil.which("systemctl")
            if not tool:
                self._quota_error = (
                    "CPU quota needs systemctl, which is not installed; set "
                    "the quota to 0 to launch without it.")
                return self._quota_error
            cmd = [tool, "--user", "show-environment"]
            label = "systemctl --user"
        else:
            tool = shutil.which("systemd-run")
            if not tool:
                self._quota_error = (
                    "CPU quota needs systemd-run, which is not installed; "
                    "set the quota to 0 to launch without it.")
                return self._quota_error
            unit = "wxb-quota-check-%d-%d" % (os.getpid(), time.time_ns())
            cmd = [tool, "--user", "--scope", "--quiet", "--collect",
                   "--unit=" + unit, "-p", "CPUQuota=100%", "--", "true"]
            label = "systemd-run --user"
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=6)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._quota_error = (
                "CPU quota needs a working systemd user session: %s" % exc)
            return self._quota_error
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            detail = detail[-1] if detail else "%s failed" % label
            self._quota_error = (
                "CPU quota needs a working systemd user session: %s" % detail)
        return self._quota_error

    def _flatpak_scope(self, launch: Launch, timeout: float = 12.0) -> tuple[str, str]:
        """Wait for Flatpak's own app scope and return (unit, cgroup path)."""
        deadline = time.monotonic() + timeout
        while True:
            for pid in descendant_pids(launch.pid):
                try:
                    with open("/proc/%d/cgroup" % pid, encoding="utf-8") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    continue
                for line in lines:
                    if not line.startswith("0::"):
                        continue
                    path = line[3:].strip()
                    unit = os.path.basename(path)
                    if unit.startswith("app-flatpak-"):
                        return unit, path
            if not launch.running() or time.monotonic() >= deadline:
                return "", ""
            time.sleep(0.1)

    def _apply_scope_quota(self, launch: Launch) -> None:
        unit, path = self._flatpak_scope(launch)
        if not unit:
            raise CpuQuotaUnavailable(
                "CPU quota could not find the Flatpak app scope; the client "
                "was stopped rather than left unthrottled.")
        tool = shutil.which("systemctl")
        try:
            done = subprocess.run(
                [tool, "--user", "set-property", "--runtime", unit,
                 "CPUQuota=%d%%" % self.cpu_quota],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CpuQuotaUnavailable(
                "CPU quota could not be applied to %s: %s" % (unit, exc))
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            detail = detail[-1] if detail else "systemctl failed"
            raise CpuQuotaUnavailable(
                "CPU quota could not be applied to %s: %s" % (unit, detail))
        try:
            with open("/sys/fs/cgroup" + path + "/cpu.max",
                      encoding="utf-8") as fh:
                quota, _period = fh.read().split()
        except (OSError, ValueError) as exc:
            raise CpuQuotaUnavailable(
                "CPU quota was set on %s but could not be verified: %s"
                % (unit, exc))
        if quota == "max" or int(quota) != self.cpu_quota * 1000:
            raise CpuQuotaUnavailable(
                "CPU quota was set on %s but the kernel still reports %r."
                % (unit, quota))

    def _kill_launch(self, launch: Launch) -> None:
        """A launch that could not be throttled must not be left running."""
        pids = descendant_pids(launch.pid)
        try:
            os.killpg(os.getpgid(launch.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            launch.popen.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError):
            pass

    # -- command ---------------------------------------------------------
    def _privilege_prefix(self) -> list[str]:
        if not self.drop_uid or os.geteuid() != 0:
            return []
        setpriv = shutil.which("setpriv")
        if not setpriv:
            raise LaunchFailed(
                "WXB is root but setpriv is missing, so it cannot drop back "
                "to the desktop user before starting Cordial")
        gid = self.drop_gid
        if gid is None:
            try:
                gid = pwd.getpwuid(int(self.drop_uid)).pw_gid
            except KeyError:
                gid = int(self.drop_uid)
        return [setpriv, "--reuid=%d" % int(self.drop_uid),
                "--regid=%d" % int(gid), "--init-groups", "--"]

    def _wrap(self, profile: str, argv: list[str]) -> list[str]:
        out = self._privilege_prefix()
        if self.cpu_quota and not self._flatpak:
            problem = self._quota_problem()
            if problem:
                raise CpuQuotaUnavailable(problem)
            runner = shutil.which("systemd-run")
            safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(profile))[:40]
            unit = "wxb-alt-%s-%d" % (safe or "client", time.time_ns())
            # A transient user scope owns the whole client tree and carries
            # a real cgroup v2 cpu.max, not a nice value or an overlay.
            out += [runner, "--user", "--scope", "--quiet", "--collect",
                    "--unit=" + unit,
                    "-p", "CPUQuota=%d%%" % self.cpu_quota, "--"]
        if self.nice:
            out += ["nice", "-n", "10"]
        if self.ionice:
            ionice = shutil.which("ionice")
            if ionice:
                out += [ionice, "-c2", "-n7"]
        return out + list(argv)

    def _spawn(self, profile: str, argv: list[str], *,
               place_id: str | None = None,
               job_id: str | None = None,
               fullscreen: bool = False) -> Launch:
        full = self._wrap(profile, argv)
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
        }
        if self.env is not None:
            kwargs["env"] = self.env
        log = None
        if self.log_dir:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                log = open(os.path.join(self.log_dir, profile + ".log"),
                           "ab", buffering=0)
            except OSError as exc:
                raise LaunchFailed("cannot open the Cordial log: %s"
                                   % exc) from exc
            kwargs["stdout"] = log
            kwargs["stderr"] = subprocess.STDOUT
        try:
            proc = self.popen(full, **kwargs)
        except OSError as exc:
            raise LaunchFailed("could not start Cordial for %r: %s"
                               % (profile, exc)) from exc
        finally:
            if log is not None:
                log.close()
        launch = Launch(profile=profile, argv=full, popen=proc,
                        started_at=self.clock(), place_id=place_id,
                        job_id=job_id, cpu_quota=self.cpu_quota,
                        fullscreen=fullscreen)
        if self.cpu_quota and self._flatpak:
            try:
                self._apply_scope_quota(launch)
            except Exception:
                self._kill_launch(launch)
                raise
        return launch

    # -- launch ----------------------------------------------------------
    def launch(self, profile: Any, place_id: Any = None, *,
               fullscreen: bool = False,
               settings: Mapping[str, Any] | None = None) -> Launch:
        name = validate_profile(profile)
        with self._lock:
            if not self.supports_many and any(
                    launch.running() for launch in self._launches.values()):
                raise SingleSessionOnly(
                    "%s supports one live session at a time. Stop it before "
                    "starting another, or use Cordial for an alt roster."
                    % self.label)
            old = self._launches.get(name)
            if old is not None and old.running():
                raise ProfileBusy("profile %r is already running (pid %d)"
                                  % (name, old.pid))
            argv = self.install.launch_argv(name, place_id,
                                            fullscreen=fullscreen,
                                            settings=settings)
            launch = self._spawn(
                name, argv,
                place_id=None if place_id is None
                else validate_place_id(place_id),
                fullscreen=fullscreen)
            self._launches[name] = launch
            return launch

    def launch_same_server(self, profile: Any, place_id: Any,
                           job_id: Any) -> Launch:
        name = validate_profile(profile)
        with self._lock:
            old = self._launches.get(name)
            if old is not None and old.running():
                raise ProfileBusy("profile %r is already running (pid %d)"
                                  % (name, old.pid))
            argv = self.install.same_server_argv(name, place_id, job_id)
            launch = self._spawn(name, argv,
                                 place_id=validate_place_id(place_id),
                                 job_id=validate_job_id(job_id))
            self._launches[name] = launch
            return launch

    def restart(self, profile: Any, *,
                settings: Mapping[str, Any] | None = None) -> Launch:
        """Restart one tracked client with its last place, server and mode.

        A stopped client is relaunched; a running one is stopped first.  The
        manager's current quota and priority settings apply to the new
        process, so a restart is also how a changed quota reaches a client
        that was started without it.
        """
        name = validate_profile(profile)
        with self._lock:
            old = self._launches.get(name)
            if old is None:
                raise LaunchFailed("no session for %r was started by this "
                                   "WXB run" % name)
            place = old.place_id
            job = old.job_id
            fullscreen = old.fullscreen
        if old.running() and not self.stop(name):
            raise LaunchFailed("could not stop %r for a restart" % name)
        if job:
            return self.launch_same_server(name, place, job)
        return self.launch(name, place, fullscreen=fullscreen,
                           settings=settings)

    def restart_all(self, *,
                    settings: Mapping[str, Any] | None = None) -> list[Launch]:
        """Restart every currently running client, leaving stopped ones alone."""
        with self._lock:
            names = sorted(name for name, launch in self._launches.items()
                           if launch.running())
        return [self.restart(name, settings=settings) for name in names]

    def clear_finished(self) -> int:
        """Forget sessions that have stopped; return how many were removed."""
        with self._lock:
            names = [name for name, launch in self._launches.items()
                     if not launch.running()]
            for name in names:
                self._launches.pop(name, None)
        return len(names)

    def launch_many(self, profiles, place_id: Any = None, *,
                    fullscreen: bool = False, stagger: float = DEFAULT_STAGGER,
                    same_server: bool = False, job_id: Any = None,
                    rollback: bool = True) -> list[Launch]:
        names = [validate_profile(name) for name in profiles]
        if not names:
            raise RobloxRuntimeError("no profiles to launch")
        if len(set(names)) != len(names):
            raise InvalidProfile("duplicate profile in the launch list")
        if not self.supports_many and len(names) > 1:
            raise SingleSessionOnly(
                "%s supports one live session at a time. Use Cordial for "
                "a multi-account roster." % self.label)
        with self._lock:
            for name in names:
                old = self._launches.get(name)
                if old is not None and old.running():
                    raise ProfileBusy("profile %r is already running (pid %d)"
                                      % (name, old.pid))
        if same_server and job_id is None:
            raise InvalidJob("same-server launch needs a job id")
        started: list[Launch] = []
        try:
            for index, name in enumerate(names):
                if same_server:
                    started.append(self.launch_same_server(
                        name, place_id, job_id))
                else:
                    started.append(self.launch(
                        name, place_id, fullscreen=fullscreen))
                if index + 1 < len(names) and stagger > 0:
                    self.sleep(stagger)     # let the build store settle
        except Exception:
            if rollback:
                self.stop_all()
            raise
        return started

    # -- status / stop ---------------------------------------------------
    def statuses(self) -> list[dict]:
        with self._lock:
            out = []
            for launch in self._launches.values():
                status = launch.status()
                status["backend"] = self.backend
                status["label"] = self.label
                out.append(status)
            return sorted(out, key=lambda s: s["profile"])

    def live_launches(self) -> list[Launch]:
        """Launch records whose process is still alive."""
        with self._lock:
            return [launch for launch in self._launches.values()
                    if launch.running()]

    def all_windows_mapped(self) -> bool:
        """True once every live launch has been assigned its own X11 window."""
        live = self.live_launches()
        return bool(live) and all(
            bool(getattr(launch, "window_id", None)) for launch in live)

    def running(self) -> list[dict]:
        return [s for s in self.statuses() if s["running"]]

    def tile(self, tiler=None, style: str = "columns"):
        """Tile each live Cordial launch into one exact work-area layout."""
        with self._lock:
            launches = [launch for launch in self._launches.values()
                        if launch.running()]
        worker = tiler or self._tiler or WindowTiler(env=self.env)
        return worker.tile(launches, style=style)

    def stop(self, profile: str, *, timeout: float = 5.0) -> bool:
        with self._lock:
            launch = self._launches.pop(profile, None)
        if launch is None:
            return False
        if launch.running():
            children = descendant_pids(launch.pid)
            self._signal(launch, signal.SIGTERM)
            for pid in children:
                if pid != launch.pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            deadline = time.monotonic() + timeout
            # Flatpak's bwrap can leave the launcher's process group, so a
            # group signal alone may leave the engine running.  Wait for the
            # snapshot too, then signal whatever is left.
            while time.monotonic() < deadline:
                if not launch.running() and not any(
                        _pid_alive(pid) for pid in children
                        if pid != launch.pid):
                    break
                time.sleep(0.02)
            children = list(dict.fromkeys(
                children + descendant_pids(launch.pid)))
            if launch.running() or any(
                    _pid_alive(pid) for pid in children
                    if pid != launch.pid):
                self._signal(launch, signal.SIGKILL)
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                try:
                    launch.popen.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return True

    def stop_all(self) -> int:
        with self._lock:
            names = list(self._launches)
        return sum(1 for name in names if self.stop(name))

    @staticmethod
    def _signal(launch: Launch, sig: int) -> None:
        try:
            os.killpg(os.getpgid(launch.pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            launch.popen.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass

    def close(self) -> int:
        return self.stop_all()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop_all()
