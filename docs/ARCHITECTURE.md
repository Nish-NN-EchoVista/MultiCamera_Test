# EMCC architecture

Echovista Multi-Camera Controller. A CustomTkinter control application for
EVS2 evaporation-sweep hardware, reached over TCP.

Companion documents:

- [`../multicameraUI_masterplan.md`](../multicameraUI_masterplan.md) — the build
  spec, protocol verification and decision log
- [`UI_SPEC.md`](UI_SPEC.md) — how the Figma design maps onto Tk
- [`PROTOCOL.md`](PROTOCOL.md) — the wire protocol, with firmware citations
- [`PLACEHOLDERS.md`](PLACEHOLDERS.md) — what is deliberately stubbed

---

## The system EMCC talks to

```
   EVS2 board 1 ──UART USART2──┐
                               │  F401 Adapter/DataController        Moxa NPort
                               ├─ (own thermocouple,          ──────────────────►  TCP :4001  ──►  EMCC
   EVS2 board 2 ──UART USART6──┘   PZT Temp @ 2 Hz)             UART→RS-232→TCP
```

**One IP address is one whole system**, and EMCC presents it as a single device
card. Two EVS2 boards sit behind one AdapterController behind one NPort serial
port. Commands are broadcast to both boards; the single thermocouple belongs to
the AdapterController.

The NPort is a transparent byte pipe, so nothing on the wire is framed beyond
CRLF lines. Its TCP Server mode defaults to **Max Connection = 1** — if the
PyGUI diagnostic tool holds a session to an IP, EMCC cannot connect to it, and
vice versa.

---

## Layers

```
main.py                       entrypoint: logging → config → UI
│
├── emcc/backend/             no Tk anywhere in here
│   ├── protocol.py           verified wire constants, line assembly, parsing
│   ├── events.py             ConnectionState, EventType, DeviceEvent
│   ├── config_manager.py     config.json: load, defaults, atomic debounced save
│   ├── logging_setup.py      rotating logs
│   ├── connection_worker.py  one thread per device, owns one socket
│   └── device_manager.py     device collection, worker lifecycle, timers
│
└── emcc/                     Tk main thread only
    ├── app.py                shell, event pump, dispatch, shutdown
    ├── theme.py              design tokens (flattened translucency)
    ├── fonts.py              family resolution, px sizes, tracking
    ├── icons.py              inline SVGs redrawn with Pillow
    ├── anim.py               tween (eased one-shot motions)
    ├── integrations.py       temperature → PyGUI hand-off
    ├── splash.py             launch splash (Windows layered window)
    └── widgets/
        ├── device_card.py    the five-column row
        ├── buttons.py        Connection / Clean / Auto / Temperature
        ├── connection_view.py backend state → the design's three treatments
        ├── dialogs.py        styled modal error and confirm
        ├── title_bar.py      traffic lights, identity, status cluster
        ├── toggle.py         the Auto pill
        ├── add_device.py     dashed row, canvas-drawn
        ├── interactive.py    hover/click across composite widgets
        └── canvas_util.py    rounded-rect geometry, DPI scaling

assets/                       echo_logo_dark.png (the banner wordmark)

tools/                        development only, never imported by emcc/
├── mock_nport.py             fake NPort + AdapterController + 2× EVS2
├── live_probe.py             probe real hardware and classify the stream
├── visual_pass.py            screenshot every UI state, mock-driven
└── window_capture.py         PrintWindow capture, immune to occlusion

tests/                        pytest
```

The dependency rule is one-way: **`emcc/widgets/` and `emcc/app.py` import from
`emcc/backend/`, never the reverse.** The backend has no idea a GUI exists,
which is what makes it testable without Tk.

---

## Threading model

This is the part most likely to be broken by a careless change, so it is stated
precisely.

```
 ConnectionWorker thread              Tk main thread
 ───────────────────────              ──────────────
 socket recv ─┐
              ├─► parse ─► DeviceEvent ─► Queue ─► App._pump (every 100 ms)
 socket send ◄┘                                        │
      ▲                                                ├─► DeviceManager._apply
      │                                                │      (mutates DeviceState)
      └────────── outgoing Queue ◄──── worker.send() ◄──┴─► card.render()
```

