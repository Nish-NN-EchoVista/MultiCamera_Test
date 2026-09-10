"""The sub-header banner: heading, device-count caption, wordmark, legend.

Relocated from `app.py._build_subheader` unchanged.

**A builder, not a widget subclass** -- ruled in `VIEW_CONTRACT.md`. It
constructs into the parent handed to it and is not itself a `CTkFrame`.
Making it one would be the idiomatic "extract a widget" shape and is
deliberately rejected: `bar`'s class changing from `CTkFrame` to `SubHeader`
changes every Tk pathname beneath it, and because Tk auto-names are
per-parent-per-class and creation-order dependent, it would also renumber
`App`'s other `CTkFrame` children -- the scrollable list frame among them. The
structural diff would then show changes to widgets nobody touched, in exactly
the run where a real re-parenting is what we are watching for.

**Creation order is load-bearing** for the same reason and is preserved
exactly: bar, row, left, heading, caption, legend frame, logo, legend items,
rule.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import fonts, icons, theme
from ..widgets.view_toggle import ViewToggle

#: Gap between legend entries. gap-5 in the design.
LEGEND_GAP = 20


class SubHeader:
    """px-8 pt-4 pb-2.5 flex items-end justify-between border-b

    Exposes `caption` and `logo` because both are facade attributes on `App`
    (`_caption`, `_logo`). `logo` is `None` when the wordmark fails to load,
    which is why `App` assigns `_logo` conditionally -- see the note there.
    """

    def __init__(self, parent, *, on_view_change) -> None:
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x")

        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(fill="x", padx=theme.PAGE_PAD_X, pady=(16, 10))

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", anchor="s")

        ctk.CTkLabel(
            left,
            text=fonts.tracked("DEVICE CONTROLLERS", 0.12),
            font=fonts.sans(11, 600),
            text_color=theme.TEXT_SUBHEAD,
            anchor="w",
        ).pack(fill="x")

        self.caption = ctk.CTkLabel(
            left, text="", font=fonts.sans(11), text_color=theme.TEXT_WHISPER,
            anchor="w", height=15,
        )
        self.caption.pack(fill="x", pady=(2, 0))  # mt-0.5

        legend = ctk.CTkFrame(row, fg_color="transparent")
        legend.pack(side="right", anchor="s", pady=(0, 2))  # pb-0.5

        # Echovista wordmark, centred in the banner between the heading and the
        # legend.
        #
        # TRUE CENTRE, not the midpoint of the gap between its neighbours. The
        # two look almost identical at the default width (they differ by ~15px
        # here), but the midpoint is anchored to the *left block's* right edge,
        # and that block's caption changes width with the device count
        # ("1 device configured" vs "25 devices configured"). A midpoint-placed
        # logo would therefore visibly shift sideways every time a device is
        # added or removed, which is exactly the kind of movement a wordmark
        # must not do. True centre is stable against both that and window
        # resizing, and it is what "centred logo" means to the eye.
        #
        # `place` rather than `pack`, because a packed centre would be the
        # centre of the *remaining* space after its siblings, i.e. the midpoint
        # again.
        #
        # Vertically *centred*, not bottom-aligned like its neighbours: at a
        # height that fills the band there is no baseline left to share, and an
        # element spanning the full row reads correctly only when its optical
        # centre matches the row's.
        self.logo = None
        logo = icons.load_logo(theme.BANNER_LOGO_H)
        if logo is not None:
            self.logo = ctk.CTkLabel(row, text="", image=logo)
            self.logo.place(relx=0.5, rely=0.5, anchor="center")

        for index, (colour, label) in enumerate(theme.LEGEND):
            item = ctk.CTkFrame(legend, fg_color="transparent")
            item.pack(side="left", padx=(0 if index == 0 else LEGEND_GAP, 0))
            dot = ctk.CTkFrame(item, width=8, height=8, corner_radius=4,
                               fg_color=colour, border_width=0)
            dot.pack(side="left")
            dot.pack_propagate(False)
            ctk.CTkLabel(item, text=label, font=fonts.sans(10.5),
                         text_color=theme.TEXT_GHOST).pack(side="left", padx=(6, 0))

        # The view selector, built LAST and packed to the right of the row.
        #
        # Created last on purpose, though it turns out not to matter here:
        # `ViewToggle` is its own class, so Tk gives it the `!viewtoggle` name
        # series and it cannot renumber the `!ctkframe` siblings whatever the
        # order. Verified after the change rather than assumed -- `_caption`
        # keeps `.!ctkframe.!ctkframe.!ctkframe.!ctklabel2`.
        #
        # Packed after the legend so the legend keeps the exact position it
        # had, and the toggle appears just inboard of it. This is the first
        # change in the refactor that deliberately alters what is on screen,
        # so the visual comparator's job changes from "nothing moved" to
        # "only the toggle appeared".
        self.view_toggle = ViewToggle(row, on_change=on_view_change)
        self.view_toggle.pack(side="right", anchor="s", padx=(0, 16),
                              pady=(0, 2))

        ctk.CTkFrame(bar, height=1, fg_color=theme.SUBHEADER_RULE,
                     corner_radius=0).pack(fill="x")

    def set_caption(self, text: str) -> None:
        """Set the device-count line.

        The *wording* belongs to the active view, which composes it in
        `caption(total)` -- each view describes its own contents. The
        sub-header only places it. Slice 4 moved the composition out of the
        shell for that reason.
        """
        self.caption.configure(text=text)
