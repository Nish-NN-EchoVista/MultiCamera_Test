"""Hourly device log files that survive crashes, kills and power cuts.

PORTED FROM PyGUI -- EchoVista-Git/PyGUI, branch `dev-to-merge_14-10-26`,
`logic/excel_logger.py` at `98a1867` ("Log each device into its own folder
under Logs"). The on-disk layout, file names and columns are PyGUI's, so an
EMCC device log opens and reads exactly like a PyGUI one::

    <root>/<device>/<YYYY-MM-DD>/<YYYY-MM-DD_HHMM>_log.xlsx   finished hours
    <root>/<device>/<YYYY-MM-DD>/<YYYY-MM-DD_HHMM>_live.csv   the hour in progress
    <root>/<device>/.writing/<stem>/                          streaming sheets

While an hour is open each row goes first to a CSV journal -- flushed every
second, forced to disk every five, and the copy that survives a crash -- and
then to a write-only workbook streaming into `.writing`, so closing the hour
only has to zip it. A finished hour is written once, to a `.part` file that is
checked and renamed into place. Each open hour holds an OS lock that the OS
drops if the process dies; on start, any journal whose lock is free is rebuilt.

Changes from PyGUI, each for a reason:

* **Closing an hour is split in two.** `Segment.close_journal()` is quick and
  runs on the writer thread; `Segment.save()` -- the zip, ~1 s of CPU per
  device-hour at Full logs, measured on the bench PC -- runs on the finisher
  thread. 25 devices crossing the hour mark together would otherwise stall the
  writer for ~25 s.
* **The `.part` is forced to disk before the rename.** PyGUI renames without
  it. If the rename reaches the disk and the data does not, a crash leaves a
  full-size file of zero bytes -- the failure a journal exists to prevent.
* **openpyxl is optional.** EMCC did not depend on it before. Without it the
  journal itself becomes the finished file (`*_log.csv`), so a PC missing the
  package still logs.
* **Failures PyGUI swallowed are logged** -- at DEBUG for best-effort cleanup,
  where a failure changes nothing, and louder where it loses data.
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Iterator, Sequence

logger = logging.getLogger("emcc.capture")

#: Decided once, by looking rather than by catching an ImportError.
HAVE_XLSX = importlib.util.find_spec("openpyxl") is not None

if HAVE_XLSX:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet import _writer as openpyxl_sheet_writer
else:  # pragma: no cover - exercised only on a PC without openpyxl
    ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")

HEADER = ("Date", "Time", "Message", "Source", "Category", "Value", "Units")
COLUMN_WIDTHS = (12, 10, 60, 10, 22, 10, 9)
#: Excel allows 1,048,576 rows a sheet; a file that would exceed it continues
#: in a new one. At Full logs a device sends ~42,500 rows an hour.
MAX_SHEET_ROWS = 1_000_000

JOURNAL_SUFFIX = "_live.csv"
XLSX_SUFFIX = "_log.xlsx"
CSV_SUFFIX = "_log.csv"
DAMAGED_SUFFIX = ".damaged"
WRITING = ".writing"


def final_suffix() -> str:
    return XLSX_SUFFIX if HAVE_XLSX else CSV_SUFFIX


# ---------------------------------------------------------------------------
# One open hour
# ---------------------------------------------------------------------------


class Segment:
    """One device-hour while it is open: its journal, lock and workbook."""

    def __init__(self, root: Path, device: str, day: str, stem: str,
                 start: float, end: float, categories: Sequence[str]) -> None:
        self.device = device
        self.start, self.end = start, end
        self.rows = 0
        self._categories = tuple(categories)
        device_dir = root / device
        self.day_dir = device_dir / day
        self.day_dir.mkdir(parents=True, exist_ok=True)
        base = unique_stem(self.day_dir, stem)
        self.journal_path = self.day_dir / f"{base}{JOURNAL_SUFFIX}"
        self.final_path = self.day_dir / f"{base}{final_suffix()}"
        self.temp_dir = device_dir / WRITING / base
        self.lock = FileLock(self.day_dir / f"{base}.lock")
        if not self.lock.acquire():
            raise OSError(f"{self.lock.path} is locked")
        opened = False
        try:
            self._journal = open(self.journal_path, "w", newline="",
                                 encoding="utf-8-sig")
            self._csv = csv.writer(self._journal)
            self._csv.writerow(HEADER)
            self._journal.flush()
            opened = True
        finally:
            if not opened:
                self.lock.release()
        self._workbook = None
        self._sheets: dict = {}
        if HAVE_XLSX:
            try:
                self._workbook, self._sheets = streaming_workbook(
                    self.temp_dir, self._categories)
                hide(device_dir / WRITING)
            except Exception as exc:
                # The journal alone is enough: save() rebuilds from it.
                logger.debug("capture: no streaming workbook for %s (%s); "
                             "the hour will be rebuilt from its journal",
                             self.journal_path, exc)
                self._discard_workbook()

    def write(self, row: list) -> None:
        self._csv.writerow(row)  # first: the journal is what survives a crash
        self.rows += 1
        if self._workbook is None:
            return
        try:
            self._sheets["All"].append(row)
            sheet = self._sheets.get(row[4])
            if sheet is not None and row[4] != "All":
                sheet.append(row)
        except Exception as exc:
            # After one rejected row a write-only sheet fails on every later
            # row, so stop feeding it; save() rebuilds from the journal.
            logger.debug("capture: streaming workbook for %s failed (%s); "
                         "the hour will be rebuilt from its journal",
                         self.journal_path, exc)
            self._discard_workbook()

    def flush(self, fsync: bool) -> None:
        self._journal.flush()
        if fsync:
            os.fsync(self._journal.fileno())

    def close_journal(self) -> None:
        """Writer thread: the hour is over. Quick -- the save happens later."""
        self.flush(fsync=True)
        self._journal.close()

    def save(self) -> int:
        """Finisher thread: write the finished file and tidy up.

        Returns the number of unreadable journal lines skipped (see
        `JournalRows`). Raises on failure, leaving the journal and the lock
        for `abandon()` and recovery.
        """
        if not HAVE_XLSX:
            replace(self.journal_path, self.final_path)
            self.lock.release()
            return 0

        part = self.final_path.with_name(self.final_path.name + ".part")
        damaged = 0
        done = False
        try:
            saved = False
            if self._workbook is not None:
                try:
                    self._workbook.save(part)
                    saved = True
                except Exception as exc:
                    logger.debug("capture: streaming save of %s failed (%s); "
                                 "rebuilding from the journal", part, exc)
                    self._discard_workbook()
                    remove_file(part)
            if not saved:
                damaged = build_from_journal(
                    self.journal_path, part,
                    self.temp_dir.with_name(self.temp_dir.name + "-rebuild"),
                    self._categories)[1]
            fsync_file(part)
            verify_xlsx(part)
            replace(part, self.final_path)
            done = True
        finally:
            if not done:
                remove_file(part)
        if damaged:
            keep_damaged_journal(self.journal_path)
        else:
            remove_file(self.journal_path)
        self.lock.release()
        remove_tree(self.temp_dir)
        return damaged

    def abandon(self) -> None:
        """Let go after a failure. The journal stays for recovery to rebuild."""
        with contextlib.suppress(OSError, ValueError):
            self._journal.close()
        self._discard_workbook()
        self.lock.release(delete=False)

    def _discard_workbook(self) -> None:
        close_sheets(self._sheets)
        self._workbook = None
        self._sheets = {}


# ---------------------------------------------------------------------------
# Crash recovery and clean-up
# ---------------------------------------------------------------------------


def recover_device_folder(folder: Path, categories: Sequence[str]) -> list[str]:
    """Rebuild every hour in `folder` whose journal no running writer holds.

    Returns one line per file recovered or failed, for the application log.
    """
    reports: list[str] = []
    for journal in sorted(folder.glob(f"*/*{JOURNAL_SUFFIX}")):
        stem = journal.name[: -len(JOURNAL_SUFFIX)]
        lock = FileLock(journal.with_name(f"{stem}.lock"))
        if not lock.acquire():
            continue  # a running writer holds it
        try:
            reports.append(_recover_journal(folder, journal, stem, categories))
        except Exception:
            logger.warning("capture: could not recover %s; the journal is kept",
                           journal, exc_info=True)
        finally:
            lock.release()
            remove_tree(folder / WRITING / stem)
            remove_tree(folder / WRITING / f"{stem}-rebuild")
    _remove_stale_leftovers(folder)
    return reports


def _recover_journal(folder: Path, journal: Path, stem: str,
                     categories: Sequence[str]) -> str:
    final = journal.with_name(f"{stem}{final_suffix()}")
    if not HAVE_XLSX:
        if final.exists():
            final = unique_path(journal.parent, f"{stem}_log", ".csv")
        replace(journal, final)
        return f"recovered {final} after an unexpected stop"
    if final.exists() and is_valid_xlsx(final):
        remove_file(journal)  # stopped after the rename, before the delete
        return f"{final} was already saved; removed its leftover journal"
    if final.exists():
        final = unique_path(journal.parent, f"{stem}_log", ".xlsx")
    part = final.with_name(final.name + ".part")
    done = False
    try:
        rows, damaged = build_from_journal(
            journal, part, folder / WRITING / f"{stem}-rebuild", categories)
        fsync_file(part)
        verify_xlsx(part)
        replace(part, final)
        done = True
    finally:
        if not done:
            remove_file(part)
    if damaged:
        kept = keep_damaged_journal(journal)
        return (f"recovered {final} ({rows:,} rows) after an unexpected stop; "
                f"{damaged:,} unreadable line(s) skipped, journal kept as {kept}")
    remove_file(journal)
    return f"recovered {final} ({rows:,} rows) after an unexpected stop"


def _remove_stale_leftovers(folder: Path) -> None:
    """Delete `.part` files and `.writing` folders no running writer is using."""
    for part in folder.glob("*/*.part"):
        stem = part.name.split("_log")[0]
        lock = FileLock(part.with_name(f"{stem}.lock"))
        if lock.acquire():
            lock.release()
            remove_file(part)
    writing = folder / WRITING
    if writing.is_dir():
        for leftover in writing.iterdir():
            stem = leftover.name.removesuffix("-rebuild")
            lock = FileLock(folder / stem[:10] / f"{stem}.lock")
            if lock.acquire():
                lock.release()
                remove_tree(leftover)


def prune(root: Path, *, keep_days: int, max_bytes: int,
          today: str) -> list[Path]:
    """Delete finished logs past `keep_days`, then oldest-first past `max_bytes`.

    Deletes only *finished* files (`*_log.xlsx`, `*_log.csv`, `*.damaged`) and
    never touches today's folder, a journal or a lock: an hour that is open or
    still waiting for recovery is not this function's to remove. Day folders
    left empty are removed. Returns the day folders it emptied or trimmed.
    """
    days: list[tuple[str, Path, list[Path], int]] = []
    for device_dir in device_folders(root):
        for day_dir in sorted(p for p in device_dir.iterdir()
                              if p.is_dir() and is_day(p.name)):
            if day_dir.name >= today:
                continue
            files = [p for p in day_dir.iterdir()
                     if p.is_file() and _is_finished(p.name)]
            size = sum(_size(p) for p in files)
            days.append((day_dir.name, day_dir, files, size))
    days.sort(key=lambda item: item[0])

    cutoff = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.mktime(time.strptime(today, "%Y-%m-%d"))
                       - keep_days * 86400))
    total = sum(size for *_, size in days) + _open_bytes(root, today)
    pruned: list[Path] = []
    for day, day_dir, files, size in days:
        if day >= cutoff and total <= max_bytes:
            continue
        for path in files:
            remove_file(path)
        total -= size
        pruned.append(day_dir)
        with contextlib.suppress(OSError):
            day_dir.rmdir()  # only succeeds once the folder is empty
    return pruned


def _is_finished(name: str) -> bool:
    return name.endswith((XLSX_SUFFIX, CSV_SUFFIX, DAMAGED_SUFFIX))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        logger.debug("capture: cannot size %s (%s)", path, exc)
        return 0


def _open_bytes(root: Path, today: str) -> int:
    """Bytes held in today's folders, which count towards the cap."""
    return sum(_size(p) for d in device_folders(root)
               for p in (d / today).glob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Helpers (from PyGUI unless noted)
# ---------------------------------------------------------------------------


class FileLock:
    """Exclusive OS lock on a small `.lock` file; dropped if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        except OSError as exc:
            logger.debug("capture: cannot open lock %s (%s)", self.path, exc)
            return False
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - EMCC runs on Windows
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # The normal "another writer holds it" answer, not a fault.
            logger.debug("capture: %s is held (%s)", self.path, exc)
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self, delete: bool = True) -> None:
        if self._fd is None:
            return
        if sys.platform == "win32":
            import msvcrt
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                # Closing the descriptor below releases it regardless.
                logger.debug("capture: unlock of %s failed (%s)", self.path, exc)
        os.close(self._fd)
        self._fd = None
        if delete:
            remove_file(self.path)


def period(ts: float, seconds: int, localtime=time.localtime) -> tuple[float, float]:
    """Start and end of the period holding `ts`, on local clock marks.

    Counted from local midnight, so files start at 14:00, 15:00... in any time
    zone, and stay one hour long in absolute time across daylight saving.
    """
    lt = localtime(ts)
    since_midnight = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (ts % 1)
    start = ts - (since_midnight % seconds)
    return start, start + seconds


_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10))}


def folder_name(name: str) -> str:
    """`name` made safe as a Windows folder name that recovery will also scan.

    Not hidden (no leading dot), not a reserved device name, not mistaken for
    a day folder.
    """
    safe = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c
                   for c in name)[:64].strip(" .")
    if not safe or safe.split(".")[0].upper() in _RESERVED or is_day(safe):
        safe = "_" + safe
    return safe


def device_folders(root: Path) -> list[Path]:
    """Per-device folders in `root`: every folder but day folders and dot-folders."""
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir())
            if p.is_dir() and not p.name.startswith(".") and not is_day(p.name)]


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_day(name: str) -> bool:
    """True for a `YYYY-MM-DD` folder name. A regex, not strptime-and-catch."""
    if not _DAY_RE.match(name):
        return False
    year, month, day = (int(part) for part in name.split("-"))
    return 1 <= month <= 12 and 1 <= day <= 31 and year >= 1970


def clean(value):
    """Replace characters XML cannot hold (e.g. a NUL from line noise).

    openpyxl rejects them, and after one rejected row a write-only sheet fails
    on every later row.
    """
    return ILLEGAL_CHARACTERS_RE.sub("�", value) if isinstance(value, str) else value


def streaming_workbook(temp_dir: Path, categories: Sequence[str]):
    """A write-only workbook whose sheets stream into `temp_dir`, not %TEMP%.

    openpyxl puts write-only sheets in anonymous temp files that a crash
    leaves behind; here they live where recovery can find and delete them.
    Relies on openpyxl 3.1's `WorksheetWriter(out=...)` and `ALL_TEMP_FILES`.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    sheets = {}
    for index, name in enumerate(categories, start=1):
        sheet = wb.create_sheet(title=name)
        for col, width in enumerate(COLUMN_WIDTHS, start=1):
            sheet.column_dimensions[get_column_letter(col)].width = width
        path = str(temp_dir / f"sheet{index}.xml")
        openpyxl_sheet_writer.ALL_TEMP_FILES.append(path)  # deleted after save
        sheet._writer = openpyxl_sheet_writer.WorksheetWriter(sheet, out=path)
        sheet._writer.write_top()
        sheet.append(list(HEADER))
        sheets[name] = sheet
    return wb, sheets


class JournalRows:
    """Rows of a journal, skipping lines that cannot be read.

    A final line torn by a crash is expected and skipped quietly. `damaged`
    counts unreadable lines with readable rows after them: the journal itself
    is damaged, so it is kept rather than deleted.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.damaged = 0

    def __iter__(self) -> Iterator[list]:
        unreadable = 0
        with open(self.path, newline="", encoding="utf-8-sig",
                  errors="replace") as f:
            reader = csv.reader(f)
            if next(reader, None) is None:  # header, or an empty journal
                return
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    return
                except csv.Error as exc:
                    logger.debug("capture: unreadable line in %s (%s)",
                                 self.path, exc)
                    unreadable += 1
                    continue
                if len(row) != len(HEADER):
                    unreadable += 1
                    continue
                self.damaged += unreadable
                unreadable = 0
                date, time_str, line, src, category, value, units = row
                yield [date, time_str, line, src or None, category,
                       _number(value), units]


def _number(text: str):
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return text


def build_from_journal(journal: Path, part: Path, temp_dir: Path,
                       categories: Sequence[str]) -> tuple[int, int]:
    """Write `part` from a journal. Returns `(rows, damaged)`."""
    wb, sheets = streaming_workbook(temp_dir, categories)
    reader = JournalRows(journal)
    rows = 0
    try:
        for row in reader:
            row = [clean(v) for v in row]
            sheets["All"].append(row)
            sheet = sheets.get(row[4])
            if sheet is not None and row[4] != "All":
                sheet.append(row)
            rows += 1
            if rows % 2000 == 0:
                time.sleep(0.001)  # let the UI thread in while rebuilding
        wb.save(part)
    finally:
        close_sheets(sheets)  # no-op after a successful save
        remove_tree(temp_dir)
    return rows, reader.damaged


def keep_damaged_journal(journal: Path) -> Path:
    """Rename a damaged journal out of recovery's way. Returns where it went."""
    kept = unique_path(journal.parent, journal.name, DAMAGED_SUFFIX)
    try:
        os.replace(journal, kept)
    except OSError:
        logger.warning("capture: could not set aside damaged journal %s",
                       journal, exc_info=True)
        return journal
    return kept


def close_sheets(sheets: dict) -> None:
    for sheet in sheets.values():
        try:
            if not sheet.closed:
                sheet.close()
        except Exception as exc:
            logger.debug("capture: closing a streaming sheet failed (%s)", exc)


def verify_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"damaged entry {bad}")


