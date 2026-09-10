"""Design tokens ported from the Figma Make export (src/App.tsx + src/index.css).

Tkinter has no alpha compositing: a widget colour must be a single opaque
value. The Figma design leans heavily on translucency (bg-blue-700/15,
border-emerald-500/35, text-red-500/70, ...), so every one of those is
flattened here against the backdrop it actually sits on.

Keeping the mix explicit -- over(BLUE_700, CARD, 0.15) rather than a
pre-computed "#192440" -- means each token still traces back to the Tailwind
class it came from, so the port can be re-checked against the design later.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Alpha flattening
# --------------------------------------------------------------------------


def _to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def over(fg: str, bg: str, alpha: float) -> str:
    """Composite fg at alpha over opaque bg -- Tailwind's ``fg/NN`` modifier."""
    f, b = _to_rgb(fg), _to_rgb(bg)
    return _to_hex(tuple(round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3)))


# --------------------------------------------------------------------------
# Tailwind source palette (sRGB hex equivalents of the v4 OKLCH ramps)
# --------------------------------------------------------------------------

# --- Blue: the Echovista brand ramp, not Tailwind's ---------------------
#
# The blue controls (Connect, Clean, the connection dot, the EMCC badge, the
# Idle legend dot, the focus ring) are matched to the "echovista" wordmark in
# assets/echo_logo_dark.png. Sampled from the flat fill of the logotype -- the
# modal colour, not an average, so antialiasing at the glyph edges cannot drag
# it off:
#
#     BRAND BLUE = #00a9fc   hue 199.8deg, lightness 49.4%, saturation 100%
#
# Tailwind's blue-600 was #2563eb: hue 221deg, saturation 83%. So the brand
# blue is a distinctly cooler, fully saturated blue -- swapping it is a visible
# change, not a nudge.
#
# The ramp is *derived* rather than hand-picked: the brand blue is anchored at
# the 600 step (the primary fill), and the other steps reuse Tailwind's own
# lightness offsets from 600 (+25.1, +14.5, +6.5, 0, -5.3 percentage points) at
# the brand hue and saturation. That keeps every contrast relationship the
# design was built on -- fill vs hover, text vs fill, ring vs card -- while
# re-hueing the whole family to brand.
#
# Everything downstream is computed from these by `over()`, so the flattened
# translucent tokens re-derive themselves and need no separate edit.
BLUE_300, BLUE_400, BLUE_500 = "#7dd4ff", "#47c2ff", "#1eb5ff"
BLUE_600, BLUE_700 = "#00a9fc", "#0097e1"

#: The sampled logotype colour, kept named for traceability. Equal to
#: BLUE_600. **Deliberately has no readers** -- it exists so the anchor is
#: greppable, so a dead-name survey should keep it rather than report it.
BRAND_BLUE = BLUE_600

