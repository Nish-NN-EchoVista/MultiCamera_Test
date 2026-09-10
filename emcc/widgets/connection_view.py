"""Maps backend connection state onto the design's three button treatments.

The Figma export has three looks for the Connect button; the backend has six
states. The extra ones (CONNECTING, RECONNECTING) need somewhere to live, and
inventing new colours would break the design language -- so they reuse an
existing treatment and are distinguished by label and caption instead.

Kept in one function so there is exactly one place that decides how a device
reads, and so the UI never has to inspect widget colour to work out what is
going on.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import theme
from ..backend.events import ConnectionState


@dataclass(frozen=True, slots=True)
class ConnectionVisual:
    """Everything the Connect button needs to render one state."""

    style: str          # key into theme.CONN
    label: str
    caption: str


def connection_visual(
    state: ConnectionState,
    *,
    attempt: int = 0,
    total_attempts: int = 0,
    error: str = "",
    has_ip: bool = True,
) -> ConnectionVisual:
    """Describe how `state` should look.

    Blue means nothing is wrong. Green means connected. Red means the device is
    not connected and nobody asked for that, which is why an initial failure
    and an exhausted reconnect both stay red rather than reverting to blue --
    reverting would make a broken device look identical to an idle one.
    """
    if state is ConnectionState.CONNECTED:
        return ConnectionVisual("connected", "Connected", "Stream active")

    if state is ConnectionState.CONNECTING:
        return ConnectionVisual("idle", "Connecting…", "Opening connection")

    if state is ConnectionState.RECONNECTING:
        caption = (
            f"Reconnecting {attempt}/{total_attempts}…"
            if attempt and total_attempts
            else "Reconnecting…"
        )
        return ConnectionVisual("fault", "Reconnecting…", caption)

    if state is ConnectionState.ERROR:
        # `error` is already short, operator-facing text from the worker.
        return ConnectionVisual("fault", "Reconnect",
                                error or "Connection failed")

    if state is ConnectionState.STOPPING:
        return ConnectionVisual("idle", "Connect", "Stopping…")

    # DISCONNECTED: never connected, or the operator asked for it.
    caption = "Not connected" if has_ip else "No IP address set"
    return ConnectionVisual("idle", "Connect", caption)


def palette(style: str) -> dict:
    return theme.CONN[style]
