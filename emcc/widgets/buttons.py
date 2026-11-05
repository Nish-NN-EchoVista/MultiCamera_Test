"""The interactive controls inside a device card.

Each is a frame + children rather than a CTkButton, because the design's exact
icon/label gaps (gap-1.5, gap-2, gap-2.5) and the connection button's leading
status dot cannot be reproduced with a stock button's single image slot.
Hover and click are supplied by widgets.interactive.Interactive.

Every control renders `DeviceState` from the backend. None of them holds its
own copy of device state, and nothing infers state from widget colour -- the
UI is a view, and `DeviceManager` is the source of truth.

PAINT vs LAYOUT
---------------
Every control splits its update in two, and the distinction matters:

* `_paint()`  -- colours and text only. Never adds, removes or resizes a widget.
* `render()`  -- paint, plus reconciling which children are packed.

Hover calls `_paint()` **only**. An earlier version had hover call `render()`,
so the pack/pack_forget reconciliation re-ran on every pointer enter and leave;
together with non-idempotent rebinding that produced visible layout thrash --
the Clean and Auto buttons appeared to expand and jitter under the cursor. A
hover is a colour change and nothing else, so it must not touch geometry.

Child presence is tracked in plain Python booleans rather than queried with
`winfo_ismapped()`, which reports stale values until Tk has processed the
geometry queue and so cannot be trusted as the source of truth.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .. import fonts, icons, theme
from ..backend.device_manager import DeviceState
from .connection_view import connection_visual
from .interactive import Interactive
from .toggle import AutoToggle


#: Sentinel for "this property has never been written", distinct from any
#: value a caller could legitimately pass.
_NEVER = object()


def _paint_diff(owner, widget, **props) -> None:
    """`widget.configure(**props)`, minus the properties already at that value.

    A no-op re-render of one card issued **24** redundant `configure` calls,
    measured, costing **21.8 ms** of Tcl round-trips against a 1.3 ms floor --
    544 ms across 25 cards, more than five times the 100 ms pump budget.

    **This caches a belief about widget state, which is the pattern F11
    deleted one layer up tonight**: `ViewHost` cached which view was behind,
    the belief went wrong, and the Dashboard stayed silently stale. F11's
    remedy was to observe instead of remember -- but observing here means
    `cget()` per property, which is itself the round-trip being saved, so
    caching is the only option and its safety has to be argued rather than
    designed away.

    **It rests on `_paint` being the sole writer of every property it sets.**
    Verified by AST over `buttons.py` and `interactive.py`: of 23
    (receiver, property) pairs, the 19 that `_paint` writes have `_paint` as
    their only writer. The other 4 are disjoint -- `TempButton._caption`'s
    text and colour belong to `_sync_alert` alone, and `cursor` to
    `Interactive` -- so neither can stale this cache.

    **That AST covers project code, and project code is not the whole of the
    risk: a library that already writes was never "added" by anyone.**
    CustomTkinter reapplies colours on an appearance-mode change and geometry
    on a scaling change, and no scan of `emcc/` can see either. Measured
    rather than assumed:

        set_appearance_mode / set_default_color_theme in emcc/   0 calls
            -> colour reapplication is unreachable, not just unlikely
        the four controls build CTkFrame and CTkLabel only
            CTkFrame._set_scaling / CTkLabel._set_scaling
            touch no property this diffs
        CTkButton._set_scaling does write on a "text" path, but the write is
            `self._text_label.configure(font=...)` -- FONT, not text

    So CTk's own writes are geometry and font: disjoint from the four
    properties diffed here.

    **Re-check whenever ANY writer of a diffed property appears -- including a
    dependency's, which arrives without a commit in this repo.** A
    CustomTkinter upgrade is an occasion to redo the measurement above. The
    failure is silent and in the direction that looks correct.

    Images are deliberately not routed through here. `icons.get()` returns a
    fresh object per call, so an identity or equality test would never match
    and the diff would buy nothing while adding a way to keep a stale icon.
    `CleanButton` already guards its icon with `_icon_visible`.
    """
    cache = owner.__dict__.setdefault("_painted", {})
    changed = {k: v for k, v in props.items()
               if cache.get((widget, k), _NEVER) != v}
    if changed:
        widget.configure(**changed)
        for k, v in changed.items():
            cache[(widget, k)] = v


#: Icon slot plus its gap (11 + gap-1.5), mirrored after the label.
_ICON_SLOT = 11 + 6
_LABEL_H = 15


def _face(frame: ctk.CTkFrame, pad: tuple[int, int], width: int | None,
          content_h: int) -> ctk.CTkFrame:
    """The row that holds a control's content, sized and centred.

    Without `width` the control is exactly as wide as its content plus `pad`,
    which is how every control was built before the Log button arrived.

    With `width` the control has that width -- the dashboard's grid may
    stretch it further -- and its content is *centred*. That is what lets a
    row of different words (Clean, Deice, Defog, Log) sit in buttons of one
    size with every label in the middle, which natural widths could not do:
    each button was its word plus padding, so no two matched. The height is
    the content's plus `pad`, so a fixed-width control is exactly as tall as
    a natural one.
    """
    row = ctk.CTkFrame(frame, fg_color="transparent")
    if width is None:
        row.pack(padx=pad[0], pady=pad[1])
    else:
        # +2 for the 1px border, top and bottom.
        frame.configure(width=width, height=content_h + 2 * pad[1] + 2)
        frame.pack_propagate(False)
        row.place(relx=0.5, rely=0.5, anchor="center")
    return row


def _icon_and_label(row: ctk.CTkFrame, text: str) -> tuple[ctk.CTkLabel, ctk.CTkLabel]:
    """The tick slot, the word, and a blank the size of the tick slot.

    The tick's slot is reserved permanently (see `CleanButton`), which put
    every label 8.5px right of centre when idle. The blank after the label
    mirrors it, so the *word* is what sits at the centre whether the tick is
    showing or not, and showing it moves nothing.
    """
    icon = ctk.CTkLabel(row, text="", width=11, height=_LABEL_H)
    icon.pack(side="left", padx=(0, 6))  # gap-1.5, always present
    label = ctk.CTkLabel(row, text=text, font=fonts.sans(12.5, 600),
                         height=_LABEL_H)
    label.pack(side="left")
    ctk.CTkFrame(row, fg_color="transparent", width=_ICON_SLOT,
                 height=_LABEL_H).pack(side="left")
    return icon, label


class ConnectionButton(ctk.CTkFrame):
    """Connect / Connecting / Connected / Reconnecting / Reconnect.

    Markup: w-full flex items-center gap-2.5 {bg} ring-1 text-[12.5px]
            font-semibold rounded-lg px-3 py-2
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None]):
        super().__init__(master, fg_color="transparent")
        self._device = device
        self._hovered = False
        self._style = ""

        self._button = ctk.CTkFrame(self, corner_radius=theme.CTRL_RADIUS,
                                    border_width=1)
        self._button.pack(fill="x")

        row = ctk.CTkFrame(self._button, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=8)  # px-3 py-2

        self._dot = ctk.CTkFrame(row, width=8, height=8, corner_radius=4,
                                 border_width=0)
        self._dot.pack(side="left")
        self._dot.pack_propagate(False)

        self._label = ctk.CTkLabel(row, text="", font=fonts.sans(12.5, 600),
                                   anchor="w", height=15)
        self._label.pack(side="left", padx=(10, 0))  # gap-2.5

        self._caption = ctk.CTkLabel(self, text="", font=fonts.sans(10),
                                     anchor="w", height=13)
        self._caption.pack(fill="x", padx=(2, 0), pady=(6, 0))  # mt-1.5 pl-0.5

        self._interactive = Interactive(self._button, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _visual(self):
        return connection_visual(
            self._device.connection,
            attempt=self._device.reconnect_attempt,
            total_attempts=self._device.reconnect_total,
            error=self._device.last_error,
            has_ip=bool(self._device.ip),
        )

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _paint(self) -> None:
        visual = self._visual()
        colours = theme.CONN[visual.style]

        _paint_diff(
            self, self._button,
            fg_color=colours["bg_hover"] if self._hovered else colours["bg"],
            border_color=colours["ring"],
        )
        _paint_diff(self, self._label, text=visual.label,
                    text_color=colours["text"])
        _paint_diff(self, self._caption, text=visual.caption,
                    text_color=colours["caption_color"])
        _paint_diff(self, self._dot, fg_color=colours["dot"])
        self._style = visual.style

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()




class CleanButton(ctk.CTkFrame):
    """Clean.

    Markup: flex items-center gap-1.5 text-[12.5px] font-semibold rounded-lg
            px-4 py-2.

    The check icon's 11px slot is reserved permanently rather than packed and
    unpacked with the active state. The design only shows the icon when
    active, but Clean now toggles on every press and again on the 10s timeout,
    so letting the width change would shunt the Auto button sideways several
    times a minute. The slot is invisible when idle.
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None],
                 pad: tuple[int, int] = (16, 8), width: int | None = None):
        super().__init__(master, corner_radius=theme.CTRL_RADIUS, border_width=1)
        self._device = device
        self._hovered = False
        self._icon_visible = False

        # `pad` and `width` are the only geometry the caller may vary: the
        # dashboard fits this control into a 112px card and cannot afford
        # px-4 py-2, and both views give their action buttons one shared
        # width so the labels line up (see `_face`).
        row = _face(self, pad, width, _LABEL_H)
        self._icon, self._label = _icon_and_label(row, "Clean")

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _colours(self) -> tuple[str, str, str]:
        if self._device.clean_active:
            return (theme.ON_BG_HOVER if self._hovered else theme.ON_BG,
                    theme.ON_BORDER, theme.ON_TEXT)
        return (theme.CLEAN_OFF_BG_HOVER if self._hovered else theme.CLEAN_OFF_BG,
                theme.CLEAN_OFF_BORDER, theme.CLEAN_OFF_TEXT)

    def _paint(self) -> None:
        fill, border, text = self._colours()
        _paint_diff(self, self, fg_color=fill, border_color=border)
        _paint_diff(self, self._label, text_color=text)

        active = self._device.clean_active
        if active != self._icon_visible:
            # Swapping the image, not the packing -- the slot never moves.
            # Hiding uses a transparent image rather than `image=None`, which
            # CustomTkinter accepts but does not apply: the tick would stay on
            # screen after a sweep completed, timed out or the link dropped.
            self._icon.configure(
                image=icons.get("check", text, 11) if active else icons.blank(11)
            )
            self._icon_visible = active
        elif active:
            self._icon.configure(image=icons.get("check", text, 11))

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()


class DeiceButton(ctk.CTkFrame):
    """Deice.

    `CleanButton` in every respect except the label and the flag it reads.
    It is a separate class rather than a `label`/`flag` parameter on
    CleanButton because that button's docstring documents a behaviour the two
    do not share -- Clean's icon slot exists because Clean re-fires on a 10s
    timeout "several times a minute", which de-ice does not do. Collapsing
    them would put a comment on CleanButton that is true of only one caller.

    The reserved icon slot is kept all the same: Deice sits between Clean and
    Auto in the list view, so a width change here would shunt Auto sideways
    exactly as the original note describes.
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None],
                 pad: tuple[int, int] = (16, 8), width: int | None = None):
        super().__init__(master, corner_radius=theme.CTRL_RADIUS, border_width=1)
        self._device = device
        self._hovered = False
        self._icon_visible = False

        row = _face(self, pad, width, _LABEL_H)
        self._icon, self._label = _icon_and_label(row, "Deice")

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _colours(self) -> tuple[str, str, str]:
        if self._device.deice_active:
            return (theme.ON_BG_HOVER if self._hovered else theme.ON_BG,
                    theme.ON_BORDER, theme.ON_TEXT)
        return (theme.CLEAN_OFF_BG_HOVER if self._hovered else theme.CLEAN_OFF_BG,
                theme.CLEAN_OFF_BORDER, theme.CLEAN_OFF_TEXT)

    def _paint(self) -> None:
        fill, border, text = self._colours()
        _paint_diff(self, self, fg_color=fill, border_color=border)
        _paint_diff(self, self._label, text_color=text)

        active = self._device.deice_active
        if active != self._icon_visible:
            # See CleanButton: hiding uses a transparent image, because
            # `image=None` is accepted and then not applied.
            self._icon.configure(
                image=icons.get("check", text, 11) if active else icons.blank(11)
            )
            self._icon_visible = active
        elif active:
            self._icon.configure(image=icons.get("check", text, 11))

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()