# --- Green: the Echovista green, NOT Tailwind emerald -------------------
#
# !! The EMERALD_* names are retained deliberately and are now inaccurate.
# !! This family is no longer Tailwind emerald. The names are read from
# !! buttons.py, dashboard_view.py, device_card.py and title_bar.py, so a
# !! rename spans both workers' lanes; it is queued as its own task rather
# !! than done in the middle of a sliced refactor, for zero behavioural gain.
#
# The green controls (the Connected button and ring, the connected dot, Clean
# and Auto when on, the Auto toggle track, the System Online dot, the
# Connected legend dot) were Tailwind emerald. Nish specified the brand green:
#
#     BRAND GREEN = #11ab17   hue 122.3deg, lightness 36.9%, saturation 81.9%
#
# Tailwind's emerald-600 was #059669: hue 161.4deg, lightness 30.4%,
# saturation 93.5%. So this is -39.0deg of hue, -11.6 points of saturation and
# **+6.5 points of lightness** -- the family moves from a blue-leaning teal to
# a true green, and gets lighter as it does. Like the blue swap, a visible
# change rather than a nudge.
#
# Derived, not hand-picked, exactly as the blue ramp above was: the brand
# green is anchored at the 600 step and the other steps reuse Tailwind's own
# lightness offsets from 600, at the brand hue and saturation.
#
# WHICH offsets is the subtle part, and it was got wrong once already:
# **emerald's** (+36.5, +21.2, +9.0, 0, -6.1), not blue's (+25.1, +14.5, +6.5,
# 0, -5.3). The instruction as first given quoted blue's. That is the one
# place the analogy breaks -- the blue ramp reused the offsets of *the family
# it replaced*, so the green ramp must reuse emerald's. Tailwind's emerald
# ramp is materially wider than its blue one (36.5 points to the 300 step
# against 25.1), so borrowing blue's would have compressed the family and
# flattened it for no reason anyone could later reconstruct. Measured against
# the relationships the design already had, emerald's offsets track them more
# closely on every pair: ON_TEXT/ON_BG 8.09 against today's 7.06, dot/CARD
# 12.29 against 11.19, ON_BORDER/CARD 2.13 against 1.91.
#
# The procedure was validated by running it backwards before applying it
# forwards: deriving BLUE_300..700 from #00a9fc reproduces all five blue
# constants above exactly.
#
# Everything downstream is computed from these by `over()`, so ON_BG,
# ON_BG_HOVER, ON_BORDER, ON_TEXT, CONN["connected"], TOGGLE_TRACK_ON,
# ONLINE_DOT and the LEGEND entry re-derive themselves and need no edit.
EMERALD_300, EMERALD_400, EMERALD_500 = "#83f388", "#3cec43", "#15d51d"
EMERALD_600, EMERALD_700 = "#11ab17", "#0e8f13"

#: The brand green, kept named for traceability. Equal to EMERALD_600.
#: **Deliberately has no readers**, as with BRAND_BLUE above.
BRAND_GREEN = EMERALD_600

RED_300, RED_400, RED_500 = "#fca5a5", "#f87171", "#ef4444"
RED_600, RED_700 = "#dc2626", "#b91c1c"

WHITE = "#ffffff"

# --------------------------------------------------------------------------
# Surfaces (opaque in the source; used as flattening backdrops)
# --------------------------------------------------------------------------

APP_BG = "#12141b"        # bg-[#12141b]  page
TITLEBAR_BG = "#0c0e14"   # bg-[#0c0e14]  header
CARD = "#181c25"          # bg-[#181c25]  device card
INPUT_BG = "#111419"      # bg-[#111419]  inputs / temperature button
AUTO_OFF_BG = "#1c1f29"   # bg-[#1c1f29]  Auto button, off state

# --------------------------------------------------------------------------
# Borders / rules
# --------------------------------------------------------------------------

BORDER_CARD = "#1e2230"          # border-[#1e2230]
BORDER_CARD_HOVER = "#272c3a"    # hover:border-[#272c3a]
BORDER_INPUT = "#252932"         # border-[#252932]
BORDER_INPUT_HOVER = "#30364a"   # hover:border-[#30364a]
SEPARATOR = "#1f2330"            # bg-[#1f2330]  vertical rule in card
TITLEBAR_RULE = "#171a22"        # border-b border-[#171a22]
SUBHEADER_RULE = "#13161e"       # border-b border-[#13161e]
DIVIDER_PIP = "#1e2230"          # bg-[#1e2230]  small vertical pips in header
DASHED_BORDER = "#1e2230"        # border-dashed border-[#1e2230]
DASHED_CIRCLE = "#2a2e3c"        # border-dashed border-[#2a2e3c]

# --------------------------------------------------------------------------
# Text ramp
# --------------------------------------------------------------------------

