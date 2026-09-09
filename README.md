# Echovista Multi-Camera Controller (EMCC)

A Python/CustomTkinter control application for EVS2 evaporation-sweep hardware,
reached over TCP. The UI is a port of the Figma design in the repository root
(`src/App.tsx`); the backend was built against the EVS2 and AdapterController
firmware as the protocol source of truth.

Each device card is **one whole system**: two EVS2 boards behind an F401
AdapterController behind a Moxa NPort, presented on a single IP.

---

## Run

Normal use — the launcher alongside the other Echo launchers:

```
Documents\Echo Launchers\Multi-Camera Controller.bat
```

It resolves `pythonw.exe` through the `py -3` launcher (so it survives Python
upgrades), falls back to the known Python 3.12 install, and runs without a
console window.

During development, run it directly so tracebacks and the font report are
visible:

```bash
python main.py
python main.py --verbose                 # DEBUG logging
python main.py --config other.json       # alternate config
```

Requires Python 3.10+, `customtkinter` and `pillow`:

```bash
pip install customtkinter pillow
```

Because the launcher uses `pythonw.exe`, a startup failure is silent. If
double-clicking does nothing, run `python main.py` from this folder to see the
error.

### Fonts

The design specifies **Inter** and **JetBrains Mono**; neither ships with
Windows. The app resolves the best available family and falls back to Segoe UI
/ Cascadia Mono, printing what it chose on startup. Installing the two families
is the single highest-value fidelity improvement available and needs no code
change.

---

## First run

With no `config.json`, three cards are created pre-filled with the bench
systems:

```
Camera 1   192.168.2.250
Camera 2   192.168.2.251
Camera 3   192.168.2.253
```

Nothing connects automatically — every device starts disconnected, Auto off,
Clean idle, temperature showing `—` until data arrives. Click **Connect** on a
card to open its TCP session.

Note the NPort accepts **one client at a time** by default. If the PyGUI
diagnostic tool is connected to an IP, EMCC cannot connect to it, and vice
versa.

---

## Using it

| Control | Behaviour |
|---|---|
| **Connect** | Blue = idle or you disconnected. Green = connected. Red = failed, lost, or retrying — click to retry. Clicking a connected card disconnects it. |
| **Clean** | Sends `start_evap_sweep` to both boards and turns green. Returns blue on the first completion from either board, or after 10 s. Press it as often as you like; each press restarts the timer. |
| **Auto** | Sends `enableauto` / `dis_auto` and flips immediately. Always starts OFF on a new connection, and is never persisted. See the note below. |
| **Temperature** | Live PZT reading, throttled to 1 Hz. Turns red above 65 °C with a HIGH badge. Click it to hand the device over to PyGUI — EMCC releases it and PyGUI opens already connected. See `docs/PYGUI_HANDOFF.md`, including the 60fps launch splash that covers PyGUI's ~8s startup. |
| **Trash icon** | Removes the device, with confirmation. Stops its worker and closes its socket first. |
| **+ Add Device** | Appends a card and persists it immediately. No maximum. |

Device name and IP save automatically, ~400 ms after you stop typing.

Connection problems do **not** pop a dialog — the card turns red with a caption,
which is better signalling and does not bury the screen when a switch blips. A
dialog appears only when automatic reconnection is finally exhausted, and
several devices failing together are coalesced into one.

---

## Branding

The blue controls are matched to the "echovista" wordmark: **`#00a9fc`**,
sampled from the logo asset. The whole blue ramp is derived from it (see
`docs/UI_SPEC.md`), so the Connect and Clean buttons, the connection dot, the
EMCC badge, the Idle legend dot and the focus ring all follow the brand rather
than Tailwind's default palette.

The banner wordmark is `assets/echo_logo_dark.png` at `theme.BANNER_LOGO_H`
(62px). It is centred on the true centre of the banner, which is stable as the
caption text changes; the alternative — the midpoint between the heading and
the legend — drifts as devices are added.

## Known hardware caveats

Observed live on the bench, and correct behaviour on EMCC's part:

- **Implausible temperatures are shown, not filtered.** `192.168.2.251` read
  ~404 °C and `192.168.2.253` ~228–242 °C during development. The reading is an
  ADC conversion at ~200 °C per volt, so a loose or disconnected sensor
  produces a large but well-formed number. EMCC does not clamp or hide it,
  because that would hide a hardware fault: such a unit sits above the 65 °C
  threshold and renders red, which is the correct thing for the software to
  say about that hardware.
