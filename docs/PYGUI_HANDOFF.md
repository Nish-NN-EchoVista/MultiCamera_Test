# The PyGUI hand-off

Clicking a device card's **temperature** readout hands that device over to
PyGUI (the "ESV4 Command Centre" single-device diagnostic tool) for detailed
work. EMCC releases the device, PyGUI opens already connected to it.

This replaces the blank placeholder that used to sit behind that button.

---

## Why a hand-off and not a panel

A Moxa NPort in TCP Server mode with **Max Connection = 1** accepts one client
at a time, and actively refuses the second with `ECONNREFUSED` while the first
keeps streaming. Verified against the bench units.

So EMCC and PyGUI cannot both hold a device — the choice is not *whether* to
release it, only whether the operator has to do that by hand. And PyGUI already
has the diagnostics; duplicating them inside EMCC would mean maintaining two
implementations of the same protocol view.

Hence: EMCC lets go, then launches PyGUI onto the device.

---

## What happens, in order

| # | Step | Where |
|---|---|---|
| 1 | Operator clicks the temperature readout | `widgets/buttons.py` (`TempButton`) |
| 2 | PyGUI is launched with `EMCC_HANDOFF_IP=<ip>` | `integrations.py` → `launch_pygui()` |
| 3 | **Only if the launch succeeded**, EMCC disconnects the device and repaints the card | `app.py` → `_open_temperature()` |
| 4 | PyGUI skips its login screen and starts an admin session | `PyGUI/app.py` |
| 5 | PyGUI's UART/TCP switch is set to TCP | `PyGUI/interface/frames/header_frame.py` |
| 6 | After 1.5 s, PyGUI presses its own Connect | same |
| 7 | PyGUI resolves the target IP from the environment, not its selector | `PyGUI/interface/app_controller.py` |

Two orderings in there are load-bearing.

**Launch before release.** If PyGUI cannot be started — moved folder, no IP set
— a working connection must not be dropped for nothing. So the launch is
attempted first and a failure leaves the device exactly as it was, with the
error surfaced in EMCC's usual dialog.

**Release before connect.** PyGUI's 1.5 s delay before connecting covers the
socket actually closing. Without it PyGUI would race EMCC's teardown and be
refused by the NPort.

EMCC does **not** track or reattach to the launched process. Closing PyGUI
frees the device; reconnect in EMCC by hand.

---

## The launch splash

PyGUI takes ~8s from click to ready, so a splash covers the gap:
`emcc/splash.py`, shown by `App._show_launch_splash`.

**It lives in EMCC, not PyGUI.** PyGUI's main thread is the thing that is
busy -- imports, then ~900 widgets -- so a splash it drew would be a frozen
image for most of its life. EMCC is idle from the moment `Popen` returns.

**A Windows layered window, not a Tk canvas.** Tk cannot composite per-pixel
alpha (`-transparentcolor` keys out one colour, so feathered edges fringe), and
`UpdateLayeredWindow` blends a full RGBA bitmap against the desktop. It also
measured *faster*: 0.22-0.84ms per frame against a 16.7ms budget for 60fps,
because a frame is one `memmove` into a DIB and the compositor does the rest.

Details that are easy to get wrong:

* the extended style goes on `frame()`, **not** `winfo_id()` -- they differ,
  and the wrong one silently does nothing
* Pillow's `"BGRa"` raw mode is premultiplied, which is what `ULW_ALPHA` wants
* `SendMessageTimeoutW`'s last parameter is pointer-sized (`PDWORD_PTR`);
  ctypes' default would write 8 bytes into a 4-byte buffer
* the sweep is drawn on a track-width strip so it is clipped *by
  construction* -- drawn on the full canvas it hung off the card's edge

Frames are pre-rendered (~350ms) on a background thread 500ms after EMCC's
first paint. Pure Pillow, no Tk, so threading it is safe -- and a click is
never blocked on rendering. ~31MB resident while a splash is open.

### Dismissal

On evidence, not a timer. `integrations.pygui_state(ip, ignore=...)` combines
two signals:

| Signal | Why |
|---|---|
| window visible | CustomTkinter keeps its window withdrawn through construction and calls `deiconify()` in its `mainloop()` preamble, so visible == the loop started |
| `SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG)` | fails while the thread is not pumping. Too noisy alone: it alternates while the config panel builds in batches |

READY needs both, debounced over two consecutive polls. `ignore` excludes
windows that existed *before* the launch, or clicking a device that already
has a PyGUI open would match the old window and dismiss instantly.

Measured under EMCC's real event loop: splash up **66ms** after the click,
status advancing to "Starting interface" at 2.9s, dismissed at **8.4s**. After
20s without a ready window it reports the failure rather than vanishing, so a
crash does not look like a slow start.

---

## The contract

Exactly one environment variable:

```
EMCC_HANDOFF_IP=192.168.2.250
```

One variable rather than three, so the hand-off cannot be half-configured —
there is no state where the IP is overridden but the session still sits on a
login prompt.

**No credential crosses the process boundary.** EMCC does not know or send a
password; it signals that a hand-off is happening and PyGUI starts the admin
session itself.

