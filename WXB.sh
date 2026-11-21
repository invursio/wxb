#!/bin/bash
# Launches WXB (AutoClicker + rotation switcher + SOCD) with no silent
# failures.
#
#   * the icon runs this script; unless told otherwise it re-launches itself
#     through pkexec, so the app runs with full access (/dev/input +
#     /dev/uinput for the SOCD cleaner). Polkit asks for
#     your password — the same click without it is `WXB_NO_ELEVATE=1`.
#   * the user's DISPLAY/XAUTHORITY/XDG_* environment and $HOME are carried
#     through the elevation, so GTK can open the window and pynput's --user
#     install is found.
#   * files this script creates while root (the log) are chowned back to you.
#   * picks the first python3 that can actually import pynput
#     (override with WXB_PYTHON=/path/to/python)
#   * the app itself refuses to start twice: a second launch brings the
#     running window forward and exits 0, which is not an error
#   * every launch is appended to wxb.log next to this script (trimmed at 64 KB)
#   * if the app dies at once, you get a dialog with the reason
set -u
cd "$(dirname "$0")" || exit 1
APP_DIR="$PWD"
SELF="$APP_DIR/$(basename "$0")"
LOG="$APP_DIR/wxb.log"
OWNER_UID="${PKEXEC_UID:-${SUDO_UID:-}}"
OWNER_USER="${PKEXEC_USER:-${SUDO_USER:-}}"

tell() {
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --width=520 --title="WXB" --text="$1" 2>/dev/null && return 0
    fi
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -u critical "WXB" "$1" 2>/dev/null && return 0
    fi
    return 1
}

# --opt-out is the documented way to run unprivileged from a terminal
for arg in "$@"; do
    case "$arg" in
        --no-elevate) export WXB_NO_ELEVATE=1 ;;
    esac
done

# ---------------------------------------------------------------------------
# elevation (icon click -> pkexec -> this same script as root)
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ] && [ "${WXB_ELEVATED:-0}" != "1" ] &&
   [ "${WXB_NO_ELEVATE:-0}" != "1" ]; then
    if [ -z "${DISPLAY:-}" ]; then
        tell "WXB needs an X11 display.

Log in with the \"X11\" session (the app's global hotkeys need it), then
click the icon again."
        exit 1
    fi
    AUTH="${XAUTHORITY:-$HOME/.Xauthority}"
    # the askpass helper runs in this environment, so it needs the display too
    export DISPLAY XAUTHORITY="$AUTH"
    ENVIRON=(
        "DISPLAY=$DISPLAY"
        "XAUTHORITY=$AUTH"
        "HOME=$HOME"
        "WXB_HOME=$HOME"
        "WXB_OWNER_UID=$(id -u)"
        "WXB_ELEVATED=1"
        "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
        "XDG_STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}"
        "XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}"
        "XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}"
        "XDG_CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}"
        "XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-x11}"
        "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
        "WXB_PORTABLE=${WXB_PORTABLE:-}"
        "DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}"
        "LANG=${LANG:-C.UTF-8}"
    )
    ASKPASS="${WXB_ASKPASS:-$APP_DIR/wxb-askpass.sh}"
    HAVE_POLKIT=0
    command -v pkexec >/dev/null 2>&1 && HAVE_POLKIT=1
    HAVE_ASKPASS=0
    if command -v sudo >/dev/null 2>&1 && [ -x "$ASKPASS" ]; then
        HAVE_ASKPASS=1
    fi
    # polkit's own dialog is nicer, but it needs a running session agent;
    # without one pkexec fails silently, so prefer the graphical sudo prompt
    if [ "$HAVE_POLKIT" = 1 ] &&
       pgrep -f 'polkit.*authentication-agent' >/dev/null 2>&1; then
        PLAN=(pkexec env "${ENVIRON[@]}" "$SELF")
    elif [ "$HAVE_ASKPASS" = 1 ]; then
        export SUDO_ASKPASS="$ASKPASS"
        PLAN=(sudo -A env "${ENVIRON[@]}" "$SELF")
    elif [ "$HAVE_POLKIT" = 1 ]; then
        PLAN=(pkexec env "${ENVIRON[@]}" "$SELF")
    else
        tell "WXB wants full access but found no way to ask for it.

