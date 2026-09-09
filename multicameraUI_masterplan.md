# EMCC Backend Implementation — Master Plan

Echovista Multi-Camera Controller (EMCC). The Figma-derived CustomTkinter UI is
built (see `docs/UI_SPEC.md`). This document is the spec of record for the
backend build, plus the findings and refinements from evaluating it.

**Status: IMPLEMENTED and verified against live hardware.**

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the built system,
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the verified wire protocol, and
[`docs/PLACEHOLDERS.md`](docs/PLACEHOLDERS.md) for what is deliberately
stubbed. This document remains the spec of record and decision log.

## Live verification summary

Exercised end to end against 192.168.2.250 through the real entrypoint:

| Scenario | Result |
|---|---|
| C — connect, auto reset, streaming temperature | connected in 270 ms; `dis_auto` sent 1.1 s after connect, acknowledged by **both** boards; temperature 24.1 °C |
| E — Clean | `start_evap_sweep` issued; **completion from both boards after 5.0 s**, inside the 10 s timeout; PZT warmed 23.7 → 32.0 °C during the sweep |
| F — Auto | `enableauto` acknowledged by board 1 only; `dis_auto` by both — the firmware asymmetry, confirmed |
| J — shutdown | **347 ms**, zero orphan threads, config saved |
| Logging | 15 semantic lines for a full session; 8 lines / 30 s across three live devices |

Two firmware artefacts were found only because of live testing and are now
handled: the temperature format can emit `240.10` meaning 241.0 (rounded tenths
with no carry), and two of the three units have open-circuit thermocouples
reading 404 °C / 240 °C. See PROTOCOL.md.

## Settled decisions

| # | Decision | Resolution |
|---|---|---|
| D-1 | TCP port | **4001** (Moxa NPort data port for serial 1) |
| D-2 | Temperature source | `PZT Temp: %d.%d C` from the AdapterController, 2 Hz |
| D-3 | Topology | EVS2 ×2 → DataController → Moxa NPort → TCP |
| D-7 | Device model | **One IP = one device card = one whole system.** No channel selector; the card commands the system |
| D-8 | Shared temperature | Moot under D-7 — one thermocouple, one card |
| D-9 | PyGUI to port from | `Github/PyGUI` (as named); mechanics only, not `ConnectionManager` |
| — | Clean command | **bare** `start_evap_sweep` — broadcasts to both boards *and* runs the pump gating, which `@N` routing skips |
| — | Auto command | **bare** `enableauto` / `dis_auto`, i.e. keep firmware behaviour (ON reaches ESV2-1 only). Hardware is not currently set up for autoclean, so a missing/incorrect response is expected and must not be treated as a fault — this is an architectural placeholder. Clean must work properly |
| — | Clean completion | First `Evaporation Sweep Completed` from any channel, or the 10 s timeout |
| — | Live testing | Authorised to send all commands to .250 / .251 / .253 |
| — | Default config | Seed `192.168.2.250 / .251 / .253`, still disconnected at startup |
| D-4 | Trash control | Replaces the decorative pencil (zero layout change) |
| D-6 | R-1 widget reduction | **In scope** — required to meet §24 at 25 cards |
| E-1 | Failed/lost colour | Red for failed or lost; Blue only for user-initiated disconnect |
| E-2 | Auto OFF colour | Keep the design's neutral grey |
| E-3 | Threshold | 65 °C from config, strict `>` |
| E-6 | Connecting state | Added: blue fill, pulsing dot, "Connecting…" / "Reconnecting (n/3)…" |
| E-7 | No-data temperature | `—` in ghost colour |
| E-9 | Popups | None on unexpected disconnect; one coalesced popup on reconnect exhaustion |
| E-10 | Socket sends | Queue only — never a lock on the UI path |
| E-11 | Reconnect timing | Immediate first attempt, then 10 s before attempts 2 and 3 |
| E-12 | Clicking red | Retries the connection |
| E-4 | Device cap | Removed; scroll hint shows the count only |

- **Part 1** — the build specification
- **Part 2** — EVS2 protocol verification (facts from firmware)
- **Part 2B** — transport topology: AdapterController, TCP port, PyGUI reuse
- **Part 3** — evaluation: corrections, contradictions, risks
- **Part 4** — open decisions needed before implementation
- **Part 5** — refined implementation sequence

---

# Part 1 — Build specification

## 1. Primary requirement

Preserve the existing UI design as closely as possible. Do not redesign the
application, replace the UI architecture unnecessarily, or alter the
Figma-derived visual styling unless required for functionality.

Build networking, persistence, configuration, logging, threading, state
management, error handling and dynamic device infrastructure *underneath* the
existing UI. The application must remain responsive, fast, thread-safe,
scalable to many simultaneous devices, cross-platform, maintainable and cleanly
architected.

## 2. EVS2 source is the protocol source of truth

Verify against the EVS2 firmware before implementing commands or parsing:
exact CLI command strings, acknowledgement/output strings, temperature output
format. Do not assume provisional strings are correct.

Provisional commands: `start_evap_sweep\r\n`, `enableauto\r\n`, `dis_auto\r\n`.
Provisional Clean completion: `Evaporation Sweep Completed`.

→ **Verified in Part 2.**

## 3. TCP architecture

Each device is independently controlled through a persistent TCP connection.
All devices use the same configurable TCP port. Each device has `device_name`
and `ip_address` (e.g. `Camera 1` / `192.168.2.101`).

The connection stays open until the user disconnects, the application exits, or
the connection is lost and reconnection ultimately fails.

Communication is text-based, ASCII/UTF-8-compatible, CRLF-terminated commands.
EVS2 continuously streams textual data. **Not** a one-command/one-response
protocol — the receiver must continuously consume incoming data.

