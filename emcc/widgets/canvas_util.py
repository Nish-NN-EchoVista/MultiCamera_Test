"""Canvas geometry helpers.

CTkFrame borders are always solid, but the design's "Add Device" row uses
`border-dashed` on both the outer rounded rectangle and the inner circle. Those
are therefore drawn on a raw tk.Canvas.

Canvas coordinates are device pixels and bypass CustomTkinter's widget scaling,
so `scaling()` is applied to every dimension by hand -- otherwise the row would
render at 1/2.25 size on a HiDPI display while everything around it scaled.
"""

from __future__ import annotations

import math

import customtkinter as ctk

DASH = (3, 3)  # approximates a browser's 1px dashed border


def scaling(widget) -> float:
    """CustomTkinter's widget scaling factor for this widget's display."""
    try:
        return ctk.ScalingTracker.get_widget_scaling(widget)
    except Exception:
        return 1.0


def rounded_rect_points(x0: float, y0: float, x1: float, y1: float,
                        r: float, seg: int = 8) -> list[float]:
    """Flat [x, y, ...] outline of a rounded rectangle, corners tessellated.

    Returned as one continuous closed path so a single create_line call
    produces an unbroken dash run around the whole shape.
    """
    r = min(r, abs(x1 - x0) / 2, abs(y1 - y0) / 2)
    pts: list[float] = []
    # (centre, start angle) per corner, clockwise from top-left.
    corners = [
        (x0 + r, y0 + r, 180.0),
        (x1 - r, y0 + r, 270.0),
        (x1 - r, y1 - r, 0.0),
        (x0 + r, y1 - r, 90.0),
    ]
    for cx, cy, start in corners:
        for i in range(seg + 1):
            a = math.radians(start + 90.0 * i / seg)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.extend(pts[:2])  # close
    return pts


def circle_points(cx: float, cy: float, r: float, seg: int = 32) -> list[float]:
    pts: list[float] = []
    for i in range(seg + 1):
        a = 2 * math.pi * i / seg
        pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts
