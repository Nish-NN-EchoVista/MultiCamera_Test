# Wire protocol

Everything here was verified against firmware source and, where noted,
confirmed on live hardware. **Do not change a string in `emcc/backend/protocol.py`
without re-checking the firmware.**

Sources:

- `Documents/Github/EVS2` — project `EVS2_2409_v0.2_ARR` (the EVS2 boards)
- `Documents/Github/F401_AdapterController` — project `F4_DataController`
- `Documents/Github/PyGUI` — the existing single-board diagnostic GUI

---

## Topology

```
   EVS2 board 1 ──UART USART2──┐
                               │  F401 AdapterController          Moxa NPort
                               ├─ own thermocouple          ─────────────────►  :4001
   EVS2 board 2 ──UART USART6──┘                              UART→RS-232→TCP
```

Documented in `F401_AdapterController/Core/Src/main.c:18-26`:

```
USART1 -> Laptop GUI
USART2 -> Device 1 (ESV2-1)
USART6 -> Device 2 (ESV2-2)

- Devices -> GUI lines tagged as [ESV2-1]/[ESV2-2]
- GUI -> "@1 <cmd>" routes to ESV2-1, "@2 <cmd>" to ESV2-2, otherwise broadcast to both
- Non-blocking periodic thermocouple print every 500 ms
```

Neither firmware has a TCP stack. The NPort is a transparent serial device
server; port **4001** is its data port for serial port 1 (4001–4016 for
multi-port units). Its TCP Server mode defaults to **Max Connection = 1**.

---

## Commands

| Purpose | Command | Firmware |
|---|---|---|
| Clean | `start_evap_sweep` | `EVS2/user_commands.c:465` |
| Auto on | `enableauto` | `EVS2/user_commands.c:469` |
| Auto off | `dis_auto` | `EVS2/user_commands.c:470` |

**Terminator: `\r\n`, and it is mandatory.** EVS2's RX handler dispatches only
on CR immediately followed by LF (`uart_buffer.c:44-49`):

```c
if (command_buffer[i-2] == '\r' && command_buffer[i-1] == '\n') {
    command_buffer[i-2] = '\0';
    user_commands(command_buffer);
```

### EMCC sends bare commands, not `@N`

This is deliberate and worth understanding before changing it.

The AdapterController's `@1 `/`@2 ` branches forward to a single board and
return an echo. **Anything else falls to the broadcast branch, which reaches
both boards *and* runs the pump gating** (`is_sequence_start` →
`sequence_active = true` → `update_pump_pin()`, `uart_handler.c:296-309`). The
`@N` branches skip that gating entirely.

Since EMCC treats one IP as one system, bare is correct. PyGUI uses `@N`
because it is a single-board diagnostic tool selecting a board — a different
job (`PyGUI/logic/command_sender.py:96`).

### Two firmware asymmetries in the bare path

Both observed on live hardware at 192.168.2.250:

| Command | Reaches | Why |
|---|---|---|
| `enableauto` | **board 1 only** | special-cased at `uart_handler.c:245-248`; the ESV2-2 line is commented out |
| `dis_auto` | **both boards** | its special case is entirely commented out (`:249-251`), so it falls through to broadcast |

Live confirmation:

```
-> sending enableauto      → auto ack: enabled=True  board=1
-> sending dis_auto        → auto ack: enabled=False board=1
                             auto ack: enabled=False board=2
```

This is kept as-is by decision: the hardware is not currently configured for
autoclean, so Auto is an architectural placeholder and a missing or asymmetric
acknowledgement is expected, not a fault. Clean must work properly, and does.

---

## Receive shape

Anything from an EVS2 board is tagged by the AdapterController
(`uart_handler.c:319`, `:347`). Anything untagged is the AdapterController's own.

| Line | Origin | EMCC action |
|---|---|---|
| `[ESV2-1] Evaporation Sweep Completed` | board 1 | Clean complete |
| `[ESV2-2] AutoClean enabled` | board 2 | log only |
| `PZT Temp: 43.7 C` | AdapterController | the displayed temperature |
| `PZT overheat detected. Entering cooldown lockout` | AdapterController | abort Clean |
| `PZT temperature normalized. Re-enabling system.` | AdapterController | lockout cleared |
| `TX to ESV2-1: start_evap_sweep` | AdapterController | **ignore — command echo** |
| `[ESV2-N] Power: 0.0W` | board | ignore |
| `[ESV2-N] Thermal check pass` | board | ignore |
| `[ESV2-N] Board Temperature = 37.7` | board | ignore (**not** the PZT reading) |
| `I: …`, `[Router] …`, `Received clean trigger from ESV2-N` | AdapterController | ignore |

Parsing is two-stage: strip an optional `[ESV2-N] ` tag to get channel plus
payload, then classify the payload.

`uart_print` appends `\r\n` itself in both firmwares, so a line-based parser is
correct. Lines are capped at 192 bytes (AdapterController) and 256 (EVS2).

### The command echo is a trap

`TX to ESV2-1: start_evap_sweep` contains the command text. Any parser that
searches received data for *command* strings will false-positive on it.
Matching on *response* strings is safe.

---

## Clean completion

```
Evaporation Sweep Completed              ← the one we want (sequences.c:155, :467)
```

Two near-misses that must **not** match:

```
Evaporation Sweep Low Power Completed    ← start_evap_sweep_low (sequences.c:207)
AutoClean triggered Evaporation Sweep    ← autonomous trigger (sequences.c:504)
```