## 4. Networking implementation

A dedicated background worker thread per connected device. **Not asyncio.**
Each worker owns its socket, receiver loop, connection state, reconnect state,
command sending, buffering and shutdown event.

Never update Tkinter widgets from worker threads.

```
DeviceWorker thread → thread-safe Queue → Tkinter main thread → UI update
```

Use `after()` on the main thread to drain backend events without blocking.
Target smooth interaction with ~25 devices connected. No hardcoded limit.

## 5. Architecture

```
backend/
    device_manager.py
    device_worker.py
    protocol.py
    config_manager.py
    logging_setup.py
```

Adapt to the existing project rather than restructuring destructively.

- **DeviceWorker** (one per active device): open socket, maintain persistent
  connection, read streaming data, send commands, detect disconnects,
  reconnect, clean shutdown, emit parsed events.
- **DeviceManager**: collection of configured devices, create/start/stop
  workers, dispatch commands, coordinate shutdown, expose events to UI.
- **Protocol parser**: line buffering, parse EVS2 output, identify temperature
  / Clean completion / Auto acknowledgements, safely ignore unrelated
  telemetry, easy to extend.

No string parsing logic in the GUI.

## 6. TCP receive buffering

TCP is a byte stream; one `recv()` is not one line.

```
incoming bytes → decode → append to buffer → split complete CR/LF lines
→ retain incomplete trailing data → parse complete lines
```

Handle `\r\n` and `\n` robustly. A partial line split across packets must parse
correctly. Multiple messages in one packet must parse correctly.

## 7. Connect button behaviour

Initial: Blue, "Disconnected / Ready".

On click: validate IP → attempt TCP connection on configured port with a
**3-second** timeout.

- Success → Green, Connected.
- Failure → Red, Connection Failure, plus an error popup identifying device
  name, IP address and a short error description. Technical detail to the log.

Pressing Connect while connected manually disconnects → Blue. A manual
disconnect must not trigger automatic reconnection.

## 8. Automatic reconnect

On unexpected loss of a previously established connection: **3 attempts, 10
seconds between attempts.** Must not block the UI; the worker stays responsive
to shutdown/manual-disconnect.

On success: restore Connected, resume streaming, reset the reconnect counter,
perform the Auto reset (§9).

On exhaustion: stop retrying, terminate that worker's connection, return the
device to the manual-disconnect state, Connect button Blue, and show one popup
identifying device name and IP explaining automatic reconnection failed.

No dead threads or sockets left behind.

## 9. Auto state reset after connection

Auto always starts logically OFF when a new connection is established. A few
seconds after a successful connection, automatically send the firmware command
that disables Auto (`dis_auto`), after **every** new successful connection
including automatic reconnections. Never visually enable Auto from persisted
state.

## 10. Auto button behaviour

Default OFF, blue/neutral.

- OFF → ON: verify connected, send `enableauto`, immediately set Auto ON,
  button Green.
- ON → OFF: send `dis_auto`, immediately set Auto OFF, button blue/neutral.

The firmware acknowledges these; parse and log where useful, but the UI toggle
does not wait for acknowledgement.

If sending fails because the socket dropped: handle as connection loss, revert
Auto to OFF, start reconnection if applicable.

Auto state is never persisted across restarts.

## 11. Clean button behaviour

On press: require a valid connected device, send `start_evap_sweep\r\n`,
immediately set Clean button Green.

When the parser detects `Evaporation Sweep Completed` → Clean button Blue.

If no completion within **10 seconds** → reset to Blue. The timeout must not
block the UI.

The user may press Clean repeatedly, even while cleaning is active. Do **not**
disable the button during a cycle — EVS2 handles duplicate commands. Each valid
press refreshes/restarts the 10-second visual timeout.

If the connection drops during Clean: cancel Clean state, button Blue, begin
normal connection-loss handling.

## 12. Temperature parsing

EVS2 continuously streams thermocouple data. Build a robust parser extracting
the floating-point value, tolerant of packet boundaries, handling negative
values.

→ **Format corrected in Part 2.**

## 13. Temperature UI updates

Update each device card's existing temperature display from the stream,
reflecting the latest known value. Update cadence no faster than ~**1 second**;
avoid flooding Tkinter.

- Normal: white text.
- `temperature > configured threshold`: red text. Default threshold **65 °C**.
- Back at or below threshold: white.

The existing temperature-click popup/placeholder stays unchanged.

## 14. Application configuration

Local `config.json`, separating global settings from saved devices:

```json
{
  "settings": {
    "tcp_port": 0,
    "temperature_warning_c": 65,
    "connection_timeout_s": 3,
    "clean_timeout_s": 10,
    "temperature_update_interval_s": 1,
    "reconnect_attempts": 3,
    "reconnect_interval_s": 10
  },
  "devices": [
    { "device_name": "Camera 1", "ip_address": "" },
    { "device_name": "Camera 2", "ip_address": "" },
    { "device_name": "Camera 3", "ip_address": "" }
  ]
}
```

Set the correct TCP port rather than leaving `0`. Tolerate missing file,
malformed JSON, missing keys and older versions — use sensible defaults, never
crash. If missing, create a default config with 3 cameras.

## 15. Device persistence

Persist only `device_name` and `ip_address`. Never persist connection state,
Auto state, Clean state, temperature or errors.

Name/IP changes save automatically, debounced (~300–500 ms after typing stops),
using atomic file replacement (write temp → replace `config.json`).

## 16. Startup behaviour

