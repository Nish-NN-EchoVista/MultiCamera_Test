# UI spec — Figma Make export → CustomTkinter

Reference: `src/App.tsx` + `src/index.css` in the repo root (a Figma Make
React export). This page records how each part of it maps onto Tk, so the port
can be re-checked against the design without re-reading the JSX.

Companion page: [`PLACEHOLDERS.md`](PLACEHOLDERS.md) — stubs and open questions.

## Surfaces

<!-- BEGIN GENERATED: surfaces -->
| Role | Token | Hex | Source |
|---|---|---|---|
| Page | `APP_BG` | `#12141b` | `bg-[#12141b]` |
| Title bar | `TITLEBAR_BG` | `#0c0e14` | `bg-[#0c0e14]` |
| Device card | `CARD` | `#181c25` | `bg-[#181c25]` |
| Inputs / temp button | `INPUT_BG` | `#111419` | `bg-[#111419]` |
| Auto button (off) | `AUTO_OFF_BG` | `#1c1f29` | `bg-[#1c1f29]` |
<!-- END GENERATED: surfaces -->

## Flattened translucency

Tk has no alpha compositing, so every `colour/NN` in the design is composited
against the surface it sits on. These are **computed at import time** by
`theme.over()` rather than hard-coded, so each one still names its Tailwind
origin — see `emcc/theme.py`.

<!-- BEGIN GENERATED: blends -->
| Role | Tailwind source | Flattened |
|---|---|---|
| Clean off fill | `bg-blue-700/15 on card` | `#142e41` |
| Clean off fill hover | `bg-blue-700/25` | `#123b54` |
| Clean off border | `border-blue-500/25` | `#1a425c` |
| Active fill | `bg-emerald-700/25` | `#163920` |
| Active fill hover | `bg-emerald-700/35` | `#14441f` |
| Active border | `border-emerald-500/35` | `#175d22` |
| Conn idle ring | `ring-blue-700/40` | `#0e4d70` |
| Conn connected ring | `ring-emerald-700/40` | `#144a1e` |
| Conn disconnected ring | `ring-red-700/40` | `#581c21` |
| Caption connected | `text-emerald-500/60` | `#168b20` |
| Caption disconnected | `text-red-500/70` | `#ae383b` |
| Temp alert border | `border-red-600/35 on input` | `#581a1e` |
| Temp alert border hover | `border-red-500/55` | `#8b2e31` |
| Temp alert caption | `text-red-500/60` | `#993438` |
| HIGH badge fill | `bg-red-600/20` | `#3a181c` |
| HIGH badge border | `border-red-500/30 on badge` | `#702528` |
| Logo fill | `bg-blue-600/20 on titlebar` | `#0a2d42` |
| Logo border | `border-blue-500/30` | `#11405a` |
| EMCC fill | `bg-blue-600/18` | `#0a2a3e` |
| EMCC border | `border-blue-500/25` | `#10384f` |
| EMCC text | `text-blue-400/90 on badge` | `#41b3ec` |
| Header alert text | `text-red-400/80` | `#c95d5e` |
| Legend idle | `bg-blue-600/70 on app` | `#057cb8` |
| Legend connected | `bg-emerald-600/70` | `#117e18` |
| Legend disconnected | `bg-red-600/70` | `#9f2123` |
| Add hover fill | `bg-blue-600/[0.03]` | `#111822` |
| Add hover border | `border-blue-600/30` | `#0d415e` |
| Add hover text | `text-blue-500/70` | `#1a86bd` |
| Add circle hover | `border-blue-600/40` | `#0a5279` |
| Input focus border | `focus:border-blue-500/50` | `#18648c` |
<!-- END GENERATED: blends -->

Base ramps: `red` is Tailwind's 300–700 (sRGB equivalents of the v4 OKLCH
ramps). `blue` and `emerald` are **Echovista brand ramps**, derived from
`#00a9fc` and `#11ab17` — see the two sections at the end of this page. The
`EMERALD_*` names are retained deliberately and no longer mean Tailwind
emerald.

## Metrics

