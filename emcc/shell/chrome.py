"""Native window chrome: caption stripping, dragging, minimise, maximise.

Relocated from `app.py`'s "Window chrome" section unchanged. `App` keeps
`_strip_caption`, `_drag_start`, `_drag_move`, `_minimise` and
`_toggle_maximise` as one-line methods, because `TitleBar` is handed them as
bound callbacks.

Note `strip_caption` here removes the *OS* title bar and has nothing to do
with `App._caption`, which is the sub-header's device-count label. The names
are close enough that a careless search merges them; they live in different
modules for that reason.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App

# Deliberately "emcc.app" and not `__name__`: these lines logged under
# that name before the extraction, and a pure move should not change what
# appears in logs/emcc.log. It also matches the codebase convention of
# explicit domain names (emcc.device, emcc.ui, emcc.integration).
logger = logging.getLogger("emcc.app")


def strip_caption(app: "App") -> None:
    if sys.platform != "win32":
        app.overrideredirect(True)
        return
    try:
        import ctypes

        app.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(app.winfo_id()) or app.winfo_id()

        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000
        SWP_NOMOVE, SWP_NOSIZE, SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0020

        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_CAPTION)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
    except Exception:
        # Cosmetic only -- fall back to the native caption rather than
        # leaving the window undraggable.
        logger.debug("could not strip the native caption", exc_info=True)


def drag_start(app: "App", event) -> None:
    app._drag_origin = (event.x_root - app.winfo_x(),
                        event.y_root - app.winfo_y())


def drag_move(app: "App", event) -> None:
    if app._maximised:
        return
    dx, dy = app._drag_origin
    app.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")


def minimise(app: "App") -> None:
    app.iconify()


def toggle_maximise(app: "App") -> None:
    # "zoomed" respects the work area, so the taskbar stays visible.
    app._maximised = not app._maximised
    app.state("zoomed" if app._maximised else "normal")