A naive `"Evaporation Sweep" in line and "Completed" in line` test matches the
low-power line. **Match the exact trimmed line.**

Since a bare command broadcasts, both boards report completion. EMCC clears the
Clean state on the **first** one — waiting for both would never finish on a
system with only one board populated.

Live timing on 192.168.2.250: completion arrived **5.0 s** after the command,
comfortably inside the 10 s UI timeout, with the PZT warming 23.7 → 32.0 °C
during the sweep.

---

## Temperature

`F401_AdapterController/Core/Src/thermocouple.c:86-93`, every 500 ms:

```c
float temp = read_temperature_celsius();
int temp_int = (int)temp;
int temp_dec = (int)((temp - temp_int) * 10 + 0.5f);   // rounded tenths
uart_print(1, "PZT Temp: %d.%d C", temp_int, abs(temp_dec));
```

One thermocouple per AdapterController, untagged, so it belongs to the system
rather than to a board.

### Firmware artefact: tenths can reach 10

`temp_dec` is rounded with **no carry into the integer part**, so when the
fraction is ≥ 0.95 it becomes 10 and the line reads:

```
PZT Temp: 240.10 C        ← means 241.0, NOT 240.1
```

Reading the text as a decimal is wrong by 0.9 whenever this happens. Observed
live on 192.168.2.253, which alternated between `242.x` and an exactly-repeating
`240.10`. `protocol.parse_pzt_temperature` therefore parses the fields
separately and recombines as `whole + tenths/10`, which handles the carry for
free.

### Firmware artefact: lost sign near zero

The sign lives only on the integer part, so values between 0 and −1 print
without it (`-0.4` → `0.4`). Unrecoverable from the text; deliberately not
guessed at.

### The reading is an ADC conversion, so a bad sensor reads large

```c
float voltage = (raw / 4095.0f) * 3.3f;
return ((voltage - 1.236f) / 0.005f) + calibration_offset;
```

At 0.005 V/°C that is **200 °C per volt** — a disconnected or miswired
thermocouple produces a large but well-formed number. Observed live:

| Host | Reading | Assessment |
|---|---|---|
| 192.168.2.250 | 23.7–24.1 °C, rose to 32.0 °C during a sweep | healthy |
| 192.168.2.251 | ~404 °C, static | almost certainly open-circuit |
| 192.168.2.253 | ~228–242 °C, drifting | almost certainly open-circuit |

**EMCC does not clamp or hide these.** A parser that suppressed them would hide
a hardware fault. The consequence is that .251 and .253 sit permanently above
the 65 °C threshold and render red with the HIGH badge, which is the correct
thing for the software to say about that hardware.

### Do not confuse with the other temperature lines

| Line | Channel | Note |
|---|---|---|
| `Board Temperature = %.1f` | on the wire (`EVS2/sequences.c:804`) | board, not PZT |
| `Temperature  = %f  C` | dead code | `Print_Temperature()` has no live caller |
| `TI TMP451 …`, `PSU_0/1 …`, `External temperature sensor …` | `printf` only | never on the wire |

---

## Thermal lockout

`F401_AdapterController/Core/Src/thermocouple.c:56-72`. On crossing
`ThermalLockoutLimit` the AdapterController prints

```
PZT overheat detected. Entering cooldown lockout
Device will re-enable when 70C is reached.
```

and then **sends `stop` to both boards** (`send_stop_to_devices`, twice each
for reliability). So a Clean in progress is already dead upstream — if EMCC
ignored this, the Clean button would sit green for its full 10 s timeout while
the hardware had stopped. EMCC therefore clears `clean_active` and sets
`thermal_lockout` on this line.

---

## Connecting: drain the stale backlog

An NPort buffers whatever the UART emitted while no client was attached, and
delivers it the instant one connects. Without discarding it, the first read can
contain an old `Evaporation Sweep Completed` and instantly false-complete the
operator's next Clean.

`ConnectionWorker._drain_stale` reads and discards for 250 ms after connecting,
then resets the line assembler. PyGUI does the same
(`services/comms/communication_handler.py:87-101`).

---

## Other lines worth handling later

Recognised in firmware but not yet acted on. The parser is structured so each
is a small addition:

- `[ESV2-N] Power: %fW` — per-board power draw
- `[ESV2-N] Thermal check pass` / failure variants
- `E: Defog board overtemp -> power cut` (`EVS2/defog.c:345`)
- `Evaporation Sweep Low Power Completed` — if the low-power sweep is exposed
- `AutoTrigger` / `Received clean trigger from ESV2-N` — autonomous cleans, which
  would let EMCC show a Clean it did not start

---

## One client at a time — verified

The NPort's TCP Server mode defaults to **Max Connection = 1**, and it does not
accept-then-multiplex: it **actively refuses** the second client.

Verified against 192.168.2.250 with an EMCC worker already connected:

```
first client connected      : True
second client               : REFUSED (WinError 10061, ConnectionRefusedError)
first client afterwards     : still CONNECTED, still streaming temperature
```

Two consequences:

1. EMCC and the PyGUI diagnostic tool **cannot both be connected to the same
   unit**. Whichever connects first wins; the other gets `ECONNREFUSED`.
2. A refusal is therefore far more likely to mean "something else is already
   connected" than "wrong port", so `_friendly()` reports
   *"Connection refused — another program may already be connected to this
   device"* rather than a generic message.

The first client is unaffected by the refused attempt, so a stray connection
attempt cannot disturb a live session.
