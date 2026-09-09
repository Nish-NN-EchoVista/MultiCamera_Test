"""The event pump: drain backend events and apply them to the UI.

Relocated from `app.py`'s "Event pump" section, with one deliberate change --
**fault announcement is separated from card rendering.**

Before this, `_render_device` repainted a card *and* popped an error dialog as
a side effect, so there was no way to repaint without also announcing. The two
are now separate functions and callers compose them:

    render_device        repaint one card, and re-align it if its shape moved
    announce_faults      pop a dialog for a final failure, coalesced
    apply_device_change  both, in that order -- what an event actually means

**The composition is preserved at both entry points on purpose.** The pump
calls `apply_device_change`, and so does `App._on_device_changed`, which is
what `DeviceManager.on_device_changed` is wired to. Removing announcement from
the manager's callback would silently stop timer-driven fault dialogs, and
**no test pins that coupling** -- checked, the only test touching
`_render_device` asserts caption text -- so nothing would have caught it. The
separation is in the code's shape, not in what reaches the operator.

`App._render_device` now renders and does not announce, which is what its name
says. That is the behavioural half of this slice, and it is declared rather
than discovered.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..backend.device_manager import DeviceState
from ..backend.events import ConnectionState

if TYPE_CHECKING:
    from ..app import App

# Deliberately "emcc.app" and not `__name__`: these lines logged under that
# name before the extraction, and moving code should not change what appears
# in logs/emcc.log.
logger = logging.getLogger("emcc.app")

#: Event drain interval. 10 Hz keeps the UI responsive without spinning: at 25
#: devices the backend produces ~25 events/s (temperature is throttled to 1 Hz
#: per device), so a tick typically has two or three events to apply.
PUMP_INTERVAL_MS = 100


def start_pump(app: "App") -> None:
    app._pump_job = app.after(PUMP_INTERVAL_MS, app._pump)


def pump(app: "App") -> None:
    """Drain backend events and re-render only the cards that changed."""
    app._pump_job = None
    # Also guard on the widget still existing: destroy() without a prior
    # shutdown() (a harness, or an unexpected teardown) leaves this timer
    # pending, and Tk then reports `invalid command name "..._pump"`.
    if app._shutting_down or not app.winfo_exists():
        return
    try:
        changed = app.manager.drain_events()
        for device_id in changed:
            apply_device_change(app, device_id)
        if changed:
            app._refresh_chrome()
    except Exception:
        # The pump must survive anything, or the UI stops updating.
        #
        # This is one of three containment sites in the codebase and it
        # predates the modularisation. It keeps the UI alive, and it also
        # means a failure in here leaves the suite green -- which is what the
        # `_no_contained_exceptions` fixture in conftest.py exists to catch.
        logger.exception("event pump iteration failed")
    finally:
        if not app._shutting_down:
            start_pump(app)


def apply_device_change(app: "App", device_id: str) -> None:
    """What a device event means: repaint the card, then announce any fault.

    The composed operation, so the two concerns stay separable while every
    real event still does both. Order matters only in that a dialog should
    not appear over a card showing stale state.
    """
    render_device(app, device_id)
    device = app.manager.get(device_id)
    if device is not None:
        announce_faults(app, device)


def render_device(app: "App", device_id: str) -> None:
    """Repaint one card, re-aligning it if its shape changed.

    Does **not** announce faults -- see the module docstring. Silently returns
    for an unknown id, which is the removed-while-an-event-was-in-flight case.
    """
    card = app._cards.get(device_id)
    device = app.manager.get(device_id)
    if card is None or device is None:
        return   # removed while an event was in flight

    card.render()

    # A card that gains or loses the "Exceeds threshold" caption changes
    # its tallest column, so its label baseline moves.
    offset = app._offsets.get(card.variant)
    if offset is None:
        # First card of this shape: measuring needs a layout flush, which
        # measured 208ms inside a pump tick. Deferred to the next idle slot
        # so the tick stays fast -- the card renders one frame at the old
        # baseline, which is imperceptible, and every later card of this
        # shape uses the cached value.
        variant = card.variant
        if variant not in app._measuring:
            app._measuring.add(variant)
            app.after_idle(lambda: app._measure_variant(device_id, variant))
    elif card._label_offset != offset:
        for section in card._sections:
            section.set_top(offset)
        card._label_offset = offset


def measure_variant(app: "App", device_id: str, variant: bool) -> None:
    """Measure and cache the label offset for a card shape, once.

    Runs off the pump tick (see `render_device`). Guarded on the card still
    existing, because a device can be removed between scheduling and firing.
    """
    app._measuring.discard(variant)
    if app._shutting_down:
        return
    card = app._cards.get(device_id)
    if card is None or not card.winfo_exists():
        return
    app.update_idletasks()
    offset = card.align_labels()
    if offset is not None:
        app._offsets[card.variant] = offset


def announce_faults(app: "App", device: DeviceState) -> None:
    """Pop a dialog only for faults worth interrupting the operator for.

    Nothing pops for an unexpected disconnect or a retry -- the card shows
    those. Only a final failure does, coalesced across devices.
    """
    if app._shutting_down or device.connection is not ConnectionState.ERROR:
        device.fault_announced = False
        return
    if device.fault_announced:
        return
    device.fault_announced = True
    app._errors.report(device.name, device.ip,
                       device.last_error or "Connection failed")