class AutoButton(ctk.CTkFrame):
    """Auto.

    Markup: flex items-center gap-2 text-[12.5px] font-semibold rounded-lg
            px-4 py-2, containing the pill toggle then the word "Auto".
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None],
                 pad: tuple[int, int] = (16, 8), width: int | None = None):
        super().__init__(master, corner_radius=theme.CTRL_RADIUS, border_width=1)
        self._device = device
        self._hovered = False

        # See CleanButton: `pad` exists for the dashboard's compact card,
        # `width` for the dashboard's even grid.
        row = _face(self, pad, width, 16)

        self._toggle = AutoToggle(row, on=device.auto_enabled)
        self._toggle.pack(side="left")

        self._label = ctk.CTkLabel(row, text="Auto", font=fonts.sans(12.5, 600),
                                   height=16)
        self._label.pack(side="left", padx=(8, 0))  # gap-2

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render(animate=False)

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _paint(self) -> None:
        if self._device.auto_enabled:
            fill = theme.ON_BG_HOVER if self._hovered else theme.ON_BG
            border, text = theme.ON_BORDER, theme.ON_TEXT
        else:
            fill = theme.AUTO_OFF_BG
            border = (theme.AUTO_OFF_BORDER_HOVER if self._hovered
                      else theme.AUTO_OFF_BORDER)
            text = (theme.AUTO_OFF_TEXT_HOVER if self._hovered
                    else theme.AUTO_OFF_TEXT)
        _paint_diff(self, self, fg_color=fill, border_color=border)
        _paint_diff(self, self._label, text_color=text)

    def render(self, animate: bool = True) -> None:
        self._paint()
        self._toggle.set(self._device.auto_enabled, animate=animate)
        self._interactive.refresh()


class DefogButton(ctk.CTkFrame):
    """Defog.

    A momentary button, not a toggle: Defog is a ~3 minute *sequence*, so the
    lit state means "running now", the same as Clean and Deice, rather than
    "switched on" the way Auto's pill does. A pill would invite the reading
    that defog stays on until switched off, which is not what the hardware
    does -- it runs its course and exits.

    The active state is therefore not the operator's to hold. It clears when
    the firmware reports the sequence exited, when the timeout fires, or when
    the operator presses again to abort -- see `DeviceManager.press_defog`
    for why a second press stops rather than restarts.
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None],
                 pad: tuple[int, int] = (16, 8), width: int | None = None):
        super().__init__(master, corner_radius=theme.CTRL_RADIUS, border_width=1)
        self._device = device
        self._hovered = False
        self._icon_visible = False

        # The tick slot is reserved permanently, as on Clean and Deice: this
        # button sits in a row of five, so a width change here would move its
        # neighbours.
        row = _face(self, pad, width, _LABEL_H)
        self._icon, self._label = _icon_and_label(row, "Defog")

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _colours(self) -> tuple[str, str, str]:
        if self._device.defog_active:
            return (theme.ON_BG_HOVER if self._hovered else theme.ON_BG,
                    theme.ON_BORDER, theme.ON_TEXT)
        return (theme.CLEAN_OFF_BG_HOVER if self._hovered else theme.CLEAN_OFF_BG,
                theme.CLEAN_OFF_BORDER, theme.CLEAN_OFF_TEXT)

    def _paint(self) -> None:
        fill, border, text = self._colours()
        _paint_diff(self, self, fg_color=fill, border_color=border)
        _paint_diff(self, self._label, text_color=text)

        active = self._device.defog_active
        if active != self._icon_visible:
            # See CleanButton: hiding uses a transparent image, because
            # `image=None` is accepted and then not applied.
            self._icon.configure(
                image=icons.get("check", text, 11) if active else icons.blank(11)
            )
            self._icon_visible = active
        elif active:
            self._icon.configure(image=icons.get("check", text, 11))

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()


