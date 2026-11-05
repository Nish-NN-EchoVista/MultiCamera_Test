"""What a device log keeps at each level, decided one row at a time.

Three levels, chosen in the Settings view and applied to every device whose
Log button is on:

* **Full** -- every line the device sends, plus the commands EMCC sends it and
  its connection events.
* **Reduced** -- the repeating readings are kept only when they move, and a
  line repeated back to back is folded into one row with a count.
* **Important only** -- errors, warnings and device triggers, plus the few
  context rows that frame a session (logging started/stopped, connected,
  disconnected).

MEASURED, not estimated. Two real PyGUI captures of a dual-board system over
Moxa -- `diagnostic-20261006-140135` (70,885 lines, 100 min) and
`diagnostic-20261029-165944` (69,827 lines, 435 min) -- are 96% four shapes:
`Power: 0.0W`, `Board Temperature = 36.8`, `Thermal check pass` and
`PZT Temp: 24.4 C`. Simulated on those captures, Reduced kept 8.9% and 4.2%
of the rows and Important only 1.8% and 2.2%. Writing a row costs ~235 us of
CPU on the bench PC, so the level is the largest lever on capture cost.

This module is pure: no threads, no files. `LevelFilter` is owned by the
capture writer thread and is never touched from anywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ..protocol import parse_pzt_temperature

# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


class LogLevel(str, Enum):
    FULL = "full"
    REDUCED = "reduced"
    IMPORTANT = "important"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]

    @classmethod
    def parse(cls, value: object) -> "LogLevel":
        """`value` as a level; the default for anything unrecognised.

        Accepts a `LogLevel`, its value (`"reduced"`) or its label, so a
        hand-edited config.json cannot stop the app from starting.
        """
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for level in cls:
            if text in (level.value, level.label.lower()):
                return level
        return DEFAULT_LEVEL


_LABELS = {
    LogLevel.FULL: "Full logs",
    LogLevel.REDUCED: "Reduced logs",
    LogLevel.IMPORTANT: "Important only",
}

_DESCRIPTIONS = {
    LogLevel.FULL: "Every line each device sends, plus the commands EMCC sends "
                   "and connection events.",
    LogLevel.REDUCED: "Repeating readings (power, temperatures, thermal check) "
                      "only when they change or once a minute; repeated lines "
                      "folded into one row with a count.",
    LogLevel.IMPORTANT: "Errors, warnings and device triggers, plus "
                        "connect/disconnect and logging start/stop.",
}

DEFAULT_LEVEL = LogLevel.REDUCED

# ---------------------------------------------------------------------------
# Entries and their classification
# ---------------------------------------------------------------------------

#: What produced an entry.
RX = "rx"        # a line the device sent
TX = "tx"        # a command EMCC sent
EVENT = "event"  # something EMCC observed: connected, lost, logging started

#: Severities an EVENT can carry. RX lines get theirs from a `W:`/`E:` prefix.
INFO, WARNING, ERROR = "info", "warning", "error"

#: Sheets in each hourly workbook, in tab order. "All" holds every row; each
#: row is also copied to the sheet named by its category, when there is one.
CATEGORIES = (
    "All",
    "Events",
    "Warnings",
    "PZT Temp",
    "Board Temp - ESV2-1",
    "Board Temp - ESV2-2",
    "Board Temp",
    "Power - ESV2-1",
    "Power - ESV2-2",
    "Power",
)

#: Source column for rows that did not come from a tagged EVS2 board.
SOURCE_ADAPTER = "Adapter"
SOURCE_EMCC = "EMCC"


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing captured for one device, before any level is applied."""

    ts: float
    kind: str
    text: str
    severity: str = INFO
    #: A context row (logging started/stopped, connected, disconnected).
    #: Kept at every level -- see the module docstring.
    context: bool = False


@dataclass(frozen=True, slots=True)
class Classified:
    """How an entry is filed, and what the levels need to know about it."""

    message: str
    source: str
    category: str
    value: float | None = None
    units: str = ""
    important: bool = False
    #: Set for the repeating readings Reduced thins out: a key naming the
    #: reading, the number (or text) it carries, and the deadband. A `band`
    #: of None means "keep when the text changes".
    periodic: str | None = None
    reading: object = None
    band: float | None = None


_TAG_RE = re.compile(r"^\[(ESV2-\d)\]\s*(.*)$")
_SEVERITY_RE = re.compile(r"^([WE]):\s")
_POWER_RE = re.compile(r"^Power:\s*(-?\d+(?:\.\d+)?)\s*W", re.IGNORECASE)
_BOARD_TEMP_RE = re.compile(r"^Board Temperature\s*=\s*(-?\d+(?:\.\d+)?)",
                            re.IGNORECASE)
_THERMAL_RE = re.compile(r"^Thermal check\s+(\w+)", re.IGNORECASE)
#: AdapterController's echo of a command EMCC sent. EMCC's own TX row already
#: records the command, so the echo is filed under Events but is not a trigger.
_ECHO_PREFIX = "TX to ESV2-"
#: Device triggers and state changes worth keeping at "Important only".
#: Matched against real firmware lines: "Evaporation sweep triggered",
#: "I: DeFog sequence started (PID-driven)", "DeIce Completed",
#: "I: DeFog sequence exited", "AutoClean enabled", "Received clean trigger
#: from ESV2-1", "PZT overheat detected. Entering cooldown lockout",
#: "PZT temperature normalized. Re-enabling system.", "I: System4 boot
#: success", "W: System rebooted due to watchdog".
_TRIGGER_RE = re.compile(
    r"triggered|\bstarted\b|completed|exited|AutoClean (?:en|dis)abled"
    r"|trigger from|overheat|lockout|normalized|watchdog|reboot|boot success"
    r"|\berror\b|\bfail|\bfault",
    re.IGNORECASE,
)

#: Reduced keeps a reading when it moves by at least this much.
TEMPERATURE_BAND_C = 0.5
POWER_BAND_W = 0.5


def classify(entry: Entry) -> Classified:
    """File one entry. Never raises: anything unrecognised goes to "All" only."""
    if entry.kind == TX:
        return Classified(f"> {entry.text}", SOURCE_EMCC, "Events",
                          important=True)

    if entry.kind == EVENT:
        warning = entry.severity in (WARNING, ERROR)
        return Classified(entry.text, SOURCE_EMCC,
                          "Warnings" if warning else "Events",
                          important=warning or entry.context)

    line = entry.text.strip()
    match = _TAG_RE.match(line)
    source, body = (match.group(1), match.group(2).strip()) if match \
        else (SOURCE_ADAPTER, line)
    board = match is not None

    if _SEVERITY_RE.match(body):
        return Classified(line, source, "Warnings", important=True)

    if body.lower().startswith("pzt temp:"):
        value = parse_pzt_temperature(body)
        if value is not None:
            return Classified(line, source, "PZT Temp", value, "°C",
                              periodic="pzt", reading=value,
                              band=TEMPERATURE_BAND_C)

    found = _BOARD_TEMP_RE.match(body)
    if found:
        value = float(found.group(1))
        return Classified(line, source,
                          f"Board Temp - {source}" if board else "Board Temp",
                          value, "°C", periodic="board_temp", reading=value,
                          band=TEMPERATURE_BAND_C)

    found = _POWER_RE.match(body)
    if found:
        value = float(found.group(1))
        return Classified(line, source,
                          f"Power - {source}" if board else "Power",
                          value, "W", periodic="power", reading=value,
                          band=POWER_BAND_W)

    found = _THERMAL_RE.match(body)
    if found:
        result = found.group(1).lower()
        # A failing thermal check is never thinned out: "fail" is a trigger.
        return Classified(line, source, "",
                          important=bool(_TRIGGER_RE.search(body)),
                          periodic="thermal", reading=result, band=None)

    if body.startswith(_ECHO_PREFIX):
        return Classified(line, source, "Events")

    if _TRIGGER_RE.search(body):
        return Classified(line, source, "Events", important=True)

    return Classified(line, source, "")


