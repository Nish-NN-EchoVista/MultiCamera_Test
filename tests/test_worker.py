"""ConnectionWorker tests against the mock NPort.

These exercise the real socket path -- connect, stale drain, streaming,
commands, unexpected drop, reconnect and shutdown -- without EVS2 hardware.
Timing-sensitive settings are shrunk so the suite stays fast.
"""

from __future__ import annotations

import logging
import queue
import time

import pytest

from emcc.backend.config_manager import Settings
from emcc.backend.connection_worker import ConnectionWorker
from emcc.backend.events import ConnectionState, DeviceEvent, EventType
from emcc.backend.protocol import CLEAN_COMPLETE, CMD_CLEAN

from tools.mock_nport import MockConfig, MockNPort

FAST = dict(
    connection_timeout_s=1.0,
    reconnect_attempts=3,
    reconnect_interval_s=0.2,
    temperature_update_interval_s=0.0,   # emit every sample
    temperature_warning_c=65.0,
)


def make_worker(port: int, **overrides) -> tuple[ConnectionWorker, "queue.Queue"]:
    settings = Settings(tcp_port=port, **{**FAST, **overrides})
    events: "queue.Queue[DeviceEvent]" = queue.Queue()
    worker = ConnectionWorker("dev-1", "Camera 1", "127.0.0.1", settings, events)
    return worker, events


def wait_for(events: "queue.Queue[DeviceEvent]", kind: EventType,
             timeout: float = 5.0) -> DeviceEvent:
    """Pull events until `kind` arrives, or fail."""
    seen: list[EventType] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(event.type)
        if event.type is kind:
            return event
    pytest.fail(f"{kind} not seen within {timeout}s; saw {seen}")


def stop(worker: ConnectionWorker) -> None:
    worker.stop()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "worker failed to stop"


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


def test_connects_and_reports_connected():
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        assert worker.state is ConnectionState.CONNECTED
        stop(worker)


def test_connection_failure_when_nothing_listening():
    mock = MockNPort(MockConfig(refuse=True)).start()
    worker, events = make_worker(mock.port)
    worker.start()
    event = wait_for(events, EventType.CONNECTION_FAILED)
    assert event.message
    assert worker.state is ConnectionState.ERROR
    worker.join(timeout=3.0)
    assert not worker.is_alive()   # exits rather than retrying an initial failure


def test_connect_timeout_to_unroutable_address():
    settings = Settings(tcp_port=4001, connection_timeout_s=0.4)
    events: "queue.Queue[DeviceEvent]" = queue.Queue()
    # TEST-NET-1, guaranteed not to route.
    worker = ConnectionWorker("d", "Camera 1", "192.0.2.1", settings, events)
    worker.start()
    event = wait_for(events, EventType.CONNECTION_FAILED, timeout=6.0)
    assert "timed out" in event.message.lower() or "unreachable" in event.message.lower()
    stop(worker)


def test_manual_disconnect_emits_disconnected_and_does_not_reconnect():
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        worker.disconnect()
        wait_for(events, EventType.DISCONNECTED)
        worker.join(timeout=3.0)
        assert not worker.is_alive()
        # No reconnect attempt should follow.
        time.sleep(0.4)
        remaining = []
        while not events.empty():
            remaining.append(events.get_nowait().type)
        assert EventType.RECONNECTING not in remaining


# ---------------------------------------------------------------------------
# Stale buffer drain
# ---------------------------------------------------------------------------


def test_stale_backlog_is_discarded_on_connect():
    """A buffered completion must not survive to false-complete a later Clean.

    This is the bug the drain exists to prevent: an NPort holds whatever the
    UART emitted while unattached, so the first read can deliver an old
    `Evaporation Sweep Completed`.
    """
    backlog = [f"PZT Temp: {40 + i}.0 C" for i in range(30)]
    backlog.append(f"[ESV2-1] {CLEAN_COMPLETE}")
    config = MockConfig(stale_backlog=backlog, temp_interval_s=99)  # no live temps
    with MockNPort(config) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        time.sleep(0.4)
        kinds = []
        while not events.empty():
            kinds.append(events.get_nowait().type)
        assert EventType.CLEAN_COMPLETE not in kinds, \
            "stale completion leaked through the drain"
        stop(worker)


# ---------------------------------------------------------------------------
# Streaming temperature
# ---------------------------------------------------------------------------


def test_streams_temperature():
    with MockNPort(MockConfig(temp_start=44.2, temp_drift=0.0)) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        event = wait_for(events, EventType.TEMPERATURE)
        assert event.value == pytest.approx(44.2, abs=0.05)
        stop(worker)


