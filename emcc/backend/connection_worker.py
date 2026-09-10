"""One background thread per device, owning one TCP connection.

Named `ConnectionWorker` rather than `DeviceWorker` because the unit of
connection is a whole EVS2 *system* -- two boards behind an AdapterController
behind one Moxa NPort serial port -- which EMCC presents as one device.

Concurrency contract
--------------------
* The worker **never** touches a Tk widget. Its only output is immutable
  `DeviceEvent` values placed on a queue.
* Only this thread ever touches the socket. The UI enqueues commands and
  returns immediately, so a wedged peer can never block the GUI. There is
  deliberately no send lock: a lock held across a slow `sendall()` would be
  held against the Tk thread.
* The loop does not poll. It `select`s on the socket plus a self-pipe, so an
  enqueued command or a stop request wakes it at once instead of waiting out a
  timeout. Reconnect back-off uses `Event.wait()`, which is equally
  interruptible -- shutdown never has to wait 10 seconds for a sleep to end.

Lifecycle
---------
    start()                 -> CONNECTING
      connect fails         -> CONNECTION_FAILED, ERROR, thread exits
      connect succeeds      -> CONNECTED, drain stale buffer, receive loop
    disconnect()            -> DISCONNECTED, thread exits, no reconnection
    peer drops              -> CONNECTION_LOST, then reconnect attempts
      any attempt succeeds  -> CONNECTED, counter reset
      all attempts fail     -> RECONNECT_FAILED, ERROR, thread exits
    stop()                  -> silent teardown (application shutdown)
"""

from __future__ import annotations

import logging
import queue
import select
import socket
import threading
import time

from .config_manager import Settings
from .events import ConnectionState, DeviceEvent, EventType
from .protocol import LineAssembler, MessageKind, encode_command, parse_line

logger = logging.getLogger("emcc.device")

#: Upper bound on a select() wait. The self-pipe makes this a safety net rather
#: than a polling interval -- it only bounds how long we go without noticing a
#: half-open socket.
_SELECT_TIMEOUT_S = 1.0

#: How long to keep reading immediately after connecting, to discard whatever
#: the NPort buffered while no client was attached. Without this the first read
#: can deliver a stale `Evaporation Sweep Completed` and instantly false-
#: complete the operator's next Clean. PyGUI does the same thing.
_DRAIN_WINDOW_S = 0.25