Rules, all of them load-bearing:

1. **A worker never touches a widget.** Not `.configure()`, not `.after()`,
   nothing. Its only output is immutable `DeviceEvent` values on a queue.
2. **Only the worker thread touches its socket.** The UI enqueues a command and
   returns immediately. There is deliberately *no send lock*: a lock held across
   a slow `sendall()` would be held against the Tk thread, and a wedged device
   would freeze the GUI.
3. **`DeviceState` is mutated only on the Tk thread**, inside
   `DeviceManager._apply`. Workers never see it.
4. **Nothing blocks the Tk thread.** No connect, no recv, no sleep, no join
   without a bounded timeout.
5. **The worker does not poll.** It `select`s on its socket *plus a self-pipe*,
   so an enqueued command or a stop request wakes it instantly. Reconnect
   back-off uses `Event.wait()`, which is equally interruptible — shutdown never
   waits out a 10-second sleep.

### Why an injected scheduler

`DeviceManager` owns the two timers the spec requires (the post-connect Auto
reset, and the Clean visual timeout) but takes `schedule`/`cancel` callables
instead of calling `after()` itself. Two reasons:

- tests drive the timers with a fake clock, so timeout behaviour is
  deterministic rather than slow and flaky
- all timer handles live in one place, which is what makes cancellation on
  device removal reliable — a stale `after()` callback firing against a deleted
  card is a leak the spec calls out explicitly

---

## State model

Backend connection state (`events.ConnectionState`):

```
DISCONNECTED ──connect──► CONNECTING ──ok──► CONNECTED
     ▲                         │                 │
     │                       fail            peer drops
     │                         ▼                 ▼
     └──operator disconnect──  ERROR ◄──exhausted── RECONNECTING
                                                     │  (immediate, then 10s ×2)
                                                     └──ok──► CONNECTED
```

The design has three button treatments for six states, so
`widgets/connection_view.py` maps them in one place:

| Treatment | States | Meaning |
|---|---|---|
| blue | DISCONNECTED, CONNECTING, STOPPING | nothing is wrong |
| green | CONNECTED | connected |
| red | ERROR, RECONNECTING | not connected, and nobody asked for that |

An initial failure and an exhausted reconnect both stay **red**, not blue.
Reverting to blue would make a broken device look identical to an idle one.

Action state lives alongside it: `auto_enabled`, `clean_active`,
`latest_temperature`, `temperature_alert`, `thermal_lockout`. The UI renders
this; **nothing infers state from widget colour.**

---

## Data flow for each operator action

**Connect** — validate IP → start worker → CONNECTING → socket opens (3s
timeout) → drain the NPort's stale backlog → CONNECTED → schedule the Auto
reset. Clicking a connected device disconnects it, and a manual disconnect
never triggers reconnection.

**Clean** — enqueue bare `start_evap_sweep`, set `clean_active`, start the 10s
timer. Whichever comes first clears it: a `[ESV2-N] Evaporation Sweep
Completed` from either board, the timeout, a connection loss, or a thermal
lockout. Repeated presses restart the timer; the button is never disabled.

**Auto** — enqueue bare `enableauto` / `dis_auto` and flip the toggle
immediately without waiting for an acknowledgement. Toggling by hand cancels
the scheduled post-connect reset, which would otherwise silently disable Auto
behind the operator. Auto is never persisted.

**Temperature** — `PZT Temp:` arrives at 2 Hz, is throttled to the configured
1 Hz, and is emitted immediately on a threshold crossing so an over-temperature
never lags by a second. Cleared on disconnect: a stale reading on a dead device
is worse than none.

---

## Configuration

`config.json` beside `main.py`. Only `device_name`, `ip_address` and a stable
`id` are persisted — never connection, Auto, Clean, temperature or errors.

Three guarantees:

- **Never crashes the app.** Missing, malformed, truncated or older-schema
  files degrade to defaults, with the reason logged and the bad file preserved
  as `config.json.corrupt`.
- **Atomic writes.** Content goes to a temp file in the same directory and is
  then `os.replace`d, which is atomic on Windows and POSIX. A crash mid-write
  cannot leave a half-written config.
- **Debounced and serialised.** Typing collapses into one write ~400 ms after
  the last keystroke, through a single lock.

---

## Shutdown

Ordered, bounded, and reachable from both the traffic light and `WM_DELETE_WINDOW`
(so Alt+F4 does not bypass it):

1. stop the event pump; discard queued dialogs
2. flush pending config writes
3. signal **all** workers, *then* join them with a shared 4-second budget — the
   waits overlap, so 25 devices take about as long as one
4. stop animations
5. flush logging, destroy Tk

Sockets are closed in each worker's `finally` block regardless, so a slow
worker delays exit but cannot leak a socket.

---

## Performance

Measured on the development machine at 2.25× DPI.

| | |
|---|---|
| widgets per device card | ~88 |
| card construction | ~198 ms |
| **first paint, 25 devices** | **~1.3 s** (5 cards visible) |
| all 25 cards present | ~5.2 s, streamed |
| worst UI tick while streaming | 0.1 ms |
| event pump tick, 25 devices | 0.01 ms |

Three things make that work:

1. **Progressive startup.** Building all 25 cards before showing the window
   measured **11.1 s of blank screen**. Only `INITIAL_CARDS` are built
   synchronously; the rest stream in on a 1 ms timer, yielding between each.
   *Use a timer, not `after_idle`* — a single `update_idletasks()` drains an
   entire `after_idle` chain, silently making the build blocking again.
2. **Cached label offsets.** Measuring a card then re-`place`ing its columns
   moves already-drawn widgets and forces a second full redraw (~178 rounded-rect
   draws instead of ~60). The offset only depends on card shape, so it is
   computed once per variant and applied at construction.
3. **Incremental add.** Adding a device inserts one card before the add button;
   it never rebuilds the list. The original full rebuild was quadratic — 3.0 s
   for the 4th device rising to 8.0 s for the 11th.

**Remaining headroom, not taken:** ~24 of the 88 widgets per card are
transparent border-less `CTkFrame`s used purely for grouping, and they render
nothing. Constructing one costs **2.101 ms** against **0.011 ms** for a plain
`tk.Frame` — 190×. Swapping them would cut card cost by roughly a third.

It was left alone on purpose. CustomTkinter applies DPI scaling inside its own
`pack`/`place`/`grid` overrides, so every pad, width and height on a raw `tk`
widget must be scaled by hand, and one missed value is a silent fidelity
regression at 2.25×. The progressive build already fixed the user-visible
symptom at zero fidelity risk, which is the better trade; this should be its
own change with its own visual diff.

---

## Testing

```bash
python -m pytest tests/ -q
```