def test_temperature_is_throttled():
    """At 2 Hz in with a 1 s interval, roughly half the samples should emit."""
    config = MockConfig(temp_start=40.0, temp_drift=0.0, temp_interval_s=0.05)
    with MockNPort(config) as mock:
        worker, events = make_worker(mock.port, temperature_update_interval_s=0.5)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        time.sleep(1.2)
        temps = 0
        while not events.empty():
            if events.get_nowait().type is EventType.TEMPERATURE:
                temps += 1
        # ~24 samples arrived; throttling should let only a handful through.
        assert 1 <= temps <= 6, f"expected throttling, got {temps} emissions"
        stop(worker)


def test_threshold_crossing_emits_immediately():
    """A crossing must not wait out the throttle interval."""
    config = MockConfig(temp_start=40.0, temp_drift=0.0, temp_interval_s=0.05)
    with MockNPort(config) as mock:
        worker, events = make_worker(
            mock.port, temperature_update_interval_s=30.0, temperature_warning_c=65.0
        )
        worker.start()
        wait_for(events, EventType.CONNECTED)
        wait_for(events, EventType.TEMPERATURE)      # first sample
        mock.push("PZT Temp: 80.0 C")                # crossing
        event = wait_for(events, EventType.TEMPERATURE, timeout=2.0)
        assert event.value == pytest.approx(80.0)
        stop(worker)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_clean_command_is_sent_bare_and_crlf_terminated():
    """Bare, not @N -- the broadcast branch also drives the pump gating."""
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        worker.send(CMD_CLEAN)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not mock.received_commands:
            time.sleep(0.05)
        assert mock.received_commands == [CMD_CLEAN]
        stop(worker)


def test_clean_completion_is_reported():
    with MockNPort(MockConfig(clean_duration_s=0.2)) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        worker.send(CMD_CLEAN)
        event = wait_for(events, EventType.CLEAN_COMPLETE)
        assert event.channel in (1, 2)
        stop(worker)


def test_commands_are_delivered_in_order():
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        for command in ("enableauto", "dis_auto", CMD_CLEAN):
            worker.send(command)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(mock.received_commands) < 3:
            time.sleep(0.05)
        assert mock.received_commands == ["enableauto", "dis_auto", CMD_CLEAN]
        stop(worker)


def test_send_does_not_block_when_peer_is_silent():
    """The UI thread must never be stalled by a wedged device."""
    with MockNPort(MockConfig(silent=True)) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        started = time.monotonic()
        for _ in range(200):
            worker.send(CMD_CLEAN)
        assert time.monotonic() - started < 0.5
        stop(worker)


# ---------------------------------------------------------------------------
# Unexpected drop and reconnect
# ---------------------------------------------------------------------------


def test_unexpected_drop_triggers_reconnect_and_recovers():
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)

        mock.disconnect_client()
        wait_for(events, EventType.CONNECTION_LOST)
        wait_for(events, EventType.RECONNECTING)
        wait_for(events, EventType.CONNECTED, timeout=6.0)
        assert worker.state is ConnectionState.CONNECTED
        assert mock.connection_count >= 2
        stop(worker)


def test_reconnect_exhaustion_reports_failure():
    mock = MockNPort(MockConfig()).start()
    worker, events = make_worker(mock.port)
    worker.start()
    wait_for(events, EventType.CONNECTED)

    # Drops the client and removes the listener at once, so every retry fails.
    mock.stop()

    wait_for(events, EventType.CONNECTION_LOST, timeout=6.0)
    event = wait_for(events, EventType.RECONNECT_FAILED, timeout=10.0)
    assert "reconnection failed" in event.message.lower()
    assert worker.state is ConnectionState.ERROR
    worker.join(timeout=3.0)
    assert not worker.is_alive()


def test_reconnect_makes_the_configured_number_of_attempts():
    # No drop_after_s. With it, the mock drops repeatedly: a reconnect can
    # succeed and correctly reset the counter before the listener is removed,
    # so the observed sequence becomes [1, 1, 2, 3]. Stopping the mock drops
    # the client and removes the listener at once, giving exactly one loss.
    mock = MockNPort(MockConfig()).start()
    worker, events = make_worker(mock.port, reconnect_attempts=3)
    worker.start()
    wait_for(events, EventType.CONNECTED)
    mock.stop()

    attempts = []
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event.type is EventType.RECONNECTING:
            attempts.append(event.attempt)
        if event.type is EventType.RECONNECT_FAILED:
            break
    assert attempts == [1, 2, 3]
    worker.join(timeout=3.0)


def test_shutdown_during_reconnect_wait_is_prompt():
    """Stopping must not have to wait out the reconnect interval."""
    mock = MockNPort(MockConfig(drop_after_s=0.2)).start()
    worker, events = make_worker(mock.port, reconnect_interval_s=30.0)
    worker.start()
    wait_for(events, EventType.CONNECTED)
    mock.stop()
    wait_for(events, EventType.CONNECTION_LOST, timeout=6.0)
    time.sleep(0.3)   # let it enter the 30s back-off

    started = time.monotonic()
    worker.stop()
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    assert time.monotonic() - started < 2.0, "stop waited out the back-off"


