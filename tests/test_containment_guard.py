"""Positive control for the contained-exception guard in `conftest.py`.

The guard is an autouse fixture that fails a test if anything logged an
exception it had swallowed. Its failure mode is the dangerous kind: if it
stops working, every run reads clean and nobody learns anything. A guard that
finds nothing is indistinguishable from a guard that cannot find anything.

So this file exists to make that distinction, permanently. It is not a
throwaway check: a change that silently disables the guard fails here instead
of quietly widening what the suite tolerates.

Same discipline as declaring a test's mutations and then verifying they die --
which is why the guard needed this before it was trusted, rather than after
something slipped past it.
"""

from __future__ import annotations

import logging

import pytest

from .conftest import _no_contained_exceptions


@pytest.mark.allow_contained_exceptions
def test_the_guard_records_a_contained_exception(_no_contained_exceptions):
    """The guard must actually fire. Marked, or it would fail itself.

    Dies on: removing the handler from the `emcc` logger, narrowing `emit` so
    `exc_info` records are dropped, or attaching the handler to a logger the
    product does not use.
    """
    logger = logging.getLogger("emcc.selftest")
    try:
        raise RuntimeError("deliberate, for the positive control")
    except RuntimeError:
        logger.exception("self-test: a deliberately contained failure")

    records = _no_contained_exceptions.records
    assert records, (
        "the contained-exception guard recorded nothing after an exception "
        "was logged -- it is not wired to the `emcc` logger, or `emit` no "
        "longer keeps `exc_info` records. Every other test in the suite is "
        "currently unguarded."
    )
    assert "deliberately contained" in records[-1].getMessage()


@pytest.mark.allow_contained_exceptions
def test_the_guard_ignores_debug_level_exc_info(_no_contained_exceptions):
    """The level clause, pinned.

    Two product sites set `exc_info` at DEBUG on purpose and are not failures:
    `integrations.py:142` and `shell/chrome.py:52`. The second runs inside
    **every** `App.__init__`, so if the guard ever captured at DEBUG it would
    fire on most of the suite, look broken on its first run, and get deleted
    rather than understood.

    Dies on: lowering the handler's level, or lowering the `emcc` logger's
    level inside the fixture -- but **only via the two explicit assertions
    below**, and that distinction was measured rather than assumed.

    The filtering is doubly defended: the handler is at WARNING *and* the
    fixture sets the `emcc` logger to WARNING. So lowering either one alone
    changes nothing observable -- the other still filters the record -- and
    the behavioural assertion at the end of this test passes. Verified: each
    single mutation survived it; only both together were caught.

    Depth in the product is good and stays. But it makes the *effect*
    undetectable under a single mutation, so the mechanism has to be asserted
    directly here. This is the one place in the suite where pinning mechanism
    beats pinning behaviour, and the reason is that redundancy hides single
    faults from any behavioural probe.
    """
    logger = logging.getLogger("emcc.selftest")

    assert _no_contained_exceptions.level == logging.WARNING, (
        "the capture handler's level moved. At DEBUG it records the two "
        "deliberate exc_info sites, one of which runs in every App.__init__."
    )
    assert logging.getLogger("emcc").level == logging.WARNING, (
        "the fixture no longer holds the emcc logger at WARNING, so DEBUG "
        "records now reach the handler."
    )

    before = len(_no_contained_exceptions.records)

    try:
        raise RuntimeError("cosmetic, deliberately below the threshold")
    except RuntimeError:
        logger.debug("self-test: cosmetic fallback", exc_info=True)

    assert len(_no_contained_exceptions.records) == before, (
        "a DEBUG-level exc_info record was captured. The guard would now fire "
        "on the two deliberate DEBUG sites -- one of which runs in every "
        "App.__init__ -- and the suite would read as broken rather than clean."
    )


def test_an_ordinary_test_records_nothing(_no_contained_exceptions):
    """The negative control: no marker, and nothing recorded.

    Without this, the two tests above could pass while the guard recorded
    something on *every* test, which would make the marker load-bearing rather
    than exceptional. Deliberately unmarked, so it is also checked by the
    autouse assertion itself.
    """
    assert _no_contained_exceptions.records == []


def test_the_guard_is_autouse_and_therefore_applies_to_every_test(request):
    """The guard must be armed for tests that never ask for it.

    Deliberately does **not** request `_no_contained_exceptions` as an
    argument. If `autouse=True` is removed, the fixture still works for the
    tests above -- they request it by name and would keep passing -- while
    every other test in the suite silently loses its guard. That is the
    failure this catches and the three tests above cannot.

    Dies on: dropping `autouse=True` from the fixture.
    """
    assert "_no_contained_exceptions" in request.fixturenames, (
        "the contained-exception guard is no longer autouse, so it only "
        "applies to tests that request it explicitly. Every other test in "
        "the suite is unguarded."
    )


def test_the_guard_actually_fails_when_a_record_is_present():
    """The alarm, not the sensor.

    Everything above proves the recorder *captures*. None of it proves the
    assertion still *fires* -- that runs at teardown, so a test cannot observe
    its own, and `xfail(strict=True)` does not help either: xfail governs the
    call phase, so a teardown failure leaves the test XPASSing. Measured, and
    that is why this drives the fixture directly instead.

    The fixture is a generator: `next()` runs it up to the yield, and the
    second `next()` runs the teardown half, assertion included.

    Dies on: neutering `assert not recorder.records` in the fixture.
    """
    class _Node:
        def get_closest_marker(self, name):
            return None            # unmarked, so the assertion must apply

    class _Request:
        node = _Node()

    generator = _no_contained_exceptions.__wrapped__(_Request())
    recorder = next(generator)

    # One captured record is what the guard exists to refuse.
    recorder.records.append(logging.LogRecord(
        name="emcc.selftest", level=logging.ERROR,
        pathname=__file__, lineno=0,
        msg="injected for the alarm check", args=(), exc_info=None,
    ))

    with pytest.raises(AssertionError, match="caught and logged"):
        next(generator, None)


def test_the_guard_respects_the_opt_out_marker():
    """The marker must actually suppress the assertion.

    Otherwise a test that legitimately provokes containment could not be
    written, and the marker would be decoration. Same direct-drive mechanism
    as above, with the marker present.

    Dies on: ignoring the marker in the fixture's teardown.
    """
    class _Node:
        def get_closest_marker(self, name):
            return object() if name == "allow_contained_exceptions" else None

    class _Request:
        node = _Node()

    generator = _no_contained_exceptions.__wrapped__(_Request())
    recorder = next(generator)
    recorder.records.append(logging.LogRecord(
        name="emcc.selftest", level=logging.ERROR,
        pathname=__file__, lineno=0,
        msg="injected, but the test is marked", args=(), exc_info=None,
    ))

    next(generator, None)          # must not raise
