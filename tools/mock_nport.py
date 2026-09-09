"""Mock Moxa NPort + AdapterController + 2x EVS2, for development and tests.

Development/test only. Nothing in `emcc/` imports this.

Reproduces the behaviours that actually matter for EMCC, including the awkward
ones:

* persistent raw TCP, one client at a time (NPort's TCP Server mode defaults to
  Max Connection = 1)
* `PZT Temp: %d.%d C` every 500 ms, untagged
* EVS2 output tagged `[ESV2-1] ` / `[ESV2-2] `
* `@1 <cmd>` / `@2 <cmd>` routing, bare commands broadcast to both
* command echo `TX to ESV2-N: <cmd>` for routed commands
* a **stale backlog** delivered on connect -- a real NPort buffers whatever the
  UART emitted while nobody was attached, which is why EMCC must drain on
  connect (see `ConnectionWorker._drain_stale`)
* scriptable faults: refuse, drop mid-session, go silent

Run standalone:

    python tools/mock_nport.py --port 4001
    python tools/mock_nport.py --port 4001 --drop-after 20
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time
from dataclasses import dataclass, field

CLEAN_COMPLETE = "Evaporation Sweep Completed"
TEMP_INTERVAL_S = 0.5


@dataclass
class MockConfig:
    """Knobs for a mock instance."""

    port: int = 0                     # 0 -> ephemeral, read back via .port
    temp_start: float = 42.0
    temp_drift: float = 0.3
    temp_interval_s: float = TEMP_INTERVAL_S
    #: Seconds after a Clean command before completion is emitted. The real
    #: sweep is much longer than EMCC's 10 s UI timeout; the default here is
    #: short so tests can see the completion path.
    clean_duration_s: float = 1.0
    #: Emit completion for these channels, in order.
    clean_channels: tuple[int, ...] = (1, 2)
    #: Lines pushed the instant a client connects, simulating NPort's buffer.
    stale_backlog: list[str] = field(default_factory=list)
    #: Drop the connection this many seconds after it is established.
    drop_after_s: float | None = None
    #: Accept the TCP connection then send nothing at all.
    silent: bool = False
    #: Refuse connections outright (listener closed).
    refuse: bool = False
    #: Emit the Auto ACK only for channel 1, as the real firmware does.
    auto_ack_channels: tuple[int, ...] = (1,)


class MockNPort:
    """A single-client mock of one EVS2 system.

    Usable as a context manager::

        with MockNPort(MockConfig()) as mock:
            sock.connect(("127.0.0.1", mock.port))
    """

    def __init__(self, config: MockConfig | None = None):
        self.config = config or MockConfig()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._client: socket.socket | None = None
        self.port: int = 0

        # Observability for tests.
        self.received_commands: list[str] = []
        self.connection_count = 0
        self._temp = self.config.temp_start

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "MockNPort":
        if self.config.refuse:
            # Bind and immediately close, so the port is closed -> ECONNREFUSED.
            probe = socket.socket()
            probe.bind(("127.0.0.1", self.config.port))
            self.port = probe.getsockname()[1]
            probe.close()
            return self

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", self.config.port))
        self._listener.listen(1)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]

        self._thread = threading.Thread(
            target=self._accept_loop, name=f"mock-nport-{self.port}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.disconnect_client()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "MockNPort":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -- client control ----------------------------------------------------

    def disconnect_client(self) -> None:
        """Force the current client's connection closed."""
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    @property
    def has_client(self) -> bool:
        with self._client_lock:
            return self._client is not None

    def push(self, line: str) -> None:
        """Inject an arbitrary line to the connected client."""
        self._send(line)

    # -- internals ---------------------------------------------------------

    def _send(self, line: str) -> None:
        with self._client_lock:
            client = self._client
        if client is None:
            return
        try:
            client.sendall((line + "\r\n").encode())
        except OSError:
            self.disconnect_client()

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._listener is not None:
            try:
                conn, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # NPort default: one client at a time.
            if self.has_client:
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            with self._client_lock:
                self._client = conn
            self.connection_count += 1
            self._serve(conn)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(0.1)
        established = time.monotonic()

        # The stale backlog lands before anything else, exactly as a real
        # NPort flushes its buffer on attach.
        for line in self.config.stale_backlog:
            self._send(line)

        buffer = bytearray()
        next_temp = time.monotonic() + self.config.temp_interval_s
        pending_cleans: list[tuple[float, int]] = []

        while not self._stop.is_set():
            with self._client_lock:
                if self._client is not conn:
                    return

            now = time.monotonic()

            if (
                self.config.drop_after_s is not None
                and now - established >= self.config.drop_after_s
            ):
                self.disconnect_client()
                return

            if not self.config.silent and now >= next_temp:
                self._temp += random.uniform(-self.config.temp_drift,
                                             self.config.temp_drift)
                self._send(f"PZT Temp: {self._temp:.1f} C")
                next_temp = now + self.config.temp_interval_s

            for due, channel in list(pending_cleans):
                if now >= due:
                    pending_cleans.remove((due, channel))
                    self._send(f"[ESV2-{channel}] {CLEAN_COMPLETE}")

            try:
                chunk = conn.recv(512)
            except socket.timeout:
                continue
            except OSError:
                self.disconnect_client()
                return
            if not chunk:
                self.disconnect_client()
                return

            buffer.extend(chunk)
            while b"\n" in buffer:
                index = buffer.find(b"\n")
                line = bytes(buffer[:index]).decode(errors="replace").strip()
                del buffer[: index + 1]
                if line:
                    self._handle_command(line, pending_cleans)

    def _handle_command(
        self, line: str, pending_cleans: list[tuple[float, int]]
    ) -> None:
        self.received_commands.append(line)

        # Routing, mirroring AdapterController uart_handler.c:237-244.
        if line.startswith("@1 ") or line.startswith("@2 "):
            channel = int(line[1])
            command = line[3:]
            self._send(f"TX to ESV2-{channel}: {command}")
            targets = (channel,)
        elif line == "enableauto":
            # Firmware special case: reaches ESV2-1 only.
            targets = (1,)
            command = line
        else:
            targets = (1, 2)   # broadcast branch
            command = line

        if command == "start_evap_sweep":
            due = time.monotonic() + self.config.clean_duration_s
            for channel in self.config.clean_channels:
                if channel in targets:
                    pending_cleans.append((due, channel))
        elif command == "enableauto":
            for channel in targets:
                if channel in self.config.auto_ack_channels:
                    self._send(f"[ESV2-{channel}] AutoClean enabled")
        elif command == "dis_auto":
            for channel in targets:
                self._send(f"[ESV2-{channel}] AutoClean disabled")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock EVS2 system over TCP.")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--drop-after", type=float, default=None,
                        help="drop the connection N seconds after it opens")
    parser.add_argument("--silent", action="store_true",
                        help="accept the connection but send nothing")
    parser.add_argument("--stale", type=int, default=0,
                        help="lines of stale backlog to deliver on connect")
    parser.add_argument("--clean-duration", type=float, default=3.0)
    args = parser.parse_args()

    backlog = [f"PZT Temp: {40 + i * 0.1:.1f} C" for i in range(args.stale)]
    if args.stale:
        # A stale completion is the nasty case: without a drain it would
        # instantly complete the operator's next Clean.
        backlog.append(f"[ESV2-1] {CLEAN_COMPLETE}")

    config = MockConfig(
        port=args.port,
        drop_after_s=args.drop_after,
        silent=args.silent,
        stale_backlog=backlog,
        clean_duration_s=args.clean_duration,
    )
    mock = MockNPort(config).start()
    print(f"mock NPort listening on 127.0.0.1:{mock.port}  (ctrl-c to stop)")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        mock.stop()


if __name__ == "__main__":
    main()
