"""Icons transcribed from the inline SVGs in src/App.tsx.

The design has no asset files -- every glyph is an inline <svg> with explicit
path data. Rather than export PNGs (which would need regenerating per DPI and
per colour), each icon is redrawn here with Pillow from the same coordinates,
supersampled and downscaled for antialiasing.

Icons are cached by (name, colour, size) because a card rebuild would otherwise
redraw them on every state change.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

import customtkinter as ctk

logger = logging.getLogger("emcc.ui")

_SS = 8  # supersample factor
_OVERSAMPLE = 4  # render at 4x nominal so CTkImage downscales rather than blurs

_cache: dict[tuple, ctk.CTkImage] = {}


def _rgba(hex_color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), round(255 * alpha))


class _Canvas:
    """Thin wrapper that maps SVG viewBox units onto a supersampled bitmap."""

    def __init__(self, view: float, px: int):
        self.scale = (px * _SS) / view
        self.img = Image.new("RGBA", (px * _SS, px * _SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def _p(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale, y * self.scale

    def line(self, pts: list[tuple[float, float]], color, width: float, closed=False):
        xy = [self._p(*p) for p in pts]
        if closed:
            xy.append(xy[0])
        self.d.line(xy, fill=color, width=max(1, round(width * self.scale)), joint="curve")
        # Round the joins/caps: Pillow's joint="curve" leaves gaps at sharp turns.
        r = width * self.scale / 2
        for x, y in xy:
            self.d.ellipse((x - r, y - r, x + r, y + r), fill=color)

    def circle(self, cx, cy, r, color, width: float | None = None):
        x, y = self._p(cx, cy)
        rr = r * self.scale
        box = (x - rr, y - rr, x + rr, y + rr)
        if width is None:
            self.d.ellipse(box, fill=color)
        else:
            self.d.ellipse(box, outline=color, width=max(1, round(width * self.scale)))

    def finish(self, px: int) -> Image.Image:
        return self.img.resize((px, px), Image.LANCZOS)


def _draw_camera(px: int, color: str) -> Image.Image:
    """Aperture mark: r=5.5 ring, r=2.5 half-opacity disc, r=1 core, 4 ticks."""
    c = _Canvas(14, px)
    col = _rgba(color)
    c.circle(7, 7, 5.5, col, width=1.2)
    c.circle(7, 7, 2.5, _rgba(color, 0.5))
    c.circle(7, 7, 1.0, col)
    for pts in ([(7, 1.5), (7, 3)], [(7, 11), (7, 12.5)],
                [(1.5, 7), (3, 7)], [(11, 7), (12.5, 7)]):
        c.line(pts, col, 1.2)
    return c.finish(px)


def _draw_pencil(px: int, color: str) -> Image.Image:
    """M1 9.5L7.5 3 9 4.5l-6.5 6.5H1V9.5z  +  M6.5 2L9 4.5"""
    c = _Canvas(11, px)
    col = _rgba(color)
    c.line([(1, 9.5), (7.5, 3), (9, 4.5), (2.5, 11), (1, 11), (1, 9.5)], col, 1.2, closed=True)
    c.line([(6.5, 2), (9, 4.5)], col, 1.2)
    return c.finish(px)


def _draw_check(px: int, color: str) -> Image.Image:
    """M1.5 6.5l3 3 6-6"""
    c = _Canvas(12, px)
    c.line([(1.5, 6.5), (4.5, 9.5), (10.5, 3.5)], _rgba(color), 1.5)
    return c.finish(px)


def _draw_warning(px: int, color: str) -> Image.Image:
    """M6 1L11 10H1L6 1z  +  M6 5v2.5  +  dot at (6, 8.5) r=0.6"""
    c = _Canvas(12, px)
    col = _rgba(color)
    c.line([(6, 1), (11, 10), (1, 10)], col, 1.3, closed=True)
    c.line([(6, 5), (6, 7.5)], col, 1.3)
    c.circle(6, 8.5, 0.6, col)
    return c.finish(px)


def _draw_trash(px: int, color: str) -> Image.Image:
    """Remove-device control.

    Not from the export -- the design has no delete affordance -- so it is
    drawn to match the others: 1.2 stroke weight on an 11-unit box, same as
    the pencil it replaces.
    """
    c = _Canvas(11, px)
    col = _rgba(color)
    c.line([(1.2, 3.0), (9.8, 3.0)], col, 1.2)              # lid
    c.line([(4.3, 2.0), (6.7, 2.0)], col, 1.2)              # handle
    c.line([(2.4, 3.0), (3.0, 10.0), (8.0, 10.0), (8.6, 3.0)], col, 1.2)  # body
    c.line([(4.5, 5.0), (4.7, 8.3)], col, 1.0)              # ribs
    c.line([(6.5, 5.0), (6.3, 8.3)], col, 1.0)
    return c.finish(px)


_DRAW = {
    "camera": _draw_camera,
    "pencil": _draw_pencil,
    "check": _draw_check,
    "warning": _draw_warning,
    "trash": _draw_trash,
}


# A CTkImage wraps a PhotoImage owned by one Tk interpreter, and it creates
# that PhotoImage *lazily* and caches it internally. So reusing a cached
# CTkImage under a different root hands out a PhotoImage belonging to a dead
# interpreter, and Tk raises `image "pyimageN" doesn't exist`.
#
# The cache is therefore cleared *explicitly* by `App.__init__`, not inferred.
# Two earlier attempts were both wrong and worth recording so they are not
# retried:
#
#   * comparing `tkinter._default_root` -- tkinter assigns that only for the
#     FIRST Tk instance ever created, so a leaked root leaves it stale forever
#     while new roots never claim it.
#   * comparing `widget.tk` by identity -- a freed interpreter object can be
#     reallocated at the same address, so `is` false-matches intermittently.
#
# Explicit invalidation has neither failure mode.


def get(name: str, color: str, size: int) -> ctk.CTkImage:
    """Return a cached CTkImage of `name` tinted `color`, `size` px nominal."""
    key = (name, color, size)
    if key not in _cache:
        img = _DRAW[name](size * _OVERSAMPLE, color)
        _cache[key] = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    return _cache[key]


def blank(size: int) -> ctk.CTkImage:
    """A fully transparent image of `size` px, for emptying an icon slot.

    `CTkLabel.configure(image=None)` records the None but does *not* clear the
    underlying Tk label's `image` option, so the previous glyph stays on
    screen -- which left Clean's tick showing after a sweep ended. Swapping in
    a transparent image of the same size empties the slot without relying on
    that, and keeps the slot's width fixed either way.
    """
    key = ("__blank__", "", size)
    if key not in _cache:
        img = Image.new("RGBA", (size * _OVERSAMPLE, size * _OVERSAMPLE), (0, 0, 0, 0))
        _cache[key] = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    return _cache[key]


# ---------------------------------------------------------------------------
# File-backed artwork
# ---------------------------------------------------------------------------

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_FILE = ASSET_DIR / "echo_logo_dark.png"


def load_logo(height: int) -> ctk.CTkImage | None:
    """The Echovista wordmark, scaled to `height` px. None if unavailable.

    The source PNG carries transparent padding (a 1968x671 mark inside a
    2172x724 canvas), so it is cropped to its alpha bounding box first.
    Otherwise `height` would size the *canvas* and the visible mark would come
    out ~4% smaller than asked for, and be off-centre, because the padding is
    not symmetric.

    Aspect ratio is taken from the cropped mark, so the caller only chooses a
    height. Returns None rather than raising: a missing or corrupt asset should
    cost the banner its logo, not stop the application from starting.
    """
    key = ("__logo__", str(LOGO_FILE), height)
    if key in _cache:
        return _cache[key]
    try:
        with Image.open(LOGO_FILE) as source:
            image = source.convert("RGBA")
            box = image.getbbox()
            if box is not None:
                image = image.crop(box)
            width = max(1, round(height * image.width / image.height))
            # Hand CTkImage the full-resolution crop and let it downscale, so
            # the mark stays crisp at any DPI scaling.
            logo = ctk.CTkImage(light_image=image, dark_image=image,
                                size=(width, height))
    except (OSError, ValueError) as exc:
        logger.warning("logo asset unavailable (%s): %s", LOGO_FILE, exc)
        return None
    _cache[key] = logo
    return logo


def clear_cache() -> None:
    """Drop every cached image. Must be called when a new Tk root is created."""
    _cache.clear()
