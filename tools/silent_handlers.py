"""Fingerprint every exception handler in `emcc/` by how visible it is.

WHY A RATCHET AND NOT A COUNT. The number of silent handlers has been
measured four different ways on four different days and been right each time
and different each time, because the population moves: handlers get narrowed,
deleted, split. A count cannot tell "one was fixed" from "one was added and
one removed", which is the roster-versus-total problem in a second domain.
So this emits NAMES, and the test asserts the observed set is a SUBSET of a
recorded baseline. Fixing a handler passes silently; adding one fails and is
named.

THE FINGERPRINT CARRIES NO LINE NUMBER:

    views/host.py::ViewHost._call [Exception] #0

Line numbers churn on every edit above them, which would make the baseline
need updating for changes that have nothing to do with containment -- and a
baseline updated as routine is a baseline nobody reads. The ordinal
disambiguates two handlers of the same type in the same scope, and moves only
when one of THOSE is added or removed.

THREE TIERS, and the distinction is what a reader of the log would see:

    SILENT      the handler logs nothing at all
    UNDECLARED  it logs at WARNING or above WITHOUT exc_info and without
                declaring containment -- so the operator sees a message with
                no traceback, and the containment fixture cannot see it
    TRACKED     it carries exc_info (or uses logger.exception), so the
                containment fixture counts it

Only SILENT and UNDECLARED are ratcheted. TRACKED is the destination, not a
debt, and pinning it would forbid remediation.

WHAT THIS CANNOT SEE, stated because a check that does not say what it missed
reads as a clean bill:

* handlers outside `emcc/` -- tests and tools are not scanned
* a handler that logs via something other than a `logger.<level>` call, e.g.
  a helper that logs on its behalf; it will read as SILENT
* DEFERRED reporting -- a handler that catches and hands the exception to
  code that logs it LATER reads as SILENT here, correctly by this tool's
  definition and misleadingly if you wanted "is it reported at all"
* whether a silent handler SHOULD log. That is a judgement per handler and
  the answer is genuinely not always yes: `_trim_rows`'s TclError fires on
  every normal teardown, and logging it would train a reader to ignore the
  channel.

    py tools/silent_handlers.py            # print the current fingerprints
    py tools/silent_handlers.py --json     # machine-readable, for the baseline
    py tools/silent_handlers.py --write    # rewrite the baseline deliberately
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "emcc"
BASELINE = ROOT / "tests" / "silent_handlers_baseline.json"

#: Levels at which a message reaches an operator's log by default.
LOUD = ("warning", "error", "critical", "exception")


def _scope_name(stack: list[ast.AST]) -> str:
    """`Class.method`, `function`, or `<module>` for the enclosing scope."""
    parts = [n.name for n in stack
             if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    return ".".join(parts) if parts else "<module>"


def _caught(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare"
    return ast.unparse(handler.type)


def _tier(handler: ast.ExceptHandler) -> str:
    """SILENT, UNDECLARED or TRACKED for one handler body."""
    loud_call = False
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in LOUD:
            continue
        # `logger.exception` always carries the traceback.
        if func.attr == "exception":
            return "TRACKED"
        loud_call = True
        for kw in node.keywords:
            if kw.arg == "exc_info" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False):
                return "TRACKED"
            if kw.arg == "extra" and "contained" in ast.unparse(kw.value):
                return "DECLARED"
    return "UNDECLARED" if loud_call else "SILENT"


def scan() -> dict[str, list[str]]:
    """Fingerprints by tier, sorted, for every handler under `emcc/`."""
    out: dict[str, list[str]] = {}
    seen: dict[tuple[str, str, str], int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix().replace("emcc/", "", 1)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[ast.AST] = []

        def walk(node: ast.AST) -> None:
            pushed = isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            if pushed:
                stack.append(node)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ExceptHandler):
                    key = (rel, _scope_name(stack), _caught(child))
                    ordinal = seen.get(key, 0)
                    seen[key] = ordinal + 1
                    fp = "%s::%s [%s] #%d" % (key[0], key[1], key[2], ordinal)
                    out.setdefault(_tier(child), []).append(fp)
                walk(child)
            if pushed:
                stack.pop()

        walk(tree)
    return {tier: sorted(fps) for tier, fps in sorted(out.items())}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--write", action="store_true",
                    help="rewrite the baseline from the current tree")
    args = ap.parse_args(argv[1:])

    observed = scan()
    if args.write:
        BASELINE.write_text(
            json.dumps(observed, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("wrote %s" % BASELINE.relative_to(ROOT).as_posix())
        for tier, fps in observed.items():
            print("  %-11s %d" % (tier, len(fps)))
        return 0

    if args.json:
        json.dump(observed, sys.stdout, indent=2)
        print()
        return 0

    total = 0
    for tier, fps in observed.items():
        print("%s (%d)" % (tier, len(fps)))
        for fp in fps:
            print("    %s" % fp)
        total += len(fps)
    print("SUM %d handlers across %d tier(s)" % (total, len(observed)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
