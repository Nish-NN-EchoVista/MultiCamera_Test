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

    def __init__(self, parent, ctx, *, on_shown=None) -> None:
        self._parent = parent
        self._ctx = ctx
        #: Called after every successful `show`, including the first.
        #:
        #: The shell owns the heading, the caption and the counts, and
        #: composes all three from the **active** view -- so every path that
        #: changes which view is active has to refresh them. Holding that
        #: here rather than at the call sites is deliberate: it was held at a
        #: call site once, and `385e718` -- the commit titled "fix the chrome
        #: not following the view" -- fixed one of the two sites and left the
        #: boot path at `_build_views` untouched, 70 lines from the docstring
        #: explaining why the refresh "is not optional". A second call site
        #: would be the same fix a third time, and the next one is added by
        #: someone who has not read either docstring.
        self._on_shown = on_shown or (lambda: None)
        self._factories: dict[str, Callable] = {}
        self._views: dict[str, object] = {}
        self._active_name: str | None = None
        #: Views whose device set is behind. A dirty view receives nothing
        #: until it is shown, at which point the host re-sends `set_devices`.
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

        self._active_name = name
        view.widget.pack(fill="both", expand=True, **view.padding)

        # Ask the view what it holds; re-seed only if it differs from the
        # authoritative set. No flag, and no caller obliged to announce a
        # change -- which is what F11 was about. A fresh view reports an
        # empty fingerprint and is therefore seeded on its first `show`
        # without a special case.
        #
        # `_call` so a view that raises here is contained like any other
        # routed call. On containment it returns `None`, which differs from
        # any real fingerprint, so the view is re-seeded -- the safe
        # direction.
        if self._call(name, "device_fingerprint") != tuple(
                (device.id, device.name) for device in devices):
            self._call(name, "set_devices", devices)
        self._call(name, "show")

        # Deliberately NOT through `_call`. Containment exists because the
        # pump delivers to views that are not active -- a faulty Dashboard
        # must not freeze the List. This is the shell's own callback, so
        # there is no foreign code to isolate, and the chrome path is the one
        # that surfaced a missing attribute during slice 2 while the same
        # fault inside containment was invisible. Keep it loud.
        self._on_shown()

    def _hide_active(self) -> None:
        name = self._active_name
        if name is None:
            return
        view = self._views.get(name)
        self._call(name, "hide")
        if view is not None:
            view.widget.pack_forget()
        # Deliberately NOT dirtied here.
        #
        # Dirtying on hide made every switch back a full `set_devices`, and
        # for `ListView` that is a teardown and progressive rebuild of every
        # card. Measured with the fleet idle and nothing whatsoever changed:
        # the switch to List blocked for **3596-3626ms at 25 devices** and
        # **790-928ms at four**, against 79-86ms and 6-15ms after this change.
        #
        # Settling time is deliberately **not** quoted. It was not reliably
        # measurable: the harness pumps `update()` in a loop, and at 25
        # devices each pump is slow enough that the loop's own cost dominates
        # -- it overran a 3s budget to 5.3s, which only the instrument can
        # explain. An earlier version of this comment cited "10s of settling"
        # and that figure is **withdrawn**; anyone wanting it must measure it
        # some other way. The
        # comment this replaces described the intent -- "anything arriving
        # while hidden marks it dirty" -- while the code dirtied regardless
        # of whether anything had arrived.
        #
        # There is no dirty flag any more either. `show` asks the view for
        # its `device_fingerprint()` and re-seeds only if it differs from the
        # authoritative set, so nothing needs marking and no caller needs to
        # announce a change. Device *state* -- connect, temperature, fault --
        # is outside the fingerprint and reaches only the active view via
        # `render_device`, so a merely busy fleet still forces no rebuild.
        #
        # An earlier version of this comment pointed at
        # `note_device_set_changed`, which F11 deleted. Deleting a method
        # strands every comment that named it, and a comment naming a
        # mechanism is the last thing a reader checks.
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
