"""Settings: the device log level, and where device logs are kept.

The third view behind the List / Dashboard / Settings toggle. It renders no
devices, but it is still a full `DeviceView` -- the host routes every view the
same way, and a view that opted out of part of the contract would be the one
the host's containment had to special-case. So it keeps the device set it is
given (to report how many devices are being logged) and answers every call.

The level applies to every device whose Log button is on, from the next line
each device sends. It is saved in config.json; which devices are logged is
not -- every device starts with logging off.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Sequence

import customtkinter as ctk

from .. import fonts, theme
from ..backend.capture import LogLevel
from ..backend.device_manager import DeviceState
from ..widgets.interactive import Interactive
from .base import StandaloneCtx

logger = logging.getLogger("emcc.ui")

#: Width of the settings panel. Wide enough for the longest description on
#: one line at the default window width, narrow enough to read as a form.
PANEL_W = 720


class _LevelOption(ctk.CTkFrame):
    """One selectable level: a radio mark, its name and what it keeps.

    Drawn like the view toggle's segments -- the selected one takes the
    toggle's blue -- rather than with `CTkRadioButton`, whose hover and focus
    treatment matches nothing else in the app.
    """

    def __init__(self, master, level: LogLevel,
                 on_select: Callable[[LogLevel], None]) -> None:
        super().__init__(master, corner_radius=theme.CTRL_RADIUS,
                         border_width=1)
        self.level = level
        self._selected = False
        self._hovered = False

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)

        self._mark = ctk.CTkFrame(row, width=14, height=14, corner_radius=7,
                                  border_width=1)
        self._mark.pack(side="left", anchor="n", pady=(2, 0))
        self._mark.pack_propagate(False)
        self._dot = ctk.CTkFrame(self._mark, width=6, height=6,
                                 corner_radius=3, border_width=0)

        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self._title = ctk.CTkLabel(text, text=level.label,
                                   font=fonts.sans(12.5, 600), anchor="w",
                                   height=16)
        self._title.pack(fill="x")
        self._detail = ctk.CTkLabel(text, text=level.description,
                                    font=fonts.sans(11), anchor="w",
                                    justify="left", height=15,
                                    wraplength=PANEL_W - 120)
        self._detail.pack(fill="x", pady=(2, 0))

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=lambda: on_select(level))
        self._paint()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        # Geometry, so here and not in `_paint` (see buttons.py, PAINT vs
        # LAYOUT): hover must never place or forget a widget.
        if selected:
            self._dot.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self._dot.place_forget()
        self._paint()

    def _paint(self) -> None:
        if self._selected:
            fill, border = theme.SEG_ON_BG, theme.SEG_ON_BORDER
            title, detail, mark = theme.SEG_ON_TEXT, theme.TEXT_SECONDARY, theme.SEG_ON_ICON
        else:
            fill = theme.CARD
            border = theme.BORDER_INPUT_HOVER if self._hovered else theme.BORDER_INPUT
            title = theme.TEXT_PRIMARY if self._hovered else theme.TEXT_TITLE
            detail, mark = theme.TEXT_MUTED, theme.TEXT_DIM
        self.configure(fg_color=fill, border_color=border)
        self._title.configure(text_color=title)
        self._detail.configure(text_color=detail)
        self._mark.configure(fg_color=fill, border_color=mark)
        self._dot.configure(fg_color=mark)


class SettingsView:
    """Device log settings. A controller, like `ListView`; `widget` is its frame."""

    name = "settings"

    def __init__(self, master, ctx=None, *,
                 level: LogLevel,
                 log_root: Path,
                 retention_days: int,
                 max_gb: float,
                 on_level: Callable[[LogLevel], None],
                 devices: Sequence[DeviceState] | None = None) -> None:
        self._ctx = ctx if ctx is not None else StandaloneCtx(master)
        self._on_level = on_level
        self._level = level
        self._log_root = Path(log_root)
        self._devices: list[DeviceState] = list(devices or [])

        self._frame = ctk.CTkFrame(master, fg_color="transparent")

        panel = ctk.CTkFrame(self._frame, fg_color=theme.CARD,
                             corner_radius=theme.CARD_RADIUS, border_width=1,
                             border_color=theme.BORDER_CARD, width=PANEL_W)
        panel.pack(anchor="n", pady=(8, 0))
        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)
        # A strut: the panel takes its width from its contents, so this is
        # what holds it at PANEL_W. Every text below wraps inside it, so none
        # can widen the panel -- a long log path once stretched it to the
        # full window.
        ctk.CTkFrame(body, width=PANEL_W - 48, height=1,
                     fg_color="transparent").pack(anchor="w")

        ctk.CTkLabel(body, text=fonts.tracked("DEVICE LOGS", 0.14),
                     font=fonts.sans(9.5, 600), text_color=theme.TEXT_LABEL,
                     anchor="w", height=13).pack(fill="x")
        ctk.CTkLabel(body, text="Log level", font=fonts.sans(14, 600),
                     text_color=theme.TEXT_TITLE, anchor="w",
                     height=18).pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(body,
                     text="Applies to every device whose Log button is on, "
                          "from the next line it sends.",
                     font=fonts.sans(11), text_color=theme.TEXT_MUTED,
                     anchor="w", height=15).pack(fill="x", pady=(2, 12))

        self._options: dict[LogLevel, _LevelOption] = {}
        for index, option_level in enumerate(LogLevel):
            option = _LevelOption(body, option_level, self._select)
            option.configure(width=PANEL_W - 48)
            option.pack(fill="x", pady=(0 if index == 0 else 8, 0))
            self._options[option_level] = option

        ctk.CTkFrame(body, height=1, fg_color=theme.SEPARATOR,
                     corner_radius=0).pack(fill="x", pady=(20, 14))

        folder_row = ctk.CTkFrame(body, fg_color="transparent")
        folder_row.pack(fill="x")
        ctk.CTkLabel(folder_row, text="Saved in", font=fonts.sans(11),
                     text_color=theme.TEXT_MUTED, height=15).pack(side="left")
        self._open = ctk.CTkLabel(folder_row, text="Open folder",
                                  font=fonts.sans(11, 600),
                                  text_color=theme.CLEAN_OFF_TEXT, height=15)
        self._open.pack(side="right")
        Interactive(self._open, on_hover=self._set_open_hover,
                    on_click=self._open_folder)
        # On its own line and wrapped: a path has no length limit.
        ctk.CTkLabel(body, text=str(self._log_root.resolve()),
                     font=fonts.mono(11), text_color=theme.TEXT_MONO,
                     anchor="w", justify="left",
                     wraplength=PANEL_W - 48).pack(fill="x", pady=(4, 0))

        ctk.CTkLabel(body,
                     text=(f"One folder per device, one file per hour. Kept for "
                           f"{retention_days} days, and oldest first once they "
                           f"pass {max_gb:g} GB."),
                     font=fonts.sans(11), text_color=theme.TEXT_MUTED,
                     anchor="w", height=15).pack(fill="x", pady=(6, 0))
        self._count = ctk.CTkLabel(body, text="", font=fonts.sans(11),
                                   text_color=theme.TEXT_MUTED, anchor="w",
                                   height=15)
        self._count.pack(fill="x", pady=(2, 0))

        self._sync()

    # -- host surface ----------------------------------------------------

    @property
    def widget(self):
        return self._frame

    @property
    def padding(self) -> dict:
        return {"padx": theme.PAGE_PAD_X, "pady": theme.PAGE_PAD_Y}

    @property
    def heading(self) -> str:
        return theme.SETTINGS_SUBHEAD

    def caption(self, total: int) -> str:
        logging_now = sum(1 for d in self._devices if d.logging_enabled)
        return (f"{logging_now} of {total} device{'' if total == 1 else 's'} "
                f"logging · {self._level.label}")

    def set_devices(self, devices: Sequence[DeviceState]) -> None:
        self._devices = list(devices)
        self._sync()

    def device_fingerprint(self) -> tuple[tuple[str, str], ...]:
        return tuple((d.id, d.name) for d in self._devices)

    def show(self) -> None:
        self._sync()

    def hide(self) -> None:
        pass

    def render_device(self, device_id: str) -> bool:
        """Nothing per device to repaint; the logging count may have moved."""
        self._sync()
        return any(d.id == device_id for d in self._devices)

    def add_device(self, device: DeviceState) -> None:
        self._devices.append(device)

    def remove_device(self, device_id: str) -> None:
        self._devices = [d for d in self._devices if d.id != device_id]

    def on_device_count_changed(self, total: int) -> None:
        self._sync()

    def shutdown(self) -> None:
        pass

    # -- level -----------------------------------------------------------

    def set_level(self, level: LogLevel) -> None:
        """Show `level` as selected without reporting it back."""
        self._level = level
        self._sync()

    def _select(self, level: LogLevel) -> None:
        if level is self._level:
            return
        self._level = level
        self._sync()
        self._on_level(level)

    def _sync(self) -> None:
        for level, option in self._options.items():
            option.set_selected(level is self._level)
        logging_now = sum(1 for d in self._devices if d.logging_enabled)
        self._count.configure(
            text=(f"{logging_now} device{'' if logging_now == 1 else 's'} "
                  f"logging now. Press a device's Log button to start or stop."))

    # -- folder ----------------------------------------------------------

    def _set_open_hover(self, hovered: bool) -> None:
        self._open.configure(text_color=theme.TEXT_PRIMARY if hovered
                             else theme.CLEAN_OFF_TEXT)

    def _open_folder(self) -> None:
        """Open the device-log folder in Explorer, creating it if need be."""
        try:
            self._log_root.mkdir(parents=True, exist_ok=True)
            os.startfile(self._log_root.resolve())  # noqa: S606 - Windows app
        except OSError:
            logger.warning("could not open the device log folder %s",
                           self._log_root, exc_info=True)
