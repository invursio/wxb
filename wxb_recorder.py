#!/usr/bin/env python3
"""Local screen recording for WXB Studio.

The GTK window never owns the capture process.  This module builds a bounded
FFmpeg X11Grab job, keeps the child off the UI thread, monitors disk/session
conditions, and finalizes a recoverable Matroska file on stop.  Native Wayland
capture is reported separately: the portal can broker permission, but this
build does not claim a capture backend when no tested PipeWire path exists.

Nothing here uploads, elevates privileges, shells out, or overwrites an existing
file.  Users are expected to install FFmpeg (and optionally PulseAudio tools)
through their distribution.
"""

from __future__ import annotations

import copy
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

RECORDING_SUFFIX = ".mkv"
SOURCE_CHOICES = ("screen", "window", "region")
RESOLUTION_CHOICES = ("source", "720p", "1080p", "1440p")
FPS_CHOICES = (15, 30, 60)
QUALITY_CHOICES = ("low", "balanced", "high")
ENCODER_CHOICES = ("auto", "cpu", "vaapi", "nvenc")
QUALITY = {
    "low": {"crf": 28, "preset": "ultrafast"},
    "balanced": {"crf": 23, "preset": "veryfast"},
    "high": {"crf": 18, "preset": "medium"},
}
MIN_FREE_BYTES = 64 * 1024 * 1024
STOP_GRACE = 8.0
_SESSION_GAP = 10.0
_CAPS: dict[str, dict] = {}
_DEVICE_CACHE: dict[str, list[str]] = {}


class RecorderError(RuntimeError):
    """A recorder operation failed in a user-visible way."""


def default_save_dir() -> Path:
    """The default local-only destination; no upload path exists."""
    videos = Path.home() / "Videos"
    return videos / "WXB" if videos.is_dir() else Path.home() / "Videos" / "WXB"


def _choice(value: Any, choices: tuple[str, ...], default: str) -> str:
    needle = str(value or "").strip().lower()
    return needle if needle in choices else default


def normalize_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Clamp a recorder settings block before it reaches a process argv."""
    source = dict(raw) if isinstance(raw, Mapping) else {}
    save_dir = str(source.get("save_dir") or "").strip()[:4096]
    if not save_dir:
        save_dir = str(default_save_dir())
    raw_window = str(source.get("window_id") or "").strip()
    if raw_window.lower().startswith("0x"):
        try:
            window_id = str(int(raw_window, 16))
        except ValueError:
            window_id = ""
    else:
        window_id = "".join(ch for ch in raw_window if ch.isdigit())[:20]
    try:
        fps = int(source.get("fps", 30))
    except (TypeError, ValueError, OverflowError):
        fps = 30
    if fps not in FPS_CHOICES:
        fps = min(FPS_CHOICES, key=lambda item: abs(item - fps))
    try:
        region_x = max(0, int(source.get("region_x", 0)))
        region_y = max(0, int(source.get("region_y", 0)))
        region_w = max(0, int(source.get("region_w", 0)))
        region_h = max(0, int(source.get("region_h", 0)))
    except (TypeError, ValueError, OverflowError):
        region_x = region_y = 0
        region_w = region_h = 0
    return {
        "source": _choice(source.get("source"), SOURCE_CHOICES, "screen"),
        "region_x": region_x,
        "region_y": region_y,
        "region_w": region_w,
        "region_h": region_h,
        "window_id": window_id,
        "resolution": _choice(source.get("resolution"),
                              RESOLUTION_CHOICES, "source"),
        "fps": fps,
        "quality": _choice(source.get("quality"), QUALITY_CHOICES, "balanced"),
        "encoder": _choice(source.get("encoder"), ENCODER_CHOICES, "auto"),
        "cursor": bool(source.get("cursor", True)),
        "save_dir": save_dir,
        "mic": bool(source.get("mic", False)),
        "mic_device": str(source.get("mic_device") or "").strip()[:200],
        "desktop_audio": bool(source.get("desktop_audio", False)),
        "desktop_device": str(source.get("desktop_device") or "").strip()[:200],
    }


def invalidate_capabilities() -> None:
    _CAPS.clear()
    _DEVICE_CACHE.clear()


def _run(argv: list[str], timeout: float = 3.0,
         env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env=None if env is None else dict(env), check=False)
        return result.returncode, result.stdout or "", result.stderr or ""
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return 127, "", str(exc)


def _ffmpeg_has(ffmpeg: str, flag: str, env: Mapping[str, str]) -> bool:
    rc, out, err = _run([ffmpeg, "-hide_banner", "-devices"], 4, env)
    return rc == 0 and flag in (out + err)


def _ffmpeg_encoders(ffmpeg: str, env: Mapping[str, str]) -> set[str]:
    rc, out, err = _run([ffmpeg, "-hide_banner", "-encoders"], 4, env)
    if rc != 0:
        return set()
    found = set()
    for line in (out + "\n" + err).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(("V", "A")):
            found.add(parts[1])
    return found


def _gst_element(name: str, env: Mapping[str, str]) -> bool:
    gst = shutil.which("gst-inspect-1.0")
    if not gst:
        return False
    rc, _out, _err = _run([gst, name], 4, env)
    return rc == 0


def _portal_has_screencast(env: Mapping[str, str]) -> bool:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return False
    rc, out, err = _run([
        gdbus, "introspect", "--session",
        "--dest", "org.freedesktop.portal.Desktop",
        "--object-path", "/org/freedesktop/portal/desktop"], 4, env)
    return rc == 0 and "org.freedesktop.portal.ScreenCast" in (out + err)


def _pulse_available(env: Mapping[str, str]) -> bool:
    pactl = shutil.which("pactl")
    if not pactl:
        return False
    rc, _out, _err = _run([pactl, "info"], 3, env)
    return rc == 0


def _x11_screen_size(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """Best-effort X11 root size; never invents a full-screen crop."""
    try:
        from Xlib import display
        handle = display.Display()
        try:
            screen = handle.screen()
            return int(screen.width_in_pixels), int(screen.height_in_pixels)
        finally:
            handle.close()
    except Exception:
        pass
    xrandr = shutil.which("xrandr")
    if xrandr:
        rc, out, _err = _run([xrandr, "--current"], 3, env)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if "current" in parts:
                    try:
                        index = parts.index("current")
                        return int(parts[index + 1]), int(
                            parts[index + 3].rstrip(","))
                    except (ValueError, IndexError):
                        pass
    return 0, 0


def detect_capabilities(env: Mapping[str, str] | None = None,
                        refresh: bool = False) -> dict[str, Any]:
    """Describe only capture paths that this machine can actually attempt."""
    environment = dict(os.environ if env is None else env)
    key = "|".join((
        environment.get("DISPLAY", ""),
        environment.get("WAYLAND_DISPLAY", ""),
        environment.get("XDG_SESSION_TYPE", ""),
        shutil.which("ffmpeg") or "",
        shutil.which("gst-launch-1.0") or "",
        environment.get("XDG_CURRENT_DESKTOP", ""),
    ))
    if not refresh and key in _CAPS:
        return copy.deepcopy(_CAPS[key])
    display = bool(str(environment.get("DISPLAY") or "").strip())
    wayland = (str(environment.get("XDG_SESSION_TYPE") or "").lower()
               == "wayland"
               or bool(str(environment.get("WAYLAND_DISPLAY") or "").strip()))
    ffmpeg = shutil.which("ffmpeg") or ""
    ffprobe = shutil.which("ffprobe") or ""
    x11grab = bool(ffmpeg and _ffmpeg_has(ffmpeg, "x11grab", environment))
    pulse = bool(ffmpeg and _ffmpeg_has(ffmpeg, "pulse", environment)
                 and _pulse_available(environment))
    encoders = _ffmpeg_encoders(ffmpeg, environment) if ffmpeg else set()
    hardware = []
    if "h264_vaapi" in encoders and Path("/dev/dri/renderD128").exists():
        hardware.append("vaapi")
    if "h264_nvenc" in encoders and (
            Path("/dev/nvidia0").exists()
            or Path("/proc/driver/nvidia/version").exists()):
        hardware.append("nvenc")
    portal = _portal_has_screencast(environment) if wayland else False
    pipewire = bool(shutil.which("gst-launch-1.0")
                    and _gst_element("pipewiresrc", environment))
    # The portal can broker consent, but this build has no tested PipeWire
    # capture process.  Never advertise a Wayland button that would fail at
    # start: report the missing backend instead and keep the X11 path fully
    # exercised.  ``portal`` and ``pipewire`` remain visible for diagnosis.
    wayland_capture = False
    x11_capture = bool(display and not wayland and x11grab)
    record = x11_capture or wayland_capture
    if x11_capture:
        session = "x11"
        backend = "ffmpeg/x11grab"
        reason = ""
    elif wayland_capture:
        session = "wayland"
        backend = "gstreamer/pipewire-portal"
        reason = ("Wayland capture uses the desktop portal; permission is "
                  "requested by the compositor and can be denied.")
    elif wayland:
        session = "wayland"
        backend = ""
        if portal and pipewire:
            reason = ("The ScreenCast portal and PipeWire are present, but "
                      "this build has no tested PipeWire capture process. "
                      "Use an X11 session for the complete recorder.")
        else:
            reason = ("Native Wayland screen capture needs xdg-desktop-portal "
                      "ScreenCast and PipeWire/GStreamer; no usable backend "
                      "was detected. Use an X11 session for the complete "
                      "recorder.")
    elif display:
        session = "x11"
        backend = ""
        reason = ("X11 is present, but FFmpeg lacks the x11grab input or is "
                  "not installed. Install FFmpeg to record.")
    else:
        session = str(environment.get("XDG_SESSION_TYPE") or "unknown")
        backend = ""
        reason = ("No X11 DISPLAY was found. Native Wayland capture needs a "
                  "portal-capable PipeWire backend; install the portal, "
                  "PipeWire and GStreamer, or use an X11 session.")
    caps = {
        "session": session,
        "x11": bool(display and not wayland),
        "xwayland": bool(display and wayland),
        "wayland": wayland,
        "display": display,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "x11grab": x11grab,
        "pulse": pulse,
        "portal": portal,
        "pipewire": pipewire,
        "backend": backend,
        "record": bool(record),
        "play": bool(record),
        "hotkeys": bool(record),
        "sources": (["screen", "window", "region"] if x11_capture else []),
        "audio": bool(record and pulse),
        "encoders": ["libx264"] + hardware,
        "hardware_encoders": hardware,
        "reason": reason,
    }
    _CAPS[key] = caps
    return copy.deepcopy(caps)


def list_audio_devices(env: Mapping[str, str] | None = None,
                       refresh: bool = False) -> dict[str, list[str]]:
    """Read Pulse/PipeWire device names without touching global settings."""
    environment = dict(os.environ if env is None else env)
    key = environment.get("PULSE_SERVER", "default")
    if not refresh and key in _DEVICE_CACHE:
        return copy.deepcopy(_DEVICE_CACHE[key])
    result = {"microphones": [], "desktop": []}
    pactl = shutil.which("pactl")
    if pactl:
        rc, out, _err = _run([pactl, "list", "short", "sources"], 3, environment)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1].strip():
                    name = parts[1].strip()
                    (result["microphones"] if ".monitor" not in name
                     else result["desktop"]).append(name)
        rc, out, _err = _run([pactl, "get-default-sink"], 3, environment)
        if rc == 0 and out.strip():
            monitor = out.strip() + ".monitor"
            if monitor not in result["desktop"]:
                result["desktop"].insert(0, monitor)
    _DEVICE_CACHE[key] = copy.deepcopy(result)
    return result


def active_window_id(env: Mapping[str, str] | None = None) -> str:
    """Read the active X11 window ID via a fixed argv, never a shell."""
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return ""
    rc, out, _err = _run([xdotool, "getactivewindow"], 3, env)
    return out.strip() if rc == 0 and out.strip().isdigit() else ""


def window_geometry(window_id: str,
                    env: Mapping[str, str] | None = None) -> tuple[int, int, int, int]:
    """Read an X11 window rectangle for the region fallback.

    FFmpeg's x11grab ``window_id`` option is not reliable on minimal/Xvfb
    servers, so the fully exercised backend captures the window's current
    rectangle.  It does not follow the window after capture starts.
    """
    xwininfo = shutil.which("xwininfo")
    if not xwininfo or not str(window_id or "").isdigit():
        return 0, 0, 0, 0
    rc, out, _err = _run([xwininfo, "-id", str(window_id)], 3, env)
    if rc != 0:
        return 0, 0, 0, 0
    values: dict[str, int] = {}
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        try:
            values[key.strip()] = int(value.strip().split()[0])
        except (ValueError, IndexError):
            pass
    return (values.get("Absolute upper-left X", 0),
            values.get("Absolute upper-left Y", 0),
            values.get("Width", 0), values.get("Height", 0))


def _unique_output(save_dir: Any) -> Path:
    raw = str(save_dir or "").strip()
    target = Path(raw).expanduser() if raw else default_save_dir()
    if not target.is_absolute():
        target = Path.home() / target
    if target.exists() and target.is_symlink():
        raise RecorderError("Refusing a symlinked recording directory.")
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise RecorderError("Could not create the recording directory: %s" % exc)
    if not target.is_dir():
        raise RecorderError("The recording destination is not a directory.")
    if not os.access(target, os.W_OK | os.X_OK):
        raise RecorderError("The recording destination is not writable.")
    base = time.strftime("WXB-%Y%m%d-%H%M%S")
    candidate = target / (base + RECORDING_SUFFIX)
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = target / ("%s-%d%s" % (base, counter, RECORDING_SUFFIX))
        counter += 1
    return candidate


def _free_bytes(path: Path) -> int:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def _scale_filter(settings: Mapping[str, Any]) -> list[str]:
    # libx264/yuv420p requires even dimensions.  Window geometry is often
    # odd (165x52 in a minimal X server), so crop at most one edge pixel
    # before scaling instead of letting FFmpeg fail at startup.
    filters = ["crop=trunc(iw/2)*2:trunc(ih/2)*2"]
    if settings["resolution"] in ("720p", "1080p", "1440p"):
        filters.append("scale=-2:%s:flags=fast_bilinear"
                       % settings["resolution"][:-1])
    filters.append("format=yuv420p")
    return filters


def _choose_encoder(settings: Mapping[str, Any], caps: Mapping[str, Any]) -> str:
    wanted = str(settings.get("encoder") or "auto")
    hardware = set(caps.get("hardware_encoders") or [])
    if wanted == "auto":
        return "libx264"
    if wanted == "cpu":
        return "libx264"
    if wanted in ("vaapi", "nvenc") and wanted in hardware:
        return "h264_%s" % wanted
    raise RecorderError(
        "%s hardware encoding is not available on this machine; choose CPU "
        "or Auto." % wanted.upper())


def build_x11_command(settings: Mapping[str, Any], caps: Mapping[str, Any],
                      output: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Build a shell-free FFmpeg argv for the selected X11 source."""
    environment = dict(os.environ if env is None else env)
    clean = normalize_settings(settings)
    ffmpeg = str(caps.get("ffmpeg") or shutil.which("ffmpeg") or "")
    if not ffmpeg or not caps.get("x11grab") or not caps.get("x11"):
        raise RecorderError(caps.get("reason") or "X11 capture is unavailable.")
    display = str(environment.get("DISPLAY") or "").strip()
    if not display:
        raise RecorderError("No X11 DISPLAY is available.")
    screen_w, screen_h = _x11_screen_size(environment)
    source = clean["source"]
    if source == "region":
        if not (clean["region_w"] > 0 and clean["region_h"] > 0):
            raise RecorderError("Set a positive region width and height.")
        x, y = clean["region_x"], clean["region_y"]
        width, height = clean["region_w"], clean["region_h"]
        if screen_w and screen_h and (x + width > screen_w
                                      or y + height > screen_h):
            raise RecorderError("The selected region is outside the screen.")
    elif source == "window":
        if not clean["window_id"]:
            raise RecorderError("Choose an X11 window ID or use Active window.")
        x, y, width, height = window_geometry(clean["window_id"], environment)
        if not (width > 0 and height > 0):
            raise RecorderError(
                "Could not read the selected X11 window's geometry; the "
                "window may have closed. Choose Active window again.")
        if x < 0 or y < 0:
            raise RecorderError("The selected window is partially off-screen.")
    else:
        x = y = 0
        width, height = screen_w, screen_h

    # The caller reserves a brand-new 0600 path with O_EXCL.  -y is allowed to
    # write only that reserved file; build_x11_command itself never chooses an
    # existing output name.
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
            "-f", "x11grab", "-framerate", str(clean["fps"]),
            "-draw_mouse", "1" if clean["cursor"] else "0"]
    if not (width > 0 and height > 0):
        raise RecorderError("Could not determine the capture size.")
    argv += ["-video_size", "%dx%d" % (width, height),
             "-i", "%s+%d,%d" % (display, x, y)]

    audio_inputs: list[str] = []
    if clean["mic"]:
        audio_inputs.append(clean["mic_device"] or "default")
    if clean["desktop_audio"]:
        devices = list_audio_devices(environment)
        device = clean["desktop_device"] or (
            devices["desktop"][0] if devices["desktop"] else "")
        if not device:
            raise RecorderError(
                "Desktop audio is unavailable: no Pulse/PipeWire monitor "
                "source was found.")
        audio_inputs.append(device)
    for device in audio_inputs:
        argv += ["-f", "pulse", "-i", device]

    filters = _scale_filter(clean)
    if len(audio_inputs) == 2:
        argv += ["-filter_complex",
                 "[1:a][2:a]amix=inputs=2:duration=longest[a]",
                 "-map", "0:v:0", "-map", "[a]"]
    elif len(audio_inputs) == 1:
        argv += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        argv += ["-an"]

    encoder = _choose_encoder(clean, caps)
    quality = QUALITY.get(clean["quality"], QUALITY["balanced"])
    if encoder == "libx264":
        argv += ["-vf", ",".join(filters),
                 "-c:v", "libx264", "-preset", quality["preset"],
                 "-crf", str(quality["crf"]), "-pix_fmt", "yuv420p"]
    elif encoder == "h264_vaapi":
        device = "/dev/dri/renderD128"
        vaapi_filters = [item for item in filters
                         if not item.startswith("format=")]
        vaapi_filters += ["format=nv12", "hwupload"]
        argv += ["-vaapi_device", device, "-vf", ",".join(vaapi_filters),
                 "-c:v", "h264_vaapi", "-qp", str(quality["crf"])]
    else:
        argv += ["-vf", ",".join(filters),
                 "-c:v", "h264_nvenc", "-preset", "p4",
                 "-cq", str(quality["crf"]), "-pix_fmt", "yuv420p"]
    if audio_inputs:
        argv += ["-c:a", "aac", "-b:a", "160k"]
    argv += ["-metadata", "title=WXB screen recording",
             "-f", "matroska", str(output)]
    return argv


