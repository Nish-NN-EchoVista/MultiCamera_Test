"""Probe real EVS2 systems and report what the protocol layer makes of them.

Development tool. Nothing in `emcc/` imports it.

Two phases:

1. **Observe** -- connect, drain the stale backlog, then listen and classify
   every line for a while. Read-only; sends nothing.
2. **Exercise** (only with `--commands`) -- issue Clean and Auto and report
   what comes back. This drives real hardware: evaporation sweeps and the
   pump. Off by default for that reason.

Usage:

    python tools/live_probe.py --observe 20
    python tools/live_probe.py --observe 10 --commands
    python tools/live_probe.py --hosts 192.168.2.250 --observe 30
"""

from __future__ import annotations

import argparse
import collections
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emcc.backend.config_manager import Settings                    # noqa: E402
from emcc.backend.connection_worker import ConnectionWorker         # noqa: E402
from emcc.backend.events import DeviceEvent, EventType              # noqa: E402
from emcc.backend.logging_setup import setup_logging                # noqa: E402
from emcc.backend.protocol import (                                 # noqa: E402
    CMD_AUTO_OFF, CMD_AUTO_ON, CMD_CLEAN, LineAssembler, parse_line,
)

DEFAULT_HOSTS = ["192.168.2.250", "192.168.2.251", "192.168.2.253"]


def raw_capture(host: str, port: int, seconds: float) -> list[str]:
    """Read raw lines with a plain socket, bypassing the worker.

    Deliberately independent of `ConnectionWorker`, so it shows what is
    actually on the wire rather than what EMCC decided to keep.
    """
    import socket

    lines: list[str] = []
    assembler = LineAssembler()
    try:
        sock = socket.create_connection((host, port), timeout=3.0)
    except OSError as exc:
        print(f"  ! raw connect failed: {type(exc).__name__}: {exc}")
        return lines

    with sock:
        sock.setblocking(False)
        import select
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sock], [], [], 0.3)
            if not ready:
                continue
            try:
                chunk = sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            lines.extend(assembler.feed(chunk))
    return lines


def observe(host: str, port: int, seconds: float, send_commands: bool) -> dict:
    """Run a real ConnectionWorker against `host` and summarise the result."""
    print(f"\n{'=' * 68}\n{host}:{port}\n{'=' * 68}")
    report: dict = {"host": host, "connected": False}

    settings = Settings(
        tcp_port=port,
        connection_timeout_s=3.0,
        temperature_update_interval_s=1.0,
        reconnect_attempts=1,
        reconnect_interval_s=1.0,
    )
    events: "queue.Queue[DeviceEvent]" = queue.Queue()
    worker = ConnectionWorker("probe", f"probe@{host}", host, settings, events)
    worker.start()

    counts: collections.Counter = collections.Counter()
    temperatures: list[float] = []
    deadline = time.monotonic() + seconds
    clean_sent_at: float | None = None
    clean_completed_at: float | None = None
    commands_done = False

    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.25)
        except queue.Empty:
            event = None

        if event is not None:
            counts[event.type.name] += 1
            if event.type is EventType.CONNECTED:
                report["connected"] = True
                print("  connected")
            elif event.type is EventType.TEMPERATURE and event.value is not None:
                temperatures.append(event.value)
                if len(temperatures) <= 3:
                    print(f"  temperature: {event.value:.1f} C")
            elif event.type is EventType.CLEAN_COMPLETE:
                if clean_completed_at is None and clean_sent_at is not None:
                    clean_completed_at = time.monotonic()
                    print(f"  clean completed after "
                          f"{clean_completed_at - clean_sent_at:.1f}s "
                          f"(board {event.channel})")
            elif event.type is EventType.AUTO_ACK:
                print(f"  auto ack: enabled={event.auto_enabled} "
                      f"board={event.channel}")
            elif event.type in (EventType.CONNECTION_FAILED,
                                EventType.CONNECTION_LOST,
                                EventType.RECONNECT_FAILED):
                print(f"  {event.type.name}: {event.message}")

        # Halfway through, optionally exercise the command paths.
        if (send_commands and not commands_done and report["connected"]
                and time.monotonic() > deadline - seconds / 2):
            commands_done = True
            print(f"  -> sending {CMD_AUTO_ON}")
            worker.send(CMD_AUTO_ON)
            time.sleep(1.0)
            print(f"  -> sending {CMD_AUTO_OFF}")
            worker.send(CMD_AUTO_OFF)
            time.sleep(1.0)
            print(f"  -> sending {CMD_CLEAN}")
            worker.send(CMD_CLEAN)
            clean_sent_at = time.monotonic()

    worker.stop()
    worker.join(timeout=3.0)

    report["events"] = dict(counts)
    report["temperatures"] = temperatures
    if temperatures:
        report["temp_min"] = min(temperatures)
        report["temp_max"] = max(temperatures)
        rate = len(temperatures) / seconds
        print(f"  {len(temperatures)} temperature update(s) over {seconds:.0f}s "
              f"({rate:.2f}/s after throttling), "
              f"range {min(temperatures):.1f}-{max(temperatures):.1f} C")
    elif report["connected"]:
        print("  ! connected but received NO temperature")
    report["thread_alive_after_stop"] = worker.is_alive()
    if worker.is_alive():
        print("  ! worker thread did not stop")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe live EVS2 systems.")
    parser.add_argument("--hosts", nargs="*", default=DEFAULT_HOSTS)
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--observe", type=float, default=15.0,
                        help="seconds to listen per host")
    parser.add_argument("--commands", action="store_true",
                        help="also send Clean and Auto (drives real hardware)")
    parser.add_argument("--raw", type=float, default=0.0,
                        help="also dump N seconds of raw classified lines")
    args = parser.parse_args()

    setup_logging(console=False)

    reports = []
    for host in args.hosts:
        if args.raw:
            print(f"\n--- raw wire sample from {host} ---")
            lines = raw_capture(host, args.port, args.raw)
            kinds: collections.Counter = collections.Counter()
            for line in lines:
                kinds[parse_line(line).kind.name] += 1
            for line in lines[:25]:
                message = parse_line(line)
                print(f"  [{message.kind.name:16}] {line[:96]}")
            if len(lines) > 25:
                print(f"  ... {len(lines) - 25} more")
            print(f"  classified: {dict(kinds)}")
        reports.append(observe(host, args.port, args.observe, args.commands))

    print(f"\n{'=' * 68}\nSUMMARY\n{'=' * 68}")
    for report in reports:
        status = "OK  " if report["connected"] else "FAIL"
        temps = len(report.get("temperatures", []))
        extra = ""
        if temps:
            extra = f"  temps={temps} range={report['temp_min']:.1f}-{report['temp_max']:.1f}C"
        print(f"  {status} {report['host']:16}{extra}")
        if report.get("thread_alive_after_stop"):
            print("       ! thread leak")
    return 0 if all(r["connected"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
