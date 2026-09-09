"""Animation support.

CSS transitions have no Tk equivalent, so the two motions the design actually
depends on are driven by `after` loops:

* `status-pulse` -- 2s ease-in-out opacity 1 -> 0.45 -> 1, infinite, on the
  connection dots and the "System Online" dot (src/index.css).
* the Auto toggle knob -- `transition-all duration-200`, sliding 2px -> 14px.

Hover colour changes are applied instantly rather than eased. The design's
`duration-200` on those is a polish detail that Tk cannot express without
animating every colour channel of every widget, and the cost/benefit is poor.
"""

from __future__ import annotations

from typing import Callable

from . import theme


def ease_in_out(t: float) -> float:
    """Smoothstep. Visually indistinguishable from CSS ease-in-out here."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class Pulse:
    """Shared ticker for every `status-pulse` element.

    One `after` loop drives all registered dots so they stay in phase, as they
    do in CSS where a single named animation starts at page load.

    Each dot's per-frame colours are precomputed on registration: the opacity
    curve is identical for all of them, only the base colour and backdrop
    differ, so there is nothing to recompute at runtime.

    Cost
    ----
    **The loop is shared; the work is not.** `_tick` iterates every registered
    dot and issues one `configure` per dot per frame, so the per-frame cost is
    **O(pulsing dots)**:

        30 fps x N dots configure calls per second, indefinitely

    At the seeded three devices that is 90/s; at the specified 25 it is
    **750/s**, and it continues for as long as the window is open because
    `theme.CONN` marks *idle* as `"pulse": True` -- only `fault` is `False`.
    Measured on the bench: ~22% of a core, continuously, with four idle cards
    and no hardware connected.

    An earlier version of this docstring claimed the frame cost "does not
    scale with device count". That was false, and it is recorded here rather
    than quietly deleted because it was believed and repeated -- a wrong
    performance claim in a docstring is worse than none, since it is what a
    reader trusts *instead of* measuring.

    There is no visibility gating: a minimised window keeps pulsing, and a
    view hidden behind another keeps pulsing its dots. Whether idle dots
    should pulse at all is a design question about the Figma spec, not a bug
    in this class.
    """

    PERIOD_MS = 2000
    FPS = 30
    MIN_OPACITY = 0.45

    def __init__(self, root):
        self._root = root
        self._frames = max(2, round(self.PERIOD_MS / 1000 * self.FPS))
        self._interval = round(self.PERIOD_MS / self._frames)
        self._curve = self._build_curve()
        self._targets: dict[int, tuple[object, list[str]]] = {}
        self._token = 0
        self._frame = 0
        self._job = None
        #: While set, the ticker never runs and every target -- including ones
        #: registered later -- is held at frame 0. See `freeze`.
        self._frozen = False

    def _build_curve(self) -> list[float]:
        """Opacity per frame: 1 -> MIN at the halfway point -> 1."""
        curve = []
        for i in range(self._frames):
            t = i / self._frames
            # Two eased half-cycles, matching the 0% / 50% / 100% keyframes.
            phase = ease_in_out(t * 2) if t < 0.5 else ease_in_out((1 - t) * 2)
            curve.append(1.0 - (1.0 - self.MIN_OPACITY) * phase)
        return curve

    def register(self, widget, color: str, backdrop: str) -> int:
        """Pulse `widget`'s fg_color. Returns a token for `unregister`."""
        shades = [theme.over(color, backdrop, o) for o in self._curve]
        self._token += 1
        self._targets[self._token] = (widget, shades)
        if self._frozen:
            # Inherit the frozen state rather than defeating it.
            self._paint(widget, shades[0])
        else:
            self._start()
        return self._token

    def unregister(self, token: int) -> None:
        self._targets.pop(token, None)
        if not self._targets:
            self._stop()

    def freeze(self) -> None:
        """Hold every dot at frame 0 and keep it there. A mode, not an event.

        For deterministic screen captures. Two successive frames of a pulsing
        window are never identical, so `tools/window_capture.capture_stable`
        -- which accepts a frame only once two agree -- cannot be used on the
        main window while this ticker runs. Freezing is therefore what lets
        every captured state use the strong check rather than the
        blank-only one, as well as making an image diff exact: measured, two
        captures of the same idle window differ by 1216 of 5.8M pixels with
        the pulse running and by zero with it stopped.

        **A mode rather than a one-shot**, because `stop_all()` stops the
        ticker but does not stop it being restarted: every new `DeviceCard`
        builds a `ConnectionButton` which registers a dot, so adding devices
        would restart it. A capture pass that adds six cards would freeze,
        appear to work, and be pulsing again by the state with the most dots.

        **Frame 0 rather than wherever it stopped**, because the phase has to
        be defined: freezing at the current frame leaves a colour that depends
        on *when* freeze was called, which is not reproducible between runs
        and would move the noise rather than remove it.

        Not called by the application. `stop_all()` remains the shutdown path,
        where "stop now" is the correct meaning.
        """
        self._frozen = True
        self._stop()
        for widget, shades in self._targets.values():
            self._paint(widget, shades[0])

    @staticmethod
    def _paint(widget, colour: str) -> None:
        """Set one target's colour, tolerating a widget already torn down."""
        try:
            if widget.winfo_exists():
                widget.configure(fg_color=colour)
        except Exception:
            pass

    def stop_all(self) -> None:
        """Drop every target and cancel the ticker. Used on shutdown, so a
        queued frame cannot fire against a widget Tk is tearing down."""
        self._targets.clear()
        self._stop()

    def _start(self) -> None:
        if self._frozen:
            return
        if self._job is None:
            self._job = self._root.after(self._interval, self._tick)

    def _stop(self) -> None:
        if self._job is not None:
            try:
                self._root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % self._frames
        dead = []
        for token, (widget, shades) in self._targets.items():
            try:
                if widget.winfo_exists():
                    widget.configure(fg_color=shades[self._frame])
                else:
                    dead.append(token)
            except Exception:
                dead.append(token)
        for token in dead:
            self._targets.pop(token, None)

        self._job = None
        if self._targets:
            self._start()


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