def probe_output(path: Path, caps: Mapping[str, Any],
                 env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read back duration and dimensions; a bad file is reported, not hidden."""
    ffprobe = str(caps.get("ffprobe") or shutil.which("ffprobe") or "")
    if not ffprobe:
        return {}
    rc, out, _err = _run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1", str(path)], 5, env)
    if rc != 0:
        return {}
    values: dict[str, Any] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    try:
        duration = float(values.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    result: dict[str, Any] = {"duration": round(duration, 3)}
    for key in ("width", "height"):
        try:
            result[key] = int(values.get(key) or 0)
        except (TypeError, ValueError):
            result[key] = 0
    rate = str(values.get("avg_frame_rate") or "")
    if "/" in rate:
        try:
            numerator, denominator = (int(part) for part in rate.split("/", 1))
            result["fps"] = round(numerator / denominator, 2) if denominator else 0
        except (TypeError, ValueError, ZeroDivisionError):
            result["fps"] = 0
    return result


def _safe_signal(proc: Any, sig: int) -> None:
    try:
        proc.send_signal(sig)
    except (OSError, ValueError, AttributeError):
        pass


class Recorder:
    """Own one local capture process and its bounded lifecycle."""

    def __init__(self, settings_getter: Callable[[], Mapping[str, Any]],
                 capabilities_getter: Callable[[], Mapping[str, Any]] | None = None,
                 popen_factory: Callable[..., Any] | None = None,
                 session_guard: Callable[[], bool] | None = None,
                 emit: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 probe_factory: Callable[..., dict] | None = None) -> None:
        self._settings_getter = settings_getter
        self._capabilities_getter = capabilities_getter or detect_capabilities
        self._popen = popen_factory or subprocess.Popen
        self._session_guard = session_guard
        self._emit_cb = emit
        self._clock = clock
        self._sleep = sleep
        self._probe = probe_factory or probe_output
        self._lock = threading.RLock()
        self._proc = None
        self._state = "idle"
        self._output: Path | None = None
        self._command: list[str] = []
        self._error = ""
        self._notice = ""
        self._started = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._stopping = False
        self._stop_reason = ""
        self._done = threading.Event()
        self._monitor: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._last_stderr = ""
        self._probe_result: dict[str, Any] = {}

    def _emit(self) -> None:
        if self._emit_cb is not None:
            try:
                self._emit_cb()
            except Exception:
                pass

    @property
    def recording(self) -> bool:
        return self._state in ("recording", "paused", "stopping")

    def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities_getter())

    def settings(self) -> dict[str, Any]:
        return normalize_settings(self._settings_getter())

    def elapsed(self) -> float:
        if not self._started:
            return 0.0
        now = self._clock()
        paused = self._paused_total
        if self._state == "paused" and self._paused_at:
            paused += max(0.0, now - self._paused_at)
        return round(max(0.0, now - self._started - paused), 3)

    def snapshot(self) -> dict[str, Any]:
        settings = self.settings()
        caps = self.capabilities()
        process = self._proc
        pid = int(getattr(process, "pid", 0) or 0) if process is not None else 0
        return {
            "available": bool(caps.get("record")),
            "recording": self._state == "recording",
            "starting": self._state == "starting",
            "paused": self._state == "paused",
            "running": self.recording,
            "state": self._state,
            "elapsed": self.elapsed(),
            "output": str(self._output or ""),
            "pid": pid,
            "error": self._error,
            "notice": self._notice,
            "probe": dict(self._probe_result),
            "settings": settings,
            "capabilities": caps,
            "audio_devices": list_audio_devices(),
            "limit": ("Matroska .mkv is recoverable if FFmpeg is interrupted; "
                      "no file is ever overwritten."),
        }

    def _prepare(self) -> Path:
        output = _unique_output(self.settings().get("save_dir"))
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except OSError as exc:
            raise RecorderError("Could not reserve the output file: %s" % exc)
        if _free_bytes(output.parent) < MIN_FREE_BYTES:
            try:
                output.unlink()
            except OSError:
                pass
            raise RecorderError(
                "Not enough free disk space (need at least 64 MiB).")
        return output

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self._state != "idle" and self._state != "error":
                return False, "A recording is already running."
            caps = self.capabilities()
            if not caps.get("record"):
                return False, caps.get("reason") or "Screen recording is unavailable."
            clean = self.settings()
            if clean["source"] not in caps.get("sources", []):
                return False, ("The selected source is not supported by this "
                               "session's capture backend.")
            output = None
            try:
                output = self._prepare()
                command = build_x11_command(clean, caps, output)
            except RecorderError as exc:
                if output is not None:
                    try:
                        output.unlink()
                    except OSError:
                        pass
                self._error = str(exc)
                self._state = "error"
                self._emit()
                return False, str(exc)
            try:
                proc = self._popen(
                    command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, bufsize=1, start_new_session=True)
            except (OSError, ValueError) as exc:
                if output is not None:
                    try:
                        output.unlink()
                    except OSError:
                        pass
                self._error = "Could not start FFmpeg: %s" % exc
                self._state = "error"
                self._emit()
                return False, self._error
            self._proc = proc
            self._command = command
            self._output = output
            self._state = "recording"
            self._error = ""
            self._notice = "Recording to %s" % output
            self._started = self._clock()
            self._paused_at = 0.0
            self._paused_total = 0.0
            self._stopping = False
            self._stop_reason = ""
            self._probe_result = {}
            self._last_stderr = ""
            self._done.clear()
            self._monitor = threading.Thread(
                target=self._monitor_process, args=(proc,),
                name="wxb-recorder-monitor", daemon=True)
            self._stderr_reader = threading.Thread(
                target=self._read_stderr, args=(proc,),
                name="wxb-recorder-stderr", daemon=True)
            self._monitor.start()
            self._stderr_reader.start()
            self._emit()
            return True, self._notice

    def _read_stderr(self, proc: Any) -> None:
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            for line in stream:
                text = str(line).strip()
                if text:
                    self._last_stderr = text[-600:]
        except (OSError, ValueError):
            pass

    def _monitor_process(self, proc: Any) -> None:
        last_tick = self._clock()
        while True:
            if proc.poll() is not None:
                break
            now = self._clock()
            if self._session_guard is not None and not self._stopping:
                try:
                    locked = bool(self._session_guard())
                except Exception:
                    locked = False
                if locked:
                    self._request_stop("Recording stopped because the "
                                       "desktop locked.")
            if not self._stopping:
                if self._output is not None and _free_bytes(
                        self._output.parent) < MIN_FREE_BYTES:
                    self._request_stop("Recording stopped because the disk "
                                       "is nearly full.")
                elif now - last_tick > _SESSION_GAP:
                    self._request_stop("Recording stopped because the desktop "
                                       "was suspended or the clock jumped.")
            last_tick = now
            self._sleep(0.5)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        self._finish(proc)

    def _request_stop(self, reason: str = "") -> None:
        with self._lock:
            if self._proc is None or self._stopping:
                return
            self._stopping = True
            self._stop_reason = reason
            self._state = "stopping"
            if self._paused_at:
                _safe_signal(self._proc, signal.SIGCONT)
                self._paused_at = 0.0
            _safe_signal(self._proc, signal.SIGINT)
            self._emit()

    def _finish(self, proc: Any) -> None:
        with self._lock:
            rc = getattr(proc, "returncode", None)
            output = self._output
            error = ""
            if not self._stopping:
                detail = self._last_stderr or "FFmpeg exited with code %s." % rc
                error = "Recording stopped unexpectedly: %s" % detail
            elif self._stop_reason:
                error = ""
            size = 0
            if output is not None:
                try:
                    size = output.stat().st_size
                except OSError:
                    size = 0
            if not size:
                if output is not None:
                    try:
                        output.unlink()
                    except OSError:
                        pass
                if not error:
                    error = "The recording did not produce a usable file."
            else:
                try:
                    os.chmod(output, 0o600)
                except OSError:
                    pass
                caps = self.capabilities()
                try:
                    self._probe_result = self._probe(output, caps)
                except Exception:
                    self._probe_result = {}
            if error:
                self._error = error
                self._state = "error"
                self._notice = ""
            else:
                self._state = "idle"
                self._error = ""
                self._notice = "Recording saved to %s" % output
                if self._stop_reason:
                    self._notice = "%s Saved to %s." % (
                        self._stop_reason, output)
            self._proc = None
            self._stopping = False
            self._done.set()
            stream = getattr(proc, "stderr", None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self._emit()

    def pause(self) -> tuple[bool, str]:
        with self._lock:
            if self._state != "recording" or self._proc is None:
                return False, "No recording is running."
            _safe_signal(self._proc, signal.SIGSTOP)
            self._state = "paused"
            self._paused_at = self._clock()
            self._notice = "Recording paused."
            self._emit()
            return True, self._notice

    def resume(self) -> tuple[bool, str]:
        with self._lock:
            if self._state != "paused" or self._proc is None:
                return False, "The recording is not paused."
            _safe_signal(self._proc, signal.SIGCONT)
            if self._paused_at:
                self._paused_total += max(0.0, self._clock() - self._paused_at)
            self._paused_at = 0.0
            self._state = "recording"
            self._notice = "Recording resumed."
            self._emit()
            return True, self._notice

    def stop(self, wait: bool = True) -> tuple[bool, str]:
        with self._lock:
            if self._proc is None:
                return False, "No recording is running."
            was_ready = self._state in ("recording", "paused")
            self._request_stop("")
            if not was_ready and self._state == "stopping":
                pass
            proc = self._proc
        if not wait:
            return True, "Stopping the recording…"
        try:
            proc.wait(timeout=STOP_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            _safe_signal(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=3.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except (OSError, AttributeError):
                    pass
        self._done.wait(timeout=3.0)
        return (self._state != "error"), (self._notice or self._error)

    def shutdown(self) -> None:
        try:
            self.stop(wait=True)
        except Exception:
            pass
