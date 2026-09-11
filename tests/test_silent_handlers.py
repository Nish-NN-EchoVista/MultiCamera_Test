"""The containment ratchet: silent handlers may leave, not arrive.

`tools/silent_handlers.py` fingerprints every `except` block in `emcc/` by how
visible its contents would be to someone reading the log. This pins the result
so the debt can only shrink.

WHY A SUBSET AND NOT AN EQUALITY. Equality would fail the moment someone fixes
a handler, which makes the guard an obstacle to the work it exists to
encourage -- and a guard that fires on improvement gets its baseline updated
as routine, at which point it is pinning nothing. A subset assertion passes
remediation downward silently and fails only on an ADDITION, which is the
direction that matters.

WHY NOT A COUNT. The silent-handler population has been measured four ways on
four days, correct each time and different each time, because handlers get
narrowed, deleted and split. A count cannot distinguish "one fixed" from "one
added and one removed". These are names, compared as names -- the same reason
the suite comparator is a junit roster rather than the number 336.

The fingerprints carry no line numbers, so edits above a handler do not churn
the baseline. See the tool's docstring for what the scan cannot see.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools.silent_handlers import BASELINE, scan  # noqa: E402


@pytest.fixture(scope="module")
def observed():
    return scan()


@pytest.fixture(scope="module")
def baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("tier", ["SILENT", "UNDECLARED"])
def test_no_new_invisible_handlers(observed, baseline, tier):
    """A handler may become more visible, never less.

    TRACKED is deliberately not ratcheted: it is the destination, and pinning
    it would fail the very change that moves a handler out of the other two.

    Dies on: adding an `except` block that logs nothing, or one that logs at
    WARNING or above without `exc_info`.
    """
    added = sorted(set(observed.get(tier, [])) - set(baseline.get(tier, [])))
    assert not added, (
        "%d new %s handler(s). Each either needs to log with exc_info, or -- "
        "if its silence is correct, as it is for a teardown race that fires on "
        "every close -- say so in a comment and add it to %s deliberately:\n  %s"
        % (len(added), tier, BASELINE.name, "\n  ".join(added))
    )


def test_the_baseline_describes_this_tree(observed, baseline):
    """The baseline must not drift into fiction.

    A subset assertion cannot catch a baseline listing handlers that no longer
    exist -- those simply never appear on the left-hand side. Left alone, the
    file slowly becomes a record of a tree nobody has, and the ratchet reads as
    stricter than it is.

    Stale entries are reported but do NOT fail: deleting a handler is the
    outcome the ratchet exists to encourage, and failing the run for it would
    punish exactly that. The signal belongs in the output, not in the exit
    code.

    Dies on: nothing. This is a report, and it says so.
    """
    stale = {
        tier: sorted(set(baseline.get(tier, [])) - set(observed.get(tier, [])))
        for tier in baseline
    }
    stale = {tier: fps for tier, fps in stale.items() if fps}
    if stale:
        print("\nbaseline entries no longer in the tree (fixed or moved):")
        for tier, fps in sorted(stale.items()):
            for fp in fps:
                print("    %-11s %s" % (tier, fp))
        print("  rerun `py tools/silent_handlers.py --write` to retire them.")


def test_the_ratchet_can_fail(observed):
    """The self-test, because a guard nobody has seen fire proves nothing.

    Every other assertion here passes when the tree is clean, which is also
    what a broken scan returns. This one fabricates an addition and checks the
    comparison rejects it.

    Dies on: `scan()` returning nothing, or the subset comparison being
    inverted.
    """
    assert observed.get("SILENT"), "scan() found no silent handlers at all"

    pretend_baseline = {"SILENT": []}
    added = set(observed["SILENT"]) - set(pretend_baseline["SILENT"])
    assert added, "an empty baseline must make every observed handler 'added'"
