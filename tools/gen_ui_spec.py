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
    raise KeyError(name)


BLOCKS = ("surfaces", "blends")


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
            return 1
        print(f"{SPEC.name} is up to date ({len(SURFACES) + len(BLENDS)} rows).")
        return 0

    if updated == text:
        print(f"{SPEC.name} already up to date; nothing written.")
        return 0
    SPEC.write_bytes(updated.encode("utf-8"))
    print(f"{SPEC.name} regenerated ({len(SURFACES) + len(BLENDS)} rows).")
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
        rows = render(name)
        print(f"  block '{name}' renders {len(rows) - 2} rows")
        ok &= len(rows) > 2

    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