| Property | Value | Source |
|---|---|---|
<!-- BEGIN GENERATED: metrics -->
| Title bar height | 52 | `h-[52px]` |
| Card min height | 142 | `min-h-[142px]` |
| Card radius | 12 | `rounded-xl` |
| Control radius | 8 | `rounded-lg` |
| Badge radius | 4 / 6 | `rounded / rounded-md` |
| Page padding | 32 x, 16 y | `px-8 py-4` |
| Card gap | 12 | `gap-3` |
| Card padding | 20 x | `px-5` |
| Label→control gap | 10 | `gap-2.5` |
| Column widths | 188 / 176 / 162 / flex / 148 | `w-[...]` |
| Scroll threshold | 4 devices | `devices.length >= 4` |
<!-- END GENERATED: metrics -->
| Separator | 1px, 4px margins | `w-px mx-1` |
| Capacity | none | the export's "supports up to 25" was a demo limit; removed |

The rows between the markers are generated from `theme.py` by
`tools/gen_ui_spec.py`. The rows below them are hand-written because their
values are not `theme` constants:

* `Separator` -- the 1px width and 4px margins are literals in `device_card.py`.
* `Capacity` -- prose.

`Badge radius` reads `4 / 6` because **there are two badges**: the red alert
chip is `theme.BADGE_RADIUS = 4` (`buttons.py`, `dashboard_view.py`) and the
EMCC wordmark is `theme.EMCC_RADIUS = 6` (`title_bar.py`). Both appear in the
design, as `rounded` and `rounded-md`. It was hand-written until `17a27ba`,
while the wordmark's radius was still a literal; both values are constants now
and the row is generated like any other.

Neither the count of generated rows nor of hand-written ones is stated here,
deliberately. That count stood in four places and every one of them went stale
the moment the badge was wired -- by a change correct in both lanes that made
it, because neither lane owned the files explaining the literal. The markers
already show which rows are which.

Inner column insets are reproduced as designed: the section *labels* sit flush
to each column's left edge while the controls below them are inset (`px-4` on
IP and Connection, `px-5` on Actions, `pl-4` on Temperature). That asymmetry is
in the export.

## Typography

Design: **Inter** 300–700 and **JetBrains Mono** 400–600, pulled from Google
Fonts in `index.css`. Neither ships with Windows.

`emcc/fonts.py` resolves the best available family at runtime:

- sans: Inter → Segoe UI Variable Text → Segoe UI → Tahoma
- mono: JetBrains Mono → Cascadia Mono → Consolas → Courier New

Install Inter and JetBrains Mono for exact fidelity; nothing else changes.
The app prints what it resolved on startup.

Two Tk mismatches are handled there:

1. **Weights** — Tk exposes only `normal`/`bold`. 600/700 map to bold,
   300/400/500 to normal, except where a named intermediate face exists
   (`Segoe UI Semibold`, `Inter Medium`), which is preferred.
2. **Sizes** — Tk sizes are points when positive and *pixels* when negative.
   The design is in px, so every size is passed negatively.

Sizes used: 9, 9.5, 10, 10.5, 11, 11.5, 12.5, 13, 14, 15, 16 px.

`CTkLabel` defaults to `height=28`, which silently inflates every small label
and makes buttons ~44px tall instead of ~33. Explicit heights are set
throughout — roughly `round(1.2 x font-size)`, matching CSS `line-height:
normal`.

## Icons

The design ships no assets — every glyph is an inline `<svg>`. Rather than
export PNGs (which need regenerating per DPI and per colour), `emcc/icons.py`
redraws each from the same path data with Pillow, supersampled 8x and
downscaled, cached by `(name, colour, size)`:

| Icon | Where | Source path |
|---|---|---|
| `camera` | title bar logo | aperture: r5.5 ring, r2.5 half-opacity disc, r1 core, 4 ticks |
| `pencil` | inside the name field | `M1 9.5L7.5 3 9 4.5l-6.5 6.5H1V9.5z` |
| `check` | Clean, when active | `M1.5 6.5l3 3 6-6` |
| `warning` | title bar temp alert | `M6 1L11 10H1L6 1z` + stem + dot |
| `trash` | remove control, replacing the pencil | not from the export; drawn at the pencil's weight |

## Motion

| Design | Implementation |
|---|---|
| Auto toggle knob, `transition-all duration-200` | `anim.Tween`, 200ms eased, knob slides x=2→14 |
| `transition-colors duration-200` on hover | applied instantly; hover changes colour in one step, with no interpolation |

Easing is smoothstep (`3t²−2t³`), visually indistinguishable from CSS
`ease-in-out` at these durations.

