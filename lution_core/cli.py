"""JSON bridge between the WXB UI and the vendored Lution core.

    python3 -m lution_core.cli <op> [args...]

Every op prints exactly one JSON object on stdout and exits 0, even when the
op failed (the object then carries `ok: false` and `error`). Lution's `log`
module prints progress to stdout, so stdout is redirected to stderr for the
duration of the op; only the final JSON reaches the caller.

Why a subprocess at all: WXB usually runs elevated (the SOCD cleaner needs
/dev/uinput) and Flatpak refuses to run as root, while every path in this
package is under the desktop user's home. The UI runs this module as that
user, so the vendored code always sees the same HOME/XDG session Lution does.
"""
import contextlib
import json
import subprocess
import sys
import traceback

from . import (cursors, envvars, fflags, fonts, fps, launcher, playtime,
               sober, soberlog)


def _quiet():
    """Lution's logger writes to stdout; keep the JSON channel clean."""
    return contextlib.redirect_stdout(sys.stderr)


def op_status():
    installed = sober.is_installed()
    data = {
        "ok": True,
        "installed": installed,
        "version": sober.get_version() if installed else "",
        "commit": (sober.installed_commit() or "") if installed else "",
        "running": launcher.sober_is_running() if installed else False,
        "fflags": fflags.get_fflags(),
        "allowlist": sorted(fflags.allowlist()),
        "env": envvars.load_vars(),
        "crashes": op_crashes(),
        "cursor": cursors.status(),
        "fps": None,
        "playtime": {"seconds": 0, "text": "0s", "sessions": 0},
        "error": None,
    }
    try:
        data["fps"] = fps.load_framerate_cap()
    except Exception as exc:                    # missing/broken settings XML
        data["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        places, seconds, sessions = playtime.scan()
        data["playtime"] = {"seconds": int(seconds),
                            "text": playtime.format_duration(seconds),
                            "sessions": sessions,
                            "places": {str(pid): int(sec)
                                       for pid, sec in places.items()}}
    except Exception:
        pass                                    # playtime is decoration
    return data


def op_playtime():
    """Per-place and total time, parsed from Sober's logs on this machine.

    This is WXB's local playtime integration: no
    account, no Roblox API, no background service. The scan is cached by
    log size and mtime, so opening Quick Play repeatedly is cheap.
    """
    places, seconds, sessions = playtime.scan()
    return {"ok": True,
            "playtime": {"seconds": int(seconds),
                         "text": playtime.format_duration(seconds),
                         "sessions": sessions,
                         "places": {str(pid): int(sec)
                                    for pid, sec in places.items()}}}


def op_ensure():
    lines = []

    def note(line):
        lines.append(str(line))
        del lines[:-6]

    ok, message = sober.ensure_sober(note)
    # a new APK can ship font faces our overlay does not cover yet
    reapplied = 0
    if ok:
        try:
            reapplied = fonts.reapply_fonts()
        except OSError as exc:
            note("font reapply failed: %s" % exc)
    return {"ok": bool(ok), "message": message or "", "lines": lines,
            "version": sober.get_version() if ok else "",
            "reapplied": reapplied}


def op_launch(plain=False, url=None, mangohud=False, gamemode=False,
              gamescope=False):
    if not sober.is_installed():
        return {"ok": False, "error": "Sober is not installed yet"}
    # The UI reads this process's stdout until EOF. A launched game that
    # inherits that pipe keeps it open, so the bridge would only return when
    # the game exits - "Start game" would sit on STARTING and then report a
    # timeout although Sober is up. Give the child its own null streams.
    streams = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        # Only send effects that are actually on.  An off launch must remain
        # byte-for-byte the same call the engine made before these options
        # existed, and old launcher stubs keep working.
        options = {key: True for key, enabled in (
            ("mangohud", mangohud), ("gamemode", gamemode),
            ("gamescope", gamescope)) if enabled}
        proc = (launcher.launch_plain(url=url, **streams, **options) if plain
                else launcher.launch(url=url, **streams, **options))
    except launcher.LaunchError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "pid": getattr(proc, "pid", None)}


def op_fflags_get():
    return {"ok": True, "fflags": fflags.get_fflags(),
            "allowlist": sorted(fflags.allowlist())}