TEXT_PRIMARY = "#d8e0ed"     # text-[#d8e0ed]
TEXT_TITLE = "#c8d4e8"       # text-[#c8d4e8]
TEXT_MONO = "#9bacc4"        # text-[#9bacc4]  IP address
TEXT_COUNT = "#b0bcce"       # connected count, emphasised
TEXT_SECONDARY = "#6b7585"   # hover:text-[#6b7585]
TEXT_MUTED = "#5a6270"       # "System Online"
TEXT_LABEL = "#454c5a"       # section labels
TEXT_DIM = "#505868"         # Auto button, off state
TEXT_DIMMER = "#4a5060"      # total device count
TEXT_FAINT = "#3e4554"       # header status text
TEXT_GHOST = "#343a47"       # placeholders, "view" hint, version
TEXT_SUBHEAD = "#404755"     # "DEVICE CONTROLLERS"
TEXT_WHISPER = "#2a2f3c"     # sub-header caption
TEXT_ADD = "#2e3340"         # "Add Device" resting
TEXT_SCROLL_HINT = "#252932"

# --------------------------------------------------------------------------
# Connection button -- three states (opaque Tailwind bg + flattened ring)
# --------------------------------------------------------------------------

# Three visual treatments, exactly as the design has them. Which one a device
# gets, and the label/caption inside it, is decided from backend state by
# `widgets.connection_view` -- the design's three looks cover six backend
# states, so the mapping lives with the UI rather than here.
#
#   blue  -> nothing is wrong: never connected, or the operator disconnected,
#            or a connection is being established
#   green -> connected
#   red   -> not connected and nobody asked for that: failed, lost, retrying
CONN = {
    "idle": {
        "bg": BLUE_600,
        "bg_hover": BLUE_500,
        "ring": over(BLUE_700, CARD, 0.40),
        "dot": BLUE_300,
        "text": WHITE,
        "caption_color": TEXT_GHOST,
    },
    "connected": {
        "bg": EMERALD_700,
        "bg_hover": EMERALD_600,
        "ring": over(EMERALD_700, CARD, 0.40),
        "dot": EMERALD_300,
        "text": WHITE,
        "caption_color": over(EMERALD_500, CARD, 0.60),
    },
    "fault": {
        "bg": RED_700,
        "bg_hover": RED_600,
        "ring": over(RED_700, CARD, 0.40),
        "dot": RED_300,
        "text": WHITE,
        "caption_color": over(RED_500, CARD, 0.70),
    },
}

# --------------------------------------------------------------------------
# Clean / Auto action buttons
# --------------------------------------------------------------------------

# Shared "on" treatment: bg-emerald-700/25 + border-emerald-500/35
ON_BG = over(EMERALD_700, CARD, 0.25)
ON_BG_HOVER = over(EMERALD_700, CARD, 0.35)
ON_BORDER = over(EMERALD_500, CARD, 0.35)
ON_TEXT = EMERALD_400

# Clean, off: bg-blue-700/15 + border-blue-500/25
CLEAN_OFF_BG = over(BLUE_700, CARD, 0.15)
CLEAN_OFF_BG_HOVER = over(BLUE_700, CARD, 0.25)
CLEAN_OFF_BORDER = over(BLUE_500, CARD, 0.25)
CLEAN_OFF_TEXT = BLUE_400

# Auto, off: opaque greys
AUTO_OFF_BORDER = BORDER_INPUT
AUTO_OFF_BORDER_HOVER = "#2e3440"
AUTO_OFF_TEXT = TEXT_DIM
AUTO_OFF_TEXT_HOVER = TEXT_SECONDARY

TOGGLE_TRACK_OFF = "#2a2e3a"
TOGGLE_TRACK_ON = EMERALD_500
TOGGLE_KNOB = WHITE

# --------------------------------------------------------------------------
# Temperature
# --------------------------------------------------------------------------

# The warning threshold is runtime configuration, not a design token, so it
# has no constant here: it lives in `config.json` as `temperature_warning_c`
# and is read from `Settings` by the backend (`connection_worker.py`,
# `device_manager.py`). No widget reads a threshold at all -- the alert
# reaches the UI as an already-computed flag.

