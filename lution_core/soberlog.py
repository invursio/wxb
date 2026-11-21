# WXB addition, not upstream Lution.
# Why a crash happened, from the only witness that survives it: Sober's log.
#
# Sober writes one opaque binary dump per abnormal exit (those live in
# data/sober/crashes and say nothing a program can read), but it also writes a
# running text log under data/sober/sober_logs/, and the Roblox client logs
# into appData/logs/.  A session that was killed stops mid-file with no
# "clean_exit" marker, so the tail of that file is the evidence: how long the
# run lasted, what Roblox last said, and how much memory it reported.
import re
from pathlib import Path

SOBER_APP_ID = "org.vinegarhq.Sober"
LOG_DIR = (Path.home() / ".var/app" / SOBER_APP_ID /
           "data/sober/sober_logs")
CLIENT_LOG_DIR = (Path.home() / ".var/app" / SOBER_APP_ID /
                  "data/sober/appData/logs")
# Enough of the file to cover a whole session's worth of context: a Roblox
# log in a busy place runs to a few hundred kilobytes, and the interesting
# line (the memory figure, the last error) is rarely in the final page.
TAIL_BYTES = 4 * 1024 * 1024
MAX_LINES = 3

# "info: Roblox: 2026-10-26T22:10:58.770Z,649.770874,c0,6,Error [FLog::X] msg"
_ROBLOX = re.compile(
    r"^.*?Roblox: (\S+?),(?P<elapsed>[0-9.]+),[0-9a-f]+,(?P<level>\d+),?"
    r"\s*(?P<body>.*)$")
_MEMORY = re.compile(r"AppMemUsageStatus\]\s*([0-9.]+)")
_DURATION = re.compile(r"clean_exit_success delta=([0-9.]+)ms")
# Sober's own log forwarding fails in the Flatpak sandbox on every line; it is
# noise here, not a symptom.
_NOISE = ("sendto failed", "Address family not supported")
# Roblox writes its severity twice: as a word at the start of the message on
# most lines, and as a numeric level on the rest (12 and up are warning and
# error).  "failed" alone is far too loose — a Stun probe reporting "0
# failed" is not a symptom.
_SEVERITY_WORDS = ("error", "fatal", "abort", "panic", "exception",
                   "assert")
_WARNING_WORDS = ("warning", "crash", "out of memory", "cannot allocate")
# Error-level lines that carry no diagnosis: Roblox logs every WebView bridge
# message at that level, which drowns out the real one.
_NOISE_BODIES = ("sending command", "received command", "rcv command",
                 "callbackid")


def _tail(path, limit=TAIL_BYTES):
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()               # drop the half line
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "ignore").splitlines()


def newest_log(directory=None, pattern="*.log", before=None):
    """The newest log, or the newest one written before `before`.

    `before` is the crash dump's own mtime: the session that died is the one
    that was being written when the dump appeared, not whatever ran next.
    """
    directory = Path(directory or LOG_DIR)
    try:
        entries = [p for p in directory.glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not entries:
        return None
    if before is not None:
        older = [p for p in entries if p.stat().st_mtime <= float(before)]
        if older:
            entries = older
    return max(entries, key=lambda p: p.stat().st_mtime)


def _trim(text, limit=180):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _summary(lines):
    """Turn the tail of one log into something a person can act on."""
    first_elapsed = last_elapsed = None
    exit_delta = None
    memory = None
    last_line = ""
    errors = []
    warnings = []
    clean = False
    for line in lines:
        if any(noise in line for noise in _NOISE):
            continue
        if "clean_exit_success" in line:
            clean = True
            found = _DURATION.search(line)
            if found:
                # how long shutting down took, not how long the run lasted
                exit_delta = float(found.group(1)) / 1000.0
            continue
        found = _MEMORY.search(line)
        if found:
            try:
                memory = float(found.group(1))
            except ValueError:
                pass
        match = _ROBLOX.match(line)
        if match:
            try:
                elapsed = float(match.group("elapsed"))
            except ValueError:
                elapsed = None
            if elapsed is not None:
                first_elapsed = (elapsed if first_elapsed is None
                                 else first_elapsed)
                last_elapsed = elapsed
            body = match.group("body").strip()
            level = match.group("level")
            if body:
                last_line = body
                low = body.lower()
                if any(noise in low for noise in _NOISE_BODIES):
                    continue
                if (low.startswith(_SEVERITY_WORDS)
                        or (level.isdigit() and int(level) >= 12)):
                    errors.append(body)
                elif low.startswith(_WARNING_WORDS):
                    warnings.append(body)
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("info: Roblox:"):
            low = stripped.lower()
            if low.startswith(("warn", "error", "fatal")):
                warnings.append(stripped)
            last_line = last_line or stripped
    return {"clean_exit": clean,
            # Roblox's own elapsed clock: the run's length on the crashed
            # sessions, and 0.0 for a session that never got going
            "ran_for": last_elapsed, "exit_delta": exit_delta,
            "memory": memory, "last_line": _trim(last_line),
            "errors": [_trim(n) for n in errors[-MAX_LINES:]],
            "warnings": [_trim(n) for n in warnings[-MAX_LINES:]]}


def crash_context(directory=None, since=None, client_directory=None):
    """What the logs say about the last session. Never raises.

    `since` is the newest crash dump's mtime, so the session that died is
    the one read even if a later run has already written its own log.
    """
    out = {"ok": False, "file": "", "clean_exit": None, "ran_for": None,
           "memory": None, "last_line": "", "errors": [], "warnings": [],
           "error": None}
    try:
        log = newest_log(directory, before=since)
        if log is None:
            out["error"] = "no Sober logs on this account"
            return out
        lines = _tail(log)
        if not lines:
            out["error"] = "the newest Sober log is empty"
            return out
        out.update(_summary(lines))
        out["file"] = log.name
        out["mtime"] = log.stat().st_mtime
        out["ok"] = True
    except Exception as exc:                    # decoration, never fatal
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def describe(context):
    """One sentence for the Stability card, or an empty string.

    The last *error* leads, because the last line of a killed session is
    usually a heartbeat. With no error in the log the real last line is
    quoted instead: "it just stopped" is itself the answer.
    """
    if not context or not context.get("ok"):
        return ""
    parts = []
    ran = context.get("ran_for")
    if ran:
        minutes, seconds = divmod(int(ran), 60)
        parts.append("Sober ran %s" % (
            "%dm %02ds" % (minutes, seconds) if minutes else "%ds" % seconds))
    memory = context.get("memory")
    if memory:
        parts.append("at %.1f GB" % (memory / (1024.0 ** 3)))
    if context.get("clean_exit") is False:
        parts.append("then stopped without a clean exit")
    elif context.get("clean_exit"):
        parts.append("and closed cleanly")
    head = ", ".join(parts)
    errors = context.get("errors") or []
    warnings = context.get("warnings") or []
    if errors:
        tail = "Last error: %s" % errors[-1]
    elif warnings:
        tail = "Last warning: %s" % warnings[-1]
    else:
        tail = "Last line: %s" % (context.get("last_line") or "")
    if head:
        return "%s. %s" % (head, tail) if tail.strip() else head
    return tail