- **Both units have since recovered, and only one repair is accounted for.**
  A loose thermocouple was reported resolved, **device unspecified**, so both
  are *recovered, cause unconfirmed* and both are queued for a stability
  re-probe. Treat a plausible reading on either as provisional until the cause
  is known — an unexplained recovery may be intermittent rather than repaired.
  `192.168.2.250` has read correctly throughout (24 °C, rising to 32 °C during
  a sweep).
- **Auto reaches board 1 only.** Bare `enableauto` is special-cased in the
  AdapterController and its ESV2-2 line is commented out, while `dis_auto`
  broadcasts to both. Kept as-is deliberately: the hardware is not currently
  configured for autoclean, so Auto is an architectural placeholder. Clean
  works properly on both boards.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | layers, threading model, state model, performance |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | the wire protocol with firmware citations, and its traps |
| [`docs/UI_SPEC.md`](docs/UI_SPEC.md) | how the Figma design maps onto Tk, and what was approximated |
| [`docs/PYGUI_HANDOFF.md`](docs/PYGUI_HANDOFF.md) | the temperature → PyGUI hand-off: contract, ordering, failure modes |
| [`docs/PLACEHOLDERS.md`](docs/PLACEHOLDERS.md) | what is deliberately stubbed and why |
| [`multicameraUI_masterplan.md`](multicameraUI_masterplan.md) | build spec, protocol verification, decision log |

---

## Layout

```
main.py                    entrypoint: logging → config → UI
config.json                settings + persisted devices (created on first run)
logs/emcc.log              rotating, 5 MB × 5

emcc/backend/              no Tk in here; testable without a display
  protocol.py              verified wire constants, line assembly, parsing
  events.py                ConnectionState, EventType, DeviceEvent
  config_manager.py        load, defaults, atomic debounced save
  logging_setup.py         rotating logs
  connection_worker.py     one thread per device, owns one socket
  device_manager.py        device collection, worker lifecycle, timers

assets/                    echo_logo_dark.png (banner wordmark)

emcc/                      Tk main thread only
  app.py                   shell, event pump, dispatch, shutdown
  theme.py fonts.py icons.py anim.py integrations.py
  widgets/                 cards, buttons, dialogs, title bar

tools/                     development only, never imported by emcc/
  mock_nport.py            fake NPort + AdapterController + 2× EVS2
  live_probe.py            probe real hardware, classify the stream
  visual_pass.py           screenshot every UI state, mock-driven
  window_capture.py        PrintWindow capture, immune to occlusion

tests/                     pytest
```

---

## Development

```bash
python -m pytest tests/ -q
```

No hardware needed — `tools/mock_nport.py` reproduces the real behaviours,
including line tagging, the `enableauto`-reaches-board-1-only quirk, and the
stale backlog an NPort delivers on connect.

Run a mock system to point the app at:

```bash
python tools/mock_nport.py --port 4001
python tools/mock_nport.py --port 4001 --drop-after 20    # test reconnect
python tools/mock_nport.py --port 4001 --stale 30         # test the drain
```

The suite asserts on widget configuration, which cannot judge appearance. For
that, screenshot every state:

```bash
python tools/visual_pass.py shots
```

It captures connecting, connected, over-threshold, alert-cleared, Clean and
Auto active, all hover treatments, the trash control, both dialogs,
reconnecting, and a scrolled list — mock-driven, so no hardware is involved.
Capture goes through `PrintWindow` rather than a screen grab, so the machine
can be in use while it runs. It has already caught three rendering bugs that
configuration-level assertions missed; see `docs/PLACEHOLDERS.md` section 8.

Probe real hardware:

```bash
python tools/live_probe.py --observe 20            # read-only
python tools/live_probe.py --observe 20 --raw 5    # plus classified raw lines
python tools/live_probe.py --observe 40 --commands # DRIVES REAL HARDWARE
```

`--commands` runs actual evaporation sweeps and the pump. It is off by default
for that reason.

### If you change the protocol

Re-verify against firmware first — `docs/PROTOCOL.md` cites file and line for
every constant. Several strings are traps: the command echo contains the
command text, `Evaporation Sweep Low Power Completed` defeats a substring test
for completion, and the temperature format can emit `240.10` meaning 241.0.

### If you touch threading

`docs/ARCHITECTURE.md` states the contract. The short version: a worker never
touches a widget, only the worker thread touches its socket, `DeviceState` is
mutated only on the Tk thread, and nothing blocks the Tk thread.