## Interaction

The design's buttons are composites (leading dot or icon + label with exact
gaps), which a stock `CTkButton` cannot reproduce — it has one image slot and
no gap control. Each is therefore a frame plus children, with hover and click
supplied by `widgets/interactive.py`.

That module exists because of two Tk behaviours:

1. A child widget swallows pointer events, so a bare `<Leave>` on the container
   fires the moment the pointer crosses onto a child. Handlers are bound across
   the whole subtree, and `<Leave>` re-tests the actual pointer position
   against the container's bounds before dropping hover.
2. Widgets created *after* binding (the Clean checkmark appearing) would miss
   the bindings, so `refresh()` re-walks the tree on every state change.

Click fires on button-1 *release while still inside*, matching a real button.

## Window chrome

The design draws its own macOS-style traffic lights, so the native caption is
removed by clearing `WS_CAPTION` via ctypes — **not** `overrideredirect(True)`,
which would also cost taskbar presence, snap, the minimise animation and resize
borders. `WS_THICKFRAME` is kept, so resizing and snapping keep working
natively and the lights only drive close / minimise / maximise.

Red closes, amber minimises, green toggles maximise; dragging anywhere on the
bar moves the window and double-clicking it toggles maximise.
`overrideredirect` remains as a non-Windows fallback.

## Known approximations

| Design feature | Status |
|---|---|
| `shadow-[0_2px_16px_rgba(0,0,0,0.35)]` on cards | dropped — no box-shadow in Tk; contributes little on a near-black ground |
| `focus:ring-blue-500/20` on inputs | dropped; the `focus:border-blue-500/50` change is kept. The `FOCUS_RING` token was removed too (2026-09-10), so no constant remains for it |
| `tracking-[0.14em]` / `[0.12em]` micro-labels | approximated by inserting hair spaces (`fonts.tracked`) — Tk fonts have no letter-spacing |
| Scrollbar 5px | 8px; `CTkScrollbar` stops rendering a usable thumb below that |
| Knob `shadow` on the Auto toggle | dropped — invisible at 12px on a white knob |
| Scrollbar auto-hide | always visible when scrollable; the CSS comment claims auto-hide but the rule does not implement it |

## Gotchas worth knowing

**`place()` scales, `place_configure()` does not.** `place()` applies
CustomTkinter's widget-scaling factor; `place_configure()` passes the value
straight through. Reconfiguring a placed widget with logical pixels therefore
writes them into a device-pixel slot and under-offsets by the scaling factor
(2.25x on the development display). `_Section._place` re-calls `place()` for
this reason.

**Hover must never touch geometry.** Each control splits its update into
`_paint()` (colours) and `render()` (colours + which children are packed), and
hover calls `_paint()` only. When hover called `render()`, the pack/pack_forget
reconciliation re-ran on every pointer enter and leave and the Clean/Auto
buttons visibly expanded and jittered under the cursor.

**`Interactive.refresh()` must be idempotent.** Tk's `bind(add="+")` appends.
Because `refresh()` is called from `render()`, which was called from the hover
handler, re-binding the whole subtree each time doubled the handler count per
hover — one mouse move eventually firing `render()` hundreds of times. It now
tracks which widgets are already bound.

**`CTkFrame.bind()` binds on the frame's internal canvas, not the frame.** So
`some_frame.event_generate("<Enter>")` does *not* reach a handler registered
via `some_frame.bind("<Enter>", ...)` — dispatch to `some_frame._canvas`
instead. This matters when testing hover behaviour: driving the handler
directly, or generating the event on the frame, both silently bypass the real
dispatch path and will make a broken build look fine.

**`winfo_ismapped()` is not a reliable source of truth** for "is this child
packed". It reports stale values until Tk has processed the geometry queue, so
child presence is tracked in plain Python booleans.

## Adding a device: what makes it fast

A device card is ~88 CustomTkinter widgets, and essentially all of the cost is
Tcl round-trips (a profile attributes ~0.76s of tottime to
`_tkinter.tkapp.call`). Three things follow from that, all of them load-bearing:

> **On the figures in this section.** The *counts* -- ~88 widgets and the ~24
> grouping frames below -- were re-verified on 2026-09-10 and are exact;
> counting is method-independent. The *timings* were measured against code
> that has since been removed, and they predate this project's git history,
> so the date they were taken is not recoverable. Read them as the record of
> why the current design won, not as reproducible measurements.