def running_refusal(what):
    """Say no to writing Sober's config while the game is using it.

    Lution bind-mounts its config over Sober's, so a flag table written
    mid-session is read by a client that already started with the old one —
    the mixed state that makes Roblox crash. The same goes for its settings
    XML, which Roblox itself writes back on exit.
    """
    try:
        if not launcher.sober_is_running():
            return None
    except Exception:
        return None
    return {"ok": False,
            "error": "%s cannot be changed while Sober is running: close "
                     "the game first, then save. Writing its config under a "
                     "live client is one of the ways it gets crashed." % what}


def op_fflags_set(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "flags must be an object"}
    refusal = running_refusal("Fast flags")
    if refusal is not None:
        return refusal
    allowed = fflags.allowlist()
    clean = {}
    bad = []
    for name, value in payload.items():
        name = str(name).strip()
        if not name:
            continue
        if name not in allowed:
            bad.append(name)
            continue
        clean[name] = value
    fflags.save_fflags(clean)
    return {"ok": True, "saved": len(clean), "removed": sorted(bad),
            "fflags": clean}


def op_fflags_clean():
    refusal = running_refusal("Fast flags")
    if refusal is not None:
        return refusal
    kept, bad = fflags.clean(fflags.get_fflags())
    fflags.save_fflags(kept)
    return {"ok": True, "kept": len(kept), "removed": sorted(bad),
            "fflags": kept}


def op_env_get():
    return {"ok": True, "env": envvars.load_vars()}


def op_env_set(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "environment must be an object"}
    saved = envvars.save_vars(payload)
    return {"ok": True, "env": saved}


def op_fonts_status():
    return {"ok": True, "font": fonts.status()}


def op_fonts_apply():
    path = sys.argv[2] if len(sys.argv) > 2 else ""
    if not path:
        return {"ok": False, "error": "no font file given"}
    try:
        names = fonts.apply_font(path)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "replaced": len(names), "font": fonts.status()}


def op_fonts_restore():
    removed = fonts.restore_fonts()
    return {"ok": True, "removed": removed, "font": fonts.status()}


def op_fonts_reapply():
    """Reapply a kept font after Sober replaced its APK."""
    try:
        count = fonts.reapply_fonts()
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "reapplied": count, "font": fonts.status()}


def op_stop():
    """Stop the user's Flatpak session from the user's own bridge."""
    try:
        result = subprocess.run(
            ["flatpak", "kill", sober.SOBER_APP_ID],
            capture_output=True, text=True, timeout=20,
            env=sober.clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": result.returncode == 0,
            "error": (result.stderr or "").strip()}


def op_crashes(since=None):
    """Crash dumps plus what the logs say about the session that made one."""
    state = launcher.crash_state()
    stamp = since if since is not None else state.get("latest")
    context = soberlog.crash_context(since=stamp)
    state["context"] = context
    state["summary"] = soberlog.describe(context)
    return state


def _op_crashes():
    """`crashes [dump-mtime]`: the UI passes the dump's own timestamp."""
    raw = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        since = float(raw) if raw else None
    except ValueError:
        since = None
    return op_crashes(since=since)


def op_running():
    """A cheap poll for the crash guard; no flatpak listing, no playtime scan."""
    return {"ok": True, "running": launcher.sober_is_running()}


def op_cursors_status():
    return {"ok": True, "cursor": cursors.status()}


def op_cursors_apply(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "cursor choices must be an object"}
    return cursors.apply(payload.get("style"), payload.get("color"),
                         payload.get("size"))


def op_cursors_restore():
    return cursors.restore()


def op_cursors_online_apply(payload):
    if not isinstance(payload, dict):
        return {"ok": False, "error": "cursor choices must be an object"}
    return cursors.apply_online(payload.get("name"), payload.get("size"))


def op_fps_get():
    try:
        return {"ok": True, "fps": fps.load_framerate_cap()}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def op_fps_set(value):
    refusal = running_refusal("The framerate cap")
    if refusal is not None:
        return refusal
    try:
        cap = int(str(value).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "framerate cap must be a whole number"}
    if not 0 <= cap <= 10 ** 6:
        return {"ok": False, "error": "framerate cap out of range"}
    try:
        fps.save_framerate_cap(cap)
    except Exception as exc:
        return {"ok": False,
                "error": "Could not write Sober's settings (%s). Start Sober "
                         "once so it creates GlobalBasicSettings_13.xml."
                         % exc}
    return {"ok": True, "fps": cap}


