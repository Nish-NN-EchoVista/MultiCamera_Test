"""The Auto pill toggle.

Source markup (src/App.tsx):

    <span class="relative inline-flex w-7 h-4 rounded-full ... {on ? bg-emerald-500 : bg-[#2a2e3a]}">
      <span class="absolute top-0.5 w-3 h-3 bg-white rounded-full shadow
           transition-all duration-200 {on ? left-3.5 : left-0.5}" />
    </span>

So: a 28x16 fully-rounded track, with a 12x12 white knob inset 2px from the
top that slides between x=2 and x=14 over 200ms. The `shadow` on the knob is
dropped -- Tk has no box-shadow, and at 12px on a light knob it is invisible.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..anim import Tween

TRACK_W, TRACK_H = 28, 16
KNOB = 12
INSET = 2
OFF_X = INSET                       # left-0.5
ON_X = TRACK_W - KNOB - INSET       # left-3.5
DURATION_MS = 200                   # duration-200


class AutoToggle(ctk.CTkFrame):
    """Track + sliding knob. Purely presentational; the parent owns the state."""

    def __init__(self, master, on: bool = False, **kwargs):
        super().__init__(
            master,
            width=TRACK_W,
            height=TRACK_H,
            corner_radius=TRACK_H // 2,
            fg_color=theme.TOGGLE_TRACK_ON if on else theme.TOGGLE_TRACK_OFF,
            border_width=0,
            **kwargs,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._on = on
        self._tween = Tween(self)

        self._knob = ctk.CTkFrame(
            self,
            width=KNOB,
            height=KNOB,
            corner_radius=KNOB // 2,
            fg_color=theme.TOGGLE_KNOB,
            border_width=0,
        )
        self._knob.place(x=ON_X if on else OFF_X, y=INSET)

    def set(self, on: bool, animate: bool = True) -> None:
        if on == self._on:
            return
        self._on = on
        self.configure(fg_color=theme.TOGGLE_TRACK_ON if on else theme.TOGGLE_TRACK_OFF)

        start = OFF_X if on else ON_X
        end = ON_X if on else OFF_X
        if not animate:
            self._knob.place(x=end, y=INSET)
            return

        def apply(p: float) -> None:
            if self._knob.winfo_exists():
                self._knob.place(x=start + (end - start) * p, y=INSET)

        self._tween.run(DURATION_MS, apply)

    def destroy(self) -> None:
        self._tween.cancel()
        super().destroy()