#: Shown until the first temperature sample arrives. The design always shows a
#: number, so this keeps the button's metrics while reading as "no data".
TEMP_PLACEHOLDER = "—"

TEMP_OK_BORDER = BORDER_INPUT
TEMP_OK_BORDER_HOVER = BORDER_INPUT_HOVER
TEMP_OK_BG_HOVER = "#13161f"
TEMP_OK_TEXT = TEXT_PRIMARY

TEMP_ALERT_BORDER = over(RED_600, INPUT_BG, 0.35)
TEMP_ALERT_BORDER_HOVER = over(RED_500, INPUT_BG, 0.55)
TEMP_ALERT_BG_HOVER = "#16101a"
TEMP_ALERT_TEXT = RED_400
TEMP_ALERT_CAPTION = over(RED_500, CARD, 0.60)

# HIGH badge: bg-red-600/20, then border-red-500/30 composited over that fill
BADGE_BG = over(RED_600, INPUT_BG, 0.20)
BADGE_BORDER = over(RED_500, BADGE_BG, 0.30)
BADGE_TEXT = RED_400

# --------------------------------------------------------------------------
# Title bar
# --------------------------------------------------------------------------

TRAFFIC = [
    ("#ff5f57", over("#e0443e", TITLEBAR_BG, 0.40)),
    ("#febc2e", over("#d4a02a", TITLEBAR_BG, 0.40)),
    ("#28c840", over("#23aa37", TITLEBAR_BG, 0.40)),
]

LOGO_BG = over(BLUE_600, TITLEBAR_BG, 0.20)
LOGO_BORDER = over(BLUE_500, TITLEBAR_BG, 0.30)
LOGO_FG = BLUE_400

EMCC_BG = over(BLUE_600, TITLEBAR_BG, 0.18)
EMCC_BORDER = over(BLUE_500, TITLEBAR_BG, 0.25)
EMCC_TEXT = over(BLUE_400, EMCC_BG, 0.90)
#: The badge's geometry and letter-spacing, here rather than in the Metrics
#: block because this file is organised by component below `Metrics` and these
#: complete one element: the three colours above are the same badge.
#:
#: Both are DISTINCT constants despite tempting neighbours, and deliberately so.
#: `BADGE_RADIUS` is 4 -- a different value under a name that reads like it
#: would fit. `HEADING_TRACKING` in `shell/subheader.py` is also 0.12, but it
#: specifies the sub-header heading; the values coincide and the elements do
#: not, so binding them would make a later change to one silently move the
#: other.
EMCC_RADIUS = 6              # rounded-md
EMCC_TRACKING = 0.12         # tracking-wider

HEADER_ALERT_TEXT = over(RED_400, TITLEBAR_BG, 0.80)
ONLINE_DOT = EMERALD_400

# --------------------------------------------------------------------------
# Sub-header legend (sits on APP_BG)
# --------------------------------------------------------------------------

LEGEND = [
    (over(BLUE_600, APP_BG, 0.70), "Idle"),
    (over(EMERALD_600, APP_BG, 0.70), "Connected"),
    (over(RED_600, APP_BG, 0.70), "Disconnected"),
]

# --------------------------------------------------------------------------
# Add Device (dashed, sits on APP_BG)
# --------------------------------------------------------------------------

ADD_BG_HOVER = over(BLUE_600, APP_BG, 0.03)
ADD_BORDER_HOVER = over(BLUE_600, APP_BG, 0.30)
ADD_TEXT_HOVER = over(BLUE_500, ADD_BG_HOVER, 0.70)
ADD_CIRCLE_HOVER = over(BLUE_600, ADD_BG_HOVER, 0.40)

# --------------------------------------------------------------------------
# Focus (inputs)
# --------------------------------------------------------------------------

