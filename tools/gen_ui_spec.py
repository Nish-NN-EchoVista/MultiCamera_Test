"""Generate the colour tables in `docs/UI_SPEC.md` from `theme.py`.

WHY GENERATED RATHER THAN GUARDED
    That table went stale twice -- once when the blue ramp was re-derived to
    the brand blue, once when the green became `#11ab17` -- and both times it
    was wrong for weeks because nothing derives from a document. A guard test
    would say the table is wrong. Generating means it cannot be.

    It is the same fix applied elsewhere on this project for the same reason:
    `_FACADE` was wrong three times as a transcribed list and has been right
    since it became a scan; the facade snapshot stopped losing names when it
    derived its reached-set; `test_dashboard_view.py`'s conformance check
    omitted `caption` entirely as a hardcoded list of eight and now derives
    from `DeviceView`.

WHAT IS AND IS NOT GENERATED
    Only the rows between markers. `UI_SPEC.md` carries real hand-written
    prose -- the two ramp derivations, the provenance line, the trap notes --
    and a generator owning the whole file would destroy them. The script
    refuses to run rather than guess if the markers are not exactly right.

    Two blocks, named, because there are two colour tables and an unnamed
    pair could not say which was which:

        <!-- BEGIN GENERATED: surfaces -->  ...  <!-- END GENERATED: surfaces -->
        <!-- BEGIN GENERATED: blends -->    ...  <!-- END GENERATED: blends -->

    Each name must appear exactly once as BEGIN and once as END, in that
    order. Anything else is an error, not a best guess.

VALUES COME FROM `theme`, NEVER RECOMPUTED HERE
    Every hex is read from the module attribute. `theme.py` calls `over()` at
    import, so reading `theme.ON_BG` *is* the result of `over()`. A generator
    that reimplemented the compositing would be a second implementation of
    `over()`, free to disagree with the first one silently -- which is the
    class of bug this whole exercise is about.

    What the generator does own is the table's *structure*: the row labels
    and the Tailwind markup column, which are prose describing provenance and
    are not derivable from `theme.py`.

USAGE
    py tools/gen_ui_spec.py            rewrite the generated blocks
    py tools/gen_ui_spec.py --check    exit 1 if the file is out of date
    py tools/gen_ui_spec.py --self-test  prove the generator actually works
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from emcc import theme  # noqa: E402

SPEC = ROOT / "docs" / "UI_SPEC.md"

#: Surfaces: (row label, theme attribute, Tailwind source). The attribute
#: name is printed in the row, so the doc names what it reads.
SURFACES = [
    ("Page",                 "APP_BG",      "bg-[#12141b]"),
    ("Title bar",            "TITLEBAR_BG", "bg-[#0c0e14]"),
    ("Device card",          "CARD",        "bg-[#181c25]"),
    ("Inputs / temp button", "INPUT_BG",    "bg-[#111419]"),
    ("Auto button (off)",    "AUTO_OFF_BG", "bg-[#1c1f29]"),
]

#: Blends: (row label, Tailwind source, accessor). The accessor returns the
#: value `theme` computed -- never a recomputation.
BLENDS = [
    ("Clean off fill",          "bg-blue-700/15 on card",      lambda t: t.CLEAN_OFF_BG),
    ("Clean off fill hover",    "bg-blue-700/25",              lambda t: t.CLEAN_OFF_BG_HOVER),
    ("Clean off border",        "border-blue-500/25",          lambda t: t.CLEAN_OFF_BORDER),
    ("Active fill",             "bg-emerald-700/25",           lambda t: t.ON_BG),
    ("Active fill hover",       "bg-emerald-700/35",           lambda t: t.ON_BG_HOVER),
    ("Active border",           "border-emerald-500/35",       lambda t: t.ON_BORDER),
    ("Conn idle ring",          "ring-blue-700/40",            lambda t: t.CONN["idle"]["ring"]),
    ("Conn connected ring",     "ring-emerald-700/40",         lambda t: t.CONN["connected"]["ring"]),
    ("Conn disconnected ring",  "ring-red-700/40",             lambda t: t.CONN["fault"]["ring"]),
    ("Caption connected",       "text-emerald-500/60",         lambda t: t.CONN["connected"]["caption_color"]),
    ("Caption disconnected",    "text-red-500/70",             lambda t: t.CONN["fault"]["caption_color"]),
    ("Temp alert border",       "border-red-600/35 on input",  lambda t: t.TEMP_ALERT_BORDER),
    ("Temp alert border hover", "border-red-500/55",           lambda t: t.TEMP_ALERT_BORDER_HOVER),
    ("Temp alert caption",      "text-red-500/60",             lambda t: t.TEMP_ALERT_CAPTION),
    ("HIGH badge fill",         "bg-red-600/20",               lambda t: t.BADGE_BG),
    ("HIGH badge border",       "border-red-500/30 on badge",  lambda t: t.BADGE_BORDER),
    ("Logo fill",               "bg-blue-600/20 on titlebar",  lambda t: t.LOGO_BG),
    ("Logo border",             "border-blue-500/30",          lambda t: t.LOGO_BORDER),
    ("EMCC fill",               "bg-blue-600/18",              lambda t: t.EMCC_BG),
    ("EMCC border",             "border-blue-500/25",          lambda t: t.EMCC_BORDER),
    ("EMCC text",               "text-blue-400/90 on badge",   lambda t: t.EMCC_TEXT),
    ("Header alert text",       "text-red-400/80",             lambda t: t.HEADER_ALERT_TEXT),
    ("Legend idle",             "bg-blue-600/70 on app",       lambda t: t.LEGEND[0][0]),
    ("Legend connected",        "bg-emerald-600/70",           lambda t: t.LEGEND[1][0]),
    ("Legend disconnected",     "bg-red-600/70",               lambda t: t.LEGEND[2][0]),
    ("Add hover fill",          "bg-blue-600/[0.03]",          lambda t: t.ADD_BG_HOVER),
    ("Add hover border",        "border-blue-600/30",          lambda t: t.ADD_BORDER_HOVER),
    ("Add hover text",          "text-blue-500/70",            lambda t: t.ADD_TEXT_HOVER),
    ("Add circle hover",        "border-blue-600/40",          lambda t: t.ADD_CIRCLE_HOVER),
    ("Input focus border",      "focus:border-blue-500/50",    lambda t: t.FOCUS_BORDER),
]


def render(name: str, module=theme) -> list[str]:
    """The rows for one named block, without line endings."""
    if name == "surfaces":
        rows = ["| Role | Token | Hex | Source |", "|---|---|---|---|"]
        for label, attr, source in SURFACES:
            rows.append(f"| {label} | `{attr}` | `{getattr(module, attr)}` | `{source}` |")
        return rows
    if name == "blends":
        rows = ["| Role | Tailwind source | Flattened |", "|---|---|---|"]
        for label, source, accessor in BLENDS:
            rows.append(f"| {label} | `{source}` | `{accessor(module)}` |")
        return rows
    if name == "metrics":
        # No header row: this block sits INSIDE an existing table whose
        # header and three hand-written rows live outside the markers.
        return [f"| {label} | {accessor(module)} | `{source}` |"
                for label, source, accessor in METRICS]
    raise KeyError(name)


#: Metrics: (row label, source markup, accessor). Only rows whose value is
#: a `theme` constant. Three rows of that table are deliberately OUTSIDE the
#: generated block and stay hand-written:
#:
#:   Badge radius   the doc says "4 / 6"; theme has only BADGE_RADIUS = 4, so
#:                  generating it would silently rewrite content rather than
#:                  refresh a value. Flagged, not resolved here.
#:   Separator      1px and its 4px margins are literals in device_card.py,
#:                  not theme constants. Transcribing them into this file
#:                  would move the staleness rather than remove it.
#:   Capacity       prose.
METRICS = [
    ("Title bar height",   "h-[52px]",              lambda t: t.TITLEBAR_H),
    ("Card min height",    "min-h-[142px]",         lambda t: t.CARD_MIN_H),
    ("Card radius",        "rounded-xl",            lambda t: t.CARD_RADIUS),
    ("Control radius",     "rounded-lg",            lambda t: t.CTRL_RADIUS),
    ("Page padding",       "px-8 py-4",
     lambda t: f"{t.PAGE_PAD_X} x, {t.PAGE_PAD_Y} y"),
    ("Card gap",           "gap-3",                 lambda t: t.CARD_GAP),
    ("Card padding",       "px-5",                  lambda t: f"{t.CARD_PAD_X} x"),
    ("Label→control gap", "gap-2.5",           lambda t: t.SECTION_GAP),
    ("Column widths",      "w-[...]",
     lambda t: f"{t.COL_NAME} / {t.COL_IP} / {t.COL_CONN} / flex / {t.COL_TEMP}"),
    ("Scroll threshold",   "devices.length >= 4",
     lambda t: f"{t.SCROLL_THRESHOLD} devices"),
]


#: The one place a block is declared. `BLOCKS` and every row count derive
#: from this, because the first version hardcoded `("surfaces", "blends")`
#: and a row total of `len(SURFACES) + len(BLENDS)` -- so adding `metrics`
#: left the tool reporting "35 rows" while generating 45, and the self-test
#: reporting 8 metric rows against 10, from a "minus 2 for the header" that
#: metrics does not have. Transcription rot, in the tool written to remove it.
SPECS = {"surfaces": SURFACES, "blends": BLENDS, "metrics": METRICS}
BLOCKS = tuple(SPECS)


def row_count() -> int:
    return sum(len(spec) for spec in SPECS.values())


def _bounds(text: str, name: str) -> tuple[int, int]:
    """Character offsets just inside one named block.

    Refuses on anything but exactly one well-ordered pair. Ownership that
    cannot be established is not a licence to guess -- a generator that
    picked the first plausible pair would eventually eat prose.
    """
    begin = list(re.finditer(rf"<!--\s*BEGIN GENERATED: {name}\s*-->", text))
    end = list(re.finditer(rf"<!--\s*END GENERATED: {name}\s*-->", text))
    if len(begin) != 1 or len(end) != 1:
        raise SystemExit(
            f"{SPEC.name}: expected exactly one BEGIN and one END marker for "
            f"'{name}', found {len(begin)} and {len(end)}. Refusing to guess."
        )
    if begin[0].end() >= end[0].start():
        raise SystemExit(f"{SPEC.name}: '{name}' END marker precedes BEGIN.")
    return begin[0].end(), end[0].start()


SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def coverage(text: str) -> tuple[list[str], list[str]]:
    """-> (sections this tool verifies, sections it does not).

    Both lists are DERIVED from the document. The covered set is found by
    locating each generated block and walking back to the heading above it;
    the uncovered set is every other `##` heading.

    Neither is transcribed, and that is not fastidiousness. A hand-written
    list here would go stale the first time someone adds a section -- which
    is precisely the failure this tool exists to remove, reproduced inside
    the tool built to remove it. When this was written by hand in a message,
    the count was 17; the document has 18.
    """
    sections = [(m.group(1).strip(), m.start()) for m in SECTION_RE.finditer(text)]
    covered: list[str] = []
    for name in BLOCKS:
        start, _ = _bounds(text, name)
        above = [title for title, offset in sections if offset < start]
        if above:
            covered.append(above[-1])
    named = set(covered)
    uncovered = [title for title, _ in sections if title not in named]
    return covered, uncovered


def scope_report(text: str) -> str:
    """What was verified, and -- the point -- what was not.

    The old message was `"UI_SPEC.md is up to date"`. True of two blocks,
    phrased about the file, and identical whether the tool had checked a
    region or never looked at it. Silence that cannot distinguish "clean"
    from "did not look" is the failure mode this project keeps finding in
    its own instruments; an instrument that cannot say "I did not look
    there" invites its silence to be read as absence of drift.
    """
    covered, uncovered = coverage(text)
    total = len(covered) + len(uncovered)
    rows = row_count()
    lines = [
        f"{SPEC.name}: verified {len(covered)} of {total} sections "
        f"({', '.join(covered)}) -- {rows} rows current.",
    ]
    if uncovered:
        lines.append(
            "NOT checked by this tool, may be stale: "
            + ", ".join(uncovered)
            + f" ({len(uncovered)} sections)"
        )
    return chr(10).join(lines)


def generate(text: str, newline: str, module=theme) -> str:
    """Return `text` with every generated block rewritten."""
    for name in BLOCKS:
        start, stop = _bounds(text, name)
        body = newline + newline.join(render(name, module)) + newline
        text = text[:start] + body + text[stop:]
    return text


def _read() -> tuple[str, str]:
    """File text plus its dominant line ending, preserved byte-exactly.

    Read in binary. `read_text()` normalises CRLF to LF on the way in, and a
    later write then re-expands it -- which is how a 30-line change diffed as
    a whole-file rewrite here three times.
    """
    raw = SPEC.read_bytes()
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    newline = "\r\n" if crlf >= lf else "\n"
    return raw.decode("utf-8"), newline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the file is out of date; write nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the generator responds to theme and is idempotent")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    text, newline = _read()
    updated = generate(text, newline)

    if args.check:
        if updated != text:
            print(f"{SPEC.name} is OUT OF DATE -- run: py tools/gen_ui_spec.py")
            print(scope_report(text))
            return 1
        print(scope_report(text))
        return 0

    if updated == text:
        print(f"{SPEC.name}: generated blocks already current; nothing written.")
        print(scope_report(text))
        return 0
    SPEC.write_bytes(updated.encode("utf-8"))
    print(f"{SPEC.name} regenerated.")
    print(scope_report(updated))
    return 0


def self_test() -> int:
    """A generator whose first run produces no diff is indistinguishable
    from one that does not work. So prove both directions before trusting it.
    """
    text, newline = _read()
    ok = True

    once = generate(text, newline)
    twice = generate(once, newline)
    print(f"  idempotent (second run changes nothing) : {twice == once}")
    ok &= twice == once

    class Shifted:
        """`theme` with one derived token altered."""
        def __getattr__(self, item):
            return getattr(theme, item)
        ON_BG = "#ff0000"

    shifted = generate(text, newline, Shifted())
    responds = shifted != once and "#ff0000" in shifted
    print(f"  responds to a changed theme value       : {responds}")
    ok &= responds

    restored = generate(shifted, newline)
    print(f"  a stale table is corrected, not appended: {restored == once}")
    ok &= restored == once

    for name in BLOCKS:
        declared = len(SPECS[name])
        print(f"  block '{name}' renders {declared} rows")
        ok &= declared > 0 and len(render(name)) >= declared

    # Guard the scope report itself: an empty or total "covered" list would
    # make the honesty line a lie in either direction.
    covered, uncovered = coverage(text)
    headings = len(SECTION_RE.findall(text))
    partitions = len(covered) + len(uncovered) == headings
    print(f"  coverage partitions all {headings} headings : {partitions}")
    print(f"  covered non-empty, uncovered non-empty  : "
          f"{bool(covered) and bool(uncovered)}")
    ok &= partitions and bool(covered) and bool(uncovered)

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
