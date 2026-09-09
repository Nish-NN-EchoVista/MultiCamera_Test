"""Device collection, worker lifecycle and command dispatch.

`DeviceManager` is the only thing the UI talks to. It holds the authoritative
backend state for every device, starts and stops workers, dispatches commands,
and owns the two timers the spec requires (the post-connect Auto reset and the
Clean visual timeout).

It runs on the Tk main thread but contains no Tk: timers go through an injected
`schedule`/`cancel` pair, so tests drive them with a fake clock. That also
keeps the timer bookkeeping in one place, which is what makes cancellation on
device removal reliable -- a stale `after()` callback firing against a deleted
card is exactly the leak the spec warns about.

Validation happens here too: an obviously invalid IP never gets a worker.
"""

from __future__ import annotations

import ipaddress
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

from .config_manager import ConfigManager, DeviceConfig
from .connection_worker import ConnectionWorker
from .events import ConnectionState, DeviceEvent, EventType
from .protocol import CMD_AUTO_OFF, CMD_AUTO_ON, CMD_CLEAN

logger = logging.getLogger("emcc.device")

#: Bounded so a pathological stream cannot exhaust memory. Far larger than any
#: realistic burst: 25 devices at 2 Hz is 50 events/s and the UI drains at
#: 10 Hz.
EVENT_QUEUE_MAX = 10000

ScheduleFn = Callable[[int, Callable[[], None]], object]
CancelFn = Callable[[object], None]


@dataclass
class DeviceState:
    """Authoritative backend state for one device.

    The UI renders this. It is never the other way round -- nothing reads a
    widget to find out what the backend is doing.
    """

    config: DeviceConfig
    connection: ConnectionState = ConnectionState.DISCONNECTED
    auto_enabled: bool = False
    clean_active: bool = False
    latest_temperature: float | None = None
    temperature_alert: bool = False
    #: Set while the AdapterController is in cooldown lockout. Hardware refuses
    #: to run sequences in this state, so Clean is pointless until it clears.
    thermal_lockout: bool = False
    #: Whether the current fault has already been shown to the operator, so a
    #: single failure does not re-pop a dialog on every re-render.
    fault_announced: bool = False
    reconnect_attempt: int = 0
    reconnect_total: int = 0
    last_error: str = ""

    #: Timer handles, owned so they can be cancelled on removal or state change.
    _clean_timer: object | None = field(default=None, repr=False)
    _auto_reset_timer: object | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.device_name

    @property
    def ip(self) -> str:
        return self.config.ip_address

    @property
    def is_connected(self) -> bool:
        return self.connection is ConnectionState.CONNECTED


