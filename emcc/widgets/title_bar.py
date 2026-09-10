"""Custom title bar (h-[52px] bg-[#0c0e14] border-b border-[#171a22]).

The design draws its own macOS-style traffic lights, so the app runs without
native window chrome and this bar provides the real behaviour: red closes,
amber minimises, green toggles maximise, and dragging anywhere on the bar moves
the window (double-click also toggles maximise, as on both platforms).

The right-hand status cluster is assembled from separate labels because each
run has its own colour -- Tk labels cannot carry mixed inline colour the way
the original's nested spans do.
"""

from __future__ import annotations

from typing import Callable

import customtkinter as ctk

from .. import fonts, icons, theme

DOT = 12          # w-[12px] h-[12px]
GAP = 6           # gap-[6px]
PIP_H = 14        # h-3.5


class TitleBar(ctk.CTkFrame):
    def __init__(self, master, on_close: Callable[[], None],
                 on_minimise: Callable[[], None], on_maximise: Callable[[], None],
                 on_drag_start: Callable, on_drag_move: Callable):
        super().__init__(master, height=theme.TITLEBAR_H, fg_color=theme.TITLEBAR_BG,
                         corner_radius=0)
        self.pack_propagate(False)

        # px-5, gap-4
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=20)

        self._build_traffic(row, on_close, on_minimise, on_maximise)
        self._build_identity(row)
        self._build_status(row)

        # 1px bottom rule.
        ctk.CTkFrame(self, height=1, fg_color=theme.TITLEBAR_RULE, corner_radius=0)\
            .pack(side="bottom", fill="x")

        # Drag anywhere that is not a control.
        for target in (self, row, self._identity, self._title, self._status):
            target.bind("<Button-1>", on_drag_start, add="+")
            target.bind("<B1-Motion>", on_drag_move, add="+")
            target.bind("<Double-Button-1>", lambda _e: on_maximise(), add="+")

    # -- traffic lights ---------------------------------------------------

    def _build_traffic(self, parent, on_close, on_minimise, on_maximise) -> None:
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(side="left", padx=(0, 12))  # mr-3

        actions = (on_close, on_minimise, on_maximise)
        for i, ((fill, border), action) in enumerate(zip(theme.TRAFFIC, actions)):
            dot = ctk.CTkFrame(
                holder, width=DOT, height=DOT, corner_radius=DOT // 2,
                fg_color=fill, border_width=1, border_color=border,
            )
            dot.pack(side="left", padx=(0 if i == 0 else GAP, 0))
            dot.pack_propagate(False)
            dot.configure(cursor="hand2")
            dot.bind("<Button-1>", lambda _e, fn=action: fn())

    # -- identity ---------------------------------------------------------

    def _build_identity(self, parent) -> None:
        self._identity = ctk.CTkFrame(parent, fg_color="transparent")
        self._identity.pack(side="left")

        # w-7 h-7 rounded-lg bg-blue-600/20 border-blue-500/30
        logo = ctk.CTkFrame(
            self._identity, width=28, height=28, corner_radius=theme.CTRL_RADIUS,
            fg_color=theme.LOGO_BG, border_width=1, border_color=theme.LOGO_BORDER,
        )
        logo.pack(side="left")
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="", image=icons.get("camera", theme.LOGO_FG, 14))\
            .place(relx=0.5, rely=0.5, anchor="center")

        self._title = ctk.CTkLabel(
            self._identity, text=theme.APP_TITLE, font=fonts.sans(14, 600),
            text_color=theme.TEXT_TITLE,
        )
        self._title.pack(side="left", padx=(10, 0))  # gap-2.5

        badge = ctk.CTkFrame(
            self._identity, corner_radius=6, fg_color=theme.EMCC_BG,
            border_width=1, border_color=theme.EMCC_BORDER,
        )
        badge.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(
            badge, text=fonts.tracked("EMCC", 0.12), font=fonts.sans(10, 700),
            text_color=theme.EMCC_TEXT,
            height=12,
        ).pack(padx=8, pady=2)  # px-2 py-0.5

    # -- status cluster ---------------------------------------------------

    def _pip(self, parent) -> ctk.CTkFrame:
        """w-px h-3.5 bg-[#1e2230] -- the small vertical rule between runs."""
        pip = ctk.CTkFrame(parent, width=1, height=PIP_H, fg_color=theme.DIVIDER_PIP,
                           corner_radius=0)
        pip.pack_propagate(False)
        return pip

    def _build_status(self, parent) -> None:
        self._status = ctk.CTkFrame(parent, fg_color="transparent")
        self._status.pack(side="right")

        # -- temperature alerts (conditional) --
        self._alert_wrap = ctk.CTkFrame(self._status, fg_color="transparent")
        ctk.CTkLabel(
            self._alert_wrap, text="", image=icons.get("warning", theme.HEADER_ALERT_TEXT, 11)
        ).pack(side="left")
        self._alert_label = ctk.CTkLabel(
            self._alert_wrap, text="", font=fonts.sans(11.5),
            text_color=theme.HEADER_ALERT_TEXT,
        )
        self._alert_label.pack(side="left", padx=(6, 0))  # gap-1.5
        self._alert_pip = self._pip(self._status)

        # -- System Online --
        online = ctk.CTkFrame(self._status, fg_color="transparent")
        self._online_dot = ctk.CTkFrame(online, width=6, height=6, corner_radius=3,
                                        fg_color=theme.ONLINE_DOT, border_width=0)
        self._online_dot.pack(side="left")
        self._online_dot.pack_propagate(False)
        ctk.CTkLabel(online, text="System Online", font=fonts.sans(11.5),
                     text_color=theme.TEXT_MUTED).pack(side="left", padx=(6, 0))
        # The dot is static since 2026-09-10. `theme.ONLINE_DOT` above is
        # exactly what the ticker painted at frame 0 -- verified,
        # `theme.over(c, bg, 1.0) == c` -- so dropping the animation leaves
        # the resting colour unchanged.

        # -- connected count: four runs, four colours --
        count = ctk.CTkFrame(self._status, fg_color="transparent")
        self._count_connected = ctk.CTkLabel(count, text="0", font=fonts.sans(11.5, 600),
                                             text_color=theme.TEXT_COUNT)
        self._count_connected.pack(side="left")
        ctk.CTkLabel(count, text=" / ", font=fonts.sans(11.5),
                     text_color=theme.TEXT_SCROLL_HINT).pack(side="left")
        self._count_total = ctk.CTkLabel(count, text="0", font=fonts.sans(11.5),
                                         text_color=theme.TEXT_DIMMER)
        self._count_total.pack(side="left")
        ctk.CTkLabel(count, text="connected", font=fonts.sans(11.5),
                     text_color=theme.TEXT_FAINT).pack(side="left", padx=(4, 0))  # ml-1

        version = ctk.CTkLabel(self._status, text=theme.APP_VERSION, font=fonts.mono(10.5),
                               text_color=theme.TEXT_GHOST)

        # Fixed left-to-right order; the alert pair is spliced in when needed.
        self._pack_order = [online, self._pip(self._status), count,
                            self._pip(self._status), version]
        self._alerts_shown = False
        self._relayout(alerts=False)

    def _relayout(self, alerts: bool) -> None:
        """Re-pack the cluster. Cheap, and avoids fragile `before=` juggling."""
        run = ([self._alert_wrap, self._alert_pip] if alerts else []) + self._pack_order
        # Unpack the alert pair unconditionally, not just the widgets being
        # re-packed. Forgetting only `run` leaves them packed when hiding, and
        # re-packing the rest *after* them keeps the chip on screen -- so the
        # count would drop to zero while "1 temp alert" stayed visible.
        for widget in (self._alert_wrap, self._alert_pip, *self._pack_order):
            widget.pack_forget()
        for widget in run:
            # gap-4 between runs == 8px either side of each pip.
            pad = 8 if widget in (self._alert_pip, *self._pack_order[1::2]) else 0
            widget.pack(side="left", padx=pad)
        self._alerts_shown = alerts

    # -- updates ----------------------------------------------------------

    def update_from(self, manager) -> None:
        """Read the device counts straight off the manager.

        Slice 4 moved the counts here. The shell used to compute
        `connected_count`, `len(devices)` and `alert_count()` and pass all
        three, which made every count change a two-place edit -- the title bar
        knowing which numbers it displays is the whole point of it owning
        them.

        `update_status` stays as the explicit-values entry point: the tests
        drive alert states through it directly, and a widget that can only be
        fed a live manager is harder to test than one that accepts numbers.
        """
        self.update_status(
            connected=manager.connected_count,
            total=len(manager.devices),
            alerts=manager.alert_count(),
        )

    def update_status(self, connected: int, total: int, alerts: int) -> None:
        self._count_connected.configure(text=str(connected))
        self._count_total.configure(text=str(total))

        if alerts > 0:
            self._alert_label.configure(
                text=f"{alerts} temp alert{'s' if alerts > 1 else ''}"
            )
        if (alerts > 0) != self._alerts_shown:
            self._relayout(alerts > 0)