Skipping login is authorised for this development tool and is scoped to the
hand-off: launching PyGUI normally is completely unaffected — same login
screen, same roles. `handoff_ip()` returns `None` and every one of the three
behaviours is a no-op.

### Why the role guard cannot bite (and when it would)

`header_frame.py` arms the auto-connect only when the UART/TCP switch exists:

```python
if is_handoff() and self.connection_mode_switch is not None:
```

That switch is `None` for **BOS-role users**. So the guard genuinely does mean
*a BOS user handed a TCP device gets no auto-connect and sits through the full
20s splash timeout* -- and that would be a silent, misleading failure.

**It is unreachable through a hand-off**, by construction rather than by luck:

```
app.py  _start_handoff_session:
   current_user_role = "admin"        # unconditional
   username          = "admin"        # unconditional
   show_interface_page()              # controller built AFTER the forcing

app_controller.py:95   is_bos_user = user_roles_manager.username == "bos"
```

`is_bos_user` is computed when the controller is constructed, which happens
*after* the username is forced to `admin`. So under a hand-off it is always
`False`, the switch always exists, and the guard always arms.

**The line ordering inside `_start_handoff_session` is what guarantees this.**
Reordering those three lines -- forcing the role after building the interface
-- would make the hazard live again with no other change.

It also becomes live if **EMCC** ever grows real user roles. EMCC is an
internal tool with a default-admin model today (Nish's ruling), and the
hand-off is an authentication bypass by design whose only gate is the ability
to set `EMCC_HANDOFF_IP` -- appropriate for a bench tool, and precisely what
makes this moot. If that posture changes, this guard is the first thing to
revisit.

Recorded rather than fixed, deliberately: what a UART-only role *should* do
with a TCP hand-off is a product question, not an implementation one.

---

## Files

**EMCC**

| File | Role |
|---|---|
| `emcc/integrations.py` | `launch_pygui(ip, pygui_path)` → error string, or `None` on success. Never raises. |
| `emcc/app.py` | `_open_temperature()` sequences launch → release → repaint, and raises the splash |
| `emcc/splash.py` | the layered-window launch splash and its pre-rendered frames |
| `emcc/backend/config_manager.py` | `Settings.pygui_path`, overridable in `config.json` |

**PyGUI** (`Documents/Github/PyGUI`)

| File | Change |
|---|---|
| `services/comms/handoff.py` | **new** — `handoff_ip()`, `is_handoff()`, `AUTO_CONNECT_DELAY_MS` |
| `app.py` | `_start_handoff_session()` — admin session, no login screen, window titled with the IP |
| `interface/app_controller.py` | `_get_selected_tcp_ip()` returns the handed-over IP first |
| `interface/frames/header_frame.py` | selects TCP and fires Connect after the delay |

The changes to PyGUI are additive and guarded on `is_handoff()`.

---

## Configuration

`pygui_path` defaults to the bench location:

```json
{ "settings": { "pygui_path": "C:/Users/.../Documents/Github/PyGUI" } }
```

An absolute default is deliberate — the location is site-specific rather than
derivable from EMCC's own path. Forward slashes work on Windows.

Launch prefers **PyGUI's own `.venv`** (`…/.venv/Scripts/pythonw.exe`) — its
dependencies are the ones PyGUI was developed against, and it measured ~0.5s
faster to import. It falls back to `pythonw.exe` beside EMCC's interpreter,
then to `sys.executable`. `pythonw` throughout, so no console window. The child
is
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`: it outlives EMCC, and EMCC's
shutdown neither waits on it nor kills it.

PyGUI's TCP port is fixed at 4001 (`services/comms/config.py`
`TCP_DEFAULT_PORT`), matching EMCC's default.

### Which PyGUI copy

Two copies exist, and this is a **deliberate arrangement**, not drift:

| Copy | State | Role |
|---|---|---|
| `Documents/Github/PyGUI` | carries the hand-off patch set | what `pygui_path` points at, so the hand-off works |
| `Documents/Redesign/PyGUI` | branch `redesign_dev`, ~7 weeks behind, **no hand-off code** | where Nish continues working, and what `Echo Launchers\GUI Launcher.bat` runs |

Nish has asked that the launcher **stay** pointing at `Redesign/PyGUI` and
that it not be repointed. An earlier version of this document called that
"worth repointing"; that is no longer the intent.

The consequence to keep in mind: launching PyGUI from that shortcut runs a
build with no hand-off support. Harmless on its own, because that path does not
involve EMCC. It stops being harmless the moment `pygui_path` is aimed at that
copy — see "What breaks if PyGUI is swapped" below, which is the whole reason
that section exists.

---

## What breaks if PyGUI is swapped

Standing requirement: **PyGUI will be replaced with a newer build, and the
hand-off has to survive that** without EMCC changing. This section is the
inventory of everywhere it currently would not.

### Already independent

**The path.** EMCC resolves PyGUI through `config.json` -> `settings.pygui_path`.
Swapping builds is a one-line config edit with no code change. Nothing else in
EMCC hardcodes a location.

### The four couplings, worst first

| # | Assumption | What the operator sees when it breaks |
|---|---|---|
| 1 | The target carries the **complete** PyGUI-side patch set (4 files) | *Silently connects to the wrong camera* -- see below |
| 2 | PyGUI titles its window `Command Centre ... (opened from EMCC)` | "PyGUI did not start" after 20s, **for a hand-off that succeeded** |
| 3 | The entry script is `app.py` | Immediate, honest error: "PyGUI is missing app.py at: ..." |
| 4 | Both sides agree on the variable name `EMCC_HANDOFF_IP` | Login screen, no connect, then coupling 2's false failure |

**1. The patch set is four files, not one.** It is tempting to think
`services/comms/handoff.py` is the dependency. It is not sufficient:

| File | Without it |
|---|---|
| `services/comms/handoff.py` | nothing reads `EMCC_HANDOFF_IP`; plain login screen |
| `app.py` (`_start_handoff_session`, `mainloop` override) | login screen is not skipped; and without the `mainloop` override the connect is armed during construction and fires inside CustomTkinter's `update()` preamble, killing the connect worker |
| `interface/frames/header_frame.py` | the connect is never armed at all |
| `interface/app_controller.py` (`_get_selected_tcp_ip`) | **PyGUI connects to whatever its TCP selector points at -- the wrong device** |

That last row is the worst outcome available from this feature. A *partial*
port -- `handoff.py` and the `app.py` changes present, the `app_controller.py`
one-liner missed -- skips login, auto-connects, and lands on `TCP_IP1` from
`services/comms/config.py` instead of the device the operator clicked. EMCC has
by then already **released** the intended device. So the operator clicks
Camera 3, gets a PyGUI showing Camera 1's data, and nothing anywhere reports an
error. A partial port is more dangerous than no port.

**2. Readiness is detected by window title, and that is a single point of
failure.** `integrations.pygui_state()` finds the window by matching
`_TITLE_MARKER` plus the IP, then judges visibility and responsiveness *on that
handle*. Both readiness signals therefore depend on the title match. A PyGUI
that titles its window differently is never found, so the splash waits out its
full 20s timeout and reports **"PyGUI did not start"** -- while PyGUI sits
there, connected and working. Reporting failure for a success is the most
misleading shape a failure can take, and it is the one this coupling produces.

### Demonstrated, not hypothetical

`Documents/Redesign/PyGUI` -- the copy `Echo Launchers\GUI Launcher.bat` runs,
and the one Nish intends to keep working in -- **has no `handoff.py` and no
`handoff` reference in `app_controller.py`** (verified 2026-09-08). Point
`pygui_path` at it today and the hand-off degrades to: PyGUI opens on its login
screen, never connects, is never recognised as ready, and the splash reports a
failure 20 seconds later. The device EMCC released stays released.

This is why the feature works from `Github/PyGUI` and would not work from
`Redesign/PyGUI` without porting the four files.

### The fix, when it is scheduled

Hardening is queued as its own task after the `app.py` refactor. The shape it
should take:

- **Identify the window by process, not by title.** `launch_pygui` currently
  *discards* the `subprocess.Popen` object. Keeping it yields `.pid`, and
  `GetWindowThreadProcessId` then finds that process's windows with no
  dependence on what PyGUI calls itself. This removes coupling 2 outright.
  Valid because EMCC execs `pythonw.exe app.py` directly, so the process it
  spawns is the one that owns the window -- a future launcher that re-execs
  would need child-process walking instead.
- **`Popen.poll()` distinguishes "crashed" from "slow".** Today they are
  indistinguishable and both surface as the 20s timeout. A dead process should
  be reported immediately and honestly.
- **Detect hand-off support before releasing the device.** Checking the target
  for the patch set (or having PyGUI advertise support) would turn coupling 1's
  silent wrong-device outcome into a refusal that keeps the device connected.
- **Discover the entry script** rather than assuming `app.py`.

---

## Failure modes

| Condition | Behaviour |
|---|---|
| Device has no IP | "This device has no IP address set." Nothing is launched or released. |
| `pygui_path` wrong | "PyGUI was not found at: …" naming the path and the setting to fix. |
| Folder exists, no `app.py` | "PyGUI is missing app.py at: …" |
| `Popen` fails | The `OSError` is logged and reported; the connection is kept. |
| Device already disconnected | PyGUI still launches and connects. Step 3 is skipped. |

In every failure case the device stays connected. A hand-off either happens or
changes nothing.

---

## Verified

Against a stand-in NPort on `127.0.0.1:4001` that accepts one client at a time,
so the bench units were untouched:

- `launch_pygui` returns `None` and PyGUI connects **to the handed-over IP**,
  sending `disable_pump\r\n` — its normal post-connect init
- driving the real `App._open_temperature`: EMCC connects, the click releases
  the device (card returns to `DISCONNECTED`), and the listener then accepts
  PyGUI as its second client — proving the release-then-connect ordering
- 6.0 s from click to PyGUI connected, most of it PyGUI's own startup
- validation paths return their intended messages for no-IP, wrong path and
  missing `app.py`

Against live hardware, the window title read
`Command Centre - 192.168.2.250 (opened from EMCC)`, confirming both the login
bypass and the IP threading.
