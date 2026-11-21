# Vendored from Wookhq/Lution @ 303be659833a (MIT, Copyright (c) 2026 WookHQ).
# Import paths are package-relative; everything else is upstream.
# See NOTICE.md next to this package.
# logging yeahh

import sys
from datetime import datetime
from pathlib import Path

from . import paths

RESET = "\x1b[0m"
BLUE = "\x1b[34m"
WHITE = "\x1b[37m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"

LEVEL_COLORS = {
    "info": WHITE,
    "debug": WHITE,
    "warning": YELLOW,
    "error": RED,
}

def log_file():
    return paths.state_dir() / "lution.log"


def _log(level, msg):
    color = LEVEL_COLORS.get(level, WHITE)
    now = datetime.now()
    time_str = now.strftime("%H:%M:%S")
    stream = sys.stderr if level == "error" else sys.stdout
    print(f"{time_str} {BLUE}lution{RESET} {color}{level}{RESET} {msg}",
          file=stream, flush=True)
    try:
        log_path = log_file()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"{stamp} lution {level} {msg}\n")
    except OSError:
        pass


def info(msg):
    _log("info", msg)


def debug(msg):
    _log("debug", msg)


def warning(msg):
    _log("warning", msg)


def error(msg):
    _log("error", msg)


def __getattr__(name):
    if name == "LOG_FILE":
        return log_file()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