1. **Never rebuild the list to add one row.** The original `_rebuild()`
   destroyed and recreated every card on every add: 3.0s for the 4th device
   rising to 8.0s for the 11th, plus a visible full-list flash.
   `DeviceManager.add_device` returns the single new device and
   `App._add_device` inserts one card through `ListView.new_card`, `before=`
   the add button. Now flat at ~0.75s regardless of count,
   and existing cards are pixel-identical across an add.
2. **The add button and scroll hint are created once.** They are permanent
   children, shown/hidden rather than rebuilt.
3. **Don't measure-then-move.** `align_labels()` re-`place`s every column,
   which moves already-drawn widgets and forces a *second* full redraw of the
   card (~178 rounded-rect draws instead of ~60). The offset only depends on
   which columns carry a caption, so `App._offsets` caches it per variant and
   passes it to the constructor -- later cards are positioned correctly as they
   are built and never measured at all.

Also: `_set_scrollbar_visible` is guarded on current state, because re-applying
`grid()` to an already-gridded widget re-runs its geometry.

**A reveal animation was tried and removed.** Animating the new card's height
inside a `CTkScrollableFrame` changes the scrollregion every frame, which calls
`CTkScrollbar.set` -> `_draw` and cascades a `_draw` over every descendant
frame. That is more work *per frame* than building the whole card, so the
"smooth" reveal was measurably jankier than a single-shot insert. (Measured
against code since removed; date not recoverable. The same claim appears in
`device_card.py`'s "no reveal/grow animation" note -- the two were written
together, so neither corroborates the other.)

**Remaining headroom, not taken:** ~24 of the 88 widgets per card are
transparent border-less `CTkFrame`s used purely for grouping, and they render
nothing. A `CTkFrame(fg_color="transparent")` costs materially more to
construct than a raw `tk.Frame` -- enough that swapping those 24 would cut a
real slice of the remaining build cost.

**The size of that gap is method-dependent and should not be quoted as a
single number.** Measured 2026-09-10 (Worker 2, CustomTkinter 5.2.2, 200
constructions per run): `CTkFrame(fg_color="transparent")` takes 5-8ms
depending on whether widgets are destroyed between constructions and whether
the parent is reused; `tk.Frame` is below ~0.02ms, at the resolution floor,
where run-to-run variation is the same order as the value being measured. The
ratio across those methods spans **3x to 744x**. Nothing in the codebase
depends on which end of that range is right -- the decision below holds at 3x.

It was left alone deliberately:
CustomTkinter applies widget scaling inside its own `pack`/`place`/`grid`
overrides, so every pad, width and height on a raw `tk` widget would need
scaling by hand, and getting one wrong is a silent fidelity regression on a
HiDPI display. Worth doing, but as its own change with its own visual diff.

---

# Backend integration additions

Added when the networking layer landed. Everything below either reuses an
existing design token or is documented as a deliberate deviation in
[`PLACEHOLDERS.md`](PLACEHOLDERS.md).

## Six backend states, three design treatments

The export has three Connect-button looks; the backend has six states.
`widgets/connection_view.py` is the single place that maps them, so no widget
ever has to guess:

| Treatment | States | Label | Caption |
|---|---|---|---|
| `idle` (blue) | DISCONNECTED | `Connect` | `Not connected` / `No IP address set` |
| `idle` (blue) | CONNECTING | `Connecting…` | `Opening connection` |
| `connected` (green) | CONNECTED | `Connected` | `Stream active` |
| `fault` (red) | RECONNECTING | `Reconnecting…` | `Reconnecting 2/3…` |
| `fault` (red) | ERROR | `Reconnect` | the short error text |

No new colour token was introduced: `CONNECTING` borrows the blue treatment and
`RECONNECTING` the red one, distinguished by label and caption alone.

The export's third state was named `disconnected`; it is now `fault`, because
it covers "failed" and "retrying" as well, and `DISCONNECTED` means something
different in the backend (the operator asked for it, which renders blue).

## New elements

