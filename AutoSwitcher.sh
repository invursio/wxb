#!/bin/bash
# Launches the standalone rotation switcher (autoswitcher.py).
#
# The 'keyboard' library needs raw /dev/input access for its global hotkey:
#   * if you are in the 'input' group it runs as you (no password), else
#   * it re-runs itself elevated, keeping your user-site packages visible.
cd "$(dirname "$0")" || exit 1
LOG="$PWD/autoswitcher.log"
SITE="$(python3 -c 'import site; print(site.USER_SITE)' 2>/dev/null)"

note() {
    command -v zenity >/dev/null 2>&1 && \
        zenity --error --width=520 --title="AutoSwitcher" --text="$1"
}

if ! python3 -c "import keyboard, pynput" >/dev/null 2>&1; then
    note "AutoSwitcher needs python3 with the keyboard and pynput packages.

Install them, then click the icon again:
  python3 -m pip install --user --break-system-packages keyboard pynput"
    exit 1
fi

{ echo; echo "=== $(date '+%F %T') launch ==="; } >>"$LOG"
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    exec python3 autoswitcher.py 2>&1 | tee -a "$LOG"
else
    exec sudo env PYTHONPATH="$SITE" python3 autoswitcher.py 2>&1 | tee -a "$LOG"
fi
