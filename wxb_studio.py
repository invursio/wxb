#!/usr/bin/env python3
"""WXB Studio — the fresh GTK4/libadwaita surface for WXB.

This module owns presentation only: settings, engines and launchers live in
:mod:`wxb_studio_service`.

Design language: deep navy glass, one pale-cyan signal, generous negative
space, a floating sidebar and a faint orbital drawing.  Inter Display handles
the headlines, a serif wordmark gives the shell a Pentagram-like signature,
and JetBrains Mono keeps machine facts legible.  The references are
deliberately mixed: Linear's command surface, Apple's product calm,
Duolingo's short onboarding, Airbnb's scannable rows and filters, Aesop's
restrained art direction, Notion's dashboard grouping, Stripe's clear primary
action and Framer's small transitions.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess

import cairo
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import (  # noqa: E402
    Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango,
)

from wxb_studio_service import (  # noqa: E402
    ACCENTS,
    ALT_TILING_CHOICES,
    CLICKER_CPS_MAX,
    CLICKER_CPS_MIN,
    CLICKER_DUTY_MAX,
    CLICKER_DUTY_MIN,
    CLICKER_DURATION_MAX,
    CLICKER_JITTER_MAX,
    foreground_app_name,
    HOTKEY_DEFAULTS,
    HOTKEY_LABELS,
    STUDIO_VERSION,
    StudioService,
    normalize_hotkey,
    portable_status,
    x11_capabilities,
    x11_configure_window,
    x11_set_window_above,
    x11_window_geometry,
)
from wxb_recorder import SOURCE_CHOICES  # noqa: E402
import wxb_input_lab as input_lab  # noqa: E402
from wxb_overlay import InputOverlayWindow  # noqa: E402


APP_ID = "io.github.wxb.Studio"
CSS_PATH = Path(__file__).with_name("wxb_studio.css")
MONO_CSS_PATH = Path(__file__).with_name("wxb_mono.css")
ICONS_PATH = Path(__file__).with_name("icons")
PRIVACY_MASK = "•••"
PRIVACY_MASK_KEY = "key •"
LEGACY_RECORDINGS_DIR = Path.home() / "Videos" / "WXB-root-owned-20261115"

# AppImage launchers leak their private runtime into child processes.  A
# file manager started with that library path can hang before mapping its
# window, so strip launcher-specific variables and AppImage path entries.
_DESKTOP_CHILD_STRIP = (
    "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME",
    "APPDIR", "APPIMAGE", "ARGV0", "OWD", "GSETTINGS_SCHEMA_DIR",
    "GIO_MODULE_DIR", "GIO_LAUNCHED_DESKTOP_FILE",
    "GIO_LAUNCHED_DESKTOP_FILE_PID",
)


def _desktop_child_env() -> dict[str, str]:
    env = dict(os.environ)
    appdir = env.get("APPDIR", "").rstrip("/")
    for key in _DESKTOP_CHILD_STRIP:
        env.pop(key, None)
    for key in ("PATH", "XDG_DATA_DIRS"):
        value = env.get(key, "")
        entries = [
            item for item in value.split(":")
            if item.startswith("/")
            and not (appdir and
                     (item == appdir or item.startswith(appdir + "/")))
        ]
        if entries:
            env[key] = ":".join(dict.fromkeys(entries))
        else:
            env.pop(key, None)
    return env


def _launch_desktop_uri(uri: str) -> bool:
    """Hand a folder URI to the system opener with AppImage leaks removed."""
    opener = shutil.which("xdg-open", path="/usr/local/bin:/usr/bin:/bin")
    if not opener:
        return False
    try:
        subprocess.Popen(
            [opener, uri], env=_desktop_child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def fmt_clicker(value) -> str:
    """Compact display for fractional CPS/CDC values."""
    text = "%.2f" % float(value)
    return text.rstrip("0").rstrip(".") or "0"


def display_hotkey(value) -> str:
    canonical = normalize_hotkey(value)
    if not canonical:
        return ""
    names = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "cmd": "Cmd",
             "mouseleft": "Mouse left", "mouseright": "Mouse right",
             "mousemiddle": "Mouse middle", "mouse4": "Mouse 4",
             "mouse5": "Mouse 5"}
    parts = canonical.split("+")
    return "+".join(names.get(p, p.upper() if len(p) <= 2 else p.title())
                    for p in parts)


def hotkey_from_event(keyval, state) -> str:
    """Canonical binding from a GTK key event, for the capture dialog.

    Modifier-only presses return an empty string so holding Ctrl does not
    finish the capture before the actual key arrives.
    """
    modifier_keys = {
        Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L,
        Gdk.KEY_Shift_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
        Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
        Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
    }
    if keyval in modifier_keys:
        return ""
    key_name = Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or ""
    aliases = {
        "return": "enter", "escape": "esc", "space": "space",
        "page_up": "pageup", "page_down": "pagedown",
    }
    key = aliases.get(key_name.lower(), key_name.lower())
    if not key:
        return ""
    mods = []
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods.append("ctrl")
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods.append("shift")
    if state & Gdk.ModifierType.ALT_MASK:
        mods.append("alt")
    if state & Gdk.ModifierType.SUPER_MASK:
        mods.append("cmd")
    return normalize_hotkey("+".join(mods + [key]))


def _css_balanced(text: str) -> bool:
    """Cheap structural guard before handing CSS to GTK.

    GTK logs parser warnings and still returns a provider for some malformed
    input.  Rejecting an unbalanced block here keeps a damaged stylesheet out
    of stderr and leaves the default theme in place.
    """
    depth = 0
    quote = None
    escaped = False
    # Comments are not CSS syntax; ignoring them keeps an apostrophe in prose
    # from being mistaken for an unterminated string literal.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and quote is None


# ---------------------------------------------------------------------------
# small construction helpers
# ---------------------------------------------------------------------------
def el(*children, vertical=True, spacing=0, css=None, hexpand=False,
       vexpand=False, halign=None, valign=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical
                  else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if css:
        for name in css.split():
            box.add_css_class(name)
    box.set_hexpand(hexpand)
    box.set_vexpand(vexpand)
    if halign is not None:
        box.set_halign(halign)
    if valign is not None:
        box.set_valign(valign)
    for child in children:
        if child is not None:
            box.append(child)
    return box


def label(text, css=None, xalign=0.0, wrap=False, max_width=None,
          selectable=False, ellipsize=None):
    w = Gtk.Label(label=text)
    w.set_xalign(xalign)
    if css:
        for name in css.split():
            w.add_css_class(name)
    if wrap:
        w.set_wrap(True)
        w.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if max_width:
        w.set_max_width_chars(max_width)
    if selectable:
        w.set_selectable(True)
    if ellipsize:
        w.set_ellipsize(ellipsize)
    return w


def icon(name, size=16, css=None):
    w = Gtk.Image.new_from_icon_name(name)
    w.set_pixel_size(size)
    if css:
        for item in css.split():
            w.add_css_class(item)
    return w


def button(text=None, icon_name=None, css="wxb-ghost", tooltip=None,
           on_click=None):
    b = Gtk.Button()
    if text and icon_name:
        b.set_child(el(icon(icon_name, 14), label(text, "wxb-button-label"),
                       vertical=False, spacing=7))
    elif text:
        b.set_label(text)
    elif icon_name:
        b.set_child(icon(icon_name, 16))
    for name in (css or "").split():
        if name:
            b.add_css_class(name)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if on_click:
        b.connect("clicked", lambda *_: on_click())
    return b


def switch():
    """One switch shape everywhere: intrinsic height, never row-stretched."""
    w = Gtk.Switch()
    w.set_halign(Gtk.Align.START)
    w.set_valign(Gtk.Align.CENTER)
    w.set_hexpand(False)
    w.add_css_class("wxb-switch")
    return w


def card(*children, css="wxb-card", spacing=16, hexpand=True):
    box = el(*children, vertical=True, spacing=spacing,
             css=css, hexpand=hexpand)
    return box


def dot(css="wxb-dot"):
    w = Gtk.Box()
    w.set_size_request(7, 7)
    w.set_halign(Gtk.Align.CENTER)
    w.set_valign(Gtk.Align.CENTER)
    for name in css.split():
        w.add_css_class(name)
    return w


def clear(box):
    while True:
        child = box.get_first_child()
        if child is None:
            break
        box.remove(child)


def set_pill(widget, text, state=""):
    widget.set_text(text)
    for name in ("live", "good", "bad"):
        widget.remove_css_class(name)
    if state:
        widget.add_css_class(state)


def css_swap(widget, add=(), remove=()):
    for name in remove:
        widget.remove_css_class(name)
    for name in add:
        widget.add_css_class(name)


def action_button_text(widget, text, icon_name=None):
    """Update a button without losing its icon.

    Gtk.Button.set_label() replaces the child, which silently stripped every
    icon on the first refresh.  Remember the last text so a one-second tick
    does not rebuild the child over and over.
    """
    if getattr(widget, "_wxb_text", None) == text:
        return
    widget._wxb_text = text
    if icon_name:
        widget.set_child(el(icon(icon_name, 14), label(text), vertical=False,
                            spacing=7))
    else:
        widget.set_label(text)


# ---------------------------------------------------------------------------
# art
# ---------------------------------------------------------------------------
class OrbitArt(Gtk.DrawingArea):
    """Quiet orbital mark: concentric lines, one cool signal.

    Drawn with Cairo rather than shipped as an image so it inherits the
    accent and stays crisp at any scale.
    """

    def __init__(self, accent="#9fcaff", size=300):
        super().__init__()
        self.accent = accent
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def _rgba(self, value, alpha=1.0):
        rgba = Gdk.RGBA()
        if not rgba.parse(value):
            rgba.parse("#9fcaff")
        rgba.alpha = alpha
        # Cairo takes four floats; Gdk.RGBA is not iterable.
        return (rgba.red, rgba.green, rgba.blue, rgba.alpha)

    def _draw(self, _area, cr, width, height):
        cx, cy = width / 2.0, height / 2.0
        radius = min(width, height) * 0.40
        cr.set_line_width(1.0)

        for i, scale in enumerate((0.40, 0.62, 0.84, 1.0)):
            r = radius * scale
            cr.set_source_rgba(*self._rgba("#f2efe9", 0.09 + i * 0.018))
            cr.arc(cx, cy, r, 0, math.tau)
            cr.stroke()

        cr.set_source_rgba(*self._rgba(self.accent, 0.85))
        cr.set_line_width(1.4)
        cr.arc(cx, cy, radius * 0.84, -math.pi * 0.12, math.pi * 0.55)
        cr.stroke()

        cr.set_source_rgba(*self._rgba(self.accent, 1.0))
        cr.arc(cx + radius * 0.84, cy, 3.0, 0, math.tau)
        cr.fill()

        cr.set_source_rgba(*self._rgba("#f2efe9", 0.8))
        cr.arc(cx, cy, 2.2, 0, math.tau)
        cr.fill()

        cr.set_source_rgba(*self._rgba("#f2efe9", 0.18))
        cr.set_line_width(0.8)
        for angle in (0.45, 2.1, 3.9, 5.3):
            x = cx + math.cos(angle) * radius * 1.04
            y = cy + math.sin(angle) * radius * 1.04
            cr.arc(x, y, 1.6, 0, math.tau)
            cr.stroke()


class SidebarShell(Gtk.Box):
    """A plain, input-safe sidebar + page pane.

    Adw.OverlaySplitView paints correctly but in this GTK/libadwaita build its
    sidebar presses are routed through the content pane on X11.  A regular box
    keeps the same wide layout without an overlay input layer.
    """

    def __init__(self, sidebar, content):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.sidebar = sidebar
        self.content = content
        self._collapsed = False
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.pane_rule = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pane_rule.add_css_class("wxb-pane-rule")
        self.pane_rule.set_size_request(2, -1)
        self.pane_rule.set_margin_top(26)
        self.pane_rule.set_margin_bottom(26)
        self.pane_rule.set_margin_start(6)
        self.pane_rule.set_margin_end(8)
        self.append(sidebar)
        self.append(self.pane_rule)
        self.append(content)

    def get_sidebar(self):
        return self.sidebar

    def get_content(self):
        return self.content

    def set_collapsed(self, collapsed):
        self._collapsed = bool(collapsed)
        if self._collapsed:
            self.sidebar.set_visible(False)
            self.pane_rule.set_visible(False)
        else:
            self.pane_rule.set_visible(self.sidebar.get_visible())

    def get_collapsed(self):
        return self._collapsed

    def set_show_sidebar(self, show):
        show = bool(show)
        self.sidebar.set_visible(show)
        self.pane_rule.set_visible(show)

    def get_show_sidebar(self):
        return self.sidebar.get_visible()

    def set_min_sidebar_width(self, _width):
        pass

    def set_max_sidebar_width(self, _width):
        pass

    def set_pin_sidebar(self, _pin):
        pass


class HotkeyCaptureWindow(Adw.Window):
    """Small modal that turns one real key press into a binding."""

    def __init__(self, parent, action_label, on_capture):
        super().__init__()
        self.add_css_class("wxb-secondary")
        self.add_css_class("wxb-hotkey-window")
        self.set_modal(True)
        self.set_transient_for(parent)
        self.set_title("Set hotkey")
        self.set_default_size(420, 270)
        self.set_resizable(False)
        self._on_capture = on_capture
        self._closed = False

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.add_css_class("wxb-root")
        body.add_css_class("wxb-hotkey-body")
        body.set_margin_top(22)
        body.set_margin_bottom(22)
        body.set_margin_start(24)
        body.set_margin_end(24)
        body.append(label("PRESS THE NEW HOTKEY", "wxb-eyebrow"))
        body.append(label(action_label, "wxb-section-title", wrap=True))
        self.combo = label("waiting…", "wxb-mono wxb-hotkey-wait")
        body.append(self.combo)
        body.append(label("Press a key combination or choose a mouse button "
                          "· Esc cancels", "wxb-hint"))
        body.append(el(
            button("Left", css="wxb-quiet",
                   on_click=lambda: self._choose_mouse("mouseleft")),
            button("Right", css="wxb-quiet",
                   on_click=lambda: self._choose_mouse("mouseright")),
            button("Middle", css="wxb-quiet",
                   on_click=lambda: self._choose_mouse("mousemiddle")),
            button("Mouse 4", css="wxb-quiet",
                   on_click=lambda: self._choose_mouse("mouse4")),
            button("Mouse 5", css="wxb-quiet",
                   on_click=lambda: self._choose_mouse("mouse5")),
            vertical=False, spacing=6))
        cancel = button("Cancel", css="wxb-quiet", on_click=self.close)
        cancel.set_halign(Gtk.Align.END)
        body.append(cancel)
        self.set_content(body)

        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key)
        controller.connect("key-released", self._on_release)
        self.add_controller(controller)
        mouse_controller = Gtk.GestureClick()
        mouse_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        mouse_controller.connect("pressed", self._on_mouse_pressed)
        self.add_controller(mouse_controller)
        self._pressed = set()
        self.connect("close-request", self._on_close)

    def _on_key(self, _controller, keyval, _keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        canonical = hotkey_from_event(keyval, state)
        if canonical:
            self._pressed.add(canonical)
            self.combo.set_text(display_hotkey(canonical))
            self._finish(canonical)
        return True

    def _on_release(self, _controller, _keyval, _keycode, _state):
        return False

    def _on_mouse_pressed(self, gesture, _n_press, _x, _y):
        canonical = {2: "mousemiddle", 8: "mouse4", 9: "mouse5"}.get(
            gesture.get_current_button())
        if not canonical:
            return False
        self.combo.set_text(display_hotkey(canonical))
        self._finish(canonical)
        return True

    def _choose_mouse(self, canonical):
        self.combo.set_text(display_hotkey(canonical))
        self._finish(canonical)

    def _finish(self, canonical):
        if self._closed:
            return
        self._closed = True
        self._on_capture(canonical)
        self.close()

    def _on_close(self, *_):
        self._closed = True
        return False


# ---------------------------------------------------------------------------
# onboarding
# ---------------------------------------------------------------------------
class OnboardingWindow(Adw.Window):
    STEPS = (
        ("A control room, not a control panel.",
         "WXB keeps the powerful parts close and the noise out of the way. "
         "One surface for clicking, rotating slots and cleaning opposite inputs.",
         "system-run-symbolic"),
        ("Three engines. One calm surface.",
         "Start and stop each engine independently, or use Stop all when a "
         "session is over. Nothing runs until you ask it to.",
         "input-mouse-symbolic"),
        ("Yours in a click.",
         "Sober, Cordial alts and every hotkey stay within reach. "
         "Use Ctrl K whenever you would rather type than hunt.",
         "system-search-symbolic"),
    )

    def __init__(self, parent, accent, on_done):
        super().__init__()
        self.add_css_class("wxb-secondary")
        self.add_css_class("wxb-onboard-window")
        self.set_modal(True)
        self.set_transient_for(parent)
        self.set_default_size(780, 520)
        self.set_resizable(False)
        self.set_deletable(False)
        self._on_done = on_done
        self._step = 0
        self._accent = accent
        self._build()

    def _build(self):
        shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        shell.add_css_class("wxb-root")

        art = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        art.set_size_request(310, -1)
        art.add_css_class("wxb-onboard-art")
        art.append(el(label("WXB", "wxb-side-title"),
                      label("STUDIO / FIRST RUN", "wxb-side-kicker"),
                      vertical=True, spacing=4,
                      css="wxb-onboard-art-head", hexpand=True))
        orbit = OrbitArt(self._accent, 260)
        orbit.set_hexpand(True)
        orbit.set_vexpand(True)
        orbit.set_halign(Gtk.Align.CENTER)
        orbit.set_valign(Gtk.Align.CENTER)
        art.append(orbit)
        foot = el(label("A small tool for long sessions.",
                        "wxb-onboard-body"),
                  label("LINUX · X11 · 2026", "wxb-version"),
                  vertical=True, spacing=6, css="wxb-onboard-art-foot")
        foot.set_margin_start(30)
        foot.set_margin_end(30)
        foot.set_margin_bottom(28)
        art.append(foot)
        art.get_first_child().set_margin_top(30)
        art.get_first_child().set_margin_start(30)
        art.get_first_child().set_margin_end(30)
        shell.append(art)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.add_css_class("wxb-onboard-pane")
        right.set_hexpand(True)
        right.append(el(label("WELCOME", "wxb-eyebrow"),
                        vertical=True, spacing=0,
                        css="wxb-onboard-kicker"))
        right.get_first_child().set_margin_top(38)
        right.get_first_child().set_margin_start(38)
        right.get_first_child().set_margin_end(38)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(260)
        self.stack.set_vexpand(True)
        for title, body, icon_name in self.STEPS:
            step = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            step.set_margin_top(28)
            step.set_margin_start(38)
            step.set_margin_end(38)
            step.append(icon(icon_name, 24, "wxb-accent"))
            step.append(label(title, "wxb-onboard-title", wrap=True,
                              max_width=30))
            step.append(label(body, "wxb-onboard-body", wrap=True,
                              max_width=48))
            self.stack.add_named(step, "step-%d" % len(self.stack.get_pages()))
        right.append(self.stack)

        self.step_label = label("01 / 03", "wxb-onboard-step")
        self.dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              spacing=10)
        self.footer.set_margin_top(20)
        self.footer.set_margin_bottom(30)
        self.footer.set_margin_start(38)
        self.footer.set_margin_end(38)
        self.back = button("Back", css="wxb-quiet", on_click=self._back)
        self.skip = button("Skip", css="wxb-quiet", on_click=self._finish)
        self.next = button("Continue", icon_name="go-next-symbolic",
                           css="wxb-primary", on_click=self._next)
        self.footer.append(self.step_label)
        self.footer.append(self.dots)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.footer.append(spacer)
        self.footer.append(self.back)
        self.footer.append(self.skip)
        self.footer.append(self.next)
        right.append(self.footer)
        shell.append(right)
        self.set_content(shell)
        self._render()

    def _render(self):
        self.stack.set_visible_child_name("step-%d" % self._step)
        self.step_label.set_text("%02d / %02d" % (self._step + 1,
                                                 len(self.STEPS)))
        clear(self.dots)
        for i in range(len(self.STEPS)):
            d = dot("wxb-onboard-dot" + (" active" if i == self._step else ""))
            self.dots.append(d)
        self.back.set_visible(self._step > 0)
        self.next.set_label("Enter studio" if self._step == len(self.STEPS) - 1
                            else "Continue")
        if self._step == len(self.STEPS) - 1:
            self.next.set_child(el(icon("go-next-symbolic", 14),
                                   label("Enter studio"), vertical=False,
                                   spacing=7))
        else:
            self.next.set_child(el(label("Continue"),
                                   icon("go-next-symbolic", 14),
                                   vertical=False, spacing=7))

    def _next(self):
        if self._step >= len(self.STEPS) - 1:
            self._finish()
            return
        self._step += 1
        self._render()

    def _back(self):
        if self._step > 0:
            self._step -= 1
            self._render()

    def _finish(self):
        self.close()
        self._on_done()


# ---------------------------------------------------------------------------
# backdrop
# ---------------------------------------------------------------------------
class NexusBackdrop(Gtk.DrawingArea):
    """The reference's wallpaper, translated into a quiet procedural wash.

    Nexus sits over a lilac anime wallpaper with butterflies.  WXB does not
    bundle that photograph; this paints the same kind of light instead — a
    violet bloom, a cool blue undertow and a few translucent wing silhouettes
    that show through the two smoked-glass panes.
    """

    def __init__(self):
        super().__init__()
        self._surface = None
        self._surface_size = None
        self.set_draw_func(self._draw)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.set_can_target(False)
        self.add_css_class("wxb-backdrop")

    @staticmethod
    def _glow(cr, cx, cy, radius, color, strength):
        red, green, blue = color
        pattern = cairo.RadialGradient(cx, cy, 1.0, cx, cy, max(1.0, radius))
        pattern.add_color_stop_rgba(0.0, red, green, blue, strength)
        pattern.add_color_stop_rgba(0.46, red, green, blue, strength * 0.32)
        pattern.add_color_stop_rgba(1.0, red, green, blue, 0.0)
        cr.set_source(pattern)
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.fill()

    @staticmethod
    def _wing(cr, size, color, alpha):
        red, green, blue = color
        cr.set_source_rgba(red, green, blue, alpha)
        cr.set_line_width(max(0.8, size * 0.004))
        # Upper and lower wings, mirrored around the body.
        for side in (-1.0, 1.0):
            cr.move_to(0, 0)
            cr.curve_to(side * size * 0.12, -size * 0.21,
                        side * size * 0.52, -size * 0.40,
                        side * size * 0.60, -size * 0.05)
            cr.curve_to(side * size * 0.64, size * 0.16,
                        side * size * 0.27, size * 0.17,
                        0, 0)
            cr.close_path()
            cr.fill_preserve()
            cr.stroke()
            cr.move_to(0, 0)
            cr.curve_to(side * size * 0.14, size * 0.15,
                        side * size * 0.42, size * 0.42,
                        side * size * 0.43, size * 0.14)
            cr.curve_to(side * size * 0.44, size * 0.03,
                        side * size * 0.20, -size * 0.02,
                        0, 0)
            cr.close_path()
            cr.fill()
        cr.set_source_rgba(red, green, blue, alpha * 0.8)
        cr.set_line_width(max(1.0, size * 0.010))
        cr.move_to(0, -size * 0.31)
        cr.line_to(0, size * 0.33)
        cr.stroke()

    def _draw(self, _area, cr, width, height):
        if width <= 1 or height <= 1:
            return
        width, height = int(width), int(height)
        if self._surface is None or self._surface_size != (width, height):
            self._surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, width, height)
            self._paint(cairo.Context(self._surface), width, height)
            self._surface.flush()
            self._surface_size = (width, height)
        cr.set_source_surface(self._surface, 0, 0)
        cr.paint()

    def _paint(self, cr, width, height):
        base = cairo.LinearGradient(0, height, width, 0)
        base.add_color_stop_rgb(0.0, 0.020, 0.020, 0.055)
        base.add_color_stop_rgb(0.45, 0.035, 0.043, 0.105)
        base.add_color_stop_rgb(0.78, 0.070, 0.047, 0.145)
        base.add_color_stop_rgb(1.0, 0.045, 0.030, 0.095)
        cr.set_source(base)
        cr.paint()

        # Large soft light sources: enough colour to read through the glass,
        # never enough to compete with the labels.
        self._glow(cr, width * 0.87, height * 0.20, width * 0.38,
                   (0.48, 0.29, 0.96), 0.50)
        self._glow(cr, width * 0.69, height * 0.47, width * 0.34,
                   (0.24, 0.39, 0.82), 0.30)
        self._glow(cr, width * 0.10, height * 0.91, width * 0.31,
                   (0.36, 0.22, 0.76), 0.34)
        self._glow(cr, width * 0.92, height * 0.70, width * 0.23,
                   (0.66, 0.30, 0.68), 0.18)

        # A pair of oversized wing silhouettes tucked behind the main pane.
        cr.save()
        cr.translate(width * 0.87, height * 0.36)
        cr.rotate(-0.34)
        self._wing(cr, min(width, height) * 0.74,
                   (0.72, 0.54, 1.0), 0.075)
        cr.restore()
        cr.save()
        cr.translate(width * 0.62, height * 0.20)
        cr.rotate(0.44)
        self._wing(cr, min(width, height) * 0.28,
                   (0.62, 0.42, 0.96), 0.06)
        cr.restore()

        # Sparse dust motes.  Deterministic placement, no animation loop.
        cr.set_source_rgba(0.78, 0.74, 1.0, 0.20)
        for xf, yf, size in ((0.80, 0.12, 1.2), (0.91, 0.31, 1.0),
                             (0.73, 0.63, 1.6), (0.95, 0.55, 0.9),
                             (0.68, 0.84, 1.1), (0.18, 0.22, 0.8)):
            cr.arc(width * xf, height * yf, size, 0, math.tau)
            cr.fill()


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------
class WxbWindow(Adw.ApplicationWindow):
    PAGES = (
        ("overview", "Overview", "view-grid-symbolic", "1"),
        ("automation", "Automation", "input-mouse-symbolic", "2"),
        ("alts", "Cordial", "view-list-symbolic", "3"),
        ("settings", "Settings", "emblem-system-symbolic", "4"),
        ("macros", "Macros", "media-record-symbolic", "5"),
        ("recorder", "Recorder", "camera-video-symbolic", "6"),
        ("gamemode", "GameMode", "applications-games-symbolic", "7"),
        ("rotation", "AutoSwitcher", "view-refresh-symbolic", ""),
        ("socd", "SOCD Cleaner", "input-keyboard-symbolic", ""),
    )

    def __init__(self, app, service: StudioService, startup_page="overview",
                 show_onboarding=True, on_quit=None, auto_scan=True):
        super().__init__(application=app)
        self.app = app
        self.service = service
        self.on_quit = on_quit
        self._closed = False
        self._refresh_pending = False
        self._sober_running_seen = None
        self.privacy = False
        self._privacy_texts = {}
        self._privacy_spin_values = {}
        self._capture_window = None
        self._syncing_above = False
        self._alt_sig = None
        self._alt_style_syncing = False
        self._alt_options_syncing = False
        self._syncing_gamemode = False
        self._page_key = None
        self._nav = {}
        self._page_scrolls = {}
        self._anchor_cards = {}
        self._reflow_groups = []
        self._layout_sig = None
        self._lab_syncing = False
        self._lab_mappings = []
        self._overlay = None
        self._window_state_restored = False
        self._overlay_unsub = self.service.add_visualizer_listener(
            self._on_visualizer_event)
        self.ui = {}
        self.set_title("WXB Studio")
        window_settings = self.service.settings.get("window", {})
        self.set_default_size(int(window_settings.get("width", 1180)),
                              int(window_settings.get("height", 780)))
        self.set_size_request(900, 620)
        self.add_css_class("wxb")
        self._build_shell()
        self._build_pages()
        self._install_breakpoints()
        self._build_command()
        self._wire_keys()
        self.show_page(startup_page or "overview")
        self.set_opacity(
            int(self.service.settings.get("opacity", 92)) / 100.0)
        self._syncing_mono = False
        self._apply_monochrome_controls(
            bool(self.service.settings.get("monochrome", False)))
        self.connect("close-request", self._on_close)
        # A window can be presented at 900px before the 120 ms watchdog runs;
        # map is the first moment the real allocation is known.
        self.connect("map", lambda *_: self._responsive())
        self.connect("map", lambda *_: GLib.timeout_add(
            250, self._restore_window_state))
        self.connect("map", lambda *_: GLib.timeout_add(
            80, self._clear_initial_nav_focus))
        self._initial_nav_focus_done = False
        self._refresh()
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add(120, self._responsive)
        GLib.idle_add(self._sync_input_lab_visualizers)
        if auto_scan:
            GLib.timeout_add(350, self._initial_scan)
        if show_onboarding and not self.service.settings.get("onboarded"):
            GLib.idle_add(self._show_onboarding)

    # -- shell -------------------------------------------------------------
    def _build_shell(self):
        header = Adw.HeaderBar()
        header.add_css_class("wxb-header")
        header.set_show_title(False)
        self.header_kicker = label("STUDIO", "wxb-header-kicker")
        brand = el(self.header_kicker,
                   vertical=False, spacing=7, css="wxb-brand")
        self.header_page = label("Overview", "wxb-header-page")
        brand.append(label("/", "wxb-slash"))
        brand.append(self.header_page)
        self.side_btn = button(icon_name="view-list-symbolic",
                               css="wxb-icon", tooltip="Show navigation",
                               on_click=self._toggle_sidebar)
        self.side_btn.set_visible(False)
        self.side_btn.set_margin_end(3)
        # Keep the toggle inside the brand row.  Packed as a separate start
        # child it was laid out underneath the wordmark at narrow widths.
        brand.prepend(self.side_btn)
        header.pack_start(brand)

        self.menu_btn = Gtk.MenuButton()
        self.menu_btn.set_icon_name("open-menu-symbolic")
        self.menu_btn.add_css_class("wxb-icon")
        menu = Gio.Menu()
        menu.append("Keyboard shortcuts", "app.shortcuts")
        menu.append("About WXB", "app.about")
        menu.append("Quit", "app.quit")
        self.menu_btn.set_menu_model(menu)
        # The reference keeps the title bar to Search, Hide UI and the window
        # controls.  About and Shortcuts now live in the sidebar; keep the
        # menu object for existing callers but out of the visual shell.
        self.menu_btn.set_visible(False)
        header.pack_end(self.menu_btn)
        self.search_btn = button("Search", "system-search-symbolic",
                                 css="wxb-quiet wxb-header-pill",
                                 tooltip="Search · Ctrl K",
                                 on_click=self.open_command)
        self.privacy_btn = button(
            "Hide UI", "view-reveal-symbolic",
            css="wxb-quiet wxb-header-pill",
            tooltip="Hide every value without changing it",
            on_click=self.toggle_privacy)
        header.pack_end(self.privacy_btn)
        header.pack_end(self.search_btn)
        self._face_privacy()
        self.stop_btn = button("Stop all", "media-playback-stop-symbolic",
                               css="wxb-danger",
                               tooltip="Stop every engine · Esc",
                               on_click=self._stop_all)
        self.stop_btn.set_visible(False)
        header.pack_end(self.stop_btn)

        # Nexus-like proportions: a 300px floating sidebar wrapper whose
        # padded glass surface sits inset from the window edge.  The main
        # surface is a second floating pane, not a full-bleed headerbar.
        sidebar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar.set_hexpand(False)
        sidebar.set_halign(Gtk.Align.FILL)
        sidebar.set_size_request(310, -1)
        self._sidebar_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                      spacing=2)
        self._sidebar_inner.add_css_class("wxb-sidebar")
        self._sidebar_inner.set_hexpand(True)
        self._sidebar_inner.set_vexpand(True)
        self._sidebar_inner.set_margin_top(12)
        self._sidebar_inner.set_margin_bottom(12)
        self._sidebar_inner.set_margin_start(12)
        self._sidebar_inner.set_margin_end(10)
        sidebar.append(self._sidebar_inner)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("wxb-side-head")
        head.set_margin_start(8)
        head.set_margin_end(5)
        head.set_margin_top(7)
        head.set_margin_bottom(17)
        wordmark = el(label("WXB", "wxb-side-title", xalign=0.5),
                      label("STUDIO · CONTROL ROOM", "wxb-side-sub",
                            xalign=0.5),
                      vertical=True, spacing=0, hexpand=True)
        head.append(wordmark)
        self.sidebar_toggle = button(
            icon_name="go-previous-symbolic", css="wxb-side-collapse",
            tooltip="Collapse navigation", on_click=self._toggle_sidebar)
        head.append(self.sidebar_toggle)
        self._sidebar_inner.append(head)

        self._add_nav(self._sidebar_inner, "overview", "Overview",
                      "view-grid-symbolic", "1")
        self._add_nav(self._sidebar_inner, "automation", "Auto Clicker",
                      "input-mouse-symbolic", "2")
        self._add_nav(self._sidebar_inner, "rotation", "AutoSwitcher",
                      "view-refresh-symbolic", "")
        self._add_nav(self._sidebar_inner, "socd", "SOCD Cleaner",
                      "input-keyboard-symbolic", "")
        self._add_nav(self._sidebar_inner, "gamemode", "GameMode",
                      "applications-games-symbolic", "7")
        self._add_nav(self._sidebar_inner, "recorder", "Recorder",
                      "media-record-symbolic", "6")
        self._add_nav(self._sidebar_inner, "macros", "Macros",
                      "media-playlist-repeat-symbolic", "5")
        self._add_nav(self._sidebar_inner, "alts", "Cordial",
                      "view-list-symbolic", "3")
        self._add_nav(self._sidebar_inner, "settings", "Settings",
                      "emblem-system-symbolic", "4")

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        self._sidebar_inner.append(spacer)

        rule = Gtk.Box()
        rule.add_css_class("wxb-side-rule")
        rule.set_margin_top(6)
        rule.set_margin_bottom(7)
        rule.set_margin_start(6)
        rule.set_margin_end(6)
        self._sidebar_inner.append(rule)

        about_btn = self._side_link(
            "About", "help-about-symbolic",
            lambda *_: self.app.activate_action("about", None))
        shortcuts_btn = self._side_link(
            "Shortcuts", "preferences-desktop-keyboard-symbolic",
            lambda *_: self.app.activate_action("shortcuts", None))
        self._sidebar_inner.append(about_btn)
        self._sidebar_inner.append(shortcuts_btn)

        starter = button("Launch Sober", "media-playback-start-symbolic",
                         css="wxb-starter",
                         tooltip="Open Sober without leaving WXB",
                         on_click=self._launch_sober)
        starter.set_margin_top(14)
        self._sidebar_inner.append(starter)

        clicker_point = self.service.settings.get("clicker", {})
        self.side_mouse_x = label(
            str(clicker_point.get("fixed_x", 0)), "wxb-mouse-value")
        self.side_mouse_y = label(
            str(clicker_point.get("fixed_y", 0)), "wxb-mouse-value")
        mouse = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        mouse.add_css_class("wxb-mouse-panel")
        mouse.set_margin_top(10)
        mouse.append(label("MOUSE POSITION", "wxb-mouse-title"))
        coords = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        coords.set_homogeneous(True)
        for axis, value in (("X", self.side_mouse_x), ("Y", self.side_mouse_y)):
            field = el(label(axis, "wxb-mouse-axis"), value,
                       vertical=False, spacing=8, css="wxb-mouse-field")
            coords.append(field)
        mouse.append(coords)
        self._sidebar_inner.append(mouse)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        footer.add_css_class("wxb-side-footer")
        footer.set_margin_top(10)
        self.side_status_dot = dot()
        self.side_status = label("Engines idle", "wxb-hint")
        footer.append(el(self.side_status_dot, self.side_status,
                         vertical=False, spacing=7))
        footer.append(label("WXB STUDIO · V" + STUDIO_VERSION,
                            "wxb-side-version", xalign=0.5))
        self._sidebar_inner.append(footer)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(180)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        content_toolbar = Adw.ToolbarView()
        content_toolbar.add_top_bar(header)
        content_toolbar.set_content(self.stack)
        main_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_panel.add_css_class("wxb-main-panel")
        main_panel.set_hexpand(True)
        main_panel.set_vexpand(True)
        main_panel.set_margin_top(12)
        main_panel.set_margin_bottom(12)
        main_panel.set_margin_start(0)
        main_panel.set_margin_end(12)
        main_panel.append(content_toolbar)
        self.split = SidebarShell(sidebar, main_panel)

        self.backdrop = NexusBackdrop()
        self.backdrop.set_visible(
            not bool(self.service.settings.get("monochrome", False)))
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.split)
        self.toast_overlay.set_hexpand(True)
        self.toast_overlay.set_vexpand(True)
        self.toast_overlay.set_halign(Gtk.Align.FILL)
        self.toast_overlay.set_valign(Gtk.Align.FILL)
        self.command_overlay = Gtk.Overlay()
        self.command_overlay.set_child(self.backdrop)
        self.command_overlay.add_overlay(self.toast_overlay)
        self.set_content(self.command_overlay)
        self.connect("notify::default-width", self._responsive)

    def _side_link(self, title, icon_name, on_click):
        b = Gtk.Button()
        b.add_css_class("wxb-nav")
        b.add_css_class("wxb-nav-secondary")
        b.set_child(el(icon(icon_name, 16), label(title, "wxb-nav-title"),
                       vertical=False, spacing=10, hexpand=True))
        # Visible labels make a tooltip redundant.  A tooltip here also
        # overlaps the next row and can swallow a fast click on it.
        b.connect("clicked", on_click)
        return b

    def _responsive(self, *_):
        width = self.get_width()
        if width <= 0:
            # A watchdog can beat the first allocation.  Return True so the
            # timeout survives until a real width is known.
            return not self._closed
        # The four-column dashboard naturally wants ~1122px.  Collapse the
        # sidebar just above that floor so a normal drag can reach the reflow
        # breakpoints instead of being stopped by the content minimum.
        compact = bool(self.service.settings.get("nav_compact"))
        sig = (width, compact)
        if sig == self._layout_sig:
            # Map, resize and tick all call this method.  Once the layout is
            # correct for this width there is nothing to allocate or apply.
            return False
        self._layout_sig = sig
        collapsed = width < 1140 or compact
        if self.split.get_collapsed() != collapsed:
            self.split.set_collapsed(collapsed)
            if collapsed:
                self.split.set_show_sidebar(False)
            else:
                self.split.set_show_sidebar(True)
        self.side_btn.set_visible(collapsed)
        if hasattr(self, "sidebar_toggle"):
            self.sidebar_toggle.set_visible(not collapsed)
        self._reflow(width)
        # The first valid pass ends the 120 ms preallocation watchdog;
        # notify::default-width, map and the one-second tick handle later
        # resizes without an idle timer waking the process eight times a
        # second.
        return False

    def _toggle_sidebar(self):
        self.split.set_show_sidebar(not self.split.get_show_sidebar())

    def _clear_initial_nav_focus(self):
        """Keep the first nav row from looking selected on launch."""
        if self._initial_nav_focus_done:
            return False
        self._initial_nav_focus_done = True
        focus = self.get_focus()
        if (isinstance(focus, Gtk.Button) and
                "wxb-nav" in focus.get_css_classes()):
            self.set_focus(None)
        return False

    def _add_nav(self, parent, key, title, icon_name, shortcut,
                 page_key=None, anchor=None):
        b = Gtk.Button()
        b.add_css_class("wxb-nav")
        b.set_child(el(icon(icon_name, 16), label(title, "wxb-nav-title"),
                       vertical=False, spacing=10, hexpand=True))
        b._nav_page = page_key or key
        b._nav_anchor = anchor
        if anchor:
            b.connect("clicked", lambda *_: self._activate_anchor(
                key, b._nav_page, anchor))
        else:
            b.connect("clicked", lambda *_: self.show_page(b._nav_page))
        # Keep the sidebar rows tooltip-free: their labels are visible and a
        # tooltip can cover the row beneath, swallowing a quick next click.
        parent.append(b)
        self._nav[key] = b

    def _activate_anchor(self, nav_key, page_key, anchor):
        self.show_page(page_key)
        for key, row in self._nav.items():
            if key == nav_key:
                row.add_css_class("active")
            else:
                row.remove_css_class("active")
        if hasattr(self, "header_page"):
            self.header_page.set_text(
                {"rotation": "Rotation", "socd": "SOCD"}.get(
                    nav_key, self.header_page.get_text()))
        GLib.timeout_add(70, self._scroll_to_anchor, page_key, anchor)

    def _scroll_to_anchor(self, page_key, anchor):
        scroll = self._page_scrolls.get(page_key)
        target = self._anchor_cards.get(anchor)
        if scroll is None or target is None:
            return False
        child = scroll.get_child()
        try:
            ok, point = target.compute_point(child, Graphene.Point().init(0, 0))
            if ok:
                adjustment = scroll.get_vadjustment()
                adjustment.set_value(max(0.0, point.y - 22.0))
        except (TypeError, ValueError):
            pass
        return False

    # -- pages -------------------------------------------------------------
    def _build_pages(self):
        for key, builder in (("overview", self._overview),
                             ("automation", self._automation),
                             ("gamemode", self._gamemode),
                             ("alts", self._alts),
                             ("settings", self._settings),
                             ("macros", self._macros),
                             ("recorder", self._recorder)):
            page = builder()
            if key == "automation":
                page, extra_pages = page
            else:
                extra_pages = {}
            self.stack.add_named(page, key)
            self._page_scrolls[key] = page
            for extra_key, extra_page in extra_pages.items():
                self.stack.add_named(extra_page, extra_key)
                self._page_scrolls[extra_key] = extra_page

    def _scroll(self, *children, compact=False):
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        spacing=7 if compact else 20)
        inner.add_css_class("wxb-page-inner")
        if compact:
            inner.add_css_class("wxb-page-inner-compact")
        for child in children:
            inner.append(child)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(920 if compact else 1040)
        clamp.set_tightening_threshold(720)
        clamp.set_child(inner)
        scroll = Gtk.ScrolledWindow()
        # The page's cards wrap; the scroller must be allowed to yield width
        # rather than turning the page's natural minimum into the window's
        # minimum.  Horizontal scrolling is a last-resort fallback for text
        # so large it cannot fit even one column.
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(False)
        scroll.set_propagate_natural_width(False)
        scroll.set_vexpand(True)
        scroll.add_css_class("wxb-page")
        scroll.set_child(clamp)
        return scroll

    def _page_head(self, kicker, title, sub, *actions, compact=False):
        section = kicker.rsplit(" / ", 1)[-1].strip().upper()
        page_icon = {
            "AUTOMATION": "input-mouse-symbolic",
            "AUTO CLICKER": "input-mouse-symbolic",
            "CORDIAL": "view-grid-symbolic",
            "GAMEMODE": "applications-games-symbolic",
            "MACROS": "media-playlist-repeat-symbolic",
            "RECORDER": "media-record-symbolic",
            "AUTOSWITCHER": "view-refresh-symbolic",
            "SOCD CLEANER": "input-keyboard-symbolic",
            "SETTINGS": "emblem-system-symbolic",
            "OVERVIEW": "view-grid-symbolic",
        }.get(section, "view-grid-symbolic")
        badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        badge.add_css_class("wxb-page-icon")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.CENTER)
        badge.append(icon(page_icon, 20, "wxb-page-icon-glyph"))
        copy = el(label(title, "wxb-page-title" +
                        (" wxb-page-title-compact" if compact else ""),
                        wrap=True, max_width=32),
                  label(sub, "wxb-page-sub" +
                        (" wxb-page-sub-compact" if compact else ""),
                        wrap=True, max_width=76),
                  vertical=True, spacing=3 if compact else 5,
                  hexpand=True)
        head = el(badge, copy, vertical=False, spacing=14,
                  css="wxb-page-head")
        if actions:
            action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                 spacing=9)
            for action in actions:
                action_row.append(action)
            copy.append(action_row)
        return head

    def _fold(self, title, sub, content):
        """A compact, collapsed section for tools outside the main task."""
        head = el(label(title, "wxb-fold-title"),
                  label(sub, "wxb-fold-sub"), vertical=True, spacing=1)
        fold = Gtk.Expander()
        fold.set_label_widget(head)
        fold.set_child(content)
        fold.set_expanded(False)
        fold.add_css_class("wxb-fold")
        return fold

    def _section_head(self, title, sub=None, action=None):
        left = el(label(title, "wxb-section-title"),
                  label(sub, "wxb-section-sub") if sub else None,
                  vertical=True, spacing=3, hexpand=True)
        row = el(left, action, vertical=False, spacing=12)
        return row

    def _metric(self, icon_name, caption, value, sub):
        value_label = label(value, "wxb-metric-value")
        w = card(el(icon(icon_name, 16, "wxb-accent"),
                    label(caption, "wxb-label"),
                    vertical=False, spacing=8),
                 value_label,
                 label(sub, "wxb-metric-sub", wrap=True, max_width=20),
                 spacing=12)
        return w, value_label

    def _reflow_grid(self, cards, max_cols=3):
        # A FlowBox wraps during allocation, so a 900px first allocation is
        # laid out correctly before the one-second refresh ever runs.  The
        # breakpoints below change its maximum line length at the same
        # thresholds the old manual reflow used.
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_column_spacing(12)
        flow.set_row_spacing(12)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(max_cols)
        flow.set_halign(Gtk.Align.FILL)
        flow.set_valign(Gtk.Align.START)
        flow.set_hexpand(True)
        for card in cards:
            flow.append(card)
        flow._cols = max_cols
        flow._cards = list(cards)
        flow._max_cols = max_cols
        self._reflow_groups.append(flow)
        return flow

    def _reflow(self, width):
        for flow in getattr(self, "_reflow_groups", []):
            if width < 760:
                cols = 1
            elif width < 1040:
                cols = min(2, flow._max_cols)
            else:
                cols = flow._max_cols
            if flow.get_max_children_per_line() != cols:
                flow.set_max_children_per_line(cols)
            flow._cols = cols

    def _install_breakpoints(self):
        """First-allocation layout, not a one-second-late fallback.

        Adw.Breakpoint applies its setters while a new allocation is being
        measured.  That is what lets a window-manager jump straight to
        900px get the collapsed sidebar and two-column cards on the first
        frame.  The tick still calls _responsive for the hero and for tests.
        """
        def value(v):
            if isinstance(v, bool):
                return GObject.Value(GObject.TYPE_BOOLEAN, v)
            if isinstance(v, int):
                return GObject.Value(GObject.TYPE_UINT, v)
            return GObject.Value(GObject.TYPE_STRING, str(v))

        def add(condition, setters):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            for obj, prop, v in setters:
                bp.add_setter(obj, prop, value(v))
            self.add_breakpoint(bp)

        add("max-width: 1139px", [
            (self.split.get_sidebar(), "visible", False),
            (self.side_btn, "visible", True),
        ])
        add("max-width: 1039px", [
            (flow, "max-children-per-line", min(2, flow._max_cols))
            for flow in self._reflow_groups
        ])
        add("max-width: 759px", [
            (flow, "max-children-per-line", 1)
            for flow in self._reflow_groups
        ])

    # ---------------------------------------------------------------- overview
    def _overview(self):
        head = self._page_head(
            "STUDIO / OVERVIEW", "Overview",
            "A calm control room for Sober: every input, recorder and alt "
            "within reach.")

        self.hero_launch = button("Launch Sober",
                                  "media-playback-start-symbolic",
                                  css="wxb-primary",
                                  on_click=self._launch_sober)
        self.hero_click = button("Start clicker", "input-mouse-symbolic",
                                 css="wxb-quiet",
                                 on_click=self._toggle_clicker_from_hero)
        hero_actions = el(self.hero_launch, self.hero_click,
                          vertical=False, spacing=8)

        self.hero_sober_dot = dot()
        self.hero_sober_text = label("Sober is not running", "wxb-hint")
        self.hero_engine_text = label("All engines idle", "wxb-hint")
        self.hero_alt_text = label("Cordial not checked", "wxb-hint")
        status_line = el(
            self.hero_sober_dot, self.hero_sober_text,
            label("·", "wxb-hint"),
            dot("wxb-dot good"), self.hero_engine_text,
            label("·", "wxb-hint"),
            dot("wxb-dot"), self.hero_alt_text,
            vertical=False, spacing=8, hexpand=True)
        status_card = card(
            el(status_line, hero_actions, vertical=False, spacing=16),
            spacing=0,
        )
        status_card.add_css_class("wxb-status-bar")
        self.hero_status_card = status_card

        m1, self.metric_engines = self._metric(
            "system-run-symbolic", "ENGINES", "0", "clicker · rotation · socd")
        m2, self.metric_clicks = self._metric(
            "input-mouse-symbolic", "CLICKS", "0", "this session")
        m3, self.metric_alts = self._metric(
            "view-list-symbolic", "ALTS", "0", "Cordial profiles")
        m4, self.metric_state = self._metric(
            "weather-clear-symbolic", "STATE", "Ready", "nothing held")
        metrics = self._reflow_grid([m1, m2, m3, m4], max_cols=4)

        self.ui["ac_card_status"] = label("IDLE", "wxb-status")
        self.ui["ac_card_value"] = label("12 CPS", "wxb-metric-value")
        ac_card = card(
            el(el(icon("input-mouse-symbolic", 17), vertical=False),
               el(label("Clicker", "wxb-section-title"),
                  label("Steady pressure", "wxb-hint"), vertical=True,
                  spacing=1, hexpand=True),
               self.ui["ac_card_status"], vertical=False, spacing=10),
            self.ui["ac_card_value"],
            label("Left click · 50% hold", "wxb-metric-sub"),
            button("Open controls", icon_name="go-next-symbolic",
                   css="wxb-quiet",
                   on_click=lambda: self.show_page("automation")),
            spacing=14,
        )
        self.ui["rot_card_status"] = label("IDLE", "wxb-status")
        self.ui["rot_card_value"] = label("1 → 2", "wxb-metric-value")
        rot_card = card(
            el(el(icon("view-refresh-symbolic", 17), vertical=False),
               el(label("AutoSwitcher", "wxb-section-title"),
                  label("Slot A to slot B", "wxb-hint"), vertical=True,
                  spacing=1, hexpand=True),
               self.ui["rot_card_status"], vertical=False, spacing=10),
            self.ui["rot_card_value"],
            label("140 ms · toggle mode", "wxb-metric-sub"),
            button("Open controls", icon_name="go-next-symbolic",
                   css="wxb-quiet",
                   on_click=lambda: self.show_page("rotation")),
            spacing=14,
        )
        self.ui["socd_card_status"] = label("IDLE", "wxb-status")
        self.ui["socd_card_value"] = label("Neutral", "wxb-metric-value")
        socd_card = card(
            el(el(icon("input-keyboard-symbolic", 17), vertical=False),
               el(label("SOCD Cleaner", "wxb-section-title"),
                  label("Opposite keys, resolved", "wxb-hint"), vertical=True,
                  spacing=1, hexpand=True),
               self.ui["socd_card_status"], vertical=False, spacing=10),
            self.ui["socd_card_value"],
            label("Horizontal axis", "wxb-metric-sub"),
            button("Open controls", icon_name="go-next-symbolic",
                   css="wxb-quiet",
                   on_click=lambda: self.show_page("socd")),
            spacing=14,
        )
        controls = self._reflow_grid([ac_card, rot_card, socd_card], max_cols=3)

        self.alts_preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=0)
        alts_section = card(
            self._section_head("Cordial", "Profiles found on this machine",
                               button("Open Cordial", icon_name="go-next-symbolic",
                                      css="wxb-quiet",
                                      on_click=lambda: self.show_page("alts"))),
            self.alts_preview,
            spacing=14,
        )
        return self._scroll(
            head,
            status_card,
            metrics,
            self._section_head("Live controls", "Small tools, close at hand"),
            controls,
            alts_section,
        )

    # -------------------------------------------------------------- automation
    def _automation(self):
        head = self._page_head(
            "STUDIO / AUTO CLICKER", "Auto Clicker",
            "Constant pressure without the cramp. It starts only when you "
            "do.",
        )

        # clicker: exact numeric controls over the engine's full safe range,
        # not a 1–50 slider that silently caps deliberate settings.
        cps = float(self.service.settings["clicker"]["cps"])
        duty = float(self.service.settings["clicker"]["duty"])
        self.ui["ac_cps_value"] = self._clicker_entry("ac_cps_value", "cps")
        self.ui["ac_cps_value"].set_text(fmt_clicker(cps))
        self.ui["ac_duty_value"] = self._clicker_entry("ac_duty_value", "duty")
        self.ui["ac_duty_value"].set_text(fmt_clicker(duty))
        self.ui["ac_cps"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, CLICKER_CPS_MIN, CLICKER_CPS_MAX,
            0.01)
        self.ui["ac_cps"].set_value(cps)
        self.ui["ac_cps"].set_draw_value(False)
        self.ui["ac_cps"].set_hexpand(True)
        self.ui["ac_cps"].add_css_class("wxb-scale")
        self.ui["ac_duty"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, CLICKER_DUTY_MIN, CLICKER_DUTY_MAX,
            0.01)
        self.ui["ac_duty"].set_value(duty)
        self.ui["ac_duty"].set_draw_value(False)
        self.ui["ac_duty"].set_hexpand(True)
        self.ui["ac_duty"].add_css_class("wxb-scale")
        self.ui["ac_button_choice"] = Gtk.DropDown.new_from_strings(
            ["Left button", "Right button", "Middle button"])
        self.ui["ac_button_choice"].add_css_class("wxb-drop")
        button_index = {"left": 0, "right": 1, "middle": 2}
        self.ui["ac_button_choice"].set_selected(
            button_index.get(self.service.settings["clicker"].get("button"), 0))
        self.ui["ac_mode"] = Gtk.DropDown.new_from_strings(["Toggle", "Hold"])
        self.ui["ac_mode"].add_css_class("wxb-drop")
        self.ui["ac_mode"].set_selected(
            0 if self.service.settings["clicker"].get("mode") == "toggle"
            else 1)
        self.ui["ac_mode"].connect(
            "notify::selected", lambda *_: self._set_clicker_mode())
        self.ui["ac_cps"].connect(
            "value-changed", lambda s: self._sync_clicker_controls())
        self.ui["ac_duty"].connect(
            "value-changed", lambda s: self._sync_clicker_controls())
        self.ui["ac_button_choice"].connect(
            "notify::selected", lambda *_: self._sync_clicker_controls())

        # Advanced clicker behavior stays in one expander so the everyday
        # CPS/hold card remains compact.  Every value is persisted and
        # normalized by the service before a worker starts.
        clicker = self.service.settings["clicker"]
        self.ui["ac_duration"] = Gtk.SpinButton.new_with_range(
            0, CLICKER_DURATION_MAX, 1)
        self.ui["ac_duration"].set_value(float(clicker.get("duration", 0)))
        self.ui["ac_duration"].set_hexpand(True)
        self.ui["ac_duration"].add_css_class("wxb-spin")
        self.ui["ac_duration"].set_tooltip_text(
            "Seconds to click; 0 keeps clicking until you stop it")
        self.ui["ac_pointer"] = Gtk.DropDown.new_from_strings(
            ["Follow cursor", "Fixed point", "Jitter"])
        self.ui["ac_pointer"].add_css_class("wxb-drop")
        pointer_modes = ("follow", "fixed", "jitter")
        self.ui["ac_pointer"].set_selected(
            pointer_modes.index(clicker.get("pointer_mode", "follow"))
            if clicker.get("pointer_mode") in pointer_modes else 0)
        self.ui["ac_jitter"] = Gtk.SpinButton.new_with_range(
            0, CLICKER_JITTER_MAX, 1)
        self.ui["ac_jitter"].set_value(int(clicker.get("jitter", 0)))
        self.ui["ac_jitter"].set_hexpand(True)
        self.ui["ac_jitter"].add_css_class("wxb-spin")
        self.ui["ac_rate_mode"] = Gtk.DropDown.new_from_strings(
            ["Exact CPS", "CPS range"])
        self.ui["ac_rate_mode"].add_css_class("wxb-drop")
        self.ui["ac_rate_mode"].set_selected(
            1 if clicker.get("rate_mode") == "range" else 0)
        self.ui["ac_cps_max"] = Gtk.SpinButton.new_with_range(
            CLICKER_CPS_MIN, CLICKER_CPS_MAX, 0.01)
        self.ui["ac_cps_max"].set_digits(2)
        self.ui["ac_cps_max"].set_value(float(clicker.get("cps_max", 14)))
        self.ui["ac_cps_max"].set_hexpand(True)
        self.ui["ac_cps_max"].add_css_class("wxb-spin")
        self.ui["ac_start_delay"] = Gtk.SpinButton.new_with_range(0, 60000, 10)
        self.ui["ac_start_delay"].set_value(
            int(clicker.get("start_delay_ms", 0)))
        self.ui["ac_start_delay"].set_hexpand(True)
        self.ui["ac_start_delay"].add_css_class("wxb-spin")
        self.ui["ac_stop_clicks"] = Gtk.SpinButton.new_with_range(
            0, 10000000, 1)
        self.ui["ac_stop_clicks"].set_value(
            int(clicker.get("stop_after_clicks", 0)))
        self.ui["ac_stop_clicks"].set_hexpand(True)
        self.ui["ac_stop_clicks"].add_css_class("wxb-spin")
        self.ui["ac_precision"] = Gtk.DropDown.new_from_strings(
            ["Efficient", "Balanced", "Precise"])
        self.ui["ac_precision"].add_css_class("wxb-drop")
        self.ui["ac_precision"].set_selected(
            ("efficient", "balanced", "precise").index(
                clicker.get("timing_precision", "balanced"))
            if clicker.get("timing_precision") in
            ("efficient", "balanced", "precise") else 1)
        self.ui["ac_failsafe_margin"] = Gtk.SpinButton.new_with_range(
            1, 10000, 1)
        self.ui["ac_failsafe_margin"].set_value(
            int(clicker.get("failsafe_margin", 2)))
        self.ui["ac_failsafe_margin"].set_hexpand(True)
        self.ui["ac_failsafe_margin"].add_css_class("wxb-spin")
        self.ui["ac_filter_mode"] = Gtk.DropDown.new_from_strings(
            ["Off", "Blacklist", "Whitelist"])
        self.ui["ac_filter_mode"].add_css_class("wxb-drop")
        self.ui["ac_filter_mode"].set_selected(
            ("off", "blacklist", "whitelist").index(
                clicker.get("app_filter_mode", "off"))
            if clicker.get("app_filter_mode") in
            ("off", "blacklist", "whitelist") else 0)
        self.ui["ac_blacklist"] = Gtk.Entry()
        self.ui["ac_blacklist"].set_placeholder_text(
            "Block in: sober, discord")
        self.ui["ac_blacklist"].set_text(
            ", ".join(clicker.get("app_blacklist", [])))
        self.ui["ac_blacklist"].add_css_class("wxb-entry")
        self.ui["ac_whitelist"] = Gtk.Entry()
        self.ui["ac_whitelist"].set_placeholder_text(
            "Only click in: sober, roblox")
        self.ui["ac_whitelist"].set_text(
            ", ".join(clicker.get("app_whitelist", [])))
        self.ui["ac_whitelist"].add_css_class("wxb-entry")
        self.ui["ac_foreground"] = label("Active app · checking…", "wxb-hint")
        self.ui["ac_filter_hint"] = label(
            "Checking foreground-app support…", "wxb-hint", wrap=True,
            max_width=52)
        self.ui["ac_corner"] = Gtk.DropDown.new_from_strings(
            ["Top left", "Top right", "Bottom left", "Bottom right", "Off"])
        self.ui["ac_corner"].add_css_class("wxb-drop")
        corners = ("top-left", "top-right", "bottom-left", "bottom-right")
        corner = clicker.get("failsafe_corner", "top-left")
        self.ui["ac_corner"].set_selected(
            corners.index(corner) if corner in corners else 4)
        self.ui["ac_failsafe"] = switch()
        self.ui["ac_failsafe"].set_active(bool(clicker.get("failsafe", True)))
        self.ui["ac_failsafe"].set_tooltip_text(
            "Stop when the pointer reaches the chosen screen corner")
        self.ui["ac_pause_lock"] = switch()
        self.ui["ac_pause_lock"].set_active(
            bool(clicker.get("pause_on_lock", True)))
        self.ui["ac_pause_lock"].set_tooltip_text(
            "Pause while the desktop reports the session locked")
        self.ui["ac_set_point"] = button(
            "Set point", icon_name="mark-location-symbolic", css="wxb-quiet",
            on_click=self._capture_clicker_point)
        self.ui["ac_point"] = label("", "wxb-hint wxb-mono")
        self.ui["ac_profile_name"] = Gtk.Entry()
        self.ui["ac_profile_name"].set_placeholder_text("Profile name")
        self.ui["ac_profile_name"].add_css_class("wxb-entry")
        self.ui["ac_profile_choice"] = Gtk.DropDown.new_from_strings(
            ["No clicker profiles"])
        self.ui["ac_profile_choice"].add_css_class("wxb-drop")
        self.ui["ac_profile_save"] = button(
            "Save", icon_name="document-save-symbolic", css="wxb-quiet",
            on_click=self._save_clicker_profile)
        self.ui["ac_profile_load"] = button(
            "Load", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._load_clicker_profile)
        self.ui["ac_profile_delete"] = button(
            "Delete", icon_name="user-trash-symbolic", css="wxb-quiet",
            on_click=self._delete_clicker_profile)
        for widget in (self.ui["ac_duration"], self.ui["ac_jitter"],
                       self.ui["ac_cps_max"], self.ui["ac_start_delay"],
                       self.ui["ac_stop_clicks"],
                       self.ui["ac_failsafe_margin"]):
            widget.connect("value-changed", lambda *_: self._sync_clicker_extras())
        self.ui["ac_pointer"].connect(
            "notify::selected", lambda *_: self._sync_clicker_extras())
        self.ui["ac_corner"].connect(
            "notify::selected", lambda *_: self._sync_clicker_extras())
        for widget in (self.ui["ac_rate_mode"], self.ui["ac_precision"],
                       self.ui["ac_filter_mode"]):
            widget.connect("notify::selected",
                           lambda *_: self._sync_clicker_extras())
        for widget in (self.ui["ac_blacklist"], self.ui["ac_whitelist"]):
            widget.connect("changed", lambda *_: self._sync_clicker_extras())
        for widget in (self.ui["ac_failsafe"], self.ui["ac_pause_lock"]):
            widget.connect("notify::active", lambda *_: self._sync_clicker_extras())
        self.ui["ac_advanced"] = Gtk.Expander(
            label="Advanced Settings")
        self.ui["ac_advanced"].add_css_class("wxb-expander")
        self.ui["ac_advanced"].set_child(el(
            label("ACTIVATION", "wxb-side-kicker"),
            el(self._mini_field("Duration (s · 0 = ∞)",
                                self.ui["ac_duration"]),
               self._mini_field("Start delay (ms)",
                                self.ui["ac_start_delay"]),
               self._mini_field("Stop after clicks (0 = ∞)",
                                self.ui["ac_stop_clicks"]),
               vertical=False, spacing=10),
            label("TIMING", "wxb-side-kicker"),
            el(self._mini_field("Rate", self.ui["ac_rate_mode"]),
               self._mini_field("Max CPS", self.ui["ac_cps_max"]),
               self._mini_field("Precision", self.ui["ac_precision"]),
               vertical=False, spacing=10),
            el(self._mini_field("Pointer", self.ui["ac_pointer"]),
               self._mini_field("Jitter ± px", self.ui["ac_jitter"]),
               self._mini_field("Failsafe size (px)",
                                self.ui["ac_failsafe_margin"]),
               vertical=False, spacing=10),
            el(self._mini_field("Failsafe corner", self.ui["ac_corner"]),
               self._mini_field("Failsafe", self.ui["ac_failsafe"]),
               self._mini_field("Pause on lock", self.ui["ac_pause_lock"]),
               vertical=False, spacing=10),
            label("APP FILTER", "wxb-side-kicker"),
            el(self._mini_field("Mode", self.ui["ac_filter_mode"]),
               self._mini_field("Blacklist", self.ui["ac_blacklist"]),
               self._mini_field("Whitelist", self.ui["ac_whitelist"]),
               vertical=False, spacing=10),
            self.ui["ac_foreground"],
            self.ui["ac_filter_hint"],
            el(self.ui["ac_set_point"], self.ui["ac_point"],
               vertical=False, spacing=8),
            label("CLICKER PROFILES", "wxb-side-kicker"),
            el(self.ui["ac_profile_name"], self.ui["ac_profile_choice"],
               vertical=True, spacing=6, hexpand=True),
            el(self.ui["ac_profile_save"], self.ui["ac_profile_load"],
               self.ui["ac_profile_delete"], vertical=False, spacing=6),
            spacing=10,
        ))
        self._refresh_clicker_profiles()
        self.ui["ac_status"] = label("IDLE", "wxb-status")
        self.ui["ac_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                    max_width=48)
        self.ui["ac_error"].set_visible(False)
        self.ui["ac_start"] = button("Start clicking",
                                     icon_name="media-playback-start-symbolic",
                                     css="wxb-primary",
                                     on_click=self._toggle_clicker)
        self.ui["ac_hotkey_hint"] = label("", "wxb-hint wxb-mono")
        self.ui["ac_hotkey_hint"].set_hexpand(True)
        self.ui["ac_hotkey_hint"].set_xalign(0.5)
        self.ui["ac_hotkey_hint"].add_css_class("wxb-hotkey-pill")
        self.ui["ac_lock"] = Gtk.ToggleButton(label="Lock")
        self.ui["ac_lock"].add_css_class("wxb-quiet")
        self.ui["ac_lock"].set_tooltip_text("Freeze clicker values")
        self.ui["ac_lock"].connect("toggled", lambda *_: self._lock_clicker())
        self._refresh_rate_mode()

        # Nexus' Idle status strip: one shared line for target, count and the
        # primary action, distinct from the settings card below it.
        self.ui["ac_page_target"] = label("Target · cursor", "wxb-hint")
        self.ui["ac_page_total"] = label("Total · 0 clicks", "wxb-hint")
        self.ac_page_dot = dot("wxb-dot good")
        status_left = el(
            self.ac_page_dot, self.ui["ac_status"],
            self.ui["ac_page_target"], self.ui["ac_page_total"],
            vertical=False, spacing=10, hexpand=True)
        automation_status = card(
            el(status_left, self.ui["ac_start"],
               vertical=False, spacing=14),
            spacing=0,
        )
        automation_status.add_css_class("wxb-status-bar")
        automation_status.add_css_class("wxb-automation-status")

        ac_card = card(
            el(el(icon("input-mouse-symbolic", 18), vertical=False),
               el(label("Auto Clicker", "wxb-section-title"),
                  label("Constant pressure, without the cramp.",
                        "wxb-page-sub"), vertical=True, spacing=2,
                  hexpand=True),
               self.ui["ac_lock"],
               vertical=False, spacing=10),
            el(self._field("Clicks per second", self.ui["ac_cps"],
                           self.ui["ac_cps_value"]),
               self._field("Hold time", self.ui["ac_duty"],
                           self.ui["ac_duty_value"]),
               vertical=True, spacing=14, hexpand=True),
            el(self._mini_field("Mouse button", self.ui["ac_button_choice"]),
               self._mini_field("Mode", self.ui["ac_mode"]),
               vertical=False, spacing=10),
            self.ui["ac_error"],
            spacing=18,
        )

        hotkey_set = button(
            "Set Hotkey", css="wxb-ghost",
            tooltip="Capture the global clicker shortcut",
            on_click=lambda: self._start_hotkey_capture("toggle_clicker"))
        hotkey_card = card(
            self._section_head(
                "Hotkey",
                "Pick a key or mouse button to turn the clicker on and off. "
                "It works in any window."),
            el(self.ui["ac_hotkey_hint"], hotkey_set,
               vertical=False, spacing=10, css="wxb-hotkey-row"),
            spacing=12,
        )
        advanced_card = card(self.ui["ac_advanced"], spacing=0)
        advanced_card.add_css_class("wxb-advanced-card")

        # AutoSwitcher (slot A / slot B rotation)
        self.ui["rot_a"] = Gtk.DropDown.new_from_strings(
            [str(i) for i in range(11)])
        self.ui["rot_a"].add_css_class("wxb-drop")
        self.ui["rot_a"].set_selected(
            int(self.service.settings["rotation"].get("slot_a", 1)))
        self.ui["rot_b"] = Gtk.DropDown.new_from_strings(
            [str(i) for i in range(11)])
        self.ui["rot_b"].add_css_class("wxb-drop")
        self.ui["rot_b"].set_selected(
            int(self.service.settings["rotation"].get("slot_b", 2)))
        self.ui["rot_da"] = Gtk.SpinButton.new_with_range(0, 5000, 10)
        self.ui["rot_da"].set_value(self.service.settings["rotation"]["delay_a"])
        self.ui["rot_da"].add_css_class("wxb-spin")
        self.ui["rot_db"] = Gtk.SpinButton.new_with_range(0, 5000, 10)
        self.ui["rot_db"].set_value(self.service.settings["rotation"]["delay_b"])
        self.ui["rot_db"].add_css_class("wxb-spin")
        self.ui["rot_mode"] = Gtk.DropDown.new_from_strings(["Toggle", "Hold"])
        self.ui["rot_mode"].add_css_class("wxb-drop")
        self.ui["rot_mode"].set_selected(
            0 if self.service.settings["rotation"].get("mode") == "toggle" else 1)
        self.ui["rot_max"] = Gtk.SpinButton.new_with_range(0, 999, 1)
        self.ui["rot_max"].set_value(self.service.settings["rotation"]["max_rot"])
        self.ui["rot_max"].add_css_class("wxb-spin")
        self.ui["rot_a_hint"] = label("", "wxb-mono wxb-hint")
        self.ui["rot_b_hint"] = label("", "wxb-mono wxb-hint")
        self.ui["rot_enabled"] = switch()
        self.ui["rot_enabled"].set_active(self.service.macro_enabled)
        self.ui["rot_enabled"].set_tooltip_text(
            "Master switch: off stops rotation and gates its hotkey")
        self.ui["rot_enabled"].connect(
            "notify::active", lambda *_: self._sync_macro_enabled())
        for widget in (self.ui["rot_a"], self.ui["rot_b"],
                       self.ui["rot_da"], self.ui["rot_db"],
                       self.ui["rot_mode"], self.ui["rot_max"]):
            widget.connect("notify::selected", lambda *_: self._sync_rotation_controls()) if hasattr(widget, "get_selected") else widget.connect("value-changed", lambda *_: self._sync_rotation_controls())
        for widget in (self.ui["rot_a"], self.ui["rot_b"]):
            widget.connect("notify::selected", lambda *_: self._sync_slot_hints())
        self._sync_slot_hints()
        self.ui["rot_status"] = label("IDLE", "wxb-status")
        self.ui["rot_start"] = button(
            "Start rotation", icon_name="media-playback-start-symbolic",
            css="wxb-primary", on_click=self._toggle_rotation)
        self.ui["rot_hotkey_hint"] = label("", "wxb-hint wxb-mono")
        self.ui["rot_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                     max_width=48)
        self.ui["rot_error"].set_visible(False)
        self.ui["rot_lock"] = Gtk.ToggleButton(label="Lock")
        self.ui["rot_lock"].add_css_class("wxb-quiet")
        self.ui["rot_lock"].set_tooltip_text("Freeze rotation values")
        self.ui["rot_lock"].connect("toggled", lambda *_: self._lock_rotation())
        rot_card = card(
            el(el(icon("view-refresh-symbolic", 18), vertical=False),
               el(label("AutoSwitcher", "wxb-section-title"),
                  label("Tap A, wait, tap B, repeat.", "wxb-page-sub"),
                  vertical=True, spacing=2, hexpand=True),
               el(label("Enabled", "wxb-label"), self.ui["rot_enabled"],
                  vertical=False, spacing=8),
               self.ui["rot_lock"], self.ui["rot_status"],
               vertical=False, spacing=10),
            el(self._mini_field("Slot A", el(self.ui["rot_a"],
                                             self.ui["rot_a_hint"],
                                             vertical=False, spacing=8)),
               self._mini_field("Delay A (ms)", self.ui["rot_da"]),
               vertical=False, spacing=10),
            el(self._mini_field("Slot B", el(self.ui["rot_b"],
                                             self.ui["rot_b_hint"],
                                             vertical=False, spacing=8)),
               self._mini_field("Delay B (ms)", self.ui["rot_db"]),
               vertical=False, spacing=10),
            el(self._mini_field("Mode", self.ui["rot_mode"]),
               self._mini_field("Max rotations (0 = ∞)", self.ui["rot_max"]),
               vertical=False, spacing=10),
            self.ui["rot_error"],
            el(self.ui["rot_start"], vertical=True, spacing=6),
            self.ui["rot_hotkey_hint"],
            label("Off stops the rotation and disables its hotkey and "
                  "controls until you switch it back on.",
                  "wxb-hint", wrap=True, max_width=52),
            spacing=18,
        )
        self._apply_rotation_gates()

        # socd
        self.ui["socd_h"] = Gtk.DropDown.new_from_strings(
            ["Neutral", "Last win", "First win", "Priority"])
        self.ui["socd_h"].add_css_class("wxb-drop")
        self.ui["socd_v"] = Gtk.DropDown.new_from_strings(
            ["Neutral", "Last win", "First win", "Priority"])
        self.ui["socd_v"].add_css_class("wxb-drop")
        self._set_socd_dropdowns()
        self.ui["socd_h"].connect(
            "notify::selected", lambda *_: self._sync_socd_controls())
        self.ui["socd_v"].connect(
            "notify::selected", lambda *_: self._sync_socd_controls())
        self.ui["socd_status"] = label("IDLE", "wxb-status")
        self.ui["socd_start"] = button(
            "Enable cleaner", icon_name="media-playback-start-symbolic",
            css="wxb-primary", on_click=self._toggle_socd)
        self.ui["socd_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                      max_width=48)
        self.ui["socd_error"].set_visible(False)
        self.ui["socd_conflicts"] = label("0 conflicts", "wxb-mono")
        self.ui["socd_live"] = label("held — · sent — · suppressed —",
                                     "wxb-mono", wrap=True, max_width=70)
        self.ui["socd_access"] = label("Checking input access…", "wxb-hint",
                                       wrap=True, max_width=70)
        self.ui["lab_capture_badge"] = label("CAPTURE · OFF", "wxb-hint")
        self.ui["lab_notice"] = label(
            "Use SOCD, remapping or visualizers only where game and "
            "competition rules allow them.", "wxb-hint", wrap=True,
            max_width=70)
        self.ui["lab_fold"] = self._input_lab()
        socd_card = card(
            el(el(icon("input-keyboard-symbolic", 18), vertical=False),
               el(label("SOCD cleaner", "wxb-section-title"),
                  label("One direction per axis. No stuck keys.",
                        "wxb-page-sub"), vertical=True, spacing=2,
                  hexpand=True),
               self.ui["socd_status"], vertical=False, spacing=10),
            el(self.ui["lab_capture_badge"], self.ui["lab_notice"],
               vertical=True, spacing=4),
            el(self._mini_field("Horizontal", self.ui["socd_h"]),
               self._mini_field("Vertical", self.ui["socd_v"]),
               vertical=False, spacing=10),
            el(label("Presets", "wxb-label"),
               *[button(name, css="wxb-ghost",
                        on_click=lambda _b, n=name: self._apply_socd_preset(n))
                 for name in ("Hitbox standard", "True neutral",
                              "Last win", "First win")],
               vertical=False, spacing=6, css="wxb-row"),
            self.ui["socd_live"],
            el(label("WASD and arrow keys are treated as one direction each.",
                    "wxb-hint", wrap=True, max_width=52),
               self.ui["socd_conflicts"], vertical=False, spacing=12,
               halign=Gtk.Align.START),
            self.ui["socd_access"],
            self.ui["socd_error"],
            self.ui["socd_start"],
            self.ui["lab_fold"],
            spacing=18,
        )
        self._anchor_cards.update({
            "clicker": ac_card,
            "rotation": rot_card,
            "socd": socd_card,
        })
        clicker_page = self._scroll(
            head,
            automation_status,
            ac_card,
            hotkey_card,
            advanced_card,
        )
        # Rotation and SOCD used to be anchors on this one long page.  Give
        # each its own page and keep the existing connected controls intact by
        # moving the already-built card into the new page.
        return clicker_page, {
            "rotation": self._tool_page(
                "AutoSwitcher",
                "Tap A, wait, tap B, repeat. Independent from the clicker.",
                "view-refresh-symbolic", rot_card),
            "socd": self._tool_page(
                "SOCD Cleaner",
                "One direction per axis. Independent from the clicker.",
                "input-keyboard-symbolic", socd_card),
        }

    def _tool_page(self, title, sub, icon_name, card):
        """A focused page for one automation tool."""
        head = self._page_head("AUTOMATION / " + title.upper(), title, sub)
        if card.get_parent() is not None:
            card.unparent()
        return self._scroll(head, card)

    def _clicker_entry(self, key, which):
        """Editable exact CPS/CDC value beside the existing scale."""
        entry = Gtk.Entry()
        entry.set_width_chars(8)
        entry.set_size_request(78, -1)
        entry.set_alignment(1.0)
        entry.add_css_class("wxb-entry")
        entry.add_css_class("wxb-value-entry")
        entry.set_has_frame(True)
        entry.set_tooltip_text("Type an exact value and press Enter")
        entry.connect("activate", lambda _e, w=which: self._commit_clicker_entry(w))
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c, w=which: self._commit_clicker_entry(w))
        entry.add_controller(focus)
        return entry

    def _commit_clicker_entry(self, which):
        entry = self.ui.get("ac_%s_value" % which)
        scale = self.ui.get("ac_%s" % which)
        if entry is None or scale is None:
            return
        try:
            value = float(entry.get_text().replace("%", "").strip())
        except ValueError:
            value = scale.get_value()
        scale.set_value(value)
        self._sync_clicker_controls()

    def _field(self, caption, control, value_label):
        box = el(label(caption, "wxb-label"), control,
                 vertical=True, spacing=5, hexpand=True)
        row = el(box, value_label, vertical=False, spacing=10)
        row.set_hexpand(True)
        return row

    def _mini_field(self, caption, control):
        return el(label(caption, "wxb-label"), control,
                  vertical=True, spacing=5, hexpand=True)

    # ---------------------------------------------------------------- gamemode
    def _gamemode(self):
        # Build from saved switches only.  Detection belongs to show_page/_refresh,
        # so constructing every page does not shell out to Flatpak at startup.
        effects = self.service.launch_effects()
        copy = (
            ("mangohud", "MangoHud",
             "FPS, frame time and hardware stats over the game.",
             "Runs the MangoHud Vulkan layer inside Sober's Flatpak."),
            ("gamemode", "Linux GameMode",
             "Ask gamemoded for performance tuning while Sober runs.",
             "Preloads the GameMode auto client, so it starts and stops "
             "with the game."),
            ("gamescope", "Gamescope",
             "Run Sober inside a nested Gamescope compositor.",
             "Uses the host package or the real Flatpak Gamescope "
             "extension. The switch is disabled until one is installed."),
        )
        rows = []
        for key, title, subtitle, detail in copy:
            widget = switch()
            widget.set_active(bool(effects[key]))
            widget.set_tooltip_text(detail)
            widget.connect(
                "notify::active",
                lambda _w, _p, k=key: self._sync_gamemode_effect(k))
            self.ui["gm_%s_switch" % key] = widget
            state = label("Checking…", "wxb-pill")
            state.set_halign(Gtk.Align.END)
            state.set_valign(Gtk.Align.CENTER)
            self.ui["gm_%s_state" % key] = state
            body = el(label(title, "wxb-label"),
                      label(subtitle, "wxb-hint", wrap=True, max_width=50),
                      vertical=True, spacing=3, hexpand=True)
            tail = el(state, widget, vertical=False, spacing=12,
                      valign=Gtk.Align.CENTER)
            rows.append(el(body, tail, vertical=False, spacing=14,
                           css="wxb-row"))

        effects_card = card(
            el(label("Launch effects", "wxb-section-title"),
               label("Each switch is independent and saved immediately.",
                     "wxb-page-sub"),
               vertical=True, spacing=2),
            *rows,
            spacing=14,
        )

        self.ui["gm_summary"] = label("No launch effects enabled",
                                       "wxb-label", wrap=True, max_width=58)
        self.ui["gm_detail"] = label("", "wxb-hint", wrap=True, max_width=62)
        self.ui["gm_launch"] = button(
            "Launch Sober", icon_name="media-playback-start-symbolic",
            css="wxb-primary", on_click=self._launch_sober)
        next_card = card(
            self._section_head("Next launch",
                               "What WXB will pass to Sober"),
            self.ui["gm_summary"],
            self.ui["gm_detail"],
            self.ui["gm_launch"],
            spacing=12,
        )

        notes = card(
            el(label("How it applies", "wxb-section-title"),
               label("Effects belong to one launch, not a permanent system "
                     "override.", "wxb-page-sub"),
               vertical=True, spacing=2),
            self._shortcut_row("MangoHud",
                               "Adds MANGOHUD=1 only to the Sober process."),
            self._shortcut_row("GameMode",
                               "Uses the installed daemon for the game's "
                               "lifetime, then releases it."),
            self._shortcut_row("Gamescope",
                               "Wraps the launch in a real Gamescope "
                               "compositor: gamescope -- <command>."),
            label("Changes are saved at once. A game that is already "
                  "running keeps the effects it started with until it exits.",
                  "wxb-hint", wrap=True, max_width=58),
            spacing=12,
        )

        page = self._scroll(
            self._page_head(
                "LAUNCH / GAMEMODE", "GameMode",
                "Pick the overlays and performance tools that ride along "
                "with Sober."),
            effects_card,
            next_card,
            notes,
        )
        # Detection runs when the page is first shown.  Building every page
        # at startup should not shell out to Flatpak before the user visits
        # this tab.
        return page

    def _sync_gamemode_effect(self, key):
        if self._syncing_gamemode:
            return
        widget = self.ui.get("gm_%s_switch" % key)
        if widget is None:
            return
        enabled = bool(widget.get_active())
        if not self.service.set_gamemode_effect(key, enabled):
            return
        self._refresh_gamemode_page(running=self._sober_running_seen)
        name = {"mangohud": "MangoHud", "gamemode": "Linux GameMode",
                "gamescope": "Gamescope"}.get(key, key)
        self._toast("%s %s for the next launch." %
                    (name, "on" if enabled else "off"))

    def _refresh_gamemode_page(self, running=None):
        if "gm_summary" not in self.ui:
            return
        status = self.service.gamemode_status()
        active = self.service.active_launch_effects()
        names = {"mangohud": "MangoHud", "gamemode": "Linux GameMode",
                 "gamescope": "Gamescope"}
        for key in self.service.GAMEMODE_KEYS:
            widget = self.ui.get("gm_%s_switch" % key)
            state = self.ui.get("gm_%s_state" % key)
            if widget is None or state is None:
                continue
            item = status[key]
            self._syncing_gamemode = True
            try:
                widget.set_active(bool(item["enabled"]))
            finally:
                self._syncing_gamemode = False
            widget.set_sensitive(bool(item["available"] or item["enabled"]))
            if item["available"] and item["enabled"]:
                set_pill(state, "On · ready", "good")
            elif item["available"]:
                set_pill(state, "Available · off", "")
            elif item["enabled"]:
                set_pill(state, "Unavailable", "bad")
            else:
                set_pill(state, "Not installed", "bad")
            state.set_tooltip_text(item["detail"])
        chosen = [names[key] for key in self.service.GAMEMODE_KEYS
                  if active[key]]
        self.ui["gm_summary"].set_text(
            " + ".join(chosen) if chosen else "No launch effects enabled")
        missing = [item["detail"] for item in status.values()
                   if not item["available"]]
        if missing:
            detail = " · ".join(missing)
        else:
            detail = "All three tools are available on this machine."
            if not active["gamescope"]:
                detail += (" Gamescope is available but switched off; "
                           "turn it on only if you want its nested window.")
        self.ui["gm_detail"].set_text(detail)
        if running is None:
            running = self.service.sober_running
        self.ui["gm_launch"].set_sensitive(not running)

    # ------------------------------------------------------------------- alts
    def _alts(self):
        self.ui["alts_status"] = label("Not checked", "wxb-pill")
        self.ui["alts_status"].set_halign(Gtk.Align.START)
        self.ui["alts_empty"] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                        spacing=6)
        # A compact two-column profile grid keeps a large alt list short
        # without hiding any profile or changing its launch action.
        self.ui["alts_list"] = Gtk.FlowBox()
        alts_list = self.ui["alts_list"]
        alts_list.set_selection_mode(Gtk.SelectionMode.NONE)
        alts_list.set_homogeneous(True)
        alts_list.set_min_children_per_line(1)
        alts_list.set_max_children_per_line(2)
        alts_list.set_column_spacing(6)
        alts_list.set_row_spacing(4)
        alts_list.set_valign(Gtk.Align.START)
        alts_list.add_css_class("wxb-alt-grid")
        refresh = button("Refresh", icon_name="view-refresh-symbolic",
                         css="wxb-ghost", on_click=self._scan_alts)
        self.ui["alts_refresh"] = refresh
        # A profile is just a directory in Cordial's data root.  Keep create
        # and delete beside the roster instead of sending people back to
        # Cordial's own window just to manage accounts.
        self.ui["alt_profile_name"] = Gtk.Entry()
        self.ui["alt_profile_name"].set_placeholder_text(
            "New profile name (letters, digits, - _)")
        self.ui["alt_profile_name"].set_hexpand(True)
        self.ui["alt_profile_name"].add_css_class("wxb-entry")
        self.ui["alt_profile_name"].set_tooltip_text(
            "Creates a real Cordial profile directory. Sign in on its first launch.")
        self.ui["alt_profile_add"] = button(
            "Add profile", icon_name="list-add-symbolic", css="wxb-ghost",
            on_click=self._add_alt_profile)
        self.ui["alt_profile_error"] = label(
            "", "wxb-hint wxb-bad", wrap=True, max_width=52)
        self.ui["alt_profile_error"].set_visible(False)
        alt_profile_add = el(
            self.ui["alt_profile_name"], self.ui["alt_profile_add"],
            vertical=False, spacing=8, css="wxb-alt-profile-add")
        opts = self.service.settings["alts"]
        self.ui["alt_auto_tile"] = switch()
        self.ui["alt_auto_tile"].set_active(bool(opts["auto_tile"]))
        self.ui["alt_auto_tile"].set_tooltip_text(
            "Arrange live Cordial clients using the selected layout.")
        self.ui["alt_tiling_style"] = Gtk.DropDown.new_from_strings(
            [title for _key, title in ALT_TILING_CHOICES])
        self.ui["alt_tiling_style"].add_css_class("wxb-drop")
        self.ui["alt_tiling_style"].set_hexpand(True)
        self.ui["alt_tiling_style"].set_size_request(128, -1)
        self.ui["alt_tiling_style"].set_tooltip_text(
            "Choose how Cordial windows share the work area.")
        selected = next((index for index, (key, _title) in enumerate(
            ALT_TILING_CHOICES) if key == opts.get("tiling_style")), 0)
        self.ui["alt_tiling_style"].set_selected(selected)
        self.ui["alt_fps_cap"] = Gtk.SpinButton.new_with_range(
            0, 1000000, 1)
        self.ui["alt_fps_cap"].set_digits(0)
        self.ui["alt_fps_cap"].set_value(int(opts["fps_cap"]))
        self.ui["alt_fps_cap"].add_css_class("wxb-spin")
        self.ui["alt_fps_cap"].set_hexpand(False)
        self.ui["alt_fps_cap"].set_size_request(84, -1)
        self.ui["alt_fps_cap"].set_tooltip_text(
            "Written to every Cordial profile before it launches; 0 disables "
            "the engine cap.")
        self.ui["alt_low_graphics"] = switch()
        self.ui["alt_low_graphics"].set_active(bool(opts["low_graphics"]))
        self.ui["alt_low_graphics"].set_tooltip_text(
            "Writes the lightweight texture and anti-aliasing flags into "
            "every alt profile before launch.")
        self.ui["alt_cpu_quota"] = Gtk.SpinButton.new_with_range(
            0, 1600, 10)
        self.ui["alt_cpu_quota"].set_digits(0)
        # Typing a value outside 0-1600 must leave the previous quota in
        # place.  The default ALWAYS policy silently clamps 1700 to 1600,
        # which then gets persisted as a different valid value.
        self.ui["alt_cpu_quota"].set_update_policy(
            Gtk.SpinButtonUpdatePolicy.IF_VALID)
        self.ui["alt_cpu_quota"].set_value(int(opts["cpu_quota"]))
        self.ui["alt_cpu_quota"].add_css_class("wxb-spin")
        self.ui["alt_cpu_quota"].set_hexpand(False)
        self.ui["alt_cpu_quota"].set_size_request(84, -1)
        self.ui["alt_cpu_quota"].set_tooltip_text(
            "CPU quota as percent of one core (0 is off). Applied to new "
            "Cordial launches; it needs WXB to run as your desktop user.")
        self.ui["alt_tile_now"] = button(
            "Tile now", icon_name="view-grid-symbolic", css="wxb-ghost",
            on_click=self._tile_alts)
        style_title = dict(ALT_TILING_CHOICES).get(
            opts.get("tiling_style"), "Side by side")
        self.ui["alt_options_status"] = label(
            "FPS cap %d · %s · low graphics %s · CPU quota %s · "
            "applied on next launch." %
            (int(opts["fps_cap"]), style_title.lower(),
             "on" if opts["low_graphics"] else "off",
             "%d%%" % int(opts["cpu_quota"])
             if opts["cpu_quota"] else "off"),
            "wxb-hint", wrap=True, max_width=56)
        self.ui["alt_auto_tile"].connect(
            "notify::active", self._alt_options_changed)
        self.ui["alt_tiling_style"].connect(
            "notify::selected", self._alt_options_changed)
        self.ui["alt_fps_cap"].connect(
            "value-changed", self._alt_options_changed)
        self.ui["alt_low_graphics"].connect(
            "notify::active", self._alt_options_changed)
        self.ui["alt_cpu_quota"].connect(
            "value-changed", self._alt_options_changed)
        alt_options = el(
            el(el(label("Auto-tile", "wxb-label"),
                  self.ui["alt_auto_tile"], vertical=False, spacing=6,
                  valign=Gtk.Align.CENTER),
               self._mini_field("Layout", self.ui["alt_tiling_style"]),
               self._mini_field("FPS cap", self.ui["alt_fps_cap"]),
               self.ui["alt_tile_now"],
               vertical=False, spacing=10),
            el(el(label("Low graphics", "wxb-label"),
                  self.ui["alt_low_graphics"], vertical=False, spacing=6,
                  valign=Gtk.Align.CENTER),
               self._mini_field("CPU quota", self.ui["alt_cpu_quota"]),
               vertical=False, spacing=14),
            vertical=True, spacing=7, css="wxb-alt-options")
        alts_card = card(
            self._section_head("Cordial profiles",
                               "Found on this machine", refresh),
            alt_profile_add,
            self.ui["alt_profile_error"],
            alt_options,
            self.ui["alt_options_status"],
            self.ui["alts_status"],
            self.ui["alts_empty"],
            self.ui["alts_list"],
            spacing=8, css="wxb-card wxb-cordial-card",
        )
        self.ui["alts_card"] = alts_card
        self.ui["quick_place"] = Gtk.Entry()
        self.ui["quick_place"].set_placeholder_text(
            "Place ID or roblox.com/games/…")
        self.ui["quick_place"].set_hexpand(True)
        self.ui["quick_place"].add_css_class("wxb-entry")
        self.ui["quick_server"] = Gtk.Entry()
        self.ui["quick_server"].set_placeholder_text("Optional server ID")
        self.ui["quick_server"].set_hexpand(True)
        self.ui["quick_server"].add_css_class("wxb-entry")
        self.ui["quick_mode"] = Gtk.DropDown.new_from_strings(
            ["Player", "Plain"])
        self.ui["quick_mode"].add_css_class("wxb-drop")
        self.ui["quick_mode"].set_selected(
            0 if self.service.settings.get("launch_mode", "player") == "player"
            else 1)
        self.ui["quick_recent"] = Gtk.DropDown.new_from_strings(
            ["No recent places"])
        self.ui["quick_recent"].add_css_class("wxb-drop")
        self.ui["quick_recent"].connect(
            "notify::selected", lambda *_: self._pick_recent_place())
        self._refresh_quick_places()
        self.ui["quick_launch"] = button(
            "Launch place", icon_name="media-playback-start-symbolic",
            css="wxb-primary", on_click=self._quick_launch)
        flags = self.service.lution_flags()
        flag_count = len(flags.get("flags") or {})
        allowed = len(self.service.flag_allowlist())
        self.ui["quick_flags"] = label(
            "Lution flags: %d active · %d allowlisted" % (flag_count, allowed),
            "wxb-hint")
        quick = card(
            el(self._mini_field("Place ID or link", self.ui["quick_place"]),
               self._mini_field("Server ID", self.ui["quick_server"]),
               vertical=False, spacing=8),
            el(self._mini_field("Launch mode", self.ui["quick_mode"]),
               self._mini_field("Recent places", self.ui["quick_recent"]),
               vertical=False, spacing=8),
            el(self.ui["quick_launch"],
               button("Launch Sober", icon_name="media-playback-start-symbolic",
                      css="wxb-ghost", on_click=self._launch_sober),
               button("Plain Sober", css="wxb-ghost",
                      on_click=lambda: self._launch_sober(plain=True)),
               vertical=False, spacing=8),
            self.ui["quick_flags"],
            spacing=8, css="wxb-card wxb-fold-card",
        )
        advanced = el(self._lution_runtime_section(),
                      self._fast_flags_section(),
                      self._linux_env_section(), spacing=12)
        self.ui["alts_quick_fold"] = self._fold(
            "Sober quick play", "Launch a place without leaving Cordial", quick)
        self.ui["alts_advanced_fold"] = self._fold(
            "Sober runtime, flags & environment",
            "Install, fast flags, fonts, cursors and launch env", advanced)
        return self._scroll(
            self._page_head("STUDIO / CORDIAL", "Cordial",
                            "Profiles and tiling, close at hand.",
                            compact=True),
            alts_card,
            self.ui["alts_quick_fold"],
            self.ui["alts_advanced_fold"],
            compact=True,
        )

    # -------------------------------------------------------- lution runtime
    def _lution_runtime_section(self):
        self.ui["sober_status"] = label("Checking Sober…", "wxb-hint",
                                        wrap=True, max_width=52)
        self.ui["sober_detail"] = label("", "wxb-hint", wrap=True,
                                        max_width=52)
        self.ui["sober_playtime"] = label("", "wxb-hint", wrap=True,
                                          max_width=52)
        self.ui["sober_ensure"] = button(
            "Install / update", icon_name="software-update-available-symbolic",
            css="wxb-quiet", on_click=self._ensure_sober)
        self.ui["sober_stop"] = button(
            "Stop game", icon_name="media-playback-stop-symbolic",
            css="wxb-quiet", on_click=self._stop_sober)
        self.ui["sober_stop"].set_sensitive(False)
        self.ui["sober_refresh"] = button(
            "Check", icon_name="view-refresh-symbolic", css="wxb-ghost",
            on_click=lambda: self._refresh_runtime(refresh=True))
        section = el(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            label("SOBER RUNTIME", "wxb-side-kicker"),
            self.ui["sober_status"],
            self.ui["sober_detail"],
            self.ui["sober_playtime"],
            el(self.ui["sober_ensure"], self.ui["sober_stop"],
               self.ui["sober_refresh"],
               vertical=False, spacing=8),
            label("Deployment is explicit: install or update the Flatpak "
                  "without launching the game.", "wxb-hint", wrap=True,
                  max_width=56),
            spacing=8,
        )
        self._refresh_runtime(refresh=True)
        return section

    def _refresh_runtime(self, refresh=False):
        if "sober_status" not in self.ui:
            return
        data = self.service.lution_runtime(refresh=refresh)
        if data.get("running"):
            state = "running"
        else:
            state = "idle"
        if data.get("installed"):
            self.ui["sober_status"].set_text(
                "Sober %s · %s" % (data.get("version") or "installed", state))
        else:
            self.ui["sober_status"].set_text("Sober is not installed")
        if not data.get("flatpak"):
            detail = "Flatpak is not installed. Install Flatpak first."
        elif data.get("error"):
            detail = data["error"]
        elif data.get("installed"):
            detail = "%d crash dump%s · Flatpak app" % (
                int(data.get("crashes") or 0),
                "" if int(data.get("crashes") or 0) == 1 else "s")
        else:
            detail = "The Flatpak will be installed on demand."
        self.ui["sober_detail"].set_text(detail)
        playtime = self.service.lution_playtime()
        if playtime.get("ok"):
            self.ui["sober_playtime"].set_text(
                "Playtime · %s · %d session%s" % (
                    playtime.get("label", "0s"),
                    int(playtime.get("sessions") or 0),
                    "" if int(playtime.get("sessions") or 0) == 1 else "s"))
        else:
            self.ui["sober_playtime"].set_text(
                "Playtime unavailable: %s" % playtime.get("error", "unknown"))
        self.ui["sober_ensure"].set_sensitive(
            bool(data.get("flatpak") and not data.get("running")))
        self.ui["sober_stop"].set_sensitive(bool(data.get("running")))

    def _ensure_sober(self):
        ok, message = self.service.ensure_sober()
        self._toast(message)
        if ok:
            self.ui["sober_status"].set_text("Installing or updating Sober…")
            self.ui["sober_detail"].set_text("This can take a few minutes.")
            self.ui["sober_ensure"].set_sensitive(False)
        else:
            self.ui["sober_detail"].set_text(message)

    def _stop_sober(self):
        # The button is disabled while Sober is idle, but a click can race a
        # refresh or arrive through accessibility tooling.  Do not spawn a
        # pointless `flatpak kill` or claim it is stopping when nothing runs.
        try:
            running = bool(self.service.sober_running)
        except Exception:
            running = True
        if not running:
            self._toast("Sober is not running.")
            return
        ok, message = self.service.stop_sober()
        self._toast(message)
        if ok:
            self.ui["sober_status"].set_text("Stopping Sober…")
            self.ui["sober_stop"].set_sensitive(False)
            self.ui["sober_ensure"].set_sensitive(False)
            self._refresh()

    # ---------------------------------------------------------- fast flags
    def _text_panel(self, key, height=128):
        view = Gtk.TextView()
        view.set_wrap_mode(Gtk.WrapMode.CHAR)
        view.set_monospace(True)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(8)
        view.set_right_margin(8)
        view.add_css_class("wxb-code")
        view.set_hexpand(True)
        self.ui[key] = view
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(height)
        scroll.set_max_content_height(240)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(view)
        return scroll

    def _fast_flags_section(self):
        panel = self._text_panel("flags_view")
        self.ui["flags_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                       max_width=52)
        self.ui["flags_error"].set_visible(False)
        self.ui["flags_save"] = button(
            "Save flags", icon_name="document-save-symbolic",
            css="wxb-quiet", on_click=self._save_flags)
        self.ui["flags_reload"] = button(
            "Reload", icon_name="view-refresh-symbolic", css="wxb-ghost",
            on_click=self._reload_flags)
        self.ui["flags_clean"] = button(
            "Remove unknown", icon_name="edit-clear-symbolic", css="wxb-ghost",
            on_click=self._clean_flags)
        self.ui["flag_profile_name"] = Gtk.Entry()
        self.ui["flag_profile_name"].set_placeholder_text("Profile name")
        self.ui["flag_profile_name"].set_hexpand(True)
        self.ui["flag_profile_name"].add_css_class("wxb-entry")
        self.ui["flag_profile_choice"] = Gtk.DropDown.new_from_strings(
            ["No flag profiles"])
        self.ui["flag_profile_choice"].add_css_class("wxb-drop")
        self.ui["flag_profile_save"] = button(
            "Save profile", icon_name="document-save-symbolic",
            css="wxb-ghost", on_click=self._save_flag_profile)
        self.ui["flag_profile_load"] = button(
            "Load", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._load_flag_profile)
        self.ui["flag_profile_delete"] = button(
            "Delete", icon_name="edit-delete-symbolic", css="wxb-ghost",
            on_click=self._delete_flag_profile)
        self.ui["flag_profile_error"] = label(
            "", "wxb-hint wxb-bad", wrap=True, max_width=52)
        self.ui["flag_profile_error"].set_visible(False)

        body = el(
            label("One NAME = value per line. Only allowlisted flags are "
                  "accepted; unknown names are refused.", "wxb-hint",
                  wrap=True, max_width=56),
            panel,
            self.ui["flags_error"],
            el(self.ui["flags_reload"], self.ui["flags_save"],
               self.ui["flags_clean"], vertical=False, spacing=8),
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            label("Profiles", "wxb-label"),
            el(self.ui["flag_profile_name"], self.ui["flag_profile_save"],
               vertical=False, spacing=8),
            el(self.ui["flag_profile_choice"], self.ui["flag_profile_load"],
               self.ui["flag_profile_delete"], vertical=False, spacing=8),
            self.ui["flag_profile_error"],
            spacing=9,
        )
        self.ui["flags_expander"] = Gtk.Expander()
        self.ui["flags_expander"].set_label("Fast flags")
        self.ui["flags_expander"].add_css_class("wxb-expander")
        self.ui["flags_expander"].set_child(body)
        self._refresh_flags()
        self._refresh_flag_profiles()
        return self.ui["flags_expander"]

    def _flag_text(self):
        if not self.ui.get("flags_view"):
            return ""
        buffer = self.ui["flags_view"].get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(),
                               True)

    def _set_flag_text(self, text):
        if self.ui.get("flags_view"):
            self.ui["flags_view"].get_buffer().set_text(text or "")

    def _refresh_flags(self):
        if "quick_flags" not in self.ui:
            return
        data = self.service.lution_flags()
        if not data.get("ok"):
            self.ui["quick_flags"].set_text(
                data.get("error", "Lution flags are unavailable."))
            self._set_visual_error("flags_error",
                                   data.get("error", ""))
            return
        flags = data.get("flags") or {}
        self.ui["quick_flags"].set_text(
            "Lution flags: %d active · %d allowlisted" % (
                len(flags), len(self.service.flag_allowlist())))
        ok, text = self.service.lution_flags_text()
        if ok:
            self._set_flag_text(text)
            self._set_visual_error("flags_error", "")
        else:
            self._set_visual_error("flags_error", text)

    def _reload_flags(self):
        self._refresh_flags()
        self._toast("Flag table reloaded.")

    def _save_flags(self):
        ok, message = self.service.set_lution_flags_text(self._flag_text())
        self._toast(message)
        self._set_visual_error("flags_error", "" if ok else message)
        if ok:
            self._refresh_flags()

    def _clean_flags(self):
        ok, message = self.service.clean_lution_flags()
        self._toast(message)
        self._set_visual_error("flags_error", "" if ok else message)
        if ok:
            self._refresh_flags()

    def _refresh_flag_profiles(self):
        if "flag_profile_choice" not in self.ui:
            return
        names = self.service.list_flag_profiles()
        self._flag_profiles = names
        self.ui["flag_profile_choice"].set_model(
            Gtk.StringList.new(names or ["No flag profiles"]))
        self.ui["flag_profile_choice"].set_selected(0)
        enabled = bool(names)
        self.ui["flag_profile_load"].set_sensitive(enabled)
        self.ui["flag_profile_delete"].set_sensitive(enabled)

    def _selected_flag_profile(self):
        names = getattr(self, "_flag_profiles", [])
        index = self.ui["flag_profile_choice"].get_selected()
        return names[index] if 0 <= index < len(names) else ""

    def _save_flag_profile(self):
        ok, message = self.service.save_flag_profile(
            self.ui["flag_profile_name"].get_text())
        self._toast(message)
        self._set_visual_error("flag_profile_error", "" if ok else message)
        if ok:
            self._refresh_flag_profiles()
            names = getattr(self, "_flag_profiles", [])
            name = self.service.clean_config_name(
                self.ui["flag_profile_name"].get_text())
            if name in names:
                self.ui["flag_profile_choice"].set_selected(names.index(name))

    def _load_flag_profile(self):
        name = self._selected_flag_profile()
        if not name:
            self._set_visual_error("flag_profile_error",
                                   "No flag profile selected.")
            return
        ok, message = self.service.load_flag_profile(name)
        self._toast(message)
        self._set_visual_error("flag_profile_error", "" if ok else message)
        if ok:
            self._refresh_flags()

    def _delete_flag_profile(self):
        name = self._selected_flag_profile()
        if not name:
            self._set_visual_error("flag_profile_error",
                                   "No flag profile selected.")
            return
        ok, message = self.service.delete_flag_profile(name)
        self._toast(message)
        self._set_visual_error("flag_profile_error", "" if ok else message)
        if ok:
            self._refresh_flag_profiles()

    # ------------------------------------------------------- linux settings
    def _linux_env_section(self):
        panel = self._text_panel("env_view", height=100)
        self.ui["env_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                     max_width=52)
        self.ui["env_error"].set_visible(False)
        self.ui["env_status"] = label("", "wxb-hint", wrap=True,
                                      max_width=52)
        self.ui["env_save"] = button(
            "Save environment", icon_name="document-save-symbolic",
            css="wxb-quiet", on_click=self._save_env)
        self.ui["env_reload"] = button(
            "Reload", icon_name="view-refresh-symbolic", css="wxb-ghost",
            on_click=self._refresh_env)
        body = el(
            label("Variables are passed to Sober at the next launch. "
                  "One NAME = value per line; invalid names are refused.",
                  "wxb-hint", wrap=True, max_width=56),
            panel,
            self.ui["env_error"],
            self.ui["env_status"],
            el(self.ui["env_reload"], self.ui["env_save"],
               vertical=False, spacing=8),
            spacing=9,
        )
        self.ui["env_expander"] = Gtk.Expander()
        self.ui["env_expander"].set_label("Linux launch environment")
        self.ui["env_expander"].add_css_class("wxb-expander")
        self.ui["env_expander"].set_child(body)
        self._refresh_env()
        return self.ui["env_expander"]

    def _set_env_text(self, text):
        if "env_view" in self.ui:
            self.ui["env_view"].get_buffer().set_text(text or "")

    def _refresh_env(self):
        if "env_status" not in self.ui:
            return
        data = self.service.lution_env()
        if not data.get("ok"):
            self._set_env_text("")
            self.ui["env_status"].set_text("Environment unavailable")
            self._set_visual_error("env_error", data.get("error", ""))
            return
        ok, text = self.service.lution_env_text()
        if ok:
            self._set_env_text(text)
            count = len(data.get("env") or {})
            self.ui["env_status"].set_text(
                "%d variable%s set for the next launch" %
                (count, "" if count == 1 else "s"))
            self._set_visual_error("env_error", "")
        else:
            self._set_visual_error("env_error", text)

    def _save_env(self):
        buffer = self.ui["env_view"].get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(),
                               True)
        ok, message = self.service.save_lution_env_text(text)
        self._toast(message)
        self._set_visual_error("env_error", "" if ok else message)
        if ok:
            self._refresh_env()

    # ---------------------------------------------------------------- visuals
    def _font_status_text(self, data):
        if not data.get("ok"):
            return data.get("error", "Font status is unavailable.")
        if not data.get("apk"):
            return "Roblox font list unavailable. Launch Sober once."
        installed = str(data.get("installed") or "")
        active = int(data.get("active") or 0)
        total = int(data.get("apk_fonts") or 0)
        if installed:
            return "%s · %d/%d faces active" % (installed, active, total)
        if active:
            return "%d/%d faces overridden" % (active, total)
        return "Stock Roblox faces · %d available" % total

    def _cursor_status_text(self, data):
        if not data.get("ok"):
            return data.get("error", "Cursor status is unavailable.")
        active = int(data.get("active") or 0)
        assets = int(data.get("assets") or 0)
        if data.get("mode") == "online":
            text = "Online · %s · %d px · %d/%d active" % (
                data.get("style", "Cursor"), int(data.get("size") or 32),
                active, assets)
        else:
            text = "%s · %s · %d px · %d/%d active" % (
                data.get("style", "Cross"), data.get("color", "Pink"),
                int(data.get("size") or 32), active, assets)
        if not data.get("pillow"):
            return text + " · Pillow is required"
        return text

    def _fps_status_text(self, data):
        if not data.get("ok"):
            return data.get("error", "Framerate cap is unavailable.")
        if not data.get("available"):
            return data.get("error", "Sober settings are not available yet.")
        text = "Current cap · %d FPS" % int(data.get("fps") or 0)
        if data.get("running"):
            text += " · close Sober to change"
        return text

    def _visual_section(self):
        fonts = self.service.fonts_status()
        self.ui["font_status"] = label(
            self._font_status_text(fonts), "wxb-hint", wrap=True, max_width=52)
        self.ui["font_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                       max_width=52)
        self.ui["font_error"].set_visible(False)
        self.ui["font_choose"] = button(
            "Choose font…", icon_name="document-open-symbolic",
            css="wxb-quiet", on_click=self._choose_font)
        self.ui["font_restore"] = button(
            "Restore", icon_name="edit-undo-symbolic", css="wxb-ghost",
            on_click=self._restore_fonts)
        self.ui["font_reapply"] = button(
            "Reapply", icon_name="view-refresh-symbolic", css="wxb-ghost",
            on_click=self._reapply_font)

        options = self.service.cursor_options()
        styles = options.get("styles") or ["Unavailable"]
        colors = options.get("colors") or ["Unavailable"]
        self._cursor_styles = list(styles)
        self._cursor_colors = list(colors)
        self.ui["cursor_style"] = Gtk.DropDown.new_from_strings(styles)
        self.ui["cursor_style"].add_css_class("wxb-drop")
        self.ui["cursor_style"].set_hexpand(True)
        self.ui["cursor_color"] = Gtk.DropDown.new_from_strings(colors)
        self.ui["cursor_color"].add_css_class("wxb-drop")
        self.ui["cursor_color"].set_hexpand(True)
        self.ui["cursor_size"] = Gtk.SpinButton.new_with_range(12, 56, 1)
        self.ui["cursor_size"].set_digits(0)
        self.ui["cursor_size"].set_hexpand(True)
        self.ui["cursor_apply"] = button(
            "Apply cursor", icon_name="input-mouse-symbolic",
            css="wxb-quiet", on_click=self._apply_cursor)
        self.ui["cursor_restore"] = button(
            "Restore", icon_name="edit-undo-symbolic", css="wxb-ghost",
            on_click=self._restore_cursors)
        online = options.get("online") or []
        self._cursor_online = [item.get("name") for item in online
                               if item.get("name")] or ["Unavailable"]
        self._cursor_pack = options.get("pack") or {}
        self.ui["cursor_online"] = Gtk.DropDown.new_from_strings(
            self._cursor_online)
        self.ui["cursor_online"].add_css_class("wxb-drop")
        self.ui["cursor_online"].set_hexpand(True)
        self.ui["cursor_download"] = button(
            "Download & apply", icon_name="document-save-symbolic",
            css="wxb-quiet", on_click=self._download_cursor)
        self.ui["cursor_online_hint"] = label(
            "CC0 online cursor pack · applied at the next launch",
            "wxb-hint", wrap=True, max_width=56)
        self.ui["cursor_status"] = label("", "wxb-hint", wrap=True,
                                         max_width=52)
        self.ui["cursor_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                        max_width=52)
        self.ui["cursor_error"].set_visible(False)

        fps = self.service.fps_status()
        self.ui["fps_value"] = Gtk.SpinButton.new_with_range(0, 1000000, 1)
        self.ui["fps_value"].set_digits(0)
        self.ui["fps_value"].set_hexpand(True)
        self.ui["fps_value"].set_value(int(fps.get("fps") or 0))
        self.ui["fps_apply"] = button(
            "Apply cap", icon_name="emblem-ok-symbolic", css="wxb-quiet",
            on_click=self._apply_fps_cap)
        self.ui["fps_status"] = label(
            self._fps_status_text(fps), "wxb-hint", wrap=True, max_width=52)
        self.ui["fps_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                     max_width=52)
        self.ui["fps_error"].set_visible(False)

        section = el(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            label("SOBER VISUALS", "wxb-side-kicker"),
            label("Fonts, cursors and frame cap · applied at the next launch",
                  "wxb-hint", wrap=True, max_width=56),
            label("Roblox font", "wxb-label"),
            self.ui["font_status"],
            el(self.ui["font_choose"], self.ui["font_restore"],
               self.ui["font_reapply"], vertical=False, spacing=8),
            self.ui["font_error"],
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            label("Cursor", "wxb-label"),
            el(self._mini_field("Style", self.ui["cursor_style"]),
               self._mini_field("Colour", self.ui["cursor_color"]),
               vertical=False, spacing=10),
            el(self._mini_field("Size", self.ui["cursor_size"]),
               self.ui["cursor_apply"], self.ui["cursor_restore"],
               vertical=False, spacing=8),
            label("Online cursor pack", "wxb-label"),
            el(self._mini_field("Cursor", self.ui["cursor_online"]),
               self.ui["cursor_download"], vertical=False, spacing=8),
            self.ui["cursor_online_hint"],
            self.ui["cursor_status"],
            self.ui["cursor_error"],
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            label("Framerate", "wxb-label"),
            el(self._mini_field("Framerate cap", self.ui["fps_value"]),
               self.ui["fps_apply"], vertical=False, spacing=8),
            self.ui["fps_status"],
            self.ui["fps_error"],
            spacing=10,
        )
        self._refresh_visuals()
        return section

    def _set_visual_error(self, key, text):
        widget = self.ui.get(key)
        if widget is not None:
            widget.set_text(text or "")
            widget.set_visible(bool(text))

    def _select_choice(self, dropdown, choices, wanted):
        needle = str(wanted or "").strip().lower()
        for index, choice in enumerate(choices):
            if str(choice).strip().lower() == needle:
                dropdown.set_selected(index)
                return

    def _refresh_visuals(self):
        if "font_status" not in self.ui:
            return
        fonts = self.service.fonts_status()
        self.ui["font_status"].set_text(self._font_status_text(fonts))
        self.ui["font_choose"].set_sensitive(bool(fonts.get("apk")))
        has_font = bool(fonts.get("installed") or fonts.get("active"))
        self.ui["font_restore"].set_sensitive(has_font)
        self.ui["font_reapply"].set_sensitive(bool(fonts.get("installed")))

        cursor = self.service.cursors_status()
        self.ui["cursor_status"].set_text(self._cursor_status_text(cursor))
        if cursor.get("ok"):
            self._select_choice(self.ui["cursor_style"],
                                self._cursor_styles, cursor.get("style"))
            self._select_choice(self.ui["cursor_color"],
                                self._cursor_colors, cursor.get("color"))
            self.ui["cursor_size"].set_value(int(cursor.get("size") or 32))
        if cursor.get("mode") == "online":
            self._select_choice(self.ui["cursor_online"],
                                self._cursor_online, cursor.get("style"))
        can_cursor = bool(cursor.get("ok") and cursor.get("pillow"))
        can_online = can_cursor and self._cursor_online[0] != "Unavailable"
        self.ui["cursor_apply"].set_sensitive(can_cursor)
        self.ui["cursor_style"].set_sensitive(can_cursor)
        self.ui["cursor_color"].set_sensitive(can_cursor)
        self.ui["cursor_size"].set_sensitive(can_cursor)
        self.ui["cursor_online"].set_sensitive(can_online)
        self.ui["cursor_download"].set_sensitive(can_online)
        cached = bool(cursor.get("pack_cached"))
        pack = self._cursor_pack
        self.ui["cursor_online_hint"].set_text(
            "%s · %s · %d choices · %s" % (
                pack.get("name") or "Online cursor pack",
                pack.get("license") or "Public domain",
                len(self._cursor_online),
                "downloaded" if cached else "one-time download"))
        self.ui["cursor_restore"].set_sensitive(
            bool(cursor.get("active")))

        fps = self.service.fps_status()
        self.ui["fps_status"].set_text(self._fps_status_text(fps))
        if fps.get("fps") is not None:
            self.ui["fps_value"].set_value(int(fps["fps"]))
        can_fps = bool(fps.get("available") and not fps.get("running"))
        self.ui["fps_value"].set_sensitive(can_fps)
        self.ui["fps_apply"].set_sensitive(can_fps)

    def _choose_font(self):
        dialog = Gtk.FileDialog(title="Choose a Roblox font")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        font_filter = Gtk.FileFilter()
        font_filter.set_name("TrueType / OpenType fonts")
        font_filter.add_pattern("*.ttf")
        font_filter.add_pattern("*.otf")
        filters.append(font_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(font_filter)
        dialog.open(self, None, self._font_chosen)

    def _font_chosen(self, dialog, result):
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return
        path = selected.get_path() if selected is not None else None
        if path:
            self._apply_font_path(path)

    def _apply_font_path(self, path):
        ok, message = self.service.apply_font(path)
        self._toast(message)
        self._set_visual_error("font_error", "" if ok else message)
        self._refresh_visuals()

    def _restore_fonts(self):
        ok, message = self.service.restore_fonts()
        self._toast(message)
        self._set_visual_error("font_error", "" if ok else message)
        self._refresh_visuals()

    def _reapply_font(self):
        ok, message = self.service.reapply_font()
        self._toast(message)
        self._set_visual_error("font_error", "" if ok else message)
        self._refresh_visuals()

    def _download_cursor(self):
        index = self.ui["cursor_online"].get_selected()
        if not 0 <= index < len(self._cursor_online):
            self._set_visual_error("cursor_error",
                                   "Choose an online cursor first.")
            return
        name = self._cursor_online[index]
        if name == "Unavailable":
            self._set_visual_error("cursor_error",
                                   "The online cursor pack is unavailable.")
            return
        ok, message = self.service.apply_online_cursor(
            name, int(self.ui["cursor_size"].get_value()))
        self._toast(message)
        self._set_visual_error("cursor_error", "" if ok else message)
        self._refresh_visuals()

    def _apply_cursor(self):
        style_index = self.ui["cursor_style"].get_selected()
        color_index = self.ui["cursor_color"].get_selected()
        if not (0 <= style_index < len(self._cursor_styles)
                and 0 <= color_index < len(self._cursor_colors)):
            self._set_visual_error("cursor_error",
                                   "Choose a cursor style and colour first.")
            return
        style = self._cursor_styles[style_index]
        color = self._cursor_colors[color_index]
        ok, message = self.service.apply_cursor(
            style, color, int(self.ui["cursor_size"].get_value()))
        self._toast(message)
        self._set_visual_error("cursor_error", "" if ok else message)
        self._refresh_visuals()

    def _restore_cursors(self):
        ok, message = self.service.restore_cursors()
        self._toast(message)
        self._set_visual_error("cursor_error", "" if ok else message)
        self._refresh_visuals()

    def _apply_fps_cap(self):
        ok, message = self.service.set_fps_cap(
            int(self.ui["fps_value"].get_value()))
        self._toast(message)
        self._set_visual_error("fps_error", "" if ok else message)
        self._refresh_visuals()

    # ------------------------------------------------------------------ macros
    def _macros(self):
        """Accessible macro recorder/player page; workers stay in the service."""
        self._macro_files = []
        self._macro_steps_sig = None
        self._macro_selected = -1
        self._macro_options_syncing = False
        caps = self.service.macro_capabilities()

        self.ui["macro_status"] = label("IDLE", "wxb-pill")
        self.ui["macro_detail"] = label(
            "No macro recorded or opened.", "wxb-hint", wrap=True,
            max_width=64)
        self.ui["macro_capability"] = label(
            caps.get("reason") or "X11 recording and playback available.",
            "wxb-hint", wrap=True, max_width=64)
        self.ui["macro_warning"] = label(
            "Privacy: recordings can contain passwords, messages and other "
            "sensitive input. Files stay on this computer, are mode 0600, "
            "and are never uploaded by WXB.", "wxb-hint wxb-bad",
            wrap=True, max_width=64)

        self.ui["macro_record"] = button(
            "Record", icon_name="media-record-symbolic", css="wxb-primary",
            tooltip="Start recording mouse, keyboard and timing",
            on_click=self._toggle_macro_record)
        self.ui["macro_play"] = button(
            "Play", icon_name="media-playback-start-symbolic",
            css="wxb-quiet", tooltip="Play the open macro",
            on_click=self._toggle_macro_play)
        self.ui["macro_stop"] = button(
            "Stop macro", icon_name="media-playback-stop-symbolic",
            css="wxb-danger", tooltip="Stop recording or playback",
            on_click=self._stop_macro)
        self.ui["macro_hotkeys"] = label("", "wxb-mono", wrap=True,
                                         max_width=64)

        self.ui["macro_speed"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.1, 10.0, 0.1)
        self.ui["macro_speed"].set_draw_value(False)
        self.ui["macro_speed"].set_hexpand(True)
        self.ui["macro_speed"].add_css_class("wxb-scale")
        self.ui["macro_speed_value"] = label("1.0x", "wxb-mono")
        self.ui["macro_speed"].connect(
            "value-changed", lambda *_: self._sync_macro_options())
        self.ui["macro_loop"] = Gtk.DropDown.new_from_strings(
            ["Once", "N times", "Continuous"])
        self.ui["macro_loop"].add_css_class("wxb-drop")
        self.ui["macro_loop"].connect(
            "notify::selected", lambda *_: self._sync_macro_options())
        self.ui["macro_repeat"] = Gtk.SpinButton.new_with_range(1, 100000, 1)
        self.ui["macro_repeat"].set_tooltip_text("Number of playback loops")
        self.ui["macro_repeat"].connect(
            "value-changed", lambda *_: self._sync_macro_options())

        self.ui["macro_name"] = Gtk.Entry()
        self.ui["macro_name"].set_placeholder_text("Macro name")
        self.ui["macro_name"].set_hexpand(True)
        self.ui["macro_save"] = button(
            "Save", icon_name="document-save-symbolic", css="wxb-primary",
            on_click=self._save_macro)
        self.ui["macro_choice"] = Gtk.DropDown.new_from_strings(
            ["No saved macros"])
        self.ui["macro_choice"].add_css_class("wxb-drop")
        self.ui["macro_open"] = button(
            "Open", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._open_macro)
        self.ui["macro_delete"] = button(
            "Delete", icon_name="edit-delete-symbolic", css="wxb-ghost",
            on_click=self._delete_macro)
        self.ui["macro_import"] = button(
            "Import", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._import_macro)
        self.ui["macro_export"] = button(
            "Export", icon_name="document-save-as-symbolic", css="wxb-quiet",
            on_click=self._export_macro)
        self.ui["macro_file_note"] = label(
            "Versioned .wxbmacro.json files; import validates before copying.",
            "wxb-hint", wrap=True, max_width=64)

        self.ui["macro_steps"] = Gtk.ListBox()
        self.ui["macro_steps"].set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.ui["macro_steps"].add_css_class("wxb-macro-steps")
        self.ui["macro_steps"].connect("row-selected",
                                       self._macro_step_selected)
        steps_scroll = Gtk.ScrolledWindow()
        steps_scroll.set_policy(Gtk.PolicyType.NEVER,
                                Gtk.PolicyType.AUTOMATIC)
        steps_scroll.set_min_content_height(180)
        steps_scroll.set_max_content_height(320)
        steps_scroll.set_propagate_natural_height(True)
        steps_scroll.add_css_class("wxb-step-scroll")
        steps_scroll.set_child(self.ui["macro_steps"])

        self.ui["macro_step_detail"] = label(
            "Select a step to inspect it.", "wxb-hint", wrap=True,
            max_width=64)
        self.ui["macro_step_delay"] = Gtk.SpinButton.new_with_range(
            0, 86400, 0.001)
        self.ui["macro_step_delay"].set_digits(3)
        self.ui["macro_step_delay"].set_tooltip_text(
            "Step time in seconds from the start")
        self.ui["macro_step_apply"] = button(
            "Apply time", icon_name="emblem-ok-symbolic", css="wxb-quiet",
            on_click=self._apply_macro_step)
        self.ui["macro_step_delete"] = button(
            "Delete step", icon_name="edit-delete-symbolic", css="wxb-ghost",
            on_click=self._delete_macro_step)
        self.ui["macro_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                       max_width=64)
        self.ui["macro_error"].set_visible(False)
        self.ui["macro_notice"] = label("", "wxb-hint", wrap=True,
                                        max_width=64)
        self.ui["macro_notice"].set_visible(False)

        recorder = card(
            self._section_head("Macro recorder",
                               "Mouse, keyboard, scroll and timing — all local."),
            el(self.ui["macro_status"], self.ui["macro_detail"],
               vertical=False, spacing=12),
            self.ui["macro_capability"],
            el(self.ui["macro_record"], self.ui["macro_play"],
               self.ui["macro_stop"], vertical=False, spacing=8),
            self._field("Playback speed", self.ui["macro_speed"],
                        self.ui["macro_speed_value"]),
            el(label("Loop", "wxb-label"), self.ui["macro_loop"],
               label("Times", "wxb-label"), self.ui["macro_repeat"],
               vertical=False, spacing=8),
            self.ui["macro_hotkeys"],
            self.ui["macro_warning"],
            spacing=14,
        )
        files = card(
            self._section_head("Macro files",
                               "Save, open, import or export a versioned file."),
            el(self.ui["macro_name"], self.ui["macro_save"],
               vertical=False, spacing=8),
            el(self.ui["macro_choice"], self.ui["macro_open"],
               self.ui["macro_delete"], vertical=False, spacing=8),
            el(self.ui["macro_import"], self.ui["macro_export"],
               vertical=False, spacing=8),
            self.ui["macro_file_note"],
            spacing=12,
        )
        editor = card(
            self._section_head("Step editor",
                               "Inspect, retime or remove a recorded step."),
            steps_scroll,
            self.ui["macro_step_detail"],
            el(label("Time (s)", "wxb-label"),
               self.ui["macro_step_delay"], self.ui["macro_step_apply"],
               self.ui["macro_step_delete"], vertical=False, spacing=8),
            self.ui["macro_notice"], self.ui["macro_error"],
            spacing=12,
        )
        self._apply_macro_controls()
        return self._scroll(
            self._page_head("STUDIO / MACROS", "Macros",
                            "Record once, replay exactly. A local "
                            "TinyTask-style recorder for accessibility and "
                            "repetitive UI work."),
            recorder, files, editor,
        )

    def _apply_macro_controls(self):
        settings = self.service.settings["macros"]
        self._macro_options_syncing = True
        try:
            self.ui["macro_speed"].set_value(float(settings["speed"]))
            self.ui["macro_speed_value"].set_text("%.1fx" % settings["speed"])
            modes = {"once": 0, "count": 1, "continuous": 2}
            self.ui["macro_loop"].set_selected(modes.get(settings["loop_mode"], 0))
            self.ui["macro_repeat"].set_value(int(settings["repeat"]))
        finally:
            self._macro_options_syncing = False
        self._sync_macro_option_gates()
        self._sync_macro_hotkey_hint()
        self._refresh_macro_files()
        self._refresh_macro_page(force=True)

    def _sync_macro_option_gates(self):
        mode = self.ui["macro_loop"].get_selected()
        self.ui["macro_repeat"].set_sensitive(mode == 1)

    def _sync_macro_hotkey_hint(self):
        hotkeys = self.service.settings["hotkeys"]
        parts = []
        for action, text in (("record_macro", "record"),
                             ("play_macro", "play"),
                             ("stop_macro", "stop")):
            value = display_hotkey(hotkeys.get(action, "")) or "unbound"
            parts.append("%s %s" % (text, value))
        self.ui["macro_hotkeys"].set_text(
            "Global shortcuts: " + " · ".join(parts))

    def _sync_macro_options(self):
        if getattr(self, "_macro_options_syncing", False):
            return
        speed = float(self.ui["macro_speed"].get_value())
        self.ui["macro_speed_value"].set_text("%.1fx" % speed)
        modes = {0: "once", 1: "count", 2: "continuous"}
        mode = modes.get(self.ui["macro_loop"].get_selected(), "once")
        ok, message = self.service.set_macro_settings(
            speed=speed, loop_mode=mode,
            repeat=int(self.ui["macro_repeat"].get_value()))
        self._sync_macro_option_gates()
        if not ok:
            self._set_error(self.ui["macro_error"], message)

    def _toggle_macro_record(self):
        if self._macro_mode_is("recording"):
            ok, message = self.service.stop_macro_recording()
        else:
            ok, message = self.service.start_macro_recording(
                self.ui["macro_name"].get_text())
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        self._refresh_macro_page(force=True)

    def _toggle_macro_play(self):
        if self._macro_mode_is("playing"):
            ok, message = self.service.stop_macro()
        else:
            ok, message = self.service.start_macro_playback()
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        self._refresh_macro_page(force=True)

    def _stop_macro(self):
        ok, message = self.service.stop_macro(wait=True)
        self._toast(message)
        self._refresh_macro_page(force=True)

    def _macro_mode_is(self, mode):
        snap = self.service.macro_snapshot()
        return bool(snap.get(mode))

    def _refresh_macro_files(self):
        if "macro_choice" not in self.ui:
            return
        names = self.service.list_macro_files()
        self._macro_files = names
        self.ui["macro_choice"].set_model(Gtk.StringList.new(
            names or ["No saved macros"]))
        selected = str(self.service.settings["macros"].get("selected") or "")
        self.ui["macro_choice"].set_selected(
            names.index(selected) if selected in names else 0)
        enabled = bool(names) and not self.service.macro_running
        self.ui["macro_open"].set_sensitive(enabled)
        self.ui["macro_delete"].set_sensitive(enabled)

    def _selected_macro(self):
        names = getattr(self, "_macro_files", [])
        index = self.ui["macro_choice"].get_selected()
        return names[index] if 0 <= index < len(names) else ""

    def _open_macro(self):
        name = self._selected_macro()
        if not name:
            self._toast("Choose a saved macro first.")
            return
        ok, message = self.service.load_macro(name)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            self.ui["macro_name"].set_text(name)
            self._refresh_macro_page(force=True)

    def _save_macro(self):
        ok, message = self.service.save_macro(self.ui["macro_name"].get_text())
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            self._refresh_macro_files()
            self._refresh_macro_page(force=True)

    def _delete_macro(self):
        name = self._selected_macro()
        if not name:
            return
        ok, message = self.service.delete_macro(name)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            self._refresh_macro_files()
            self._refresh_macro_page(force=True)

    def _macro_file_filter(self):
        filters = Gio.ListStore.new(Gtk.FileFilter)
        macro_filter = Gtk.FileFilter()
        macro_filter.set_name("WXB macros (*.wxbmacro.json)")
        macro_filter.add_pattern("*.wxbmacro.json")
        macro_filter.add_pattern("*.json")
        filters.append(macro_filter)
        return filters, macro_filter

    def _import_macro(self):
        dialog = Gtk.FileDialog(title="Import a macro")
        filters, macro_filter = self._macro_file_filter()
        dialog.set_filters(filters)
        dialog.set_default_filter(macro_filter)
        dialog.open(self, None, self._macro_import_chosen)

    def _macro_import_chosen(self, dialog, result):
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return
        path = selected.get_path() if selected is not None else None
        if not path:
            return
        ok, message = self.service.import_macro(path)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            snap = self.service.macro_snapshot()
            self.ui["macro_name"].set_text(snap["name"])
            self._refresh_macro_files()
            self._refresh_macro_page(force=True)

    def _export_macro(self):
        if self.service.macro_snapshot()["steps"] == 0:
            self._toast("Record or open a macro first.")
            return
        dialog = Gtk.FileDialog(title="Export a macro")
        filters, macro_filter = self._macro_file_filter()
        dialog.set_filters(filters)
        dialog.set_default_filter(macro_filter)
        dialog.save(self, None, self._macro_export_chosen)

    def _macro_export_chosen(self, dialog, result):
        try:
            selected = dialog.save_finish(result)
        except GLib.Error:
            return
        path = selected.get_path() if selected is not None else None
        if not path:
            return
        ok, message = self.service.export_macro(path)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)

    def _macro_step_text(self, index, event):
        at = float(event.get("at") or 0.0)
        kind = str(event.get("type") or "")
        if kind == "mouse_move":
            detail = "move %s, %s" % (event.get("x"), event.get("y"))
        elif kind == "mouse_button":
            detail = "%s %s" % (event.get("button"),
                                "down" if event.get("pressed") else "up")
        elif kind == "mouse_scroll":
            detail = "scroll %s, %s" % (event.get("dx"), event.get("dy"))
        else:
            detail = "%s %s" % (event.get("key"),
                                "down" if event.get("pressed") else "up")
        return "%d  %7.3fs  %s" % (index + 1, at, detail)

    def _refresh_macro_steps(self, force=False):
        if "macro_steps" not in self.ui:
            return
        steps = self.service.list_macro_steps()
        sig = (len(steps), tuple((str(event.get("type")),
                                  round(float(event.get("at") or 0), 3),
                                  str(event.get("key") or event.get("button") or ""))
                                 for event in steps))
        if not force and sig == self._macro_steps_sig:
            return
        self._macro_steps_sig = sig
        selected = self._macro_selected
        clear(self.ui["macro_steps"])
        for index, event in enumerate(steps):
            row = Gtk.ListBoxRow()
            row.set_child(label(self._macro_step_text(index, event),
                                "wxb-mono"))
            row.set_tooltip_text("Macro step %d" % (index + 1))
            row._macro_index = index
            self.ui["macro_steps"].append(row)
        if 0 <= selected < len(steps):
            row = self.ui["macro_steps"].get_row_at_index(selected)
            if row is not None:
                self.ui["macro_steps"].select_row(row)
        else:
            self._macro_selected = -1
            self.ui["macro_step_delay"].set_value(0)
            self.ui["macro_step_detail"].set_text(
                "Select a step to inspect it." if steps else "No steps yet.")
        self.ui["macro_step_apply"].set_sensitive(bool(steps))
        self.ui["macro_step_delete"].set_sensitive(bool(steps))

    def _macro_step_selected(self, _list, row):
        if row is None or not hasattr(row, "_macro_index"):
            self._macro_selected = -1
            return
        index = int(row._macro_index)
        steps = self.service.list_macro_steps()
        if not 0 <= index < len(steps):
            return
        self._macro_selected = index
        event = steps[index]
        self.ui["macro_step_delay"].set_value(float(event.get("at") or 0.0))
        self.ui["macro_step_detail"].set_text(self._macro_step_text(index, event))

    def _apply_macro_step(self):
        index = self._macro_selected
        steps = self.service.list_macro_steps()
        if not 0 <= index < len(steps):
            self._set_error(self.ui["macro_error"], "Select a macro step first.")
            return
        event = dict(steps[index])
        event["at"] = float(self.ui["macro_step_delay"].get_value())
        ok, message = self.service.replace_macro_step(index, event)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            self._refresh_macro_page(force=True)

    def _delete_macro_step(self):
        index = self._macro_selected
        if index < 0:
            self._set_error(self.ui["macro_error"], "Select a macro step first.")
            return
        ok, message = self.service.delete_macro_step(index)
        self._toast(message)
        self._set_error(self.ui["macro_error"], "" if ok else message)
        if ok:
            self._macro_selected = -1
            self._refresh_macro_page(force=True)

    def _refresh_macro_page(self, force=False, snap=None):
        if "macro_status" not in self.ui:
            return
        if snap is None:
            snap = self.service.macro_snapshot()
        caps = snap.get("capabilities") or {}
        active = bool(snap.get("running"))
        if snap.get("error"):
            state, text = "bad", "ERROR"
        elif snap.get("recording"):
            state, text = "live", "RECORDING"
        elif snap.get("playing"):
            state, text = "live", "PLAYING"
        elif snap.get("steps"):
            state, text = "", "READY"
        else:
            state, text = "", "IDLE"
        set_pill(self.ui["macro_status"], text, state)
        loop_labels = {"once": "once", "count": "%d times" % snap.get("repeat"),
                       "continuous": "continuous"}
        detail = "%s · %d steps · %.2fs · %s" % (
            snap.get("name") or "No macro", int(snap.get("steps") or 0),
            float(snap.get("duration") or 0.0),
            loop_labels.get(snap.get("loop_mode"), "once"))
        if snap.get("playing"):
            detail += " · loop %d, step %d" % (
                int(snap.get("loop_done") or 0), int(snap.get("step") or 0))
        self.ui["macro_detail"].set_text(detail)
        self.ui["macro_capability"].set_text(
            caps.get("reason") or "X11 recording and playback available.")
        recording = bool(snap.get("recording"))
        playing = bool(snap.get("playing"))
        self.ui["macro_record"].set_sensitive(
            bool(caps.get("record")) and (recording or not active))
        self.ui["macro_play"].set_sensitive(
            bool(caps.get("play")) and not recording)
        self.ui["macro_stop"].set_sensitive(active)
        if recording:
            action_button_text(self.ui["macro_record"], "Stop recording",
                               "media-playback-stop-symbolic")
        elif playing:
            action_button_text(self.ui["macro_record"], "Record",
                               "media-record-symbolic")
        else:
            action_button_text(self.ui["macro_record"], "Record",
                               "media-record-symbolic")
        action_button_text(self.ui["macro_play"],
                           "Stop playback" if playing else "Play",
                           "media-playback-stop-symbolic" if playing
                           else "media-playback-start-symbolic")
        self.ui["macro_save"].set_sensitive(bool(snap.get("steps")) and not active)
        self.ui["macro_import"].set_sensitive(not active)
        self.ui["macro_open"].set_sensitive(
            bool(snap.get("files")) and not active)
        self.ui["macro_delete"].set_sensitive(
            bool(snap.get("files")) and not active)
        self.ui["macro_export"].set_sensitive(bool(snap.get("steps")))
        self.ui["macro_step_apply"].set_sensitive(
            bool(snap.get("steps")) and not active)
        self.ui["macro_step_delete"].set_sensitive(
            bool(snap.get("steps")) and not active)
        self._set_error(self.ui["macro_error"], snap.get("error", ""))
        self._set_error(self.ui["macro_notice"], snap.get("notice", ""))
        self.ui["macro_notice"].remove_css_class("wxb-bad")
        if force:
            self._refresh_macro_files()
        self._refresh_macro_steps(force=force)
        self._sync_macro_hotkey_hint()

    # --------------------------------------------------------------- recorder
    def _recorder(self):
        """Screen capture page.  FFmpeg stays in the service process tree."""
        self._recorder_syncing = False
        self._recorder_audio_sig = None
        self._rec_encoder_sig = None
        self._rec_mic_values = [""]
        self._rec_desktop_values = [""]
        caps = self.service.recorder_capabilities()
        snap = self.service.recorder_snapshot()

        self.ui["rec_status"] = label("IDLE", "wxb-pill")
        self.ui["rec_indicator"] = label("●", "wxb-mono wxb-hint")
        self.ui["rec_detail"] = label(
            "No recording is running.", "wxb-hint", wrap=True, max_width=66)
        self.ui["rec_capability"] = label(
            caps.get("reason") or "Local X11 screen capture via FFmpeg.",
            "wxb-hint", wrap=True, max_width=66)
        self.ui["rec_privacy"] = label(
            "Privacy: recordings are local files and can contain everything "
            "visible on screen, including passwords, notifications and "
            "private messages. WXB never uploads them. Delete recordings you "
            "no longer need; the default folder is private to your account.",
            "wxb-hint wxb-bad", wrap=True, max_width=66)

        self.ui["rec_source"] = Gtk.DropDown.new_from_strings(
            ["Screen", "Window", "Region"])
        self.ui["rec_source"].add_css_class("wxb-drop")
        self.ui["rec_source"].connect(
            "notify::selected", lambda *_: self._recorder_source_changed())
        self.ui["rec_window_id"] = Gtk.Entry()
        self.ui["rec_window_id"].set_placeholder_text("X11 window ID")
        self.ui["rec_window_id"].add_css_class("wxb-entry")
        self.ui["rec_active_window"] = button(
            "Active window", icon_name="window-new-symbolic", css="wxb-quiet",
            tooltip="Read the active X11 window ID",
            on_click=self._recorder_use_active_window)
        self.ui["rec_window_box"] = el(
            self._mini_field("Window", el(self.ui["rec_window_id"],
                                          self.ui["rec_active_window"],
                                          vertical=False, spacing=6)),
            spacing=0)
        self.ui["rec_region_x"] = Gtk.SpinButton.new_with_range(0, 10000, 1)
        self.ui["rec_region_y"] = Gtk.SpinButton.new_with_range(0, 10000, 1)
        self.ui["rec_region_w"] = Gtk.SpinButton.new_with_range(0, 10000, 1)
        self.ui["rec_region_h"] = Gtk.SpinButton.new_with_range(0, 10000, 1)
        for widget in (self.ui["rec_region_x"], self.ui["rec_region_y"],
                       self.ui["rec_region_w"], self.ui["rec_region_h"]):
            widget.add_css_class("wxb-spin")
            widget.connect("value-changed", lambda *_: self._recorder_changed())
        self.ui["rec_region_box"] = el(
            el(self._mini_field("X", self.ui["rec_region_x"]),
               self._mini_field("Y", self.ui["rec_region_y"]),
               self._mini_field("Width", self.ui["rec_region_w"]),
               self._mini_field("Height", self.ui["rec_region_h"]),
               vertical=False, spacing=8), spacing=0)

        self._rec_resolution_values = ["source", "720p", "1080p", "1440p"]
        self.ui["rec_resolution"] = Gtk.DropDown.new_from_strings(
            ["Source", "720p", "1080p", "1440p"])
        self.ui["rec_resolution"].add_css_class("wxb-drop")
        self.ui["rec_fps"] = Gtk.DropDown.new_from_strings(
            ["15 FPS", "30 FPS", "60 FPS"])
        self.ui["rec_fps"].add_css_class("wxb-drop")
        self._rec_fps_values = [15, 30, 60]
        self.ui["rec_quality"] = Gtk.DropDown.new_from_strings(
            ["Low · fastest", "Balanced", "High · largest"])
        self.ui["rec_quality"].add_css_class("wxb-drop")
        self._rec_quality_values = ["low", "balanced", "high"]
        self._sync_recorder_encoders(caps, snap["settings"].get("encoder"))
        for widget in (self.ui["rec_resolution"], self.ui["rec_fps"],
                       self.ui["rec_quality"], self.ui["rec_encoder"]):
            widget.connect("notify::selected", lambda *_: self._recorder_changed())
        self.ui["rec_cursor"] = switch()
        self.ui["rec_cursor"].connect(
            "notify::active", lambda *_: self._recorder_changed())
        self.ui["rec_dir"] = Gtk.Entry()
        self.ui["rec_dir"].set_hexpand(True)
        self.ui["rec_dir"].set_placeholder_text("Recording folder")
        self.ui["rec_dir"].add_css_class("wxb-entry")
        self.ui["rec_dir"].connect(
            "activate", lambda *_: self._recorder_changed())
        rec_dir_focus = Gtk.EventControllerFocus()
        rec_dir_focus.connect("leave", lambda *_: self._recorder_changed())
        self.ui["rec_dir"].add_controller(rec_dir_focus)
        rec_window_focus = Gtk.EventControllerFocus()
        rec_window_focus.connect("leave", lambda *_: self._recorder_changed())
        self.ui["rec_window_id"].add_controller(rec_window_focus)
        self.ui["rec_browse"] = button(
            "Choose…", icon_name="folder-open-symbolic", css="wxb-quiet",
            on_click=self._choose_recording_folder)
        self.ui["rec_open"] = button(
            "Open folder", icon_name="folder-symbolic", css="wxb-quiet",
            tooltip="Open the recording folder in your file manager",
            on_click=self._open_recording_folder)
        self.ui["rec_dir_hint"] = label(
            "New timestamped .mkv files; existing files are never overwritten.",
            "wxb-hint", wrap=True, max_width=64)
        self.ui["rec_legacy_note"] = label(
            "Older recordings are preserved separately: %s" %
            LEGACY_RECORDINGS_DIR, "wxb-hint", wrap=True, max_width=64)
        self.ui["rec_legacy_open"] = button(
            "Open older recordings", icon_name="document-open-recent-symbolic",
            css="wxb-quiet",
            tooltip="Open the preserved recordings folder from before the permission repair",
            on_click=self._open_legacy_recordings)
        legacy_visible = LEGACY_RECORDINGS_DIR.is_dir()
        self.ui["rec_legacy_note"].set_visible(legacy_visible)
        self.ui["rec_legacy_open"].set_visible(legacy_visible)
        self.ui["rec_mic"] = switch()
        self.ui["rec_mic"].connect(
            "notify::active", lambda *_: self._recorder_changed())
        self.ui["rec_desktop_audio"] = switch()
        self.ui["rec_desktop_audio"].connect(
            "notify::active", lambda *_: self._recorder_changed())
        self.ui["rec_mic_device"] = Gtk.DropDown.new_from_strings(["Default"])
        self.ui["rec_mic_device"].add_css_class("wxb-drop")
        self.ui["rec_desktop_device"] = Gtk.DropDown.new_from_strings(["Default"])
        self.ui["rec_desktop_device"].add_css_class("wxb-drop")
        for widget in (self.ui["rec_mic_device"],
                       self.ui["rec_desktop_device"]):
            widget.connect("notify::selected", lambda *_: self._recorder_changed())
        self._refresh_recorder_devices(force=True)

        self.ui["rec_start"] = button(
            "Start recording", icon_name="media-record-symbolic",
            css="wxb-primary",
            tooltip="Start local screen capture; never starts automatically",
            on_click=self._recorder_start)
        self.ui["rec_pause"] = button(
            "Pause", icon_name="media-playback-pause-symbolic",
            css="wxb-quiet", tooltip="Pause or resume capture",
            on_click=self._recorder_pause)
        self.ui["rec_stop"] = button(
            "Stop & save", icon_name="media-playback-stop-symbolic",
            css="wxb-danger", tooltip="Stop and finalize the Matroska file",
            on_click=self._recorder_stop)
        self.ui["rec_hotkey"] = label("", "wxb-mono", wrap=True, max_width=66)
        self.ui["rec_perf"] = label("", "wxb-hint", wrap=True, max_width=66)
        self.ui["rec_output"] = label("", "wxb-mono", wrap=True,
                                      max_width=66, selectable=True)
        self.ui["rec_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                     max_width=66)
        self.ui["rec_error"].set_visible(False)
        self.ui["rec_notice"] = label("", "wxb-hint", wrap=True,
                                      max_width=66)
        self.ui["rec_notice"].set_visible(False)

        source_card = card(
            self._section_head("Capture source",
                               "Screen, one X11 window, or a positive region."),
            self._mini_field("Source", self.ui["rec_source"]),
            self.ui["rec_window_box"],
            self.ui["rec_region_box"],
            el(self._mini_field("Resolution", self.ui["rec_resolution"]),
               self._mini_field("Frame rate", self.ui["rec_fps"]),
               self._mini_field("Quality / load", self.ui["rec_quality"]),
               vertical=False, spacing=8),
            el(self._mini_field("Encoder", self.ui["rec_encoder"]),
               el(label("Show cursor", "wxb-label"), self.ui["rec_cursor"],
                  vertical=False, spacing=8),
               vertical=False, spacing=12),
            self.ui["rec_dir"],
            el(self.ui["rec_browse"], self.ui["rec_open"],
               vertical=False, spacing=8),
            self.ui["rec_dir_hint"],
            el(self.ui["rec_legacy_note"], self.ui["rec_legacy_open"],
               vertical=True, spacing=6),
            spacing=12,
        )
        actions_card = card(
            self._section_head("Recording",
                               "Capture runs in the service, not the UI thread."),
            el(self.ui["rec_indicator"], self.ui["rec_status"],
               self.ui["rec_detail"], vertical=False, spacing=10),
            el(self.ui["rec_start"], self.ui["rec_pause"],
               self.ui["rec_stop"], vertical=False, spacing=8),
            self.ui["rec_hotkey"], self.ui["rec_output"],
            self.ui["rec_notice"], self.ui["rec_error"],
            self.ui["rec_perf"],
            spacing=12,
        )
        audio_card = card(
            self._section_head("Audio",
                               "Optional. Separate microphone and desktop toggles."),
            el(label("Microphone", "wxb-label"), self.ui["rec_mic"],
               vertical=False, spacing=8),
            self.ui["rec_mic_device"],
            el(label("Desktop audio", "wxb-label"),
               self.ui["rec_desktop_audio"], vertical=False, spacing=8),
            self.ui["rec_desktop_device"],
            label("Audio is muxed as AAC into the same local file. "
                  "Device access is granted by Pulse/PipeWire; WXB never "
                  "requests administrator access.", "wxb-hint", wrap=True,
                  max_width=64),
            spacing=12,
        )
        self.ui["rec_capability_card"] = card(
            self._section_head("Backend and limits",
                               "Detection is explicit; unsupported controls disable."),
            self.ui["rec_capability"], self.ui["rec_privacy"], spacing=10)
        self._sync_recorder_controls(snap, force=True)
        return self._scroll(
            self._page_head("STUDIO / RECORDER", "Recorder",
                            "Capture the screen and keep it local. An "
                            "OBS-inspired recorder with explicit backend, "
                            "performance and privacy limits."),
            source_card, actions_card, audio_card, self.ui["rec_capability_card"],
        )

    def _sync_recorder_encoders(self, caps, selected="auto"):
        values = ["auto", "cpu"]
        labels = ["Auto · CPU fallback", "CPU · libx264"]
        for name, label_text in (("vaapi", "VAAPI · hardware"),
                                 ("nvenc", "NVENC · hardware")):
            if name in (caps.get("hardware_encoders") or []):
                values.append(name)
                labels.append(label_text)
        if selected in values:
            chosen = selected
        else:
            chosen = "auto"
        sig = tuple(values)
        if ("rec_encoder" in self.ui
                and getattr(self, "_rec_encoder_sig", None) == sig):
            return
        self._rec_encoder_sig = sig
        self._rec_encoder_values = values
        self.ui["rec_encoder"] = Gtk.DropDown.new_from_strings(labels)
        self.ui["rec_encoder"].add_css_class("wxb-drop")
        if "rec_encoder" in self.ui:
            self.ui["rec_encoder"].set_selected(values.index(chosen))
            self.ui["rec_encoder"].connect(
                "notify::selected", lambda *_: self._recorder_changed())

    def _refresh_recorder_devices(self, force=False):
        if "rec_mic_device" not in self.ui:
            return
        devices = self.service.recorder_devices(refresh=force)
        sig = (tuple(devices.get("microphones") or []),
               tuple(devices.get("desktop") or []))
        if not force and sig == self._recorder_audio_sig:
            return
        self._recorder_audio_sig = sig
        old_mic = self._rec_mic_values[self.ui["rec_mic_device"].get_selected()] \
            if self._rec_mic_values and self.ui["rec_mic_device"].get_selected() >= 0 else ""
        old_desktop = (self._rec_desktop_values[
            self.ui["rec_desktop_device"].get_selected()]
            if self._rec_desktop_values
            and self.ui["rec_desktop_device"].get_selected() >= 0 else "")
        self._rec_mic_values = [""] + list(devices.get("microphones") or [])
        self._rec_desktop_values = [""] + list(devices.get("desktop") or [])
        self.ui["rec_mic_device"].set_model(Gtk.StringList.new(
            ["Default source"] + (devices.get("microphones") or [])))
        self.ui["rec_desktop_device"].set_model(Gtk.StringList.new(
            ["Default monitor"] + (devices.get("desktop") or [])))
        self.ui["rec_mic_device"].set_selected(
            self._rec_mic_values.index(old_mic) if old_mic in self._rec_mic_values else 0)
        self.ui["rec_desktop_device"].set_selected(
            self._rec_desktop_values.index(old_desktop)
            if old_desktop in self._rec_desktop_values else 0)

    def _recorder_source_changed(self):
        if getattr(self, "_recorder_syncing", False):
            return
        self._recorder_changed()
        self._sync_recorder_visibility()

    def _sync_recorder_visibility(self):
        if "rec_source" not in self.ui:
            return
        source = SOURCE_CHOICES[self.ui["rec_source"].get_selected()] \
            if 0 <= self.ui["rec_source"].get_selected() < len(SOURCE_CHOICES) else "screen"
        self.ui["rec_window_box"].set_visible(source == "window")
        self.ui["rec_region_box"].set_visible(source == "region")
        caps = self.service.recorder_capabilities()
        self.ui["rec_source"].set_sensitive(bool(caps.get("record")))

    def _recorder_values(self) -> dict:
        source_i = self.ui["rec_source"].get_selected()
        resolution_i = self.ui["rec_resolution"].get_selected()
        fps_i = self.ui["rec_fps"].get_selected()
        quality_i = self.ui["rec_quality"].get_selected()
        encoder_i = self.ui["rec_encoder"].get_selected()
        mic_i = self.ui["rec_mic_device"].get_selected()
        desk_i = self.ui["rec_desktop_device"].get_selected()
        return {
            "source": (SOURCE_CHOICES[source_i] if 0 <= source_i < len(SOURCE_CHOICES)
                       else "screen"),
            "region_x": int(self.ui["rec_region_x"].get_value()),
            "region_y": int(self.ui["rec_region_y"].get_value()),
            "region_w": int(self.ui["rec_region_w"].get_value()),
            "region_h": int(self.ui["rec_region_h"].get_value()),
            "window_id": self.ui["rec_window_id"].get_text().strip(),
            "resolution": (self._rec_resolution_values[resolution_i]
                           if 0 <= resolution_i < len(self._rec_resolution_values)
                           else "source"),
            "fps": (self._rec_fps_values[fps_i]
                    if 0 <= fps_i < len(self._rec_fps_values) else 30),
            "quality": (self._rec_quality_values[quality_i]
                        if 0 <= quality_i < len(self._rec_quality_values)
                        else "balanced"),
            "encoder": (self._rec_encoder_values[encoder_i]
                        if 0 <= encoder_i < len(self._rec_encoder_values)
                        else "auto"),
            "cursor": bool(self.ui["rec_cursor"].get_active()),
            "save_dir": self.ui["rec_dir"].get_text().strip(),
            "mic": bool(self.ui["rec_mic"].get_active()),
            "mic_device": (self._rec_mic_values[mic_i]
                           if 0 <= mic_i < len(self._rec_mic_values) else ""),
            "desktop_audio": bool(self.ui["rec_desktop_audio"].get_active()),
            "desktop_device": (self._rec_desktop_values[desk_i]
                               if 0 <= desk_i < len(self._rec_desktop_values)
                               else ""),
        }

    def _recorder_changed(self):
        if getattr(self, "_recorder_syncing", False) or "rec_start" not in self.ui:
            return
        snap = self.service.recorder_snapshot()
        if snap.get("running"):
            return
        ok, message = self.service.set_recorder_settings(**self._recorder_values())
        self._set_error(self.ui["rec_error"], "" if ok else message)
        self._sync_recorder_visibility()
        self._sync_recorder_controls(self.service.recorder_snapshot(), force=True)

    def _recorder_use_active_window(self):
        window_id = self.service.recorder_active_window()
        if not window_id:
            self._set_error(self.ui["rec_error"],
                            "Could not read the active X11 window.")
            return
        self.ui["rec_window_id"].set_text(window_id)
        self.ui["rec_source"].set_selected(1)
        self._recorder_changed()

    def _choose_recording_folder(self):
        dialog = Gtk.FileDialog(title="Choose a recording folder")
        dialog.select_folder(self, None, self._recording_folder_chosen)

    def _recording_folder_chosen(self, dialog, result):
        try:
            selected = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        path = selected.get_path() if selected is not None else None
        if path:
            self.ui["rec_dir"].set_text(path)
            self._recorder_changed()

    def _open_recording_folder(self):
        """Open the current local folder with the desktop file manager.

        Recording does not need administrator access, and viewing the files
        should not either.  Create the folder as the desktop user if this is
        the first recording, then hand the URI to the normal xdg handler.
        """
        raw = self.ui["rec_dir"].get_text().strip()
        path = Path(raw).expanduser() if raw else Path.home() / "Videos" / "WXB"
        if not path.is_absolute():
            path = Path.home() / path
        self._open_folder_path(path, create=True)

    def _open_legacy_recordings(self):
        self._open_folder_path(LEGACY_RECORDINGS_DIR)

    def _open_folder_path(self, path: Path, create=False):
        if create:
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                self._set_error(self.ui["rec_error"],
                                "Could not create the recording folder: %s" % exc)
                return
        if not path.is_dir():
            self._set_error(self.ui["rec_error"],
                            "The recording folder does not exist.")
            return
        try:
            opened = _launch_desktop_uri(path.resolve().as_uri())
        except OSError as exc:
            self._set_error(self.ui["rec_error"],
                            "Could not open the recording folder: %s" % exc)
            return
        if not opened:
            self._set_error(self.ui["rec_error"],
                            "No file manager is available to open the recording folder.")
            return
        self._toast("Opened %s" % path)

    def _recorder_start(self):
        snap = self.service.recorder_snapshot()
        if snap.get("running"):
            self._recorder_stop()
            return
        self._recorder_changed()
        ok, message = self.service.start_recording()
        self._toast(message)
        self._set_error(self.ui["rec_error"], "" if ok else message)
        self._refresh()

    def _recorder_stop(self):
        # Finalization is bounded in the service's monitor thread; the GTK
        # thread must not wait up to the process grace period.
        ok, message = self.service.stop_recording(wait=False)
        self._toast(message)
        self._set_error(self.ui["rec_error"], "" if ok else message)
        self._refresh()

    def _recorder_pause(self):
        snap = self.service.recorder_snapshot()
        if snap.get("paused"):
            ok, message = self.service.resume_recording()
        else:
            ok, message = self.service.pause_recording()
        self._toast(message)
        self._set_error(self.ui["rec_error"], "" if ok else message)
        self._refresh()

    @staticmethod
    def _recorder_elapsed_text(seconds):
        total = max(0, int(float(seconds or 0)))
        return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60,
                                    total % 60)

    def _sync_recorder_controls(self, snap, force=False):
        if "rec_status" not in self.ui:
            return
        settings = snap.get("settings") or {}
        caps = snap.get("capabilities") or self.service.recorder_capabilities()
        self._recorder_syncing = True
        try:
            try:
                self.ui["rec_source"].set_selected(
                    SOURCE_CHOICES.index(settings.get("source", "screen")))
            except ValueError:
                self.ui["rec_source"].set_selected(0)
            if not self.ui["rec_window_id"].has_focus():
                self.ui["rec_window_id"].set_text(settings.get("window_id", ""))
            for key in ("region_x", "region_y", "region_w", "region_h"):
                self.ui["rec_" + key].set_value(int(settings.get(key, 0) or 0))
            try:
                self.ui["rec_resolution"].set_selected(
                    self._rec_resolution_values.index(settings.get("resolution", "source")))
            except ValueError:
                self.ui["rec_resolution"].set_selected(0)
            try:
                self.ui["rec_fps"].set_selected(
                    self._rec_fps_values.index(int(settings.get("fps", 30))))
            except ValueError:
                self.ui["rec_fps"].set_selected(1)
            try:
                self.ui["rec_quality"].set_selected(
                    self._rec_quality_values.index(settings.get("quality", "balanced")))
            except ValueError:
                self.ui["rec_quality"].set_selected(1)
            if force:
                self._sync_recorder_encoders(caps, settings.get("encoder", "auto"))
                self._refresh_recorder_devices(force=False)
            try:
                self.ui["rec_encoder"].set_selected(
                    self._rec_encoder_values.index(settings.get("encoder", "auto")))
            except ValueError:
                self.ui["rec_encoder"].set_selected(0)
            self.ui["rec_cursor"].set_active(bool(settings.get("cursor", True)))
            if not self.ui["rec_dir"].has_focus():
                self.ui["rec_dir"].set_text(str(settings.get("save_dir") or ""))
            self.ui["rec_mic"].set_active(bool(settings.get("mic", False)))
            self.ui["rec_desktop_audio"].set_active(
                bool(settings.get("desktop_audio", False)))
            mic = settings.get("mic_device") or ""
            desktop = settings.get("desktop_device") or ""
            self.ui["rec_mic_device"].set_selected(
                self._rec_mic_values.index(mic) if mic in self._rec_mic_values else 0)
            self.ui["rec_desktop_device"].set_selected(
                self._rec_desktop_values.index(desktop)
                if desktop in self._rec_desktop_values else 0)
        finally:
            self._recorder_syncing = False
        self._sync_recorder_visibility()
        running = bool(snap.get("running"))
        paused = bool(snap.get("paused"))
        starting = bool(snap.get("starting"))
        if snap.get("error"):
            state, text = "bad", "ERROR"
        elif snap.get("recording"):
            state, text = "live", "RECORDING"
        elif paused:
            state, text = "live", "PAUSED"
        elif starting:
            state, text = "live", "WAITING"
        else:
            state, text = "", "READY" if caps.get("record") else "UNAVAILABLE"
        set_pill(self.ui["rec_status"], text, state)
        self.ui["rec_indicator"].set_text("●" if running else "○")
        self.ui["rec_indicator"].remove_css_class("wxb-good")
        self.ui["rec_indicator"].remove_css_class("wxb-bad")
        if snap.get("error"):
            self.ui["rec_indicator"].add_css_class("wxb-bad")
        elif running:
            self.ui["rec_indicator"].add_css_class("wxb-good")
        detail = "%s · %s" % (self._recorder_elapsed_text(snap.get("elapsed")),
                               caps.get("backend") or "no backend")
        if paused:
            detail += " · paused"
        if starting:
            detail += " · waiting for portal consent"
        self.ui["rec_detail"].set_text(detail)
        self.ui["rec_capability"].set_text(
            caps.get("reason") or
            "%s · FFmpeg x11grab · pulse audio %s · CPU fallback available" % (
                caps.get("session", "unknown").upper(),
                "on" if caps.get("audio") else "off"))
        self.ui["rec_start"].set_sensitive(bool(caps.get("record")) or running)
        action_button_text(self.ui["rec_start"],
                           "Stop & save" if running else "Start recording",
                           "media-playback-stop-symbolic" if running
                           else "media-record-symbolic")
        self.ui["rec_pause"].set_sensitive(running)
        action_button_text(self.ui["rec_pause"],
                           "Resume" if paused else "Pause",
                           "media-playback-start-symbolic" if paused
                           else "media-playback-pause-symbolic")
        self.ui["rec_stop"].set_sensitive(running)
        for widget in (self.ui["rec_source"], self.ui["rec_window_id"],
                       self.ui["rec_active_window"], self.ui["rec_region_x"],
                       self.ui["rec_region_y"], self.ui["rec_region_w"],
                       self.ui["rec_region_h"], self.ui["rec_resolution"],
                       self.ui["rec_fps"], self.ui["rec_quality"],
                       self.ui["rec_encoder"], self.ui["rec_cursor"],
                       self.ui["rec_dir"], self.ui["rec_browse"],
                       self.ui["rec_mic"], self.ui["rec_mic_device"],
                       self.ui["rec_desktop_audio"],
                       self.ui["rec_desktop_device"]):
            widget.set_sensitive(bool(caps.get("record")) and not running
                                 and not starting)
        self.ui["rec_source"].set_sensitive(
            bool(caps.get("record")) and not running and not starting)
        self.ui["rec_mic"].set_sensitive(
            bool(caps.get("record")) and not running and not starting
            and bool(caps.get("audio", True)))
        self.ui["rec_desktop_audio"].set_sensitive(
            bool(caps.get("record")) and not running and not starting
            and bool(caps.get("audio", True)))
        for widget in (self.ui["rec_mic_device"],
                       self.ui["rec_desktop_device"]):
            widget.set_sensitive(
                bool(caps.get("record")) and not running and not starting
                and bool(caps.get("audio", True)))
        self.ui["rec_output"].set_text(
            ("File: " + str(snap.get("output"))) if snap.get("output")
            else "No recording file yet.")
        probe = snap.get("probe") or {}
        if probe.get("duration"):
            self.ui["rec_output"].set_text(
                "%s · %sx%s · %s fps · %.1fs" % (
                    snap.get("output"), probe.get("width"), probe.get("height"),
                    probe.get("fps"), float(probe.get("duration"))))
        self._set_error(self.ui["rec_error"], snap.get("error", ""))
        self._set_error(self.ui["rec_notice"], snap.get("notice", ""))
        hotkey = display_hotkey(self.service.settings["hotkeys"].get(
            "stop_recording", "")) or "not set"
        conflicts = [HOTKEY_LABELS.get(key, key) for key, value
                     in self.service.settings["hotkeys"].items()
                     if key != "stop_recording"
                     and normalize_hotkey(value) == normalize_hotkey(
                         self.service.settings["hotkeys"].get("stop_recording", ""))]
        self.ui["rec_hotkey"].set_text(
            "Global stop: " + hotkey + " · F9 / Esc stop everything"
            + (" · CONFLICT with %s; rebind in Settings" % ", ".join(conflicts)
               if conflicts else ""))
        fps = int(settings.get("fps", 30) or 30)
        performance = (
            "%d FPS at %s, %s quality. Lower FPS or use a lower preset if "
            "the desktop or encoder falls behind; hardware encoders are "
            "offered only when detected." % (
                fps, settings.get("resolution", "source"),
                settings.get("quality", "balanced")))
        if (settings.get("encoder") in ("auto", "cpu")
                and (fps >= 60 or settings.get("resolution") in ("1080p", "1440p")
                     or settings.get("quality") == "high")):
            performance += (" This CPU setting is a high-load choice; the preview "
                            "or desktop may stutter if the processor cannot keep up.")
        self.ui["rec_perf"].set_text(performance)

    def _refresh_recorder_page(self, snap=None):
        if "rec_status" not in self.ui:
            return
        self._sync_recorder_controls(
            snap if snap is not None else self.service.recorder_snapshot())

    # ---------------------------------------------------------------- settings
    def _settings(self):
        accent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._swatches = {}
        for name, hex_value in ACCENTS.items():
            b = Gtk.Button()
            b.set_size_request(28, 28)
            b.add_css_class("wxb-swatch")
            b.add_css_class(name)
            b.set_tooltip_text(name.title())
            if self.service.settings.get("accent") == name:
                b.add_css_class("selected")
            b.connect("clicked", lambda _b, n=name: self._set_accent(n))
            self._swatches[name] = b
            accent_row.append(b)
        self.ui["accent_buttons"] = list(self._swatches.values())

        self.ui["monochrome"] = switch()
        self.ui["monochrome"].set_active(
            bool(self.service.settings.get("monochrome", False)))
        self.ui["monochrome"].set_halign(Gtk.Align.START)
        self.ui["monochrome"].set_tooltip_text(
            "Layered graphite and soft white. Colour accents are paused "
            "while this is on.")
        self.ui["monochrome"].connect(
            "notify::active", lambda *_: self._sync_monochrome())
        mono_copy = el(
            label("Monochrome", "wxb-label"),
            label("Layered graphite. Accent colour is paused.",
                  "wxb-hint", wrap=True, max_width=42),
            vertical=True, spacing=2, hexpand=True)
        mono_row = el(mono_copy, self.ui["monochrome"], vertical=False,
                      spacing=14)

        self.ui["opacity_value"] = label(
            "%d%%" % int(self.service.settings.get("opacity", 92)),
            "wxb-mono")
        self.ui["opacity"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 20, 100, 1)
        self.ui["opacity"].set_value(
            int(self.service.settings.get("opacity", 92)))
        self.ui["opacity"].set_draw_value(False)
        self.ui["opacity"].set_hexpand(True)
        self.ui["opacity"].add_css_class("wxb-scale")
        self.ui["opacity"].connect(
            "value-changed", lambda *_: self._sync_opacity())
        self.ui["nav_compact"] = switch()
        self.ui["nav_compact"].set_active(
            bool(self.service.settings.get("nav_compact")))
        self.ui["nav_compact"].set_halign(Gtk.Align.START)
        self.ui["nav_compact"].connect(
            "notify::active", lambda *_: self._sync_nav_compact())
        self.ui["always_on_top"] = switch()
        self.ui["always_on_top"].set_active(
            bool(self.service.settings.get("always_on_top", False)))
        self.ui["always_on_top"].set_halign(Gtk.Align.START)
        self.ui["always_on_top"].set_tooltip_text(
            "Keep WXB above other windows on X11")
        self.ui["always_on_top"].connect(
            "notify::active", lambda *_: self._sync_always_on_top())
        self.ui["always_on_top_hint"] = label("", "wxb-hint", wrap=True,
                                              max_width=52)
        self.ui["reset_appearance"] = button(
            "Reset appearance", icon_name="edit-undo-symbolic",
            css="wxb-ghost", on_click=self._reset_appearance)
        appearance = card(
            el(label("Appearance", "wxb-section-title"),
               label("A single accent, used sparingly.", "wxb-page-sub"),
               vertical=True, spacing=2, hexpand=True),
            el(label("Accent", "wxb-label"), accent_row, vertical=False,
               spacing=14, halign=Gtk.Align.START),
            mono_row,
            self._field("Window opacity", self.ui["opacity"],
                        self.ui["opacity_value"]),
            el(label("Navigation pane", "wxb-label"),
               self.ui["nav_compact"], vertical=False, spacing=12),
            el(label("Always on top", "wxb-label"),
               self.ui["always_on_top"], vertical=False, spacing=12),
            self.ui["always_on_top_hint"],
            self.ui["reset_appearance"],
            self._visual_section(),
            spacing=16,
        )

        self.ui["stop_on_close"] = switch()
        self.ui["stop_on_close"].set_active(
            bool(self.service.settings.get("stop_on_close", False)))
        self.ui["stop_on_close"].set_halign(Gtk.Align.START)
        self.ui["stop_on_close"].set_tooltip_text(
            "Stop the input engines and Cordial clients when WXB closes. "
            "Sober and your Roblox game stay open.")
        self.ui["stop_on_close"].connect(
            "notify::active", lambda *_: self._sync_stop_on_close())
        behavior = card(
            el(label("Behavior", "wxb-section-title"),
               label("Decide what closing WXB leaves running.",
                     "wxb-page-sub"),
               vertical=True, spacing=2),
            el(label("Stop engines on close", "wxb-label"),
               self.ui["stop_on_close"], vertical=False, spacing=12),
            label("Stops WXB's input engines and Cordial clients. Sober "
                  "and the Roblox session stay open.",
                  "wxb-hint", wrap=True, max_width=52),
            spacing=14,
        )

        # Keybindings live inside the existing Keyboard card; no extra
        # surface or visual treatment is introduced.
        hotkey_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                spacing=8)
        self.ui["hotkey_entries"] = {}
        self.ui["hotkey_set_buttons"] = {}
        for action, label_text in HOTKEY_LABELS.items():
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            entry.set_width_chars(18)
            entry.set_placeholder_text("e.g. Ctrl+Shift+F8")
            entry.set_text(display_hotkey(
                self.service.settings["hotkeys"].get(action, "")))
            self.ui["hotkey_entries"][action] = entry
            capture = button("Set", icon_name="input-keyboard-symbolic",
                             css="wxb-quiet",
                             tooltip="Capture a key combination",
                             on_click=lambda a=action: self._start_hotkey_capture(a))
            reset_one = button(icon_name="edit-undo-symbolic",
                               css="wxb-icon",
                               tooltip="Reset this binding",
                               on_click=lambda a=action: self._reset_hotkey(a))
            self.ui["hotkey_set_buttons"][action] = capture
            self.ui.setdefault("hotkey_reset_buttons", {})[action] = reset_one
            hotkey_fields.append(el(
                label(label_text, "wxb-label"), entry, capture, reset_one,
                vertical=False, spacing=8, css="wxb-row"))
        self.ui["hotkey_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                        max_width=52)
        self.ui["hotkey_error"].set_visible(False)
        self.ui["hotkey_reset"] = button(
            "Reset defaults", icon_name="edit-undo-symbolic",
            css="wxb-ghost", on_click=self._reset_hotkeys)
        self.ui["hotkey_apply"] = button(
            "Apply keybindings", icon_name="emblem-ok-symbolic",
            css="wxb-quiet", on_click=self._apply_hotkeys)
        shortcuts = card(
            el(label("Keyboard", "wxb-section-title"),
               label("Move without reaching for the mouse.", "wxb-page-sub"),
               vertical=True, spacing=2),
            self._shortcut_row("Ctrl K", "Search commands and pages"),
            self._shortcut_row("Ctrl 1 — 7", "Jump to a page"),
            self._shortcut_row("Esc", "Close search, or stop every engine"),
            label("Global keybindings", "wxb-label"),
            hotkey_fields,
            self.ui["hotkey_error"],
            el(self.ui["hotkey_reset"], self.ui["hotkey_apply"],
               vertical=False, spacing=8),
            label("Esc is reserved as the emergency stop. Set captures "
                  "the next combination you press.",
                  "wxb-hint", wrap=True, max_width=52),
            spacing=12,
        )

        # Saved configs live in the existing About card alongside the
        # settings path.
        self.ui["config_name"] = Gtk.Entry()
        self.ui["config_name"].set_placeholder_text("My config")
        self.ui["config_name"].set_hexpand(True)
        self.ui["config_save"] = button(
            "Save current", icon_name="document-save-symbolic",
            css="wxb-primary", on_click=self._save_config)
        self.ui["config_choice"] = Gtk.DropDown.new_from_strings(
            ["No saved configs"])
        self.ui["config_choice"].add_css_class("wxb-drop")
        self.ui["config_load"] = button(
            "Load", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._load_config)
        self.ui["config_rename"] = button(
            "Rename", icon_name="edit-rename-symbolic", css="wxb-ghost",
            on_click=self._rename_config)
        self.ui["config_delete"] = button(
            "Delete", icon_name="edit-delete-symbolic", css="wxb-ghost",
            on_click=self._delete_config)
        self.ui["config_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                        max_width=52)
        self.ui["config_error"].set_visible(False)
        self._refresh_configs()
        self.ui["settings_welcome"] = button(
            "Show welcome again", icon_name="view-refresh-symbolic",
            css="wxb-quiet", on_click=self._reset_onboarding)
        self.ui["portable_hint"] = label("", "wxb-hint", wrap=True)
        self.ui["window_geometry_hint"] = label("", "wxb-hint", wrap=True)
        about = card(
            el(label("About", "wxb-section-title"),
               label("WXB Studio " + STUDIO_VERSION, "wxb-page-sub"),
               vertical=True, spacing=2),
            self.ui["settings_welcome"],
            label("Settings live at", "wxb-label"),
            label(str(self.service.path), "wxb-mono", selectable=True),
            self.ui["portable_hint"],
            self.ui["window_geometry_hint"],
            label("Saved configs", "wxb-label"),
            el(self.ui["config_name"], self.ui["config_save"],
               vertical=False, spacing=8),
            el(self.ui["config_choice"], self.ui["config_load"],
               self.ui["config_rename"], self.ui["config_delete"],
               vertical=False, spacing=8),
            self.ui["config_error"],
            label("Engines · pynput · GTK 4 · libadwaita",
                  "wxb-hint", wrap=True),
            spacing=10,
        )
        return self._scroll(
            self._page_head("STUDIO / SETTINGS", "Settings",
                            "Make it yours. Small choices, kept in one quiet "
                            "place."),
            appearance, behavior, shortcuts, about,
        )

    def _shortcut_row(self, keys, text):
        return el(label(keys, "wxb-command-key"), label(text, "wxb-label"),
                  vertical=False, spacing=12, css="wxb-row")

    def _sync_opacity(self):
        if self.privacy:
            return
        if self.service.settings.get("monochrome"):
            self.set_opacity(1.0)
            return
        value = int(round(self.ui["opacity"].get_value()))
        self.ui["opacity_value"].set_text("%d%%" % value)
        self.service.settings["opacity"] = value
        self.set_opacity(value / 100.0)

    def _set_mono_switch(self, enabled):
        widget = self.ui.get("monochrome")
        if widget is None:
            return
        self._syncing_mono = True
        try:
            widget.set_active(bool(enabled))
        finally:
            self._syncing_mono = False

    def _apply_monochrome_controls(self, enabled):
        """Keep appearance controls honest while the mono theme is active."""
        enabled = bool(enabled)
        for swatch in getattr(self, "_swatches", {}).values():
            swatch.set_sensitive(not enabled)
        opacity = self.ui.get("opacity")
        if opacity is not None:
            opacity.set_sensitive(not enabled and not self.privacy)
        if hasattr(self, "backdrop"):
            self.backdrop.set_visible(not enabled)
        if enabled:
            # The window is deliberately opaque in mono, so make the control
            # show the value it is actually using without overwriting the
            # stored opacity to restore when the mode is switched off.
            opacity.set_value(100)
            self.set_opacity(1.0)
            if not self.privacy:
                self.ui["opacity_value"].set_text("100%")
        else:
            value = int(self.service.settings.get("opacity", 92))
            opacity.set_value(value)
            self.set_opacity(value / 100.0)
            if not self.privacy:
                self.ui["opacity_value"].set_text("%d%%" % value)

    def _sync_monochrome(self):
        if getattr(self, "_syncing_mono", False):
            return
        enabled = bool(self.ui["monochrome"].get_active())
        self.service.settings["monochrome"] = enabled
        self.service.save()
        self._apply_monochrome_controls(enabled)
        self.app.apply_appearance()
        self._toast("Monochrome on. Graphite and soft white." if enabled
                    else "Monochrome off. Colour restored.")

    def _sync_nav_compact(self):
        self.service.settings["nav_compact"] = bool(
            self.ui["nav_compact"].get_active())
        self.service.save()
        self._responsive()

    def _x11_window_id(self):
        surface = self.get_surface()
        try:
            from gi.repository import GdkX11
            if isinstance(surface, GdkX11.X11Surface):
                return int(surface.get_xid())
        except Exception:
            pass
        return None

    def _display_is_x11(self):
        try:
            from gi.repository import GdkX11
            return isinstance(Gdk.Display.get_default(), GdkX11.X11Display)
        except Exception:
            return False

    def _refresh_capability_hints(self):
        caps = x11_capabilities()
        is_x11 = self._display_is_x11()
        foreground_ok = bool(caps["foreground"] and is_x11)
        above_ok = bool(caps["above"] and is_x11)
        geometry_ok = bool(caps["geometry"] and is_x11)
        locked = (bool(self.ui["ac_lock"].get_active())
                  if "ac_lock" in self.ui else False)
        if "ac_filter_hint" in self.ui:
            if foreground_ok:
                self.ui["ac_filter_hint"].set_text(
                    "Foreground app detection · %s. The filter is checked "
                    "before every click." % caps["foreground_backend"])
            else:
                self.ui["ac_filter_hint"].set_text(
                    "Foreground app detection is unavailable on this "
                    "display. The app filter cannot run here; choose Off "
                    "or start WXB in an X11 session.")
            if "ac_blacklist" in self.ui:
                for widget in (self.ui["ac_blacklist"],
                               self.ui["ac_whitelist"]):
                    widget.set_sensitive(foreground_ok and not locked
                                         and not self.privacy)
        if "always_on_top_hint" in self.ui:
            self.ui["always_on_top_hint"].set_text(
                "Applies immediately through %s." % caps["above_backend"]
                if above_ok else
                "Saved preference only: this display does not expose X11 "
                "window stacking. Start under X11 to apply it.")
        if "window_geometry_hint" in self.ui:
            self.ui["window_geometry_hint"].set_text(
                "Window position and size are remembered on this X11 "
                "display." if geometry_ok else
                "Window position is not remembered on this display; start "
                "under X11 to save geometry.")
        if "portable_hint" in self.ui:
            status = portable_status()
            if status["enabled"] and status["warning"]:
                text = "Portable mode warning · " + status["warning"]
            elif status["enabled"]:
                text = ("Portable mode · settings are stored beside the "
                        "launcher and seed from your normal config on first "
                        "launch.")
            else:
                text = ("Portable mode · off (set WXB_PORTABLE=1 or add "
                        "portable.flag).")
            self.ui["portable_hint"].set_text(text)

    def _apply_always_on_top(self, enabled=None):
        if enabled is None:
            enabled = bool(self.service.settings.get("always_on_top", False))
        xid = self._x11_window_id()
        if xid is None or not self._display_is_x11():
            return False
        if shutil.which("wmctrl"):
            action = "add,above" if enabled else "remove,above"
            try:
                result = subprocess.run(
                    ["wmctrl", "-i", "-r", str(xid), "-b", action],
                    capture_output=True, timeout=1.0, check=False)
                if result.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        return x11_set_window_above(xid, enabled)

    def _sync_always_on_top(self):
        if self._syncing_above:
            return
        enabled = bool(self.ui["always_on_top"].get_active())
        caps = x11_capabilities()
        supported = bool(caps["above"] and self._display_is_x11())
        applied = supported and self._apply_always_on_top(enabled)
        failed = bool(enabled and not applied)
        if enabled and not applied:
            self._syncing_above = True
            try:
                self.ui["always_on_top"].set_active(False)
            finally:
                self._syncing_above = False
            enabled = False
        self.service.settings["always_on_top"] = enabled
        self.service.save()
        self._refresh_capability_hints()
        if failed:
            self._toast("Always on top could not be applied on this display.")

    def _save_window_geometry(self):
        window = dict(self.service.settings.get("window", {}))
        window["maximized"] = bool(self.is_maximized())
        if not window["maximized"]:
            xid = self._x11_window_id()
            width_saved = False
            height_saved = False
            if xid is not None and shutil.which("xdotool"):
                try:
                    result = subprocess.run(
                        ["xdotool", "getwindowgeometry", "--shell", str(xid)],
                        capture_output=True, text=True, timeout=1.0,
                        check=False)
                    for line in result.stdout.splitlines():
                        key, _, value = line.partition("=")
                        if key in ("X", "Y", "WIDTH", "HEIGHT") \
                                and value.strip().lstrip("-").isdigit():
                            number = int(value.strip())
                            if key == "X":
                                window["x"] = number
                            elif key == "Y":
                                window["y"] = number
                            elif key == "WIDTH":
                                window["width"] = max(900, number)
                                width_saved = True
                            elif key == "HEIGHT":
                                window["height"] = max(620, number)
                                height_saved = True
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
            if xid is not None:
                geometry = x11_window_geometry(xid)
                if geometry is not None:
                    # Store the X11 outer geometry.  GTK's get_width()
                    # excludes client-side shadow margins, so saving it and
                    # feeding it back through xdotool shrank the window by
                    # the shadow width on every save/restart cycle.
                    window["x"], window["y"] = geometry[0], geometry[1]
                    if not width_saved:
                        window["width"] = max(900, geometry[2])
                        width_saved = True
                    if not height_saved:
                        window["height"] = max(620, geometry[3])
                        height_saved = True
            if not width_saved:
                window["width"] = max(900, int(self.get_width()))
            if not height_saved:
                window["height"] = max(620, int(self.get_height()))
        self.service.settings["window"] = window

    def _restore_window_state(self):
        if self._window_state_restored:
            return False
        self._window_state_restored = True
        window = self.service.settings.get("window", {})
        if window.get("maximized"):
            self.maximize()
        else:
            xid = self._x11_window_id()
            if (xid is not None and window.get("x") is not None
                    and window.get("y") is not None):
                width = max(900, int(window.get("width", 1180)))
                height = max(620, int(window.get("height", 780)))
                moved = False
                if shutil.which("xdotool"):
                    try:
                        subprocess.run(
                            ["xdotool", "windowsize", str(xid), str(width),
                             str(height)], capture_output=True, timeout=1.0,
                            check=False)
                        result = subprocess.run(
                            ["xdotool", "windowmove", str(xid),
                             str(int(window["x"])), str(int(window["y"]))],
                            capture_output=True, timeout=1.0, check=False)
                        moved = result.returncode == 0
                    except (OSError, subprocess.SubprocessError, ValueError):
                        pass
                if not moved:
                    x11_configure_window(xid, int(window["x"]),
                                         int(window["y"]), width, height)
        self._apply_always_on_top(
            bool(self.service.settings.get("always_on_top", False)))
        return False

    def _sync_stop_on_close(self):
        self.service.settings["stop_on_close"] = bool(
            self.ui["stop_on_close"].get_active())
        self.service.save()

    def _reset_appearance(self):
        self.service.settings["accent"] = "cyan"
        self.service.settings["monochrome"] = False
        self.service.settings["opacity"] = 92
        self.service.settings["nav_compact"] = False
        self.service.save()
        self.ui["opacity"].set_value(92)
        self.ui["nav_compact"].set_active(False)
        self._set_mono_switch(False)
        self._apply_monochrome_controls(False)
        self._set_accent("cyan")
        self._toast("Appearance reset.")

    def _set_accent(self, name):
        if self.service.settings.get("monochrome"):
            # Choosing a colour is an explicit request to leave mono mode.
            self.service.settings["monochrome"] = False
            self._set_mono_switch(False)
            self._apply_monochrome_controls(False)
        self.service.set_accent(name)
        self.app.apply_appearance()
        for key, b in self._swatches.items():
            if key == name:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")

    def _sync_clicker_controls(self):
        if self.privacy:
            return
        cps = float(self.ui["ac_cps"].get_value())
        duty = float(self.ui["ac_duty"].get_value())
        self.ui["ac_cps_value"].set_text(fmt_clicker(cps))
        self.ui["ac_duty_value"].set_text(fmt_clicker(duty))
        self.service.settings["clicker"].update({
            "cps": cps,
            "duty": duty,
            "button": self._selected_clicker_button(),
            "mode": ("hold" if self.ui["ac_mode"].get_selected() else "toggle"),
        })
        self._refresh_rate_mode()

    def _selected_clicker_button(self):
        index = self.ui["ac_button_choice"].get_selected()
        return ("left", "right", "middle")[index] if index in (0, 1, 2) \
            else "left"

    def _refresh_rate_mode(self):
        if "ac_cps_max" not in self.ui:
            return
        ranged = self.ui["ac_rate_mode"].get_selected() == 1
        locked = bool(self.ui["ac_lock"].get_active()) \
            if "ac_lock" in self.ui else False
        self.ui["ac_cps_max"].set_sensitive(ranged and not locked
                                            and not self.privacy)

    def _sync_clicker_extras(self):
        if self.privacy or "ac_pointer" not in self.ui:
            return
        pointer_modes = ("follow", "fixed", "jitter")
        corners = ("top-left", "top-right", "bottom-left", "bottom-right")
        pointer = self.ui["ac_pointer"].get_selected()
        corner = self.ui["ac_corner"].get_selected()
        rate_modes = ("exact", "range")
        precisions = ("efficient", "balanced", "precise")
        filters = ("off", "blacklist", "whitelist")
        rate = self.ui["ac_rate_mode"].get_selected()
        precision = self.ui["ac_precision"].get_selected()
        filter_mode = self.ui["ac_filter_mode"].get_selected()
        if rate == 1:
            cps_now = float(self.ui["ac_cps"].get_value())
            if float(self.ui["ac_cps_max"].get_value()) < cps_now:
                self.ui["ac_cps_max"].set_value(cps_now)
        self.service.settings["clicker"].update({
            "duration": float(self.ui["ac_duration"].get_value()),
            "pointer_mode": (pointer_modes[pointer]
                             if 0 <= pointer < len(pointer_modes)
                             else "follow"),
            "jitter": int(self.ui["ac_jitter"].get_value()),
            "failsafe": bool(self.ui["ac_failsafe"].get_active()),
            "failsafe_corner": (corners[corner]
                                if 0 <= corner < len(corners) else "off"),
            "pause_on_lock": bool(self.ui["ac_pause_lock"].get_active()),
            "rate_mode": (rate_modes[rate]
                          if 0 <= rate < len(rate_modes) else "exact"),
            "cps_max": float(self.ui["ac_cps_max"].get_value()),
            "start_delay_ms": int(self.ui["ac_start_delay"].get_value()),
            "stop_after_clicks": int(self.ui["ac_stop_clicks"].get_value()),
            "timing_precision": (precisions[precision]
                                 if 0 <= precision < len(precisions)
                                 else "balanced"),
            "failsafe_margin": int(
                self.ui["ac_failsafe_margin"].get_value()),
            "app_filter_mode": (filters[filter_mode]
                                if 0 <= filter_mode < len(filters) else "off"),
            "app_blacklist": [item.strip() for item in
                              self.ui["ac_blacklist"].get_text().split(",")
                              if item.strip()],
            "app_whitelist": [item.strip() for item in
                              self.ui["ac_whitelist"].get_text().split(",")
                              if item.strip()],
        })
        self._refresh_rate_mode()
        self.service.save()
        self._refresh_clicker_point()

    def _refresh_sidebar_mouse(self):
        """The Nexus-style coordinate pair, fed by the real X pointer."""
        if self.privacy:
            self.side_mouse_x.set_text(PRIVACY_MASK)
            self.side_mouse_y.set_text(PRIVACY_MASK)
            return
        try:
            position = self.service.pointer_position()
        except Exception:
            position = None
        if position:
            self.side_mouse_x.set_text(str(position[0]))
            self.side_mouse_y.set_text(str(position[1]))

    def _refresh_clicker_point(self):
        if "ac_point" not in self.ui:
            return
        clicker = self.service.settings["clicker"]
        if clicker.get("pointer_mode") == "follow":
            self.ui["ac_point"].set_text("Cursor follows the pointer.")
        else:
            self.ui["ac_point"].set_text(
                "Point · %s, %s" % (clicker.get("fixed_x", 0),
                                    clicker.get("fixed_y", 0)))


    def _capture_clicker_point(self):
        ok, message = self.service.capture_clicker_point()
        self._toast(message)
        if ok:
            self._refresh_clicker_point()

    def _refresh_clicker_profiles(self):
        if "ac_profile_choice" not in self.ui:
            return
        names = self.service.list_clicker_profiles()
        self._clicker_profiles = names
        self.ui["ac_profile_choice"].set_model(
            Gtk.StringList.new(names or ["No clicker profiles"]))
        self.ui["ac_profile_choice"].set_selected(0)
        enabled = bool(names)
        self.ui["ac_profile_load"].set_sensitive(enabled)
        self.ui["ac_profile_delete"].set_sensitive(enabled)

    def _selected_clicker_profile(self):
        names = getattr(self, "_clicker_profiles", [])
        index = self.ui["ac_profile_choice"].get_selected()
        return names[index] if 0 <= index < len(names) else ""

    def _save_clicker_profile(self):
        ok, message = self.service.save_clicker_profile(
            self.ui["ac_profile_name"].get_text())
        self._toast(message)
        self._set_error(self.ui["ac_error"], "" if ok else message)
        if ok:
            self.ui["ac_profile_name"].set_text(
                self.service.clean_config_name(
                    self.ui["ac_profile_name"].get_text()))
            self._refresh_clicker_profiles()
            names = getattr(self, "_clicker_profiles", [])
            name = self.ui["ac_profile_name"].get_text()
            if name in names:
                self.ui["ac_profile_choice"].set_selected(names.index(name))

    def _load_clicker_profile(self):
        name = self._selected_clicker_profile()
        if not name:
            self._toast("Choose a clicker profile to load.")
            return
        ok, message = self.service.load_clicker_profile(name)
        self._toast(message)
        self._set_error(self.ui["ac_error"], "" if ok else message)
        if ok:
            self._apply_clicker_settings()
            self._refresh()

    def _delete_clicker_profile(self):
        name = self._selected_clicker_profile()
        if not name:
            return
        ok, message = self.service.delete_clicker_profile(name)
        self._toast(message)
        self._set_error(self.ui["ac_error"], "" if ok else message)
        if ok:
            self._refresh_clicker_profiles()

    def _apply_clicker_settings(self):
        clicker = self.service.settings["clicker"]
        self.ui["ac_cps"].set_value(float(clicker.get("cps", 12)))
        self.ui["ac_duty"].set_value(float(clicker.get("duty", 50)))
        self.ui["ac_cps_value"].set_text(fmt_clicker(clicker.get("cps", 12)))
        self.ui["ac_duty_value"].set_text(fmt_clicker(clicker.get("duty", 50)))
        self.ui["ac_button_choice"].set_selected(
            {"left": 0, "right": 1, "middle": 2}.get(
                clicker.get("button"), 0))
        self.ui["ac_mode"].set_selected(
            1 if clicker.get("mode") == "hold" else 0)
        self.ui["ac_duration"].set_value(float(clicker.get("duration", 0)))
        modes = ("follow", "fixed", "jitter")
        self.ui["ac_pointer"].set_selected(
            modes.index(clicker.get("pointer_mode"))
            if clicker.get("pointer_mode") in modes else 0)
        self.ui["ac_jitter"].set_value(int(clicker.get("jitter", 0)))
        corners = ("top-left", "top-right", "bottom-left", "bottom-right")
        corner = clicker.get("failsafe_corner", "top-left")
        self.ui["ac_corner"].set_selected(
            corners.index(corner) if corner in corners else 4)
        self.ui["ac_failsafe"].set_active(bool(clicker.get("failsafe", True)))
        self.ui["ac_pause_lock"].set_active(
            bool(clicker.get("pause_on_lock", True)))
        self.ui["ac_rate_mode"].set_selected(
            1 if clicker.get("rate_mode") == "range" else 0)
        self.ui["ac_cps_max"].set_value(float(clicker.get("cps_max", 14)))
        self.ui["ac_start_delay"].set_value(
            int(clicker.get("start_delay_ms", 0)))
        self.ui["ac_stop_clicks"].set_value(
            int(clicker.get("stop_after_clicks", 0)))
        precisions = ("efficient", "balanced", "precise")
        precision = clicker.get("timing_precision", "balanced")
        self.ui["ac_precision"].set_selected(
            precisions.index(precision) if precision in precisions else 1)
        self.ui["ac_failsafe_margin"].set_value(
            int(clicker.get("failsafe_margin", 2)))
        filters = ("off", "blacklist", "whitelist")
        filter_mode = clicker.get("app_filter_mode", "off")
        self.ui["ac_filter_mode"].set_selected(
            filters.index(filter_mode) if filter_mode in filters else 0)
        self.ui["ac_blacklist"].set_text(
            ", ".join(clicker.get("app_blacklist", [])))
        self.ui["ac_whitelist"].set_text(
            ", ".join(clicker.get("app_whitelist", [])))
        self._sync_clicker_controls()
        self._sync_clicker_extras()

    def _set_clicker_mode(self):
        mode = "hold" if self.ui["ac_mode"].get_selected() else "toggle"
        self.service.settings["clicker"]["mode"] = mode
        self.service.save()

    def _refresh_configs(self):
        if "config_choice" not in self.ui:
            return
        names = self.service.list_configs()
        self._saved_configs = names
        choices = names or ["No saved configs"]
        self.ui["config_choice"].set_model(Gtk.StringList.new(choices))
        self.ui["config_choice"].set_selected(0)
        enabled = bool(names)
        self.ui["config_load"].set_sensitive(enabled)
        self.ui["config_rename"].set_sensitive(enabled)
        self.ui["config_delete"].set_sensitive(enabled)

    def _selected_config(self):
        names = getattr(self, "_saved_configs", [])
        index = self.ui["config_choice"].get_selected()
        return names[index] if 0 <= index < len(names) else ""

    def _save_config(self):
        ok, message = self.service.save_config(
            self.ui["config_name"].get_text())
        self._toast(message)
        self._set_config_error("" if ok else message)
        if ok:
            self.ui["config_name"].set_text(
                self.service.clean_config_name(
                    self.ui["config_name"].get_text()))
            self._refresh_configs()
            names = getattr(self, "_saved_configs", [])
            name = self.ui["config_name"].get_text()
            if name in names:
                self.ui["config_choice"].set_selected(names.index(name))

    def _load_config(self):
        name = self._selected_config()
        if not name:
            self._set_config_error("No saved config selected.")
            return
        ok, message = self.service.load_config(name)
        self._toast(message)
        self._set_config_error("" if ok else message)
        if ok:
            self._apply_settings_to_controls()

    def _delete_config(self):
        name = self._selected_config()
        if not name:
            self._set_config_error("No saved config selected.")
            return
        ok, message = self.service.delete_config(name)
        self._toast(message)
        self._set_config_error("" if ok else message)
        if ok:
            self._refresh_configs()

    def _rename_config(self):
        old = self._selected_config()
        new = self.ui["config_name"].get_text().strip()
        if not old:
            self._set_config_error("No saved config selected.")
            return
        if not new:
            self._set_config_error("Type the new name in the config field.")
            return
        ok, message = self.service.rename_config(old, new)
        self._toast(message)
        self._set_config_error("" if ok else message)
        if ok:
            clean = self.service.clean_config_name(new)
            self.ui["config_name"].set_text(clean)
            self._refresh_configs()
            names = getattr(self, "_saved_configs", [])
            if clean in names:
                self.ui["config_choice"].set_selected(names.index(clean))

    def _set_config_error(self, text):
        widget = self.ui.get("config_error")
        if widget is not None:
            widget.set_text(text or "")
            widget.set_visible(bool(text))

    def _apply_settings_to_controls(self):
        settings = self.service.settings
        clicker = settings["clicker"]
        self.ui["ac_cps"].set_value(float(clicker["cps"]))
        self.ui["ac_duty"].set_value(float(clicker["duty"]))
        self.ui["ac_button_choice"].set_selected(
            {"left": 0, "right": 1, "middle": 2}.get(
                clicker.get("button"), 0))
        self.ui["ac_mode"].set_selected(
            0 if clicker.get("mode", "toggle") == "toggle" else 1)
        self.ui["ac_rate_mode"].set_selected(
            1 if clicker.get("rate_mode") == "range" else 0)
        self.ui["ac_cps_max"].set_value(float(clicker.get("cps_max", 14)))
        self.ui["ac_start_delay"].set_value(
            int(clicker.get("start_delay_ms", 0)))
        self.ui["ac_stop_clicks"].set_value(
            int(clicker.get("stop_after_clicks", 0)))
        precisions = ("efficient", "balanced", "precise")
        precision = clicker.get("timing_precision", "balanced")
        self.ui["ac_precision"].set_selected(
            precisions.index(precision) if precision in precisions else 1)
        self.ui["ac_failsafe_margin"].set_value(
            int(clicker.get("failsafe_margin", 2)))
        filters = ("off", "blacklist", "whitelist")
        filter_mode = clicker.get("app_filter_mode", "off")
        self.ui["ac_filter_mode"].set_selected(
            filters.index(filter_mode) if filter_mode in filters else 0)
        self.ui["ac_blacklist"].set_text(
            ", ".join(clicker.get("app_blacklist", [])))
        self.ui["ac_whitelist"].set_text(
            ", ".join(clicker.get("app_whitelist", [])))
        rotation = settings["rotation"]
        self.ui["rot_a"].set_selected(int(rotation.get("slot_a", 1)))
        self.ui["rot_b"].set_selected(int(rotation.get("slot_b", 2)))
        self.ui["rot_da"].set_value(rotation.get("delay_a", 140))
        self.ui["rot_db"].set_value(rotation.get("delay_b", 140))
        self.ui["rot_mode"].set_selected(
            0 if rotation.get("mode") == "toggle" else 1)
        self.ui["rot_max"].set_value(rotation.get("max_rot", 0))
        self.ui["rot_enabled"].set_active(self.service.macro_enabled)
        self._apply_rotation_gates()
        self._sync_slot_hints()
        self._set_socd_dropdowns()
        if "quick_mode" in self.ui:
            self.ui["quick_mode"].set_selected(
                0 if settings.get("launch_mode", "player") == "player" else 1)
            self._refresh_quick_places()
        opacity = int(settings.get("opacity", 92))
        self.ui["opacity"].set_value(opacity)
        if not self.privacy:
            self.ui["opacity_value"].set_text("%d%%" % opacity)
        self.set_opacity(opacity / 100.0)
        if "nav_compact" in self.ui:
            self.ui["nav_compact"].set_active(
                bool(settings.get("nav_compact", False)))
            self._responsive()
        if "stop_on_close" in self.ui:
            self.ui["stop_on_close"].set_active(
                bool(settings.get("stop_on_close", False)))
        if "alt_auto_tile" in self.ui:
            self._sync_alt_options(settings["alts"])
        monochrome = bool(settings.get("monochrome", False))
        if "monochrome" in self.ui:
            self._set_mono_switch(monochrome)
            self._apply_monochrome_controls(monochrome)
        accent = settings.get("accent", "cyan")
        if accent not in ACCENTS:
            accent = "cyan"
        self.service.set_accent(accent)
        self.app.apply_appearance()
        for name, swatch in self._swatches.items():
            if name == accent:
                swatch.add_css_class("selected")
            else:
                swatch.remove_css_class("selected")
        if not self.privacy:
            for action, entry in self.ui["hotkey_entries"].items():
                entry.set_text(display_hotkey(
                    settings["hotkeys"].get(action, "")))
        if "macro_speed" in self.ui:
            self._apply_macro_controls()
        self._mask_privacy_values()
        self._refresh()

    def _apply_hotkeys(self):
        values = {action: entry.get_text()
                  for action, entry in self.ui["hotkey_entries"].items()}
        ok, message = self.service.set_hotkeys(values)
        self._toast(message)
        field = self.ui.get("hotkey_error")
        if field is not None:
            field.set_text("" if ok else message)
            field.set_visible(not ok)
        if ok and not self.privacy:
            for action, entry in self.ui["hotkey_entries"].items():
                entry.set_text(display_hotkey(
                    self.service.settings["hotkeys"].get(action, "")))
            self._refresh_hotkey_hints()

    def _reset_hotkeys(self):
        ok, message = self.service.set_hotkeys(HOTKEY_DEFAULTS)
        self._toast(message)
        field = self.ui.get("hotkey_error")
        if field is not None:
            field.set_text("" if ok else message)
            field.set_visible(not ok)
        if ok and not self.privacy:
            for action, entry in self.ui["hotkey_entries"].items():
                entry.set_text(display_hotkey(
                    self.service.settings["hotkeys"].get(action, "")))
            self._refresh_hotkey_hints()

    def _slot_key(self, number):
        try:
            slot = int(number)
        except (TypeError, ValueError):
            return "?"
        return "0" if slot == 10 else str(slot)

    def _sync_slot_hints(self):
        if "rot_a_hint" not in self.ui:
            return
        for widget, hint in ((self.ui["rot_a"], self.ui["rot_a_hint"]),
                             (self.ui["rot_b"], self.ui["rot_b_hint"])):
            hint.set_text(PRIVACY_MASK_KEY if self.privacy else
                          "key %s" % self._slot_key(widget.get_selected()))

    def _refresh_hotkey_hints(self):
        if "ac_hotkey_hint" in self.ui:
            for action, widget in (
                    ("toggle_clicker", self.ui["ac_hotkey_hint"]),
                    ("toggle_rotation", self.ui["rot_hotkey_hint"])):
                if self.privacy:
                    widget.set_text("Hotkey · " + PRIVACY_MASK)
                else:
                    value = display_hotkey(
                        self.service.settings["hotkeys"].get(action, ""))
                    widget.set_text("Hotkey · " + (value or "not set"))
        if "alt_tiling_style" in self.ui:
            binding = display_hotkey(
                self.service.settings["hotkeys"].get("cycle_tiling", ""))
            self.ui["alt_tiling_style"].set_tooltip_text(
                "Choose how Cordial windows share the work area. "
                + (("%s cycles layouts globally." % binding) if binding
                   else "Set a tiling hotkey in Settings."))
        self._sync_macro_hotkey_hint()

    def _reset_hotkey(self, action):
        values = dict(self.service.settings["hotkeys"])
        values[action] = HOTKEY_DEFAULTS[action]
        ok, message = self.service.set_hotkeys(values)
        self._toast(message)
        field = self.ui.get("hotkey_error")
        if field is not None:
            field.set_text("" if ok else message)
            field.set_visible(not ok)
        if ok and not self.privacy:
            for name, entry in self.ui["hotkey_entries"].items():
                entry.set_text(display_hotkey(
                    self.service.settings["hotkeys"].get(name, "")))
            self._refresh_hotkey_hints()

    def _sync_rotation_controls(self):
        if self.privacy:
            return
        self._sync_slot_hints()
        self.service.settings["rotation"].update({
            "slot_a": self.ui["rot_a"].get_selected(),
            "slot_b": self.ui["rot_b"].get_selected(),
            "delay_a": int(self.ui["rot_da"].get_value()),
            "delay_b": int(self.ui["rot_db"].get_value()),
            "mode": "hold" if self.ui["rot_mode"].get_selected() else "toggle",
            "max_rot": int(self.ui["rot_max"].get_value()),
        })

    def _reset_onboarding(self):
        self.service.set_onboarded(False)
        self._show_onboarding()

    def _show_onboarding(self):
        if getattr(self, "_onboard", None) is not None:
            return
        accent = ("#ffffff" if self.service.settings.get("monochrome")
                  else ACCENTS.get(
                      self.service.settings.get("accent", "cyan"),
                      "#9fcaff"))
        self._onboard = OnboardingWindow(self, accent, self._onboard_done)
        self._onboard.connect("close-request", self._onboard_closed)
        self._onboard.present()

    def _onboard_closed(self, *_):
        self._onboard = None
        return False

    def _onboard_done(self):
        self._onboard = None
        self.service.set_onboarded(True)
        self._toast("Welcome to the studio.")

    # ------------------------------------------------------------------ command
    def _build_command(self):
        self.cmd_revealer = Gtk.Revealer()
        self.cmd_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.cmd_revealer.set_transition_duration(180)
        self.cmd_revealer.set_reveal_child(False)
        # A full-window revealer still participates in picking when its child
        # is hidden.  Left targetable, it silently swallows every click in the
        # studio.  Only the open palette may capture the pointer.
        self.cmd_revealer.set_can_target(False)
        self.cmd_revealer.set_visible(False)
        self.cmd_revealer.set_hexpand(True)
        self.cmd_revealer.set_vexpand(True)

        scrim = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scrim.add_css_class("wxb-cmd-scrim")
        scrim.set_hexpand(True)
        scrim.set_vexpand(True)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("wxb-command")
        panel.set_size_request(600, -1)
        panel.set_halign(Gtk.Align.CENTER)
        panel.set_valign(Gtk.Align.START)
        panel.set_margin_top(96)
        self.cmd_close = button(icon_name="window-close-symbolic",
                                css="wxb-quiet", tooltip="Close · Esc",
                                on_click=self.close_command)
        command_head = el(label("COMMANDS", "wxb-side-kicker"),
                          Gtk.Box(), self.cmd_close,
                          vertical=False, spacing=8, css="wxb-command-head")
        command_head.get_last_child().set_hexpand(True)
        panel.append(command_head)
        self.cmd_entry = Gtk.SearchEntry()
        self.cmd_entry.add_css_class("wxb-cmd-entry")
        if hasattr(self.cmd_entry, "set_has_frame"):
            self.cmd_entry.set_has_frame(False)
        self.cmd_entry.set_placeholder_text("Search pages, actions, settings…")
        self.cmd_entry.connect("search-changed", lambda *_: self._fill_command())
        self.cmd_entry.connect("activate", lambda *_: self._run_first_command())
        panel.append(self.cmd_entry)
        self.cmd_list = Gtk.ListBox()
        self.cmd_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.cmd_list.connect("row-activated", self._activate_command)
        cmd_scroll = Gtk.ScrolledWindow()
        cmd_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cmd_scroll.set_max_content_height(324)
        cmd_scroll.set_propagate_natural_height(True)
        cmd_scroll.set_child(self.cmd_list)
        panel.append(cmd_scroll)
        self.cmd_footer = label("↑↓ move · ↵ open · esc close",
                                "wxb-command-footer")
        panel.append(self.cmd_footer)
        scrim.append(panel)
        self.cmd_revealer.set_child(scrim)
        # Do not leave even an invisible overlay child mounted: GtkOverlay
        # can still retain it in the event target chain.
        self.command_overlay.add_overlay(self.cmd_revealer)
        self.command_overlay.remove_overlay(self.cmd_revealer)
        self._fill_command()

    def _commands(self):
        return [
            ("Overview", "Go to the control room", "view-grid-symbolic",
             "Ctrl 1", lambda: self.show_page("overview")),
            ("Auto Clicker", "Clicker controls", "input-mouse-symbolic",
             "Ctrl 2", lambda: self.show_page("automation")),
            ("AutoSwitcher", "Rotation macro controls", "view-refresh-symbolic",
             "", lambda: self.show_page("rotation")),
            ("SOCD Cleaner", "Opposite-key cleaner controls",
             "input-keyboard-symbolic", "", lambda: self.show_page("socd")),
            ("Cordial", "Cordial profiles & tiling",
             "view-list-symbolic", "Ctrl 3", lambda: self.show_page("alts")),
            ("Settings", "Appearance and behavior", "emblem-system-symbolic",
             "Ctrl 4", lambda: self.show_page("settings")),
            ("Macros", "Record and replay input", "media-record-symbolic",
             "Ctrl 5", lambda: self.show_page("macros")),
            ("Recorder", "Record the screen locally", "camera-video-symbolic",
             "Ctrl 6", lambda: self.show_page("recorder")),
            ("GameMode", "MangoHud, Linux GameMode and Gamescope",
             "applications-games-symbolic", "Ctrl 7",
             lambda: self.show_page("gamemode")),
            ("Launch Sober", "Start the game with the Lution overlay",
             "media-playback-start-symbolic", "", self._launch_sober),
            ("Toggle clicker", "Start or stop the auto clicker",
             "input-mouse-symbolic", "", self._toggle_clicker),
            ("Toggle rotation", "Start or stop the rotation macro",
             "view-refresh-symbolic", "", self._toggle_rotation),
            ("Stop everything", "Release every engine and input",
             "media-playback-stop-symbolic", "", self._stop_all),
        ]

    def _fill_command(self):
        clear(self.cmd_list)
        query = self.cmd_entry.get_text().strip().lower()
        items = [c for c in self._commands()
                 if query in c[0].lower() or query in c[1].lower()]
        if not items:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(label("No matches. Try “clicker” or “cordial”.",
                                "wxb-command-sub"))
            self.cmd_list.append(row)
        self.cmd_footer.set_text(
            "%d result%s · ↑↓ move · ↵ open · esc close" %
            (len(items), "" if len(items) == 1 else "s"))
        for title, sub, icon_name, key, run in items:
            row = Gtk.ListBoxRow()
            row.set_child(el(icon(icon_name, 16),
                             el(label(title, "wxb-command-name"),
                                label(sub, "wxb-command-sub"),
                                vertical=True, spacing=1, hexpand=True),
                             label(key, "wxb-command-key") if key else Gtk.Box(),
                             vertical=False, spacing=12))
            row._command = run
            self.cmd_list.append(row)
        first = self.cmd_list.get_row_at_index(0)
        if first is not None and getattr(first, "_command", None):
            self.cmd_list.select_row(first)

    def _activate_command(self, _list, row):
        run = getattr(row, "_command", None)
        self.close_command()
        if run:
            run()

    def _run_first_command(self):
        row = self.cmd_list.get_selected_row() or self.cmd_list.get_row_at_index(0)
        if row is not None:
            self._activate_command(self.cmd_list, row)

    def open_command(self):
        self.cmd_entry.set_text("")
        self._fill_command()
        if self.cmd_revealer.get_parent() is None:
            self.command_overlay.add_overlay(self.cmd_revealer)
        self.cmd_revealer.set_visible(True)
        self.cmd_revealer.set_can_target(True)
        self.cmd_revealer.set_reveal_child(True)
        self.cmd_entry.grab_focus()

    def close_command(self):
        self.cmd_revealer.set_reveal_child(False)
        self.cmd_revealer.set_can_target(False)
        self.cmd_revealer.set_visible(False)
        if self.cmd_revealer.get_parent() is not None:
            self.command_overlay.remove_overlay(self.cmd_revealer)

    def _command_open(self):
        return self.cmd_revealer.get_reveal_child()

    # -------------------------------------------------------------------- keys
    def _wire_keys(self):
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    def _on_key(self, _controller, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval == Gdk.KEY_k:
            self.open_command()
            return True
        if keyval == Gdk.KEY_Escape:
            if self._capture_window is not None:
                return False
            if self._command_open():
                self.close_command()
            else:
                self.emergency_stop()
            return True
        # Local panic stop works even when the global listener could not bind.
        if (keyval == Gdk.KEY_F8 and self._capture_window is None
                and self.service.ac_running):
            self.service.stop_clicker(wait=False)
            self._toast("Clicker panic-stopped (F8).")
            self._refresh()
            return True
        if (keyval == Gdk.KEY_F8 and self._capture_window is None
                and self.service.macro_running):
            self.service.stop_macro(wait=True)
            self._toast("Macro panic-stopped (F8).")
            self._refresh()
            return True
        if ctrl and Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            index = keyval - Gdk.KEY_1
            if index < len(self.PAGES):
                self.show_page(self.PAGES[index][0])
                return True
        if ctrl and keyval == Gdk.KEY_q:
            self.close()
            return True
        return False

    # ------------------------------------------------------------------ pages
    def show_page(self, key):
        keys = [p[0] for p in self.PAGES]
        if key not in keys:
            key = "overview"
        self.stack.set_visible_child_name(key)
        self._page_key = key
        if key == "gamemode" and "gm_summary" in self.ui:
            self._refresh_gamemode_page(running=self._sober_running_seen)
        if key == "macros" and "macro_status" in self.ui:
            self._refresh_macro_page()
        if key == "recorder" and "rec_status" in self.ui:
            self._refresh_recorder_page()
        if hasattr(self, "header_kicker"):
            self.header_kicker.set_text({
                "overview": "STUDIO",
                "automation": "AUTOMATION",
                "rotation": "AUTOMATION",
                "socd": "AUTOMATION",
                "alts": "CORDIAL",
                "gamemode": "LAUNCH",
                "settings": "SYSTEM",
                "macros": "CAPTURE",
                "recorder": "CAPTURE",
            }.get(key, "STUDIO"))
        for nav_key, b in self._nav.items():
            if nav_key == key:
                b.add_css_class("active")
            else:
                b.remove_css_class("active")
        for page_key, title, _icon, _shortcut in self.PAGES:
            if page_key == key:
                self.header_page.set_text({
                    "automation": "Auto Clicker",
                    "rotation": "AutoSwitcher",
                    "socd": "SOCD Cleaner",
                }.get(page_key, title))
                break
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    # ------------------------------------------------------------------ actions
    def _toast(self, text):
        toast = Adw.Toast.new(text)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    def _refresh_quick_places(self):
        if "quick_recent" not in self.ui:
            return
        places = self.service.settings.get("places") or []
        labels = []
        ids = []
        for place in places:
            pid = str(place.get("id") or "")
            if not pid:
                continue
            server = str(place.get("server") or "")
            labels.append(pid + ((" · " + server) if server else ""))
            ids.append(pid)
        if not labels:
            labels = ["No recent places"]
        self._quick_places = ids
        self.ui["quick_recent"].set_model(Gtk.StringList.new(labels))
        self.ui["quick_recent"].set_selected(0)

    def _pick_recent_place(self):
        index = self.ui["quick_recent"].get_selected()
        ids = getattr(self, "_quick_places", [])
        if index < 0 or index >= len(ids):
            return
        pid = ids[index]
        place = next((item for item in
                      (self.service.settings.get("places") or [])
                      if str(item.get("id") or "") == pid), None)
        if place is None:
            return
        self.ui["quick_place"].set_text(str(place.get("id") or ""))
        self.ui["quick_server"].set_text(str(place.get("server") or ""))

    def _quick_launch(self):
        parsed = self.service.parse_place(
            self.ui["quick_place"].get_text())
        if parsed is None:
            self._toast("Use a place ID or a roblox.com/games link.")
            return
        place_id, server = parsed
        if not server:
            server = self.ui["quick_server"].get_text().strip()
        plain = bool(self.ui["quick_mode"].get_selected())
        self.service.settings["launch_mode"] = "plain" if plain else "player"
        self.service.save()
        self.service.remember_place(place_id, server)
        self._refresh_quick_places()
        url = "roblox://placeId=%s" % place_id
        if server:
            url += "&gameInstanceId=%s" % server
        ok, message = self.service.launch_sober(plain=plain, url=url)
        self._toast(message)

    def _launch_sober(self, plain=False):
        ok, message = self.service.launch_sober(plain=plain)
        self._toast(message)

    def _toggle_clicker_from_hero(self):
        self._toggle_clicker()

    def _toggle_clicker(self):
        self._sync_clicker_controls()
        self._sync_clicker_extras()
        if self.service.ac_running:
            # Do not join a clicker worker on the GTK thread.  The worker
            # releases the button in its own finally block.
            self.service.stop_clicker(wait=False)
            ok, message = True, "Clicker stopped."
        else:
            ok, message = self.service.start_clicker(
                float(self.ui["ac_cps"].get_value()),
                float(self.ui["ac_duty"].get_value()),
                self._selected_clicker_button(),
            )
        self._set_error(self.ui["ac_error"], "" if ok else message)
        self._toast(message)
        self._refresh()

    def _sync_macro_enabled(self):
        enabled = bool(self.ui["rot_enabled"].get_active())
        ok, message = self.service.set_macro_enabled(enabled)
        if not ok:
            self._toast(message)
        self._apply_rotation_gates()
        self._refresh()

    def _apply_rotation_gates(self):
        """Combine the macro master switch, value lock and privacy."""
        if "rot_enabled" not in self.ui:
            return
        enabled = bool(self.service.macro_enabled)
        locked = bool(self.ui["rot_lock"].get_active())
        sensitive = enabled and not locked and not self.privacy
        for widget in (self.ui["rot_a"], self.ui["rot_b"],
                       self.ui["rot_da"], self.ui["rot_db"],
                       self.ui["rot_mode"], self.ui["rot_max"]):
            widget.set_sensitive(sensitive)
        self.ui["rot_start"].set_sensitive(enabled)
        self.ui["rot_enabled"].set_sensitive(True)

    def _toggle_rotation(self):
        values = {
            "slot_a": self.ui["rot_a"].get_selected(),
            "slot_b": self.ui["rot_b"].get_selected(),
            "delay_a": self.ui["rot_da"].get_value(),
            "delay_b": self.ui["rot_db"].get_value(),
            "mode": "hold" if self.ui["rot_mode"].get_selected() else "toggle",
            "max_rot": int(self.ui["rot_max"].get_value()),
        }
        ok, message = self.service.toggle_rotation(values)
        self._toast(message)
        self._apply_rotation_gates()
        self._refresh()

    def _socd_modes(self):
        modes = ["Neutral", "Last win", "First win", "Priority"]
        return {
            "horizontal": modes[max(0, min(3, self.ui["socd_h"].get_selected()))],
            "vertical": modes[max(0, min(3, self.ui["socd_v"].get_selected()))],
        }

    def _lock_clicker(self):
        locked = self.ui["ac_lock"].get_active()
        for widget in (self.ui["ac_cps"], self.ui["ac_duty"],
                       self.ui["ac_cps_value"], self.ui["ac_duty_value"],
                       self.ui["ac_button_choice"], self.ui["ac_mode"],
                       self.ui["ac_duration"], self.ui["ac_pointer"],
                       self.ui["ac_jitter"], self.ui["ac_corner"],
                       self.ui["ac_failsafe"], self.ui["ac_pause_lock"],
                       self.ui["ac_set_point"], self.ui["ac_rate_mode"],
                       self.ui["ac_cps_max"], self.ui["ac_start_delay"],
                       self.ui["ac_stop_clicks"], self.ui["ac_precision"],
                       self.ui["ac_failsafe_margin"],
                       self.ui["ac_filter_mode"], self.ui["ac_blacklist"],
                       self.ui["ac_whitelist"]):
            widget.set_sensitive(not locked and not self.privacy)
        self.ui["ac_lock"].set_label("Locked" if locked else "Lock")
        self._refresh_rate_mode()
        self._refresh_capability_hints()

    def _lock_rotation(self):
        locked = self.ui["rot_lock"].get_active()
        self._apply_rotation_gates()
        self.ui["rot_lock"].set_label("Locked" if locked else "Lock")

    def _apply_socd_preset(self, name):
        presets = {
            "Hitbox standard": ("Neutral", "Priority"),
            "True neutral": ("Neutral", "Neutral"),
            "Last win": ("Last win", "Last win"),
            "First win": ("First win", "First win"),
        }
        horizontal, vertical = presets[name]
        modes = ["Neutral", "Last win", "First win", "Priority"]
        self.ui["socd_h"].set_selected(modes.index(horizontal))
        self.ui["socd_v"].set_selected(modes.index(vertical))
        self._sync_socd_controls()

    def _sync_socd_controls(self):
        modes = self._socd_modes()
        self.service.settings["socd"] = {
            "mode_h": modes["horizontal"],
            "mode_v": modes["vertical"],
        }

    def _set_socd_dropdowns(self):
        modes = ["Neutral", "Last win", "First win", "Priority"]
        for axis, widget in (("horizontal", self.ui["socd_h"]),
                             ("vertical", self.ui["socd_v"])):
            mode = self.service.settings["socd"].get(
                "mode_" + ("h" if axis == "horizontal" else "v"), "Neutral")
            widget.set_selected(modes.index(mode) if mode in modes else 0)

    # ------------------------------------------------------------- Input Lab
    def _input_lab(self):
        settings = self.service.settings["input_lab"]
        self.ui["lab_capture"] = label("CAPTURE · OFF", "wxb-status")
        self.ui["lab_caps"] = label("Checking input capabilities…", "wxb-hint",
                                     wrap=True, max_width=66)
        self.ui["lab_error"] = label("", "wxb-hint wxb-bad", wrap=True,
                                      max_width=66)
        self.ui["lab_error"].set_visible(False)

        live = Gtk.Grid(column_spacing=10, row_spacing=4)
        self.ui["lab_live"] = {}
        for row, direction in enumerate(("LEFT", "RIGHT", "UP", "DOWN")):
            name = label(direction.title(), "wxb-label")
            phys = label("—", "wxb-mono")
            out = label("—", "wxb-mono")
            blocked = label("", "wxb-hint")
            live.attach(name, 0, row, 1, 1)
            live.attach(phys, 1, row, 1, 1)
            live.attach(label("→", "wxb-hint"), 2, row, 1, 1)
            live.attach(out, 3, row, 1, 1)
            live.attach(blocked, 4, row, 1, 1)
            self.ui["lab_live"][direction] = (phys, out, blocked)

        self.ui["lab_timeline_switch"] = switch()
        self.ui["lab_timeline_switch"].set_active(bool(settings["timeline"]))
        self.ui["lab_timeline_switch"].connect(
            "notify::active", self._lab_set_timeline)
        self.ui["lab_timeline"] = label(
            "Timeline off — no direction events stored.", "wxb-mono",
            wrap=True, max_width=66)

        # Remap profiles stay collapsed inside the Input Lab; the SOCD
        # controls above are unaffected and remain the default view.
        self.ui["lab_profile_name"] = Gtk.Entry()
        self.ui["lab_profile_name"].set_placeholder_text("Remap profile name")
        self.ui["lab_profile_name"].add_css_class("wxb-entry")
        self.ui["lab_profile_choice"] = Gtk.DropDown.new_from_strings(
            ["No remap profiles"])
        self.ui["lab_profile_choice"].add_css_class("wxb-drop")
        self.ui["lab_profile_mode"] = Gtk.DropDown.new_from_strings(
            ["Hold", "Toggle"])
        self.ui["lab_profile_mode"].add_css_class("wxb-drop")
        self.ui["lab_source"] = Gtk.DropDown.new_from_strings(
            [name for name, _code in input_lab.INPUT_KEY_CHOICES])
        self.ui["lab_source"].add_css_class("wxb-drop")
        self.ui["lab_target"] = Gtk.DropDown.new_from_strings(
            [name for name, _code in input_lab.INPUT_KEY_CHOICES])
        self.ui["lab_target"].add_css_class("wxb-drop")
        self.ui["lab_source"].set_selected(0)
        target_names = [name for name, _code in input_lab.INPUT_KEY_CHOICES]
        self.ui["lab_target"].set_selected(
            target_names.index("Enter") if "Enter" in target_names else 0)
        self.ui["lab_mapping_add"] = button(
            "Add mapping", icon_name="list-add-symbolic", css="wxb-quiet",
            on_click=self._lab_add_mapping)
        self.ui["lab_mapping_clear"] = button(
            "Clear", css="wxb-quiet", on_click=self._lab_clear_mappings)
        self.ui["lab_profile_save"] = button(
            "Save", icon_name="document-save-symbolic", css="wxb-quiet",
            on_click=self._lab_save_profile)
        self.ui["lab_profile_load"] = button(
            "Load", icon_name="document-open-symbolic", css="wxb-quiet",
            on_click=self._lab_load_profile)
        self.ui["lab_profile_delete"] = button(
            "Delete", icon_name="user-trash-symbolic", css="wxb-quiet",
            on_click=self._lab_delete_profile)
        self.ui["lab_profile_status"] = label("No remap profile active.",
                                              "wxb-hint", wrap=True)
        self.ui["lab_profile_error"] = label("", "wxb-hint wxb-bad",
                                             wrap=True, max_width=66)
        self.ui["lab_profile_error"].set_visible(False)
        self.ui["lab_mappings"] = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.ui["lab_conflicts"] = label("No conflicts detected.", "wxb-hint",
                                         wrap=True, max_width=66)
        self.ui["lab_conflict_check"] = button(
            "Check conflicts", icon_name="dialog-warning-symbolic",
            css="wxb-quiet", on_click=self._lab_check_conflicts)
        profile_body = el(
            el(self.ui["lab_profile_name"], self.ui["lab_profile_choice"],
               vertical=True, spacing=6, hexpand=True),
            el(self._mini_field("Mode", self.ui["lab_profile_mode"]),
               vertical=False, spacing=10),
            el(self._mini_field("Source", self.ui["lab_source"]),
               self._mini_field("Target", self.ui["lab_target"]),
               vertical=False, spacing=10),
            el(self.ui["lab_mapping_add"], self.ui["lab_mapping_clear"],
               vertical=False, spacing=6),
            self.ui["lab_mappings"],
            el(self.ui["lab_profile_save"], self.ui["lab_profile_load"],
               self.ui["lab_profile_delete"], vertical=False, spacing=6),
            self.ui["lab_profile_status"],
            self.ui["lab_profile_error"],
            label("Mouse-button suppression is unavailable with the keyboard "
                  "capture backend; supported keyboard mappings are stored "
                  "only while the cleaner owns a keyboard.", "wxb-hint",
                  wrap=True, max_width=66),
            el(self.ui["lab_conflict_check"], self.ui["lab_conflicts"],
               vertical=False, spacing=8),
            spacing=9)
        profile_fold = self._fold(
            "Remap profiles",
            "Hold or toggle keyboard mappings. Off by default.",
            profile_body)

        self.ui["lab_controller_button"] = button(
            "Start controller test", icon_name="applications-games-symbolic",
            css="wxb-quiet", on_click=self._lab_toggle_controller)
        self.ui["lab_controller_status"] = label("Controller test stopped.",
                                                  "wxb-hint", wrap=True)
        self.ui["lab_controller_buttons"] = label("—", "wxb-mono", wrap=True)
        self.ui["lab_controller_axes"] = label("—", "wxb-mono", wrap=True)
        self.ui["lab_deadzone"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 50, 1)
        self.ui["lab_deadzone"].set_draw_value(False)
        self.ui["lab_deadzone"].set_value(float(settings["deadzone"]) * 100)
        self.ui["lab_deadzone_value"] = label("12%", "wxb-mono")
        self.ui["lab_deadzone"].connect(
            "value-changed", self._lab_set_deadzone)
        controller_body = el(
            el(self.ui["lab_controller_button"],
               self.ui["lab_controller_status"], vertical=False, spacing=8),
            self.ui["lab_controller_buttons"],
            self.ui["lab_controller_axes"],
            el(label("Stick dead zone", "wxb-label"),
               self.ui["lab_deadzone"], self.ui["lab_deadzone_value"],
               vertical=False, spacing=8),
            label("Read-only test: controller events are shown locally and "
                  "never saved.", "wxb-hint", wrap=True, max_width=66),
            spacing=9)
        controller_fold = self._fold(
            "Controller test", "Buttons, sticks, triggers and dead zone.",
            controller_body)

        self.ui["lab_keys_switch"] = switch()
        self.ui["lab_keys_switch"].set_active(bool(settings["visualizer"]["keys"]))
        self.ui["lab_clicks_switch"] = switch()
        self.ui["lab_clicks_switch"].set_active(bool(settings["visualizer"]["clicks"]))
        for widget in (self.ui["lab_keys_switch"], self.ui["lab_clicks_switch"]):
            widget.connect("notify::active", self._lab_visual_changed)
        self.ui["lab_size"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, input_lab.VISUALIZER_SIZE_MIN,
            input_lab.VISUALIZER_SIZE_MAX, 1)
        self.ui["lab_size"].set_draw_value(False)
        self.ui["lab_size"].set_value(int(settings["visualizer"]["size"]))
        self.ui["lab_size_value"] = label(
            "%d%%" % int(settings["visualizer"]["size"]), "wxb-mono")
        self.ui["lab_size"].connect("value-changed", self._lab_visual_changed)
        self.ui["lab_opacity"] = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, input_lab.VISUALIZER_OPACITY_MIN,
            input_lab.VISUALIZER_OPACITY_MAX, 1)
        self.ui["lab_opacity"].set_draw_value(False)
        self.ui["lab_opacity"].set_value(int(settings["visualizer"]["opacity"]))
        self.ui["lab_opacity_value"] = label(
            "%d%%" % int(settings["visualizer"]["opacity"]), "wxb-mono")
        self.ui["lab_opacity"].connect("value-changed", self._lab_visual_changed)
        self.ui["lab_color"] = Gtk.Entry()
        self.ui["lab_color"].set_text(settings["visualizer"]["color"])
        self.ui["lab_color"].set_width_chars(9)
        self.ui["lab_color"].add_css_class("wxb-entry")
        self.ui["lab_color"].connect("activate", self._lab_color_changed)
        self.ui["lab_visual_status"] = label(
            "Visualizers off. No input listeners are running.", "wxb-hint",
            wrap=True, max_width=66)
        visual_body = el(
            el(self._mini_field("Keystrokes", self.ui["lab_keys_switch"]),
               self._mini_field("Click rings", self.ui["lab_clicks_switch"]),
               vertical=False, spacing=14),
            el(self._mini_field("Size", self.ui["lab_size"]),
               self.ui["lab_size_value"], vertical=False, spacing=8),
            el(self._mini_field("Opacity", self.ui["lab_opacity"]),
               self.ui["lab_opacity_value"], vertical=False, spacing=8),
            el(label("Colour", "wxb-label"), self.ui["lab_color"],
               vertical=False, spacing=8),
            self.ui["lab_visual_status"],
            label("Key names only; no typed text, history or upload. The "
                  "overlay is separate from the existing WXB toasts and "
                  "hotkeys.", "wxb-hint", wrap=True, max_width=66),
            spacing=9)
        visual_fold = self._fold(
            "Visualizers", "Optional keystroke overlay and click rings.",
            visual_body)

        self.ui["lab_reset"] = button(
            "Safe reset", icon_name="media-playback-stop-symbolic",
            css="wxb-danger", on_click=self._lab_safe_reset)
        body = el(
            el(self.ui["lab_capture"], self.ui["lab_caps"],
               vertical=False, spacing=12),
            live,
            el(label("Direction timeline", "wxb-label"),
               self.ui["lab_timeline_switch"], vertical=False, spacing=8),
            self.ui["lab_timeline"],
            profile_fold,
            controller_fold,
            visual_fold,
            self.ui["lab_error"],
            self.ui["lab_reset"],
            label("Capture starts only for the cleaner, an explicit "
                  "controller test, or an enabled visualizer.", "wxb-hint",
                  wrap=True, max_width=66),
            spacing=11)
        self._refresh_lab_profiles()
        self._refresh_lab_mappings()
        return self._fold(
            "Input Lab",
            "Optional live input, remaps, controller test and visualizers",
            body)

    def _lab_selected_profile(self):
        names = getattr(self, "_lab_profile_names", [])
        index = self.ui["lab_profile_choice"].get_selected()
        return names[index] if 0 <= index < len(names) else ""

    def _refresh_lab_profiles(self):
        if "lab_profile_choice" not in self.ui:
            return
        names = self.service.list_input_lab_profiles()
        self._lab_profile_names = names
        self.ui["lab_profile_choice"].set_model(Gtk.StringList.new(
            names or ["No remap profiles"]))
        active = self.service.settings["input_lab"].get("active_profile", "")
        if active in names:
            self.ui["lab_profile_choice"].set_selected(names.index(active))
        else:
            self.ui["lab_profile_choice"].set_selected(0)

    def _refresh_lab_mappings(self):
        if "lab_mappings" not in self.ui:
            return
        clear(self.ui["lab_mappings"])
        if not self._lab_mappings:
            self.ui["lab_mappings"].append(
                label("No mappings in this draft.", "wxb-hint"))
            return
        for index, item in enumerate(self._lab_mappings):
            remove = button(
                icon_name="list-remove-symbolic", css="wxb-quiet",
                tooltip="Remove this mapping")
            remove.connect("clicked",
                           lambda _b, i=index: self._lab_remove_mapping(i))
            self.ui["lab_mappings"].append(el(
                label("%s → %s" % (item["source"], item["target"]),
                      "wxb-mono"), remove,
                vertical=False, spacing=6))

    def _lab_remove_mapping(self, index):
        if 0 <= index < len(self._lab_mappings):
            del self._lab_mappings[index]
            self._refresh_lab_mappings()

    def _lab_add_mapping(self):
        sources = [name for name, _code in input_lab.INPUT_KEY_CHOICES]
        source = sources[max(0, self.ui["lab_source"].get_selected())]
        target = sources[max(0, self.ui["lab_target"].get_selected())]
        mode = "toggle" if self.ui["lab_profile_mode"].get_selected() else "hold"
        candidate = list(self._lab_mappings) + [
            {"source": source, "target": target}]
        _profile, error = input_lab.normalize_profile(
            {"name": "Draft", "mode": mode, "mappings": candidate})
        if error:
            self._set_error(self.ui["lab_profile_error"], error)
            self._toast(error)
            return
        self._lab_mappings = candidate
        self._set_error(self.ui["lab_profile_error"], "")
        self._refresh_lab_mappings()

    def _lab_clear_mappings(self):
        self._lab_mappings = []
        self._refresh_lab_mappings()

    def _lab_save_profile(self):
        name = self.ui["lab_profile_name"].get_text().strip()
        mode = "toggle" if self.ui["lab_profile_mode"].get_selected() else "hold"
        ok, message = self.service.save_input_lab_profile(
            name, self._lab_mappings, mode)
        self._set_error(self.ui["lab_profile_error"], "" if ok else message)
        self._toast(message)
        if ok:
            self._refresh_lab_profiles()
            self._lab_profile_names = self.service.list_input_lab_profiles()
            if name in self._lab_profile_names:
                self.ui["lab_profile_choice"].set_selected(
                    self._lab_profile_names.index(name))

    def _lab_load_profile(self):
        name = self._lab_selected_profile()
        if not name:
            self._toast("Choose a remap profile first.")
            return
        ok, message = self.service.load_input_lab_profile(name)
        self._toast(message)
        if ok:
            profile = self.service.input_lab_snapshot().get("profile") or {}
            self._lab_mappings = list(profile.get("mappings") or [])
            self._lab_profile_name = profile.get("name", "")
            self.ui["lab_profile_name"].set_text(self._lab_profile_name)
            self.ui["lab_profile_mode"].set_selected(
                1 if profile.get("mode") == "toggle" else 0)
            self._refresh_lab_mappings()
        self._refresh()

    def _lab_delete_profile(self):
        name = self._lab_selected_profile()
        if not name:
            self._toast("Choose a remap profile first.")
            return
        ok, message = self.service.delete_input_lab_profile(name)
        self._toast(message)
        self._refresh_lab_profiles()
        self._refresh()

    def _lab_set_timeline(self, *_args):
        if self._lab_syncing:
            return
        self.service.set_input_lab_settings(
            timeline=self.ui["lab_timeline_switch"].get_active())
        self._refresh()

    def _lab_set_deadzone(self, *_args):
        if self._lab_syncing:
            return
        value = int(round(self.ui["lab_deadzone"].get_value()))
        self.ui["lab_deadzone_value"].set_text("%d%%" % value)
        self.service.set_input_lab_settings(deadzone=value / 100.0)

    def _lab_visual_changed(self, *_args):
        if self._lab_syncing:
            return
        visual = self.service.settings["input_lab"]["visualizer"]
        ok, message = self.service.set_input_lab_settings(
            keys=self.ui["lab_keys_switch"].get_active(),
            clicks=self.ui["lab_clicks_switch"].get_active(),
            size=int(round(self.ui["lab_size"].get_value())),
            opacity=int(round(self.ui["lab_opacity"].get_value())),
            color=self.ui["lab_color"].get_text().strip() or visual["color"])
        if not ok:
            self._toast(message)
        self._sync_overlay()
        self._refresh()

    def _lab_color_changed(self, *_args):
        if self._lab_syncing:
            return
        color = self.ui["lab_color"].get_text().strip()
        clean = input_lab.normalize_input_lab({
            "visualizer": {"color": color}})["visualizer"]["color"]
        if color.lower() != clean.lower():
            self.ui["lab_color"].set_text(clean)
            self._toast("Use a #rrggbb colour.")
        self._lab_visual_changed()

    def _lab_toggle_controller(self):
        if self.service._controller is not None and self.service._controller.running:
            self.service.stop_controller_test()
            self._toast("Controller test stopped.")
        else:
            ok, message = self.service.start_controller_test()
            self._toast(message)
        self._refresh()

    def _lab_check_conflicts(self):
        conflicts = self.service.input_lab_conflicts(external=True)
        text = " · ".join(conflicts) if conflicts else "No conflicts detected."
        self.ui["lab_conflicts"].set_text(text)
        self._toast("Conflict check complete.")

    def _lab_safe_reset(self):
        ok, message = self.service.safe_input_reset()
        self._toast(message)
        if self._overlay is not None:
            self._overlay.hide()
        self._refresh()

    def _on_visualizer_event(self, kind, label_text, x, y):
        GLib.idle_add(self._show_visual_event, kind, label_text, x, y)

    def _show_visual_event(self, kind, label_text, x, y):
        if self._overlay is None:
            self._sync_overlay()
        if self._overlay is not None:
            if kind == "click":
                self._overlay.show_click(label_text, x, y)
            else:
                self._overlay.show_key(label_text)
        return False

    def _sync_overlay(self):
        visual = self.service.settings["input_lab"]["visualizer"]
        enabled = bool(visual["keys"] or visual["clicks"])
        if not enabled:
            if self._overlay is not None:
                self._overlay.hide()
            return
        if self._overlay is None:
            self._overlay = InputOverlayWindow(
                on_error=self._overlay_error)
        self._overlay.apply_settings(visual)
        self._overlay.set_enabled(visual["keys"], visual["clicks"])
        if self._overlay.error:
            self._toast(self._overlay.error)

    def _overlay_error(self, message):
        GLib.idle_add(self._disable_overlay_after_error, message)

    def _disable_overlay_after_error(self, message):
        if self._closed:
            return False
        self._toast("Visualizer disabled: " + str(message))
        overlay, self._overlay = self._overlay, None
        if overlay is not None:
            overlay.hide()
            overlay.destroy()
        self.service.set_input_lab_settings(keys=False, clicks=False)
        self._refresh()
        return False

    def _sync_input_lab_visualizers(self):
        visual = self.service.settings["input_lab"]["visualizer"]
        if visual["keys"] or visual["clicks"]:
            self.service.set_input_lab_settings()
        self._sync_overlay()
        self._refresh()
        return False

    def _toggle_socd(self):
        if self.service.socd_running:
            self.service.stop_socd()
            self._toast("SOCD cleaner stopped.")
        else:
            ok, message = self.service.start_socd(self._socd_modes())
            self._toast(message)
        self._refresh()

    def _stop_all(self):
        self.service.stop_all()
        self._toast("Everything stopped. Inputs released.")
        self._refresh()

    def emergency_stop(self):
        """ESC and the old deck's emergency action share one path."""
        self._stop_all()

    @property
    def macro_enabled(self):
        return self.service.macro_enabled

    def start_capture(self, which):
        """Old-deck spelling: ac/macro/socd map to the studio actions."""
        action = {"ac": "toggle_clicker", "macro": "toggle_rotation",
                  "socd": "toggle_socd"}.get(which, which)
        self._start_hotkey_capture(action)

    # ------------------------------------------------------------------ privacy
    def toggle_privacy(self):
        self.set_privacy(not self.privacy)

    def set_privacy(self, on):
        """Hide values without changing them or the running engines.

        This is per-session by design.  Turning it off restores the exact
        text that was on screen, including edits that had not been applied.
        """
        on = bool(on)
        if on == self.privacy:
            return
        self.privacy = on
        if on:
            self._privacy_texts = {
                "ac_cps": self.ui["ac_cps_value"].get_text(),
                "ac_duty": self.ui["ac_duty_value"].get_text(),
                "opacity": self.ui["opacity_value"].get_text(),
                "hotkeys": {action: entry.get_text()
                            for action, entry in
                            self.ui["hotkey_entries"].items()},
            }
        self._apply_privacy()
        self._face_privacy()
        self._refresh()

    def _apply_privacy(self):
        on = self.privacy
        # Clicker numbers: exact fields are replaced by the mask and lock; the
        # sliders stop responding so their knob cannot give the value away.
        for widget in (self.ui["ac_cps_value"], self.ui["ac_duty_value"]):
            widget.set_sensitive(not on and not self.ui["ac_lock"].get_active())
        for widget in (self.ui["ac_cps"], self.ui["ac_duty"]):
            widget.set_sensitive(not on and not self.ui["ac_lock"].get_active())
            css_swap(widget, add=("wxb-private-scale",) if on else (),
                     remove=() if on else ("wxb-private-scale",))
        if on:
            self.ui["ac_cps_value"].set_text(PRIVACY_MASK)
            self.ui["ac_duty_value"].set_text(PRIVACY_MASK)
        else:
            self.ui["ac_cps_value"].set_text(
                self._privacy_texts.get("ac_cps", ""))
            self.ui["ac_duty_value"].set_text(
                self._privacy_texts.get("ac_duty", ""))

        # Rotation values: the slots/delays/limit only show dots.  A spin
        # button keeps its real value while its visible text is blank, so a
        # hidden value can never be read off the control.
        for widget in (self.ui["rot_a"], self.ui["rot_b"],
                       self.ui["rot_da"], self.ui["rot_db"],
                       self.ui["rot_max"]):
            css_swap(widget, add=("wxb-private",) if on else (),
                     remove=() if on else ("wxb-private",))
        spins = (self.ui["rot_da"], self.ui["rot_db"],
                 self.ui["rot_max"])
        if on:
            for widget in spins:
                self._privacy_spin_values.setdefault(widget,
                                                     widget.get_value())
                widget.set_text("")
        else:
            for widget, value in self._privacy_spin_values.items():
                widget.set_value(value)
            self._privacy_spin_values.clear()
        self._apply_rotation_gates()

        # Appearance and keybindings are values too.
        mono = bool(self.service.settings.get("monochrome"))
        self.ui["opacity"].set_sensitive(not on and not mono)
        if not on:
            self.ui["opacity_value"].set_text(
                "100%" if mono else self._privacy_texts.get(
                    "opacity", "%d%%" % int(round(
                        self.ui["opacity"].get_value()))))
        css_swap(self.ui["opacity"],
                 add=("wxb-private-scale",) if on else (),
                 remove=() if on else ("wxb-private-scale",))
        self.ui["hotkey_apply"].set_sensitive(not on)
        self.ui["hotkey_reset"].set_sensitive(not on)
        for action, entry in self.ui["hotkey_entries"].items():
            entry.set_sensitive(not on)
            entry.set_text(PRIVACY_MASK if on
                           else self._privacy_texts.get("hotkeys", {}).get(
                               action, display_hotkey(
                                   self.service.settings["hotkeys"].get(
                                       action, ""))))
            self.ui["hotkey_set_buttons"][action].set_sensitive(not on)
            self.ui["hotkey_reset_buttons"][action].set_sensitive(not on)
        if not on:
            self._privacy_texts = {}
        self._sync_slot_hints()
        self._refresh_hotkey_hints()
        self._mask_privacy_values()

    def _mask_privacy_values(self):
        if not self.privacy:
            return
        for key in ("ac_card_value", "rot_card_value", "socd_card_value"):
            widget = self.ui.get(key)
            if widget is not None:
                widget.set_text(PRIVACY_MASK)
        if "ac_page_total" in self.ui:
            self.ui["ac_page_total"].set_text(PRIVACY_MASK + " clicks")
        for attr in ("metric_clicks", "metric_alts"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.set_text(PRIVACY_MASK)
        if "opacity_value" in self.ui:
            self.ui["opacity_value"].set_text(PRIVACY_MASK)
        if "hero_alt_text" in self.__dict__:
            self.hero_alt_text.set_text("Cordial profiles hidden")
        if "socd_conflicts" in self.ui:
            self.ui["socd_conflicts"].set_text(PRIVACY_MASK + " conflicts")
        if "socd_live" in self.ui:
            self.ui["socd_live"].set_text("held • · sent • · suppressed •")
        if "socd_access" in self.ui:
            access = self.ui["socd_access"].get_text()
            if access.startswith("Input access ready"):
                self.ui["socd_access"].set_text(
                    "Input access ready · keyboards hidden.")

    def _face_privacy(self):
        btn = getattr(self, "privacy_btn", None)
        if btn is None:
            return
        action_button_text(
            btn, "Hidden" if self.privacy else "Hide UI",
            "view-conceal-symbolic" if self.privacy
            else "view-reveal-symbolic")
        css_swap(btn, add=("wxb-privacy-on",) if self.privacy else (),
                 remove=() if self.privacy else ("wxb-privacy-on",))

    # --------------------------------------------------------------- hotkey capture
    def _start_hotkey_capture(self, action):
        if self.privacy or self._capture_window is not None:
            return
        self._capture_window = HotkeyCaptureWindow(
            self, HOTKEY_LABELS.get(action, action),
            lambda canonical: self._capture_hotkey(action, canonical))
        self._capture_window.connect(
            "close-request", lambda *_a: self._capture_closed())
        self._capture_window.present()

    def _capture_closed(self):
        self._capture_window = None
        return False

    def _capture_hotkey(self, action, canonical):
        canonical = normalize_hotkey(canonical)
        if not canonical or canonical == "esc":
            return
        entry = self.ui["hotkey_entries"].get(action)
        if entry is None:
            return
        entry.set_text(display_hotkey(canonical))
        self._apply_hotkeys()
        self._capture_window = None

    def _initial_scan(self):
        self.service.scan_alts()
        return False

    def _scan_alts(self):
        self.service.scan_alts()
        self._refresh()

    # ----------------------------------------------------------------- status
    def _tick(self):
        if self._closed:
            return False
        self._responsive()
        self._refresh()
        return True

    def _queue_refresh(self):
        if self._refresh_pending or self._closed:
            return
        self._refresh_pending = True
        GLib.idle_add(self._flush_refresh)

    def _flush_refresh(self):
        self._refresh_pending = False
        if not self._closed:
            self._refresh()
        return False

    def _refresh(self):
        if self._closed:
            return
        self._refresh_capability_hints()
        snap = self.service.snapshot()
        ac, rot, socd = snap["clicker"], snap["rotation"], snap["socd"]
        alts, sober = snap["alts"], snap["sober"]
        macro = snap["macros"]
        rec = snap.get("recorder") or {}
        if hasattr(self, "side_mouse_x"):
            self._refresh_sidebar_mouse()
        # Stop all never closes the game, so Sober is deliberately not part
        # of the stoppable-engine count that drives the header action.
        active = sum((bool(ac["running"]), bool(rot["running"]),
                      bool(socd.get("running")),
                      bool(macro.get("running")), bool(rec.get("running"))))
        if "metric_engines" in self.__dict__:
            self.metric_engines.set_text(str(active))
            self.metric_clicks.set_text(str(ac["clicks"]))
            self.metric_alts.set_text(str(len(alts["profiles"])))
            self.metric_state.set_text("Live" if active else "Ready")
        self.stop_btn.set_visible(active > 0)
        if active:
            set_pill(self.side_status, "%d running" % active, "live")
            css_swap(self.side_status_dot, add=("live",), remove=("good", "bad"))
        else:
            set_pill(self.side_status, "Engines idle")
            css_swap(self.side_status_dot, add=("good",), remove=("live", "bad"))
        # Hidden pages keep their state from the last visit.  Refreshing
        # every recorder/macro control on every tick was pure widget churn
        # while the user was reading another tab; show_page refreshes them
        # immediately when they become visible again.
        if "macro_status" in self.ui and self._page_key == "macros":
            self._refresh_macro_page(snap=macro)
        if "rec_status" in self.ui and self._page_key == "recorder":
            self._refresh_recorder_page(snap=rec)
        if "sober_status" in self.ui:
            running_now = bool(sober.get("running"))
            if "sober_stop" in self.ui:
                self.ui["sober_stop"].set_sensitive(running_now)
            if (self._sober_running_seen is not None
                    and self._sober_running_seen != running_now):
                self._refresh_runtime(refresh=True)
            self._sober_running_seen = running_now
            if sober.get("maintenance"):
                self.ui["sober_status"].set_text("Installing or updating Sober…")
                self.ui["sober_detail"].set_text(
                    sober.get("detail") or "This can take a few minutes.")
                self.ui["sober_ensure"].set_sensitive(False)
            elif sober.get("runtime_dirty"):
                self._refresh_runtime(refresh=True)
            elif sober.get("error"):
                self.ui["sober_detail"].set_text(sober["error"])
        if "gm_summary" in self.ui and self._page_key == "gamemode":
            self._refresh_gamemode_page(running=bool(sober.get("running")))
        if "hero_sober_text" in self.__dict__:
            self.hero_sober_text.set_text(
                ("Sober: " + sober["error"][:52]) if sober.get("error")
                else ("Sober is running" if sober["running"]
                      else "Sober is not running"))
            css_swap(self.hero_sober_dot,
                     add=("good",) if sober["running"] else (),
                     remove=("live",) if not sober["running"] else ())
            action_button_text(
                self.hero_click,
                "Stop clicker" if ac["running"] else "Start clicker",
                "media-playback-stop-symbolic" if ac["running"]
                else "input-mouse-symbolic")
            self.hero_engine_text.set_text(
                "%d engine%s running" % (active, "" if active == 1 else "s")
                if active else "All engines idle")
            self.hero_alt_text.set_text(
                "%d Cordial profile%s" % (len(alts["profiles"]),
                                          "" if len(alts["profiles"]) == 1 else "s")
                if alts["profiles"] else "Cordial not detected")

        # overview cards
        if "ac_card_status" in self.ui:
            set_pill(self.ui["ac_card_status"],
                     "PAUSED" if ac.get("paused") else (
                         "LIVE" if ac["running"] else "IDLE"),
                     "live" if ac["running"] else "")
            self.ui["ac_card_value"].set_text(
                "%s CPS" % fmt_clicker(self.service.settings["clicker"]["cps"]))
            set_pill(self.ui["rot_card_status"],
                     "LIVE" if rot["running"] else (
                         "IDLE" if rot.get("enabled", True) else "DISABLED"),
                     "live" if rot["running"] else "")
            self.ui["rot_card_value"].set_text(
                "%s → %s" % (self.service.settings["rotation"]["slot_a"],
                             self.service.settings["rotation"]["slot_b"]))
            set_pill(self.ui["socd_card_status"],
                     "LIVE" if socd.get("running") else "IDLE",
                     "live" if socd.get("running") else "")
            mode = self.service.settings["socd"].get("mode_h", "neutral")
            self.ui["socd_card_value"].set_text(mode.title())

        # automation
        if "ac_start" in self.ui:
            set_pill(self.ui["ac_status"],
                     "PAUSED" if ac.get("paused") else (
                         "LIVE" if ac["running"] else "IDLE"),
                     "live" if ac["running"] else "")
            if "ac_page_total" in self.ui:
                mode = self.service.settings["clicker"].get(
                    "pointer_mode", "follow")
                target = {"follow": "cursor", "fixed": "fixed point",
                          "jitter": "jitter"}.get(mode, mode)
                self.ui["ac_page_target"].set_text("Target · " + target)
                clicks = int(ac.get("clicks", 0))
                self.ui["ac_page_total"].set_text(
                    PRIVACY_MASK + " clicks" if self.privacy else
                    "Total · %d click%s" % (
                        clicks, "" if clicks == 1 else "s"))
                if "ac_foreground" in self.ui:
                    foreground_ok = bool(
                        x11_capabilities()["foreground"]
                        and self._display_is_x11())
                    active = (PRIVACY_MASK if self.privacy else
                              foreground_app_name() if foreground_ok else
                              "unavailable (X11 required)")
                    self.ui["ac_foreground"].set_text(
                        "Active app · " + (active or "unavailable"))
                css_swap(self.ac_page_dot,
                         add=("live",) if ac["running"] else ("good",),
                         remove=("good",) if ac["running"] else ("live",))
            action_button_text(
                self.ui["ac_start"],
                "Stop clicking" if ac["running"] else "Start clicking",
                "media-playback-stop-symbolic" if ac["running"]
                else "media-playback-start-symbolic")
            css_swap(self.ui["ac_start"],
                     add=("wxb-danger",) if ac["running"] else ("wxb-primary",),
                     remove=("wxb-primary",) if ac["running"] else ("wxb-danger",))
            blocked = str(ac.get("blocked") or "")
            self._set_error(
                self.ui["ac_error"],
                ac["error"] or ("App filter · paused in %s" % blocked
                                if blocked else ""))
            self._refresh_clicker_point()
            enabled = bool(rot.get("enabled", True))
            set_pill(self.ui["rot_status"],
                     "LIVE" if rot["running"] else (
                         "IDLE" if enabled else "DISABLED"),
                     "live" if rot["running"] else "")
            self.ui["rot_start"].set_sensitive(enabled)
            action_button_text(
                self.ui["rot_start"],
                "Stop rotation" if rot["running"] else "Start rotation",
                "media-playback-stop-symbolic" if rot["running"]
                else "media-playback-start-symbolic")
            css_swap(self.ui["rot_start"],
                     add=("wxb-danger",) if rot["running"] else ("wxb-primary",),
                     remove=("wxb-primary",) if rot["running"] else ("wxb-danger",))
            self._set_error(self.ui["rot_error"], rot["error"])
            set_pill(self.ui["socd_status"],
                     "LIVE" if socd.get("running") else "IDLE",
                     "live" if socd.get("running") else "")
            action_button_text(
                self.ui["socd_start"],
                "Disable cleaner" if socd.get("running") else "Enable cleaner",
                "media-playback-stop-symbolic" if socd.get("running")
                else "media-playback-start-symbolic")
            css_swap(self.ui["socd_start"],
                     add=("wxb-danger",) if socd.get("running") else ("wxb-primary",),
                     remove=("wxb-primary",) if socd.get("running") else ("wxb-danger",))
            self._set_error(self.ui["socd_error"],
                            socd.get("error") or "")
            self.ui["socd_conflicts"].set_text(
                "%d conflict%s" % (socd.get("conflicts", 0),
                                   "" if socd.get("conflicts", 0) == 1 else "s"))
            if "socd_live" in self.ui:
                self.ui["socd_live"].set_text(self._socd_live_text(socd))
                access = socd.get("access") or {}
                if access.get("ok"):
                    self.ui["socd_access"].set_text(
                        "Input access ready · %d keyboard%s."
                        % (int(socd.get("keyboards", 0)),
                           "" if socd.get("keyboards", 0) == 1 else "s"))
                else:
                    self.ui["socd_access"].set_text(
                        "Input access missing: " + (
                            access.get("text") or "cannot read keyboards"))
            if "lab_capture" in self.ui:
                self._refresh_input_lab(socd, snap.get("input_lab") or {})

        self._sync_alt_options(alts)
        self._refresh_alts(alts)
        self._refresh_hotkey_hints()
        self._mask_privacy_values()

    def _refresh_input_lab(self, socd, lab):
        if "lab_capture" not in self.ui:
            return
        settings = (lab.get("settings") or
                    self.service.settings["input_lab"])
        caps = lab.get("capabilities") or {}
        visual = settings.get("visualizer") or {}
        self._lab_syncing = True
        try:
            self.ui["lab_keys_switch"].set_active(bool(visual.get("keys")))
            self.ui["lab_clicks_switch"].set_active(bool(visual.get("clicks")))
            self.ui["lab_size"].set_value(int(visual.get("size", 100)))
            self.ui["lab_size_value"].set_text(
                "%d%%" % int(visual.get("size", 100)))
            self.ui["lab_opacity"].set_value(int(visual.get("opacity", 80)))
            self.ui["lab_opacity_value"].set_text(
                "%d%%" % int(visual.get("opacity", 80)))
            if not self.ui["lab_color"].is_focus():
                self.ui["lab_color"].set_text(visual.get("color", "#9fcaff"))
            deadzone = int(round(float(settings.get("deadzone", 0.12)) * 100))
            self.ui["lab_deadzone"].set_value(deadzone)
            self.ui["lab_deadzone_value"].set_text("%d%%" % deadzone)
            self.ui["lab_timeline_switch"].set_active(
                bool(settings.get("timeline")))
        finally:
            self._lab_syncing = False

        capture = lab.get("capture", "off")
        labels = {"off": "CAPTURE · OFF", "socd": "CAPTURE · SOCD",
                  "controller": "CAPTURE · CONTROLLER",
                  "visualizer": "CAPTURE · VISUALIZER"}
        for widget in (self.ui["lab_capture"],
                       self.ui.get("lab_capture_badge")):
            if widget is not None:
                set_pill(widget, labels.get(capture, "CAPTURE · OFF"),
                         "live" if capture != "off" else "")
        capability = "%s · remap %s · controller %s · visualizer %s" % (
            str(caps.get("session", "unknown")).upper(),
            "ready" if caps.get("remap") else "off",
            "ready" if caps.get("controller") else "off",
            "ready" if caps.get("visualizer") else "off")
        if caps.get("reason"):
            capability += " — " + str(caps["reason"])
        self.ui["lab_caps"].set_text(capability)

        for direction, (phys_label, out_label, blocked_label) in \
                self.ui["lab_live"].items():
            names = (socd.get("codes") or {}).get(direction) or []
            phys = bool((socd.get("phys") or {}).get(direction))
            out = bool((socd.get("out") or {}).get(direction))
            blocked = bool((socd.get("suppressed") or {}).get(direction))
            phys_label.set_text(" + ".join(names) if names else
                                ("held" if phys else "—"))
            out_label.set_text("SENT" if out else "—")
            blocked_label.set_text("suppressed" if blocked else "")
        if settings.get("timeline"):
            events = socd.get("timeline") or []
            if events:
                lines = ["%s %s → %s" % (
                    item.get("key", "?"), "↓" if item.get("kind") == "down"
                    else "↑", item.get("out") or "suppressed")
                    for item in events[-8:]]
                self.ui["lab_timeline"].set_text(" · ".join(lines))
            else:
                self.ui["lab_timeline"].set_text(
                    "Timeline on — no direction events yet.")
        else:
            self.ui["lab_timeline"].set_text(
                "Timeline off — no direction events stored.")

        remap_ok = bool(caps.get("remap"))
        for key in ("lab_profile_name", "lab_profile_choice",
                    "lab_profile_mode", "lab_source", "lab_target",
                    "lab_mapping_add", "lab_mapping_clear",
                    "lab_profile_save", "lab_profile_load",
                    "lab_profile_delete", "lab_conflict_check"):
            self.ui[key].set_sensitive(remap_ok)
        profile = lab.get("profile") or {}
        if profile and getattr(self, "_lab_active_name", "") != profile.get("name"):
            self._lab_active_name = profile.get("name", "")
            self._lab_mappings = list(profile.get("mappings") or [])
            self.ui["lab_profile_name"].set_text(self._lab_active_name)
            self.ui["lab_profile_mode"].set_selected(
                1 if profile.get("mode") == "toggle" else 0)
            self._refresh_lab_mappings()
        if profile:
            self.ui["lab_profile_status"].set_text(
                "Active: %s · %s · %d mapping%s" % (
                    profile.get("name", "?"), profile.get("mode", "hold"),
                    len(profile.get("mappings") or []),
                    "" if len(profile.get("mappings") or []) == 1 else "s"))
        else:
            self.ui["lab_profile_status"].set_text(
                "No remap profile active.")
        conflicts = lab.get("conflicts") or []
        self.ui["lab_conflicts"].set_text(
            " · ".join(conflicts) if conflicts else "No conflicts detected.")
        self.ui["lab_conflict_check"].set_tooltip_text(
            "Checks WXB hotkeys and, on X11, passive external key grabs."
            if caps.get("external_conflicts") else
            "External checks need X11 and python-xlib; WXB conflicts are "
            "still checked.")

        controller = lab.get("controller") or {}
        running = bool(controller.get("running"))
        action_button_text(
            self.ui["lab_controller_button"],
            "Stop controller test" if running else "Start controller test",
            "applications-games-symbolic")
        self.ui["lab_controller_button"].set_sensitive(
            bool(caps.get("controller")) or running)
        if running and not controller.get("connected"):
            self.ui["lab_controller_status"].set_text(
                "Waiting for a controller…")
        elif running:
            self.ui["lab_controller_status"].set_text(
                "Test active · %s" % controller.get("name", "controller"))
        else:
            self.ui["lab_controller_status"].set_text(
                "Controller test stopped.")
        buttons = controller.get("buttons") or []
        self.ui["lab_controller_buttons"].set_text(
            "Buttons: " + (", ".join(buttons) if buttons else "—"))
        axes = controller.get("axes") or {}
        self.ui["lab_controller_axes"].set_text(
            "Axes: " + (", ".join("%s %+.2f" % (name, data.get("value", 0))
                                 for name, data in axes.items())
                        if axes else "—"))
        self.ui["lab_deadzone"].set_sensitive(
            bool(caps.get("controller")) or running)

        visual_running = bool((lab.get("visualizer") or {}).get("running"))
        self.ui["lab_keys_switch"].set_sensitive(
            bool(caps.get("visualizer")) or bool(visual.get("keys")))
        self.ui["lab_clicks_switch"].set_sensitive(
            bool(caps.get("visualizer")) or bool(visual.get("clicks")))
        self.ui["lab_visual_status"].set_text(
            "Capture active — listeners run only for the selected visualizers."
            if visual_running else
            "Visualizers off. No input listeners are running.")
        errors = [str(lab.get("error") or ""),
                  str(lab.get("profile_error") or "")]
        self._set_error(self.ui["lab_error"],
                        " · ".join(item for item in errors if item))

    @staticmethod
    def _socd_live_text(socd):
        directions = ("LEFT", "RIGHT", "UP", "DOWN")
        short = {"LEFT": "L", "RIGHT": "R", "UP": "U", "DOWN": "D"}
        phys = socd.get("phys") or {}
        out = socd.get("out") or {}
        suppressed = socd.get("suppressed") or {}
        codes = socd.get("codes") or {}
        held = []
        sent = []
        blocked = []
        for direction in directions:
            key = short[direction]
            if phys.get(direction):
                names = codes.get(direction) or []
                held.append("%s(%s)" % (key, "/".join(names) or "held"))
            if out.get(direction):
                sent.append(key)
            if suppressed.get(direction):
                blocked.append(key)
        return "held %s · sent %s · suppressed %s" % (
            " ".join(held) or "—", " ".join(sent) or "—",
            " ".join(blocked) or "—")

    def _set_error(self, widget, text):
        widget.set_text(text or "")
        widget.set_visible(bool(text))

    def _set_alt_options_status(self, style):
        if "alt_options_status" not in self.ui:
            return
        title = dict(ALT_TILING_CHOICES).get(style, "Side by side")
        cap = int(self.ui["alt_fps_cap"].get_value())
        low = bool(self.ui["alt_low_graphics"].get_active())
        quota = int(self.ui["alt_cpu_quota"].get_value())
        quota_note = (" · needs unprivileged WXB"
                      if quota and os.geteuid() == 0 else "")
        self.ui["alt_options_status"].set_text(
            "FPS cap %d · %s · low graphics %s · CPU quota %s · "
            "applied on next launch%s." %
            (cap, title.lower(), "on" if low else "off",
             "%d%%" % quota if quota else "off", quota_note))

    def _sync_alt_options(self, alts):
        """Mirror service-side Cordial options into every option control."""
        if "alt_auto_tile" not in self.ui:
            return
        style = alts.get("tiling_style", "columns")
        self._alt_options_syncing = True
        try:
            self.ui["alt_auto_tile"].set_active(
                bool(alts.get("auto_tile", True)))
            self.ui["alt_fps_cap"].set_value(
                int(alts.get("fps_cap", 15) or 0))
            self.ui["alt_low_graphics"].set_active(
                bool(alts.get("low_graphics", True)))
            self.ui["alt_cpu_quota"].set_value(
                int(alts.get("cpu_quota", 0) or 0))
            self._sync_alt_tiling(alts)
        finally:
            self._alt_options_syncing = False
        self._set_alt_options_status(style)

    def _sync_alt_tiling(self, alts):
        """Mirror a hotkey-driven style change into the real dropdown."""
        if "alt_tiling_style" not in self.ui:
            return
        style = alts.get("tiling_style")
        choices = [key for key, _title in ALT_TILING_CHOICES]
        if style not in choices:
            return
        selected = self.ui["alt_tiling_style"].get_selected()
        index = choices.index(style)
        if selected == index:
            return
        self._alt_style_syncing = True
        try:
            self.ui["alt_tiling_style"].set_selected(index)
        finally:
            self._alt_style_syncing = False
        self._set_alt_options_status(style)

    def _refresh_alts(self, alts):
        if "alts_list" not in self.ui:
            return
        running = {item.get("profile") for item in alts.get("running", [])
                   if item.get("running")}
        sig = (tuple(alts.get("profiles") or ()), tuple(sorted(running)),
               bool(alts.get("scanning")), alts.get("error", ""),
               alts.get("backend", ""))
        if sig == self._alt_sig:
            return
        self._alt_sig = sig
        clear(self.ui["alts_list"])
        clear(self.ui["alts_empty"])
        profiles = alts.get("profiles") or []
        if alts.get("scanning"):
            set_pill(self.ui["alts_status"], "SCANNING", "live")
            self.ui["alts_list"].append(label("Looking for Cordial…",
                                              "wxb-hint"))
            return
        if not profiles and alts.get("error"):
            set_pill(self.ui["alts_status"], "NOT AVAILABLE", "bad")
            self.ui["alts_empty"].append(
                self._empty("No Cordial profiles found",
                            "Cordial is not installed or has no profiles yet. "
                            "Sober quick play still works.",
                            "utilities-terminal-symbolic"))
        elif not profiles:
            set_pill(self.ui["alts_status"], "EMPTY")
            self.ui["alts_empty"].append(
                self._empty("No alts yet",
                            "Install Cordial and create a profile, then "
                            "refresh. WXB never asks for your password.",
                            "view-list-symbolic"))
        else:
            set_pill(self.ui["alts_status"],
                     "%d PROFILE%s" % (len(profiles),
                                       "" if len(profiles) == 1 else "S"),
                     "good")
            for name in profiles:
                self.ui["alts_list"].append(self._alt_row(name, name in running))
        # overview preview
        if "alts_preview" in self.__dict__:
            clear(self.alts_preview)
            if profiles:
                for name in profiles[:3]:
                    self.alts_preview.append(self._alt_row(name, name in running,
                                                           compact=True))
            else:
                self.alts_preview.append(
                    label("No Cordial profiles found. Sober quick play is "
                          "still available.", "wxb-hint", wrap=True))

    def _alt_row(self, name, running, compact=False):
        display = str(name) if name is not None else "?"
        initial = label(display[:1].upper() or "?", "wxb-avatar")
        status = label("RUNNING" if running else "READY",
                       "wxb-status" + (" live" if running else ""))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if running:
            actions.append(button("Stop", icon_name="media-playback-stop-symbolic",
                                  css="wxb-quiet",
                                  on_click=lambda n=name: self._stop_alt(n)))
        else:
            actions.append(button("Launch", icon_name="media-playback-start-symbolic",
                                  css="wxb-quiet",
                                  on_click=lambda n=name: self._launch_alt(n)))
        if not compact:
            delete = button(icon_name="edit-delete-symbolic", css="wxb-quiet",
                            tooltip="Delete this Cordial profile and its local "
                                    "account data")
            delete.set_sensitive(not running)
            if running:
                delete.set_tooltip_text(
                    "Stop this client before deleting its profile.")
            delete.connect("clicked", lambda _b, n=name: self._delete_alt(n))
            actions.append(delete)
        name_label = label(display, "wxb-alt-name",
                           ellipsize=Pango.EllipsizeMode.END)
        name_label.set_max_width_chars(28)
        name_label.set_hexpand(True)
        row = el(initial,
                 el(name_label, vertical=True, spacing=0, hexpand=True),
                 status, actions, vertical=False, spacing=8,
                 css="wxb-row wxb-alt-row")
        row.set_tooltip_text("Cordial profile: %s" % display)
        row.set_margin_top(4 if compact else 0)
        return row

    def _empty(self, title, sub, icon_name):
        return el(icon(icon_name, 22, "wxb-dim"),
                  label(title, "wxb-empty-title"),
                  label(sub, "wxb-empty-sub", wrap=True, max_width=54),
                  vertical=True, spacing=6, css="wxb-empty")

    def _launch_alt(self, name):
        ok, message = self.service.launch_alt(name)
        self._toast(message)
        self._refresh()

    def _add_alt_profile(self):
        name = self.ui["alt_profile_name"].get_text().strip()
        ok, message = self.service.add_alt_profile(name)
        self._set_error(self.ui["alt_profile_error"], "" if ok else message)
        self._toast(message)
        if ok:
            self.ui["alt_profile_name"].set_text("")
            self._alt_sig = None
            self._refresh()

    def _delete_alt(self, name):
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True,
            heading="Delete Cordial profile?",
            body=("Delete “%s” and its local Cordial data? The account itself "
                  "is not removed from Roblox, and this cannot be undone." % name))
        dialog.add_css_class("wxb-dialog")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete",
                                        Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._delete_alt_response, name)
        dialog.present()

    def _delete_alt_response(self, _dialog, response, name):
        if response == "delete":
            self._delete_alt_now(name)

    def _delete_alt_now(self, name):
        ok, message = self.service.delete_alt_profile(name)
        self._set_error(self.ui["alt_profile_error"], "" if ok else message)
        self._toast(message)
        if ok:
            self._alt_sig = None
            self._refresh()

    def _alt_options_changed(self, *_args):
        if self._alt_style_syncing or self._alt_options_syncing:
            return
        selected = self.ui["alt_tiling_style"].get_selected()
        style = (ALT_TILING_CHOICES[selected][0]
                 if 0 <= selected < len(ALT_TILING_CHOICES) else "columns")
        ok, message = self.service.set_alt_options(
            auto_tile=self.ui["alt_auto_tile"].get_active(),
            fps_cap=int(self.ui["alt_fps_cap"].get_value()),
            low_graphics=self.ui["alt_low_graphics"].get_active(),
            cpu_quota=int(self.ui["alt_cpu_quota"].get_value()),
            tiling_style=style)
        if not ok:
            self._toast(message)
            self._sync_alt_options(self.service.settings["alts"])
            return
        self._set_alt_options_status(style)
        self._alt_sig = None
        self._refresh()

    def _tile_alts(self):
        ok, message = self.service.tile_alts()
        self._toast(message)
        self._refresh()

    def _stop_alt(self, name):
        self.service.stop_alt(name)
        self._refresh()

    # ------------------------------------------------------------------ close
    def raise_window(self):
        """Second launch / icon click: bring the existing window forward."""
        if self._closed:
            return
        self.present()

    def _on_close(self, *_):
        if self._closed:
            return False
        self._closed = True
        if self._overlay_unsub is not None:
            try:
                self._overlay_unsub()
            except Exception:
                pass
            self._overlay_unsub = None
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay.destroy()
            self._overlay = None
        if self.service.settings.get("stop_on_close", False):
            self.service.stop_all()
        else:
            # A recorder is a file being written, not a background input
            # engine: finalize it even when "stop engines on close" is off.
            try:
                self.service.stop_recording(wait=True)
            except Exception:
                pass
        self._save_window_geometry()
        self.service.save()
        if self.on_quit:
            self.on_quit()
        return False


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------
class WxbApplication(Adw.Application):
    def __init__(self, config_path=None, startup_page="overview",
                 show_onboarding=True, auto_scan=True, hotkeys=True):
        flags = (Gio.ApplicationFlags.NON_UNIQUE
                 if os.environ.get("WXB_STANDALONE") == "1"
                 else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)
        self.config_path = config_path
        self.startup_page = startup_page
        self.show_onboarding = show_onboarding
        self.auto_scan = auto_scan
        self.hotkeys = hotkeys
        self.service = None
        self.window = None
        self._provider = None
        self._accent = "#9fcaff"

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None and ICONS_PATH.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(
                str(ICONS_PATH))
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK)
        for name, action in (
            ("quit", self._quit),
            ("shortcuts", self._shortcuts),
            ("about", self._about),
            ("search", self._search),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda *_a, fn=action: fn())
            self.add_action(act)
        self.set_accels_for_action("app.search", ["<Control>k"])
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        if self.window is not None:
            self.window.raise_window()
            return
        self.service = StudioService(path=self.config_path,
                                     emit=self._queue_refresh)
        self.apply_appearance()
        self.window = WxbWindow(self, self.service,
                                startup_page=self.startup_page,
                                show_onboarding=self.show_onboarding,
                                on_quit=self._window_closed,
                                auto_scan=self.auto_scan)
        if self.hotkeys:
            self.service.start_hotkeys()
        self.window.present()

    def _queue_refresh(self):
        if self.window is not None:
            self.window._queue_refresh()

    def _window_closed(self):
        # Keep the application alive only while the window is being destroyed.
        pass

    def _quit(self):
        if self.window is not None:
            self.window.close()
        else:
            self.quit()

    def _search(self):
        if self.window is not None:
            self.window.open_command()

    def _shortcuts(self):
        dialog = Adw.MessageDialog(
            transient_for=self.window,
            heading="Keyboard shortcuts",
            body="Ctrl K — search\nCtrl 1–7 — move between pages\n"
                 "Esc — close search, or stop every engine\n"
                 "F6 / F7 / F8 — clicker / rotation / SOCD\n"
                 "F9 — stop every engine\nCtrl Q — quit",
        )
        dialog.add_css_class("wxb-dialog")
        dialog.add_response("close", "Close")
        dialog.present()

    def _about(self):
        mono = bool(self.service.settings.get("monochrome"))
        dialog = Adw.AboutDialog(
            application_name="WXB Studio",
            application_icon=(APP_ID + ".Mono") if mono else APP_ID,
            version=STUDIO_VERSION,
            developer_name="WXB",
            comments="A quieter control room for Roblox on Linux.\n"
                     "Built with GTK 4 and libadwaita.",
        )
        dialog.add_css_class("wxb-dialog")
        dialog.add_css_class("wxb-about-dialog")
        # Adw.AboutDialog installs its own stylesheet on present.  Move our
        # provider to the end of the same priority after the sheet maps so
        # the studio surface wins over Adwaita's grey default.
        dialog.connect("map", lambda *_: self._raise_css_provider())
        dialog.present(self.window)

    def _raise_css_provider(self):
        if self._provider is None or self.window is None:
            return
        display = self.window.get_display()
        Gtk.StyleContext.remove_provider_for_display(display, self._provider)
        Gtk.StyleContext.add_provider_for_display(
            display, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def apply_appearance(self):
        """Load the active accent, with the neutral mono sheet on top when on."""
        settings = getattr(self, "service", None)
        settings = settings.settings if settings is not None else {}
        monochrome = bool(settings.get("monochrome", False))
        accent = ACCENTS.get(settings.get("accent", "cyan"), "#9fcaff")
        return self.apply_accent("#ffffff" if monochrome else accent,
                                 monochrome=monochrome)

    def apply_accent(self, accent, monochrome=False):
        display = Gdk.Display.get_default()
        if display is None:
            return False
        rgba = Gdk.RGBA()
        if not rgba.parse(str(accent)):
            accent = "#9fcaff"
            rgba.parse(accent)
        self._accent = accent
        self._monochrome = bool(monochrome)
        if self._provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display,
                                                         self._provider)
            self._provider = None
        try:
            css = CSS_PATH.read_text(encoding="utf-8")
        except OSError:
            # A missing stylesheet must not take the studio down.  GTK's
            # default theme is the fallback.
            return False
        if not _css_balanced(css):
            return False
        css = css.replace("@define-color wxb_accent #9fcaff;",
                          "@define-color wxb_accent %s;" % accent)
        if monochrome:
            try:
                mono = MONO_CSS_PATH.read_text(encoding="utf-8")
            except OSError:
                mono = ""
            # A damaged optional mono layer must not cost the user the
            # working dark theme; simply keep the neutral accent instead.
            if mono and _css_balanced(mono):
                css += "\n" + mono
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(css)
        except GLib.Error:
            # A renamed or damaged stylesheet is a packaging problem, not a
            # startup crash.  Keep the previous/default theme.
            return False
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._provider = provider
        return True

    def do_shutdown(self):
        if self.service is not None:
            self.service.shutdown()
        Adw.Application.do_shutdown(self)


# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="WXB Studio")
    parser.add_argument("--config", default=None,
                        help="alternate settings file (for previews and tests)")
    parser.add_argument("--page", default="overview",
                        choices=[p[0] for p in WxbWindow.PAGES])
    parser.add_argument("--no-onboarding", action="store_true")
    parser.add_argument("--standalone", action="store_true",
                        help="allow a second preview instance")
    parser.add_argument("--quit-after", type=float, default=0,
                        help="close automatically after N seconds (previews)")
    parser.add_argument("--command", action="store_true",
                        help="open the command palette on launch (previews)")
    args = parser.parse_args(argv)
    if args.standalone:
        os.environ["WXB_STANDALONE"] = "1"
    # Match StartupWMClass in WXB.desktop so the running window groups with
    # its icon instead of showing Python's generic class.
    GLib.set_prgname("wxb")
    GLib.set_application_name("WXB Studio")
    app = WxbApplication(config_path=args.config, startup_page=args.page,
                         show_onboarding=not args.no_onboarding)
    if args.quit_after:
        GLib.timeout_add_seconds(int(max(1, args.quit_after)),
                                 lambda: (app.quit(), False)[1])
    if args.command:
        def _open():
            if app.window is not None:
                app.window.open_command()
            return False
        GLib.timeout_add(500, _open)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