def is_valid_xlsx(path: Path) -> bool:
    try:
        verify_xlsx(path)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.debug("capture: %s is not a valid workbook (%s)", path, exc)
        return False
    return True


def fsync_file(path: Path) -> None:
    """Force `path`'s data to disk before it is renamed into place. (New.)"""
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def replace(part: Path, final: Path) -> None:
    """Rename into place, retrying while antivirus or indexing holds the file."""
    for attempt in range(10):
        try:
            os.replace(part, final)
            return
        except PermissionError as exc:
            if attempt == 9:
                raise
            logger.debug("capture: %s is held (%s); retrying the rename",
                         final, exc)
            time.sleep(0.2 * (attempt + 1))


def unique_stem(directory: Path, stem: str) -> str:
    """`stem`, or `stem-1`... if any file for it already exists."""
    candidate, n = stem, 1
    tails = (XLSX_SUFFIX, CSV_SUFFIX, JOURNAL_SUFFIX, ".lock")
    while any((directory / f"{candidate}{tail}").exists() for tail in tails):
        candidate = f"{stem}-{n}"
        n += 1
    return candidate


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate, n = directory / f"{stem}{suffix}", 1
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def hide(folder: Path) -> None:
    """Mark the temp folder hidden in Explorer (Windows only)."""
    if sys.platform == "win32" and folder.is_dir():
        import ctypes
        if not ctypes.windll.kernel32.SetFileAttributesW(str(folder), 0x2):
            logger.debug("capture: could not hide %s", folder)


def remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    except OSError as exc:
        logger.debug("capture: could not remove %s (%s)", path, exc)


def remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
