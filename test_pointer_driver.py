#!/usr/bin/env python3
"""Real-X11 pointer proof for WXB Studio (no WM; Xvfb + XTest)."""
import os, subprocess, time
from pathlib import Path
os.environ['GDK_DEBUG'] = 'gl-disable'
os.environ['GSK_RENDERER'] = 'cairo'
os.environ['WXB_STANDALONE'] = '1'
import wxb_test_isolation  # noqa: E402,F401
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import GLib, Graphene, Gtk
from wxb_studio import WxbApplication, WxbWindow

print(wxb_test_isolation.assert_isolated("pointer"))

LOG = Path('/tmp/wxb-pointer-proof.log')
LOG.write_text('')
events = []
last_press = [None]

def rec(line):
    events.append(line)
    LOG.write_text('\n'.join(events) + '\n')
    print(line, flush=True)

# App-side action logging, attached before the window is built.
_show = WxbWindow.show_page
def show_page(self, key):
    result = _show(self, key)
    rec('PAGE ' + str(key))
    return result
WxbWindow.show_page = show_page

_open = WxbWindow.open_command
def open_command(self):
    result = _open(self)
    rec('PALETTE_OPEN')
    return result
WxbWindow.open_command = open_command

_close = WxbWindow.close_command
def close_command(self):
    result = _close(self)
    rec('PALETTE_CLOSE')
    return result
WxbWindow.close_command = close_command

_fill = WxbWindow._fill_command
def fill_command(self):
    result = _fill(self)
    rows = sum(1 for row in self.cmd_list if getattr(row, '_command', None))
    rec('FILTER %r rows=%d' % (self.cmd_entry.get_text(), rows))
    return result
WxbWindow._fill_command = fill_command

_activate = WxbWindow._activate_command
def activate_command(self, lst, row):
    result = _activate(self, lst, row)
    rec('COMMAND_ROW')
    return result
WxbWindow._activate_command = activate_command

_toggle_clicker = WxbWindow._toggle_clicker
def toggle_clicker(self):
    result = _toggle_clicker(self)
    rec('CLICKER running=%s error=%r' % (bool(self.service.ac_running), self.service._ac_error))
    return result
WxbWindow._toggle_clicker = toggle_clicker

_scan = WxbWindow._scan_alts
def scan_alts(self):
    result = _scan(self)
    rec('ALTS_REFRESH')
    return result
WxbWindow._scan_alts = scan_alts

_set_accent = WxbWindow._set_accent
def set_accent(self, name):
    result = _set_accent(self, name)
    rec('ACCENT ' + str(name))
    return result
WxbWindow._set_accent = set_accent

app = WxbApplication(config_path=Path('/tmp/wxb-pointer.json'),
                     show_onboarding=False, auto_scan=False, hotkeys=False)
app.register(None)
app.activate()
win = app.window
win.set_default_size(1180, 780)
win.present()

ctx = GLib.MainContext.default()
def pump(seconds=0.25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)

def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(0.02)
    return bool(predicate())

pump(0.8)
wid = subprocess.check_output(
    ['xdotool', 'search', '--name', 'WXB Studio'], text=True).split()[0]