| Element | Treatment |
|---|---|
| Remove control | Replaces the decorative pencil at `right-2.5` inside the Device Name field. `TEXT_GHOST` at rest, `RED_400` on hover — a destructive control should not be the loudest thing on the row. Drawn at the pencil's 1.2 stroke weight on an 11-unit box. |
| Temperature placeholder | `—°C` in `TEXT_GHOST` until the first sample. Keeps the button's metrics; the HIGH badge is hidden. |
| Error / confirm dialogs | `widgets/dialogs.py`, built only from existing tokens: `APP_BG` ground, the tracked micro-label as a heading, and buttons reusing the connection button's blue/red fills and the Auto-off grey for "Cancel". |

## Changed values

| | Was | Now | Why |
|---|---|---|---|
| Temperature threshold | `TEMP_ALERT = 70` in theme | `settings.temperature_warning_c`, default 65 | runtime configuration, not a design token |
| Threshold comparison | `>=` | `>` | spec: strictly greater turns red, so exactly 65 reads normal |
| Temperature precision | integer | one decimal | the wire carries tenths |
| Device cap | `MAX_DEVICES = 25` | removed | spec requires no maximum; 25 was a demo limit |
| Scroll hint | "supports up to 25" | device count only | follows from the above |
| Clean icon slot | packed only when active | always reserved | Clean now toggles on every press and on timeout; a changing width would shunt Auto sideways repeatedly |

## Rendering path

A card is a **view**. `card.render()` reads `DeviceState` and repaints; it holds
no device state and decides nothing. The event pump calls it only for devices
that actually changed in that tick.

One subtlety: a card gaining or losing the "Exceeds threshold" caption changes
its tallest column, so its shared label baseline moves. `App._render_device`
re-applies the cached offset for the card's new variant rather than
re-measuring, for the reason given in ARCHITECTURE.md.

---

# Brand identity additions

Added after the backend landed, at the client's direction. Both depart from the
Figma export deliberately.

## The blue is the brand blue, not Tailwind's

The blue controls are matched to the "echovista" wordmark in
`assets/echo_logo_dark.png`:

```
BRAND BLUE = #00a9fc     hue 199.8deg, lightness 49.4%, saturation 100%
(was Tailwind blue-600 = #2563eb, hue 221deg, saturation 83%)
```

Sampled as the **modal** colour of the logotype's flat fill, not an average —
averaging would drag the value off by mixing in glyph-edge antialiasing.

The ramp is *derived* rather than hand-picked. The brand blue is anchored at the
600 step (the primary fill) and the other steps reuse Tailwind's own lightness
offsets from 600 (+25.1, +14.5, +6.5, 0, −5.3 percentage points) at the brand
hue and saturation:

| Step | Was (Tailwind) | Now (brand) | Role |
|---|---|---|---|
| 300 | `#93c5fd` | `#7dd4ff` | connection status dot |
| 400 | `#60a5fa` | `#47c2ff` | Clean label, title-bar mark |
| 500 | `#3b82f6` | `#1eb5ff` | Connect hover |
| 600 | `#2563eb` | **`#00a9fc`** | **Connect fill — the brand blue** |
| 700 | `#1d4ed8` | `#0097e1` | rings, Clean fill base |

Keeping the lightness *relationships* preserves every contrast pairing the
design was built on — fill vs hover, text vs fill, ring vs card — while
re-hueing the family. And because the flattened translucent tokens are computed
from these by `over()`, they all re-derived themselves with no separate edit;
that is what the computed-token design in `theme.py` was for.

Verified in the rendered window: the Connect fill samples `#00a9fc`, identical
to the source asset. The rendered *logo* samples `#00a7fa` — a 2/255 delta
caused by CTkImage downscaling the PNG ~5×, not by a colour mismatch.

Everything blue follows: the Clean button, the connection dot, the EMCC badge,
the Idle legend dot, the input focus ring, the title-bar mark and the Add Device
hover.

## The green is the brand green, not Tailwind's emerald

Specified directly by Nish rather than sampled from an asset:

```
BRAND GREEN = #11ab17     hue 122.3deg, lightness 36.9%, saturation 81.9%
(was Tailwind emerald-600 = #059669, hue 161.4deg, lightness 30.4%, saturation 93.5%)
```

That is −39.0° of hue, −11.6 points of saturation and **+6.5 points of
lightness**. The family moves from a blue-leaning teal to a true green *and
gets lighter as it does* — the lightness shift is easy to miss when reading the
hex alone, and it lifts every derived token with it.

