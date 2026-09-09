"""Capture a Tk window's own pixels, not the screen region it occupies.

`ImageGrab.grab(bbox=...)` reads the desktop, so anything overlapping the app
ends up in the shot -- a lock screen, a browser, another window. `PrintWindow`
with `PW_RENDERFULLCONTENT` asks the window to render itself into a bitmap
instead, which is unaffected by what is on top of it and does not require the
window to be focused or even visible.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from PIL import Image, ImageStat

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
BI_RGB = 0


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


def _toplevel_hwnd(widget) -> int:
    """The OS window behind a Tk widget.

    Tk's `winfo_id()` on a toplevel is its client-area HWND, which is what we
    want: it excludes the title bar the OS draws, and its origin matches
    `winfo_rootx/y`.
    """
    return int(widget.winfo_toplevel().winfo_id())


class BlankCaptureError(RuntimeError):
    """A capture succeeded according to the API and contained nothing.

    `PrintWindow` returns non-zero on success and can still leave the bitmap
    entirely one colour -- typically black, before the window has painted, or
    for a window whose content Windows declines to render into a DC. Checking
    only the return code therefore reports a silent wrong answer: two of the
    twelve visual reference images were pure black while the tool reported
    success, and one of them had never contained anything at all.

    So a flat single-colour frame is treated as a *failure*, not a capture.
    """


#: Attempts before giving up. A first-paint race usually clears on the second.
CAPTURE_ATTEMPTS = 4
#: Pause between attempts, giving Windows a chance to paint.
CAPTURE_SETTLE_S = 0.12


def is_blank(image: Image.Image) -> bool:
    """True if every pixel is the same colour, in which case nothing rendered.

    Deliberately strict: any real capture of this UI contains at least a
    border, a glyph or an antialiased edge. A genuinely uniform frame is not
    something this tool is ever asked for, so treating it as failure cannot
    reject a legitimate result.
    """
    extrema = image.convert("RGB").getextrema()
    return all(low == high for low, high in extrema)


def capture(widget, *, attempts: int = CAPTURE_ATTEMPTS,
            allow_blank: bool = False) -> Image.Image:
    """Return the widget's toplevel as an RGB image.

    Retries a flat frame rather than returning it, and raises
    `BlankCaptureError` if it never fills in. Pass `allow_blank=True` only if
    a uniform frame is a legitimate expected result.
    """
    top = widget.winfo_toplevel()
    for attempt in range(1, attempts + 1):
        image = _capture_once(top)
        if allow_blank or not is_blank(image):
            return image
        if attempt < attempts:
            # Let Windows paint, and let Tk flush anything pending, before
            # asking again.
            try:
                top.update_idletasks()
                top.update()
            except Exception:
                pass
            time.sleep(CAPTURE_SETTLE_S)
    raise BlankCaptureError(
        f"capture of {top} was a flat "
        f"{image.convert('RGB').getpixel((0, 0))} after {attempts} attempts; "
        "PrintWindow reported success but rendered nothing"
    )


def capture_stable(widget, *, attempts: int = 14, settle_s: float = 0.06,
                   pumps: int = 4) -> Image.Image:
    """Capture only once the frame has stopped changing.

    Stronger than `capture`, and necessary for anything freshly mapped.
    `is_blank` catches a frame that rendered *nothing*; it cannot catch one
    that rendered *partially*, which is the more common failure and the one
    that produced a usable-looking reference image with a wrong region in it.

    Measured on the error dialog: captured after `update_idletasks()` and
    `update()` but with no wall-clock delay, the Dismiss button's label came
    back as an opaque dark block filling 8844 of 8844 sampled pixels. With
    60ms it rendered correctly. **Pumping Tk is not sufficient** -- Tk's queue
    drains long before Windows has composited the child widget, so the only
    honest test is whether two successive frames agree.

    Not usable on a window containing an *endless* animation: `emcc.anim.Pulse`
    ticks at 30fps, so two frames never agree while it runs -- call
    `Pulse.freeze()` first, which is what `tools/visual_pass.py` does. A
    *transient* animation is different and is simply waited out: the toggle
    knob's 200ms slide settles on its own, which is why the attempt budget is
    generous rather than the animation being frozen.
    """
    previous = None
    for _ in range(attempts):
        # Several cycles, not one. `emcc.anim.Tween` is driven by chained
        # `after` callbacks rather than wall clock, so a single `update()` per
        # attempt advances the 200ms toggle-knob slide by only a frame or two
        # and it outlives a stingy budget. Measured: the knob took 8 rounds of
        # one-pump sampling to settle, and the moving region was a 33x27 box
        # travelling horizontally -- the knob, not instability in the app.
        for _ in range(pumps):
            try:
                widget.update_idletasks()
                widget.update()
            except Exception:
                break
        time.sleep(settle_s)
        current = _capture_once(widget.winfo_toplevel())
        if previous is not None and current.tobytes() == previous.tobytes():
            if is_blank(current):
                raise BlankCaptureError(
                    f"capture of {widget} settled but is flat "
                    f"{current.convert('RGB').getpixel((0, 0))}"
                )
            return current
        previous = current
    raise BlankCaptureError(
        f"capture of {widget} never settled over {attempts} attempts -- "
        "the window is still repainting, or something on it is animating"
    )


def _capture_once(widget) -> Image.Image:
    """One PrintWindow round trip, with no validation."""
    hwnd = _toplevel_hwnd(widget)
    top = widget.winfo_toplevel()
    width, height = top.winfo_width(), top.winfo_height()

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height          # negative == top-down rows
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        window_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
    )
    gdi32.SelectObject(memory_dc, bitmap)

    try:
        if not user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow failed")
        buffer = ctypes.string_at(bits, width * height * 4)
        # CreateDIBSection gives BGRA; drop alpha, which PrintWindow leaves at 0.
        return Image.frombuffer(
            "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
        ).convert("RGB")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def capture_widget(widget, pad: int = 0, *,
                   allow_blank: bool = False) -> Image.Image:
    """Capture just one widget, cropped out of its toplevel's own pixels.

    `pad` expands the crop *within* the toplevel. It cannot reach outside the
    window -- `PrintWindow` renders the window and nothing behind it -- so the
    box is clamped to the window's bounds.
    """
    top = widget.winfo_toplevel()
    image = capture(top, allow_blank=allow_blank)
    x = widget.winfo_rootx() - top.winfo_rootx()
    y = widget.winfo_rooty() - top.winfo_rooty()
    box = (
        max(0, x - pad), max(0, y - pad),
        min(image.width, x + widget.winfo_width() + pad),
        min(image.height, y + widget.winfo_height() + pad),
    )
    crop = image.crop(box)
    if not allow_blank and is_blank(crop):
        raise BlankCaptureError(
            f"crop of {widget} is flat; the window rendered but this region "
            "did not -- check the widget is mapped and has non-zero size"
        )
    return crop
