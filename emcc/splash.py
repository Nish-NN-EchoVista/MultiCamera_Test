"""The launch splash shown while PyGUI starts.

PyGUI takes roughly 7-8s from click to usable: ~1.3s importing, ~1-2.5s
building its window, then its own connect. This covers that gap.

Why the splash lives *here* and not in PyGUI
--------------------------------------------
PyGUI's main thread is the thing that is busy. A splash drawn by PyGUI would
be a frozen image for most of its life, because the thread that would animate
it is the thread doing the work. EMCC, by contrast, is idle from the moment
`Popen` returns, so its event loop can hold a steady 60fps.

Why a layered window rather than a Tk canvas
--------------------------------------------
Tk cannot composite per-pixel alpha: `-transparentcolor` keys out exactly one
colour, so feathered edges, glows and shadows fringe against it. A Windows
layered window blends a full RGBA bitmap against the desktop, which is what
makes this read as a floating object rather than a rectangle.

It is also *faster*. Measured on this machine: 0.22ms per frame against a
16.7ms budget for 60fps -- about 75x headroom -- because a frame costs one
`memmove` into a DIB and the compositor does the blending. The equivalent Tk
canvas path measured 0.6-0.7ms.

Two consequences worth knowing:

* Drawing does not go through Tk at all. The Toplevel exists to own a window
  handle and to give us `after()` for frame timing; nothing is packed into it.
* The extended style must go on `frame()`, not `winfo_id()`. Those differ,
  and setting it on the wrong one silently does nothing.

Frames are pre-rendered because decoding or drawing during playback is what
makes Tk animations stutter. Pre-rendering is pure Pillow with no Tk
involved, so it runs on a background thread at startup (~390ms) and the
frames are resident long before the operator clicks anything.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import icons, theme

logger = logging.getLogger("emcc.ui")

# -- geometry ---------------------------------------------------------------
#
# These are 1.5x the original 540x240 design. Every value is scaled and the
# artwork is *re-rendered* at the new size -- the finished bitmap is never
# resampled. That distinction is what preserves quality: the wordmark asset is
# 2172x724 native, so even LOGO_W 480 still downsamples through LANCZOS, and
# everything else (card, text, gradient ramp) is drawn vectorially by Pillow at
# render time and so is crisp at any size.

WIDTH, HEIGHT = 810, 360
#: Inset of the card inside the canvas. The margin is where the shadow and
#: glow live, so it cannot be zero.
MARGIN = 36
CARD_RADIUS = 24

LOGO_W = 480
LOGO_Y = 87
TRACK_W = 480
TRACK_Y = 258
#: 3 rather than 2: a 1px rule inside a 1.5x card reads as a hairline artefact
#: rather than a deliberate line.
TRACK_H = 3

#: Room either side of the track for the sweep's bloom to spill into, and how
#: much thicker the sweep bar is than the track. Both live here rather than
#: with the animation constants because the status band's position is derived
#: from them.
SWEEP_BLEED = 18
BAR_PAD = 3

#: Where the sweep layer's lowest pixel can fall.
#:
#: The bloom extends well below the track -- a blurred strip of
#: `TRACK_H + BAR_PAD + 2*SWEEP_BLEED` rows -- and the status band overwrites
#: whole rows with static content, so a band starting above this would erase
#: the bottom of the glow in every frame.
#:
#: This is re-derived rather than scaled, and doing so found that the original
#: 540x240 layout overlapped by 8 rows: its guard test asserted only
#: `BAND_Y > TRACK_Y + TRACK_H`, which checks the *track* and not the bloom.
#: Invisible at that blur radius, but the invariant was not holding.
SWEEP_BOTTOM = TRACK_Y - BAR_PAD // 2 - SWEEP_BLEED + (
    TRACK_H + BAR_PAD + SWEEP_BLEED * 2
)

#: Rows carrying the status text. Replaced in place when the status changes,
#: so it must clear the sweep entirely -- see `set_status`.
BAND_Y = SWEEP_BOTTOM + 3
BAND_H = 60

#: Vertical centre of the status text, as an offset inside the band.
#:
#: Derived, not hardcoded. The original was `BAND_H // 2 - 6`, and that `-6`
#: was tuned by eye for a 12px font -- it does not scale with either the band
#: or the font, so multiplying it would have drifted the text off centre.
#: This places the text on the midpoint between the track and the card's
#: bottom edge, which is what the eye actually reads as centred.
STATUS_CENTRE_Y = (TRACK_Y + TRACK_H + (HEIGHT - MARGIN)) // 2 - BAND_Y

# -- animation --------------------------------------------------------------

FPS = 60
FRAME_INTERVAL_MS = 16          # 1000/60, rounded to Tk's practical floor
FRAME_COUNT = 60                # a 1.0s loop
SWEEP_TAIL = 165                # length of the comet tail, px (1.5x)

FADE_MS = 16
FADE_STEPS = 12

#: How often to ask whether PyGUI is up. Cheap, and 150ms is imperceptible
#: against a multi-second launch.
POLL_INTERVAL_MS = 150

# -- win32 ------------------------------------------------------------------

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080   # keeps it out of the taskbar and Alt-Tab
ULW_ALPHA = 0x00000002
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL

# 64-bit wants the Ptr variants; the plain ones truncate a handle.
_get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------


def _rgba(hex_colour: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = hex_colour.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Segoe UI at `size`, or Pillow's built-in if it is unavailable."""
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    for candidate in (Path("C:/Windows/Fonts") / name, Path("C:/Windows/Fonts/arial.ttf")):
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _compose_card() -> Image.Image:
    """The static composition: shadow, card, wordmark and the sweep's track.

    Everything that does not move. Frames are this plus a sweep, and the
    status band is cropped from this so replacing the text keeps the card
    behind it intact.
    """
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    box = (MARGIN, MARGIN, WIDTH - MARGIN, HEIGHT - MARGIN)

    # Drop shadow: a blurred black card, nudged down. Feathered edges like
    # this are the whole reason for per-pixel alpha.
    shadow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0], box[1] + 9, box[2], box[3] + 12), CARD_RADIUS, fill=(0, 0, 0, 150)
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    # A faint brand-blue bloom, so the card sits in light rather than on top.
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(box, CARD_RADIUS,
                                           outline=_rgba(theme.BLUE_600, 90), width=5)
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))

    # The card itself. Slightly translucent, so the desktop reads through it
    # just enough to feel like glass without hurting legibility.
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, CARD_RADIUS, fill=_rgba(theme.TITLEBAR_BG, 238),
                           outline=_rgba(theme.BORDER_CARD, 255), width=2)

    # Wordmark, cropped to its own ink so padding in the asset cannot offset it.
    try:
        with Image.open(icons.LOGO_FILE) as source:
            logo = source.convert("RGBA")
            bbox = logo.getbbox()
            if bbox:
                logo = logo.crop(bbox)
            height = max(1, round(LOGO_W * logo.height / logo.width))
            logo = logo.resize((LOGO_W, height), Image.LANCZOS)
        canvas.alpha_composite(logo, ((WIDTH - LOGO_W) // 2, LOGO_Y))
    except (OSError, ValueError) as exc:
        logger.warning("splash: logo unavailable (%s)", exc)
        draw.text((WIDTH // 2, 144), "ECHOVISTA", font=_font(39, bold=True),
                  fill=_rgba(theme.BLUE_400), anchor="mm")

    # The track the sweep runs along.
    track_x = (WIDTH - TRACK_W) // 2
    draw.rounded_rectangle(
        (track_x, TRACK_Y, track_x + TRACK_W, TRACK_Y + TRACK_H),
        TRACK_H, fill=_rgba(theme.BORDER_INPUT, 255),
    )
    return canvas


def _sweep_layer(phase: float) -> Image.Image:
    """The moving highlight, as a full-canvas layer to composite.

    `phase` runs 0..1 over one loop. The tail is an eased alpha ramp rather
    than a hard block, and the result is blurred, so the light appears to
    trail rather than slide.

    Built on a strip the width of the track, not on the full canvas, so the
    highlight is *clipped to the track by construction*. Drawing it straight
    onto the canvas let the head and tail run past the track and over the
    card's edge, which read as a rendering fault.
    """
    strip_w = TRACK_W + SWEEP_BLEED * 2
    strip_h = TRACK_H + BAR_PAD + SWEEP_BLEED * 2
    strip = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))

    # Travel one tail-length beyond each end, so the light enters and leaves
    # instead of appearing and vanishing mid-track.
    head = phase * (TRACK_W + SWEEP_TAIL * 2) - SWEEP_TAIL

    ramp = Image.new("L", (SWEEP_TAIL, 1))
    ramp.putdata([int(255 * (i / (SWEEP_TAIL - 1)) ** 2.2) for i in range(SWEEP_TAIL)])
    mask = ramp.resize((SWEEP_TAIL, TRACK_H + BAR_PAD), Image.BILINEAR)
    bar = Image.new("RGBA", (SWEEP_TAIL, TRACK_H + BAR_PAD),
                    _rgba(theme.BLUE_400))

    # Anything outside the strip is dropped by paste, which is the clipping.
    strip.paste(bar, (int(head - SWEEP_TAIL) + SWEEP_BLEED, SWEEP_BLEED), mask)

    # A wider, dimmer copy underneath reads as light spilling onto the card.
    bloom = strip.filter(ImageFilter.GaussianBlur(9))
    bloom.putalpha(bloom.getchannel("A").point(lambda v: int(v * 0.7)))
    bloom.alpha_composite(strip)

    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    track_x = (WIDTH - TRACK_W) // 2
    layer.alpha_composite(bloom, (track_x - SWEEP_BLEED,
                                  TRACK_Y - BAR_PAD // 2 - SWEEP_BLEED))
    return layer


class FrameBank:
    """Pre-rendered frames, built once off the UI thread.

    Frames are `bytearray` rather than `bytes` so the status band can be
    written into them in place -- see `LaunchSplash.set_status`.
    """

    def __init__(self) -> None:
        self._frames: list[bytearray] | None = None
        self._card: Image.Image | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def prepare_async(self) -> None:
        """Start building in the background. Safe to call more than once."""
        with self._lock:
            if self._frames is not None or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._build, name="splash-prerender", daemon=True
            )
            self._thread.start()

    def _build(self) -> None:
        try:
            card = _compose_card()
            frames = []
            for index in range(FRAME_COUNT):
                frame = card.copy()
                frame.alpha_composite(_sweep_layer(index / FRAME_COUNT))
                frames.append(bytearray(frame.tobytes("raw", "BGRa")))
        except Exception:
            logger.exception("splash: pre-render failed; splash disabled")
            with self._lock:
                self._thread = None
            return
        with self._lock:
            self._card, self._frames, self._thread = card, frames, None
        logger.debug("splash: %d frames pre-rendered", FRAME_COUNT)

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._frames is not None

    def get(self) -> tuple[list[bytearray], Image.Image] | None:
        """Frames and the static card, or None if not built yet."""
        with self._lock:
            if self._frames is None or self._card is None:
                return None
            return self._frames, self._card


#: One bank per process. The frames are identical for every device, so there
#: is nothing per-splash to cache.
FRAMES = FrameBank()


def prepare() -> None:
    """Begin pre-rendering. Call once, after the main window has painted."""
    FRAMES.prepare_async()


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class LaunchSplash:
    """A floating, animated splash for one PyGUI launch.

    Lifecycle: `show()`, then `set_status()` as things progress, then
    `close()` -- or `fail()` if PyGUI never arrives. Every method is safe to
    call after the window is gone, so a late timer cannot raise.
    """

    def __init__(self, master: ctk.CTk, on_dismiss: Callable[[], None] | None = None):
        self._master = master
        self._on_dismiss = on_dismiss
        self._window: ctk.CTkToplevel | None = None
        self._frames: list[bytearray] = []
        self._card: Image.Image | None = None
        self._index = 0
        self._alpha = 0
        self._closing = False
        self._anim_job: str | None = None
        self._fade_job: str | None = None
        self._status = ""

        self._hwnd = 0
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._old_bitmap = None
        self._bits = None

    # -- public ------------------------------------------------------------

    def show(self, status: str) -> bool:
        """Create and start the splash. False if it could not be shown."""
        bank = FRAMES.get()
        if bank is None:
            # Not pre-rendered yet (an unusually fast click, or the build
            # failed). Going without is better than blocking on ~390ms of
            # rendering at the exact moment the operator wants a response.
            logger.info("splash: frames not ready, skipping")
            return False
        self._frames, self._card = bank

        try:
            self._create_window()
        except Exception:
            logger.exception("splash: could not create the layered window")
            self._destroy_window()
            return False

        self.set_status(status)
        self._tick()
        self._fade(target=255)
        return True

    def set_status(self, text: str) -> None:
        """Replace the status line, in place across every frame."""
        if text == self._status or self._card is None:
            return
        self._status = text
        band = self._render_band(text)
        offset = BAND_Y * WIDTH * 4
        length = BAND_H * WIDTH * 4
        for frame in self._frames:
            frame[offset:offset + length] = band

    def fail(self, message: str, close_after_ms: int = 4000) -> None:
        """Report that the launch did not complete, then close."""
        self.set_status(message)
        if self._window is not None:
            self._window.after(close_after_ms, self.close)

    def close(self) -> None:
        """Fade out and destroy. Idempotent."""
        if self._closing:
            return
        self._closing = True
        self._fade(target=0, then=self._destroy_window)

    @property
    def alive(self) -> bool:
        return self._window is not None and not self._closing

    # -- status band -------------------------------------------------------

    def _render_band(self, text: str) -> bytes:
        """Premultiplied bytes for the status rows, card background included.

        The band is written over whole rows, so it has to carry the card
        behind the text rather than just the glyphs. Cropping it from the
        static card is why the sweep must stay clear of these rows.
        """
        assert self._card is not None
        band = self._card.crop((0, BAND_Y, WIDTH, BAND_Y + BAND_H)).copy()
        draw = ImageDraw.Draw(band)
        draw.text((WIDTH // 2, STATUS_CENTRE_Y), text, font=_font(18),
                  fill=_rgba(theme.TEXT_MONO), anchor="mm")
        return band.tobytes("raw", "BGRa")

    # -- window ------------------------------------------------------------

    def _create_window(self) -> None:
        window = ctk.CTkToplevel(self._master)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        # Nothing is packed in: the visible content is the layered surface.
        # Position centred on EMCC, so it appears where the operator is looking.
        self._master.update_idletasks()
        x = self._master.winfo_rootx() + (self._master.winfo_width() - WIDTH) // 2
        y = self._master.winfo_rooty() + (self._master.winfo_height() - HEIGHT) // 3
        window.geometry(f"{WIDTH}x{HEIGHT}+{max(0, x)}+{max(0, y)}")
        window.update_idletasks()
        window.bind("<Button-1>", lambda _e: self.close())
        self._window = window

        raw = window.frame()
        self._hwnd = int(raw, 16) if str(raw).startswith("0x") else int(raw)
        style = _get_style(self._hwnd, GWL_EXSTYLE)
        _set_style(self._hwnd, GWL_EXSTYLE,
                   style | WS_EX_LAYERED | WS_EX_TOOLWINDOW)

        self._screen_dc = user32.GetDC(0)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = WIDTH
        info.bmiHeader.biHeight = -HEIGHT      # negative == top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        self._bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(
            self._screen_dc, ctypes.byref(info), 0, ctypes.byref(self._bits), None, 0
        )
        self._old_bitmap = gdi32.SelectObject(self._mem_dc, self._bitmap)

    def _push(self, frame: bytearray) -> None:
        if self._window is None or self._bits is None:
            return
        buffer = (ctypes.c_char * len(frame)).from_buffer(frame)
        ctypes.memmove(self._bits, buffer, len(frame))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, self._alpha, AC_SRC_ALPHA)
        size = wintypes.SIZE(WIDTH, HEIGHT)
        src = wintypes.POINT(0, 0)
        position = wintypes.POINT(self._window.winfo_rootx(),
                                  self._window.winfo_rooty())
        user32.UpdateLayeredWindow(
            self._hwnd, self._screen_dc, ctypes.byref(position),
            ctypes.byref(size), self._mem_dc, ctypes.byref(src), 0,
            ctypes.byref(blend), ULW_ALPHA,
        )

    def _tick(self) -> None:
        """Advance one frame. The only thing on the animation hot path."""
        self._anim_job = None
        if self._window is None or not self._window.winfo_exists():
            return
        self._push(self._frames[self._index])
        self._index = (self._index + 1) % len(self._frames)
        self._anim_job = self._window.after(FRAME_INTERVAL_MS, self._tick)

    def _fade(self, target: int, then: Callable[[], None] | None = None) -> None:
        """Ramp the window's constant alpha to `target`."""
        step = max(1, 255 // FADE_STEPS)

        def advance() -> None:
            self._fade_job = None
            if self._window is None or not self._window.winfo_exists():
                if then is not None:
                    then()
                return
            if self._alpha < target:
                self._alpha = min(target, self._alpha + step)
            elif self._alpha > target:
                self._alpha = max(target, self._alpha - step)
            if self._alpha != target:
                self._fade_job = self._window.after(FADE_MS, advance)
            elif then is not None:
                then()

        advance()

    def _destroy_window(self) -> None:
        window, self._window = self._window, None
        for job in (self._anim_job, self._fade_job):
            if job is not None and window is not None:
                try:
                    window.after_cancel(job)
                except Exception:
                    pass
        self._anim_job = self._fade_job = None

        if self._mem_dc is not None:
            if self._old_bitmap:
                gdi32.SelectObject(self._mem_dc, self._old_bitmap)
            if self._bitmap:
                gdi32.DeleteObject(self._bitmap)
            gdi32.DeleteDC(self._mem_dc)
        if self._screen_dc is not None:
            user32.ReleaseDC(0, self._screen_dc)
        self._mem_dc = self._screen_dc = self._bitmap = self._old_bitmap = None
        self._bits = None
        # Release the frame references; the bank keeps its own copy.
        self._frames = []
        self._card = None

        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
        if self._on_dismiss is not None:
            self._on_dismiss()
