"""EVS2 / AdapterController wire protocol.

Every constant here was verified against firmware source; see
`multicameraUI_masterplan.md` Parts 2 and 2B for the citations. Do not change a
string in this module without re-checking the firmware.

Topology
--------
    EVS2 #1 --UART USART2--.
                            F401 AdapterController --USART1--> Moxa NPort --TCP--> EMCC
    EVS2 #2 --UART USART6--'   (own thermocouple, PZT Temp @2Hz)

One IP address is one whole system: two EVS2 boards behind one AdapterController
behind one NPort serial port. EMCC treats that as a single device.

Command routing
---------------
The AdapterController accepts `@1 <cmd>` / `@2 <cmd>` to address one board, and
treats anything else as a broadcast to both. EMCC deliberately sends **bare**
commands, because the broadcast branch also runs the pump-gating logic
(`is_sequence_start` -> `update_pump_pin`) which the `@N` branches skip. PyGUI
uses `@N` because it is a single-board diagnostic tool; that is a different job.

Receive shape
-------------
Anything originating from an EVS2 board is tagged by the AdapterController:

    [ESV2-1] Evaporation Sweep Completed
    [ESV2-2] AutoClean enabled

Anything untagged came from the AdapterController itself:

    PZT Temp: 43.7 C                    <- the temperature EMCC displays
    TX to ESV2-1: start_evap_sweep      <- command echo, must be ignored
    I: Pump enabled (PB12 gated ...)
    Received clean trigger from ESV2-1
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto

# ---------------------------------------------------------------------------
# Commands (verified: EVS2 user_commands.c:465-470)
# ---------------------------------------------------------------------------

CMD_CLEAN = "start_evap_sweep"
CMD_AUTO_ON = "enableauto"
CMD_AUTO_OFF = "dis_auto"

#: EVS2's RX handler dispatches only on CR immediately followed by LF
#: (uart_buffer.c:44-49), so this terminator is mandatory, not conventional.
TERMINATOR = "\r\n"

#: Prefix that routes to a single board. EMCC does not use it -- see the module
#: docstring -- but the parser must recognise the echo it produces.
ROUTE_PREFIX = "@"


def encode_command(command: str) -> bytes:
    """Frame a command for the wire."""
    return (command + TERMINATOR).encode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Response strings (verified against both firmwares)
# ---------------------------------------------------------------------------

#: EVS2 sequences.c:155 and :467.
CLEAN_COMPLETE = "Evaporation Sweep Completed"

#: Near-misses that must NOT register as completion. `Evaporation Sweep Low
#: Power Completed` in particular defeats any naive
#: `"Evaporation Sweep" in line and "Completed" in line` test.
_CLEAN_NEAR_MISSES = (
    "Evaporation Sweep Low Power Completed",   # EVS2 sequences.c:207
    "AutoClean triggered Evaporation Sweep",   # EVS2 sequences.c:504
)

#: EVS2 user_commands.c:265 / :281.
AUTO_ON_ACK = "AutoClean enabled"
AUTO_OFF_ACK = "AutoClean disabled"

# ---------------------------------------------------------------------------
# Thermal lockout (AdapterController thermocouple.c:56-72)
#
# This one matters operationally rather than cosmetically. On entering
# lockout the AdapterController sends `stop` to *both* boards, so any Clean in
# progress is aborted -- if EMCC ignored it, the Clean button would sit green
# for its full timeout while the hardware had already stopped.
# ---------------------------------------------------------------------------

LOCKOUT_ENTERED = "PZT overheat detected. Entering cooldown lockout"
LOCKOUT_CLEARED = "PZT temperature normalized. Re-enabling system."

#: AdapterController command echo, uart_handler.c:238 / :242. Contains the
#: command text, so anything scanning received data for *command* strings will
#: false-positive on it.
_ECHO_PREFIX = "TX to ESV2-"

# ---------------------------------------------------------------------------
# Line tagging (AdapterController uart_handler.c:319 / :347)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"^\[ESV2-(\d)\]\s*(.*)$")

# ---------------------------------------------------------------------------
# Temperature (AdapterController thermocouple.c:86-93, every 500 ms)
#
#   int temp_int = (int)temp;
#   int temp_dec = (int)((temp - temp_int) * 10 + 0.5f);   // rounded tenths
#   uart_print(1, "PZT Temp: %d.%d C", temp_int, abs(temp_dec));
#
# Two firmware artefacts have to be handled, and both were observed on real
# hardware:
#
# 1. `temp_dec` is a *rounded* tenths value with no carry into `temp_int`, so
#    when the fraction is >= 0.95 it becomes 10 and the line reads
#    `PZT Temp: 240.10 C` -- which means 241.0, not 240.1. Reading the text as
#    a decimal number is therefore wrong by 0.9 whenever it happens. The
#    fields are parsed separately and recombined as `whole + tenths/10`, which
#    gets this right for free.
# 2. The sign lives only on the integer part, so values between 0 and -1 print
#    without it (-0.4 -> "0.4"). That is unrecoverable from the text and is
#    deliberately not guessed at.
# ---------------------------------------------------------------------------

_PZT_TEMP_RE = re.compile(
    r"^PZT Temp:\s*(-?)(\d+)(?:\.(\d+))?\s*C\b", re.IGNORECASE
)


def parse_pzt_temperature(payload: str) -> float | None:
    """Extract the temperature from a `PZT Temp:` line, or None."""
    match = _PZT_TEMP_RE.match(payload)
    if match is None:
        return None
    sign = -1.0 if match.group(1) else 1.0
    try:
        whole = int(match.group(2))
    except ValueError:
        return None
    digits = match.group(3)
    if digits is None:
        return sign * whole
    try:
        # Firmware emits %d, so no leading zeros: the digits are always a
        # tenths count, which is why 10 must add a whole degree.
        tenths = int(digits)
    except ValueError:
        return None
    return sign * (whole + tenths / 10.0)

#: EVS2 also emits board temperatures. They must not be mistaken for the PZT
#: reading. (`Temperature  = %f  C` is currently dead code in EVS2 -- reached
#: only via an uncalled function -- but `Board Temperature` is live.)
_OTHER_TEMP_PREFIXES = ("Board Temperature", "Temperature ")


class MessageKind(Enum):
    """What a received line means to EMCC."""

    PZT_TEMPERATURE = auto()
    CLEAN_COMPLETE = auto()
    AUTO_ON_ACK = auto()
    AUTO_OFF_ACK = auto()
    LOCKOUT_ENTERED = auto()
    LOCKOUT_CLEARED = auto()
    COMMAND_ECHO = auto()
    #: Recognised as EVS2/AdapterController chatter, deliberately not acted on.
    OTHER = auto()


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One decoded line.

    Attributes
    ----------
    kind:
        What the line means.
    raw:
        The line as received, tag included, for logging.
    payload:
        The line with any `[ESV2-N] ` tag stripped.
    channel:
        1 or 2 when the line came from a tagged EVS2 board, else None for
        AdapterController-originated lines.
    value:
        Numeric payload where the kind carries one (temperature in Celsius).
    """

    kind: MessageKind
    raw: str
    payload: str
    channel: int | None = None
    value: float | None = None