Load config, recreate exactly the saved device cards with saved names and IPs,
all disconnected, Auto OFF, Clean idle, temperature unset/placeholder until
data arrives. Do not auto-connect. If no config, create `Camera 1/2/3` with
blank IPs.

## 17. Adding devices

The existing `+` button adds another device card with the next sensible default
name (`Camera 4`, `Camera 5`, …), avoiding ambiguity if the user has renamed
existing cards. Immediately persist. No hard maximum. Existing scroll behaviour
stays intact.

## 18. Removing devices

Add a small red trash control to the right side of each Device Name field.
Before removal: gracefully stop the worker, disconnect the socket, remove from
DeviceManager, clear associated timers/events. Then remove the card, remove the
config entry, save. No orphan threads or sockets.

Use a lightweight confirmation dialog (`Remove Camera 4?`, with IP if
available), visually consistent with the app.

## 19. Thread safety

Critical. Worker threads must never call `.configure()`, `.set()`,
`.destroy()`, `.after()` on widgets.

Backend threads emit structured events (dataclasses/enums preferred):

```python
{"type": "temperature", "device_id": "...", "value": 43.7}
```

Event types: `CONNECTED`, `DISCONNECTED`, `CONNECTION_FAILED`, `RECONNECTING`,
`RECONNECT_FAILED`, `TEMPERATURE`, `CLEAN_COMPLETE`, `AUTO_ACK`, `ERROR`.

The Tkinter main thread processes these.

## 20. Device identity

Do not use `device_name` as the internal identifier — users rename devices.
Each runtime device gets a stable internal ID (e.g. UUID), not displayed.
Persisting the ID is permitted as an implementation detail; the functional
persistence requirement remains name + IP.

## 21. Socket sending

Serialise sends per device — a per-device send lock or outgoing command queue.
Never concurrent `sendall()` on the same socket from different threads.

## 22. Error handling

Errors must not crash EMCC. User-facing errors use popups identifying device
name and IP:

```
Camera 7 — 192.168.2.115
Connection timed out
```

Avoid spamming popups during reconnect attempts: one informative popup on
unexpected disconnect if appropriate, quiet/status-driven internal retries, one
final popup after 3 failures. Short UI text; full detail to logs.

## 23. Logging

Python `logging`, rotating: `logs/emcc.log`, `RotatingFileHandler`,
`maxBytes = 5 MB`, `backupCount = 5` (~25 MB retained).

Log: startup, shutdown, config load, config save failures, device
creation/removal, connection attempts/success, user disconnect, unexpected
disconnect, reconnect attempts and outcomes, commands at a semantic level,
Clean start/completion, Auto ON/OFF, temperature threshold warnings, parser
errors, socket errors, unexpected exceptions.

Do **not** log the raw RX/TX stream or every temperature sample. Log a
temperature warning on *entering* over-temperature, not every second while
still above. Optionally log return to normal.

```
INFO Camera 2 [192.168.2.105] connected
INFO Camera 2 Clean command issued
WARNING Camera 2 temperature exceeded limit: 67.3 C
ERROR Camera 4 connection lost: ConnectionResetError
```

## 24. Responsiveness and performance

Never on the Tkinter main thread: socket connect, blocking recv, reconnect
sleep, long file operations, blocking waits. Networking is independent per
device — one bad camera must not affect another.

No busy loops: use `threading.Event`, `Queue.get(timeout=...)`, socket
timeouts/`select`. Keep UI event polling lightweight. Target smooth interaction
with 25+ device cards and persistent connections.

## 25. Graceful shutdown

On close: prevent new network actions, save latest device config, signal all
workers to stop, cancel reconnect waits, close all sockets, join threads with
bounded timeouts, flush logging, destroy the Tkinter app.

Must not hang. Must safely interrupt workers that are blocked on recv,
reconnecting, cleaning or transmitting. Daemon threads are not a substitute for
correct cleanup.

## 26. Protocol parser extensibility

Parse only what EMCC needs: temperature, Clean completion, Auto
acknowledgements, connection-relevant messages. Ignore unknown lines safely.
Design so more telemetry can be added later.

## 27. State management

Explicit backend state per device: `DISCONNECTED`, `CONNECTING`, `CONNECTED`,
`RECONNECTING`, `ERROR`, `STOPPING`. Separately: `auto_enabled`,
`clean_active`, `latest_temperature`, `temperature_alert`.

Never derive backend state from widget colour. The UI renders backend state; it
is not the source of truth.

## 28. Input validation

Validate IP addresses before connecting; IPv4 minimum. Architect so
hostname/IPv6 would not be hard later, without over-engineering. Invalid IP
produces a popup with device name, entered IP and reason. Do not launch a
worker for an obviously invalid address.

## 29. Config write resilience

Protect writes with locking, debounce them, make them atomic, never let worker
threads edit config files directly, centralise persistence through
ConfigManager. On failure: keep the UI alive, log, and show a popup if the
failure is persistent/important.

## 30. Existing UI integration

Inspect the existing UI first. Identify the device card class, button
callbacks, StringVars, temperature label, scroll container, add-device button,
temperature placeholder callback and state styling functions. Integrate
cleanly; do not duplicate the UI. Replace hard-coded demonstration state
changes with real backend events.

Preserve dimensions, colours, fonts, spacing, animations, Figma styling and the
existing temperature placeholder popup.

## 31. Tests

Backend-oriented, no real hardware required.

- **Parser**: `PZT Temp: 43.7 C` → `43.7`; fragmented packets; multiple lines
  per packet; unrelated output; Clean completion; Auto acknowledgement.
- **Config**: missing, valid, malformed; device add/delete/edit.
- **State**: connect success/failure, disconnect, reconnect logic, three failed
  attempts, Clean timeout, Clean completion, temperature threshold crossing.