class DeviceManager:
    def __init__(
        self,
        config: ConfigManager,
        schedule: ScheduleFn,
        cancel: CancelFn,
    ):
        self.config = config
        self._schedule = schedule
        self._cancel = cancel

        self.events: "queue.Queue[DeviceEvent]" = queue.Queue(maxsize=EVENT_QUEUE_MAX)
        self._devices: dict[str, DeviceState] = {}
        self._workers: dict[str, ConnectionWorker] = {}
        self._order: list[str] = []
        self._shutting_down = False
        self._lock = threading.Lock()   # guards _workers against late arrivals

        for device in config.devices:
            self._devices[device.id] = DeviceState(config=device)
            self._order.append(device.id)

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    @property
    def devices(self) -> list[DeviceState]:
        return [self._devices[i] for i in self._order if i in self._devices]

    def get(self, device_id: str) -> DeviceState | None:
        return self._devices.get(device_id)

    @property
    def connected_count(self) -> int:
        return sum(1 for d in self.devices if d.is_connected)

    def alert_count(self) -> int:
        return sum(1 for d in self.devices if d.temperature_alert)

    def add_device(self) -> DeviceState:
        """Append a device and persist it."""
        device_config = DeviceConfig(device_name=self.config.next_device_name())
        self.config.add_device(device_config)
        state = DeviceState(config=device_config)
        self._devices[device_config.id] = state
        self._order.append(device_config.id)
        logger.info("device created: %s", device_config.device_name)
        return state

    def remove_device(self, device_id: str) -> None:
        """Stop the worker, cancel timers, drop the device and persist."""
        state = self._devices.pop(device_id, None)
        if state is None:
            return
        if device_id in self._order:
            self._order.remove(device_id)

        self._cancel_timers(state)
        worker = self._detach_worker(device_id)
        if worker is not None:
            worker.stop()
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.warning("%s worker did not stop within 2s", state.name)

        self.config.remove_device(device_id)
        logger.info("device removed: %s [%s]", state.name, state.ip or "no IP")

    def rename_device(self, device_id: str, name: str) -> None:
        state = self._devices.get(device_id)
        if state is None:
            return
        state.config.device_name = name
        worker = self._workers.get(device_id)
        if worker is not None:
            worker.rename(name)
        self.config.request_save()

    def set_ip(self, device_id: str, ip: str) -> None:
        state = self._devices.get(device_id)
        if state is None:
            return
        state.config.ip_address = ip.strip()
        self.config.request_save()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @staticmethod
    def validate_ip(ip: str) -> str | None:
        """Return an error message, or None when the address is usable.

        IPv4 only for now. Written so hostname/IPv6 support is a local change
        here rather than a change to the worker.
        """
        text = (ip or "").strip()
        if not text:
            return "No IP address set"
        try:
            ipaddress.IPv4Address(text)
        except ipaddress.AddressValueError:
            try:
                ipaddress.IPv6Address(text)
            except ipaddress.AddressValueError:
                return f"{text!r} is not a valid IPv4 address"
            return "IPv6 addresses are not supported yet"
        return None

    def toggle_connection(self, device_id: str) -> str | None:
        """Connect, or disconnect if already live. Returns a validation error.

        Clicking a failed (red) device retries, which is why ERROR is treated
        the same as DISCONNECTED here.
        """
        state = self._devices.get(device_id)
        if state is None or self._shutting_down:
            return None

        if state.connection.is_live:
            self.disconnect(device_id)
            return None

        error = self.validate_ip(state.ip)
        if error is not None:
            logger.warning("%s connect rejected: %s", state.name, error)
            return error

        self._start_worker(state)
        return None

    def _start_worker(self, state: DeviceState) -> None:
        # Guard against a second worker for one card -- the spec calls this out
        # explicitly, and it would produce two sockets and duplicate events.
        existing = self._workers.get(state.id)
        if existing is not None and existing.is_alive():
            logger.debug("%s already has a live worker; ignoring connect",
                         state.name)
            return

        worker = ConnectionWorker(
            device_id=state.id,
            device_name=state.name,
            ip_address=state.ip,
            settings=self.config.settings,
            events=self.events,
        )
        with self._lock:
            self._workers[state.id] = worker
        state.connection = ConnectionState.CONNECTING
        state.last_error = ""
        worker.start()

    def disconnect(self, device_id: str) -> None:
        """Operator-initiated disconnect. Suppresses reconnection."""
        state = self._devices.get(device_id)
        worker = self._workers.get(device_id)
        if state is not None:
            self._cancel_timers(state)
            state.clean_active = False
            state.auto_enabled = False
        if worker is not None:
            worker.disconnect()

    def _detach_worker(self, device_id: str) -> ConnectionWorker | None:
        with self._lock:
            return self._workers.pop(device_id, None)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _send(self, state: DeviceState, command: str) -> bool:
        worker = self._workers.get(state.id)
        if worker is None or not state.is_connected:
            return False
        worker.send(command)
        return True

    def start_clean(self, device_id: str) -> bool:
        """Issue Clean and (re)start the visual timeout.

        Deliberately sends the *bare* command. The AdapterController's
        broadcast branch reaches both boards and also runs the pump gating
        (`is_sequence_start` -> `update_pump_pin`), which the `@N` routed
        branches skip.

        Repeated presses are allowed and each one restarts the timeout; the
        firmware copes with duplicate commands.
        """
        state = self._devices.get(device_id)
        if state is None or not self._send(state, CMD_CLEAN):
            return False

        logger.info("%s Clean command issued", state.name)
        state.clean_active = True
        self._restart_clean_timer(state)
        return True

    def _restart_clean_timer(self, state: DeviceState) -> None:
        self._cancel_clean_timer(state)
        delay_ms = int(self.config.settings.clean_timeout_s * 1000)
        state._clean_timer = self._schedule(
            delay_ms, lambda: self._on_clean_timeout(state.id)
        )

    def _cancel_clean_timer(self, state: DeviceState) -> None:
        if state._clean_timer is not None:
            self._cancel(state._clean_timer)
            state._clean_timer = None

    def _on_clean_timeout(self, device_id: str) -> None:
        state = self._devices.get(device_id)
        if state is None:      # removed while the timer was pending
            return
        state._clean_timer = None
        if state.clean_active:
            logger.info("%s Clean timed out after %.0fs with no completion",
                        state.name, self.config.settings.clean_timeout_s)
            state.clean_active = False
            self._notify_ui(device_id)

    def set_auto(self, device_id: str, enabled: bool) -> bool:
        """Toggle Auto.

        Sends the bare command, matching firmware behaviour: bare `enableauto`
        reaches ESV2-1 only (the ESV2-2 line is commented out upstream), while
        bare `dis_auto` broadcasts. The hardware is not currently configured
        for autoclean, so a missing or unexpected acknowledgement is expected
        and is not treated as a fault -- this path is architectural.

        The UI is authoritative: the toggle does not wait for an ACK.
        """
        state = self._devices.get(device_id)
        if state is None:
            return False

        # Toggling by hand cancels the scheduled post-connect reset, which
        # would otherwise silently disable Auto behind the operator's back.
        self._cancel_auto_reset(state)

        command = CMD_AUTO_ON if enabled else CMD_AUTO_OFF
        if not self._send(state, command):
            return False
        logger.info("%s Auto %s", state.name, "ON" if enabled else "OFF")
        state.auto_enabled = enabled
        return True

    def _schedule_auto_reset(self, state: DeviceState) -> None:
        """Queue the post-connect `dis_auto`.

        Auto must always start logically OFF on a new connection, including
        after an automatic reconnect. Delayed slightly to let the firmware
        settle after the link comes up.
        """
        self._cancel_auto_reset(state)
        delay_ms = int(self.config.settings.auto_reset_delay_s * 1000)
        state._auto_reset_timer = self._schedule(
            delay_ms, lambda: self._on_auto_reset(state.id)
        )

    def _cancel_auto_reset(self, state: DeviceState) -> None:
        if state._auto_reset_timer is not None:
            self._cancel(state._auto_reset_timer)
            state._auto_reset_timer = None

    def _on_auto_reset(self, device_id: str) -> None:
        state = self._devices.get(device_id)
        if state is None:
            return
        state._auto_reset_timer = None
        if not state.is_connected:
            return
        if self._send(state, CMD_AUTO_OFF):
            logger.info("%s Auto reset to OFF after connection", state.name)
        state.auto_enabled = False
        self._notify_ui(device_id)

    def _cancel_timers(self, state: DeviceState) -> None:
        self._cancel_clean_timer(state)
        self._cancel_auto_reset(state)

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------

    #: Set by the UI so the manager can request a re-render after changing
    #: state from a timer.
    on_device_changed: Callable[[str], None] | None = None

    def _notify_ui(self, device_id: str) -> None:
        if self.on_device_changed is not None:
            try:
                self.on_device_changed(device_id)
            except Exception:
                logger.exception("UI update callback raised")

    def drain_events(self, limit: int = 500) -> list[str]:
        """Apply queued worker events to backend state.

        Called from the Tk main thread. Returns the ids of devices that
        changed, so the caller can re-render only those cards.

        `limit` bounds the work done in one tick so a burst cannot stall the
        GUI; anything left over is picked up on the next tick.
        """
        changed: list[str] = []
        for _ in range(limit):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            device_id = self._apply(event)
            if device_id is not None and device_id not in changed:
                changed.append(device_id)
        return changed

    def _apply(self, event: DeviceEvent) -> str | None:
        state = self._devices.get(event.device_id)
        if state is None:
            return None   # device removed while the event was in flight

        kind = event.type

        if kind is EventType.CONNECTED:
            state.connection = ConnectionState.CONNECTED
            state.reconnect_attempt = 0
            state.reconnect_total = 0
            state.last_error = ""
            # Auto always starts OFF on a new link, reconnects included.
            state.auto_enabled = False
            self._schedule_auto_reset(state)

        elif kind is EventType.DISCONNECTED:
            state.connection = ConnectionState.DISCONNECTED
            self._on_link_down(state)
            self._detach_worker(state.id)

        elif kind is EventType.CONNECTION_FAILED:
            state.connection = ConnectionState.ERROR
            state.last_error = event.message
            self._on_link_down(state)
            self._detach_worker(state.id)

        elif kind is EventType.CONNECTION_LOST:
            state.connection = ConnectionState.RECONNECTING
            state.last_error = event.message
            self._on_link_down(state)

        elif kind is EventType.RECONNECTING:
            state.connection = ConnectionState.RECONNECTING
            state.reconnect_attempt = event.attempt
            state.reconnect_total = event.total_attempts

        elif kind is EventType.RECONNECT_FAILED:
            state.connection = ConnectionState.ERROR
            state.last_error = event.message
            self._on_link_down(state)
            self._detach_worker(state.id)

        elif kind is EventType.TEMPERATURE:
            state.latest_temperature = event.value
            state.temperature_alert = (
                event.value is not None
                and event.value > self.config.settings.temperature_warning_c
            )

        elif kind is EventType.CLEAN_COMPLETE:
            # First completion from either board clears it. Waiting for both
            # would never finish on a single-board system.
            if state.clean_active:
                state.clean_active = False
                self._cancel_clean_timer(state)

        elif kind is EventType.AUTO_ACK:
            # Logged by the worker; the UI toggle stays authoritative so an
            # ACK never fights the operator.
            return None

        elif kind is EventType.LOCKOUT:
            # Hardware already stopped both boards, so Clean is over whether or
            # not a completion message follows.
            state.thermal_lockout = True
            state.last_error = event.message
            if state.clean_active:
                state.clean_active = False
                self._cancel_clean_timer(state)
            state.auto_enabled = False

        elif kind is EventType.LOCKOUT_CLEARED:
            state.thermal_lockout = False
            state.last_error = ""

        elif kind is EventType.ERROR:
            state.last_error = event.message

        return state.id

    def _on_link_down(self, state: DeviceState) -> None:
        """Common cleanup whenever the link is no longer usable."""
        self._cancel_timers(state)
        state.clean_active = False
        state.auto_enabled = False
        state.latest_temperature = None
        state.temperature_alert = False

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, timeout_s: float = 4.0) -> None:
        """Stop every worker and wait, bounded.

        Signals all workers first and only then joins, so the waits overlap
        instead of accumulating -- 25 devices take about as long as one.
        """
        self._shutting_down = True

        for state in list(self._devices.values()):
            self._cancel_timers(state)

        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()

        if not workers:
            logger.info("shutdown: no active connections")
            return

        logger.info("shutdown: stopping %d worker(s)", len(workers))
        for worker in workers:
            worker.stop()

        deadline = timeout_s
        for worker in workers:
            if deadline <= 0:
                break
            import time
            started = time.monotonic()
            worker.join(timeout=deadline)
            deadline -= time.monotonic() - started

        stuck = [w.device_name for w in workers if w.is_alive()]
        if stuck:
            # Sockets are closed in the worker's finally block regardless, so
            # this is a slow exit rather than a leak.
            logger.warning("shutdown: %d worker(s) still running: %s",
                           len(stuck), ", ".join(stuck))
        else:
            logger.info("shutdown: all workers stopped cleanly")
