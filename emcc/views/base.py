"""What a device view must provide, and the shim it gets without a host.

`DeviceView` is a `typing.Protocol`, so a view need not inherit from it --
the host accepts any structurally-matching object. That is deliberate: the
two real views are different shapes (the Dashboard is a `CTkFrame`,
`ListView` owns one), and the contract declares `widget` rather than assuming
the view is a widget.

**A Protocol supplies defaults only to classes that inherit from it.** Since
the host accepts structural matches, an omitted method raises `AttributeError`
inside the event pump rather than quietly doing nothing -- which is why
`on_device_count_changed` is required here and not a defaulted no-op, and why
the host carries no `hasattr` guard for it. A silently absent count handler is
how a view ends up showing a stale count nobody notices.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from ..backend.device_manager import DeviceState


class StandaloneCtx:
    """`ctx` for a view built without a host: tests, capture and diagnostics.

    Falls back to the view's own widget for timers and reports that nothing is
    shutting down. Deliberately not something the host can hand out by
    accident -- it exists so a view stays constructible on its own.

    `widgets/dashboard_view.py` carries its own equivalent, written before
    this module existed. Unifying them means editing that file, which is
    Worker 2's lane and a change with no behavioural payoff, so the
    duplication is left in place and noted rather than tidied across a lane
    boundary.
    """

    def __init__(self, widget) -> None:
        self._widget = widget

    def is_shutting_down(self) -> bool:
        return False

    def after(self, ms: int, fn):
        return self._widget.after(ms, fn)

    def after_cancel(self, job) -> None:
        self._widget.after_cancel(job)


@runtime_checkable
class DeviceView(Protocol):
    """The surface the host routes through. See `VIEW_CONTRACT.md`."""


    @property
    def widget(self):
        """The widget the host packs. May be the view itself."""

    @property
    def padding(self) -> dict:
        """`padx`/`pady` the host applies when packing `widget`."""

    @property
    def heading(self) -> str:
        """The sub-header heading for this view. **Raw text, not tracked.**

        Letter-spacing is applied at the render site --
        `shell/subheader.py` wraps this in `fonts.tracked(..., 0.12)` -- so
        return the plain words. A view that pre-tracked its own heading would
        be tracked twice and render with doubled hair spaces.

        Same division as `caption`: the view owns the words, the shell owns
        how they look.

        **A property, not an annotated class attribute**, and the difference
        is enforcement rather than style. `name` is declared as an annotation
        (`name: str`), and annotations live in `__annotations__` rather than
        in `vars()`, so the conformance test that derives its expectations
        from this class cannot see them -- it checks methods by signature and
        properties by presence, and an annotated attribute gets neither. So
        `name` is currently unenforced; a property is checked.
        """

    def caption(self, total: int) -> str:
        """The sub-header caption line for this view at `total` devices.

        Each view describes its own contents, so the wording belongs to the
        view rather than to the shell. The shell still composes the caption in
        slice 2; it starts asking the active view in slice 4.
        """

    def set_devices(self, devices: Sequence[DeviceState]) -> None:
        """Replace what the view believes exists."""

    def device_fingerprint(self) -> tuple[tuple[str, str], ...]:
        """The `(id, name)` pairs this view holds, in display order.

        The host compares this against the authoritative set on `show` and
        re-seeds only on a difference, so a view reports what it *has* rather
        than being told when it is behind.

        **Report what is displayed plus what is queued to be**, not a private
        record of the last `set_devices`. `ListView` builds progressively, so
        its built cards and its pending devices together are what it holds;
        either alone is wrong in one direction.

        FIELD SELECTION IS A JUDGEMENT and this is the one thing about the
        mechanism that can be wrong. `(id, name)` asserts a view is behind iff
        its id/name set differs, so a rendered field absent from the
        fingerprint goes stale. `name` is included because it is rendered and
        changes rarely; connection state and temperature are excluded because
        they reach the active view through `render_device` and including them
        would re-seed on every reading.

        The improvement over a dirty flag is not that this cannot be wrong --
        it is that it fails on the **field** omitted, uniformly across views,
        rather than on the **view** nobody marked dirty.
        """

    def show(self) -> None:
        """Become visible. Reconciles rather than merely repainting, so a
        device set that moved while hidden cannot leave stale cards."""

    def hide(self) -> None:
        """Stop rendering. The host unpacks `widget` separately."""

    def render_device(self, device_id: str) -> bool:
        """Repaint one device. `True` if handled.

        **`False` is not a failure.** It means "not handled by me" -- no card
        for that id, or hidden and deliberately deferring -- and a hidden view
        may return `False` on every pump by design. Only a raised exception
        counts against the host's broken-view threshold.
        """

    def add_device(self, device: DeviceState) -> None:
        """A device appeared."""

    def remove_device(self, device_id: str) -> None:
        """A device went away."""

    def on_device_count_changed(self, total: int) -> None:
        """The device count changed. Required, never a defaulted no-op."""

    def shutdown(self) -> None:
        """Terminal. Cancel timers and release resources.

        `show()` after `shutdown()` is undefined and stays that way: the host
        never re-shows a shut-down view, so a view need not defend against it.
        """