## 32. Development test server

Optional mock EVS2 TCP server, separate from production code: persistent
connection, temperature messages every second, Clean completion, Auto
acknowledgement, forced disconnect. Production must not depend on it.

## 33. Code quality

Use type hints, dataclasses, enums for state, focused classes, clear naming,
short methods, documented concurrency behaviour. Avoid monolithic GUI classes,
global mutable networking state, duplicated socket code, duplicated command
strings, magic constants, threading logic scattered through the UI. Centralise
verified EVS2 protocol constants.

## 34. Final review

After implementation, review specifically for: Tkinter calls from worker
threads, socket race conditions, reconnect race conditions, stale callbacks
after device deletion, duplicate workers for one card, unclosed sockets,
blocked shutdown, config corruption, excessive UI event frequency, excessive
logging, timer/callback leaks, UI/backend state mismatches.

The result should behave like production control software, not a UI demo.

---

# Part 2 — EVS2 protocol verification

Source: `C:\Users\NishathNawaz\OneDrive - Echovista Limited\Documents\Github\EVS2`
Project `EVS2_2409_v0.2_ARR`, 237 C/H files. Verified by direct inspection.

## Confirmed correct in the spec

| Item | Verified | Location |
|---|---|---|
| `start_evap_sweep` | exact | `user_commands.c:465` |
| `enableauto` | exact | `user_commands.c:469` |
| `dis_auto` | exact | `user_commands.c:470` |
| `Evaporation Sweep Completed` | exact | `sequences.c:155`, `:467` |
| `\r\n` command terminator | **required** | `uart_buffer.c:44-49` |

The RX handler dispatches only when it sees `\r` immediately followed by `\n`:

```c
if (command_buffer[i-2] == '\r' && command_buffer[i-1] == '\n') {
    command_buffer[i-2] = '\0';
    user_commands(command_buffer);
```

All output is CRLF-terminated — `uart_print()` appends `\r\n` itself
(`uart_buffer.c:76-79`), so a line-based parser is correct. Max line ~253
chars + CRLF.

## Corrections to the spec

### C-1. EVS2's own temperature output is dead code (not the one we want)

EVS2 does contain a temperature emitter:

```c
// temp_sensor.c:232
uart_print(&huart4, "Temperature  = %f  C", temperature);   // board temp, 6 decimals
```

but `Print_Temperature()` is reached only via `Temperature_Print_Out()`
(`sequences.c:884`), and **that function has no callers anywhere in the
repository.** There is also no temperature command in any dispatch table.
So EVS2 emits no temperature as built.

This does **not** block EMCC: the temperature EMCC needs comes from the
AdapterController instead — see **T-3**. EVS2's board-temp line is only
relevant as something the parser must *not* confuse with it, along with
`Board Temperature = %.1f` (`sequences.c:804`, also on the wire). The
`TI TMP451`, `PSU_0/1` and `External temperature sensor` lines are `printf`
only and never reach the wire.

### C-2. Correction to an earlier finding

An earlier draft of this document stated the spec's `PZT Temp: %d.%d C` format
"does not exist in the firmware". That was wrong about the *source*, not the
format. The spec's format — and its exact `uart_print(1, ...)` call — are
correct; they live in the **AdapterController**, not EVS2. See **T-3**.

### C-3. Auto acknowledgements identified

The spec asked for these; they are:

```
AutoClean enabled     ← enableauto
AutoClean disabled    ← dis_auto
```

`enableauto` may additionally emit, when built with
`AUTODETECTION_SWEEP_MODE == CONSTANT_AUTODETECTION_SWEEP`:

```
AutoTrigger
I: Autodetection sweep starting, Driving Voltage: %f, from %d Hz to %d Hz, with %f Hz step
```

The parser must tolerate both presence and absence.

### C-4. Clean completion has dangerous near-misses

```
Evaporation Sweep Completed              ← the one we want
Evaporation Sweep Low Power Completed    ← start_evap_sweep_low
AutoClean triggered Evaporation Sweep    ← autonomous trigger, sequences.c:504
```

A naive `"Evaporation Sweep" in line and "Completed" in line` test matches the
low-power line too. **Match the exact trimmed line.**

### C-5. EVS2 has no TCP stack — it is two hops from EMCC

Zero hits for `lwip`, `socket`, `netconn`, `tcp_`, `W5500`, `ENC28J60`; the
seven `ETH_` hits are unused ST HAL/CMSIS headers. Same for the
AdapterController. **Neither firmware has a TCP stack.**

### C-6. Firmware quirk worth knowing

`HAL_UART_RxCpltCallback` uses a single `static char command_buffer` shared
between `huart3` and `huart4` (`uart_buffer.c:38`). Concurrent input on both
UARTs would interleave into one buffer and corrupt commands. Not EMCC's bug,
but the likely explanation if commands are occasionally mis-parsed while
someone also has a debug console attached.

---

# Part 2B — Transport topology (resolved)

Sources: `Github/F401_AdapterController` (95 C/H files) and `Github/PyGUI`
(the existing production GUI, which already speaks this protocol).

## The actual chain

```
EVS2 #1 ──UART USART2──┐
                       │  F401 AdapterController      USART1        serial↔Ethernet        TCP
                       ├─ (own thermocouple,      ──────────────►   device server    ────────────►  EMCC
EVS2 #2 ──UART USART6──┘   PZT Temp every 500 ms)                   port 4001
```

Documented verbatim in `F401_AdapterController/Core/Src/main.c:18-26`:

```
USART1 -> Laptop GUI
USART2 -> Device 1 (ESV2-1)
USART6 -> Device 2 (ESV2-2)

- Devices -> GUI lines tagged as [ESV2-1]/[ESV2-2]
- GUI -> "@1 <cmd>" routes to ESV2-1, "@2 <cmd>" to ESV2-2, otherwise broadcast to both
- Non-blocking periodic thermocouple print every 500 ms
```

## T-1. TCP port = 4001 — resolves D-1

From PyGUI `services/comms/config.py`:

```python
TCP_IP1 = "192.168.2.250"   # Device 1
TCP_IP2 = "192.168.2.251"   # Device 2
TCP_IP3 = "192.168.2.252"   # ... up to .254
TCP_DEFAULT_PORT = 4001
BAUD_DEFAULT = 57600
```

Neither firmware has a TCP stack, and 4001 is the conventional first-serial-port
default for Moxa / Lantronix / USR device servers, so the bridge is an
off-the-shelf **transparent** serial device server. No framing or IAC
negotiation to handle. One IP per AdapterController; PyGUI provisions five
(.250–.254).

## T-2. **One IP address serves TWO cameras** — new blocker, see D-7

An AdapterController fronts *two* EVS2 devices on one IP. This breaks the
spec's `{device_name, ip_address}` device model in two ways:

1. A card needs a **channel** (1 or 2) as well as an IP to be addressable.
2. Two cards on the same IP must **share one TCP connection**. Most serial
   device servers accept only one client per serial port — a second socket to
   :4001 is typically refused, or worse, interleaves. So the unit of
   connection is the **AdapterController, not the camera card**.

That inverts part of §4/§5: `DeviceWorker` should own a *connection to an
AdapterController*, and camera cards multiplex over it, keyed by channel.

## T-3. Temperature: `PZT Temp` is real, and it is the AdapterController's

`F401_AdapterController/Core/Src/thermocouple.c:92`:

```c
uart_print(1, "PZT Temp: %d.%d C", temp_int, abs(temp_dec));   // every 500 ms
```

The spec's format was right all along. Notes:

- **2 Hz on the wire**, so §13's 1 Hz UI throttle is correctly sized.
- `%d.%d` with `abs(temp_dec)`, so one decimal place: `PZT Temp: 43.7 C`.
- Negative values print correctly (`-2.4`) **except** between 0 and −1, where
  the integer part is `0` and the sign is lost — `-0.4` prints as `0.4`.
  Cosmetic, but do not "fix" it in the parser; it is a firmware artefact.
- It is **untagged**, i.e. per-AdapterController, from a single thermocouple.
  Two cards sharing an IP therefore share one temperature reading. See D-8.

## T-4. Commands must be channel-prefixed — correctness hazard

`uart_handler.c:237-244`:

```c
if (strncmp(line, "@1 ", 3) == 0) { uart_print(1, "TX to ESV2-1: %s", line+3);
                                    uart_send_raw(2, line+3, len-3); tx_crlf(&U[2]); }
else if (strncmp(line, "@2 ", 3) == 0) { ... USART6 ... }
```

So EMCC must send `@1 start_evap_sweep\r\n`, **never** bare
`start_evap_sweep\r\n`. A bare command falls through to the broadcast branch and
would clean **both** cameras. PyGUI does exactly this already
(`command_sender.py:96`: `command = f"@1 {command}"`).

Two traps in the bare-command special cases, which `@N` routing avoids entirely:

- bare `enableauto` is hard-wired to **ESV2-1 only** — the ESV2-2 line is
  commented out (`uart_handler.c:246-247`)
- bare `dis_auto` handling is **entirely commented out**, and the dead code
  mapped it to `auto_0`, not `dis_auto` (`:249-251`)

**Always route explicitly with `@1 `/`@2 `.**

The AdapterController also *intercepts* some commands and does not forward them
(`enable_pump`, `disable_pump`, `lockout++/--`, `thermocouple_calibrate++/--`).
EMCC needs none of them, but the bridge is not a pure passthrough.

## T-5. Incoming lines are tagged — parser needs two stages

`uart_handler.c:319` / `:347`:

```c
uart_print(1, "[ESV2-1] %s", U[2].line_buf);
uart_print(1, "[ESV2-2] %s", U[6].line_buf);
```

So the wire carries:

| Line shape | Origin | EMCC action |
|---|---|---|
| `[ESV2-1] Evaporation Sweep Completed` | EVS2 ch.1 | route to ch.1 card |
| `[ESV2-2] AutoClean enabled` | EVS2 ch.2 | route to ch.2 card |
| `PZT Temp: 43.7 C` | AdapterController | applies to the whole IP |
| `TX to ESV2-1: start_evap_sweep` | AdapterController | **command echo — ignore** |
| `I: Pump enabled (...)`, `[Router] ...` | AdapterController | ignore/log |
| `Received clean trigger from ESV2-1` | AdapterController | ignore/log |

Parse in two stages: strip an optional `[ESV2-N] ` tag to get channel +
payload, then run the payload through the message parser. This is cleaner than
the flat parser originally planned.

Note the echo hazard: `TX to ESV2-1: start_evap_sweep` contains the command
text, so any parser that searches for *command* strings in received data will
false-positive. Matching on *response* strings (as specified) is safe.

## T-6. Stale-buffer drain on connect — not in the spec, but needed

PyGUI does this immediately after `connect()`
(`communication_handler.py:87-101`):

```python
sock.settimeout(3)          # connect
...
sock.settimeout(0.1)        # then drain whatever the bridge buffered
while True:
    junk = sock.recv(256)   # until timeout
sock.settimeout(3)
```

A device server buffers everything the UART emitted while no client was
connected. Without this drain, EMCC's first read can deliver a large stale
backlog — including an old `Evaporation Sweep Completed`, which would
immediately and wrongly complete a fresh Clean. **Replicate this.**

