"""Hover / press behaviour for composite widgets.

The design's buttons are not single controls -- each is a frame containing a
dot or icon plus a label, because Tk cannot reproduce the exact gap and
alignment of the originals with a stock button. That creates two problems this
module solves:

1. A child widget swallows pointer events, so a bare <Leave> on the container
   fires spuriously the moment the pointer crosses onto a child. Every handler
   is therefore bound across the whole subtree, and <Leave> re-tests the actual
   pointer position against the container's bounds before dropping hover.
2. Widgets created *after* binding (a label swapped in on a state change) would
   miss the bindings, so `refresh` re-walks the tree.
"""

from __future__ import annotations

from typing import Callable

CURSOR = "hand2"


def bind_tree(widget, sequence: str, handler, add: bool = True) -> None:
    """Bind `sequence` on `widget` and every descendant."""
    widget.bind(sequence, handler, add="+" if add else "")
    for child in widget.winfo_children():
        bind_tree(child, sequence, handler, add)


def set_cursor_tree(widget, cursor: str = CURSOR) -> None:
    try:
        widget.configure(cursor=cursor)
    except Exception:
        pass
    for child in widget.winfo_children():
        set_cursor_tree(child, cursor)


class Interactive:
    """Tracks hover/press for a composite and reports transitions.

    `on_hover(bool)` fires on genuine enter/leave of the composite as a whole.
    `on_click()` fires on button-1 release while still inside, matching how a
    real button behaves when you press, drag off, and release.
    """

    def __init__(self, root, on_hover: Callable[[bool], None] | None = None,
                 on_click: Callable[[], None] | None = None, cursor: bool = True):
        self.root = root
        self._on_hover = on_hover
        self._on_click = on_click
        self.hovered = False
        self._pressed = False
        self._cursor = cursor
        self._bound: set[str] = set()
        self.refresh()

    # -- binding ----------------------------------------------------------

    def refresh(self) -> None:
        """Bind any widget in the subtree that is not bound yet.

        MUST be idempotent. Tk's bind(add="+") appends, so a refresh that
        re-bound the whole subtree every time would double the handler count on
        each call -- and because refresh() runs from render(), which runs from
        the hover handler, that compounds: one hover becomes two handlers, then
        four, then eight. Within a dozen hovers a single mouse move fires
        render() thousands of times and the pack()/pack_forget() inside it
        thrashes the layout. Hence `_bound`.
        """
        self._bind_new(self.root)

    def _bind_new(self, widget) -> None:
        key = str(widget)
        if key not in self._bound:
            self._bound.add(key)
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            if self._on_click is not None:
                widget.bind("<Button-1>", self._press, add="+")
                widget.bind("<ButtonRelease-1>", self._release, add="+")
                if self._cursor:
                    try:
                        widget.configure(cursor=CURSOR)
                    except Exception:
                        pass
        for child in widget.winfo_children():
            self._bind_new(child)

    # -- geometry ---------------------------------------------------------

    def _inside(self) -> bool:
        w = self.root
        if not w.winfo_exists():
            return False
        px, py = w.winfo_pointerxy()
        x, y = w.winfo_rootx(), w.winfo_rooty()
        return x <= px < x + w.winfo_width() and y <= py < y + w.winfo_height()

    # -- handlers ---------------------------------------------------------

    def _enter(self, _event=None):
        if not self.hovered:
            self.hovered = True
            if self._on_hover:
                self._on_hover(True)

    def _leave(self, _event=None):
        # Defer: on a parent->child crossing the pointer coordinates have not
        # settled yet, and _inside would give the wrong answer.
        self.root.after(1, self._leave_check)

    def _leave_check(self):
        if self.hovered and not self._inside():
            self.hovered = False
            self._pressed = False
            if self._on_hover:
                self._on_hover(False)

    def _press(self, _event=None):
        self._pressed = True

    def _release(self, _event=None):
        if self._pressed and self._inside():
            self._pressed = False
            if self._on_click:
                self._on_click()
        self._pressed = False
