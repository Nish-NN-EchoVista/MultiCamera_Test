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

    # Release the Python-side objects that hold Tcl resources -- widget
    # wrappers, `CTkImage` and `CTkFont` handles -- promptly rather than
    # whenever CPython gets round to it.
    #
    # It does NOT prevent interpreter exhaustion, and this comment used to say
    # it did. That claim was wrong four ways:
    #
    #   * `make_app`'s own docstring below described the same symptom as a
    #     transient open failure on a file that is present and readable. The
    #     two paragraphs contradicted each other in one file. Described rather
    #     than quoted here on purpose: quoting the other comment's wording is
    #     what made this bullet go stale the first time that wording changed;
    #   * collection provably cannot free the roots. CustomTkinter's
    #     `ScalingTracker.window_dpi_scaling_dict` is written at three sites
    #     and deleted from at none, so every root the process creates stays
    #     strongly referenced for the life of the interpreter. Measured: three
    #     roots created, given widgets and destroyed with `gc.collect()`
    #     between, and the dict grew 1, 2, 3;
    #   * this suite builds 323 roots in one process with
    #     `failures=0 errors=0 skipped=0`, and a 200-root control constructed
    #     and destroyed in one process gave zero failures -- four to six times
    #     the claimed threshold, no exhaustion;
    #   * the failure signature is a path lookup inside the Python
    #     installation's own Tcl tree -- both measured retries carried
    #     `Can't find a usable tk.tcl` -- and leaked root objects cannot make
    #     a file lookup fail. This is the reason that does not depend on a
    #     rate, which makes it the strongest of the four.
    #
    # The call is kept rather than removed: it still frees what is freeable,
    # and its value here is unmeasured rather than shown to be nil. Removing
    # it would be a behaviour change on no evidence, which is the same error
    # in the other direction.
    gc.collect()


def make_app(config, attempts: int = 5):
    """Construct an `App`, retrying a transient Tk start-up failure.

    Creating Tk interpreters in one process intermittently fails on Windows
    while the interpreter is being initialised, at roughly 0.5% of
    constructions: 2 in 370, pooled across three full suites (2/119, 0/122,
    0/129). Pooled rather than from one run -- the 1.7% that circulated is
    2/119, a single-run figure quoted in an argument about single-run figures.
    The loop absorbed both failures silently, with no log line and nothing in
    the pass-set, which is why the rate was unknown until it was instrumented.

    NOT a function of how many. An earlier version of this docstring said
    "~50" and attributed it to interpreter exhaustion. Refuted three ways: a
    200-root control gave zero failures, this suite builds 323, and the
    retries land at constructions #4 and #73 of 119 and at #2 of a
    two-construction control. Nothing cumulative fails at #2.

    THE CAUSE OF THE TRANSIENT IS NOT ESTABLISHED. Both measured retries
    carried:

        Can't find a usable tk.tcl in the following directories:
          .../Python312/tcl/tcl8.6/tk8.6

    and these have been seen at other times:

        Can't find a usable init.tcl ...
          couldn't read file ".../tcl8.6/init.tcl": No error
        invalid command name "tcl_findLibrary"

    Every one is a lookup inside the *Python installation's* own Tcl tree
    rather than the project tree, so where this repository lives cannot
    affect it. Whether they share a single cause is unmeasured, and an
    earlier version of this docstring asserted that they did.

    That version also named a cause -- "handle churn, or a scanner holding
    it". Plausible, never measured, and removed for exactly that reason: a
    replacement that names a cause inherits the defect it is correcting.

    What the retry itself evidences, and the whole of what it relies on: the
    errno on the init.tcl variant is "No error" and the file reads correctly
    afterwards, so the failure is transient rather than a misconfiguration.
    It lands on whichever test is next, so it looks like a product bug and is
    not one.

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
