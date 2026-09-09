"""Shell concerns lifted out of `App`, as pure relocations.

`App` remains the facade: every name that `tests/` and `tools/` reach stays a
real attribute on it (see `_FACADE_NAMES` in `app.py`), and the methods here
are still reached as `app._connect_device` and so on. That is not cosmetic --
`TitleBar` and `DeviceCard` are handed **bound methods** as callbacks
(`on_minimise=self._minimise`, `on_connect=self._connect_device`), so the
methods must exist on `App` to be bound at all.

So each module here takes the `App` as its first argument and `App` keeps a
one-line method that calls it. The logic moves; the surface does not.

`SubHeader` is the exception in shape, not in principle: it is a *builder*
that constructs into a parent handed to it, per `VIEW_CONTRACT.md`. It is
deliberately not a `CTkFrame` subclass -- that would change every Tk pathname
beneath it and renumber `App`'s other `CTkFrame` children, producing a
structural diff for widgets nobody touched.
"""