## T-7. Reusable from PyGUI

PyGUI is a mature, documented, single-device diagnostic GUI. Directly portable:

| PyGUI component | Value to EMCC |
|---|---|
| `services/comms/communication_handler.py` (302 ln) | socket setup, 3 s connect timeout, junk drain, `recv(512)` + line buffering, `(cmd + "\r\n").encode()`, `shutdown()` + `close()`, batched line flush |
| `logic/message/message_parser.py` | `MessageKind`/`MessageCategory` taxonomy and per-kind parsers — already handles both `PZT Temp:` and `Temperature` |
| `logic/command_sender.py` | `@1`/`@2` prefixing, `SelectedDevice` enum |
| `logic/device_data_store.py` | per-device state store shape |
| `logic/connection_manager.py` (528 ln) | lifecycle/callback shape — but see caveat |

**Caveat on wholesale reuse:** PyGUI is architecturally *single-connection* —
one active socket plus a `SelectedDevice` toggle, and its
`CommunicationHandler` takes a `tk_root` and calls `after()` itself. EMCC needs
N concurrent connections and a strictly Tk-free, queue-only worker. So port the
**parser, command formatting and socket/line-buffer mechanics** (high value,
low risk) and re-architect the **connection lifecycle** per-connection rather
than lifting `ConnectionManager` as-is.

Also: PyGUI has a binary sub-protocol for bulk transfers
(`BINSTART`/`BIN2START` headers, `binary_receiver.py`, oscilloscope/IL data).
EMCC does not need it, but if someone runs diagnostic PyGUI against the same
hardware, binary payloads may appear on the wire. Decode defensively —
`decode(errors="replace")`, as PyGUI already does.

**Two PyGUI copies exist:** `Github/PyGUI` (which you named) and
`Redesign/PyGUI` (which `Echo Launchers\GUI Launcher.bat` actually runs, and
which has `build/`, `dist/` and an `EVS2 Control Centre.spec`). They differ in
size. Worth confirming which is canonical — see D-9.

---

# Part 3 — Evaluation

## Contradictions and ambiguities to settle

**E-1. Connect failure colour is inconsistent.** §7 says connection failure →
**Red**; §8 says reconnect exhaustion → **Blue**. Both are "we are not
connected and did not choose that", so they should look the same.
*Recommend:* Red for any failed/lost state (matches the existing UI's red
"Reconnect" state and its "TCP connection lost" caption); Blue reserved for
user-initiated disconnect and never-connected. Clicking Red retries.

**E-2. Auto OFF colour.** §10 says "Blue / neutral". The Figma design's Auto
OFF is **neutral grey** (`#1c1f29` fill, `#505868` text), not blue.
*Recommend:* keep the existing grey — §1 says preserve the design, and
"blue/neutral" most likely just means "not green".

**E-3. Temperature threshold constant and comparison.** Spec default is 65 °C;
the existing code has `TEMP_ALERT = 70` compared with `>=`. Spec §13 says `>`
threshold → red, at-or-below → white.
*Recommend:* 65 from config, strict `>`. Note the consequence: the seeded
Camera 3 at exactly 65 °C renders **normal**, not alert.
Also `theme.TEMP_ALERT` must stop being a theme constant — it is currently
imported by `widgets/buttons.py` and `models.py` and has to become runtime
config.

**E-4. Device cap contradiction.** §17 says no hard maximum; the existing UI
has `MAX_DEVICES = 25` and renders "supports up to 25" in the scroll hint.
*Recommend:* remove the cap and change the hint to just the count.

**E-5. Trash control has nowhere to go.** §18 wants a red trash control at the
right of the Device Name field — but that exact spot already holds the
decorative pencil icon (`right-2.5`), and the column is a fixed 188 px with the
input filling it. Two options, both preserving the layout:
  - **(a)** Replace the pencil with the trash. The pencil is purely decorative
    and non-functional, the field is obviously editable, and this costs zero
    layout change. *Recommended.*
  - **(b)** Keep the pencil, shrink the input ~24 px and put the trash after
    it. Changes the Figma metrics.

**E-6. No CONNECTING state exists in the design.** §7 has a 3-second timeout
and §27 lists `CONNECTING`/`RECONNECTING`, but the design has only three
connection states. Without a visual, the button looks frozen for up to 3 s.
*Recommend:* add a connecting treatment built only from existing tokens — keep
the blue fill, pulse the dot, caption "Connecting…" — and "Reconnecting (2/3)…"
for retries. This is additive and does not alter the design language.

**E-7. Temperature has no "no data yet" representation.** §16 requires
placeholder until data arrives; the design always shows a number.
*Recommend:* `—` in the ghost colour (`#343a47`), keeping the button's metrics
and hiding the HIGH badge.

**E-8. The automatic `dis_auto` races the user.** §9 sends `dis_auto` "a few
seconds after" connecting. If the user toggles Auto ON inside that window, the
scheduled reset silently disables Auto in firmware while the UI shows ON —
exactly the UI/backend mismatch §34 warns about.
*Recommend:* make the delay explicit and configurable
(`auto_reset_delay_s`, default 2) **and** cancel the scheduled reset if the
user toggles Auto first.

**E-9. The popup policy will produce a popup storm.** §22 allows a popup on
unexpected disconnect. With 25 devices on one switch, a network blip means up
to 25 modal dialogs, and 25 more when retries exhaust — the app becomes
unusable exactly when something is wrong.
*Recommend:* **no popup on unexpected disconnect** (the card already turns red
with a caption, which is better signalling), one popup only on reconnect
exhaustion, and coalesce into a single dialog when several devices fail inside
a short window. The underlying gap is that the design has no toast/notification
surface; a coalesced dialog is the cheapest fix that doesn't invent new UI.