FOCUS_BORDER = over(BLUE_500, INPUT_BG, 0.50)   # focus:border-blue-500/50
# No token for `focus:ring-blue-500/20`: that design feature was dropped --
# see "Known approximations" in docs/UI_SPEC.md.

# --------------------------------------------------------------------------
# Scrollbar (.device-scroll in index.css)
# --------------------------------------------------------------------------

SCROLL_THUMB = "#252932"
SCROLL_THUMB_HOVER = "#373d4a"

# --------------------------------------------------------------------------
# Remove-device control
#
# Occupies the slot the decorative pencil had inside the Device Name field
# (`right-2.5`), so the card's metrics are unchanged. Resting state is muted
# so a destructive control is not the loudest thing on the row; it only reads
# as red on hover.
# --------------------------------------------------------------------------

TRASH_IDLE = TEXT_GHOST
TRASH_HOVER = RED_400

# --------------------------------------------------------------------------
# Metrics -- Tailwind spacing resolved to px
# --------------------------------------------------------------------------

TITLEBAR_H = 52          # h-[52px]
CARD_MIN_H = 142         # min-h-[142px]
CARD_RADIUS = 12         # rounded-xl
CTRL_RADIUS = 8          # rounded-lg
BADGE_RADIUS = 4         # rounded
PAGE_PAD_X = 32          # px-8
PAGE_PAD_Y = 16          # py-4
CARD_GAP = 12            # gap-3
CARD_PAD_X = 20          # px-5
SECTION_GAP = 10         # gap-2.5

COL_NAME = 188           # w-[188px]
COL_IP = 176             # w-[176px]
COL_CONN = 162           # w-[162px]
COL_TEMP = 148           # w-[148px]

#: Banner wordmark height.
#
# This is the primary brand mark and is deliberately sized to dominate the
# sub-header band: it spans from above the "DEVICE CONTROLLERS" heading down
# past the caption beneath it.
#
# Arrived at by rendering candidates side by side and then by explicit
# direction: 26 and 40 read as a secondary mark, 52 read as the primary logo,
# and 62 (52 + 20%) is the chosen weight. At 62 the mark is ~182px wide.
#
# It does NOT change the banner's height. The logo is `place`d, which takes it
# out of geometry propagation, so the row stays 45.3px and the first card stays
# at y=140.4 whatever the logo height -- measured at 26/52/62/80. The logo
# simply overflows into the row's existing padding (16px above, 10px below).
#
# That padding is also the practical ceiling: at 62 the mark extends ~8.4px
# above the row, comfortably inside the 16px pad. By 80 it extends ~17.3px and
# would breach the top of the band into the title bar's rule. So ~70 is the
# most this layout can carry without also increasing PAGE padding.
#
# Clearances either side, measured: ~200px to the heading block and ~315px to
# the legend at the default 1280px width; ~150px and ~265px at the 1180px
# minimum. No collision risk even with a 25-device caption.
BANNER_LOGO_H = 62

SCROLL_THRESHOLD = 4     # devices.length >= 4
# No device cap: the export's "supports up to 25" was a demo limit and the
# spec requires no hard maximum. Scaling is a performance concern, not a
# counted one -- see multicameraUI_masterplan.md R-1.

WINDOW_W = 1280
WINDOW_H = 760
WINDOW_MIN_W = 1180
WINDOW_MIN_H = 420

APP_VERSION = "v2.4.1"
APP_TITLE = "Echovista Multi-Camera Controller"


# --------------------------------------------------------------------------
# Dashboard view -- density grid
# --------------------------------------------------------------------------

# The dashboard trades detail for count: no IP, no Connect button, no per-
# column micro-labels. It shows name, connection, temperature, Clean and Auto
# for many devices at once.
#
# 5 columns is fixed; rows grow with the device count. The "25 devices" target
# is what should fit on one screen, not a cap -- SCROLL_THRESHOLD's note above
# applies here too, so the grid scrolls rather than refusing a 26th device.
DASH_COLS = 5

