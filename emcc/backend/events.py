"""The vocabulary that crosses the worker/UI thread boundary.

Workers never touch a widget. They put immutable `DeviceEvent` values on a
queue; the Tkinter main thread drains it and renders. Everything here is
frozen and carries only plain data, so nothing shared can be mutated from two
threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class ConnectionState(Enum):
    """Backend connection state for one device.

    The UI renders this; it is never inferred from widget colour.
    """

    DISCONNECTED = auto()   # never connected, or the operator disconnected
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()
    #: A connect attempt failed, or reconnection was exhausted. Distinct from
    #: DISCONNECTED so the UI can show red for "not our choice" and blue for
    #: "you asked for this".
    ERROR = auto()
    STOPPING = auto()

    @property
    def is_live(self) -> bool:
        """True when a socket exists or is being established."""
        return self in (
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.RECONNECTING,
        )


class EventType(Enum):
    CONNECTED = auto()
    DISCONNECTED = auto()          # graceful, operator-initiated
    CONNECTION_FAILED = auto()     # initial connect failed
    CONNECTION_LOST = auto()       # established connection dropped
    RECONNECTING = auto()
    RECONNECT_FAILED = auto()      # all attempts exhausted
    TEMPERATURE = auto()
    CLEAN_COMPLETE = auto()
    AUTO_ACK = auto()
    #: The AdapterController hit its thermal limit and sent `stop` to both
    #: boards, so any Clean in progress has already been aborted upstream.
    LOCKOUT = auto()
    LOCKOUT_CLEARED = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    """One thing that happened to one device.

    Attributes
    ----------
    type:
        What happened.
    device_id:
        Stable internal id, never the user-editable name.
    value:
        Numeric payload (temperature in Celsius) where applicable.
    message:
        Short, operator-readable text. Full technical detail belongs in the log,
        not here.
    attempt, total_attempts:
        Reconnect progress, for the "Reconnecting (2/3)" caption.
    channel:
        EVS2 board 1 or 2 when the originating line was tagged, else None.
    auto_enabled:
        For AUTO_ACK: what the firmware said it did. Informational only -- the
        UI is authoritative for the Auto toggle, so this is logged rather than
        rendered.
    """

    type: EventType
    device_id: str
    value: float | None = None
    message: str = ""
    attempt: int = 0
    total_attempts: int = 0
    channel: int | None = None
    auto_enabled: bool | None = None
