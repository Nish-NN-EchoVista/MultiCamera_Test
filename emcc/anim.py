"""Animation support.

CSS transitions have no Tk equivalent, so the one motion the design still
depends on is driven by an `after` loop:

* the Auto toggle knob -- `transition-all duration-200`, sliding 2px -> 14px.

The design also specifies `status-pulse` -- 2s ease-in-out opacity 1 -> 0.45
-> 1, infinite, on the connection dots and the "System Online" dot
(src/index.css). It was implemented here as one shared ticker and removed on
2026-09-10. Recorded rather than quietly dropped, because the removal is a
deviation from the Figma spec and a reader comparing the two will otherwise
take it for an oversight:

* the per-frame cost was **O(pulsing dots)**, not constant -- one `configure`
  per dot per frame at 30fps, so 750 calls/s at the specified 25 devices,
  measured at ~22% of a core continuously with four idle cards and nothing
  connected;
* it never stopped -- there was no visibility gating, so a minimised window
  and a view hidden behind another both kept pulsing;
* and two successive frames of a pulsing window are never identical, which
  cost every whole-window screen capture its settled-frame check.

The dots are static now.

Hover colour changes are applied instantly rather than eased: the colour
changes in one step, with no interpolation.
"""

from __future__ import annotations

from typing import Callable

from . import theme


def ease_in_out(t: float) -> float:
    """Smoothstep. Visually indistinguishable from CSS ease-in-out here."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class Tween:
    """One-shot eased interpolation, used for the toggle knob slide."""

    def __init__(self, root):
        self._root = root
        self._job = None

    def run(self, duration_ms: int, apply: Callable[[float], None],
            fps: int = 60) -> None:
        """Call `apply(eased_progress)` from 0 to 1 over `duration_ms`."""
        self.cancel()
        frames = max(1, round(duration_ms / 1000 * fps))
        interval = max(1, round(duration_ms / frames))

        def step(i: int):
            apply(ease_in_out(i / frames))
            if i < frames:
                self._job = self._root.after(interval, step, i + 1)
            else:
                self._job = None

        step(0)

    def cancel(self) -> None:
        if self._job is not None:
            try:
                self._root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
