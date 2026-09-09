"""The List / Dashboard segmented control.

CustomTkinter ships `CTkSegmentedButton`, but it is not used anywhere in this
app and its hover, radius and icon handling do not match the rest of the
sub-header. This follows the same shape as every other composite control here:
a frame of frames driven by `widgets.interactive.Interactive`, with the
paint/layout split documented at the top of `buttons.py` -- `_paint()` for
colours only, `render()` for colours plus geometry, and hover calling
`_paint()` alone so a pointer move never touches the layout.

The two glyphs are drawn on a local canvas rather than added to `icons.py`.
They are 12px primitives (three rules; four squares) with no shared use, and
keeping them here means the control is one self-contained file.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .. import fonts, theme
from .interactive import Interactive

#: Identifiers for the two views. `App` stores one of these.
LIST = "list"
DASHBOARD = "dashboard"


class _Glyph(ctk.CTkFrame):
    """A 12x12 canvas holding either the list or the grid mark."""

    SIZE = 12

    def __init__(self, master, kind: str):
        super().__init__(master, fg_color="transparent",
                         width=self.SIZE, height=self.SIZE)
        self.pack_propagate(False)
        self._kind = kind
        self._canvas = ctk.CTkCanvas(self, width=self.SIZE, height=self.SIZE,
                                     highlightthickness=0, borderwidth=0)
        self._canvas.pack()
        self._colour: str | None = None

    def paint(self, colour: str, bg: str) -> None:
        """Redraw in `colour` on `bg`.

        A bare Tk canvas has no notion of a transparent parent, so the panel
        colour behind it has to be passed in and repainted whenever the
        segment's own fill changes.
        """
        self._canvas.configure(bg=bg)
        if colour == self._colour:
            return
        self._colour = colour
        self._canvas.delete("all")
        if self._kind == LIST:
            # Three rules, 2px tall, at y = 1, 5, 9.
            for y in (1, 5, 9):
                self._canvas.create_rectangle(0, y, self.SIZE, y + 1,
                                              fill=colour, outline="")
        else:
            # Four 5x5 squares with a 2px gutter.
            for x in (0, 7):
                for y in (0, 7):
                    self._canvas.create_rectangle(x, y, x + 4, y + 4,
                                                  fill=colour, outline="")


class _Segment(ctk.CTkFrame):
    """One half of the control: glyph plus label, selected or not."""

    def __init__(self, master, view: str, label: str,
                 on_click: Callable[[], None]):
        super().__init__(master, corner_radius=theme.SEG_RADIUS, border_width=1,
                         fg_color="transparent", border_color=theme.SEG_BG)
        self.view = view
        self._selected = False
        self._hovered = False

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=10, pady=5)

        self._glyph = _Glyph(row, view)
        self._glyph.pack(side="left", padx=(0, 7))

        self._label = ctk.CTkLabel(row, text=label, font=fonts.sans(11.5, 600),
                                   height=14)
        self._label.pack(side="left")

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._paint()

    def _paint(self) -> None:
        if self._selected:
            fill, border = theme.SEG_ON_BG, theme.SEG_ON_BORDER
            text, icon = theme.SEG_ON_TEXT, theme.SEG_ON_ICON
        else:
            # Unselected segments sit on the container's own fill, so they
            # take its colour rather than painting a second panel over it.
            fill, border = theme.SEG_BG, theme.SEG_BG
            text = (theme.SEG_OFF_TEXT_HOVER if self._hovered
                    else theme.SEG_OFF_TEXT)
            icon = (theme.SEG_OFF_ICON_HOVER if self._hovered
                    else theme.SEG_OFF_ICON)
        self.configure(fg_color=fill, border_color=border)
        self._label.configure(text_color=text)
        self._glyph.paint(icon, fill)

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()


class ViewToggle(ctk.CTkFrame):
    """Two-segment selector for the List and Dashboard views.

    `on_change(view)` fires only on a genuine change, so re-clicking the
    active segment is a no-op rather than a view rebuild.
    """

    def __init__(self, master, *, view: str = LIST,
                 on_change: Callable[[str], None]):
        super().__init__(master, corner_radius=theme.SEG_RADIUS, border_width=1,
                         fg_color=theme.SEG_BG, border_color=theme.SEG_BORDER)
        self._view = view
        self._on_change = on_change

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=3, pady=3)

        self._segments = {
            LIST: _Segment(row, LIST, "List", lambda: self._select(LIST)),
            DASHBOARD: _Segment(row, DASHBOARD, "Dashboard",
                                lambda: self._select(DASHBOARD)),
        }
        self._segments[LIST].pack(side="left")
        self._segments[DASHBOARD].pack(side="left", padx=(3, 0))

        self._sync()

    @property
    def view(self) -> str:
        return self._view

    def _select(self, view: str) -> None:
        if view == self._view:
            return
        self.set_view(view)
        self._on_change(view)

    def set_view(self, view: str) -> None:
        """Move the selection without firing `on_change`.

        Lets `App` drive the control from its own state -- restoring a saved
        view, say -- without the control calling back into the switch that
        set it.
        """
        if view not in self._segments:
            raise ValueError(f"unknown view {view!r}")
        self._view = view
        self._sync()

    def _sync(self) -> None:
        for view, segment in self._segments.items():
            segment.set_selected(view == self._view)

    def render(self) -> None:
        for segment in self._segments.values():
            segment.render()
