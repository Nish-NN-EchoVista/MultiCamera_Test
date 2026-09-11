"""The Dashboard view: many devices at once, in a 5-column grid.

Where the list view gives one device a full row -- name, IP, Connect,
temperature, actions -- the dashboard gives it a card about a seventh of that
area, and drops what does not survive the shrink: the IP address, the Connect
button, and the per-column micro-labels. What is left is name, connection
state, temperature, Clean and Auto.

Two deliberate departures from `DeviceCard`, both about density:

* **A separate card class, not a resized `DeviceCard`.** That card pins its
  height to `theme.CARD_MIN_H` with `pack_propagate(False)` and computes label
  baselines from that same constant, so shrinking the frame leaves its
  alignment maths describing a card that no longer exists. Its five fixed
  columns plus separators also put a ~700px floor on width, and a 5-column
  grid at 1280px gives each cell ~232px.
* **A static status dot.** It never pulsed: 25 cards would have meant 25
  tweens on the shared timer, and at this density a dot reads as a state key
  rather than a live indicator. The status pulse was removed from the whole
  app on 2026-09-10, so this is no longer a departure from `DeviceCard` --
  kept in the list because it explains why the dot is painted once per
  render.

`CleanButton` and `AutoButton` are reused as-is apart from their padding: they
read `DeviceState` live and hold no state of their own, so the only thing they
needed was a `pad` argument (`theme.DASH_CTRL_PAD`) to fit a 112px card. Their
list-view spacing is the default and is unchanged.

The connection *colour* also comes from the list view's own source,
`connection_view.connection_visual`, so one toggle click cannot change what a
device's colour means. Only the wording differs -- see `status_text`.

Empty grid cells are left genuinely empty -- no dashed outline, no ghost card.
A slot with no device in it is blank space.
"""

from __future__ import annotations

from typing import Callable, Sequence

import logging
import tkinter

import customtkinter as ctk

from .. import fonts, theme
from ..backend.device_manager import DeviceManager, DeviceState
from ..backend.events import ConnectionState
from .buttons import AutoButton, CleanButton
from .canvas_util import scaling
from .connection_view import connection_visual

logger = logging.getLogger("emcc.dashboard")

#: Distinct from None, which is a real value for `latest_temperature`.
_UNSET = object()


class _StandaloneCtx:
    """The host's `ctx`, for a view built without a host.

    The contract has the host supply `is_shutting_down`, `after` and
    `after_cancel`. Tests and the capture scripts build this view directly,
    so it falls back to its own widget's timers and reports that nothing is
    shutting down. Deliberately not a default the host can accidentally get:
    it is only used when no ctx was passed at all.
    """

    def __init__(self, widget):
        self._widget = widget

    def is_shutting_down(self) -> bool:
        return False

    def after(self, ms: int, fn):
        return self._widget.after(ms, fn)

    def after_cancel(self, job) -> None:
        self._widget.after_cancel(job)


