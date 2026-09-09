"""Registration, switching and render routing for device views.

**Contributes no widget of its own.** The host holds a parent reference and
packs `view.widget` directly into it. Inserting a frame between `App` and the
views would change every device-card pathname and light up the structural
snapshot for a reason unrelated to whatever was actually being changed. It
also keeps the host simple: nothing to size, nothing to configure, no geometry
of its own to get wrong.

Views are constructed lazily, on first show, from a registered factory --
`register` builds nothing.

See `VIEW_CONTRACT.md` for the call order and the two hard requirements
implemented here: per-view exception containment, and `False` from
`render_device` never counting as a failure.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

from ..backend.device_manager import DeviceState

logger = logging.getLogger("emcc.ui")

#: Consecutive raising calls before a view is abandoned. The pump is the one
#: path by which network activity reaches the UI, and a view is third-party
#: code from the shell's point of view: a permanently-raising view degrading
#: to "not updating" beats it freezing the view the operator is looking at.
BROKEN_THRESHOLD = 3


class ViewHost:
    """Owns which view is showing and what reaches it."""

    def __init__(self, parent, ctx) -> None:
        self._parent = parent
        self._ctx = ctx
        self._factories: dict[str, Callable] = {}
        self._views: dict[str, object] = {}
        self._active_name: str | None = None
        #: Views whose device set is behind. A dirty view receives nothing
        #: until it is shown, at which point the host re-sends `set_devices`.
        self._dirty: set[str] = set()
        self._failures: dict[str, int] = {}
        self._broken: set[str] = set()
        #: Every contained failure, cumulatively. **Never cleared, not even by
        #: `shutdown`** -- a teardown failure is exactly the kind worth
        #: recording.
        #:
        #: Separate from `_failures` because that is a *consecutive* streak, by
        #: design, since `BROKEN_THRESHOLD` needs it to be. Any success resets
        #: it, so it answers "is this view raising right now?" and cannot
        #: answer "did anything raise at all?". Measured: a failure in
        #: `set_devices` followed by a successful `show()` leaves it at zero,
        #: which is how a construction failure passed an assertion written to
        #: catch exactly that.
        #:
        #: `repr()` rather than the traceback: `logger.exception` already has
        #: the traceback, so the log stays the deep source and this stays the
        #: oracle.
        self._incidents: list[tuple[str, str, str]] = []

    # -- registration ----------------------------------------------------

    def register(self, name: str, factory: Callable) -> None:
        """Record a factory. Nothing is constructed and no widget is built."""
        self._factories[name] = factory

    @property
    def names(self) -> list[str]:
        return list(self._factories)

    @property
    def active(self):
        """The showing view, or `None` before the first `show`."""
        if self._active_name is None:
            return None
        return self._views.get(self._active_name)

    @property
    def active_name(self) -> str | None:
        return self._active_name

    def constructed(self, name: str) -> bool:
        return name in self._views

    # -- switching -------------------------------------------------------

    def show(self, name: str, devices: Sequence[DeviceState]) -> None:
        """Make `name` the active view, constructing it if this is its first.

        Order matters and matches the contract: pack, `set_devices`, `show`.
        A view that was hidden and has gone dirty is re-sent the device set
        before being shown, and `show()` reconciles as well -- belt and braces
        against a device set that moved while it was away.
        """
        if name not in self._factories:
            raise KeyError(f"no view registered as {name!r}")

        if self._active_name is not None and self._active_name != name:
            self._hide_active()

        view = self._views.get(name)
        if view is None:
            view = self._factories[name](self._parent, self._ctx)
            self._views[name] = view
            self._dirty.add(name)

        self._active_name = name
        view.widget.pack(fill="both", expand=True, **view.padding)

        if name in self._dirty:
            self._call(name, "set_devices", devices)
            self._dirty.discard(name)
        self._call(name, "show")

    def _hide_active(self) -> None:
        name = self._active_name
        if name is None:
            return
        view = self._views.get(name)
        self._call(name, "hide")
        if view is not None:
            view.widget.pack_forget()
        # Anything arriving while hidden marks it dirty rather than being
        # delivered, so it reconciles on the way back in.
        self._dirty.add(name)
        self._active_name = None

    # -- routing ---------------------------------------------------------

    def render_device(self, device_id: str) -> bool:
        """Repaint one device in the active view only.

        A `False` return is passed straight through and is **not** a failure:
        it means the view did not handle that id. Only a raised exception
        counts towards the broken threshold -- if `False` counted, a hidden
        view returning `False` on every pump by design would be marked broken
        for behaving exactly as specified, and marked broken while hidden, so
        nobody would see it until they switched to it.
        """
        name = self._active_name
        if name is None:
            return False
        return bool(self._call(name, "render_device", device_id))

    def add_device(self, device: DeviceState, total: int) -> None:
        """A device appeared: tell every clean view, then the new count."""
        for name in list(self._views):
            if name in self._dirty:
                continue
            self._call(name, "add_device", device)
            self._call(name, "on_device_count_changed", total)

    def remove_device(self, device_id: str, total: int) -> None:
        """A device went away: tell every clean view, then the new count."""
        for name in list(self._views):
            if name in self._dirty:
                continue
            self._call(name, "remove_device", device_id)
            self._call(name, "on_device_count_changed", total)

    def set_devices(self, devices: Sequence[DeviceState]) -> None:
        """Re-seed the active view; mark the rest dirty rather than updating."""
        for name in list(self._views):
            if name == self._active_name:
                self._call(name, "set_devices", devices)
            else:
                self._dirty.add(name)

    # -- lifecycle -------------------------------------------------------

    def shutdown(self) -> None:
        """Shut down every view that was actually constructed."""
        for name in list(self._views):
            view = self._views[name]
            try:
                view.shutdown()
            except Exception as exc:
                self._incidents.append((name, "shutdown", repr(exc)))
                logger.exception("view %r raised during shutdown", name)
        self._active_name = None

    # -- containment -----------------------------------------------------

    def _call(self, name: str, method: str, *args):
        """Call one view method, containing whatever it raises.

        Contained per view rather than per pump: the pump must stay scheduled,
        because it is the single path by which network activity reaches the
        UI. After `BROKEN_THRESHOLD` consecutive raises the view is abandoned
        and no longer called at all.
        """
        if name in self._broken:
            return None
        view = self._views.get(name)
        if view is None:
            return None
        try:
            result = getattr(view, method)(*args)
        except Exception as exc:
            self._incidents.append((name, method, repr(exc)))
            self._failures[name] = self._failures.get(name, 0) + 1
            count = self._failures[name]
            logger.exception("view %r raised in %s (%d consecutive)",
                             name, method, count)
            if count >= BROKEN_THRESHOLD:
                logger.error("view %r marked broken after %d consecutive "
                             "failures; it will no longer be called",
                             name, count)
                self._broken.add(name)
            return None
        # Only a raise resets nothing -- a successful call clears the streak,
        # including a legitimate `False` from render_device.
        self._failures[name] = 0
        return result

    def is_broken(self, name: str) -> bool:
        return name in self._broken

    @property
    def incidents(self) -> tuple[tuple[str, str, str], ...]:
        """Every contained failure since construction, oldest first.

        Read-only and public, so verification asserts on a supported surface
        rather than reaching into `_incidents`. Empty is the only acceptable
        value at the end of a slice: containment keeps the UI alive at
        runtime, which is right, and this is what stops it also keeping a bug
        invisible.
        """
        return tuple(self._incidents)