#: Fixed card height. Rows take their height from the card rather than
#: sharing the viewport equally, so 3 devices give 3 cards at full height with
#: empty space beneath, not 3 cards stretched over a fifth of the window.
#
# Sized so 5 rows fit the default 760px window: 760 - TITLEBAR_H(52)
# - sub-header band(~71) - 2*PAGE_PAD_Y(32) - footer caption(~20) leaves ~585px
# for the grid; 5 rows of 112 plus 4 gaps of 10 is 600. Slightly over, so the
# fifth row needs a ~15px taller window or a maximised one -- measured in
# tests/test_dashboard_view.py rather than asserted here.
DASH_CARD_H = 112
DASH_GAP = 10            # gap-2.5 between grid cells
DASH_CARD_PAD = 8        # p-2 inside a card
DASH_ROW_GAP = 3         # gap-0.5 between a card's stacked rows
DASH_TEMP_H = 34         # the inset temperature panel

#: Clean/Auto padding on a dashboard card, passed to their `pad` argument.
#
# The list view's px-4 py-2 makes a 33px control; three of those stacked with
# the header and the temperature panel overflow a 112px card and Tk clips the
# labels rather than growing the frame. 10/5 gives a 27px control, which is
# what makes the budget close:
#   header 28 + temp 34 + actions 27 + padding 16 + gaps 6 = 111
DASH_CTRL_PAD = (10, 5)

#: Status dot on a dashboard card. Smaller than the list view's 8px.
#
# It never pulsed, when the list view still did, because at this density a
# dot reads as a legend key rather than a live indicator. The status pulse
# was removed everywhere on 2026-09-10, so the asymmetry is gone -- kept as
# a note only because the size difference outlived the reason for it.
DASH_DOT = 6

#: The List view sub-header heading. Raw words -- `SubHeader` applies the
#: letter-spacing, so the view owns the wording and the shell owns how it
#: looks, the same division as `caption`.
LIST_SUBHEAD = "DEVICE CONTROLLERS"

DASH_SUBHEAD = "DASHBOARD OVERVIEW"
DASH_LABEL = "DEVICE"

#: Temperature caption beneath the value.
DASH_TEMP_NOMINAL = "nominal"
DASH_TEMP_ALERT = "alert"

# --------------------------------------------------------------------------
# View toggle -- the List / Dashboard segmented control
# --------------------------------------------------------------------------

SEG_BG = INPUT_BG
SEG_BORDER = BORDER_INPUT
SEG_RADIUS = 8

# The selected segment carries the accent; the other is quiet, so the control
# reads as "you are here" rather than as two buttons.
SEG_ON_BG = over(BLUE_700, INPUT_BG, 0.22)
SEG_ON_BORDER = over(BLUE_500, INPUT_BG, 0.30)
SEG_ON_TEXT = WHITE
SEG_ON_ICON = BLUE_300

SEG_OFF_TEXT = TEXT_SECONDARY
SEG_OFF_TEXT_HOVER = TEXT_PRIMARY
SEG_OFF_ICON = TEXT_DIM
SEG_OFF_ICON_HOVER = TEXT_SECONDARY

#: Dashboard cards built synchronously before the view is first shown; the
#: rest stream in on a timer. One row appears immediately and the grid fills
#: downward, which is the same treatment INITIAL_CARDS gives the list view --
#: a card costs ~100ms to build, so 25 of them is ~2.5s of blocked UI if
#: built in one go.
DASH_INITIAL_CARDS = 5

#: Gap between streamed cards. NOT 1ms: a job due in 1ms is re-serviced by the
# same event-loop pass that just ran one, so the loop paints between cards but
# stays saturated for the whole build -- the cost is deferred in name only.
# 16ms is one frame at 60Hz, which yields the loop back between cards and
# makes the fill genuinely progressive.
DASH_BUILD_INTERVAL_MS = 16
