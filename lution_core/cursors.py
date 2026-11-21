# WXB addition, not upstream Lution.
"""Custom Roblox cursors, drawn into Lution's asset overlay.

Roblox ships its mouse pointers as PNGs inside the APK; Sober mounts
``data/sober/asset_overlay/content`` over that directory, and Lution mounts
``~/.local/share/Lution/overlay/content`` over the overlay at launch. Writing
the same relative paths therefore replaces the pointer for the next session,
without touching the APK (or Sober's install).

Only the three KeyboardMouse pointers are replaced (arrow, far arrow and
I-beam): the focus, gamepad and camera icons are left alone so their meaning
does not change. This is the same set upstream Lution replaces.
"""

import io
import json
import os
import urllib.request
import zipfile
from pathlib import Path

from . import paths

try:                                    # Pillow is already WXB's cover-art
    from PIL import Image, ImageDraw      # dependency; keep the import local
except ImportError:                     # so the rest of the bridge still works
    Image = ImageDraw = None

STYLES = ("Cross", "Dot", "Heart", "Paw", "Spark", "Arrow")
COLORS = {
    "Pink":     ("#ff8fbd", "#fff7fb"),
    "White":    ("#fffdfd", "#f08db3"),
    "Black":    ("#1b1b1b", "#ffffff"),
    "Mint":     ("#8fe8cf", "#ffffff"),
    "Lavender": ("#c7b3ff", "#fff7fb"),
}
SIZES = (16, 24, 32, 40, 48)
DEFAULT_STYLE = "Cross"
DEFAULT_COLOR = "Pink"
DEFAULT_SIZE = 32
CANVAS = 64

# A public-domain cursor pack, downloaded only when an online style is
# chosen.  The ZIP is cached under Lution's installed cursor folder so the
# choices keep working offline after the first download.
PACK_URL = "https://opengameart.org/sites/default/files/kenney_cursor-pack.zip"
PACK_NAME = "Kenney Cursor Pack"
PACK_LICENSE = "CC0 1.0"
PACK_CREDIT = "Kenney (kenney.nl)"
PACK_MEMBER_DIR = "PNG/Outline/Default"
MAX_PACK_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 1024 * 1024

# These are the labelled subset exposed in the UI.  ``top`` anchors the
# pointer at the canvas origin (the Roblox hotspot); ``center`` is for
# crosshair/target/tool cursors whose hotspot is their middle.
ONLINE_STYLES = (
    {"name": "Classic pointer", "member": "pointer_a", "anchor": "top"},
    {"name": "Flat pointer", "member": "pointer_b", "anchor": "top"},
    {"name": "Wide pointer", "member": "pointer_c", "anchor": "top"},
    {"name": "Round pointer", "member": "pointer_d", "anchor": "top"},
    {"name": "Sci-fi pointer", "member": "pointer_scifi_a", "anchor": "top"},
    {"name": "Toon pointer", "member": "pointer_toon_a", "anchor": "top"},
    {"name": "Pointing hand", "member": "hand_point", "anchor": "top"},
    {"name": "Open hand", "member": "hand_open", "anchor": "top"},
    {"name": "Crosshair", "member": "cross_large", "anchor": "center"},
    {"name": "Fine crosshair", "member": "cross_small", "anchor": "center"},
    {"name": "Target", "member": "target_a", "anchor": "center"},
    {"name": "Ring target", "member": "target_round_a", "anchor": "center"},
    {"name": "Hourglass", "member": "busy_hourglass", "anchor": "center"},
    {"name": "Zoom", "member": "zoom", "anchor": "center"},
    {"name": "Resize", "member": "resize_a_diagonal", "anchor": "center"},
    {"name": "Sword", "member": "tool_sword_a", "anchor": "center"},
    {"name": "Hammer", "member": "tool_hammer", "anchor": "center"},
    {"name": "East arrow", "member": "arrow_e", "anchor": "center"},
)
ONLINE_BY_NAME = {item["name"].lower(): item for item in ONLINE_STYLES}

# The in-window mouse pointers Lution replaces; Roblox reads these from the
# KeyboardMouse folder. IBeam is absent from the current APK but some builds
# ship it, and writing it is harmless when they do not.
ASSETS = (
    "Cursors/KeyboardMouse/ArrowCursor.png",
    "Cursors/KeyboardMouse/ArrowFarCursor.png",
    "Cursors/KeyboardMouse/IBeamCursor.png",
)
def overlay_textures():
    return paths.lution_overlay() / "content/textures"


def kept_dir():
    return paths.lution_root() / "installed_cursors"


