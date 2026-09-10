"""One device row.

Layout, from src/App.tsx:

    flex items-center gap-0 rounded-xl border bg-[#181c25] px-5 py-0 min-h-[142px]
      Section "Device Name"  w-[188px]   input + remove control
      VSep
      Section "IP Address"   w-[176px]   input, inner px-4
      VSep
      Section "Connection"   w-[162px]   ConnectionButton, inner px-4
      VSep
      Section "Actions"      flex-1      Clean + Auto, inner px-5, gap-2
      VSep
      Section "Temperature"  w-[148px]   TempButton, inner pl-4

Faithfulness notes:

* The section *labels* sit flush to each column's left edge while the controls
  below them are inset by the inner px-4 / px-5 / pl-4. That asymmetry is in
  the export, so it is reproduced rather than tidied up.
* `shadow-[0_2px_16px_rgba(0,0,0,0.35)]` on the card is dropped -- Tk has no
  box-shadow. On the near-black background it contributes very little.
* The remove control occupies the slot the decorative pencil had, inside the
  Device Name field at `right-2.5`. The design has no delete affordance and the
  188px column has no spare width, so replacing the (non-functional) pencil
  keeps the card's metrics byte-identical.

The card is a *view*. It renders `DeviceState` and forwards intent to
callbacks; it holds no device state of its own and never decides anything the
backend should decide.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .. import fonts, icons, theme
from ..backend.device_manager import DeviceState
from .buttons import AutoButton, CleanButton, ConnectionButton, TempButton
from .canvas_util import scaling
from .interactive import Interactive

LABEL_TRACKING = 0.14  # tracking-[0.14em]


class _Section(ctk.CTkFrame):
    """flex flex-col gap-2.5 -- a tracked uppercase label above its control.

    DELIBERATE DEVIATION FROM THE EXPORT
    ------------------------------------
    The card is `flex items-center`, which centres every column on the cross
    axis *independently*. Because the columns are different heights -- the
    Connection column carries a caption, Temperature carries one only above
    threshold, the two inputs carry none -- that centring puts each column's
    label at a different vertical offset, so the row of section labels does not
    line up. Reproduced exactly, it reads as a rendering fault.

    The design's evident intent is a tidy row of labelled columns, so the
    label/control group is offset by a *shared* amount instead: the tallest
    column is centred in the card, and every other column aligns its label to
    that same baseline. `set_top` receives that offset from the parent card.
    """

    def __init__(self, master, label: str, width: int | None = None):
        super().__init__(master, fg_color="transparent")
        if width is not None:
            self.configure(width=width)
            self.pack_propagate(False)

        self.group = ctk.CTkFrame(self, fg_color="transparent")
        self._place(0)

        ctk.CTkLabel(
            self.group,
            text=fonts.tracked(label.upper(), LABEL_TRACKING),
            font=fonts.sans(9.5, 600),
            text_color=theme.TEXT_LABEL,
            anchor="w",
            height=13,
        ).pack(fill="x")

        self.body = ctk.CTkFrame(self.group, fg_color="transparent")
        self.body.pack(fill="x", pady=(theme.SECTION_GAP, 0))  # gap-2.5

    def _place(self, y: float) -> None:
        """Position the group `y` *logical* px from the section top.

        Uses place() rather than place_configure(): CustomTkinter applies its
        widget-scaling factor in place() but not in place_configure(), so
        reconfiguring would write a logical value into a device-pixel slot and
        under-offset the group by the scaling factor (2.25x on this display).
        """
        self.group.place(relx=0, y=y, relwidth=1.0, anchor="nw")

    def set_top(self, y: float) -> None:
        self._place(y)


class _VSep(ctk.CTkFrame):
    """w-px self-stretch mx-1 bg-[#1f2330]"""

    def __init__(self, master):
        super().__init__(master, width=1, fg_color=theme.SEPARATOR,
                         corner_radius=0, border_width=0)
        self.pack_propagate(False)


class DeviceCard(ctk.CTkFrame):
    """A row rendering one `DeviceState`.

    `label_offset` is the shared label baseline (see `_Section`). Pass it when
    known and the columns are positioned correctly as they are built; leave it
    None and the caller must call `align_labels()` once layout has run.

    That distinction is a performance one. Measuring then re-`place`ing every
    column moves widgets that have already been drawn, so CustomTkinter redraws
    the whole card a second time -- adding one card issued ~178 rounded-rect
    draws instead of ~60. Since the offset only depends on which columns carry
    a caption, it can be computed once and reused for every later card of the
    same shape.
    """

    def __init__(
        self,
        master,
        device: DeviceState,
        *,
        on_connect: Callable[[str], None],
        on_clean: Callable[[str], None],
        on_auto: Callable[[str], None],
        on_temperature: Callable[[str], None],
        on_remove: Callable[[str], None],
        on_rename: Callable[[str, str], None],
        on_ip_change: Callable[[str, str], None],
        label_offset: float | None = None,
    ):
        super().__init__(
            master,
            fg_color=theme.CARD,
            corner_radius=theme.CARD_RADIUS,
            border_width=1,
            border_color=theme.BORDER_CARD,
            height=theme.CARD_MIN_H,
        )
        self.pack_propagate(False)

        self.device = device
        self._label_offset = label_offset
        self._sections: list[_Section] = []

        self._on_connect = on_connect
        self._on_clean = on_clean
        self._on_auto = on_auto
        self._on_temperature = on_temperature
        self._on_remove = on_remove
        self._on_rename = on_rename
        self._on_ip_change = on_ip_change

        # px-5, and items-center within the 142px min height.
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=theme.CARD_PAD_X)

        self._build_name(row)
        self._sep(row)
        self._build_ip(row)
        self._sep(row)
        self._build_connection(row)
        self._sep(row)
        self._build_actions(row)
        self._sep(row)
        self._build_temperature(row)

        # hover:border-[#272c3a] on the card itself.
        Interactive(self, on_hover=self._set_hover, cursor=False)

    # -- identity ---------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self.device.id

    @property
    def variant(self) -> bool:
        """Layout shape key: whether the Temperature column carries a caption.

        That is the only thing that changes a card's tallest column, so it is
        enough to identify cards whose label offset is the same.
        """
        return self.device.temperature_alert

    # -- rendering --------------------------------------------------------

    def render(self) -> None:
        """Re-render every control from the current backend state."""
        self._connection.render()
        self._clean.render()
        self._auto.render()
        self._temp.render()

    def align_labels(self) -> float | None:
        """Measure and apply the shared label baseline. Returns the offset.

        Requires computed geometry: until layout has run, a section's group
        still reports CTkFrame's default requested height. The caller flushes
        (App._align_all does one shared pass rather than one per card).
        """
        if not self.winfo_exists():
            return None
        factor = scaling(self)
        # winfo_reqheight is device pixels; place(y=) is scaled by CustomTkinter.
        heights = [s.group.winfo_reqheight() / factor for s in self._sections]
        if not heights:
            return None
        top = max(0.0, (theme.CARD_MIN_H - max(heights)) / 2)
        if top != self._label_offset:
            for section in self._sections:
                section.set_top(top)
            self._label_offset = top
        return top

    # NOTE: no reveal/grow animation here, deliberately.
    #
    # Animating the card's height was tried and removed. Inside a
    # CTkScrollableFrame, changing a child's height alters the scrollregion,
    # which calls CTkScrollbar.set -> _draw and cascades a _draw over every
    # descendant CTkFrame. Per animation frame that is far more work than the
    # whole card costs to build, so the "smooth" reveal was strictly jankier
    # than inserting the card in one shot.

    # -- chrome -----------------------------------------------------------

    def _section(self, parent, label: str, width: int | None = None) -> _Section:
        section = _Section(parent, label, width)
        if self._label_offset is not None:
            # Position before the children are built, so nothing is drawn at
            # one offset and then moved to another.
            section.set_top(self._label_offset)
        self._sections.append(section)
        return section

    def _sep(self, parent) -> None:
        # mx-1 == 4px either side; self-stretch spans the flex line.
        _VSep(parent).pack(side="left", fill="y", padx=4, pady=0)

    def _set_hover(self, hovered: bool) -> None:
        self.configure(
            border_color=theme.BORDER_CARD_HOVER if hovered else theme.BORDER_CARD
        )

    def _entry(self, parent, value: str, mono: bool, placeholder: str,
               on_change: Callable[[str], None]) -> ctk.CTkEntry:
        """Shared input treatment: bg-[#111419] border rounded-lg py-2."""
        var = ctk.StringVar(value=value)
        entry = ctk.CTkEntry(
            parent,
            textvariable=var,
            font=fonts.mono(13) if mono else fonts.sans(13),
            text_color=theme.TEXT_MONO if mono else theme.TEXT_PRIMARY,
            placeholder_text=placeholder,
            placeholder_text_color=theme.TEXT_GHOST,
            fg_color=theme.INPUT_BG,
            border_color=theme.BORDER_INPUT,
            border_width=1,
            corner_radius=theme.CTRL_RADIUS,
            height=36,
        )
        var.trace_add("write", lambda *_: on_change(var.get()))
        # focus:border-blue-500/50 (the /20 ring has no Tk equivalent).
        entry.bind("<FocusIn>",
                   lambda _e: entry.configure(border_color=theme.FOCUS_BORDER))
        entry.bind("<FocusOut>",
                   lambda _e: entry.configure(border_color=theme.BORDER_INPUT))
        return entry

    # -- columns ----------------------------------------------------------

    def _build_name(self, parent) -> None:
        section = self._section(parent, "Device Name", theme.COL_NAME)
        section.pack(side="left", fill="y")

        holder = ctk.CTkFrame(section.body, fg_color="transparent")
        holder.pack(fill="x")

        entry = self._entry(
            holder, self.device.name, False, "Camera name…",
            lambda value: self._on_rename(self.device_id, value),
        )
        entry.pack(fill="x")

        # Remove control, in the slot the decorative pencil occupied.
        # `bg_color` must be the *entry's* fill, not the column's: the label is
        # placed on top of the entry but parented to the column, so a
        # transparent label paints the column's colour and shows as a lighter
        # square over the darker field.
        self._trash = ctk.CTkLabel(
            holder, text="", image=icons.get("trash", theme.TRASH_IDLE, 11),
            width=14, height=14, bg_color=theme.INPUT_BG,
        )
        self._trash.place(relx=1.0, rely=0.5, x=-10, anchor="e")
        Interactive(
            self._trash,
            on_hover=self._set_trash_hover,
            on_click=lambda: self._on_remove(self.device_id),
        )

    def _set_trash_hover(self, hovered: bool) -> None:
        colour = theme.TRASH_HOVER if hovered else theme.TRASH_IDLE
        self._trash.configure(image=icons.get("trash", colour, 11))

    def _build_ip(self, parent) -> None:
        section = self._section(parent, "IP Address", theme.COL_IP)
        section.pack(side="left", fill="y")

        entry = self._entry(
            section.body, self.device.ip, True, "192.168.x.x",
            lambda value: self._on_ip_change(self.device_id, value),
        )
        entry.pack(fill="x", padx=16)  # inner px-4

    def _build_connection(self, parent) -> None:
        section = self._section(parent, "Connection", theme.COL_CONN)
        section.pack(side="left", fill="y")

        self._connection = ConnectionButton(
            section.body, self.device,
            lambda: self._on_connect(self.device_id),
        )
        self._connection.pack(fill="x", padx=16)  # inner px-4

    def _build_actions(self, parent) -> None:
        section = self._section(parent, "Actions")
        section.pack(side="left", fill="both", expand=True)  # flex-1

        wrap = ctk.CTkFrame(section.body, fg_color="transparent")
        wrap.pack(fill="x", padx=20)  # inner px-5

        self._clean = CleanButton(
            wrap, self.device, lambda: self._on_clean(self.device_id)
        )
        self._clean.pack(side="left")

        self._auto = AutoButton(
            wrap, self.device, lambda: self._on_auto(self.device_id)
        )
        self._auto.pack(side="left", padx=(8, 0))  # gap-2

    def _build_temperature(self, parent) -> None:
        section = self._section(parent, "Temperature", theme.COL_TEMP)
        section.pack(side="left", fill="y")

        self._temp = TempButton(
            section.body, self.device,
            lambda: self._on_temperature(self.device_id),
        )
        self._temp.pack(fill="x", padx=(16, 0))  # inner pl-4
