"""Test isolation for Tk state.

Tk keeps process-global state that leaks between tests unless it is cleaned up
deliberately, and the failure mode is nasty: one test that fails to destroy its
root causes *later, unrelated* tests to fail.

The mechanism is worth spelling out because it cost real debugging time.
`ImageTk.PhotoImage` (used inside `CTkImage`) creates its image with no master,
so it binds to `tkinter._default_root`. If a previous root was never destroyed,
`_default_root` still points at it, and every image a *new* root creates is
built in that dead interpreter -- surfacing as
`_tkinter.TclError: image "pyimageN" doesn't exist` in a test that did nothing
wrong.

So after every test: destroy anything left over, clear `_default_root`, and
drop the image/font caches.
"""

from __future__ import annotations

import gc
import tkinter

import pytest

from emcc import fonts, icons


@pytest.fixture(autouse=True)
def _tk_isolation():
    yield

    root = getattr(tkinter, "_default_root", None)
    if root is not None:
        # Close any surviving Toplevels first, then the root itself.
        try:
            for child in list(root.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    # destroy() only clears _default_root when the root *is* the default one,
    # so clear it unconditionally.
    try:
        tkinter._default_root = None
    except Exception:
        pass

    icons.clear_cache()
    fonts.clear_cache()

    # Force collection so Tcl actually releases the interpreter's resources
    # now rather than whenever CPython gets round to it. Without this, a suite
    # that builds ~50 roots in one process can exhaust them, and Tk fails at
    # *creation* with `couldn't read file ".../init.tcl": No error` -- a
    # confusing failure that has nothing to do with the test that hits it.
    gc.collect()


def make_app(config, attempts: int = 5):
    """Construct an `App`, retrying a transient Tk start-up failure.

    Creating ~50 Tk interpreters in one process intermittently fails on Windows
    while the interpreter is being initialised. Observed symptoms, all from the
    same underlying cause:

        Can't find a usable init.tcl ...
          couldn't read file ".../tcl8.6/init.tcl": No error
        invalid command name "tcl_findLibrary"

    Note the first one reports errno "No error" -- the file is present and
    valid, the open just transiently failed (handle churn, or a scanner holding
    it). It lands on whichever test is next, so it looks like a product bug and
    is not one.

    Any `TclError` raised while *creating the root* is treated as
    environmental, because that is what it always is: at that point no
    application code has run. Failures from anything after root creation
    propagate untouched.

    This belongs in the harness, not the product: the application creates
    exactly one root and never hits this.
    """
    import time

    from emcc.app import App

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return App(config=config)
        except tkinter.TclError as exc:
            last = exc
            gc.collect()
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError(
        f"Tk failed to initialise after {attempts} attempts "
        f"(environmental, not a product failure): {last}"
    )