class LogButton(ctk.CTkFrame):
    """Log: whether this device's lines are being written to its log folder.

    A switch, unlike Clean, Deice and Defog: lit means "being logged now" and
    it stays lit until pressed again. It is drawn like them rather than like
    Auto's pill because the operator asked for it in the same font and size
    as the other action buttons, and the green fill with a tick already reads
    as "on" in this row.

    It never starts lit: logging is not remembered across restarts.
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None],
                 pad: tuple[int, int] = (16, 8), width: int | None = None):
        super().__init__(master, corner_radius=theme.CTRL_RADIUS, border_width=1)
        self._device = device
        self._hovered = False
        self._icon_visible = False

        row = _face(self, pad, width, _LABEL_H)
        self._icon, self._label = _icon_and_label(row, "Log")

        self._interactive = Interactive(self, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _colours(self) -> tuple[str, str, str]:
        if self._device.logging_enabled:
            return (theme.ON_BG_HOVER if self._hovered else theme.ON_BG,
                    theme.ON_BORDER, theme.ON_TEXT)
        return (theme.CLEAN_OFF_BG_HOVER if self._hovered else theme.CLEAN_OFF_BG,
                theme.CLEAN_OFF_BORDER, theme.CLEAN_OFF_TEXT)

    def _paint(self) -> None:
        fill, border, text = self._colours()
        _paint_diff(self, self, fg_color=fill, border_color=border)
        _paint_diff(self, self._label, text_color=text)

        active = self._device.logging_enabled
        if active != self._icon_visible:
            # See CleanButton: hiding uses a transparent image, because
            # `image=None` is accepted and then not applied.
            self._icon.configure(
                image=icons.get("check", text, 11) if active else icons.blank(11)
            )
            self._icon_visible = active
        elif active:
            self._icon.configure(image=icons.get("check", text, 11))

    def render(self) -> None:
        self._paint()
        self._interactive.refresh()


class TempButton(ctk.CTkFrame):
    """Temperature readout.

    Markup: w-full flex items-center justify-between gap-2 bg-[#111419]
            border rounded-lg px-3.5 py-2, with a HIGH badge above threshold
            and a "view" affordance below it.

    Shows an em dash until the first sample arrives -- the design always shows
    a number, and a stale or invented reading on a disconnected device would be
    worse than showing none.

    Clicking opens the placeholder detail window; see integrations.py.
    """

    def __init__(self, master, device: DeviceState, on_click: Callable[[], None]):
        super().__init__(master, fg_color="transparent")
        self._device = device
        self._hovered = False
        self._alert_shown: bool | None = None

        self._button = ctk.CTkFrame(self, corner_radius=theme.CTRL_RADIUS,
                                    border_width=1, fg_color=theme.INPUT_BG)
        self._button.pack(fill="x")

        row = ctk.CTkFrame(self._button, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=8)  # px-3.5 py-2

        self._value = ctk.CTkLabel(row, text="", font=fonts.mono(15, 600),
                                   anchor="w", height=18)
        self._value.pack(side="left")

        self._badge = ctk.CTkFrame(
            row, corner_radius=theme.BADGE_RADIUS, border_width=1,
            fg_color=theme.BADGE_BG, border_color=theme.BADGE_BORDER,
        )
        ctk.CTkLabel(self._badge, text="HIGH", font=fonts.sans(9, 700),
                     text_color=theme.BADGE_TEXT, height=11).pack(padx=6, pady=2)

        self._hint = ctk.CTkLabel(row, text="▲ view", font=fonts.sans(9),
                                  height=12)
        self._caption = ctk.CTkLabel(self, text="", font=fonts.sans(10),
                                     anchor="w", height=13)

        self._interactive = Interactive(self._button, on_hover=self._set_hover,
                                        on_click=on_click)
        self.render()

    def _set_hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _paint(self) -> None:
        device = self._device
        alert = device.temperature_alert
        has_reading = device.latest_temperature is not None

        if alert:
            border = (theme.TEMP_ALERT_BORDER_HOVER if self._hovered
                      else theme.TEMP_ALERT_BORDER)
            fill = theme.TEMP_ALERT_BG_HOVER if self._hovered else theme.INPUT_BG
            value_colour = theme.TEMP_ALERT_TEXT
        else:
            border = (theme.TEMP_OK_BORDER_HOVER if self._hovered
                      else theme.TEMP_OK_BORDER)
            fill = theme.TEMP_OK_BG_HOVER if self._hovered else theme.INPUT_BG
            value_colour = (theme.TEMP_OK_TEXT if has_reading else theme.TEXT_GHOST)
            _paint_diff(
                self, self._hint,
                text_color=theme.TEXT_LABEL if self._hovered else theme.TEXT_GHOST,
            )

        _paint_diff(self, self._button, border_color=border, fg_color=fill)
        _paint_diff(
            self, self._value,
            text=(f"{device.latest_temperature:.1f}°C" if has_reading
                  else f"{theme.TEMP_PLACEHOLDER}°C"),
            text_color=value_colour,
        )

    def render(self) -> None:
        self._paint()
        self._sync_alert()
        self._interactive.refresh()

    def _sync_alert(self) -> None:
        """Swap the badge and the hint. No-ops unless the threshold was crossed.

        **`_caption` keeps its text when unpacked, and that is unobservable.**
        The `else` branch below calls `pack_forget()` without clearing the
        text, so a card that has been in alert and left it still holds
        "Exceeds threshold" on a hidden widget. Checked rather than assumed:
        this caption has exactly **one** pack site -- the `pack` in the alert
        branch -- and the `configure(text=...)` two lines above it always
        runs first. There is no path that re-packs it without setting the
        text, so the stale value cannot reach the screen.

        Left as it is deliberately. Clearing the text on hide would be two
        lines and would look tidier, but it would also be an untested branch
        guarding nothing, which is the shape this file spent the night
        removing. **If a second pack site is ever added, clear the text on
        hide** -- that is the change which makes the staleness reachable.

        Predates the paint-diff: verified by running the same probe against
        `b516d7e`, byte-identical result.

        **`_alert_shown` IS A CACHED BELIEF ABOUT WIDGET STATE, and it
        passes the same sole-writer test the paint-diff had to pass.**
        It short-circuits this whole method, so if anything changed the alert
        display without coming through here the belief would go stale and the
        badge would stop tracking the device -- the failure mode `ViewHost`
        had before F11, one layer down. Checked by AST rather than by eye,
        every `pack` / `pack_forget` / `configure` on the three widgets
        this display owns:

            TempButton   :410 :411 :412 :414 :416 :417 :418   ALL in _sync_alert
            ConnectionButton :137                             a DIFFERENT class

        Seven writers, all here. The eighth hit is `ConnectionButton._caption`,
        which shares a name and nothing else -- the same-name-different-class
        trap that produced a wrong read of this file once already, so the
        check attributes each site to its class rather than matching
        `self._caption` across the module.

        Re-check if a second writer appears -- including one in a library,
        which arrives without a commit in this repo.
        """
        alert = self._device.temperature_alert
        if alert == self._alert_shown:
            return
        if alert:
            self._hint.pack_forget()
            self._badge.pack(side="right")
            self._caption.configure(text="Exceeds threshold",
                                    text_color=theme.TEMP_ALERT_CAPTION)
            self._caption.pack(fill="x", pady=(6, 0))  # mt-1.5
        else:
            self._badge.pack_forget()
            self._caption.pack_forget()
            self._hint.pack(side="right")
        self._alert_shown = alert