def test_shutdown_is_silent():
    """No events after stop() -- the UI is going away."""
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        while not events.empty():
            events.get_nowait()
        stop(worker)
        leftovers = []
        while not events.empty():
            leftovers.append(events.get_nowait().type)
        assert EventType.CONNECTION_LOST not in leftovers
        assert EventType.RECONNECTING not in leftovers


def test_socket_is_closed_after_stop():
    with MockNPort(MockConfig()) as mock:
        worker, events = make_worker(mock.port)
        worker.start()
        wait_for(events, EventType.CONNECTED)
        stop(worker)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and mock.has_client:
            time.sleep(0.05)
        assert not mock.has_client, "server still sees a client after stop"


def test_second_client_is_refused_like_a_real_nport():
    """NPort TCP Server mode defaults to Max Connection = 1."""
    with MockNPort(MockConfig()) as mock:
        first, first_events = make_worker(mock.port)
        first.start()
        wait_for(first_events, EventType.CONNECTED)

        second, second_events = make_worker(mock.port)
        second.start()
        # The mock accepts then immediately closes, so the second worker sees
        # the link die rather than never opening.
        deadline = time.monotonic() + 4.0
        outcomes = []
        while time.monotonic() < deadline:
            try:
                outcomes.append(second_events.get(timeout=0.2).type)
            except queue.Empty:
                continue
            if EventType.CONNECTION_LOST in outcomes or \
               EventType.CONNECTION_FAILED in outcomes:
                break
        assert outcomes, "second worker produced no events"
        stop(second)
        stop(first)


# ---------------------------------------------------------------------------
# The self-pipe
# ---------------------------------------------------------------------------


def test_a_failed_self_pipe_wake_is_reported(caplog):
    """A broken self-pipe degrades the loop silently, so it must be logged.

    `_wake` used to swallow the `OSError`. Nothing broke -- the loop checks
    the stop flags at the top of each iteration and flushes queued commands
    unconditionally -- so a lost wake only *delays* work, by up to
    `_SELECT_TIMEOUT_S`. That is exactly what the module docstring denies
    ("the loop does not poll") and what the comment on that constant denies
    ("a safety net rather than a polling interval"). The application keeps
    running, a second slower, with nothing said.

    **Interrogated before the worker is stopped**, which is the point.
    `test_send_does_not_block_when_peer_is_silent` has the tightest timing
    bound in this file and is still blind to this: its 200 sends return
    immediately either way, because `send` only enqueues, and the lost second
    lands in `stop()` -- after its assertion.

    No socket and no thread: `_wake` is reachable on an unstarted worker, so
    this needs neither the mock NPort nor a second of real time.

    Dies on: swallowing the OSError again, or dropping the level below
    WARNING.
    """
    worker, _events = make_worker(1)          # never started, never connected
    worker._wake_w.close()                    # the write end a dead socket leaves

    with caplog.at_level(logging.WARNING, logger="emcc.device"):
        worker.send(CMD_CLEAN)

    reported = [r for r in caplog.records if "self-pipe wake failed" in r.getMessage()]
    assert reported, (
        "a failed wake was swallowed. The loop still works, one second "
        "slower, and nothing anywhere says so."
    )
    assert reported[0].levelno >= logging.WARNING, (
        f"reported at {reported[0].levelname}, which the emcc loggers filter"
    )
    message = reported[0].getMessage()
    assert "Camera 1" in message, "the message must identify the device"
    assert "late" in message, "the message must say what the operator loses"


def test_the_self_pipe_warning_is_logged_once_not_per_send(caplog):
    """A dead self-pipe is persistent, and `send` is called constantly.

    Without the guard this floods the log file that someone has to read to
    find the warning in the first place -- 200 sends in the sibling timing
    test would be 200 identical lines.

    Dies on: removing the `_wake_failed` guard.
    """
    worker, _events = make_worker(1)
    worker._wake_w.close()

    with caplog.at_level(logging.WARNING, logger="emcc.device"):
        for _ in range(25):
            worker.send(CMD_CLEAN)
        worker.disconnect()                   # also wakes
        worker.stop()                         # and so does this

    reported = [r for r in caplog.records if "self-pipe wake failed" in r.getMessage()]
    assert len(reported) == 1, (
        f"{len(reported)} warnings from 27 wake attempts -- the once-only "
        f"guard is gone"
    )


def test_a_healthy_self_pipe_says_nothing(caplog):
    """The negative control.

    Without it, warning unconditionally would pass both tests above while
    crying wolf on every command the application ever sends -- and a warning
    that fires always is read as noise, which is how the real one is missed.
    """
    worker, _events = make_worker(1)

    with caplog.at_level(logging.WARNING, logger="emcc.device"):
        worker.send(CMD_CLEAN)

    assert not [r for r in caplog.records if "self-pipe" in r.getMessage()], (
        "warned about a working self-pipe"
    )
