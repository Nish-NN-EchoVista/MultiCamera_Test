"""Modal dialogs, styled to match the design.

The Figma design has no toast or notification surface, so a modal is the only
channel available for telling the operator something went wrong. That makes
restraint important: with 25 devices on one switch, a network blip that popped
one modal per device would bury the screen in dialogs exactly when the operator
needs to see the cards. So:

* nothing pops on an unexpected disconnect -- the card already turns red with a
  caption, which is better signalling than a modal
* reconnect exhaustion pops once, and `ErrorReporter` merges several
  failures inside a short window into a single dialog
* dialogs are transient, grab-set and centred on the parent, so they cannot be
  lost behind the main window
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

import customtkinter as ctk

from .. import fonts, theme
from .interactive import Interactive

logger = logging.getLogger("emcc.ui")


def _centre(window: ctk.CTkToplevel, parent, width: int, height: int) -> None:
    """Place `window` centred on the parent, clamped to the screen."""
    window.update_idletasks()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
    except Exception:
        px = py = 0
        pw, ph = window.winfo_screenwidth(), window.winfo_screenheight()
    x = max(0, px + (pw - width) // 2)
    y = max(0, py + (ph - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")


class _BaseDialog(ctk.CTkToplevel):
    """Shared chrome: dark ground, title, body, button row."""

    WIDTH = 460

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.configure(fg_color=theme.APP_BG)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())

        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(expand=True, fill="both", padx=26, pady=22)

        self._buttons = ctk.CTkFrame(self, fg_color="transparent")
        self._buttons.pack(fill="x", padx=26, pady=(0, 20))

    def _heading(self, text: str, colour: str = theme.TEXT_LABEL) -> None:
        ctk.CTkLabel(self._body, text=fonts.tracked(text.upper(), 0.12),
                     font=fonts.sans(9.5, 600), text_color=colour,
                     height=13, anchor="w").pack(anchor="w")

    def _title_line(self, text: str) -> None:
        ctk.CTkLabel(self._body, text=text, font=fonts.sans(15, 600),
                     text_color=theme.TEXT_PRIMARY, anchor="w",
                     height=20).pack(anchor="w", pady=(8, 0))

    def _detail(self, text: str, colour: str = theme.TEXT_FAINT) -> None:
        ctk.CTkLabel(self._body, text=text, font=fonts.sans(11),
                     text_color=colour, justify="left",
                     anchor="w").pack(anchor="w", pady=(6, 0), fill="x")

    def _button(self, parent, label: str, on_click: Callable[[], None],
                *, danger: bool = False, primary: bool = False) -> ctk.CTkFrame:
        """A pressable frame styled from the design's existing button tokens."""
        if danger:
            fill, fill_hover = theme.RED_700, theme.RED_600
            border, text = theme.over(theme.RED_700, theme.APP_BG, 0.40), theme.WHITE
        elif primary:
            fill, fill_hover = theme.BLUE_600, theme.BLUE_500
            border, text = theme.over(theme.BLUE_700, theme.APP_BG, 0.40), theme.WHITE
        else:
            fill = fill_hover = theme.AUTO_OFF_BG
            border, text = theme.BORDER_INPUT, theme.TEXT_SECONDARY

        frame = ctk.CTkFrame(parent, corner_radius=theme.CTRL_RADIUS,
                             border_width=1, fg_color=fill, border_color=border)
        label_widget = ctk.CTkLabel(frame, text=label, font=fonts.sans(12.5, 600),
                                    text_color=text, height=15)
        label_widget.pack(padx=18, pady=8)

        def on_hover(hovered: bool) -> None:
            frame.configure(fg_color=fill_hover if hovered else fill)
            if not (danger or primary):
                label_widget.configure(
                    text_color=theme.TEXT_PRIMARY if hovered else theme.TEXT_SECONDARY
                )

        Interactive(frame, on_hover=on_hover, on_click=on_click)
        return frame

    def _finish(self, height: int, parent) -> None:
        _centre(self, parent, self.WIDTH, height)
        self.after(30, self._grab)
        self.bind("<Escape>", lambda _e: self._on_escape())

    def _grab(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:
            pass   # window may already be gone

    def _on_escape(self) -> None:
        self.destroy()


class ErrorDialog(_BaseDialog):
    """Reports one or more device faults.

    Every entry names the device and its IP, as the spec requires, and keeps
    the text short -- the technical detail is in the log, not here.
    """

    def __init__(self, parent, entries: Sequence[tuple[str, str, str]]):
        title = "Connection problem" if len(entries) == 1 else "Connection problems"
        super().__init__(parent, title)

        self._heading(title, theme.HEADER_ALERT_TEXT)

        if len(entries) == 1:
            name, ip, message = entries[0]
            self._title_line(f"{name} — {ip}" if ip else name)
            self._detail(message)
            height = 210
        else:
            self._title_line(f"{len(entries)} devices need attention")
            for name, ip, message in entries[:8]:
                self._detail(f"{name} — {ip or 'no IP'}\n    {message}")
            if len(entries) > 8:
                self._detail(f"…and {len(entries) - 8} more (see the log)",
                             theme.TEXT_GHOST)
            height = min(560, 190 + 46 * min(len(entries), 8))

        self._detail("Full details have been written to logs/emcc.log.",
                     theme.TEXT_GHOST)

        row = ctk.CTkFrame(self._buttons, fg_color="transparent")
        row.pack(side="right")
        self._button(row, "Dismiss", self.destroy, primary=True).pack(side="right")

        self._finish(height, parent)


class ErrorReporter:
    """Collects device faults and shows at most one dialog per burst.

    Deliberately an instance owned by the App rather than class-level state on
    `ErrorDialog`. A shared timer handle outlives the window it was scheduled
    against, so a fault queued just before shutdown would later be cancelled or
    flushed against a destroyed root -- which showed up as cross-test
    interference and would, in the app, mean a dialog for a window that no
    longer exists.
    """

    def __init__(self, parent, window_ms: int = 700):
        self._parent = parent
        self._window_ms = window_ms
        self._pending: list[tuple[str, str, str]] = []
        self._timer: object | None = None
        self._closed = False

    def report(self, name: str, ip: str, message: str) -> None:
        """Queue a fault. Faults arriving together produce one dialog.

        Several devices behind the same switch fail at once, so without this a
        network blip would open a modal per device.
        """
        if self._closed:
            return
        self._pending.append((name, ip, message))
        self._cancel()
        self._timer = self._parent.after(self._window_ms, self._flush)

    def show_now(self, name: str, ip: str, message: str) -> None:
        """Show a single fault immediately, for a direct operator action."""
        if self._closed:
            return
        try:
            ErrorDialog(self._parent, [(name, ip, message)])
        except Exception:
            logger.exception("error dialog failed")

    def _flush(self) -> None:
        self._timer = None
        entries, self._pending = self._pending, []
        if not entries or self._closed:
            return
        try:
            ErrorDialog(self._parent, entries)
        except Exception:
            logger.exception("error dialog failed")

    def _cancel(self) -> None:
        if self._timer is not None:
            try:
                self._parent.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def close(self) -> None:
        """Drop anything queued and refuse further reports. Idempotent."""
        self._closed = True
        self._cancel()
        self._pending = []


class ConfirmDialog(_BaseDialog):
    """Yes/no confirmation. `on_confirm` runs only on explicit confirmation."""

    def __init__(self, parent, title: str, detail: str,
                 on_confirm: Callable[[], None],
                 confirm_label: str = "Remove", danger: bool = True):
        super().__init__(parent, title)
        self._on_confirm = on_confirm

        self._heading("Confirm")
        self._title_line(title)
        if detail:
            self._detail(detail)

        row = ctk.CTkFrame(self._buttons, fg_color="transparent")
        row.pack(side="right")
        self._button(row, confirm_label, self._confirm,
                     danger=danger).pack(side="right")
        self._button(row, "Cancel", self.destroy).pack(side="right", padx=(0, 10))

        self._finish(200, parent)

    def _confirm(self) -> None:
        self.destroy()
        self._on_confirm()