class ConnectionWorker(threading.Thread):
    def __init__(
        self,
        device_id: str,
        device_name: str,
        ip_address: str,
        settings: Settings,
        events: "queue.Queue[DeviceEvent]",
    ):
        super().__init__(name=f"emcc-conn-{device_name}", daemon=False)
        self.device_id = device_id
        self.ip_address = ip_address
        self.settings = settings

        self._device_name = device_name
        self._events = events
        self._outgoing: "queue.Queue[str]" = queue.Queue()

        self._shutdown = threading.Event()          # application shutdown
        self._manual_disconnect = threading.Event()
        self._socket: socket.socket | None = None
        self._assembler = LineAssembler()

        #: Set the first time `_wake` fails, so the warning is emitted
        #: once rather than on every `send`. See `_wake`.
        self._wake_failed = False

        # Self-pipe: written to wake select() when there is work or we must quit.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)

        self._state = ConnectionState.DISCONNECTED
        self._state_lock = threading.Lock()

        self._dropped = 0

        # Temperature emission throttle.
        self._last_temp_emit = 0.0
        self._last_alert: bool | None = None

    # ------------------------------------------------------------------
    # Public API -- all of this is called from the Tk main thread
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    @property
    def device_name(self) -> str:
        return self._device_name

    def rename(self, device_name: str) -> None:
        """Track the display name so log lines stay meaningful after a rename."""
        self._device_name = device_name

    def send(self, command: str) -> None:
        """Queue a command. Returns immediately; never blocks on the socket."""
        self._outgoing.put(command)
        self._wake()

    def disconnect(self) -> None:
        """Operator-initiated disconnect. Suppresses reconnection."""
        self._manual_disconnect.set()
        self._wake()

    def stop(self) -> None:
        """Application shutdown. Silent -- emits no further events."""
        self._shutdown.set()
        self._manual_disconnect.set()
        self._wake()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\x01")
        except OSError as exc:
            # Reported, because this failure is a silent *degradation*
            # rather than a breakage -- the harder kind to notice.
            #
            # The loop checks the stop flags at the top of every iteration
            # and calls `_flush_outgoing` unconditionally, so a lost wake
            # does not lose the command: it delays it by up to
            # `_SELECT_TIMEOUT_S`. The module docstring promises the loop
            # "does not poll", and the comment on that constant calls it
            # "a safety net rather than a polling interval" -- with the
            # self-pipe broken it becomes exactly the polling interval
            # those two lines deny. The application keeps working, one
            # second slower, and nothing said so.
            #
            # Logged once. A dead self-pipe is persistent, and `_wake`
            # runs on every `send` as well as on `disconnect` and `stop`,
            # so logging per call would flood the file someone has to
            # read to find this.
            if not self._wake_failed:
                self._wake_failed = True
                logger.warning(
                    "%s self-pipe wake failed (%s); commands and stop "
                    "requests will now be served up to %.0fs late rather "
                    "than at once, for the life of this worker",
                    self._tag(), exc, _SELECT_TIMEOUT_S,
                )

    def _drain_wake(self) -> None:
        try:
            while True:
                if not self._wake_r.recv(256):
                    break
        except (BlockingIOError, OSError):
            pass

    def _set_state(self, state: ConnectionState) -> None:
        with self._state_lock:
            self._state = state

    def _emit(self, event_type: EventType, **kwargs) -> None:
        """Publish an event, unless we are shutting down.

        Never blocks. The queue is bounded, and a blocking `put` would stall
        this worker -- and therefore its socket and its shutdown -- if the UI
        ever stopped draining. Dropping is the right failure mode: the queue
        holds 10 000 events against a normal rate of ~1/s per device, so if it
        is full something is badly wrong and the newest temperature sample is
        the least valuable thing in the system.
        """
        if self._shutdown.is_set() and event_type is not EventType.DISCONNECTED:
            return
        event = DeviceEvent(type=event_type, device_id=self.device_id, **kwargs)
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 100, 1000):
                logger.error("%s event queue full, dropped %d event(s)",
                             self._tag(), self._dropped)

    def _tag(self) -> str:
        return f"{self._device_name} [{self.ip_address}]"

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except Exception:
            # A worker must never take the app down.
            logger.exception("%s worker crashed", self._tag())
            self._set_state(ConnectionState.ERROR)
            self._emit(EventType.ERROR, message="Internal error; see log")
        finally:
            self._teardown_socket()
            self._close_wake_pipe()
            if self.state is not ConnectionState.ERROR:
                self._set_state(ConnectionState.DISCONNECTED)

    def _run(self) -> None:
        self._set_state(ConnectionState.CONNECTING)
        logger.info("%s connecting", self._tag())

        error = self._open_socket()
        if error is not None:
            self._set_state(ConnectionState.ERROR)
            logger.error("%s connection failed: %s", self._tag(), error)
            self._emit(EventType.CONNECTION_FAILED, message=_friendly(error))
            return

        self._on_connected(first=True)

        # Outer loop: receive until the link drops, then try to restore it.
        while not self._shutdown.is_set() and not self._manual_disconnect.is_set():
            dropped_reason = self._receive_loop()

            if self._shutdown.is_set():
                return
            if self._manual_disconnect.is_set():
                logger.info("%s disconnected by operator", self._tag())
                self._emit(EventType.DISCONNECTED)
                self._set_state(ConnectionState.DISCONNECTED)
                return

            logger.warning("%s connection lost: %s", self._tag(), dropped_reason)
            self._emit(EventType.CONNECTION_LOST, message=_friendly(dropped_reason))

            if not self._reconnect():
                self._set_state(ConnectionState.ERROR)
                logger.error("%s automatic reconnection failed after %d attempts",
                             self._tag(), self.settings.reconnect_attempts)
                self._emit(
                    EventType.RECONNECT_FAILED,
                    message=(f"Automatic reconnection failed after "
                             f"{self.settings.reconnect_attempts} attempts"),
                )
                return

    # -- connecting --------------------------------------------------------

    def _open_socket(self) -> str | None:
        """Open the socket and drain stale data. Returns an error string or None."""
        self._teardown_socket()
        self._assembler.reset()
        try:
            sock = socket.create_connection(
                (self.ip_address, self.settings.tcp_port),
                timeout=self.settings.connection_timeout_s,
            )
        except OSError as exc:
            return _describe(exc)

        try:
            # Small payloads sent promptly matters more than coalescing here.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.setblocking(False)
        self._socket = sock

        self._drain_stale()
        return None

    def _drain_stale(self) -> None:
        """Discard the NPort's buffered backlog. See `_DRAIN_WINDOW_S`."""
        assert self._socket is not None
        deadline = time.monotonic() + _DRAIN_WINDOW_S
        discarded = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                ready, _, _ = select.select([self._socket], [], [], max(0.0, remaining))
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                chunk = self._socket.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            discarded += len(chunk)
        self._assembler.reset()
        if discarded:
            logger.debug("%s discarded %d stale byte(s) on connect",
                         self._tag(), discarded)

    def _on_connected(self, first: bool) -> None:
        self._set_state(ConnectionState.CONNECTED)
        self._last_temp_emit = 0.0
        self._last_alert = None
        logger.info("%s connected", self._tag())
        self._emit(EventType.CONNECTED)

    def _reconnect(self) -> bool:
        """Try to restore the link. True if reconnected.

        First attempt is immediate -- most drops are transient and this
        recovers in milliseconds instead of waiting out the interval. Later
        attempts are spaced by `reconnect_interval_s`.
        """
        total = max(1, self.settings.reconnect_attempts)
        for attempt in range(1, total + 1):
            if self._shutdown.is_set() or self._manual_disconnect.is_set():
                return False

            self._set_state(ConnectionState.RECONNECTING)
            self._emit(EventType.RECONNECTING, attempt=attempt, total_attempts=total)
            logger.info("%s reconnect attempt %d/%d", self._tag(), attempt, total)

            error = self._open_socket()
            if error is None:
                logger.info("%s reconnected on attempt %d", self._tag(), attempt)
                self._on_connected(first=False)
                return True

            logger.warning("%s reconnect attempt %d/%d failed: %s",
                           self._tag(), attempt, total, error)

            if attempt < total:
                # Interruptible: shutdown does not wait this out.
                if self._shutdown.wait(self.settings.reconnect_interval_s):
                    return False
                if self._manual_disconnect.is_set():
                    return False
        return False

    # -- receiving ---------------------------------------------------------

    def _receive_loop(self) -> str:
        """Read until the link drops or we are told to stop.

        Returns a short reason for the drop.
        """
        sock = self._socket
        if sock is None:
            return "no socket"

        while True:
            if self._shutdown.is_set() or self._manual_disconnect.is_set():
                return "stopped"

            try:
                readable, _, _ = select.select(
                    [sock, self._wake_r], [], [sock], _SELECT_TIMEOUT_S
                )
            except (OSError, ValueError) as exc:
                return _describe(exc)

            if self._wake_r in readable:
                self._drain_wake()
                if self._shutdown.is_set() or self._manual_disconnect.is_set():
                    return "stopped"

            # Flush queued commands whether or not the wake fired, so a command
            # enqueued during a read is not left sitting.
            error = self._flush_outgoing(sock)
            if error is not None:
                return error

            if sock in readable:
                try:
                    chunk = sock.recv(4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as exc:
                    return _describe(exc)
                if not chunk:
                    return "peer closed the connection"
                self._handle_chunk(chunk)

    def _flush_outgoing(self, sock: socket.socket) -> str | None:
        """Send every queued command. Returns an error string on failure."""
        while True:
            try:
                command = self._outgoing.get_nowait()
            except queue.Empty:
                return None
            try:
                sock.sendall(encode_command(command))
            except OSError as exc:
                # Put it back so a successful reconnect can still deliver it?
                # No: a command issued against a dead link is stale by the time
                # the link returns, and re-firing a sweep unasked would be
                # worse than dropping it.
                logger.warning("%s send of %r failed: %s",
                               self._tag(), command, _describe(exc))
                return _describe(exc)
            logger.info("%s command issued: %s", self._tag(), command)

    def _handle_chunk(self, chunk: bytes) -> None:
        for line in self._assembler.feed(chunk):
            message = parse_line(line)

            if message.kind is MessageKind.PZT_TEMPERATURE:
                self._handle_temperature(message.value)

            elif message.kind is MessageKind.CLEAN_COMPLETE:
                logger.info("%s evaporation sweep completed%s", self._tag(),
                            f" (board {message.channel})" if message.channel else "")
                self._emit(EventType.CLEAN_COMPLETE, channel=message.channel)

            elif message.kind is MessageKind.AUTO_ON_ACK:
                logger.info("%s firmware acknowledged AutoClean enabled", self._tag())
                self._emit(EventType.AUTO_ACK, auto_enabled=True,
                           channel=message.channel)

            elif message.kind is MessageKind.AUTO_OFF_ACK:
                logger.info("%s firmware acknowledged AutoClean disabled", self._tag())
                self._emit(EventType.AUTO_ACK, auto_enabled=False,
                           channel=message.channel)

            elif message.kind is MessageKind.LOCKOUT_ENTERED:
                # The AdapterController has just sent `stop` to both boards, so
                # any Clean in progress is already dead.
                logger.warning("%s thermal lockout: hardware stopped both boards",
                               self._tag())
                self._emit(EventType.LOCKOUT,
                           message="Thermal lockout — hardware stopped",
                           auto_enabled=False)

            elif message.kind is MessageKind.LOCKOUT_CLEARED:
                logger.info("%s thermal lockout cleared", self._tag())
                self._emit(EventType.LOCKOUT_CLEARED)

            # COMMAND_ECHO and OTHER are deliberately dropped. The stream
            # carries a lot of telemetry EMCC has no use for, and logging it
            # would bury the events that matter.

    def _handle_temperature(self, value: float | None) -> None:
        """Throttle temperature to the configured cadence.

        The wire carries 2 Hz; the UI wants at most 1 Hz. A crossing of the
        warning threshold is emitted immediately regardless, so the display
        never lags a genuine over-temperature by up to a second.
        """
        if value is None:
            return
        alert = value > self.settings.temperature_warning_c
        now = time.monotonic()
        due = now - self._last_temp_emit >= self.settings.temperature_update_interval_s
        crossed = alert != self._last_alert

        if crossed:
            if alert:
                # Also fires on the first sample of an already-hot device: an
                # over-temperature present at connect is exactly as worth
                # knowing about as one that develops later.
                logger.warning("%s temperature exceeded limit: %.1f C",
                               self._tag(), value)
            elif self._last_alert is not None:
                # Not on the first sample -- "returned to normal" is meaningless
                # before there was anything to return from.
                logger.info("%s temperature returned to normal: %.1f C",
                            self._tag(), value)

        if not (due or crossed):
            return
        self._last_temp_emit = now
        self._last_alert = alert
        self._emit(EventType.TEMPERATURE, value=value)

    # -- teardown ----------------------------------------------------------

    def _teardown_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _close_wake_pipe(self) -> None:
        for sock in (self._wake_r, self._wake_w):
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Error text
# ---------------------------------------------------------------------------


def _describe(exc: BaseException | str) -> str:
    """Technical description, for logs."""
    if isinstance(exc, str):
        return exc
    if isinstance(exc, socket.timeout):
        return "timed out"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _friendly(detail: str) -> str:
    """Short operator-facing text. Full detail stays in the log."""
    lowered = detail.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "Connection failed: timed out"
    if "refused" in lowered:
        # Verified against a real NPort: it *actively refuses* a second client
        # rather than accepting and multiplexing, because TCP Server mode
        # defaults to Max Connection = 1. So the overwhelmingly likely cause is
        # another program (usually the PyGUI diagnostic tool) already holding
        # the session -- worth saying, rather than a generic "refused".
        return ("Connection refused — another program may already be "
                "connected to this device")
    if "unreachable" in lowered or "no route" in lowered:
        return "Host unreachable"
    if "reset" in lowered:
        return "Connection reset by the device"
    if "peer closed" in lowered:
        return "The device closed the connection"
    if "getaddrinfo" in lowered or "name or service" in lowered:
        return "Address could not be resolved"
    return detail
