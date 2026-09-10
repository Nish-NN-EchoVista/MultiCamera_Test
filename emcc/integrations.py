"""External application integration: the PyGUI hand-off.

Clicking a device card's temperature readout hands that device over to PyGUI
(the "ESV4 Command Centre" single-device diagnostic tool) for detailed work.

Why a hand-off rather than a panel
----------------------------------
A Moxa NPort in TCP Server mode accepts **one client at a time** -- verified
against real hardware, where a second connection is actively refused
(ECONNREFUSED) while the first keeps streaming. So EMCC and PyGUI cannot both
hold a device. Rather than duplicate PyGUI's diagnostics inside EMCC, EMCC
releases the device and launches PyGUI onto it.

The contract
------------
EMCC sets exactly one environment variable:

    EMCC_HANDOFF_IP=<ip>

PyGUI's `services/comms/handoff.py` reads it and, only when present:

* skips the login screen and starts an admin session
* returns this IP from `_get_selected_tcp_ip()`, so the device does not have to
  be one of PyGUI's five hard-coded addresses
* starts on TCP and presses its own Connect button after a short delay

One variable, so the hand-off cannot be half-configured. **No credential
crosses the process boundary**: EMCC signals the hand-off, and PyGUI starts the
admin session itself.

Ordering matters
----------------
The device must be disconnected *before* PyGUI is launched, or PyGUI's connect
is refused by the NPort. `App._open_temperature` sequences that; PyGUI's own
1.5s delay before connecting covers the socket actually being released.

EMCC does not track or reattach to the launched process. Closing PyGUI frees
the device again and the operator reconnects in EMCC by hand.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from enum import Enum
from pathlib import Path
from typing import Iterable, NamedTuple

logger = logging.getLogger("emcc.integration")

#: Environment variable PyGUI reads. Must match
#: `PyGUI/services/comms/handoff.py:ENV_HANDOFF_IP`.
ENV_HANDOFF_IP = "EMCC_HANDOFF_IP"

#: PyGUI's entry script, relative to its root.
PYGUI_ENTRY = "app.py"


class PyGuiState(Enum):
    """How far along a handed-over PyGUI is.

    Used to drive the launch splash, so it dismisses on evidence rather than
    on a guessed delay.
    """

    #: No window with this device's title yet -- still importing.
    ABSENT = "absent"
    #: The window exists but its thread is not pumping messages: PyGUI is
    #: mid-construction, building its widgets.
    STARTING = "starting"
    #: The window is processing messages, so its event loop is running.
    READY = "ready"


#: PyGUI titles its window `Command Centre â€” <ip> (opened from EMCC)` when
#: launched by us -- see `PyGUI/app.py:_start_handoff_session`. Matching the
#: IP as well as the marker keeps concurrent hand-offs distinct.
_TITLE_MARKER = "opened from EMCC"

_WM_NULL = 0x0000
_SMTO_ABORTIFHUNG = 0x0002


def _configure_win32() -> None:
    """Pin the signatures we rely on.

    `SendMessageTimeoutW`'s last parameter is `PDWORD_PTR` -- pointer-sized.
    Left to ctypes' defaults it would be handed a pointer to a 4-byte
    `c_ulong` on a 64-bit build, and the API would write 8 bytes into it.
    """
    user32 = ctypes.windll.user32
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int


if sys.platform == "win32":
    _configure_win32()


def pygui_windows(ip: str) -> list[int]:
    """Top-level window handles belonging to a PyGUI handed `ip`.

    Matched on the marker *and* the IP, so concurrent hand-offs to different
    devices stay distinct. Not filtered on visibility: a hidden window is
    exactly what distinguishes "still building" from "not started".

    The platform guard below exists for the CONTRACT, not for POSIX support.
    See `pygui_state` for the reason and the evidence.
    """
    if sys.platform != "win32":
        return []
    ip = (ip or "").strip()
    if not ip:
        return []

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _param):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if _TITLE_MARKER in title and ip in title:
            found.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(visit, 0)
    except Exception:
        logger.debug("pygui_windows: enumeration failed", exc_info=True)
        return []
    return found


def pygui_state(ip: str, ignore: "Iterable[int]" = ()) -> "PyGuiState":
    """Report how far along the PyGUI launched for `ip` is.

    What "ready" means here, and why
    --------------------------------
    Two signals, because neither is sufficient alone.

    **Window visibility.** CustomTkinter keeps its window *withdrawn* through
    construction and calls `deiconify()` in its `mainloop()` preamble, so
    visible is equivalent to "the event loop has started". Measured: the
    window exists and is titled from ~2.5s while still hidden.

    **`SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG)`**, which fails while the
    target thread is not pumping messages. `WM_NULL` is a no-op, so nothing is
    delivered. On its own it is too noisy: PyGUI's config panel builds in
    batches and the probe alternates between success and failure between them.

    So READY requires both -- the loop has started *and* the thread is
    currently responsive. The caller should still debounce; see
    `App._poll_pygui`.

    `ignore` excludes window handles that already existed before the launch.
    Without it, clicking a device that already has a PyGUI open would match
    that older window and report READY immediately, dismissing the splash
    while the new instance was still starting.

    Always returns a state and never raises.

    THE PLATFORM GUARD IS FOR THE CONTRACT, NOT FOR POSIX SUPPORT, and an
    earlier version of this line read "reports ABSENT off Windows" as though
    that were a supported mode. It is not: **EMCC cannot start off Windows.**

        emcc/splash.py:129-130   `ctypes.windll.user32` at MODULE scope,
                                 no platform guard
        ctypes/__init__.py:476   `windll` defined only under
                                 `if _os.name == "nt"`
        emcc/app.py:33           imports `splash` at module level

    Those three compose: importing `emcc.app` on POSIX evaluates
    `ctypes.windll` and raises. Static, and complete on its own -- an attempt
    to confirm it by deleting `ctypes.windll` and importing was worthless,
    because leaving `sys.platform == "win32"` made every platform branch take
    the Windows path and fail for want of a library. That simulates a broken
    Windows box, not POSIX.

    So the guard is kept for two reasons that survive the app being
    Windows-only: it preserves "never raises", which callers rely on **on the
    platform we do ship**, and it is exercised by tests that stub the
    platform. It is NOT a portability affordance, and deleting it as
    unreachable would take the contract with it.
    """
    if sys.platform != "win32":
        return PyGuiState.ABSENT
    windows = [h for h in pygui_windows(ip) if h not in set(ignore)]
    if not windows:
        return PyGuiState.ABSENT

    hwnd = windows[0]
    user32 = ctypes.windll.user32
    if not user32.IsWindowVisible(hwnd):
        return PyGuiState.STARTING

    result = ctypes.c_size_t(0)
    responsive = user32.SendMessageTimeoutW(
        hwnd, _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG, 60, ctypes.byref(result)
    )
    return PyGuiState.READY if responsive else PyGuiState.STARTING


def _interpreter(root: Path) -> str:
    """The interpreter to launch PyGUI with, best available first.

    Preference order:

    1. PyGUI's own `.venv` -- its dependencies are the ones it was developed
       against, and it measured ~0.5s faster to import than the system
       install.
    2. `pythonw.exe` beside the interpreter running EMCC.
    3. That interpreter itself (a non-Windows checkout, or an unusual layout).

    `pythonw` rather than `python` throughout, so PyGUI opens without a
    console window behind it.
    """
    venv = root / ".venv" / "Scripts" / "pythonw.exe"
    if venv.is_file():
        return str(venv)
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


class Launch(NamedTuple):
    """The outcome of a hand-off attempt.

    `error` is a message for the operator, or `None` on success. `process` is
    the launched child, or `None` if nothing was started.

    **The process is returned rather than discarded**, and that is the whole
    point of this type. `poll_pygui` previously had no way to tell a crash
    from a slow start -- its own comment said so -- because readiness was
    inferred from window enumeration and a dead process enumerates exactly
    like one that has not drawn yet. A `Popen` answers the question directly.
    """

    error: str | None = None
    process: "subprocess.Popen | None" = None


def launch_pygui(ip: str, pygui_path: str) -> Launch:
    """Launch PyGUI pointed at `ip`.

    Never raises: a failed hand-off should report itself and leave EMCC
    running.
    """
    ip = (ip or "").strip()
    if not ip:
        return Launch("This device has no IP address set.")

    root = Path(pygui_path).expanduser()
    entry = root / PYGUI_ENTRY

    if not root.is_dir():
        return Launch(f"PyGUI was not found at:\n{root}\n\n"
                      "Set 'pygui_path' in config.json to its folder.")
    if not entry.is_file():
        return Launch(f"PyGUI is missing {PYGUI_ENTRY} at:\n{root}\n\n"
                      "Set 'pygui_path' in config.json to its folder.")

    handoff = os.environ.copy()
    handoff[ENV_HANDOFF_IP] = ip

    try:
        process = subprocess.Popen(
            [_interpreter(root), PYGUI_ENTRY],
            cwd=str(root),
            env=handoff,
            # Detached: PyGUI outlives EMCC, and EMCC's shutdown must not wait
            # on it or kill it.
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if sys.platform == "win32" else 0,
            close_fds=True,
        )
    except OSError as exc:
        logger.error("PyGUI hand-off for %s failed to launch: %s", ip, exc)
        return Launch(f"PyGUI could not be started:\n{exc}")

    logger.info("handed %s over to PyGUI (%s), pid %s", ip, root, process.pid)
    return Launch(process=process)
