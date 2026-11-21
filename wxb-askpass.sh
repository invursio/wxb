#!/bin/bash
# Graphical password prompt for `sudo -A`, used when pkexec is missing.
# sudo runs this, reads one line from stdout, and never shows a terminal.
# Cancelling prints nothing, which makes sudo fail instead of hanging.
set -u
if command -v zenity >/dev/null 2>&1; then
    zenity --password --title="WXB — full access" 2>/dev/null
    exit $?
fi
if command -v kdialog >/dev/null 2>&1; then
    kdialog --password "WXB needs full access (input devices)" \
        2>/dev/null
    exit $?
fi
echo "WXB needs zenity or kdialog for the password prompt" >&2
exit 1
