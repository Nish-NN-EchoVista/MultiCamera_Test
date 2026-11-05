"""Capture for many devices at once: one writer thread, one finisher thread.

PyGUI's logger holds one device at a time -- a row for a different device
closes the open file and opens another -- which is right for a single-device
tool and wrong for EMCC, where 25 devices interleave row by row. So the file
machinery is PyGUI's (see `files.py`) and the threading is new.

Threads, and what each may touch
--------------------------------
* **Connection workers** call `record_rx` / `record_tx` / `record_event`. Each
  is one dict lookup and, if that device is being logged, one `deque.append`.
  No parsing and no disk, so a slow disk can never stall a socket. A device
  with logging off costs the lookup and nothing else.
* **The Tk thread** calls `enable` / `disable` / `rename` / `set_level` /
  `close`. These write `_enabled` and append control items to the *same*
  queue as the rows, so "Logging stopped" always lands after every row that
  was queued before the operator pressed the button.
* **The writer thread** owns everything else: one open hour per device, the
  level filter and the hold buffer. It wakes every second, drains the queue,
  hands finished hours to the finisher, and every five seconds forces every
  open journal to disk in one pass.
* **The finisher thread** saves finished hours (the zip, ~1 s of CPU per
  device-hour at Full logs), rebuilds hours left by a crash, and prunes old
  logs. Saving one device at a time is what spreads the hour-mark cost out
  instead of landing it on the writer all at once.

Both threads exist only while there is work: with logging off for every
device, EMCC runs no capture thread at all.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .files import (
    HAVE_XLSX,
    MAX_SHEET_ROWS,
    Segment,
    clean,
    device_folders,
    folder_name,
    period,
    prune,
    recover_device_folder,
)
from .levels import (
    CATEGORIES,
    DEFAULT_LEVEL,
    EVENT,
    INFO,
    RX,
    TX,
    Classified,
    Entry,
    LevelFilter,
    LogLevel,
    classify,
)

logger = logging.getLogger("emcc.capture")

# Queue item kinds. Items are tuples: (kind, device_id, folder, payload).
_ROW = "row"
_ENABLE = "enable"
_DISABLE = "disable"
_LEVEL = "level"
_SYNC = "sync"

GB = 1024 ** 3


class DeviceCapture:
    """Hourly per-device log files, at the level chosen in Settings.

    Lifecycle::

        capture = DeviceCapture(root, level="reduced")
        capture.start()                          # recover crashed hours, prune
        capture.enable(device_id, "Camera 1")    # the device's Log button
        capture.record_rx(device_id, line)       # from its connection worker
        capture.set_level("important")           # from Settings
        capture.disable(device_id)
        capture.close(timeout=5.0)               # app exit
    """

    FLUSH_INTERVAL_S = 1.0
    FSYNC_INTERVAL_S = 5.0
    #: A row stamped just before an hour mark can reach the writer just after.
    CLOSE_GRACE_S = 2.0
    #: Rows kept while the disk refuses writes, before the oldest are dropped.
    HOLD_LIMIT = 200_000
    PRUNE_INTERVAL_S = 3600.0

    def __init__(
        self,
        root: str | Path,
        *,
        level: LogLevel | str = DEFAULT_LEVEL,
        retention_days: int = 30,
        max_bytes: int = 10 * GB,
        segment_minutes: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.segment_seconds = max(1, int(segment_minutes * 60))
        self._clock = clock
        self._level = LogLevel.parse(level)

        #: device id -> folder name, for devices being logged. Written on the
        #: Tk thread, read by the connection workers.
        self._enabled: dict[str, str] = {}
        self._queue: deque = deque()
        self._wake = threading.Event()
        self._closing = False
        self._threads_lock = threading.Lock()
        self._writer: threading.Thread | None = None
        self._finisher: threading.Thread | None = None
        self._jobs: deque = deque()

        # -- writer thread only ------------------------------------------
        self._writer_level = self._level
        self._segments: dict[str, Segment] = {}
        self._name_by_start: dict[str, bool] = {}
        self._held: deque = deque()
        self._filter = LevelFilter()
        self._last_fsync = 0.0
        self._last_prune = 0.0
        #: The current disk error, or None. Reported once per run of errors.
        self.write_error: str | None = None
        self.dropped = 0

    # ------------------------------------------------------------------
    # Tk thread
    # ------------------------------------------------------------------

    @property
    def level(self) -> LogLevel:
        return self._level

    def is_enabled(self, device_id: str) -> bool:
        return device_id in self._enabled

    def start(self) -> None:
        """Once at app start: rebuild hours left by a crash, prune old logs."""
        if not HAVE_XLSX:
            logger.warning("capture: openpyxl is not installed; device logs "
                           "are saved as CSV instead of Excel workbooks")
        self._submit(self._recover)
        self._submit(self._prune)

    def enable(self, device_id: str, name: str) -> None:
        """Start logging `device_id` into `<root>/<name>/`."""
        if self._closing or device_id in self._enabled:
            return
        folder = folder_name(name)
        self._enabled[device_id] = folder
        self._queue.append((_ENABLE, device_id, folder, self._clock()))
        self._ensure_writer()

    def disable(self, device_id: str) -> None:
        """Stop logging `device_id`; its open hour is saved in the background."""
        folder = self._enabled.pop(device_id, None)
        if folder is None:
            return
        self._queue.append((_DISABLE, device_id, folder, self._clock()))
        self._ensure_writer()

    def rename(self, device_id: str, name: str) -> None:
        """Rows queued from now on go to `<root>/<name>/`, in a new file."""
        if device_id in self._enabled:
            self._enabled[device_id] = folder_name(name)

    def set_level(self, level: LogLevel | str) -> None:
        """Apply `level` from the next row on, for every device."""
        level = LogLevel.parse(level)
        if level is self._level:
            return
        self._level = level
        self._queue.append((_LEVEL, None, None, level))
        self._ensure_writer()

    def sync(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued so far has been written. For tests."""
        done = threading.Event()
        self._queue.append((_SYNC, None, None, done))
        self._ensure_writer()
        return done.wait(timeout)

    def wait_saved(self, timeout: float = 10.0) -> bool:
        """Wait until the finisher has nothing left to do."""
        deadline = time.monotonic() + timeout
        while True:
            finisher = self._finisher
            if finisher is None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            finisher.join(remaining)

    def close(self, timeout: float = 5.0) -> bool:
        """App exit: stop every device and save what can be saved in time.

        Anything not saved within `timeout` is not lost: its journal is on
        disk and its lock dies with the process, so the next start rebuilds
        it. Waiting longer would hold the window open for the zip of every
        device's open hour.
        """
        self._closing = True
        for device_id in list(self._enabled):
            self.disable(device_id)
        deadline = time.monotonic() + timeout
        writer = self._writer
        if writer is not None:
            self._wake.set()
            writer.join(max(0.0, deadline - time.monotonic()))
        writer_done = writer is None or not writer.is_alive()
        saved = self.wait_saved(max(0.0, deadline - time.monotonic()))
        if not (writer_done and saved):
            logger.warning("capture: still saving at exit; unsaved hours are "
                           "kept as journals and rebuilt on the next start")
        return writer_done and saved

    # ------------------------------------------------------------------
    # Any thread (connection workers)
    # ------------------------------------------------------------------

    def record_rx(self, device_id: str, line: str) -> None:
        """A line the device sent."""
        folder = self._enabled.get(device_id)
        if folder is not None:
            self._queue.append(
                (_ROW, device_id, folder, Entry(self._clock(), RX, line)))

    def record_tx(self, device_id: str, command: str) -> None:
        """A command EMCC sent the device."""
        folder = self._enabled.get(device_id)
        if folder is not None:
            self._queue.append(
                (_ROW, device_id, folder, Entry(self._clock(), TX, command)))

    def record_event(self, device_id: str, text: str, severity: str = INFO,
                     *, context: bool = False) -> None:
        """Something EMCC observed about the device (connected, lost, ...)."""
        folder = self._enabled.get(device_id)
        if folder is not None:
            self._queue.append(
                (_ROW, device_id, folder,
                 Entry(self._clock(), EVENT, text, severity, context)))

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    def _ensure_writer(self) -> None:
        with self._threads_lock:
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._run_writer, name="emcc-capture-writer",
                    daemon=True)
                self._writer.start()
        self._wake.set()

    def _submit(self, job: Callable[[], None]) -> None:
        self._jobs.append(job)
        with self._threads_lock:
            if self._finisher is None:
                self._finisher = threading.Thread(
                    target=self._run_finisher, name="emcc-capture-finisher",
                    daemon=True)
                self._finisher.start()

    def _idle(self) -> bool:
        return not (self._enabled or self._segments or self._queue
                    or self._held)

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _run_writer(self) -> None:
        while True:
            self._wake.wait(self.FLUSH_INTERVAL_S)
            self._wake.clear()
            try:
                self._tick()
            except Exception:
                logger.exception("capture: writer error; open hours are kept "
                                 "as journals and will be rebuilt")
                self._abandon_all()
            # Checked under the lock `_ensure_writer` takes, so a device
            # enabled at this instant either sees this thread still running
            # or starts a new one -- never neither.
            with self._threads_lock:
                if self._idle():
                    self._writer = None
                    return

    def _tick(self) -> None:
        self._drain()
        now = self._clock()
        for device_id, segment in [(d, s) for d, s in self._segments.items()
                                   if now >= s.end + self.CLOSE_GRACE_S]:
            # The hour passed with no new row to trigger the hand-over.
            # A repeat count stamped after the mark has just opened the next
            # hour's file, so close only the hour that expired -- closing
            # whatever is open now would end the new file at one row.
            self._emit(device_id, segment.device,
                       self._filter.pending(device_id))
            if self._segments.get(device_id) is segment:
                self._close(device_id, rollover=True)
        fsync = now - self._last_fsync >= self.FSYNC_INTERVAL_S
        for device_id, segment in list(self._segments.items()):
            try:
                segment.flush(fsync)
            except OSError:
                logger.warning("capture: cannot write %s; it is kept and will "
                               "be rebuilt", segment.journal_path, exc_info=True)
                self._segments.pop(device_id).abandon()
                self._submit(self._recover)
        if fsync:
            self._last_fsync = now

    def _drain(self) -> None:
        if self._held and not self._retry_held():
            return
        while self._queue:
            self._handle(self._queue.popleft())
            if self._held:
                return  # the disk refused a write; try again next tick

    def _handle(self, item: tuple) -> None:
        kind, device_id, folder, payload = item
        if kind == _ROW:
            entry: Entry = payload
            rows = self._filter.apply(self._writer_level, device_id, entry,
                                      classify(entry))
            self._emit(device_id, folder, rows)
        elif kind == _ENABLE:
            self._emit(device_id, folder, [self._context(
                payload, f"Logging started ({self._writer_level.label})")])
        elif kind == _DISABLE:
            self._emit(device_id, folder, self._filter.pending(device_id))
            self._emit(device_id, folder,
                       [self._context(payload, "Logging stopped")])
            self._close(device_id, rollover=False)
            self._filter.forget(device_id)
            self._name_by_start.pop(device_id, None)
        elif kind == _LEVEL:
            # Counts folded under the old level are written before it changes.
            for pending_device in self._filter.pending_devices():
                target = self._folder_of(pending_device)
                if target is not None:
                    self._emit(pending_device, target,
                               self._filter.pending(pending_device))
            self._writer_level = payload
            for active in list(self._segments):
                self._emit(active, self._segments[active].device,
                           [self._context(self._clock(),
                                          f"Log level changed to {payload.label}")])
        elif kind == _SYNC:
            payload.set()

    def _folder_of(self, device_id: str) -> str | None:
        segment = self._segments.get(device_id)
        return segment.device if segment is not None else self._enabled.get(device_id)

    @staticmethod
    def _context(ts: float, text: str) -> tuple[Entry, Classified]:
        entry = Entry(ts, EVENT, text, INFO, context=True)
        return entry, classify(entry)

    def _emit(self, device_id: str, folder: str,
              rows: list[tuple[Entry, Classified]]) -> None:
        """Write `rows`, or hold them, in order, if the disk refuses."""
        for entry, info in rows:
            if self._held:
                self._hold((device_id, folder, entry, info))
                continue
            try:
                self._write(device_id, folder, entry, info)
            except OSError as exc:
                self._note_write_error(exc)
                self._hold((device_id, folder, entry, info))

    def _retry_held(self) -> bool:
        while self._held:
            try:
                self._write(*self._held[0])
            except OSError as exc:
                self._note_write_error(exc)
                return False
            self._held.popleft()
        logger.info("capture: log writing resumed; held rows were written")
        self.write_error = None
        return True

    def _hold(self, row: tuple) -> None:
        self._held.append(row)
        while len(self._held) > self.HOLD_LIMIT:
            self._held.popleft()
            self.dropped += 1

    def _note_write_error(self, exc: OSError) -> None:
        if self.write_error is None:
            logger.warning("capture: cannot write device logs (%s); holding "
                           "rows and retrying every second", exc,
                           exc_info=True)
        self.write_error = str(exc)

    def _write(self, device_id: str, folder: str, entry: Entry,
               info: Classified) -> None:
        segment = self._segments.get(device_id)
        if segment is not None:
            if segment.device != folder:
                self._close(device_id, rollover=False)   # renamed
                segment = None
            elif entry.ts >= segment.end:
                self._close(device_id, rollover=True)    # the next hour
                segment = None
            elif segment.rows >= MAX_SHEET_ROWS:
                self._close(device_id, rollover=False)   # Excel's row limit
                segment = None
        if segment is None:
            segment = self._open(device_id, folder, entry.ts)
        local = time.localtime(entry.ts)
        segment.write([
            time.strftime("%Y-%m-%d", local),
            time.strftime("%H:%M:%S", local),
            clean(info.message),
            info.source,
            info.category,
            info.value,
            info.units,
        ])

    def _open(self, device_id: str, folder: str, ts: float) -> Segment:
        start, end = period(ts, self.segment_seconds)
        # The first file of a run is named by when logging started (13:56);
        # the files after an hour mark by the mark itself (14:00), as PyGUI.
        named = ts if self._name_by_start.get(device_id, True) else start
        local = time.localtime(named)
        segment = Segment(self.root, folder, time.strftime("%Y-%m-%d", local),
                          time.strftime("%Y-%m-%d_%H%M", local), start, end,
                          CATEGORIES)
        self._segments[device_id] = segment
        return segment

    def _close(self, device_id: str, *, rollover: bool) -> None:
        segment = self._segments.pop(device_id, None)
        if segment is None:
            return
        self._name_by_start[device_id] = not rollover
        try:
            segment.close_journal()
        except OSError:
            logger.warning("capture: could not close %s; it will be rebuilt",
                           segment.journal_path, exc_info=True)
            segment.abandon()
            self._submit(self._recover)
            return
        self._submit(lambda: self._save(segment))
        if rollover and self._clock() - self._last_prune >= self.PRUNE_INTERVAL_S:
            self._last_prune = self._clock()
            self._submit(self._prune)

    def _abandon_all(self) -> None:
        for segment in self._segments.values():
            segment.abandon()
        self._segments.clear()
        self._submit(self._recover)

    # ------------------------------------------------------------------
    # Finisher thread
    # ------------------------------------------------------------------

    def _run_finisher(self) -> None:
        while True:
            with self._threads_lock:
                if not self._jobs:
                    self._finisher = None
                    return
                job = self._jobs.popleft()
            try:
                job()
            except Exception:
                logger.exception("capture: background save failed")

    def _save(self, segment: Segment) -> None:
        try:
            damaged = segment.save()
        except Exception:
            logger.warning("capture: could not save %s; its journal is kept "
                           "and will be rebuilt", segment.final_path,
                           exc_info=True)
            segment.abandon()
            self._submit(self._recover)
            return
        logger.debug("capture: saved %s (%d rows)", segment.final_path,
                     segment.rows)
        if damaged:
            logger.warning("capture: %d unreadable line(s) skipped in %s; "
                           "the journal is kept", damaged, segment.final_path)

    def _recover(self) -> None:
        for folder in device_folders(self.root):
            for report in recover_device_folder(folder, CATEGORIES):
                logger.info("capture: %s", report)

    def _prune(self) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        pruned = prune(self.root, keep_days=self.retention_days,
                       max_bytes=self.max_bytes, today=today)
        if pruned:
            logger.info("capture: removed device logs from %d day folder(s) "
                        "(older than %d days or over %.1f GB)", len(pruned),
                        self.retention_days, self.max_bytes / GB)
