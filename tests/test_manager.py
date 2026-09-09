"""DeviceManager tests.

Timers are driven through an injected fake scheduler rather than real time, so
the Clean timeout and the post-connect Auto reset are deterministic. That is
the whole reason `DeviceManager` takes `schedule`/`cancel` instead of calling
`after()` itself.
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field

import pytest

from emcc.backend.config_manager import ConfigManager
from emcc.backend.device_manager import DeviceManager
from emcc.backend.events import ConnectionState, DeviceEvent, EventType
from emcc.backend.protocol import CMD_AUTO_OFF, CMD_AUTO_ON

from tools.mock_nport import MockConfig, MockNPort


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeScheduler:
    """Records scheduled callbacks so tests can fire or assert on them."""

    jobs: dict[int, tuple[int, object]] = field(default_factory=dict)
    _next: int = 0

    def schedule(self, delay_ms: int, callback):
        self._next += 1
        handle = self._next
        self.jobs[handle] = (delay_ms, callback)
        return handle

    def cancel(self, handle) -> None:
        self.jobs.pop(handle, None)

    def fire_all(self) -> int:
        """Run every pending job. Returns how many fired."""
        pending = list(self.jobs.items())
        self.jobs.clear()
        for _handle, (_delay, callback) in pending:
            callback()
        return len(pending)

    @property
    def pending(self) -> int:
        return len(self.jobs)


@pytest.fixture()
def manager(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    config.load()
    # 3.0s (the production default), not a shorter value: under load the
    # worker would otherwise abandon a connect the mock had *already*
    # accepted, and the mock -- faithfully refusing a second client, as a real
    # NPort does -- closes the retry, surfacing as a spurious "peer closed the
    # connection". Tests that want a fast failure set this locally.
    config.settings.connection_timeout_s = 3.0
    config.settings.reconnect_interval_s = 0.2
    config.settings.temperature_update_interval_s = 0.0
    scheduler = FakeScheduler()
    mgr = DeviceManager(config, scheduler.schedule, scheduler.cancel)
    mgr.scheduler = scheduler          # test-only handle
    yield mgr
    mgr.shutdown(timeout_s=2.0)
    config.close()


def put(manager: DeviceManager, event_type: EventType, device_id: str, **kwargs):
    manager.events.put(DeviceEvent(type=event_type, device_id=device_id, **kwargs))
    return manager.drain_events()


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def test_devices_come_from_config(manager):
    assert len(manager.devices) == 3
    assert manager.devices[0].name == "Camera 1"
    assert manager.devices[0].ip == "192.168.2.250"


def test_all_devices_start_disconnected(manager):
    assert all(
        d.connection is ConnectionState.DISCONNECTED for d in manager.devices
    )
    assert manager.connected_count == 0


def test_startup_leaves_auto_off_and_clean_idle(manager):
    for device in manager.devices:
        assert device.auto_enabled is False
        assert device.clean_active is False
        assert device.latest_temperature is None


def test_add_device_persists_and_appends(manager):
    state = manager.add_device()
    assert state.name == "Camera 4"
    assert len(manager.devices) == 4
    manager.config.flush()
    reloaded = ConfigManager(manager.config.path)
    reloaded.load()
    assert len(reloaded.devices) == 4


def test_remove_device_persists(manager):
    target = manager.devices[1]
    manager.remove_device(target.id)
    assert len(manager.devices) == 2
    assert manager.get(target.id) is None
    manager.config.flush()
    reloaded = ConfigManager(manager.config.path)
    reloaded.load()
    assert len(reloaded.devices) == 2


def test_rename_and_ip_edit_persist(manager):
    device = manager.devices[0]
    manager.rename_device(device.id, "Left Nozzle")
    manager.set_ip(device.id, " 192.168.2.99 ")
    manager.config.flush()
    reloaded = ConfigManager(manager.config.path)
    reloaded.load()
    assert reloaded.devices[0].device_name == "Left Nozzle"
    assert reloaded.devices[0].ip_address == "192.168.2.99"   # trimmed


# ---------------------------------------------------------------------------
# IP validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip", ["192.168.2.250", "10.0.0.1", "127.0.0.1"])
def test_valid_ipv4_accepted(ip):
    assert DeviceManager.validate_ip(ip) is None


@pytest.mark.parametrize(
    "ip,fragment",
    [
        ("", "No IP"),
        ("   ", "No IP"),
        ("192.168.2", "not a valid"),
        ("192.168.2.999", "not a valid"),
        ("hello", "not a valid"),
        ("192.168.2.1/24", "not a valid"),
        ("::1", "IPv6"),
    ],
)
def test_invalid_ip_rejected(ip, fragment):
    error = DeviceManager.validate_ip(ip)
    assert error is not None and fragment.lower() in error.lower()


def test_connect_with_invalid_ip_starts_no_worker(manager):
    device = manager.devices[0]
    manager.set_ip(device.id, "not-an-ip")
    error = manager.toggle_connection(device.id)
    assert error is not None
    assert device.connection is ConnectionState.DISCONNECTED
    assert manager._workers == {}


# ---------------------------------------------------------------------------
# State transitions driven by events
# ---------------------------------------------------------------------------


def test_connected_event_sets_state_and_schedules_auto_reset(manager):
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    assert device.connection is ConnectionState.CONNECTED
    assert manager.connected_count == 1
    # Auto must always start OFF, with the reset queued.
    assert device.auto_enabled is False
    assert manager.scheduler.pending == 1


def test_connection_failed_sets_error_state(manager):
    device = manager.devices[0]
    put(manager, EventType.CONNECTION_FAILED, device.id, message="timed out")
    assert device.connection is ConnectionState.ERROR
    assert device.last_error == "timed out"


def test_disconnected_event_sets_disconnected(manager):
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    put(manager, EventType.DISCONNECTED, device.id)
    assert device.connection is ConnectionState.DISCONNECTED


def test_reconnecting_tracks_attempt_numbers(manager):
    device = manager.devices[0]
    put(manager, EventType.RECONNECTING, device.id, attempt=2, total_attempts=3)
    assert device.connection is ConnectionState.RECONNECTING
    assert (device.reconnect_attempt, device.reconnect_total) == (2, 3)


def test_reconnect_failed_sets_error(manager):
    device = manager.devices[0]
    put(manager, EventType.RECONNECT_FAILED, device.id, message="gave up")
    assert device.connection is ConnectionState.ERROR


def test_link_down_clears_transient_state(manager):
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    device.auto_enabled = True
    device.clean_active = True
    device.latest_temperature = 70.0
    device.temperature_alert = True

    put(manager, EventType.CONNECTION_LOST, device.id, message="reset")
    assert device.auto_enabled is False
    assert device.clean_active is False
    assert device.latest_temperature is None
    assert device.temperature_alert is False


def test_events_for_removed_device_are_ignored(manager):
    """A stale event in flight must not resurrect a deleted device."""
    device = manager.devices[0]
    device_id = device.id
    manager.remove_device(device_id)
    changed = put(manager, EventType.TEMPERATURE, device_id, value=50.0)
    assert changed == []


# ---------------------------------------------------------------------------
# Temperature and threshold
# ---------------------------------------------------------------------------


def test_temperature_below_threshold_is_not_alert(manager):
    device = manager.devices[0]
    put(manager, EventType.TEMPERATURE, device.id, value=64.0)
    assert device.latest_temperature == 64.0
    assert device.temperature_alert is False


def test_temperature_at_threshold_is_not_alert(manager):
    """Spec: strictly greater than the threshold turns red."""
    device = manager.devices[0]
    put(manager, EventType.TEMPERATURE, device.id, value=65.0)
    assert device.temperature_alert is False


def test_temperature_above_threshold_is_alert(manager):
    device = manager.devices[0]
    put(manager, EventType.TEMPERATURE, device.id, value=66.5)
    assert device.temperature_alert is True


def test_threshold_crossing_both_ways(manager):
    device = manager.devices[0]
    put(manager, EventType.TEMPERATURE, device.id, value=66.5)
    assert device.temperature_alert is True
    put(manager, EventType.TEMPERATURE, device.id, value=64.0)
    assert device.temperature_alert is False


def test_only_the_affected_device_alerts(manager):
    first, second = manager.devices[0], manager.devices[1]
    put(manager, EventType.TEMPERATURE, first.id, value=80.0)
    assert first.temperature_alert is True
    assert second.temperature_alert is False
    assert manager.alert_count() == 1


def test_threshold_comes_from_config(manager):
    manager.config.settings.temperature_warning_c = 50.0
    device = manager.devices[0]
    put(manager, EventType.TEMPERATURE, device.id, value=55.0)
    assert device.temperature_alert is True


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def test_clean_requires_a_connection(manager):
    device = manager.devices[0]
    assert manager.start_clean(device.id) is False
    assert device.clean_active is False


def test_clean_timeout_clears_the_flag(manager):
    """The 10s timeout must reset Clean with no completion message."""
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect_to_mock(manager, mock)
        assert manager.start_clean(device.id) is True
        assert device.clean_active is True
        assert manager.scheduler.pending >= 1
        manager.scheduler.fire_all()
        assert device.clean_active is False


def test_clean_completion_clears_flag_and_cancels_timer(manager):
    """Completion must cancel the timeout, not just clear the flag.

    The timer assertion is the point of this test, and it was missing: with
    only `clean_active is False`, deleting `_cancel_clean_timer` from the
    CLEAN_COMPLETE branch left the whole suite green. The consequence is a
    scheduled callback that outlives the sweep it belonged to.
    """
    with MockNPort(MockConfig(clean_duration_s=0.1)) as mock:
        device = _connect_to_mock(manager, mock)
        manager.start_clean(device.id)
        timer = device._clean_timer
        assert timer is not None and timer in manager.scheduler.jobs

        _drain_until(manager, lambda: device.clean_active is False, timeout=4.0)
        assert device.clean_active is False
        # The scheduler was actually told, not merely the bookkeeping cleared.
        assert timer not in manager.scheduler.jobs
        assert device._clean_timer is None


def test_repeated_clean_presses_restart_the_timeout(manager):
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect_to_mock(manager, mock)
        manager.scheduler.fire_all()          # clear the auto-reset timer

        manager.start_clean(device.id)
        first_handle = device._clean_timer
        assert manager.scheduler.pending == 1

        manager.start_clean(device.id)
        assert device._clean_timer != first_handle   # replaced, not reused
        assert manager.scheduler.pending == 1        # and not accumulating
        assert first_handle not in manager.scheduler.jobs   # old one cancelled
        assert device.clean_active is True


def test_clean_timer_does_not_fire_after_removal(manager):
    """A pending timer must not touch a deleted device."""
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect_to_mock(manager, mock)
        manager.start_clean(device.id)
        manager.remove_device(device.id)
        assert manager.scheduler.pending == 0     # cancelled on removal
        manager.scheduler.fire_all()              # no-op, must not raise


def test_clean_completion_for_second_board_is_harmless(manager):
    """Both boards complete; the first clears it and the second is ignored."""
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    device.clean_active = True
    put(manager, EventType.CLEAN_COMPLETE, device.id, channel=1)
    assert device.clean_active is False
    put(manager, EventType.CLEAN_COMPLETE, device.id, channel=2)
    assert device.clean_active is False


# ---------------------------------------------------------------------------
# Auto
# ---------------------------------------------------------------------------


def test_auto_requires_a_connection(manager):
    device = manager.devices[0]
    assert manager.set_auto(device.id, True) is False
    assert device.auto_enabled is False


def test_auto_toggle_sends_commands(manager):
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        assert manager.set_auto(device.id, True) is True
        assert device.auto_enabled is True
        assert manager.set_auto(device.id, False) is True
        assert device.auto_enabled is False
        _wait_for_commands(mock, 2)
        assert mock.received_commands[:2] == [CMD_AUTO_ON, CMD_AUTO_OFF]


def test_manual_auto_toggle_cancels_the_scheduled_reset(manager):
    """Otherwise the post-connect dis_auto would silently undo the operator."""
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        assert manager.scheduler.pending == 1      # the auto reset
        manager.set_auto(device.id, True)
        assert manager.scheduler.pending == 0      # cancelled
        assert device.auto_enabled is True


def test_auto_reset_fires_after_connect(manager):
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        manager.scheduler.fire_all()
        _wait_for_commands(mock, 1)
        assert CMD_AUTO_OFF in mock.received_commands
        assert device.auto_enabled is False


def test_shutdown_leaves_no_timer_pending(manager):
    """A manager timer outliving shutdown fires into a dead interpreter.

    `App.shutdown` cancels only its own jobs (`_pump_job`, `_build_job`) and
    then delegates to `manager.shutdown`, whose `_cancel_timers` loop is the
    *only* thing that cancels manager-owned timers -- they run through the
    injected scheduler, so nothing else knows they exist.

    This earns a test for an unusual reason: the symptom of losing it is not
    wrong state, it is a pending callback firing into a destroyed Tk
    interpreter -- `invalid command name`, dead `pyimage`. That is exactly
    what a verification charter classifies as *environmental, re-run*, so the
    defect would hide behind a correct-looking classification indefinitely.

    Asserts the property rather than the method: nothing outlives shutdown.

    Dies on: removing the `_cancel_timers(state)` loop from `shutdown`.
    """
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect_to_mock(manager, mock)
        manager.start_clean(device.id)
        assert manager.scheduler.jobs, "expected clean and auto-reset pending"

        manager.shutdown(timeout_s=4.0)
        assert manager.scheduler.jobs == {}


def test_toggling_auto_cancels_the_pending_post_connect_reset(manager):
    """Otherwise the reset fires and silently turns Auto back off.

    Connecting schedules a 2s Auto reset. `set_auto` cancels it, because an
    operator who reaches for Auto inside that window means it -- and a reset
    arriving afterwards would undo a deliberate action with no indication.

    This is a product risk rather than a test gap, which is why it earns a
    test of its own: without the cancellation the only symptom in the suite
    is three Auto tests in `test_ui_integration.py` going *intermittently*
    flaky, because they assert `auto_enabled` inside that same 2s window.
    The natural response to a flaky test is to move its assertion, which
    would leave the real defect sitting in `set_auto`.

    Dies on: removing `self._cancel_auto_reset(state)` from `set_auto`.
    """
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        handle = device._auto_reset_timer
        assert handle is not None, "connecting should schedule the reset"
        assert handle in manager.scheduler.jobs

        assert manager.set_auto(device.id, True) is True
        assert device._auto_reset_timer is None
        assert handle not in manager.scheduler.jobs, "reset was not cancelled"

        # Nothing left that could flip it back.
        manager.scheduler.fire_all()
        assert device.auto_enabled is True


def test_auto_reset_is_rescheduled_after_reconnect(manager):
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    manager.scheduler.fire_all()
    put(manager, EventType.CONNECTION_LOST, device.id, message="drop")
    put(manager, EventType.CONNECTED, device.id)
    assert manager.scheduler.pending == 1


def test_auto_ack_does_not_drive_ui_state(manager):
    """The UI toggle is authoritative; an ACK must never fight it."""
    device = manager.devices[0]
    put(manager, EventType.CONNECTED, device.id)
    device.auto_enabled = True
    put(manager, EventType.AUTO_ACK, device.id, auto_enabled=False)
    assert device.auto_enabled is True


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_no_duplicate_worker_for_one_device(manager):
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        before = manager._workers[device.id]
        manager._start_worker(device)           # a second connect click
        assert manager._workers[device.id] is before
        assert mock.connection_count == 1


def test_toggle_disconnects_when_connected(manager):
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        manager.toggle_connection(device.id)
        _drain_until(
            manager,
            lambda: device.connection is ConnectionState.DISCONNECTED,
            timeout=4.0,
        )
        assert device.connection is ConnectionState.DISCONNECTED


def test_remove_connected_device_stops_worker(manager):
    with MockNPort(MockConfig()) as mock:
        device = _connect_to_mock(manager, mock)
        device_id = device.id
        manager.remove_device(device_id)
        assert device_id not in manager._workers
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and mock.has_client:
            time.sleep(0.05)
        assert not mock.has_client, "socket left open after removal"


def test_shutdown_stops_every_worker(manager):
    mocks = [MockNPort(MockConfig()).start() for _ in range(3)]
    try:
        for device, mock in zip(manager.devices, mocks):
            manager.set_ip(device.id, "127.0.0.1")
            manager.config.settings.tcp_port = mock.port
            manager.toggle_connection(device.id)
            _drain_until(manager, lambda d=device: d.is_connected, timeout=4.0)
        manager.shutdown(timeout_s=4.0)
        assert manager._workers == {}
    finally:
        for mock in mocks:
            mock.stop()


def test_drain_events_is_bounded(manager):
    """A burst must not stall the GUI in one tick."""
    device = manager.devices[0]
    for index in range(50):
        manager.events.put(DeviceEvent(type=EventType.TEMPERATURE,
                                       device_id=device.id, value=float(index)))
    manager.drain_events(limit=10)
    assert manager.events.qsize() == 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect_to_mock(manager: DeviceManager, mock: MockNPort):
    device = manager.devices[0]
    manager.config.settings.tcp_port = mock.port
    manager.set_ip(device.id, "127.0.0.1")
    assert manager.toggle_connection(device.id) is None
    _drain_until(manager, lambda: device.is_connected, timeout=4.0)
    assert device.is_connected, "did not reach CONNECTED"
    return device


def _drain_until(manager: DeviceManager, predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manager.drain_events()
        if predicate():
            return
        time.sleep(0.02)
    manager.drain_events()


def _wait_for_commands(mock: MockNPort, count: int, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(mock.received_commands) < count:
        time.sleep(0.02)