**E-10. A send lock can block the UI thread.** §21 offers "per-device send lock
or queue". A lock held across a slow `sendall()` (full socket buffer, wedged
peer) will block whichever thread wants it — and that is the Tkinter thread,
violating §24.
*Recommend:* **queue only.** The UI enqueues and returns immediately; only the
worker ever touches the socket. Never a lock on the UI path.

**E-11. Reconnect timing is ambiguous.** "3 attempts, 10 seconds between" does
not say whether attempt 1 is immediate or after 10 s.
*Recommend:* immediate first attempt, then 10 s before attempts 2 and 3 — most
real drops are transient and this recovers in milliseconds rather than 10 s.
Needs confirming; it changes worst-case time-to-give-up from 30 s to 20 s.

**E-12. Clicking a Red Connect button is undefined.** §7 only defines clicking
while connected. *Recommend:* Red → retry connection.

## Risks

**R-1. 25 device cards is a measured performance problem, not a theoretical
one.** From the work already done on this UI:

- one device card = **88 CustomTkinter widgets**
- constructing one card ≈ **209 ms**; full click-to-painted add ≈ **750 ms**
- essentially all of it is Tcl round-trips (~0.76 s of profile `tottime` in
  `_tkinter.tkapp.call`)

At 25 devices that is ~2 200 widgets and an estimated **5 s+ startup**, against
§24's "smooth interaction with 25+ device cards".

Mitigation is already identified and measured: **24 of the 88 widgets per card
are transparent, border-less `CTkFrame`s used purely for grouping** — they
render nothing, and cost **2.101 ms** each to construct versus **0.011 ms** for
a plain `tk.Frame` (190×). Swapping them should cut card cost substantially.

*Recommend:* treat this swap as **in scope for this build** rather than a
nice-to-have — §24 effectively requires it. Caveat: CustomTkinter applies DPI
scaling inside its own `pack`/`place`/`grid` overrides, so every pad, width and
height on a raw `tk` widget must be scaled by hand (2.25× on the current
display); each one missed is a silent fidelity regression. It needs its own
visual diff pass.

**R-2. Event pump sizing.** 25 workers × temperature at 1 Hz plus state events
is light, but drain interval and batching matter.
*Recommend:* drain every ~100 ms, process all queued events per tick, and
coalesce temperature events per device so only the latest value is rendered.

**R-3. Alt+F4 bypasses graceful shutdown today.** Because the native caption is
stripped, the only close path wired up is the red traffic light
(`on_close=self.destroy`). `WM_DELETE_WINDOW` is not bound, so Alt+F4 would
skip everything §25 requires.
*Recommend:* bind `protocol("WM_DELETE_WINDOW", ...)` to the same shutdown
sequence.

## Smaller refinements

- **S-1.** Clean's width changes when it activates (the check icon appears),
  which shifts the Auto button. §11 makes Clean toggle on *every press* plus a
  10 s timeout, so this will now jitter frequently. Reserve the icon's 11 px
  slot permanently so the button is fixed-width in both states.
- **S-2.** Remove the demo `DeviceStore.cycle_connection()` — §30.
- **S-3.** Persist the UUID in config (permitted by §20). Makes rename and
  reorder robust and lets logs correlate across restarts.
- **S-4.** Allow an optional per-device `tcp_port` override in config (not in
  the UI) — near-zero cost, satisfies §3's "same configurable port" today while
  not painting us into a corner.
- **S-5.** The mock EVS2 server (§32) should be **required** for the test suite,
  since §31's reconnect/Clean-timeout/state tests need a controllable peer. Keep
  it out of production imports.
- **S-6.** Directory mapping: add `emcc/backend/` and keep the existing
  `emcc/widgets/` as the UI layer — do **not** rename it to `ui/` (§5 says
  adapt, not restructure).
- **S-7.** `config.json` and `logs/` next to `main.py` per §5/§23. Note for
  later: if EMCC is ever packaged, both need a user-writable location.
- **S-8.** Auto ACKs must **not** drive UI state — the UI is authoritative per
  §10. Log only. Otherwise the automatic `dis_auto` ACK (E-8) could fight the
  UI.
- **S-9.** §31's parser test case uses the wrong sample. Replace
  `PZT Temp: 43.7 C → 43.7` with `Temperature  = 43.700000  C → 43.7`, and add
  a negative case asserting `Evaporation Sweep Low Power Completed` does **not**
  register as Clean completion (C-4).

---

# Part 4 — Decisions needed before implementation

**~~D-1~~ RESOLVED** — TCP port **4001**, transparent serial device server
(T-1). `tcp_port: 4001` goes in `config.json` settings.

**~~D-2~~ RESOLVED** — temperature is `PZT Temp: %d.%d C` from the
AdapterController at 2 Hz (T-3). No firmware change needed.

**~~D-3~~ RESOLVED** — the AdapterController (`F401_AdapterController`) is in
the path and is a second protocol source of truth (Part 2B).

## D-7 — Device model: one IP, two cameras (blocker)

The single decision that shapes the build. An AdapterController fronts two EVS2
devices on one IP (T-2), so `{device_name, ip_address}` cannot address a camera.

Options:

- **(a) Card = `{device_name, ip_address, channel}`; one shared connection per
  IP.** *Recommended.* `DeviceWorker` owns a connection to an
  AdapterController; cards attach to it as channel 1 or 2. Matches the hardware
  exactly, keeps one socket per device server (which is all it will accept),
  and PZT temperature naturally fans out to both cards on that IP. Cost: adds a
  channel selector to the card UI, and `DeviceManager` gains a connection→cards
  mapping. §14's persisted schema gains `"channel": 1`.
