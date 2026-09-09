# Placeholders, deviations and open questions

Everything on this page is deliberate. None of it is unfinished work: each item
is either blocked on a decision, blocked on hardware, or a considered trade-off.
Each one records what is settled and what is not.

---

## 1. Temperature view → PyGUI hand-off — RESOLVED

**No longer a placeholder.** Implemented and verified; full detail in
[`PYGUI_HANDOFF.md`](PYGUI_HANDOFF.md).

Clicking a card's temperature readout releases that device and launches PyGUI
already connected to it, via a single `EMCC_HANDOFF_IP` environment variable.

Kept here as the record of how the open questions closed:

| Was open | Resolution |
|---|---|
| How the IP is passed | One environment variable, `EMCC_HANDOFF_IP`. Additive, `is_handoff()`-guarded changes in PyGUI; no config-file writing, no argv parsing. |
| Which PyGUI copy is canonical | `Documents/Github/PyGUI`. `Redesign/PyGUI` is ~7 weeks behind. `Echo Launchers\GUI Launcher.bat` still points at the older copy and should be repointed. |
| NPort one-client limit | EMCC disconnects automatically. The launch is attempted *first*, so a failed hand-off never costs a working connection. |
| Process model | One PyGUI per hand-off, detached. EMCC does not track or reattach; closing PyGUI frees the device and the operator reconnects by hand. |
| Blank IP | The button reports "This device has no IP address set." and nothing is launched or released. |

The one thing knowingly *not* done: the login screen is skipped using an admin
session, authorised explicitly as acceptable for a development tool. No
credential crosses the process boundary — PyGUI starts that session itself.

---

## 2. Auto is an architectural placeholder

**By explicit decision.** The hardware is not currently configured for
autoclean, so a missing or asymmetric acknowledgement is expected and is *not*
treated as a fault.

The commands work and the UI is complete, but note the firmware asymmetry,
confirmed live on 192.168.2.250:

| Command | Reaches |
|---|---|
| `enableauto` | **board 1 only** — special-cased in the AdapterController with the ESV2-2 line commented out |
| `dis_auto` | **both boards** — its special case is commented out, so it broadcasts |

Kept as-is on request. Fixing it means uncommenting `uart_handler.c:247` in the
AdapterController and reflashing; EMCC needs no change.

---

## 3. Implausible temperature readings are not filtered

Observed during development: `192.168.2.251` read ~404 °C and `192.168.2.253`
~228–242 °C, both static or slowly drifting, while `192.168.2.250` read
correctly (24 °C, rising to 32 °C during a sweep).

The reading is an ADC conversion at ~200 °C per volt
(`thermocouple.c:74-81`), so a loose or open-circuit thermocouple produces a
large but well-formed number.

**Both units have since recovered, and only one repair is accounted for.**
Nish reported, verbatim: *"the thermocouple was lose, thats resolved"* --
**device unspecified**. An earlier version of this section attributed that to
`.253`; that was inference on my part presented as fact, and it is withdrawn.

So the current status of both is *recovered, cause unconfirmed*:

| Unit | Was | Now | Cause |
|---|---|---|---|
| `192.168.2.251` | ~404 °C | ~25 °C | unconfirmed |
| `192.168.2.253` | ~228-242 °C | plausible | unconfirmed |

One repair was reported and two units recovered, so **at least one recovery is
unexplained whichever device was fixed.** That matters because an unexplained
recovery may be an intermittent joint rather than a repair -- reading correctly
right up until it does not. Both are queued for a stability re-probe, and one
word from Nish naming the device collapses the ambiguity.

Recording it this way is the lesson as much as the fact: **his words and my
attribution belong in separate sentences.** By the time an inference is a
confident sentence in a document its provenance is gone, and no amount of care
downstream reconstructs it.

**EMCC deliberately does not clamp, filter or hide readings like these**, and
that policy stands regardless of the current state of the bench. Suppressing
them in software would have hidden the loose connection instead of surfacing
it. The consequence, while a unit is faulty, is that it sits permanently above
the 65 °C threshold, renders red with the HIGH badge, and logs one
over-temperature warning on connect — which is the correct thing for the
software to say about that hardware.

Note also that the AdapterController's own thermal lockout fires on such a
unit, sending `stop` to both boards, so Clean may be refused there.

---

## 4. There is no "connecting" state in the design

The design has three connection treatments; the backend has six states.
`widgets/connection_view.py` maps them, reusing existing colours rather than
inventing new ones:

| Treatment | States |
|---|---|
| blue | DISCONNECTED, CONNECTING, STOPPING |
| green | CONNECTED |
| red | ERROR, RECONNECTING |

`CONNECTING` and `RECONNECTING` are distinguished only by label and caption
("Connecting…", "Reconnecting 2/3…"). This is additive: no new colour token was
introduced.

Note the spec asked for red on an initial failure but blue after reconnect
exhaustion. Both are "not connected and nobody asked for that", so **both are
red**; blue is reserved for idle and operator-initiated disconnect. Reverting a
failed device to blue would make it indistinguishable from an idle one.

---

