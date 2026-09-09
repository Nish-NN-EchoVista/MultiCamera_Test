"""The "Add Device" row.

    flex items-center justify-center gap-3 rounded-xl border border-dashed
    border-[#1e2230] hover:border-blue-600/30 hover:bg-blue-600/[0.03]
    text-[#2e3340] hover:text-blue-500/70 py-4 text-[13px] font-medium mt-1
      span: w-6 h-6 rounded-full border border-dashed border-[#2a2e3c]
            hover:border-blue-600/40  -> "+"

Both borders are dashed, which CTkFrame cannot do, so the whole row is drawn on
a tk.Canvas. That also makes the hover transition a matter of reconfiguring
four item colours rather than rebuilding widgets.

Canvas coordinates are device pixels, so every dimension is multiplied by the
CustomTkinter widget scaling factor by hand.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

import customtkinter as ctk

from .. import fonts, theme
from .canvas_util import DASH, circle_points, rounded_rect_points, scaling
from .interactive import Interactive

PAD_Y = 16      # py-4
CIRCLE = 24     # w-6 h-6
GAP = 12        # gap-3
HEIGHT = PAD_Y * 2 + CIRCLE


class AddDeviceButton(ctk.CTkFrame):
    def __init__(self, master, on_click: Callable[[], None]):
        super().__init__(master, fg_color="transparent", height=HEIGHT)
        self.pack_propagate(False)

        self._hovered = False
        self._label_font = fonts.sans(13, 500)
        self._plus_font = fonts.sans(16, 400)  # text-base

        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                 background=theme.APP_BG)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", lambda _e: self._render())

        Interactive(self, on_hover=self._set_hover, on_click=on_click)

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._render()

    def _render(self) -> None:
        c = self._canvas
        c.delete("all")

        s = scaling(self)
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1:
            return

        if self._hovered:
            fill = theme.ADD_BG_HOVER
            border = theme.ADD_BORDER_HOVER
            text = theme.ADD_TEXT_HOVER
            circle = theme.ADD_CIRCLE_HOVER
        else:
            fill = theme.APP_BG
            border = theme.DASHED_BORDER
            text = theme.TEXT_ADD
            circle = theme.DASHED_CIRCLE

        c.configure(background=theme.APP_BG)

        # Canvas dash lengths are device pixels, so scale them too -- otherwise
        # the dashes stay 3px wide while everything around them grows.
        dash = tuple(max(1, round(d * s)) for d in DASH)
        stroke = max(1, round(s))

        # -- outer rounded rectangle: solid fill + dashed outline --
        inset = max(1.0, s / 2)
        outline = rounded_rect_points(
            inset, inset, w - inset, h - inset, theme.CARD_RADIUS * s
        )
        c.create_polygon(outline, fill=fill, outline="")
        c.create_line(outline, fill=border, dash=dash, width=stroke)

        # -- centred content group: dashed circle + "+" then the label --
        label_font = self._label_font.create_scaled_tuple(s)
        plus_font = self._plus_font.create_scaled_tuple(s)
        label_w = tkfont.Font(root=self, font=label_font).measure("Add Device")

        group_w = CIRCLE * s + GAP * s + label_w
        x = (w - group_w) / 2
        cy = h / 2

        r = CIRCLE * s / 2
        c.create_line(circle_points(x + r, cy, r - stroke / 2),
                      fill=circle, dash=dash, width=stroke)
        c.create_text(x + r, cy, text="+", fill=text, font=plus_font)

        c.create_text(x + CIRCLE * s + GAP * s, cy, text="Add Device",
                      fill=text, font=label_font, anchor="w")