def kept_file():
    return kept_dir() / "cursor.png"


def config_file():
    return paths.lution_root() / "cursor.json"


def pack_file():
    """The cached Kenney ZIP; no network is needed after this exists."""
    return kept_dir() / "kenney-cursor-pack.zip"


def _clean_choice(value, choices, fallback):
    text = str(value or "").strip()
    for choice in choices:
        if text.lower() == choice.lower():
            return choice
    return fallback


def _clean_size(value):
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return max(12, min(56, size))


def _style(name):
    """The lowercase drawing recipe for a style name."""
    return {"Dot": "dot", "Heart": "heart", "Paw": "paw",
            "Spark": "spark", "Arrow": "arrow"}.get(name, "cross")


def _draw_shape(draw, style, fill, outline, box):
    """Draw one cursor at 4x into `box` (l, t, r, b)."""
    l, t, r, b = box
    w = r - l
    h = b - t
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    stroke = max(3, int(w * 0.14))
    if style == "cross":
        arm = w * 0.34
        for dx, dy in ((-1, -1), (-1, 1)):
            draw.line((cx + dx * arm, cy + dy * arm,
                       cx - dx * arm, cy - dy * arm),
                      fill=outline, width=stroke + 8, joint="curve")
        for dx, dy in ((-1, -1), (-1, 1)):
            draw.line((cx + dx * arm, cy + dy * arm,
                       cx - dx * arm, cy - dy * arm),
                      fill=fill, width=stroke, joint="curve")
        for dx, dy in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
            x, y = cx + dx * arm, cy + dy * arm
            draw.ellipse((x - stroke / 2, y - stroke / 2,
                          x + stroke / 2, y + stroke / 2), fill=outline)
            draw.ellipse((x - stroke / 3, y - stroke / 3,
                          x + stroke / 3, y + stroke / 3), fill=fill)
        draw.ellipse((cx - w * 0.09, cy - h * 0.09,
                      cx + w * 0.09, cy + h * 0.09), fill=outline)
        draw.ellipse((cx - w * 0.055, cy - h * 0.055,
                      cx + w * 0.055, cy + h * 0.055), fill=fill)
    elif style == "dot":
        draw.ellipse((l, t, r, b), fill=outline)
        pad = w * 0.16
        draw.ellipse((l + pad, t + pad, r - pad, b - pad), fill=fill)
        draw.ellipse((cx - w * 0.10, cy - h * 0.16,
                      cx + w * 0.02, cy - h * 0.04),
                     fill="#ffffff")
    elif style == "heart":
        rr = w * 0.27
        draw.ellipse((cx - rr * 2.0, cy - rr * 1.35,
                      cx, cy + rr * 0.65), fill=outline)
        draw.ellipse((cx, cy - rr * 1.35,
                      cx + rr * 2.0, cy + rr * 0.65), fill=outline)
        draw.polygon(((cx - rr * 1.92, cy - rr * 0.12),
                      (cx + rr * 1.92, cy - rr * 0.12),
                      (cx, b)), fill=outline)
        draw.ellipse((l + stroke, t + stroke, cx - stroke, cy + rr * 0.3),
                     fill=fill)
        draw.ellipse((cx + stroke, t + stroke, r - stroke, cy + rr * 0.3),
                     fill=fill)
        draw.polygon(((cx - rr * 1.62, cy - rr * 0.18),
                      (cx + rr * 1.62, cy - rr * 0.18),
                      (cx, b - stroke * 1.2)), fill=fill)
        draw.ellipse((cx - rr * 0.95, cy - rr * 0.95,
                      cx - rr * 0.30, cy - rr * 0.30), fill="#ffffff")
    elif style == "paw":
        pad_w, pad_h = w * 0.46, h * 0.38
        draw.ellipse((cx - pad_w / 2, cy - pad_h * 0.15,
                      cx + pad_w / 2, cy + pad_h * 0.9), fill=outline)
        draw.ellipse((cx - pad_w / 2 + stroke, cy - pad_h * 0.15 + stroke,
                      cx + pad_w / 2 - stroke, cy + pad_h * 0.9 - stroke),
                     fill=fill)
        toes = ((-0.33, -0.33, 0.13), (-0.12, -0.50, 0.14),
                (0.12, -0.50, 0.14), (0.33, -0.33, 0.13))
        for dx, dy, rr in toes:
            x, y, rad = cx + dx * w, cy + dy * h, rr * w
            draw.ellipse((x - rad - stroke, y - rad - stroke,
                          x + rad + stroke, y + rad + stroke), fill=outline)
            draw.ellipse((x - rad, y - rad, x + rad, y + rad), fill=fill)
    elif style == "spark":
        q = w * 0.16
        pts = ((cx, t), (cx + q, cy - q), (r, cy), (cx + q, cy + q),
               (cx, b), (cx - q, cy + q), (l, cy), (cx - q, cy - q))
        draw.polygon(pts, fill=outline)
        q2 = q * 0.62
        inset = stroke * 1.1
        pts2 = ((cx, t + inset), (cx + q2, cy - q2),
                (r - inset, cy), (cx + q2, cy + q2),
                (cx, b - inset), (cx - q2, cy + q2),
                (l + inset, cy), (cx - q2, cy - q2))
        draw.polygon(pts2, fill=fill)
        draw.ellipse((cx - q2 * 0.32, cy - q2 * 0.32,
                      cx + q2 * 0.32, cy + q2 * 0.32), fill="#ffffff")
    else:                                   # arrow
        tip = (l + w * 0.10, t + h * 0.08)
        pts = (tip,
               (l + w * 0.80, t + h * 0.40),
               (l + w * 0.52, t + h * 0.44),
               (l + w * 0.72, t + h * 0.78),
               (l + w * 0.56, t + h * 0.88),
               (l + w * 0.37, t + h * 0.55),
               (l + w * 0.10, t + h * 0.74))
        draw.polygon(pts, fill=outline)
        inset = w * 0.045
        pts2 = ((tip[0] + inset, tip[1] + inset),
                (l + w * 0.70, t + h * 0.40),
                (l + w * 0.47, t + h * 0.43),
                (l + w * 0.65, t + h * 0.75),
                (l + w * 0.57, t + h * 0.79),
                (l + w * 0.40, t + h * 0.51),
                (l + w * 0.12, t + h * 0.68))
        draw.polygon(pts2, fill=fill)