Derived the same way as the blue, and anchored at the 600 step. **The offsets
are emerald's own (+36.5, +21.2, +9.0, 0, −6.1), not blue's** — the rule is
that a re-hued ramp reuses the offsets of *the family it replaces*, and
Tailwind's emerald ramp is materially wider than its blue one, 36.5 points to
the 300 step against 25.1. Borrowing blue's offsets would have compressed the
family and flattened it, for a reason nobody could later reconstruct from the
values. This was got wrong once in the instructions before it was caught:

| Step | Was (Tailwind emerald) | Now (brand) | Role |
|---|---|---|---|
| 300 | `#6ee7b7` | `#83f388` | connected status dot |
| 400 | `#34d399` | `#3cec43` | Clean/Auto "on" label, System Online dot |
| 500 | `#10b981` | `#15d51d` | Auto toggle track, "on" border base |
| 600 | `#059669` | **`#11ab17`** | **Connected hover — the brand green** |
| 700 | `#047857` | `#0e8f13` | Connected fill, rings, "on" fill base |

Measured against the relationships the design already had, emerald's offsets
track them more closely on every pair than the compressed alternative would:
`ON_TEXT`/`ON_BG` 8.09 against the old 7.06, connected dot against card 12.29
against 11.19, `ON_BORDER` against card 2.13 against 1.91.

The derivation procedure was validated by running it **backwards** first —
re-deriving `BLUE_300..700` from `#00a9fc` reproduces all five existing blue
constants exactly, which is what established that the method was sound before
it was trusted forwards on a new hue.

As with the blue, the flattened translucent tokens re-derived themselves
through `over()` with no separate edit: `ON_BG`, `ON_BG_HOVER`, `ON_BORDER`,
`ON_TEXT`, the whole of `CONN["connected"]`, `TOGGLE_TRACK_ON`, `ONLINE_DOT`
and the Connected legend dot.

Verified in the rendered window — **measured by the Tester** on the post-green
visual capture, by pixel-level diff, not asserted by the author of the change:

| Sampled | Pixels | Token |
|---|---|---|
| `#0e8f13` | 22,025 | `CONN["connected"]["bg"]` |
| `#11ab17` | 4,405 | hover / `BRAND_GREEN` |
| `#15d51d` | 1,021 | `TOGGLE_TRACK_ON` |
| `#3cec43` | 603 | `ONLINE_DOT` / `ON_TEXT` |
| `#117e18` | 456 | Connected legend dot |

Everything green follows: the Connected button and its ring, the connected
dot, Clean and Auto when active, the Auto toggle track, the System Online dot
and the Connected legend dot.

**The `EMERALD_*` names are kept on purpose.** They are now inaccurate — this
is not Tailwind emerald — but they are read from `buttons.py`,
`dashboard_view.py`, `device_card.py` and `title_bar.py`, so renaming them
spans both workers' lanes. The rename is queued as its own task rather than
done mid-refactor for zero behavioural gain.

## Banner wordmark

`assets/echo_logo_dark.png`, loaded by `icons.load_logo()` at
`theme.BANNER_LOGO_H` (62px, ~182px wide).

**Cropped to its alpha bounding box first.** The source is a 1968×671 mark
inside a 2172×724 canvas with asymmetric transparent padding, so sizing the
canvas would make the visible mark ~4% smaller than requested and off-centre.

**Placed at the row's true centre, not the midpoint of the gap between its
neighbours.** Measured, the two differ by ~55px at the default width, and the
midpoint *moves* as the caption text changes length:

| devices | logo centre (true) | gap midpoint |
|---|---|---|
| 1 | 608 | 661 |
| 3 | 608 | 664 |
| 25 | 608 | 667 |

A midpoint-anchored wordmark would visibly shift sideways every time a device
was added. True centre is stable against that and against window resizing.
`place(relx=0.5)` is used because a *packed* centre would be the centre of the
space remaining after its siblings — i.e. the midpoint again.

**It does not change the banner's height.** Because it is placed, it is outside
geometry propagation: the row stays 45.3px and the first card stays at y=140.4
at every logo size tested (26/52/62/80). The mark simply overflows into the
row's existing 16px/10px padding. That padding is also the ceiling — at 62 it
extends ~8.4px above the row, but by 80 it would extend ~17.3px and breach the
band into the title bar's rule.

A missing or corrupt asset logs a warning and omits the logo; it never stops the
app from starting.