subprocess.run(['xdotool', 'windowfocus', wid], check=False,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
rec('WINDOW id=%s size=%dx%d' % (wid, win.get_width(), win.get_height()))

# Window-level capture controller is instrumentation only. It must see every
# press and supplies the exact local coordinates for coordinate verification.
probe = Gtk.GestureClick()
probe.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
def on_press(_g, n, x, y):
    last_press[0] = (float(x), float(y))
    rec('PRESS button=%d local=%.1f,%.1f' % (n, x, y))
probe.connect('pressed', on_press)
win.add_controller(probe)

# Calibrate the surface origin once, using a harmless header press.
last_press[0] = None
subprocess.run(['xdotool', 'mousemove', '20', '20', 'click', '1'],
               check=True, stdout=subprocess.DEVNULL)
pump(0.35)
if last_press[0] is None:
    raise SystemExit('FAIL calibration press never arrived')
cx, cy = last_press[0]
origin = (20.0 - cx, 20.0 - cy)
rec('CALIBRATED root_origin=%.1f,%.1f' % origin)
if not (-2 <= origin[0] <= 30 and -2 <= origin[1] <= 30):
    raise SystemExit('FAIL implausible surface origin %r' % (origin,))

def center(widget):
    if not widget.get_mapped() or widget.get_width() <= 0:
        pump(0.2)
    p = Graphene.Point().init(widget.get_width() / 2.0,
                             widget.get_height() / 2.0)
    ok, q = widget.compute_point(win, p)
    if not ok:
        raise AssertionError('cannot translate widget %r' % (widget,))
    return q.x, q.y

def click(widget, label, expected, park=False):
    lx, ly = center(widget)
    rx, ry = origin[0] + lx, origin[1] + ly
    before = len(events)
    last_press[0] = None
    subprocess.run(['xdotool', 'mousemove', str(round(rx)), str(round(ry)),
                    'click', '1'], check=True, stdout=subprocess.DEVNULL)
    # Capture the real user press before any clicker-generated press can
    # overwrite it.
    pump(0.06)
    pressed = last_press[0]
    if park:
        # Starting the clicker leaves the pointer on its own Start button
        # otherwise, causing a second generated click to toggle it off.
        subprocess.run(['xdotool', 'mousemove', '20', '20'], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pump(0.29)
    if pressed is None:
        raise AssertionError('%s: no press reached window at %r' % (label, (rx, ry)))
    px, py = pressed
    if abs(px - lx) > 3 or abs(py - ly) > 3:
        raise AssertionError('%s: coordinate mismatch expected %.1f,%.1f got %.1f,%.1f'
                             % (label, lx, ly, px, py))
    new = events[before:]
    if not any(e.startswith(expected) for e in new):
        raise AssertionError('%s: handler did not emit %s; got %r' %
                             (label, expected, new))
    rec('PASS %s local=%.1f,%.1f root=%.1f,%.1f' % (label, lx, ly, rx, ry))

try:
    # All four sidebar rows.
    for key in ('overview', 'automation', 'alts', 'settings'):
        click(win._nav[key], 'nav/' + key, 'PAGE ' + key)
        if win.stack.get_visible_child_name() != key:
            raise AssertionError('nav/%s did not switch page' % key)
        rec('PASS stack page=%s' % key)

    # Sidebar geometry at the wide layout.
    sw = win.split.get_sidebar().get_width()
    rec('SIDEBAR_WIDTH %d' % sw)
    if sw != 310:
        raise AssertionError('sidebar width is not the intended 310: %d' % sw)

    # Common controls: search opens palette, close button closes it.
    click(win.search_btn, 'search/open', 'PALETTE_OPEN')
    if not win.cmd_revealer.get_reveal_child():
        raise AssertionError('palette did not open')
    click(win.cmd_close, 'palette/close', 'PALETTE_CLOSE')
    if win.cmd_revealer.get_reveal_child():
        raise AssertionError('palette did not close')

    # Reopen, type a filter, then click the filtered command row.
    click(win.search_btn, 'search/reopen', 'PALETTE_OPEN')
    win.cmd_entry.grab_focus()
    pump(0.1)
    subprocess.run(['xdotool', 'type', '--delay', '25', 'cordial'], check=True)
    pump(0.5)
    if not any(e.startswith("FILTER 'cordial' rows=1") for e in events):
        raise AssertionError('search typing did not filter to one row: %r' %
                             [e for e in events if e.startswith('FILTER')])
    row = win.cmd_list.get_row_at_index(0)
    if row is None or not hasattr(row, '_command'):
        raise AssertionError('no command result row after filtering')
    click(row, 'palette/result-cordial', 'PAGE alts')
    if win.stack.get_visible_child_name() != 'alts':
        raise AssertionError('command result did not switch to Cordial')

    # Page actions: overview clicker, automation clicker, alts refresh,
    # settings accent. Each is a real button press.  Use a 1 CPS engine so
    # its next generated click cannot land before the stop press.
    win.ui["ac_cps"].set_value(1)
    win.ui["ac_duty"].set_value(1)
    win.ui["ac_button_choice"].set_selected(1)
    click(win._nav['overview'], 'nav/overview-again', 'PAGE overview')
    click(win.hero_click, 'overview/start-clicker', 'CLICKER running=True',
          park=True)
    if not win.service.ac_running:
        raise AssertionError('overview clicker did not start')
    click(win.hero_click, 'overview/stop-clicker', 'CLICKER')
    if not wait_until(lambda: not win.service.ac_running):
        raise AssertionError('overview clicker did not stop after the click')
    click(win._nav['automation'], 'nav/automation-again', 'PAGE automation')
    click(win.ui['ac_start'], 'automation/clicker', 'CLICKER running=True',
          park=True)
    if not win.service.ac_running:
        raise AssertionError('automation clicker did not start')
    click(win.ui['ac_start'], 'automation/clicker-stop', 'CLICKER')
    if not wait_until(lambda: not win.service.ac_running):
        raise AssertionError('automation clicker did not stop after the click')
    click(win._nav['alts'], 'nav/alts-again', 'PAGE alts')
    click(win.ui['alts_refresh'], 'alts/refresh', 'ALTS_REFRESH')
    click(win._nav['settings'], 'nav/settings-again', 'PAGE settings')
    click(win.ui['accent_buttons'][1], 'settings/accent', 'ACCENT')

    # Keyboard regression: CtrlK opens, Esc closes, Ctrl2 moves page.
    subprocess.run(['xdotool', 'windowfocus', wid], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['xdotool', 'key', 'ctrl+k'], check=True)
    pump(0.3)
    if not win.cmd_revealer.get_reveal_child():
        raise AssertionError('Ctrl K did not open palette')
    rec('PASS keyboard/ctrl-k')
    subprocess.run(['xdotool', 'key', 'Escape'], check=True)
    pump(0.3)
    if win.cmd_revealer.get_reveal_child():
        raise AssertionError('Escape did not close palette')
    rec('PASS keyboard/escape')
    subprocess.run(['xdotool', 'key', 'ctrl+2'], check=True)
    pump(0.3)
    if win.stack.get_visible_child_name() != 'automation':
        raise AssertionError('Ctrl 2 did not select automation')
    rec('PASS keyboard/ctrl-2')

    # Responsive layout after the shell change.
    win.set_default_size(900, 700)
    pump(1.5)
    sw2 = win.split.get_sidebar().get_width()
    rec('RESPONSIVE size=%dx%d collapsed=%s sidebar_visible=%s sidebar_width=%d'
        % (win.get_width(), win.get_height(), win.split.get_collapsed(),
           win.split.get_show_sidebar(), sw2))
    if win.get_width() > 900 or not win.split.get_collapsed():
        raise AssertionError('900px responsive layout did not collapse')
    if win.split.get_show_sidebar():
        raise AssertionError('sidebar still shown at 900px')
    rec('PASS responsive/900')
    win.set_default_size(1180, 780)
    pump(1.2)
    rec('PASS restore-wide')

    # Final real keyboard quit check after the page and pointer work.
    subprocess.run(['xdotool', 'windowfocus', wid], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['xdotool', 'key', 'ctrl+q'], check=True)
    pump(0.5)
    if not getattr(win, '_closed', False):
        raise AssertionError('Ctrl Q did not close the window')
    rec('PASS keyboard/ctrl-q')
finally:
    win.close()
    app.quit()

rec('ALL_POINTER_CHECKS_PASSED')