Install policykit-1 (it ships pkexec):
  sudo apt install policykit-1
Or start it unprivileged on purpose:
  WXB_NO_ELEVATE=1 ~/wxb/WXB.sh"
        exit 1
    fi
    # a dry run prints exactly what would be executed, without asking
    if [ "${WXB_DRY_RUN:-0}" = "1" ]; then
        printf '%q ' "${PLAN[@]}"
        printf '\n'
        exit 0
    fi
    "${PLAN[@]}"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        tell "WXB could not get full access (exit $rc).

You can still run it unprivileged — the input
group works there:
  WXB_NO_ELEVATE=1 ~/wxb/WXB.sh"
        exit "$rc"
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# running as root: keep the user's home so pynput --user and the ledger work
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    if [ -z "${WXB_HOME:-}" ] && [ -n "$OWNER_USER" ]; then
        WXB_HOME="$(getent passwd "$OWNER_USER" 2>/dev/null | cut -d: -f6)"
    fi
    if [ -n "${WXB_HOME:-}" ] && [ -d "$WXB_HOME" ]; then
        export HOME="$WXB_HOME"
    fi
    [ -n "$OWNER_UID" ] && export WXB_OWNER_UID="$OWNER_UID"
    # don't leave root-owned .pyc files in a user-writable app folder
    export PYTHONDONTWRITEBYTECODE=1
fi

# a log written by root must stay editable by the user afterwards
claim_log() {
    if [ -n "${OWNER_UID:-}" ] && [ "$(id -u)" -eq 0 ]; then
        touch "$LOG" 2>/dev/null && chown "$OWNER_UID" "$LOG" 2>/dev/null || true
        [ -f "$LOG.1" ] && chown "$OWNER_UID" "$LOG.1" 2>/dev/null || true
    fi
}
claim_log

# root's python only sees the owner's pip --user packages when HOME points at
# the user; add the matching user-site explicitly as a second chance
user_site_for() {
    local py="$1" ver dir
    [ -n "${WXB_HOME:-}" ] || return 1
    ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' \
        2>/dev/null)" || return 1
    [ -n "$ver" ] || return 1
    dir="$WXB_HOME/.local/lib/python$ver/site-packages"
    [ -d "$dir" ] && printf '%s\n' "$dir"
}

pick_python() {
    local cand site
    for cand in "${WXB_PYTHON:-python3}" "$APP_DIR/.venv/bin/python"; do
        if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
            if "$cand" -c "import pynput" >/dev/null 2>&1; then
                printf '%s|\n' "$cand"
                return 0
            fi
            site="$(user_site_for "$cand")" || site=""
            if [ -n "$site" ] &&
               PYTHONPATH="$site${PYTHONPATH:+:$PYTHONPATH}" \
                   "$cand" -c "import pynput" >/dev/null 2>&1; then
                printf '%s|%s\n' "$cand" "$site"
                return 0
            fi
        fi
    done
    return 1
}

# keep the log small enough to actually read
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 65536 ]; then
    mv -f "$LOG" "$LOG.1" 2>/dev/null || true
    claim_log
fi

CHOICE="$(pick_python)" || {
    tell "WXB needs python3 with the pynput package.

Install it, then click the icon again:
  python3 -m pip install --user --break-system-packages pynput"
    { echo; echo "=== $(date '+%F %T') no python3 with pynput ==="; } >>"$LOG"
    claim_log
    exit 1
}
PY="${CHOICE%%|*}"
SITE="${CHOICE#*|}"
if [ -n "$SITE" ] && [ "$SITE" != "$CHOICE" ]; then
    export PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}"
fi

{ echo; echo "=== $(date '+%F %T') launch with $PY (uid=$(id -u), owner=${OWNER_UID:-self})"; } >>"$LOG"
claim_log
"$PY" "$APP_DIR/wxb.py" >>"$LOG" 2>&1 &
PID=$!
sleep 2
if kill -0 "$PID" 2>/dev/null; then
    exit 0
fi

# it exited already: exit 0 means "WXB was already open, brought it forward"
wait "$PID"
code=$?
if [ "$code" -eq 0 ]; then
    exit 0
fi

tell "WXB failed to start (exit $code).

$(tail -n 8 "$LOG")"
exit 1
