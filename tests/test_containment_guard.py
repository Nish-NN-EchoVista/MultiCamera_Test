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
    level inside the fixture.
    """
    logger = logging.getLogger("emcc.selftest")
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