## 5. Deviations from the Figma export

| Deviation | Reason |
|---|---|
| Section labels share a baseline across columns | `items-center` centres each column independently, which misaligns the label row because the columns are different heights. Reproduced exactly it reads as a rendering fault. |
| Clean's check-icon slot is always reserved | The design only shows the icon when active, but Clean now toggles on every press and again on timeout; letting the width change would shunt the Auto button sideways several times a minute. |
| Trash control replaces the decorative pencil | The design has no delete affordance and the 188px column has no spare width. The pencil was non-functional, so replacing it keeps the card's metrics unchanged. |
| `MAX_DEVICES = 25` removed, hint text shortened | The spec requires no hard maximum; 25 was a demo limit. Scaling is a performance concern, not a counted one. |
| Temperature shows `—` before first data | The design always shows a number; a stale or invented reading on a disconnected device would be worse than none. |
| No popup on unexpected disconnect | With 25 devices on one switch a blip would open 25 modals. The card already turns red with a caption. One coalesced dialog appears only on reconnect exhaustion. |

---

## 6. Dropped or approximated for Tk

Not open questions — places where Tk cannot express the CSS. Full detail in
[`UI_SPEC.md`](UI_SPEC.md).

| Design feature | Status |
|---|---|
| `shadow-[0_2px_16px_rgba(0,0,0,0.35)]` on cards | dropped — no box-shadow in Tk |
| `focus:ring-blue-500/20` on inputs | dropped; the border colour change is kept |
| `transition-*` on hover colours | applied instantly, not eased |
| `tracking-[0.14em]` micro-labels | approximated with hair spaces |
| Scrollbar width 5px | 8px, CustomTkinter's practical minimum |
| Card reveal animation | tried and removed — it was measurably *jankier* than an instant insert (see ARCHITECTURE.md) |
| Inter / JetBrains Mono | fall back to Segoe UI / Cascadia Mono if not installed |

---

## 7. Performance headroom not taken

~24 of the 88 widgets per card are transparent border-less `CTkFrame`s used
purely for grouping. Constructing one costs **2.101 ms** against **0.011 ms**
for a plain `tk.Frame` — 190×.

Not done, on purpose: CustomTkinter applies DPI scaling inside its own
`pack`/`place`/`grid` overrides, so every pad, width and height on a raw `tk`
widget would need scaling by hand, and one missed value is a silent fidelity
regression at 2.25× DPI. The progressive startup already fixed the
user-visible symptom (11.1 s → 1.3 s to first paint) at zero fidelity risk.

This should be its own change with its own visual diff.

---

## 8. Visual verification — done, and it found three bugs

Originally blocked: the workstation locked partway through the build, so
screenshots captured the lock screen. Everything was verified
**programmatically** instead — asserting on widget configuration, geometry and
resolved colour tokens — plus against live hardware. Stronger than a
screenshot for behaviour, weaker for appearance.

The pass has now been done, with `tools/visual_pass.py`. It caught three
defects that configuration-level assertions could not, because in each case
the widget's *configuration* was right and only the rendered result was wrong:

| Bug | Cause |
|---|---|
| "1 temp alert" stayed in the title bar after the reading returned to normal | `TitleBar._relayout` only unpacked the widgets it was about to re-pack, so hiding never unpacked the chip. Re-packing the rest *after* it left it on screen. |
| Clean kept its green tick after a sweep ended | `CTkLabel.configure(image=None)` records the `None` but does not clear the underlying Tk label's `image`, so the glyph persisted over the reverted blue fill. Now swaps in `icons.blank(11)`. |
| A lighter square sat behind the trash icon | The label is *placed over* the name entry but parented to the column, so a transparent background painted the column's colour, not the field's. Now `bg_color=theme.INPUT_BG`. |

Each has a regression test that was confirmed to fail before the fix.

### Capturing reliably

`ImageGrab.grab(bbox=...)` reads the desktop, so it captures whatever is on
top of the app — a lock screen, a browser, another window. Both earlier
attempts were spoiled that way.

`tools/window_capture.py` uses `PrintWindow` with `PW_RENDERFULLCONTENT`,
which asks the window to render itself into a bitmap. It is unaffected by
occlusion and needs neither focus nor visibility, so the pass can run while
the machine is in use.

States captured: connecting, connected, over-threshold (red + HIGH badge),
alert cleared, Clean and Auto active, all hover treatments, the trash control
idle and hovered, both dialogs, reconnecting, and ten devices scrolled to the
end.

---

## 9. Still simulated or absent

- **Nothing** in the connection path is simulated any more — connect,
  reconnect, streaming, Clean and Auto were all exercised against real
  hardware at 192.168.2.250.
- **Board-level state is not modelled.** Both EVS2 boards are commanded and
  reported as one device, per the "one IP = one system" decision. Per-board
  status would need a card redesign.
- **Additional telemetry is parsed but ignored** — `Power: %fW`,
  `Thermal check pass`, board temperatures, defog messages. Listed in
  [`PROTOCOL.md`](PROTOCOL.md) with the parser structured so each is a small
  addition.
