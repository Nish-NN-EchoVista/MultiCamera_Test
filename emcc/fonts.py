"""Font resolution for the EMCC port.

The design specifies Inter (300-700) and JetBrains Mono (400-600), loaded from
Google Fonts in src/index.css. Neither ships with Windows, so this module
resolves the best available family at runtime and degrades to Segoe UI /
Consolas. Install the two families for exact typographic fidelity -- nothing
else in the app changes.

Two impedance mismatches with Tk are handled here:

1. Tk's font object exposes only ``normal`` and ``bold``. The design uses five
   numeric weights, so 600/700 map to bold and 300/400/500 to normal -- except
   where the installed family ships a named intermediate face (Segoe UI
   Semibold, Inter Medium), which is preferred when present.
2. Tk sizes are points when positive and *pixels* when negative. The design is
   specified in px, so every size here is passed through negatively.
"""

from __future__ import annotations

import tkinter.font as tkfont

import customtkinter as ctk

# Preference order, best fidelity first.
_SANS_STACK = ["Inter", "Segoe UI Variable Text", "Segoe UI", "Tahoma"]
_MONO_STACK = ["JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New"]

# Named intermediate faces, keyed by (base family, weight).
_NAMED_FACES = {
    ("Inter", 500): "Inter Medium",
    ("Inter", 600): "Inter SemiBold",
    ("Segoe UI", 600): "Segoe UI Semibold",
    ("Segoe UI", 300): "Segoe UI Light",
    ("JetBrains Mono", 500): "JetBrains Mono Medium",
}

_available: set[str] | None = None
_sans: str | None = None
_mono: str | None = None


def _families() -> set[str]:
    """Installed font families. Requires an initialised Tk root."""
    global _available
    if _available is None:
        _available = set(tkfont.families())
    return _available


def _resolve(stack: list[str]) -> str:
    for family in stack:
        if family in _families():
            return family
    return stack[-1]


def init() -> None:
    """Resolve the sans and mono families. Call once after the root exists."""
    global _sans, _mono
    _sans = _resolve(_SANS_STACK)
    _mono = _resolve(_MONO_STACK)


def report() -> str:
    """Human-readable summary of what was actually resolved."""
    sans_exact = _sans == _SANS_STACK[0]
    mono_exact = _mono == _MONO_STACK[0]
    lines = [
        f"  sans: {_sans}" + ("" if sans_exact else f"  (design asks for {_SANS_STACK[0]})"),
        f"  mono: {_mono}" + ("" if mono_exact else f"  (design asks for {_MONO_STACK[0]})"),
    ]
    if not (sans_exact and mono_exact):
        lines.append("  -> install Inter + JetBrains Mono for exact typography")
    return "\n".join(lines)


def _build(base: str, size_px: float, weight: int) -> ctk.CTkFont:
    family = _NAMED_FACES.get((base, weight))
    if family and family in _families():
        bold = False
    else:
        family = base
        bold = weight >= 600
    # Negative size == pixels, matching the design's px specification.
    return ctk.CTkFont(family=family, size=-round(size_px), weight="bold" if bold else "normal")


# A device card builds ~88 widgets and nearly every one asks for a font. Left
# uncached that is 88 CTkFont objects per card, each registering itself for
# scaling updates -- a real cost when cards are created at runtime. The design
# only uses about a dozen distinct (family, size, weight) triples, and sharing
# a CTkFont across widgets is how CustomTkinter's own theme fonts work.
_font_cache: dict[tuple[str, float, int], ctk.CTkFont] = {}

def _cached(base: str, size_px: float, weight: int) -> ctk.CTkFont:
    key = (base, size_px, weight)
    font = _font_cache.get(key)
    if font is None:
        font = _build(base, size_px, weight)
        _font_cache[key] = font
    return font


def clear_cache() -> None:
    """Drop every cached font and re-resolve families.

    A CTkFont belongs to one Tk interpreter, so this must be called when a new
    root is created -- `App.__init__` does. Explicit rather than inferred, for
    the reasons recorded in `icons.py`.
    """
    global _available, _sans, _mono
    _font_cache.clear()
    _available = None       # families must be re-read from the new interpreter
    _sans = _mono = None


def sans(size_px: float, weight: int = 400) -> ctk.CTkFont:
    if _sans is None:
        init()
    return _cached(_sans, size_px, weight)


# Tk fonts have no letter-spacing property, so the design's tracked uppercase
# micro-labels (tracking-[0.14em] on the section labels, tracking-[0.12em] on
# the sub-header and the EMCC badge) are approximated by inserting hair spaces
# between characters. A hair space is roughly 1/12 em in the families used
# here, so one spacer lands close to 0.08em and two close to 0.16em.
_HAIR = " "


def tracked(text: str, em: float = 0.14) -> str:
    """Approximate CSS letter-spacing of `em` by hair-space insertion."""
    spacers = max(1, round(em / 0.08))
    return (_HAIR * spacers).join(text)


def mono(size_px: float, weight: int = 400) -> ctk.CTkFont:
    if _mono is None:
        init()
    return _cached(_mono, size_px, weight)