def render(style=DEFAULT_STYLE, color=DEFAULT_COLOR, size=DEFAULT_SIZE):
    """A 64x64 RGBA cursor, or raises ValueError without Pillow."""
    if Image is None or ImageDraw is None:
        raise ValueError("Pillow is needed to draw a cursor (pip install Pillow)")
    style = _clean_choice(style, STYLES, DEFAULT_STYLE)
    color = _clean_choice(color, tuple(COLORS), DEFAULT_COLOR)
    fill, outline = COLORS[color]
    size = _clean_size(size)
    # Draw four times bigger and downsample: PIL has no antialiased shapes.
    scale = 4
    big = Image.new("RGBA", (CANVAS * scale, CANVAS * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    span = size * scale
    left = (CANVAS * scale - span) / 2.0
    top = (CANVAS * scale - span) / 2.0
    _draw_shape(draw, _style(style), fill, outline,
                (left, top, left + span, top + span))
    return big.resize((CANVAS, CANVAS), Image.LANCZOS)


def _prepare_online(image, size, anchor):
    """Fit a downloaded cursor PNG onto the 64px Roblox canvas."""
    image = image.convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("cursor image is fully transparent")
    image = image.crop(bbox)
    target = _clean_size(size)
    scale = min(target / float(image.width), target / float(image.height))
    fitted = (max(1, int(round(image.width * scale))),
              max(1, int(round(image.height * scale))))
    if fitted != image.size:
        image = image.resize(fitted, Image.NEAREST)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    if anchor == "top":
        position = (0, 0)
    else:
        position = ((CANVAS - image.width) // 2,
                    (CANVAS - image.height) // 2)
    canvas.alpha_composite(image, position)
    return canvas


def _download_pack():
    """Fetch and atomically cache the public-domain cursor ZIP."""
    path = pack_file()
    if path.is_file() and zipfile.is_zipfile(path):
        return path
    try:
        path.unlink()
    except OSError:
        pass
    request = urllib.request.Request(
        PACK_URL, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) WXB/4.1"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = response.read(MAX_PACK_BYTES + 1)
    except Exception as exc:
        raise ValueError("could not download the cursor pack: %s" % exc)
    if len(data) > MAX_PACK_BYTES:
        raise ValueError("cursor pack is larger than expected")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError("cursor pack download was not a ZIP archive")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".wxb-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def _load_online_image(style):
    entry = ONLINE_BY_NAME.get(str(style or "").strip().lower())
    if entry is None:
        raise ValueError("unknown online cursor style")
    pack = _download_pack()
    member = "%s/%s.png" % (PACK_MEMBER_DIR, entry["member"])
    try:
        with zipfile.ZipFile(pack) as archive:
            info = archive.getinfo(member)
            if info.file_size > MAX_IMAGE_BYTES:
                raise ValueError("cursor image is larger than expected")
            data = archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ValueError("cursor pack has no usable %s image: %s" %
                         (entry["member"], exc))
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("cursor image dimensions are too large")
        image.load()
    except (OSError, ValueError) as exc:
        raise ValueError("cursor image is not readable: %s" % exc)
    return entry, image


def _load_config():
    config = config_file()
    try:
        data = json.loads(config.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_config(data):
    config = config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.with_name(config.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, config)


def _paths():
    overlay = overlay_textures()
    return [overlay / name for name in ASSETS]


def status():
    """What is installed, without changing anything."""
    overlay = overlay_textures()
    active = [p for p in _paths() if p.is_file()]
    cfg = _load_config()
    mode = "online" if cfg.get("mode") == "online" else "drawn"
    if mode == "online":
        style = str(cfg.get("style") or "Online cursor")
        color = "Original"
    else:
        style = _clean_choice(cfg.get("style"), STYLES, DEFAULT_STYLE)
        color = _clean_choice(cfg.get("color"), tuple(COLORS), DEFAULT_COLOR)
    return {
        "active": len(active),
        "assets": len(ASSETS),
        "style": style,
        "color": color,
        "size": _clean_size(cfg.get("size")),
        "mode": mode,
        "online": mode == "online",
        "source": str(cfg.get("source") or ""),
        "credit": str(cfg.get("credit") or ""),
        "pack_cached": pack_file().is_file(),
        "overlay": str(overlay),
        "pillow": Image is not None,
    }


def _write_cursor(image, config):
    paths.ensure_lution_dirs()
    overlay = overlay_textures()
    kept = kept_file()
    overlay.mkdir(parents=True, exist_ok=True)
    kept.parent.mkdir(parents=True, exist_ok=True)
    image.save(kept, "PNG")
    written = []
    for dest in _paths():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".wxb-tmp")
        image.save(tmp, "PNG")
        os.replace(tmp, dest)
        written.append(dest)
    data = dict(config)
    data["files"] = [str(p.relative_to(overlay)) for p in written]
    _save_config(data)
    return {"ok": True, "cursor": status()}


def apply(style=DEFAULT_STYLE, color=DEFAULT_COLOR, size=DEFAULT_SIZE):
    """Draw and place the pointer overrides. Returns the new status."""
    style = _clean_choice(style, STYLES, None)
    if style is None:
        return {"ok": False, "error": "unknown cursor style"}
    color = _clean_choice(color, tuple(COLORS), None)
    if color is None:
        return {"ok": False, "error": "unknown cursor colour"}
    try:
        wanted = int(str(size).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "cursor size must be a whole number"}
    if not 12 <= wanted <= 56:
        return {"ok": False, "error": "cursor size must be 12-56 pixels"}
    image = render(style, color, wanted)
    return _write_cursor(image, {"mode": "drawn", "style": style,
                                 "color": color, "size": wanted})


def apply_online(style, size=DEFAULT_SIZE):
    """Download the CC0 pack if needed and apply one online cursor."""
    if Image is None or ImageDraw is None:
        return {"ok": False,
                "error": "Pillow is needed to apply a cursor (pip install Pillow)"}
    entry = ONLINE_BY_NAME.get(str(style or "").strip().lower())
    if entry is None:
        return {"ok": False, "error": "unknown online cursor style"}
    try:
        wanted = int(str(size).strip())
    except (TypeError, ValueError):
        return {"ok": False, "error": "cursor size must be a whole number"}
    if not 12 <= wanted <= 56:
        return {"ok": False, "error": "cursor size must be 12-56 pixels"}
    try:
        entry, image = _load_online_image(entry["name"])
        prepared = _prepare_online(image, wanted, entry["anchor"])
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return _write_cursor(prepared, {
        "mode": "online", "style": entry["name"], "color": "Original",
        "size": wanted, "source": PACK_URL, "license": PACK_LICENSE,
        "credit": PACK_CREDIT,
    })


def restore():
    """Remove the pointer overrides and the kept copy."""
    removed = 0
    for path in _paths():
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError:
            pass
    for path in (kept_file(), config_file()):
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    return {"ok": True, "removed": removed, "cursor": status()}


def __getattr__(name):
    """Keep the historical path names as dynamic attributes."""
    getters = {
        "OVERLAY_TEXTURES": overlay_textures,
        "KEPT_DIR": kept_dir,
        "KEPT_FILE": kept_file,
        "CONFIG_FILE": config_file,
    }
    try:
        return getters[name]()
    except KeyError:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
