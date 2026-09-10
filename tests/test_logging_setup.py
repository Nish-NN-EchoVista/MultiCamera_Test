"""F10: a file-logging failure must not be reported through a discarded print.

`setup_logging` used to announce the failure with

    print(f"warning: file logging unavailable ({exc})", file=sys.stderr)

which is discarded under a windowed interpreter. `sys.stderr` is `None` there,
`print(file=None)` falls back to `sys.stdout` -- also `None` -- and returns
**silently**. Measured against a real file descriptor, not inferred.

So the one message telling the operator there would be no log file was itself
destroyed, in exactly the situation where the log could not record it either.
That the condition is reachable is not a guess: the `console` branch in the
same function guards `sys.stderr is not None` for precisely this reason, so
either that guard is dead code or this one was missing.

These tests hold global logging state, so each one saves and restores the root
handlers and the module's `_configured` flag. Without that, clearing root
handlers would disturb pytest's own capture for every test that ran
afterwards -- a test that breaks its successors is worse than the bug.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

import pytest

from emcc.backend import logging_setup


class _Capture(logging.Handler):
    """Collects records, attached to the module's own logger.

    Attached to `emcc.backend.logging_setup` rather than to root on purpose:
    `setup_logging` clears root's handlers as its first act, so a capture
    there would be removed before the message it exists to catch is emitted.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def isolate_logging(monkeypatch):
    """Restore root handlers and `_configured` however the test exits."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setattr(logging_setup, "_configured", False)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


@pytest.fixture
def failing_file_handler(monkeypatch):
    """Make the rotating file handler unopenable, as a locked file would."""
    def refuse(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", refuse)


def _capture_setup(tmp_path, **kwargs):
    """Run `setup_logging`, collecting what its own logger emits."""
    module_logger = logging.getLogger("emcc.backend.logging_setup")
    capture = _Capture()
    module_logger.addHandler(capture)
    try:
        path = logging_setup.setup_logging(log_dir=tmp_path, **kwargs)
    finally:
        module_logger.removeHandler(capture)
    return path, capture.records


def test_the_failure_survives_a_windowed_interpreter(
    tmp_path, isolate_logging, failing_file_handler, monkeypatch
):
    """The condition the old `print` could not report.

    Both streams absent and no console handler -- exactly the pythonw case.
    The old code's single message went nowhere at all; a log record is at
    least emitted, so any handler that exists will see it.

    Dies on: reverting to `print`, or emitting before the handlers are set up.
    """
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)

    path, records = _capture_setup(tmp_path, console=False)

    assert path is None, "a failed file handler must report no log path"
    assert records, (
        "nothing was emitted. Under a windowed interpreter the old print was "
        "discarded silently, which is the whole defect."
    )
    message = records[-1].getMessage()
    assert "file logging unavailable" in message
    assert "Permission denied" in message, (
        "the cause must travel with the message -- there is no log to consult"
    )


def test_the_failure_is_a_warning_not_an_info(
    tmp_path, isolate_logging, failing_file_handler
):
    """Level is the difference between reported and merely recorded.

    A handler counts as fixed only at WARNING or above: `logger.debug(...)`
    would satisfy a "no bare excepts" metric while surfacing nothing to
    anyone, because the loggers sit at WARNING and DEBUG is filtered before
    any handler sees it.

    Dies on: lowering this to INFO or DEBUG.
    """
    _path, records = _capture_setup(tmp_path, console=False)

    assert records
    assert records[-1].levelno >= logging.WARNING, (
        f"emitted at {records[-1].levelname}, which the emcc loggers filter"
    )


def test_it_reaches_a_console_handler_when_one_exists(
    tmp_path, isolate_logging, failing_file_handler, monkeypatch
):
    """With a console, the operator actually sees it.

    This is what the old print achieved in the console case, and the fix must
    not lose it while fixing the windowed case. Emitting *after* the handlers
    are installed is what makes this work -- a message emitted first would
    reach nothing, since `setup_logging` clears root's handlers on entry.

    Dies on: emitting the warning before the console handler is added.
    """
    import io

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buffer)

    logging_setup.setup_logging(log_dir=tmp_path, console=True)

    assert "file logging unavailable" in buffer.getvalue(), (
        "the console handler did not receive it, so the warning is emitted "
        "before the handlers are installed"
    )


def test_a_working_log_file_says_nothing(tmp_path, isolate_logging):
    """The negative control.

    Without it, unconditionally warning would pass every test above while
    crying wolf on every normal start -- and a warning that fires always is
    read as noise and then ignored, which is how the real one gets missed.
    """
    path, records = _capture_setup(tmp_path, console=False)

    assert path is not None, "the file handler should have opened"
    assert not [r for r in records if r.levelno >= logging.WARNING], (
        f"warned on a healthy start: {[r.getMessage() for r in records]}"
    )