- **(b) Card = one AdapterController (both cameras in one card).** Fewer moving
  parts, but the Figma card has one Clean, one Auto and one Connect — it cannot
  express two independently controllable cameras without a redesign, which §1
  forbids.
- **(c) Keep one card = one IP and always address `@1`.** Simplest, but half
  the hardware becomes unreachable from EMCC.

If (a): where should the channel selector live in the card? The Device Name
column is the only one with spare vertical room, but see E-5 — the trash
control is also competing for that space. My suggestion is a compact `1`/`2`
segmented control in the IP Address column beneath the field, since IP and
channel together form the address.

## D-8 — Shared temperature display

PZT temperature is one thermocouple per AdapterController (T-3), so two cards on
the same IP show the *same* value, and a threshold breach reddens both.
Options: **(a)** accept it and show the shared reading on both cards
*(recommended — it is one physical sensor and pretending otherwise would be a
lie)*; **(b)** show it only on the channel-1 card and leave channel 2 blank;
**(c)** firmware change to add per-channel thermocouples.

## D-9 — Which PyGUI is canonical?

`Github/PyGUI` (you named it) or `Redesign/PyGUI` (what `GUI Launcher.bat`
runs, and which has `build/`/`dist/`)? Affects which copy I port from, and
which one the §later temperature-view integration should launch.

## D-4 — Trash control placement

E-5 option (a) replace the pencil *(recommended)*, or (b) shrink the input.
Note this now interacts with D-7(a)'s channel selector for space.

## D-5 — Confirm the recommendations in E-1, E-2, E-3, E-6, E-7, E-9, E-11

Each changes visible behaviour. My recommendations are stated; silence on any
of them will be taken as agreement with the recommendation.

## D-6 — Is R-1's widget optimisation in scope now?

It is needed to meet §24 at 25 devices, but it is a fidelity-sensitive refactor
deserving its own review. Note D-7(a) softens this: 25 *cameras* is only ~13
connections, though still 25 cards.

---

# Part 5 — Refined implementation sequence

Ordering changed from §33 in three ways: protocol verification is already done
(Part 2); the performance refactor is inserted before device cards multiply;
and the mock server moves early because the worker cannot be tested without it.

| # | Step | Depends on |
|---|---|---|
| 0 | ~~Inspect UI / EVS2 / AdapterController / PyGUI; confirm strings~~ | **done — Parts 2, 2B** |
| 1 | Resolve **D-7** (device model), D-8, D-9 | user |
| 2 | `protocol.py` — verified constants, `[ESV2-N]` tag split, line buffering, parsers, `@N` command formatting | Parts 2, 2B |
| 3 | Parser unit tests (C-4 negative case, T-5 tag routing, T-5 echo lines) | 2 |
| 4 | Mock AdapterController TCP server (tagging, 2 Hz `PZT Temp`, `@N` routing, stale backlog on connect) | 2 |
| 5 | `config_manager.py` — load/defaults/migration, atomic debounced save | D-7 |
| 6 | Config tests | 5 |
| 7 | `logging_setup.py` — rotating logs | — |
| 8 | Event dataclasses + enums (backend state, event types, channel) | D-7 |
| 9 | `connection_worker.py` — socket, T-6 drain, recv loop, send queue, reconnect, shutdown | 2,4,8 |
| 10 | `device_manager.py` — connection↔card mapping, lifecycle, dispatch, event fan-out by channel | 9, D-7 |
| 11 | Worker/manager tests against the mock server | 4,9,10 |
| 12 | UI event pump (`after()` drain, batching, coalescing) | 10 |
| 13 | **R-1 widget reduction + visual diff** | D-6 |
| 14 | Wire Connect (incl. E-1, E-6, E-12; shared-connection semantics) | 12 |
| 15 | Wire temperature display (incl. E-3, E-7, D-8 fan-out) | 12 |
| 16 | Wire Clean (incl. S-1, `@N` routing) | 12 |
| 17 | Wire Auto (incl. E-8, S-8, `@N` routing) | 12 |
| 18 | Device add/remove + trash control (D-4) + channel selector (D-7) | 5,10 |
| 19 | Graceful shutdown incl. `WM_DELETE_WINDOW` (R-3) | 10 |
| 20 | State/threshold/timeout tests | 11 |
| 21 | Run tests + static checks; fix | all |
| 22 | Concurrency review against §34's checklist | all |
| 23 | Walk Scenarios A–J | all |

Note the rename at step 9: `DeviceWorker` → `ConnectionWorker`, because under
D-7(a) the unit of connection is the AdapterController, not the camera.

## Acceptance scenarios (from §34, unchanged)

**A** No config → Camera 1/2/3, all disconnected, no auto-connect.
**B** Name + IP survive restart.
**C** Connect → green; `dis_auto` sent shortly after; streaming temperature
updates the correct card.
**D** 66.5 °C turns only that card's temperature red; 64.0 °C returns it white.
**E** Clean → green; completion returns it blue; 10 s timeout also returns it
blue.
**F** Auto ON sends `enableauto` and goes green immediately; OFF sends
`dis_auto` and returns neutral.
**G** Device disappears → 3 reconnect attempts; success restores Connected;
total failure leaves it disconnected with one final popup naming camera and IP.
**H** One unreachable device does not affect 20 working ones.
**I** Removing a connected camera stops its worker, closes its socket, removes
the card and config entry, leaves no orphan thread.
**J** Closing with several active connections stops all workers, closes all
sockets, saves config, exits without hanging.