def op_config_apply(payload):
    """Restore a whole saved setup in one process.

    A config is Sober-side state: the pointer, the flag table, the
    environment and the framerate cap. Doing each in its own bridge run cost
    four interpreter starts and left a half-applied config behind when one
    of them failed, so a named config is applied here as one transaction.
    The running guard is per piece: a live client must not see the flag
    table or the settings XML change under it, while the environment file
    and the pointer are only read at the next launch and are always safe.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "config must be an object"}
    result = {"ok": True, "skipped": [], "notes": []}
    try:
        running = launcher.sober_is_running()
    except Exception:
        running = False

    cursor_choice = payload.get("cursor")
    if isinstance(cursor_choice, dict):
        got = cursors.apply(cursor_choice.get("style"),
                            cursor_choice.get("color"),
                            cursor_choice.get("size"))
        if got.get("ok"):
            result["cursor"] = got.get("cursor")
        else:
            # a missing Pillow must not stop the flags from landing
            result["skipped"].append("cursor")
            result["notes"].append(got.get("error") or "cursor failed")

    wanted_flags = payload.get("fflags")
    if isinstance(wanted_flags, dict):
        if running:
            result["skipped"].append("flags")
        else:
            allowed = fflags.allowlist()
            clean, bad = {}, []
            for name, value in wanted_flags.items():
                name = str(name).strip()
                if not name:
                    continue
                if name in allowed:
                    clean[name] = value
                else:
                    bad.append(name)
            fflags.save_fflags(clean)
            result["fflags"] = clean
            result["removed"] = sorted(bad)

    wanted_env = payload.get("env")
    if isinstance(wanted_env, dict):
        result["env"] = envvars.save_vars(wanted_env)

    wanted_fps = payload.get("fps")
    if wanted_fps not in (None, ""):
        if running:
            result["skipped"].append("framerate cap")
        else:
            try:
                cap = int(str(wanted_fps).strip())
            except (TypeError, ValueError):
                cap = None
            if cap is None or not 0 <= cap <= 10 ** 6:
                result["skipped"].append("framerate cap")
                result["notes"].append("framerate cap out of range")
            else:
                try:
                    fps.save_framerate_cap(cap)
                    result["fps"] = cap
                except Exception as exc:
                    result["skipped"].append("framerate cap")
                    result["notes"].append(
                        "framerate cap: %s" % type(exc).__name__)

    if result["skipped"] and running:
        result["notes"].append("Sober is running, so its flag table and "
                               "settings were left alone")
    return result


def _read_payload():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _op_launch():
    """`launch [url] [--plain] [--mangohud] [--gamemode] [--gamescope]`"""
    args = sys.argv[2:]
    url = next((a for a in args if not a.startswith("--")), None)
    if "--drop-flags" in args:
        # the crash guard's reopen: a dump means the client died holding the
        # flag table it fetched before that run, so do not hand it back
        launcher.invalidate_flag_cache()
    return op_launch(plain="--plain" in args, url=url,
                     mangohud="--mangohud" in args,
                     gamemode="--gamemode" in args,
                     gamescope="--gamescope" in args)


OPS = {
    "status": op_status,
    "playtime": op_playtime,
    "ensure": op_ensure,
    "launch": _op_launch,
    "fflags-get": op_fflags_get,
    "fflags-set": lambda: op_fflags_set(_read_payload()),
    "fflags-clean": op_fflags_clean,
    "env-get": op_env_get,
    "env-set": lambda: op_env_set(_read_payload()),
    "config-apply": lambda: op_config_apply(_read_payload()),
    "fps-get": op_fps_get,
    "fps-set": lambda: op_fps_set(sys.argv[2] if len(sys.argv) > 2 else ""),
    "fonts-status": op_fonts_status,
    "fonts-apply": op_fonts_apply,
    "fonts-restore": op_fonts_restore,
    "fonts-reapply": op_fonts_reapply,
    "stop": op_stop,
    "crashes": _op_crashes,
    "running": op_running,
    "cursors-status": op_cursors_status,
    "cursors-apply": lambda: op_cursors_apply(_read_payload()),
    "cursors-online-apply": lambda: op_cursors_online_apply(_read_payload()),
    "cursors-restore": op_cursors_restore,
}


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    op = argv[1] if len(argv) > 1 else "status"
    try:
        fn = OPS[op]
    except KeyError:
        print(json.dumps({"ok": False, "error": "unknown op: %s" % op}))
        return 0
    try:
        with _quiet():
            result = fn()
    except Exception as exc:                    # never leave the UI hanging
        traceback.print_exc(file=sys.stderr)
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(result, dict):
        result = {"ok": True, "value": result}
    result.setdefault("ok", True)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
