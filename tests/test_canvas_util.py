"""`canvas_util.scaling()`: that it reports the tracker's factor, and that it
no longer substitutes a value when it cannot.

WHY THIS FILE EXISTS RATHER THAN A LINE IN test_ui_integration.py

`scaling()` used to be `except Exception: return 1.0`. Measured before the
change, `scaling(None)` returned `1.0` -- a plausible number from a nonsense
input. Consumers divide by the factor (`device_card.align_labels`) or multiply
by it (`add_device._render`), so a wrong 1.0 corrupts geometry silently rather
than failing: every section label mis-centres, or the Add Device row draws at
1/2.25 size.

WHY NOT `assert scaling(w) != 1.0`

That was the originally specified test and it cannot hold. The factor is
`window_dpi_scaling_dict[root] * widget_scaling`, and CustomTkinter's
`get_window_dpi_scaling` returns `(x_dpi + y_dpi) / 192` on Windows -- exactly
1.0 at 96 DPI -- and a literal `1` on macOS and Linux. So on *correct* code
that assertion fails on any 100%-scaling display and on every Linux machine.
It would have asserted a property of this monitor rather than of the code.

The question it was reaching for is *did the value come from the tracker or
from the handler?*, and that is answerable without reference to the display:
assert the mechanism, not the number.

WHICH OF THESE ACTUALLY DISCRIMINATE THIS CHANGE -- checked, not assumed

Each assertion below was run against HEAD's `canvas_util.py` (extracted from
git, not by mutating the working tree) to see whether the old code already
satisfied it:

    scaling(None) raises AttributeError          FAILS on old (returned 1.0)
    scaling("not a widget") raises AttributeError FAILS on old (returned 1.0)
    unregistered root -> 1.0                     PASSES on old
    destroyed widget -> tracked factor           PASSES on old

So **two of the four tests here do not guard this change at all**, and saying
so is the point. The old bare `except` returned 1.0 for the unregistered-root
case too, by accident rather than by reason. Those two exist to guard the
*next* change -- a future "this handler is unreachable, delete it" -- and to
pin CustomTkinter behaviour the docstring in `scaling()` depends on. A test
that cannot fail for the reason you are adding it is worth keeping only if you
know that and can say what it does catch.
"""

from __future__ import annotations

import gc
import time
import tkinter

import customtkinter as ctk
import pytest

from emcc.widgets.canvas_util import scaling


def _root(attempts: int = 5) -> ctk.CTk:
    """A bare root, retrying Tk's transient start-up failure.

    Duplicated from `test_dashboard_view._root` rather than shared: hoisting it
    into `conftest.py` is the right long-term move, but `conftest.py` is shared
    across lanes and this landed under a scoped exception on `canvas_util.py`.
    Deliberately not the sized/themed variant -- nothing here renders.
    """
    last: tkinter.TclError | None = None
    for attempt in range(attempts):
        try:
            return ctk.CTk()
        except tkinter.TclError as exc:
            last = exc
            gc.collect()
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError(
        f"Tk failed to initialise after {attempts} attempts "
        f"(environmental, not a product failure): {last}"
    )


def test_scaling_reports_the_trackers_factor():
    """The positive control, and display-independent by construction.

    Compares against the tracker rather than against a number: on this machine
    both sides are 2.25, on a 100%-scaling display both are 1.0, and the test
    means the same thing either way.
    """
    root = _root()
    frame = ctk.CTkFrame(root)

    assert scaling(frame) == ctk.ScalingTracker.get_widget_scaling(frame)
    assert scaling(root) == ctk.ScalingTracker.get_widget_scaling(root)


def test_scaling_does_not_substitute_a_value_for_a_non_widget():
    """The defect this file exists for. Fails before the fix, passes after.

    Measured on the old code: `scaling(None)` returned `1.0`, as did
    `scaling("not a widget")`. Both are caller bugs, and the factor they
    produced was indistinguishable from a real one.
    """
    _root()

    with pytest.raises(AttributeError):
        scaling(None)

    with pytest.raises(AttributeError):
        scaling("not a widget")


def test_an_unregistered_window_root_falls_back_to_one():
    """The one substitution deliberately kept -- so it is tested, not argued.

    A raw `tk.Toplevel` is never registered with CustomTkinter, so the tracker
    raises `KeyError` for widgets inside it. 1.0 is correct there rather than
    merely plausible: nothing in such a window is CTk-scaled, so the
    conversion both callers use this factor for is the identity.

    Unreachable in `emcc/` today -- every Toplevel is a `CTkToplevel` -- which
    is exactly why it needs a test. An untested branch that cannot fire in
    production is the shape the original defect had.
    """
    root = _root()
    unregistered = tkinter.Toplevel(root)   # raw, NOT ctk.CTkToplevel
    canvas = tkinter.Canvas(unregistered)

    with pytest.raises(KeyError):
        ctk.ScalingTracker.get_widget_scaling(canvas)

    assert scaling(canvas) == 1.0


def test_a_destroyed_widget_still_reports_the_tracked_factor():
    """Pins the third-party behaviour `scaling`'s docstring depends on.

    A teardown race is the most plausible motive anyone would supply for the
    old bare `except`, and it is refuted rather than merely unsupported: a
    destroyed widget does not raise. This is CustomTkinter's behaviour, not
    ours, so it is pinned -- if a future version raises here instead, the
    `AttributeError` would propagate out of a real teardown path and the
    docstring's claim would be silently false.
    """
    root = _root()
    frame = ctk.CTkFrame(root)
    expected = scaling(frame)
    frame.destroy()

    assert scaling(frame) == expected