| File | Covers |
|---|---|
| `test_protocol.py` | command framing, tag splitting, temperature (incl. the firmware's tenths-of-ten carry), Clean near-misses, line assembly over fragmented packets |
| `test_config.py` | defaults, round trip, malformed/truncated/hostile input, atomicity, debounce, naming collisions |
| `test_worker.py` | real sockets against the mock: connect, stale drain, streaming, throttling, commands, drop, reconnect, exhaustion, prompt shutdown |
| `test_manager.py` | state transitions, thresholds, Clean/Auto timers via a fake clock, removal, duplicate-worker guard |
| `test_ui_integration.py` | Scenarios A–J end to end through the real App and widgets |

No EVS2 hardware is required: `tools/mock_nport.py` reproduces the real
behaviours including line tagging, the `enableauto`-reaches-board-1-only quirk,
and the stale backlog on connect.

`tools/live_probe.py` exercises real hardware when it is available.
`--commands` drives actual evaporation sweeps and the pump, so it is off by
default.

### Transient states are not observable at the UI layer

`drain_events()` applies a whole batch before the UI re-renders, so any state
that both begins and ends inside one batch is invisible to a UI-level test —
and to the operator, which is the point of batching.

The worker's first reconnect attempt is immediate, so against a healthy peer
`CONNECTION_LOST → RECONNECTING → CONNECTED` completes in the worker thread
and arrives as one batch: the observable state never leaves `CONNECTED`. A
test that polls for `RECONNECTING` there is asserting on something that
cannot be seen, and will fail or pass on timing alone.

Two ways to test such a state honestly:

* **At the queue.** `test_worker.py` asserts on emitted events, where nothing
  coalesces and every transition is visible.
* **Make it persist.** `test_scenario_g_drop_shows_reconnecting_then_recovers`
  removes the mock's listener as well as the client, so the immediate retry
  fails and the state lasts a real interval; restoring the listener on the
  same port then lets recovery happen.

### What assertions cannot cover

Every test above asserts on widget *configuration* — the right check for
behaviour, and blind to a widget that is configured correctly yet renders
wrongly. Three bugs lived in exactly that gap: a title-bar chip that was
unpacked from a list it wasn't in, a `CTkLabel` image that `configure(image=
None)` records but does not clear, and a label painting its parent's colour
while placed over a differently-coloured field.

`tools/visual_pass.py` screenshots every state for that reason. It captures
through `PrintWindow` rather than a screen grab, so occlusion — a lock screen,
another window — cannot spoil a run. Each bug it found now also has a
configuration-level regression test, verified to fail before its fix.

---

## Live verification

Exercised against real hardware (192.168.2.250/.251/.253) through the real
entrypoint, not a harness.

### Single device, full session

```
config: loaded 1 device(s)
Bench 1 [192.168.2.250] connecting
Bench 1 [192.168.2.250] connected                        <- 270 ms
Bench 1 Auto reset to OFF after connection               <- +1.1 s
Bench 1 [192.168.2.250] command issued: dis_auto
Bench 1 [192.168.2.250] firmware acknowledged AutoClean disabled   <- board 1
Bench 1 [192.168.2.250] firmware acknowledged AutoClean disabled   <- board 2
Bench 1 Clean command issued
Bench 1 [192.168.2.250] command issued: start_evap_sweep
Bench 1 [192.168.2.250] evaporation sweep completed (board 1)      <- +5.0 s
Bench 1 [192.168.2.250] evaporation sweep completed (board 2)
shutdown requested
shutdown: stopping 1 worker(s)
shutdown: all workers stopped cleanly                    <- 347 ms
```

Both boards acknowledge and both complete, confirming the broadcast. The PZT
warmed 23.7 → 32.0 °C during the sweep. Clean's 5.0 s completion sits well
inside the 10 s timeout, so the timeout is a fallback rather than the norm.

### Four devices, one unreachable (Scenario H)

| Device | State after 9 s | Temperature |
|---|---|---|
| Bench 250 | CONNECTED | 24.2 °C |
| Bench 251 | CONNECTED | 404.1 °C — alert |
| Bench 253 | CONNECTED | 228.7 °C — alert |
| Unreachable (192.0.2.1) | ERROR | — |

- 3/4 connected; the unreachable device reached ERROR without affecting any
  other card
- shutdown 1.3 s, **zero orphan threads**
- event pump: **mean 0.01 ms, worst 0.02 ms** over 41 ticks

That worst-tick figure is the result of one fix. It was originally **208 ms**,
caused by the first alert-variant card forcing a synchronous
`update_idletasks()` inside a pump tick to measure its label offset. That
measurement is now deferred to the next idle slot (`_measure_variant`): the card
renders one frame at the previous baseline, which is imperceptible, and every
later card of that shape uses the cached value.

### Log volume

Three live devices for 30 s produced **8 lines / 831 bytes** — connect lines
plus one over-temperature warning each for the two faulty units. No temperature
spam: samples are logged only when they *cross* the threshold, including the
first sample of an already-hot device.
