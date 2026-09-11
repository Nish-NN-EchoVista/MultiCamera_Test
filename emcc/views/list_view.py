"""The device list: one full-width card per device, built progressively.

**A controller, not a `CTkFrame` subclass.** `widget` returns the
`CTkScrollableFrame` it creates -- the same object, created the same way and
with the same master, as `app.py` created it before this extraction. Making
`ListView` the frame would change its Tk pathname and renumber `App`'s other
`CTkFrame` children, which is the whole reason `VIEW_CONTRACT.md` declares
`widget` instead of assuming a view is one.

Relocated from `app.py`'s "Device list" section, and since slice 4 this owns
the scroll hint and scrollbar-visibility rules too -- they are this view's
business, not the shell's. The Dashboard has a scrollport but no rules
governing it, which is why they live here rather than in a shared base class.

One thing stays behind deliberately: **the label-offset cache.** `offsets` is
handed in and held by reference rather than owned here, because the shell's
`render_device`, `measure_variant` and `_settle` all still use it. One dict,
one source of truth -- a second cache here would diverge silently.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

import customtkinter as ctk

from .. import fonts, theme
from ..backend.device_manager import DeviceManager, DeviceState
from ..widgets.add_device import AddDeviceButton
from ..widgets.device_card import DeviceCard
from .base import StandaloneCtx

logger = logging.getLogger("emcc.ui")

#: Cards built before the window is shown. Enough to fill the viewport at the
#: default window height (142px card + 12px gap); the rest stream in on idle.
INITIAL_CARDS = 5

#: Gap between streamed cards. A timer, not `after_idle`: a single
#: `update_idletasks()` drains an entire after_idle chain, because each
#: callback re-queues before the queue is checked again -- which silently
#: turns the progressive build back into a blocking one.
CARD_BUILD_INTERVAL_MS = 1


class ListView:
    """flex-1 px-8 py-4 flex flex-col gap-3, scrolling past the threshold."""

    name = "list"

    def __init__(self, master, ctx=None, *,
                 manager: DeviceManager,
                 offsets: dict,
                 devices: Sequence[DeviceState] | None = None,
                 on_add: Callable[[], None],
                 on_connect: Callable[[str], None],
                 on_clean: Callable[[str], None],
                 on_auto: Callable[[str], None],
                 on_temperature: Callable[[str], None],
                 on_remove: Callable[[str], None],
                 on_rename: Callable[[str, str], object],
                 on_ip_change: Callable[[str, str], object],
                 on_changed: Callable[[], None] | None = None,
                 is_shutting_down: Callable[[], bool] | None = None) -> None:
        self._ctx = ctx if ctx is not None else StandaloneCtx(master)
        if is_shutting_down is not None:
            self._is_shutting_down = is_shutting_down
        else:
            self._is_shutting_down = self._ctx.is_shutting_down

        self.manager = manager
        #: Held by reference, not copied -- see the module docstring.
        self.offsets = offsets

        self._on_add = on_add
        self._on_connect = on_connect
        self._on_clean = on_clean
        self._on_auto = on_auto
        self._on_temperature = on_temperature
        self._on_remove = on_remove
        self._on_rename = on_rename
        self._on_ip_change = on_ip_change
        #: Called wherever `_rebuild`/`_build_remaining` called
        #: `_refresh_chrome`, so the shell still refreshes at the same points.
        self._on_changed = on_changed or (lambda: None)

        self.cards: dict[str, DeviceCard] = {}
        self.order: list[str] = []
        self.pending_devices: list[DeviceState] = []
        self._build_job = None

        self._frame = ctk.CTkScrollableFrame(
            master,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=theme.SCROLL_THUMB,
            scrollbar_button_hover_color=theme.SCROLL_THUMB_HOVER,
            scrollbar_fg_color="transparent",
        )

        # .device-scroll is 5px wide; CTkScrollbar defaults to 16 and stops
        # rendering a usable thumb below ~8, so 8 is the closest faithful value.
        bar = getattr(self._frame, "_scrollbar", None)
        if bar is not None:
            bar.configure(width=8)

        # The add button and the scroll hint are created ONCE and stay put --
        # new cards are inserted before the button. Recreating them per add is
        # what made adding a device redraw the entire list.
        self.add_button = AddDeviceButton(self._frame, on_click=self._on_add)
        self.add_button.pack(fill="x", pady=(theme.CARD_GAP + 4, 0))

        self.hint = ctk.CTkLabel(
            self._frame, text="", font=fonts.sans(10.5),
            text_color=theme.TEXT_SCROLL_HINT, height=14,
        )
        #: Cached because the guard in `on_device_count_changed` is the whole
        #: point of it, and because the hint has no derivable alternative:
        #: after `pack_forget()` Tk's `pack_info()` **raises TclError** rather
        #: than returning empty, so there is nothing to observe. The scrollbar
        #: used to be cached alongside it and no longer is -- `grid_info()`
        #: returns `{}` after `grid_remove()`, so that one can be observed.
        #: Measured both.
        self._hint_shown = False

        self._devices: list[DeviceState] = (
            list(devices) if devices is not None else list(manager.devices)
        )

    # -- host surface ----------------------------------------------------

    @property
    def widget(self):
        return self._frame

    @property
    def padding(self) -> dict:
        return {"padx": theme.PAGE_PAD_X - 8, "pady": theme.PAGE_PAD_Y}

    @property
    def heading(self) -> str:
        """Raw words; `SubHeader` applies the letter-spacing.

        The string itself lives in `theme.py` beside `DASH_SUBHEAD`, which is
        where this codebase keeps user-visible wording -- seven such constants
        are there already. Discoverability decided it rather than taxonomy:
        grepping the string works either way, and so does reading the contract
        and visiting each view, but *looking in `theme.py` for UI wording*
        only works if the string is in it. More paths reach it there, and
        there is no asymmetry left to explain.
        """
        return theme.LIST_SUBHEAD

    def caption(self, total: int) -> str:
        return (f"{total} device{'' if total == 1 else 's'} configured Ãƒâ€šÃ‚Â· "
                "click Connect to establish TCP connection")

    def show(self) -> None:
        """Reconcile against the device set, then build if nothing is built.

        Reconciling rather than repainting: the set can have moved while this
        view was hidden, and the host's `set_devices` on a dirty show is belt,
        this is braces.
        """
        if not self.cards and not self.pending_devices:
            self.rebuild()

    def hide(self) -> None:
        # Nothing to tear down: the progressive build is safe to leave
        # running, and cancelling it would make a switch away lose cards that
        # a switch back would then have to rebuild.
        pass

    def set_devices(self, devices: Sequence[DeviceState]) -> None:
        self._devices = list(devices)
        self.rebuild()

    def device_fingerprint(self) -> tuple[tuple[str, str], ...]:
        """Built cards followed by devices still queued to be built.

        NOT `self._devices`, and the difference is the whole point. The shell
        appends a card in place through `new_card` without updating
        `_devices`, so `_devices` is stale after any add -- a fingerprint from
        it would report the view as behind and trigger the full rebuild this
        mechanism exists to avoid.

        NOT `order` alone either: `rebuild` streams cards one per idle tick,
        so mid-build `order` holds only a prefix and the view would report
        itself behind while it was merely unfinished.

        Together they are exactly what the view holds. `rebuild` fills
        `pending_devices` from `_devices` and pops from the front as it
        appends to `order`, so the concatenation is the full set in display
        order at every point during the build.
        """
        return tuple(
            [(device_id, self.cards[device_id].device.name)
             for device_id in self.order]
            + [(device.id, device.name) for device in self.pending_devices]
        )

    # -- building --------------------------------------------------------

    def rebuild(self) -> None:
        """Build the initial list, progressively.

        A card is ~90 CustomTkinter widgets and costs a few hundred
        milliseconds, essentially all of it Tcl round-trips. Building 25 of
        them before the window appears measured **11.1 seconds** of blank
        screen, which fails the "responsive with 25+ devices" requirement
        outright.

        So only enough cards to fill the viewport are built synchronously; the
        rest stream in one per idle tick. The window is up and interactive in
        well under a second, and the remaining cards appear as the operator is
        still reading the first ones. Scrolling before they finish simply shows
        them arriving, which is far better than showing nothing at all.
        """
        for card in self.cards.values():
            card.destroy()
        self.cards.clear()
        self.order.clear()
        self.pending_devices[:] = list(self._devices)

        for _ in range(INITIAL_CARDS):
            if not self.build_next_card():
                break

        self.align_all()
        self._on_changed()

        if self.pending_devices:
            self._build_job = self._ctx.after(CARD_BUILD_INTERVAL_MS,
                                              self.build_remaining)

    def new_card(self, device: DeviceState, first: bool,
                 label_offset: float | None = None) -> DeviceCard:
        card = DeviceCard(
            self._frame, device,
            on_connect=self._on_connect,
            on_clean=self._on_clean,
            on_auto=self._on_auto,
            on_temperature=self._on_temperature,
            on_remove=self._on_remove,
            on_rename=self._on_rename,
            on_ip_change=self._on_ip_change,
            label_offset=label_offset,
        )
        card.pack(fill="x", pady=(0 if first else theme.CARD_GAP, 0),
                  before=self.add_button)
        self.cards[device.id] = card
        self.order.append(device.id)
        return card

    def build_next_card(self) -> bool:
        """Build the next queued card. False when the queue is empty."""
        if not self.pending_devices:
            return False
        device = self.pending_devices.pop(0)
        # A device could have been removed while still queued.
        if self.manager.get(device.id) is None:
            return bool(self.pending_devices)
        offset = self.offsets.get(device.temperature_alert)
        self.new_card(device, first=len(self.cards) == 0, label_offset=offset)
        return True

    def build_remaining(self) -> None:
        """Stream in the rest of the cards, one per idle tick.

        Deliberately does NOT call `update_idletasks()`. Doing so re-enters
        Tk's idle queue and drains this whole chain in a single pass, which
        silently turns the progressive build back into a blocking one -- the
        exact bug this replaced. The label offset is taken from the cache that
        `align_all` seeded from the first batch, so no measurement is needed;
        a card whose variant has no cached offset is aligned later by the
        shell's `_render_device`.
        """
        self._build_job = None
        if self._is_shutting_down() or not self.pending_devices:
            self.pending_devices[:] = []
            return
        self.build_next_card()
        self._on_changed()
        if self.pending_devices:
            self._build_job = self._ctx.after(CARD_BUILD_INTERVAL_MS,
                                              self.build_remaining)

    def align_all(self) -> None:
        """One shared alignment pass, seeding the per-variant offset cache.

        Each card's alignment needs computed geometry, so doing it per card
        would mean one forced relayout per card. Batching keeps startup to a
        single pass, and the offsets learned here let every card added later
        skip measuring entirely.
        """
        self._frame.update_idletasks()
        for card in self.cards.values():
            offset = card.align_labels()
            if offset is not None:
                self.offsets[card.variant] = offset

    # -- routing ---------------------------------------------------------

    def render_device(self, device_id: str) -> bool:
        """Repaint one card. `False` when this view has no card for it.

        Only the card's own repaint. Fault announcement is the shell's, and
        stays there: separating it from card rendering is slice 3's job, and
        the shell's `_render_device` is still the path the pump uses this
        slice.
        """
        card = self.cards.get(device_id)
        if card is None:
            return False
        card.render()
        return True

    def add_device(self, device: DeviceState) -> None:
        self._devices.append(device)

    def remove_device(self, device_id: str) -> None:
        self._devices = [d for d in self._devices if d.id != device_id]

    def on_device_count_changed(self, total: int) -> None:
        """The scroll hint and the scrollbar, which are this view's business.

        Moved here in slice 4. The Dashboard has a scrollport but no rules
        governing it, so these belong to `ListView` and not to a shared base
        class -- a view inheriting a hint it has no use for is exactly what
        `VIEW_CONTRACT.md` warns against.

        **Deliberately does not call `_on_changed`.** It used to, and the
        shell's `_refresh_chrome` now calls *this*, so calling back would
        recurse without terminating.

        Both branches stay guarded, but **no longer both on a cached flag** --
        the hint compares against `_hint_shown`, the scrollbar against
        `bool(grid_info())`. This paragraph claimed both were cached until
        `8366654` derived one of them; Tk allows the derivation for `grid` and
        not for `pack`, so the asymmetry is forced rather than chosen. See
        `_set_scrollbar_visible`.

        The guards earn their place on redraw, not on cost. Re-applying
        `pack()` to a widget that already has it re-runs its geometry, and
        this runs on every chrome refresh, so the no-op case has to be a
        genuine no-op. MEASURED, because the magnitude was never checked when
        that reason was written: including the idle geometry pass it provokes,
        a redundant `pack()` costs **+1.5 us** per refresh over a do-nothing
        control and a redundant `pack_forget()` **+0.8 us**. At ten refreshes
        a second that is ~15 us/s. So the guards are worth keeping and are not
        worth defending further -- which is the reason there is no mirror of
        `test_scroll_hint_is_not_repacked_on_every_refresh` for the hidden
        branch. That test was queued and then declined on measurement: the
        mutation it was written for (`if scrolling and scrolling != ...`) is
        already caught by `test_scroll_hint_appears_only_at_the_threshold`,
        which drives the transition *down* through the threshold, and the only
        mutation left escaping costs 0.8 us a refresh.
        """
        # No device cap: the design's "supports up to 25" was a demo limit.
        self.hint.configure(text=f"Scroll to view all {total} devices")

        scrolling = total >= theme.SCROLL_THRESHOLD
        if scrolling != self._hint_shown:
            if scrolling:
                self.hint.pack(pady=(8, 4))
            else:
                self.hint.pack_forget()
            self._hint_shown = scrolling

        self._set_scrollbar_visible(scrolling)

    def _set_scrollbar_visible(self, visible: bool) -> None:
        """The design only shows the scrollbar at >= 4 devices.

        Derived from `bool(bar.grid_info())` rather than from a cached flag:
        the widget is asked what it is, instead of this view remembering what
        it last did. Same move as `ViewHost.show` asking each view for its
        `device_fingerprint`, and it is available here only because Tk allows
        it -- `grid_info()` returns `{}` after `grid_remove()`, where
        `pack_info()` raises after `pack_forget()`. The hint therefore keeps
        its cache and this does not.

        **THE CACHE WAS NOT WRONG, and the two holes this was queued to fix
        are both latent rather than live.** Measured before changing it:

        * the scrollbar *is* gridded at construction, so `= True` matched
          reality -- there was no boot hole;
        * `CTkScrollableFrame.configure()` re-grids it via `_create_grid()`,
          but only for `corner_radius`, `border_width` or `label_text`, and
          `emcc/` never calls `configure()` on that frame at all. So the
          inverse hole needs a call nobody makes.

        What the derivation buys is therefore not a bug fix. The flag's
        correctness rested on a CustomTkinter implementation detail *and* on
        an absence -- neither enforced, and the absence is one `configure()`
        away from ending. An observation cannot diverge from what it
        observes.

        Cost, because this is the reason the same move was *rejected* for
        `DeviceCard._paint`: `grid_info()` measures **7.4 us** against
        `cget()`'s 0.3, so it is the more expensive call by 25x. It survives
        here on frequency -- once per `_refresh_chrome`, which is once per
        pump tick that had changes, so ~74 us/s at the 100 ms interval.
        `_paint` faced 250 reads per render. Cheap-per-call versus
        few-per-second, and only the second is affordable.
        """
        bar = getattr(self._frame, "_scrollbar", None)
        if bar is None:
            return
        if visible == bool(bar.grid_info()):
            return
        if visible:
            bar.grid()
        else:
            bar.grid_remove()

    # -- lifecycle -------------------------------------------------------

    def shutdown(self) -> None:
        self.pending_devices[:] = []
        if self._build_job is not None:
            try:
                self._ctx.after_cancel(self._build_job)
            except Exception:
                pass
            self._build_job = None
