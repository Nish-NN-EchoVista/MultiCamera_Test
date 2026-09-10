"""Which public names in a module have no readers? Derived, not eyeballed.

Run deliberately, not as a guard. **This must not become a test or a ratchet
target.** "Dead public names" rises the moment anyone adds a constant before
wiring it, which is a normal intermediate state and not a defect -- so a
pinned count would fire on ordinary work. The same reasoning that kept the
silent-handler count from being pinned applies here.

    py tools/dead_names.py

THREE CATEGORIES, BECAUSE "UNUSED" IS TOO BLUNT

    EXTERNAL   read outside the module          live public surface
    INTERNAL   read only inside the module      an intermediate, still load-bearing
    NONE       read nowhere                     genuinely dead

The INTERNAL category is what stops this being a naive scan. `EMERALD_300`
and friends have no external readers at all -- they exist to compute the
flattened tokens that everything else uses. A tool reporting them as dead
would be inviting someone to delete the ramp.

THE DYNAMIC-ACCESS CHECK, AND WHY IT IS HERE

`tools/gen_ui_spec.py` reads five surface tokens by **string** name through
`getattr`, so `APP_BG` never appears as `theme.APP_BG` anywhere. A purely
static scan would report those five as dead.

**The check currently finds 0 such names** -- because all five also have
direct readers. That zero is not the reason the check exists. Without it the
survey's first run would have opened with five false deaths, and the finding
would have been acted on before anyone thought to doubt it. The result is
zero; the reason the check exists is the deliverable, and anyone pointing
this at another module should keep it.

WHY IT ASKS THE AST RATHER THAN GREPPING

An earlier version used a regex for internal reads and counted a name's own
`def` line, and every tuple-assigned constant's own left-hand side, as reads
-- so `mix()` was reported as internally used when it had no callers at all.
`ast.Name` with `ctx=Load` is the actual question.

A NOTE ON ACTING ON THE OUTPUT

Deleting a dead name mutates the graph this measured. `SEG_BORDER_HOVER` was
itself the only reader of nothing else, but it *was* a reader -- had its
target had no other reader, deleting it would have created the next dead
name, invisibly, because a survey reports what is dead now and says nothing
about what its own remedy will kill. **Re-run this after deleting**, and
report the second number rather than the first.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The module surveyed. Deliberately a constant rather than an argument --
#: this is a tool for a specific question, not a framework.
MODULE = ROOT / "emcc" / "theme.py"


def public_names(tree: ast.Module) -> list[str]:
    """Module-level names not starting with an underscore."""
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                elements = [target] if isinstance(target, ast.Name) else getattr(target, "elts", [])
                for element in elements:
                    if isinstance(element, ast.Name) and not element.id.startswith("_"):
                        found.append(element.id)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                found.append(node.name)
    return sorted(set(found))


def main() -> int:
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = public_names(tree)

    # Reads *within* the module: loaded names and attribute accesses only.
    # Assignment targets and `def` lines are not reads.
    internal_reads = {n.id for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    internal_reads |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    stem = MODULE.stem
    files = [p for p in ROOT.rglob("*.py")
             if ".git" not in p.parts and "__pycache__" not in p.parts]
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}

    external, internal_only, dynamic_only, dead = [], [], [], []
    for name in names:
        attribute = re.compile(rf"\b{stem}\.{name}\b")
        imported = re.compile(rf"from\s+\.*{stem}\s+import\s+[^\n]*\b{name}\b")
        quoted = re.compile(rf'["\']{name}["\']')

        readers, dynamic = [], []
        for path, text in texts.items():
            if path == MODULE:
                continue
            if attribute.search(text) or imported.search(text):
                readers.append(path.relative_to(ROOT).as_posix())
            elif quoted.search(text):
                dynamic.append(path.relative_to(ROOT).as_posix())

        if readers:
            external.append(name)
        elif dynamic:
            dynamic_only.append((name, sorted(set(dynamic))))
        elif name in internal_reads:
            internal_only.append(name)
        else:
            dead.append(name)

    print(f"{MODULE.relative_to(ROOT).as_posix()}: {len(names)} public names")
    print(f"  read outside the module : {len(external)}")
    print(f"  read only via getattr   : {len(dynamic_only)}")
    print(f"  read only inside it     : {len(internal_only)}")
    print(f"  read NOWHERE            : {len(dead)}")

    if dynamic_only:
        print("\nDynamic-only -- a static scan would call these dead:")
        for name, where in dynamic_only:
            print(f"  {name:<24} {', '.join(where)}")

    if internal_only:
        print("\nRead only inside the module (intermediates, not dead):")
        print("  " + ", ".join(internal_only))

    print("\nREAD NOWHERE:")
    for name in dead:
        print(f"  {name}")
    print("\nSome of these are deliberate -- check the name's own comment "
          "before deleting. See BRAND_BLUE / BRAND_GREEN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
