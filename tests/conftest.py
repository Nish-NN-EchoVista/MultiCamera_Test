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
import logging
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


# ---------------------------------------------------------------------------
# Contained exceptions must not pass silently
# ---------------------------------------------------------------------------
#
# The codebase contains exceptions in three places on purpose, and each one
# logs and continues:
#
#     app.py:414             _pump        the whole drain/render/refresh loop
#     device_manager.py:383  _notify_ui   the UI callback
#     views/host.py          _call, and a second site in shutdown
#
# That is right at runtime -- the pump is the only path by which network
# activity reaches the UI, and a raising view must not freeze the view the
# operator is looking at. The problem is what it does to *verification*: a
# failure inside any of them leaves the suite green. Whether a bug is loud or
# silent depends on the call path rather than on the bug, and slice 4 moves
# chrome rules behind the host, so paths that are loud today go quiet.
#
# There is also no log to fall back on. `setup_logging` is called from
# `main.py` and the two tools, and never from `tests/` or this file, so no
# handler is attached and these records reach pytest's capture and are
# discarded on a pass. Grepping `logs/emcc.log` after a run proves nothing:
# zero hits there means the suite never wrote to it.
#
# So the oracle has to be in-process, and it has to key on something a call
# path cannot change.

#: Tests that deliberately provoke a contained failure. Mark with
#: `@pytest.mark.allow_contained_exceptions` and say why in the test.
_ALLOW_MARK = "allow_contained_exceptions"


class _ContainedExceptionRecorder(logging.Handler):
    """Records log entries that carry an exception, at WARNING or above.

    **Both conditions matter, and the level one is load-bearing rather than
    tidiness.** `exc_info` alone would be the cleaner rule -- every
    containment site uses `logger.exception`, which sets it, while ordinary
    error logging does not. But two sites set `exc_info` at **DEBUG** and are
    deliberate non-failures:

        integrations.py:142  pygui_windows: enumeration failed
        shell/chrome.py:52   could not strip the native caption

    The second runs inside **every** `App.__init__` -- it is the cosmetic
    caption-stripping fallback. The `emcc` loggers sit at WARNING, so both are
    filtered before any handler sees them, which is the only reason `exc_info`
    works as a separator at all.

    Do not lower this handler's level "to be thorough". At DEBUG, those two
    sites fire on most of the suite, the fixture looks broken on its first
    run, and the next person deletes it instead of understanding it.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info or record.exc_text:
            self.records.append(record)

    def describe(self) -> str:
        return "\n".join(
            f"    {r.name}: {r.getMessage()}  ({r.pathname}:{r.lineno})"
            for r in self.records
        )


@pytest.fixture(autouse=True)
def _no_contained_exceptions(request):
    """Fail a test if anything logged an exception it had swallowed.

    Attached to the `emcc` logger rather than the root, so a library logging
    its own handled exceptions cannot fail our tests.

    Keyed on the record rather than on the call path, which is the whole
    point: the same bug is loud through one route and silent through another,
    and this cannot tell the difference -- so it catches both.
    """
    recorder = _ContainedExceptionRecorder()
    emcc_logger = logging.getLogger("emcc")
    previous_level = emcc_logger.level
    emcc_logger.addHandler(recorder)
    # Records must actually reach the handler. The default effective level is
    # WARNING, which is what we want -- set it explicitly so a test that
    # raised it cannot blind this one, and never below WARNING (see the
    # handler docstring).
    emcc_logger.setLevel(logging.WARNING)
    try:
        yield recorder
    finally:
        emcc_logger.removeHandler(recorder)
        emcc_logger.setLevel(previous_level)

    if request.node.get_closest_marker(_ALLOW_MARK) is not None:
        return
    assert not recorder.records, (
        f"{len(recorder.records)} exception(s) were caught and logged rather "
        f"than raised, so this test passed over a real failure:\n"
        f"{recorder.describe()}\n"
        f"    If that is deliberate, mark the test with "
        f"@pytest.mark.{_ALLOW_MARK} and say why."
    )
