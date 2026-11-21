# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# basically managing sober like reinstalling uninstalling installing updating
# this is now useless because it's replaced with a how to install sober etc etc button
import subprocess
from pathlib import Path
import sys
import os

from . import log
from . import paths

SOBER_APP_ID = "org.vinegarhq.Sober"
REMOTE = "flathub"
FLATHUB_REPO = "https://flathub.org/repo/flathub.flatpakrepo"

def sober_data_dir():
    return paths.lution_home() / ".var/app" / SOBER_APP_ID


def clean_env():
    env = os.environ.copy()
    # Flatpak writes its user installation under HOME.  In an elevated WXB
    # process the Lution/desktop home is authoritative even when the bridge
    # did not get to replace root's HOME.
    env["HOME"] = str(paths.lution_home())
    lp = env.get("LD_LIBRARY_PATH", "")
    if lp:
        meipass = getattr(sys, "_MEIPASS", None)
        keep = [p for p in lp.split(":") if p
                and (not meipass or not p.startswith(meipass))]
        env["LD_LIBRARY_PATH"] = ":".join(keep)
    return env


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=clean_env())
    except subprocess.TimeoutExpired:
        log.warning(f"Command timed out after {timeout}s: {cmd[0]} {cmd[1] if len(cmd)>1 else ''}")
        return None
    except OSError as exc:
        # flatpak not installed at all (upstream only caught timeouts, so this
        # raised FileNotFoundError straight into the UI)
        log.warning(f"Command failed: {cmd[0]}: {exc}")
        return None
def _stream(args, output_cb=None):
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=clean_env())
    except FileNotFoundError:
        return False, "flatpak not found on this system"

    def emit(raw):
        line = raw.decode(errors="replace").strip()
        if line and output_cb:
            output_cb(line)

    buf = b""
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if chunk in (b"\n", b"\r"):
            emit(buf)
            buf = b""
        else:
            buf += chunk
    emit(buf)
    try:
        proc.wait()
    finally:
        # Do not leave the pipe (or the subprocess) to the garbage collector;
        # focused integration runs otherwise end with ResourceWarnings.
        if proc.stdout is not None:
            proc.stdout.close()

    ok = proc.returncode == 0
    return ok, "done" if ok else "failed"

def is_installed():
    result = _run(["flatpak", "list", "--app", "--columns=application"])
    if result is None or result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if line.strip() == SOBER_APP_ID:
            return True
    return False

def has_flathub():
    result = _run(["flatpak", "remotes", "--columns=name"])
    if result is None or result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if line.strip() == REMOTE:
            return True
    return False

def add_flathub():
    result = _run(["flatpak", "remote-add", "--if-not-exists", REMOTE,
                    FLATHUB_REPO])
    if result is None:
        return False, "flatpak command timed out"
    return result.returncode == 0, (result.stderr or "").strip()

def get_version():
    result = _run(["flatpak", "info", SOBER_APP_ID])
    if result is None or result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Version:"):
            return stripped.split(":", 1)[1].strip()
    return ""

def _commit_from_output(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Commit:"):
            return stripped.split(":", 1)[1].strip()
    return ""

def installed_commit():
    result = _run(["flatpak", "info", SOBER_APP_ID])
    if result is None or result.returncode != 0:
        return ""
    return _commit_from_output(result.stdout)

def remote_commit():
    result = _run(["flatpak", "remote-info", REMOTE, SOBER_APP_ID])
    # `_run` returns None when flatpak is missing or the call times out; that
    # is the offline case `ensure_sober` handles, not an AttributeError
    if result is None or result.returncode != 0:
        return ""
    return _commit_from_output(result.stdout)

def install(output_cb=None):
    return _stream(["flatpak", "install", "-y", REMOTE, SOBER_APP_ID], output_cb)

def update(output_cb=None):
    return _stream(["flatpak", "update", "-y", SOBER_APP_ID], output_cb)

def ensure_sober(output_cb=None):
    if not is_installed():
        log.info("Sober not found, installing")
        if output_cb:
            output_cb("Sober not found, installing...")
        if not has_flathub():
            log.info("Adding flathub remote")
            if output_cb:
                output_cb("Adding flathub remote...")
            ok, err = add_flathub()
            if not ok:
                log.error(f"Could not add flathub: {err}")
                return False, f"Could not add flathub how weird: {err}"
        return install(output_cb)

    remote = remote_commit()
    if not remote:
        log.warning("flathub is unreachable sorry bro we are skipping the update check")
        if output_cb:
            output_cb("Can't reach Flathub — launching installed Sober")
        return True, "offline"

    local = installed_commit()
    if local and local == remote:
        log.info(f"Sober is already up to date (v{get_version()})")
        if output_cb:
            output_cb("Sober is already up to date")
        return True, "already up to date"

    log.info("Sober found, checking for updates")
    if output_cb:
        output_cb("Sober found, checking for updates...")
    return update(output_cb)

def uninstall(output_cb=None):
    return _stream(["flatpak", "uninstall", "-y", SOBER_APP_ID], output_cb)

def delete_sober_data():
    import shutil
    data_dir = sober_data_dir()
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
        return not data_dir.exists()
    return True


def __getattr__(name):
    if name == "SOBER_DATA_DIR":
        return sober_data_dir()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