class DashboardCard(ctk.CTkFrame):
    """One device, compact.

    The border carries the connection state, using the same `theme.CONN` ring
    the list view's Connect button uses -- blue for idle, green for connected,
    red for a loss nobody asked for. The two views therefore agree on what a
    device's colour means, which matters when a toggle click is all that
    separates them.
    """

    def __init__(self, master, device: DeviceState, *,
                 on_clean: Callable[[str], None],
                 on_auto: Callable[[str], None]):
        super().__init__(master, fg_color=theme.CARD,
                         corner_radius=theme.CARD_RADIUS, border_width=1,
                         border_color=theme.BORDER_CARD,
                         height=theme.DASH_CARD_H)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.device = device
        #: Last value written to screen, per painted key. See `render`.
        self._painted: dict[str, object] = {}

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=theme.DASH_CARD_PAD,
                  pady=theme.DASH_CARD_PAD)

        self._build_header(body)
        self._build_temperature(body)
        self._build_actions(body, on_clean, on_auto)

        self.render()

    # -- properties -------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self.device.id

    # -- construction -----------------------------------------------------

    def _build_header(self, parent) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x")

        # The status chip is packed FIRST so it reserves its width, and the
        # name gets what is left. Packed the other way round -- which is how
        # this was written, and what a 40-character device name exposed --
        # the expanding left frame takes the whole row and shoves the chip
        # off the card entirely: no connection state shown at all, which is
        # the one thing this view exists to make scannable.
        status = ctk.CTkFrame(header, fg_color="transparent")
        status.pack(side="right", anchor="n", padx=(6, 0), pady=(1, 0))

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(left, text=fonts.tracked(theme.DASH_LABEL),
                     font=fonts.sans(8, 600), text_color=theme.TEXT_LABEL,
                     height=11, anchor="w").pack(fill="x")

        # The name is shortened to fit rather than left to clip. `width=1`
        # alone is not enough: CustomTkinter's label still asks for its
        # natural text width, which widens the column -- and because the
        # columns are `uniform`, one long name would widen all five. So the
        # text itself is measured against the space available and ellipsised.
        # Built empty so the first layout pass is not driven by an
        # untruncated name asking for twice the width it can have. With
        # `uniform` columns the measured effect on column width was nil --
        # the columns equalise regardless -- but it removes the transient
        # and the dependency on that happening to be true.
        self._name_full = device_name(self.device)
        self._name = ctk.CTkLabel(left, text="", font=fonts.sans(13, 600),
                                  text_color=theme.TEXT_TITLE, height=17,
                                  width=1, anchor="w")
        self._name.pack(fill="x")
        self._name.bind("<Configure>", self._fit_name)

        self._dot = ctk.CTkFrame(status, width=theme.DASH_DOT,
                                 height=theme.DASH_DOT,
                                 corner_radius=theme.DASH_DOT // 2)
        self._dot.pack(side="left", pady=(3, 0))
        self._dot.pack_propagate(False)

        self._status = ctk.CTkLabel(status, text="", font=fonts.sans(10.5, 600),
                                    height=13)
        self._status.pack(side="left", padx=(5, 0))

    def _fit_name(self, event=None) -> None:
        """Shorten the name to the width the label actually has.

        Runs on `<Configure>` as well as on a rename, so a resized window
        restores characters it previously had to drop. Only writes when the
        text differs, which is what keeps a Configure handler that sets text
        from re-triggering itself.
        """
        width = self._name.winfo_width()
        font = fonts.sans(13, 600)
        if width <= 1:
            # Not laid out yet; the Configure binding will call back.
            return

        # `winfo_width` is physical pixels; `font.measure` is logical units.
        # On a 2.25x display a 40-character name measures 268 against a label
        # 317 pixels wide and looks like it fits, while it actually needs 600
        # -- so the comparison has to happen in one unit or it silently never
        # truncates on exactly the machines where the text is largest.
        # Deferred to `canvas_util.scaling` rather than looked up inline: this
        # was the fourth call site for the same question and the only one that
        # answered it differently. It caught everything and substituted 1 --
        # the defect F9 removed from `scaling` itself -- and here a wrong 1.0
        # compares `font.measure` against a 2.25x overstated width, so a long
        # name silently never truncates on exactly the displays where the text
        # is largest. The `or 1` guarded nothing reachable either:
        # `set_widget_scaling` clamps to `max(factor, 0.4)` and the DPI term is
        # `(x_dpi + y_dpi) / 192`, strictly positive, so the product cannot be
        # falsy.
        width = width / scaling(self._name)

        text = self._name_full
        if font.measure(text) > width:
            ellipsis = "…"
            budget = max(0, width - font.measure(ellipsis))
            kept = 0
            for index in range(1, len(text) + 1):
                if font.measure(text[:index]) > budget:
                    break
                kept = index
            text = text[:kept].rstrip() + ellipsis
        if text != self._name.cget("text"):
            self._name.configure(text=text)

    def _build_temperature(self, parent) -> None:
        self._panel = ctk.CTkFrame(parent, fg_color=theme.INPUT_BG,
                                   corner_radius=theme.CTRL_RADIUS,
                                   border_width=1,
                                   height=theme.DASH_TEMP_H)
        self._panel.pack(fill="x", pady=(theme.DASH_ROW_GAP, 0))
        self._panel.pack_propagate(False)

        inner = ctk.CTkFrame(self._panel, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=8, pady=4)

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")

        ctk.CTkLabel(top, text=fonts.tracked("TEMP"), font=fonts.sans(7.5, 600),
                     text_color=theme.TEXT_LABEL, height=10,
                     anchor="w").pack(side="left")

        # The unit sits on the label line, not beside the value, so the value
        # can grow to three digits without shifting anything.
        self._unit = ctk.CTkLabel(top, text="°C", font=fonts.sans(8),
                                  text_color=theme.TEXT_FAINT, height=10)
        self._unit.pack(side="right")

        #: The HIGH chip. Built once and packed on demand -- an alert is a
        #: state change, not a hover, so reconciling geometry here is fine.
        self._badge = ctk.CTkLabel(top, text="HIGH", font=fonts.sans(7.5, 700),
                                   text_color=theme.BADGE_TEXT,
                                   fg_color=theme.BADGE_BG,
                                   corner_radius=theme.BADGE_RADIUS,
                                   height=11, width=30)
        self._badge_visible = False

        bottom = ctk.CTkFrame(inner, fg_color="transparent")
        bottom.pack(fill="x")

        self._value = ctk.CTkLabel(bottom, text=theme.TEMP_PLACEHOLDER,
                                   font=fonts.sans(17), height=20, anchor="w")
        self._value.pack(side="left")

        self._caption = ctk.CTkLabel(bottom, text="", font=fonts.sans(8),
                                     height=11)
        self._caption.pack(side="right", pady=(4, 0))

    def _build_actions(self, parent, on_clean, on_auto) -> None:
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.pack(fill="x", side="bottom")

        device_id = self.device.id
        self._clean = CleanButton(actions, self.device,
                                  lambda: on_clean(device_id),
                                  pad=theme.DASH_CTRL_PAD)
        self._clean.pack(side="left", fill="x", expand=True)

        self._auto = AutoButton(actions, self.device,
                                lambda: on_auto(device_id),
                                pad=theme.DASH_CTRL_PAD)
        self._auto.pack(side="left", fill="x", expand=True,
                        padx=(theme.DASH_ROW_GAP, 0))

    # -- painting ---------------------------------------------------------

    def render(self, animate: bool = True, force: bool = False) -> None:
        """Repaint from `self.device`, painting only what actually changed.

        `configure()` on a CustomTkinter widget redraws its rounded-rect
        canvas whether the value differs or not, so an unconditional repaint
        of this card measured ~44ms -- more than double the ~18ms its four
        sections cost individually. At the 100ms pump that put a ceiling of
        roughly two live devices on a view built to hold 25.

        So each painted value is compared against the last one written.
        Nothing is cached about the device: the comparison is against what is
        on screen, so swapping in a fresh `DeviceState` (as `sync` does) is
        safe and repaints exactly the fields that differ.

        `force=True` repaints unconditionally, for a view coming back from
        hidden where the screen may be stale.
        """
        if force:
            self._painted.clear()
        self._render_status()
        self._render_temperature()
        self._render_actions(animate)

    def _changed(self, key: str, value) -> bool:
        """True if `value` differs from what was last painted for `key`."""
        if self._painted.get(key, _UNSET) == value:
            return False
        self._painted[key] = value
        return True

    def _render_status(self) -> None:
        device = self.device
        visual = connection_visual(
            device.connection,
            attempt=device.reconnect_attempt,
            total_attempts=device.reconnect_total,
            error=device.last_error,
            has_ip=bool(device.ip),
        )
        style = theme.CONN[visual.style]

        # Both keys are tested before branching, because `_changed` records
        # as it compares. Testing "status" only inside the else would leave it
        # unrecorded whenever the style branch ran, so the very next repaint
        # would see a phantom change and redraw the label once for nothing.
        style_moved = self._changed("style", visual.style)
        wording_moved = self._changed("status", device.connection)

        if style_moved:
            self.configure(border_color=style["ring"])
            self._dot.configure(fg_color=style["dot"])
            # `visual.label` is the list view's *button* text -- an
            # instruction ("Connect", "Reconnect"). This is a readout, so it
            # needs the state instead, or a disconnected device would read as
            # an invitation.
            self._status.configure(text=status_text(device.connection),
                                   text_color=style["dot"])
        elif wording_moved:
            # Same treatment, different wording: CONNECTING and STOPPING both
            # map to "idle".
            self._status.configure(text=status_text(device.connection))

        if self._changed("name", device_name(device)):
            self._name_full = device_name(device)
            self._fit_name()

    def _render_temperature(self) -> None:
        device = self.device
        alert = device.temperature_alert
        temperature = device.latest_temperature

        text = (f"{temperature:.0f}°" if temperature is not None
                else theme.TEMP_PLACEHOLDER)
        colour = (theme.TEMP_ALERT_TEXT if alert
                  else theme.TEMP_OK_TEXT if temperature is not None
                  else theme.TEXT_GHOST)
        if self._changed("temp", (text, colour)):
            self._value.configure(text=text, text_color=colour)

        if self._changed("alert", alert):
            self._panel.configure(
                border_color=(theme.TEMP_ALERT_BORDER if alert
                              else theme.TEMP_OK_BORDER)
            )
            if alert:
                self._badge.pack(side="right", padx=(0, 6))
            else:
                self._badge.pack_forget()
            self._badge_visible = alert

        caption = (theme.DASH_TEMP_ALERT if alert
                   else theme.DASH_TEMP_NOMINAL if temperature is not None
                   else "")
        caption_colour = (theme.TEMP_ALERT_CAPTION if alert
                          else theme.TEXT_FAINT)
        if self._changed("caption", (caption, caption_colour)):
            self._caption.configure(text=caption, text_color=caption_colour)

    def _render_actions(self, animate: bool) -> None:
        """Delegate to Clean and Auto only when their own state moved.

        Both re-`configure()` their frame and label on every call (~5.4ms and
        ~5.8ms), and both read `DeviceState` live, so skipping the call when
        the flag they render has not moved is equivalent and free. Their own
        internals are shared with the List view's card and are left alone.
        """
        if self._changed("clean", self.device.clean_active):
            self._clean.render()
        if self._changed("auto", self.device.auto_enabled):
            self._auto.render(animate=animate)


def device_name(device: DeviceState) -> str:
    """The card's name text, falling back for an unnamed device."""
    return device.name or "Unnamed"


#: Status readout wording. `connection_visual` supplies the colour, but its
#: labels are the list view's button actions, so the words come from here.
_STATUS_TEXT = {
    ConnectionState.DISCONNECTED: "Disconnected",
    ConnectionState.CONNECTING: "Connecting…",
    ConnectionState.CONNECTED: "Connected",
    ConnectionState.RECONNECTING: "Reconnecting…",
    ConnectionState.ERROR: "Error",
    ConnectionState.STOPPING: "Stopping…",
}


def status_text(state: ConnectionState) -> str:
    return _STATUS_TEXT.get(state, "Disconnected")


class DashboardView(ctk.CTkFrame):
    """A scrollable 5-column grid of `DashboardCard`.

    Columns are uniform and share the width; rows take their height from the
    card. That is what leaves empty space beneath a short device list instead
    of stretching three cards over the whole viewport.

    The grid scrolls rather than capping the device count. 25 is the number
    that should fit on one screen, not a maximum -- `theme.SCROLL_THRESHOLD`
    already records that the export's 25-device limit was a demo constraint.

    Cards are built on demand: `App` should construct this view the first time
    the dashboard is selected, so a session that never leaves the list view
    pays nothing for it.
    """

    #: Identifies this view to the host, and labels it in the host's
    #: per-view exception log.
    name = "dashboard"

    @property
    def heading(self) -> str:
        """The sub-header heading for this view. Raw text, not tracked.

        Letter-spacing is applied at the render site -- `shell/subheader.py`
        wraps the heading in `fonts.tracked(..., 0.12)` -- so this returns the
        plain string, the same division as `caption`: the view owns the words,
        the shell owns how they look.

        **A property rather than an annotated class attribute**, and that is
        not cosmetic: `DeviceView` declares `name: str` as an annotation only,
        and annotations appear in `__annotations__` rather than in `vars()`,
        so the derived conformance test cannot see them. It enforces methods
        by signature and properties by presence; an annotated attribute gets
        no enforcement at all. A property is checked.

        Landed here before `DeviceView` declares it, deliberately. The
        conformance test derives what it demands from the contract, so adding
        it to the contract first would make the test require a member this
        class lacks -- correctly red, but red between two commits for no
        reason. Having it early is inert; missing it is a failure.
        """
        return theme.DASH_SUBHEAD

    @property
    def padding(self) -> dict:
        """`padx`/`pady` the host applies when packing `widget`.

        A **mapping**, not a tuple: the host packs with
        `view.widget.pack(..., **view.padding)` (`views/host.py:112`), so a
        two-tuple raises `TypeError: argument after ** must be a mapping`.
        This was a tuple until the seam landed, which would have failed the
        mount at the pack call rather than anywhere informative.

        The value is the view's design decision and the host applies it:
        `ListView` wants `PAGE_PAD_X - 8` so its scrollbar sits in the page
        margin, this grid wants the full pad.
        """
        return {"padx": theme.PAGE_PAD_X, "pady": theme.PAGE_PAD_Y}

    def __init__(self, master, ctx=None, *,
                 manager: DeviceManager | None = None,
                 devices: Sequence[DeviceState] | None = None,
                 on_clean: Callable[[str], None],
                 on_auto: Callable[[str], None],
                 is_shutting_down: Callable[[], bool] | None = None):
        super().__init__(master, fg_color="transparent")
        # `ctx` is what the host owes a view: `is_shutting_down`, `after` and
        # `after_cancel`. A view built without a host -- tests, the capture
        # and diagnostic scripts -- gets a shim over its own widget, so the
        # class stays usable standalone. `is_shutting_down` is also accepted
        # directly for the same reason; an explicit one wins over ctx.
        self._ctx = ctx if ctx is not None else _StandaloneCtx(self)
        if is_shutting_down is not None:
            self._is_shutting_down = is_shutting_down
        else:
            self._is_shutting_down = self._ctx.is_shutting_down

        # The device list is the host's to supply via `set_devices`. A manager
        # may be passed instead, which seeds the list once and is not
        # consulted again -- so there is exactly one source of truth for what
        # this view believes exists.
        self._devices: list[DeviceState] = (
            list(devices) if devices is not None
            else list(manager.devices) if manager is not None
            else []
        )
        self._on_clean = on_clean
        self._on_auto = on_auto
        # Constructed means mounted: the host builds this view lazily, on the
        # first switch to it, so it is on screen by the time it exists.
        # `hide()` is what takes it off.
        self._visible = True
        self._stale = False
        self._cards: dict[str, DashboardCard] = {}
        self._order: list[str] = []
        self._pending: list[DeviceState] = []
        self._build_job: str | None = None

        self._grid = ctk.CTkScrollableFrame(
            self, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=theme.SCROLL_THUMB,
            scrollbar_button_hover_color=theme.SCROLL_THUMB_HOVER,
            scrollbar_fg_color="transparent",
        )
        self._grid.pack(fill="both", expand=True)
        try:
            self._grid._scrollbar.configure(width=8)
        except AttributeError:
            # `_scrollbar` is CustomTkinter's private attribute. If a future
            # version renames it the scrollbar keeps its default width, which
            # is cosmetic -- but it must not take the view down with it.
            # Narrowed from `except Exception`, and the `# pragma: no cover`
            # is gone with it: the branch is provokable by deleting the
            # attribute, so it is tested rather than annotated away.
            #
            # IT LOGS, and with `exc_info`, because the thing it contains is a
            # BROKEN ASSUMPTION ABOUT A DEPENDENCY rather than an expected
            # runtime condition. Silence here means a CustomTkinter upgrade
            # quietly widens the scrollbar and nothing ever says why. Carrying
            # the traceback puts the site in the containment fixture's view --
            # `TRACKED` rather than `HANDLER_UNDECLARED` -- so it is counted
            # by the thing that counts containment, which is the whole reason
            # the tier exists.
            logger.warning(
                "dashboard: could not narrow the scrollbar -- "
                "CTkScrollableFrame._scrollbar is missing, so it keeps its "
                "default width. Cosmetic; likely a CustomTkinter rename.",
                exc_info=True,
            )
            pass

        for column in range(theme.DASH_COLS):
            self._grid.grid_columnconfigure(column, weight=1,
                                            uniform="dashboard")

        self.sync()

    # -- reconciliation ---------------------------------------------------

    def device_fingerprint(self) -> tuple[tuple[str, str], ...]:
        """The (id, name) pairs this view holds, in display order.

        `ViewHost.show` compares this against its own record and reseeds only
        on a difference, which replaces the `_dirty` flag and the obligation
        to call `note_device_set_changed`. Order is significant: `sync` sets
        `_order` from `_devices`, so a reorder is a real display change.

        **(id, name), not ids alone.** The name is rendered (`device_name`
        above), so a rename that changed nothing else would leave a stale card
        under an id-only fingerprint. It is cheap to include because names
        change rarely -- unlike connection or temperature, which would reseed
        a busy fleet on every poll.

        **The residual, stated because it is a judgement and not a proof:**
        field selection is a choice, and any rendered field left out of this
        tuple can go stale. What improves over the flag it replaces is not
        that staleness becomes impossible -- it is that the failure is
        uniform across views and attaches to the *field* omitted, rather than
        to whichever view nobody remembered to mark dirty.
        """
        return tuple((d.id, d.name) for d in self._devices)

    def set_devices(self, devices: Sequence[DeviceState]) -> None:
        """Replace the device list and reconcile the grid.

        The host's bulk path, used on a dirty show. `sync` does the work; this
        is the contract's name for it.
        """
        self._devices = list(devices)
        self.sync()

    def add_device(self, device: DeviceState) -> None:
        """The host's incremental path for one new device.

        Appending rather than re-listing matters: a new device does not move
        any existing card, so nothing already on screen needs re-gridding.
        The host follows this with `on_device_count_changed`, which is what
        re-flows and trims.
        """
        if any(d.id == device.id for d in self._devices):
            return
        self._devices.append(device)
        if self._visible:
            self.sync()
        else:
            self._stale = True

    def remove_device(self, device_id: str) -> None:
        """The host's incremental path for one removal."""
        self._devices = [d for d in self._devices if d.id != device_id]
        card = self._cards.pop(device_id, None)
        if card is not None:
            card.destroy()
        if self._visible:
            self.sync()
        else:
            self._stale = True

    def sync(self) -> None:
        """Bring the grid in line with the device list this view holds.

        Cards for devices that are still present are kept -- re-gridded if
        their position moved, never rebuilt -- so adding a device does not
        flash the other 24. Missing cards are built a row at a time: the
        first `theme.DASH_INITIAL_CARDS` before returning, the rest on a
        timer, so switching to a 25-device dashboard paints at once instead
        of blocking for ~2.5s.
        """
        devices = self._devices
        wanted = [device.id for device in devices]

        for device_id in list(self._cards):
            if device_id not in wanted:
                self._cards.pop(device_id).destroy()

        self._order = wanted
        self._pending = [d for d in devices if d.id not in self._cards]

        for index, device in enumerate(devices):
            card = self._cards.get(device.id)
            if card is None:
                continue
            # The card holds a reference to the DeviceState; the manager
            # replaces that object on reload, so refresh it.
            card.device = device
            card.render(animate=False)
            self._place(card, index)

        for _ in range(theme.DASH_INITIAL_CARDS):
            if not self._build_next():
                break
        self._schedule_build()
        self._trim_rows(len(devices))

    def _build_next(self) -> bool:
        """Build one queued card. Returns False when the queue is empty."""
        if not self._pending:
            return False
        device = self._pending.pop(0)
        card = DashboardCard(self._grid, device, on_clean=self._on_clean,
                             on_auto=self._on_auto)
        self._cards[device.id] = card
        if device.id in self._order:
            self._place(card, self._order.index(device.id))
        return True

    def _schedule_build(self) -> None:
        if self._build_job is not None or not self._pending:
            return
        self._build_job = self._ctx.after(theme.DASH_BUILD_INTERVAL_MS,
                                          self._build_remaining)

    def _build_remaining(self) -> None:
        self._build_job = None
        if self._is_shutting_down():
            return
        if not self._visible:
            # `hide()` cancels the job, but one already queued when hide
            # landed still fires. Without this it would build one more card
            # behind an unmapped view -- and then re-schedule, which would
            # defeat the cancellation entirely.
            return
        if not self.winfo_exists():
            return
        if self._build_next():
            self._schedule_build()
            if not self._pending:
                # Row trimming forces a full grid relayout, so it runs once
                # the queue drains rather than after every streamed card.
                # Doing it per card added ~25 needless relayouts to a build
                # that is already layout-bound.
                self._trim_rows(len(self._order))

    def build_now(self) -> None:
        """Finish the queue synchronously.

        For tests and for anything that needs every card to exist before it
        measures the grid. Normal use should let `sync` stream them.
        """
        self._cancel_build()
        while self._build_next():
            pass
        self._trim_rows(len(self._order))

    def _cancel_build(self) -> None:
        if self._build_job is not None:
            # No handler, deliberately. `after_cancel` raises nothing --
            # measured against an already-fired job, an already-cancelled one,
            # a bogus id, a destroyed widget and a destroyed root. tkinter's
            # own `Misc.after_cancel` wraps its `after info` lookup in
            # `except TclError: pass` and Tcl's `after cancel` is a no-op for
            # an unknown id, so the handler that stood here was wrapped around
            # a callee that had already swallowed the exception it was written
            # to catch. Its one reachable raise is `ValueError` on a falsy id,
            # which the `is not None` above prevents. The comment it carried,
            # "already fired", named precisely the case that does not raise.
            self._ctx.after_cancel(self._build_job)
            self._build_job = None

    def shutdown(self) -> None:
        """The host's terminal call.

        Distinct from `destroy()` on purpose: Tk invokes `destroy` itself
        during parent teardown, so a contract method of that name would be
        ambiguous about who called it and why. This is the host saying it is
        finished with the view; `destroy` below stays as the safety net for
        when Tk tears the widget down without anyone asking.
        """
        self._cancel_build()
        self._visible = False

    def destroy(self) -> None:
        self._cancel_build()
        super().destroy()

    def _place(self, card: DashboardCard, index: int) -> None:
        row, column = divmod(index, theme.DASH_COLS)
        card.grid(
            row=row, column=column, sticky="new",
            padx=(0 if column == 0 else theme.DASH_GAP // 2,
                  0 if column == theme.DASH_COLS - 1 else theme.DASH_GAP // 2),
            pady=(0 if row == 0 else theme.DASH_GAP, 0),
        )

    def _trim_rows(self, count: int) -> None:
        """Drop row configuration for rows that no longer hold a card.

        Without this a grid that once held 25 devices keeps five configured
        rows after a removal, and `grid_propagate` reserves their height --
        leaving a gap under the last card.
        """
        used = -(-count // theme.DASH_COLS)  # ceil
        for row in range(used, used + theme.DASH_COLS + 1):
            try:
                self._grid.grid_rowconfigure(row, minsize=0, weight=0)
            except tkinter.TclError:
                # The grid can be destroyed between a removal and this reset;
                # Tk then reports `bad window path name`. Narrowed from
                # `except Exception` and tested, so the `# pragma: no cover`
                # is gone.
                #
                # DELIBERATELY SILENT, and the sibling handler in `__init__`
                # deliberately is not. The audit item that reached both asked
                # for four sites to log; two of the four had already been
                # narrowed or deleted, and of the two left these want OPPOSITE
                # dispositions. The difference is what the exception MEANS:
                #
                #   __init__   a dependency's private attribute vanished --
                #              an assumption broke, nobody knows, log it
                #   here       a view was destroyed mid-teardown while a
                #              removal was in flight -- ORDINARY, EXPECTED,
                #              and it happens on every close
                #
                # Logging this one would emit a WARNING on normal shutdown,
                # which trains a reader to ignore the channel. A handler is
                # not a defect merely because it is quiet; batch-converting
                # the quiet ones is how a log becomes noise.
                pass

    # -- painting ---------------------------------------------------------

    def render_device(self, device_id: str) -> bool:
        """Repaint one card.

        Returns False when this view did not handle the change -- either it
        has no card for `device_id`, or it is hidden and has deferred the
        repaint until `show`. The host can use that to stop delivering
        changes to a view that is not on screen.
        """
        if not self._visible:
            # Declining while hidden is the point: a hidden 25-card grid
            # would otherwise repaint on every pump for nobody to see.
            self._stale = True
            return False
        card = self._cards.get(device_id)
        if card is None:
            return False
        card.render()
        return True

    def render_all(self, force: bool = False) -> None:
        for card in self._cards.values():
            card.render(animate=False, force=force)

    # -- view-host lifecycle ----------------------------------------------

    def show(self) -> None:
        """Called by the host after mounting this view.

        Reconciles first: the device set may have changed while hidden, and
        any repaint declined during that time left the screen stale.
        """
        self._visible = True
        self.sync()
        if self._stale:
            self.render_all(force=True)
            self._stale = False

    def hide(self) -> None:
        """Called by the host before unmapping this view.

        Releases the streamed build. Per the view contract, anything on a
        timer stops when a view goes off screen. The Dashboard has no
        animation to release -- the status pulse was removed app-wide on
        2026-09-10, and this view never had one -- but a build in progress
        is the same kind of waste: each card costs tens of milliseconds to
        construct, spent on a grid nobody is looking at and competing with
        whichever view *is* on screen.

        `_pending` is left alone. `show()` calls `sync()`, which recomputes
        the queue from `_devices` and re-schedules, so an interrupted build
        resumes without any state kept here.
        """
        self._visible = False
        self._cancel_build()

    def on_device_count_changed(self, total: int) -> None:
        """A device was added or removed.

        Not a no-op for this view: the grid has to re-flow, because removing
        a device shifts every later card's cell. `sync` keeps the survivors
        and only rebuilds what is genuinely new.
        """
        if self._visible:
            self.sync()
        else:
            self._stale = True

    # -- introspection ----------------------------------------------------

    @property
    def widget(self):
        """What the host packs. This view *is* its own widget."""
        return self

    @property
    def cards(self) -> dict[str, DashboardCard]:
        return self._cards

    @property
    def slots(self) -> int:
        """Cells on one screenful of grid -- 25 for a 5x5."""
        return theme.DASH_COLS * theme.DASH_COLS

    def caption(self, total: int) -> str:
        """Sub-header caption: '3 devices - 1 connected - 3 of 25 slots'.

        **`total` is accepted and deliberately ignored.** The host passes
        `len(manager.devices)`, which is authoritative -- but this line
        renders *two* numbers and the contract carries no connected count, so
        `connected` can only come from `self._devices`. Taking the total from
        the host and the connected count from here would put the two halves
        on different sources, which is precisely the defect fixed below:
        "3 devices - 4 connected" is reachable the moment they disagree.
        Authoritative for one half is worse than consistent across both, so
        both come from `_devices` and the parameter is unused.

        If host-authoritative counts are ever wanted, the contract needs
        `caption(self, total, connected)` -- then both halves come from the
        shell and the inconsistency is impossible by construction. That is a
        contract change, not something to fake from here.

        Both counts come from `_devices`. Taking the total from `_order`
        instead -- which is only rewritten inside `sync()` -- meant a device
        added while this view was hidden was counted by one half of the
        sentence and not the other, because `add_device` deliberately does
        not sync when hidden. That produced "3 devices - 4 connected - 3 of
        25 slots": no exception, no crash, just a number that looks like a
        number. Unreachable through the host's documented call order, but
        `caption()` is a required contract member now, and "the host happens
        never to ask while hidden" is not a property this method should
        depend on.
        """
        total = len(self._devices)
        connected = sum(1 for d in self._devices if d.is_connected)
        return (f"{total} device{'' if total == 1 else 's'} · "
                f"{connected} connected · {total} of {self.slots} slots")