# ---------------------------------------------------------------------------
# The Reduced and Important-only filters
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Kept:
    reading: object
    ts: float


@dataclass(slots=True)
class _Run:
    """A line and how many identical copies have been folded into it."""

    message: str
    classified: Classified
    kept_ts: float
    last_ts: float
    folded: int = 0


@dataclass(slots=True)
class LevelFilter:
    """Decides which rows reach the file. Writer-thread only.

    `apply` returns the rows to write for one entry -- none, the entry itself,
    or a pending repeat-count row followed by the entry.
    """

    #: A thinned reading, or a folded repeat, is written again after this long
    #: even if nothing changed, so a quiet hour still shows the device alive.
    heartbeat_s: float = 60.0
    _kept: dict = field(default_factory=dict)
    _runs: dict = field(default_factory=dict)

    def apply(self, level: LogLevel, device: str, entry: Entry,
              info: Classified) -> list[tuple[Entry, Classified]]:
        if level is LogLevel.FULL:
            return [(entry, info)]
        if level is LogLevel.IMPORTANT:
            return [(entry, info)] if (info.important or entry.context) else []
        if entry.context:
            # Context rows are never folded: two "Connected" rows an hour
            # apart are two connections, not a repeat.
            return [(entry, info)]
        if info.periodic is not None and not info.important:
            return [(entry, info)] if self._reading_moved(device, info, entry.ts) else []
        return self._fold(device, entry, info)

    def pending(self, device: str | None = None) -> list[tuple[Entry, Classified]]:
        """Repeat-count rows not yet written, for `device` or every device.

        Called when logging stops, the level changes or an hour closes, so a
        count is never lost with the run it belongs to.
        """
        rows = []
        for key in [k for k in self._runs if device is None or k[0] == device]:
            run = self._runs.pop(key)
            if run.folded:
                rows.append(_count_row(run))
        return rows

    def pending_devices(self) -> set[str]:
        """Devices holding a folded run whose count is not yet written."""
        return {key[0] for key, run in self._runs.items() if run.folded}

    def forget(self, device: str) -> None:
        for store in (self._kept, self._runs):
            for key in [k for k in store if k[0] == device]:
                del store[key]

    # -- rules -------------------------------------------------------------

    def _reading_moved(self, device: str, info: Classified, ts: float) -> bool:
        key = (device, info.source, info.periodic)
        last = self._kept.get(key)
        if last is not None and ts - last.ts < self.heartbeat_s:
            if info.band is None:
                moved = info.reading != last.reading
            else:
                moved = abs(info.reading - last.reading) >= info.band
            if not moved:
                return False
        self._kept[key] = _Kept(info.reading, ts)
        return True

    def _fold(self, device: str, entry: Entry,
              info: Classified) -> list[tuple[Entry, Classified]]:
        key = (device, info.source)
        run = self._runs.get(key)
        if (run is not None and run.message == info.message
                and entry.ts - run.kept_ts < self.heartbeat_s):
            run.folded += 1
            run.last_ts = entry.ts
            return []
        rows = [_count_row(run)] if run is not None and run.folded else []
        self._runs[key] = _Run(info.message, info, entry.ts, entry.ts)
        rows.append((entry, info))
        return rows


def _count_row(run: _Run) -> tuple[Entry, Classified]:
    """The row recording how many copies of a line were folded away."""
    times = "time" if run.folded == 1 else "times"
    text = f"(previous line repeated {run.folded:,} more {times})"
    info = Classified(text, run.classified.source, run.classified.category,
                      important=run.classified.important)
    return Entry(run.last_ts, EVENT, text), info