def split_tag(line: str) -> tuple[int | None, str]:
    """Split `[ESV2-N] rest` into `(N, rest)`; `(None, line)` when untagged."""
    match = _TAG_RE.match(line)
    if match is None:
        return None, line
    return int(match.group(1)), match.group(2)


def parse_line(line: str) -> ParsedMessage:
    """Classify a single complete line. Never raises."""
    raw = line
    stripped = line.strip()
    channel, payload = split_tag(stripped)
    payload = payload.strip()

    if payload.startswith(_ECHO_PREFIX):
        return ParsedMessage(MessageKind.COMMAND_ECHO, raw, payload, channel)

    # Temperature is untagged (it is the AdapterController's own sensor), but
    # accept a tagged form too rather than silently dropping it if the firmware
    # ever starts tagging.
    if payload.lower().startswith("pzt temp:"):
        value = parse_pzt_temperature(payload)
        if value is not None:
            return ParsedMessage(
                MessageKind.PZT_TEMPERATURE, raw, payload, channel, value
            )
        return ParsedMessage(MessageKind.OTHER, raw, payload, channel)

    # Order matters: the near-miss check must precede the completion check.
    if payload in _CLEAN_NEAR_MISSES:
        return ParsedMessage(MessageKind.OTHER, raw, payload, channel)
    if payload == CLEAN_COMPLETE:
        return ParsedMessage(MessageKind.CLEAN_COMPLETE, raw, payload, channel)

    if payload == AUTO_ON_ACK:
        return ParsedMessage(MessageKind.AUTO_ON_ACK, raw, payload, channel)
    if payload == AUTO_OFF_ACK:
        return ParsedMessage(MessageKind.AUTO_OFF_ACK, raw, payload, channel)

    if payload == LOCKOUT_ENTERED:
        return ParsedMessage(MessageKind.LOCKOUT_ENTERED, raw, payload, channel)
    if payload == LOCKOUT_CLEARED:
        return ParsedMessage(MessageKind.LOCKOUT_CLEARED, raw, payload, channel)

    return ParsedMessage(MessageKind.OTHER, raw, payload, channel)


class LineAssembler:
    """Turns a TCP byte stream into complete lines.

    TCP gives no message boundaries: one `recv()` may hold half a line, or six
    lines, or a line split mid-number. Bytes are accumulated until a terminator
    is seen and any incomplete tail is carried forward.

    Both firmwares terminate with CRLF, but a bare LF is accepted too so a
    stray or reconfigured emitter cannot wedge the parser. Decoding uses
    `errors="replace"` because PyGUI's binary bulk-transfer sub-protocol
    (BINSTART/BIN2START) can put non-text bytes on the same wire if someone
    runs the diagnostic GUI against the hardware; a mojibake line is preferable
    to an exception in the receive loop.
    """

    #: Guard against a peer that never sends a terminator. Firmware lines are
    #: capped at 192 (AdapterController) and 256 (EVS2) bytes, so anything an
    #: order of magnitude larger is not a line.
    MAX_BUFFER = 8192

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.overflow_count = 0

    def feed(self, chunk: bytes) -> list[str]:
        """Add received bytes; return whatever complete lines that produced."""
        if not chunk:
            return []
        self._buffer.extend(chunk)

        if len(self._buffer) > self.MAX_BUFFER:
            # Keep the tail: a real terminator is far likelier to arrive next
            # than to be hiding at the front of a run of junk.
            self.overflow_count += 1
            del self._buffer[: -self.MAX_BUFFER // 2]

        lines: list[str] = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            if text.strip():
                lines.append(text)
        return lines

    def reset(self) -> None:
        """Drop any partial line. Call on (re)connect."""
        self._buffer.clear()
