#!/usr/bin/env python3
"""Transparent, opt-in Input Lab visualizer window.

The overlay is presentation-only.  It receives already-formatted key labels
and click coordinates, stores only a short animation queue, and never keeps a
text buffer.  It attempts to make its X11/GDK surface click-through; if that
cannot be done it hides itself and reports why instead of blocking the desktop.
"""

from __future__ import annotations

import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import wxb_input_lab as input_lab  # noqa: E402


class InputOverlayWindow(Gtk.Window):
    """Full-screen visualizer with a click-through input region."""

    def __init__(self, on_error=None) -> None:
        super().__init__(title="WXB Input Visualizer")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_can_focus(False)
        self.set_modal(False)
        self.set_default_size(640, 220)
        display = Gdk.Display.get_default()
        if display is not None:
            monitors = display.get_monitors()
            if monitors.get_n_items():
                geometry = monitors.get_item(0).get_geometry()
                self.set_default_size(max(320, geometry.width),
                                      max(240, geometry.height))
        self.add_css_class("wxb-visualizer")
        self._settings = dict(input_lab.DEFAULT_INPUT_LAB["visualizer"])
        self._keys_enabled = False
        self._clicks_enabled = False
        self._rings = []
        self._key_hide_at = 0.0
        self._timer = None
        self._css_provider = None
        self._click_through = False
        self._error = ""
        self.on_error = on_error

        self._keycap = Gtk.Label(label="")
        self._keycap.add_css_class("wxb-visualizer-key")
        self._keycap.set_halign(Gtk.Align.CENTER)
        self._keycap.set_valign(Gtk.Align.END)
        self._keycap.set_margin_bottom(38)
        self._keycap.set_visible(False)
        self._hint = Gtk.Label(
            label="Key names only · nothing is recorded or uploaded")
        self._hint.add_css_class("wxb-visualizer-hint")
        self._hint.set_halign(Gtk.Align.CENTER)
        self._hint.set_valign(Gtk.Align.END)
        self._hint.set_margin_bottom(12)

        self._drawing = Gtk.DrawingArea()
        self._drawing.set_draw_func(self._draw)
        self._drawing.set_hexpand(True)
        self._drawing.set_vexpand(True)
        self._drawing.set_can_target(False)
        self._keycap.set_can_target(False)
        self._hint.set_can_target(False)
        self._overlay = Gtk.Overlay()
        self._overlay.set_child(self._drawing)
        self._overlay.add_overlay(self._keycap)
        self._overlay.add_overlay(self._hint)
        self._overlay.set_can_target(False)
        self.set_child(self._overlay)

        self.connect("realize", self._on_realize)
        self.connect("map", self._on_map)
        self.connect("unmap", self._stop_timer)
        self.connect("destroy", self._on_destroy)

    @property
    def click_through(self) -> bool:
        return self._click_through

    @property
    def error(self) -> str:
        return self._error

    def apply_settings(self, visual: dict) -> None:
        """Apply bounded size/colour/opacity values and keycap CSS."""
        clean = input_lab.normalize_input_lab(
            {"visualizer": visual})["visualizer"]
        self._settings = clean
        size = int(clean["size"])
        colour = clean["color"]
        opacity = int(clean["opacity"]) / 100.0
        self.set_opacity(opacity)
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        css = (
            "window.wxb-visualizer { background-color: transparent; }\n"
            ".wxb-visualizer-key {"
            "  color: %s; font-size: %dpx; font-weight: 700;"
            "  padding: 7px 15px; border-radius: 12px;"
            "  background-color: rgba(8, 14, 26, 0.82);"
            "  border: 1px solid %s;"
            "}\n"
            ".wxb-visualizer-hint {"
            "  color: %s; font-size: 11px;"
            "  background-color: rgba(8, 14, 26, 0.62);"
            "  padding: 4px 10px; border-radius: 8px;"
            "}\n"
        ) % (colour, max(18, int(30 * size / 100)), colour, colour)
        try:
            provider.load_from_data(css.encode("utf-8"))
            if self._css_provider is not None:
                Gtk.StyleContext.remove_provider_for_display(
                    display, self._css_provider)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._css_provider = provider
        except Exception as exc:
            self._error = "Could not style visualizer: %s" % exc

    def set_enabled(self, keys: bool, clicks: bool) -> None:
        self._keys_enabled = bool(keys)
        self._clicks_enabled = bool(clicks)
        if not (keys or clicks):
            self.hide()
            self._stop_timer()
            return
        if not self._click_through and self.get_realized():
            self._error = ("The compositor refused a click-through input "
                           "region; the visualizer is disabled.")
            self._report_error(self._error)
            self.hide()
            return
        self._keycap.set_visible(bool(keys))
        if not keys:
            self._keycap.set_text("")
        if not clicks:
            self._rings.clear()
            self._drawing.queue_draw()
        self.show()
        try:
            self.fullscreen()
        except Exception:
            pass

    def show_key(self, label: str) -> None:
        if not self._keys_enabled or not label:
            return
        self._keycap.set_text(str(label)[:24])
        self._keycap.set_visible(True)
        self._key_hide_at = time.monotonic() + 1.1
        self._ensure_timer()
        self._ensure_visible()

    def show_click(self, label: str = "", x: int = 0, y: int = 0) -> None:
        if not self._clicks_enabled:
            return
        self._rings.append((max(0, int(x)), max(0, int(y)),
                            time.monotonic()))
        self._rings = self._rings[-8:]
        self._ensure_timer()
        self._ensure_visible()
        self._drawing.queue_draw()

    def _ensure_visible(self):
        if not self.get_visible():
            self._error = ("The visualizer is disabled because it cannot be "
                           "click-through in this session.")
            return
        self._drawing.queue_draw()

    def _on_realize(self, *_args):
        self._make_click_through()

    def _on_map(self, *_args):
        self._make_click_through()
        self._raise_above()
        self._keycap.set_visible(self._keys_enabled)

    def _make_click_through(self):
        surface = self.get_surface()
        if surface is None:
            self._error = "The visualizer has no GDK surface."
            return
        try:
            surface.set_input_region(cairo.Region())
            self._click_through = True
            self._error = ""
        except Exception as exc:
            self._click_through = False
            self._error = ("The compositor refused a click-through input "
                           "region: %s" % exc)
            self._report_error(self._error)
            self.hide()

    def _report_error(self, message):
        if self.on_error is None:
            return
        try:
            self.on_error(str(message))
        except Exception:
            pass

    def _raise_above(self):
        """Best-effort X11 above state; failure is harmless on Wayland."""
        try:
            import gi as _gi
            _gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
            from Xlib import Xatom, display
            surface = self.get_surface()
            if not isinstance(surface, GdkX11.X11Surface):
                return
            xid = surface.get_xid()
            d = display.Display()
            window = d.create_resource_object("window", xid)
            above = d.intern_atom("_NET_WM_STATE_ABOVE")
            state = d.intern_atom("_NET_WM_STATE")
            window.change_property(state, Xatom.ATOM, 32, [above])
            d.flush()
            d.close()
        except Exception:
            pass

    def _draw(self, _area, cr, width, height):
        now = time.monotonic()
        self._rings = [ring for ring in self._rings
                       if now - ring[2] < 0.55]
        for x, y, started in self._rings:
            age = max(0.0, min(1.0, (now - started) / 0.55))
            radius = 13 + age * 34
            alpha = max(0.0, 1.0 - age)
            cr.set_source_rgba(0.62, 0.79, 1.0, 0.80 * alpha)
            cr.set_line_width(2.5)
            cr.arc(x, y, radius, 0, 2 * 3.141592653589793)
            cr.stroke()
        return False

    def _ensure_timer(self):
        if self._timer is None:
            self._timer = GLib.timeout_add(33, self._tick)

    def _tick(self):
        now = time.monotonic()
        if self._key_hide_at and now >= self._key_hide_at:
            self._keycap.set_text("")
            self._keycap.set_visible(False)
            self._key_hide_at = 0.0
        self._rings = [ring for ring in self._rings
                       if now - ring[2] < 0.55]
        self._drawing.queue_draw()
        if not self._rings and not self._key_hide_at:
            self._timer = None
            return False
        return True

    def _stop_timer(self, *_args):
        if self._timer is not None:
            try:
                GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = None

    def _on_destroy(self, *_args):
        self._stop_timer()
        display = Gdk.Display.get_default()
        if display is not None and self._css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(
                    display, self._css_provider)
            except Exception:
                pass
            self._css_provider = None
