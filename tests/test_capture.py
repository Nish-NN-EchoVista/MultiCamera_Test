"""Per-device capture: levels, files, crash recovery and clean-up.

The lines used here are copied from real PyGUI captures of a dual-board
system over Moxa (diagnostic-20261006-140135), not invented.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from emcc.backend.capture import DeviceCapture, LogLevel
from emcc.backend.capture import files as capture_files
from emcc.backend.capture.levels import (
    EVENT,
    RX,
    TX,
    WARNING,
    Entry,
    LevelFilter,
    classify,
)

openpyxl = pytest.importorskip("openpyxl")

# A fixed local time well inside an hour, so the tests do not depend on when
# they run. 2026-11-05 14:20:00 local.
T0 = time.mktime((2026, 11, 5, 14, 20, 0, 0, 0, -1))


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def rows_of(path: Path, sheet: str = "All") -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return [tuple(r) for r in wb[sheet].iter_rows(min_row=2, values_only=True)]
    finally:
        wb.close()


def finished(root: Path) -> list[Path]:
    return sorted(root.rglob("*_log.xlsx"))


def leftovers(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.suffix in (".lock", ".part") or p.name.endswith("_live.csv"))


@pytest.fixture
def capture(tmp_path):
    clock = Clock()
    cap = DeviceCapture(tmp_path / "devices", level="full", clock=clock)
    cap.clock = clock
    yield cap
    cap.close(timeout=10)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def test_level_parse_accepts_values_and_labels_and_defaults_otherwise():
    assert LogLevel.parse("full") is LogLevel.FULL
    assert LogLevel.parse("Important only") is LogLevel.IMPORTANT
    assert LogLevel.parse(LogLevel.REDUCED) is LogLevel.REDUCED
    assert LogLevel.parse("nonsense") is LogLevel.REDUCED
    assert LogLevel.parse(None) is LogLevel.REDUCED


@pytest.mark.parametrize("line, category, value, important", [
    ("PZT Temp: 24.4 C", "PZT Temp", 24.4, False),
    ("PZT Temp: 240.10 C", "PZT Temp", 241.0, False),   # firmware carry quirk
    ("[ESV2-1] Board Temperature = 36.8", "Board Temp - ESV2-1", 36.8, False),
    ("[ESV2-2] Power: 0.0W", "Power - ESV2-2", 0.0, False),
    ("[ESV2-2] W: Defog TC stale -> power cut (age=2120ms)", "Warnings", None, True),
    ("W: System rebooted due to watchdog", "Warnings", None, True),
    ("[ESV2-1] Evaporation sweep triggered", "Events", None, True),
    ("[ESV2-1] Evaporation Sweep Completed", "Events", None, True),
    ("[ESV2-2] I: DeFog sequence started (PID-driven)", "Events", None, True),
    ("PZT overheat detected. Entering cooldown lockout", "Events", None, True),
    ("TX to ESV2-1: start_evap_sweep", "Events", None, False),
    ("[ESV2-1] Thermal check pass", "", None, False),
    ("[ESV2-1] Thermal check fail", "", None, True),
    ("[ESV2-2] I: Frequency 1 changed to: 2100000", "", None, False),
])
def test_classify_files_real_lines(line, category, value, important):
    info = classify(Entry(T0, RX, line))
    assert (info.category, info.value, info.important) == (category, value, important)
    assert info.message == line


def test_commands_and_warnings_from_emcc_are_important():
    assert classify(Entry(T0, TX, "start_evap_sweep")).message == "> start_evap_sweep"
    assert classify(Entry(T0, TX, "start_evap_sweep")).important
    lost = classify(Entry(T0, EVENT, "Connection lost", WARNING))
    assert (lost.category, lost.important) == ("Warnings", True)
    assert not classify(Entry(T0, EVENT, "Reconnect attempt 1/3")).important


def _apply(level, lines, step=0.5):
    flt = LevelFilter()
    kept = []
    for i, line in enumerate(lines):
        entry = Entry(T0 + i * step, RX, line)
        kept += [info.message for _, info in flt.apply(level, "dev", entry, classify(entry))]
    kept += [info.message for _, info in flt.pending("dev")]
    return kept


def test_reduced_keeps_readings_only_when_they_move():
    lines = ["PZT Temp: 24.4 C", "PZT Temp: 24.5 C", "PZT Temp: 24.8 C",
             "PZT Temp: 25.0 C", "PZT Temp: 24.9 C"]
    # 24.4 kept; 24.5 and 24.8 within 0.5 of it; 25.0 moved 0.6; 24.9 within.
    assert _apply(LogLevel.REDUCED, lines) == ["PZT Temp: 24.4 C", "PZT Temp: 25.0 C"]


def test_reduced_writes_an_unchanged_reading_again_after_a_minute():
    lines = ["[ESV2-1] Power: 0.0W"] * 130          # 65 s at 2 Hz
    assert _apply(LogLevel.REDUCED, lines) == ["[ESV2-1] Power: 0.0W"] * 2


def test_reduced_folds_repeats_into_a_count_and_never_loses_it():
    line = "[ESV2-2] W: Defog TC stale -> power cut (age=2120ms)"
    kept = _apply(LogLevel.REDUCED, [line] * 5 + ["[ESV2-2] I: Pump enabled"])
    assert kept == [line, "(previous line repeated 4 more times)",
                    "[ESV2-2] I: Pump enabled"]
    # A run still open when logging stops is flushed by `pending`.
    assert _apply(LogLevel.REDUCED, [line] * 3)[-1] == "(previous line repeated 2 more times)"


def test_important_keeps_only_warnings_triggers_and_context():
    lines = ["PZT Temp: 24.4 C", "[ESV2-1] Thermal check pass",
             "[ESV2-1] Evaporation sweep triggered", "[ESV2-1] W: low supply"]
    assert _apply(LogLevel.IMPORTANT, lines) == lines[2:]
    flt = LevelFilter()
    started = Entry(T0, EVENT, "Logging started", context=True)
    assert flt.apply(LogLevel.IMPORTANT, "dev", started, classify(started))


def test_full_keeps_everything():
    lines = ["PZT Temp: 24.4 C"] * 3 + ["[ESV2-1] Thermal check pass"] * 3
    assert _apply(LogLevel.FULL, lines) == lines


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_two_devices_interleaved_get_one_file_each(capture, tmp_path):
    capture.enable("a", "Camera 1")
    capture.enable("b", "Camera 2")
    for i in range(20):
        capture.clock.now = T0 + i
        capture.record_rx("a", f"PZT Temp: {20 + i}.0 C")
        capture.record_rx("b", f"[ESV2-1] Power: {i}.0W")
    capture.record_tx("a", "start_evap_sweep")
    capture.disable("a")
    capture.disable("b")
    assert capture.sync() and capture.wait_saved()

    root = tmp_path / "devices"
    files = finished(root)
    assert [p.parent.parent.name for p in files] == ["Camera 1", "Camera 2"]
    assert files[0].name == "2026-11-05_1420_log.xlsx"   # named by the start
    a = rows_of(files[0])
    # started + 20 readings + command + stopped
    assert len(a) == 23
    assert a[0][2].startswith("Logging started") and a[-1][2] == "Logging stopped"
    assert ("> start_evap_sweep", "EMCC", "Events") == a[-2][2:5]
    assert len(rows_of(files[0], "PZT Temp")) == 20
    assert len(rows_of(files[1], "Power - ESV2-1")) == 20
    assert leftovers(root) == []


def test_an_hour_mark_starts_a_file_named_by_the_mark(capture, tmp_path):
    capture.enable("a", "Camera 1")
    capture.record_rx("a", "PZT Temp: 20.0 C")
    capture.clock.now = T0 + 45 * 60          # 15:05
    capture.record_rx("a", "PZT Temp: 21.0 C")
    capture.disable("a")
    assert capture.sync() and capture.wait_saved()
    names = [p.name for p in finished(tmp_path / "devices")]
    assert names == ["2026-11-05_1420_log.xlsx", "2026-11-05_1500_log.xlsx"]


def test_a_repeat_run_across_the_hour_mark_does_not_split_the_next_file(tmp_path):
    clock = Clock(T0 + 39 * 60 + 50)              # 14:59:50
    cap = DeviceCapture(tmp_path / "devices", level="reduced", clock=clock)
    cap.enable("a", "Camera 1")
    line = "[ESV2-2] W: Defog TC stale -> power cut (age=2120ms)"
    for second in range(0, 20, 2):                 # 14:59:50 .. 15:00:08
        clock.now = T0 + 39 * 60 + 50 + second
        cap.record_rx("a", line)
    assert cap.sync()
    clock.now = T0 + 40 * 60 + 10                  # past the mark and the grace
    # Twice: a sync returns when its marker is drained, which is before the
    # same tick's hour-mark check; the second guarantees that check has run.
    assert cap.sync() and cap.sync()
    clock.now = T0 + 40 * 60 + 20
    cap.record_rx("a", "[ESV2-2] I: Pump enabled")
    cap.disable("a")
    assert cap.sync() and cap.wait_saved()
    cap.close(timeout=5)
    names = [p.name for p in finished(tmp_path / "devices")]
    assert names == ["2026-11-05_1459_log.xlsx", "2026-11-05_1500_log.xlsx"], names


def test_a_rename_continues_in_the_new_folder(capture, tmp_path):
    capture.enable("a", "Camera 1")
    capture.record_rx("a", "PZT Temp: 20.0 C")
    assert capture.sync()
    capture.rename("a", "Gate 2: LPR")      # ':' is not allowed in a folder
    capture.record_rx("a", "PZT Temp: 21.0 C")
    capture.disable("a")
    assert capture.sync() and capture.wait_saved()
    folders = [p.parent.parent.name for p in finished(tmp_path / "devices")]
    assert folders == ["Camera 1", "Gate 2_ LPR"]


def test_changing_level_applies_from_the_next_row(capture, tmp_path):
    capture.enable("a", "Camera 1")
    capture.record_rx("a", "PZT Temp: 20.0 C")
    capture.set_level("important")
    capture.record_rx("a", "PZT Temp: 30.0 C")                   # dropped
    capture.record_rx("a", "[ESV2-1] Evaporation sweep triggered")
    capture.disable("a")
    assert capture.sync() and capture.wait_saved()
    messages = [r[2] for r in rows_of(finished(tmp_path / "devices")[0])]
    assert messages[1:] == ["PZT Temp: 20.0 C", "Log level changed to Important only",
                            "[ESV2-1] Evaporation sweep triggered", "Logging stopped"]


def test_records_for_a_device_not_being_logged_are_ignored(capture, tmp_path):
    capture.record_rx("nobody", "PZT Temp: 20.0 C")
    assert capture.sync()
    assert not (tmp_path / "devices").exists()


def test_no_capture_thread_runs_once_logging_is_off(capture):
    capture.enable("a", "Camera 1")
    capture.record_rx("a", "PZT Temp: 20.0 C")
    capture.disable("a")
    assert capture.sync() and capture.wait_saved()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
            t.name.startswith("emcc-capture") for t in threading.enumerate()):
        time.sleep(0.05)
    assert not [t.name for t in threading.enumerate()
                if t.name.startswith("emcc-capture")]


# ---------------------------------------------------------------------------
# Crash recovery and clean-up
# ---------------------------------------------------------------------------


def test_an_hour_left_by_a_crash_is_rebuilt_on_the_next_start(tmp_path):
    root = tmp_path / "devices"
    segment = capture_files.Segment(root, "Camera 1", "2026-11-05",
                                    "2026-11-05_1420", T0, T0 + 3600,
                                    ("All", "PZT Temp"))
    segment.write(["2026-11-05", "14:20:00", "PZT Temp: 20.0 C", "Adapter",
                   "PZT Temp", 20.0, "°C"])
    segment.flush(fsync=True)
    # A crash: the process dies, the OS drops the lock, nothing is saved.
    segment.abandon()
    assert not finished(root)

    cap = DeviceCapture(root, clock=Clock())
    cap.start()
    assert cap.wait_saved()
    (saved,) = finished(root)
    assert rows_of(saved)[0][2] == "PZT Temp: 20.0 C"
    assert not list(root.rglob("*_live.csv"))


def _day(root: Path, device: str, day: str, *, size: int = 10,
         journal: bool = False) -> Path:
    folder = root / device / day
    folder.mkdir(parents=True)
    (folder / f"{day}_1400_log.xlsx").write_bytes(b"x" * size)
    if journal:
        (folder / f"{day}_1500_live.csv").write_bytes(b"x")
    return folder


def test_prune_removes_old_days_but_never_today_or_a_journal(tmp_path):
    root = tmp_path / "devices"
    old = _day(root, "Camera 1", "2026-09-01")
    old_with_journal = _day(root, "Camera 2", "2026-09-01", journal=True)
    recent = _day(root, "Camera 1", "2026-11-04")
    today = _day(root, "Camera 1", "2026-11-05")

    pruned = capture_files.prune(root, keep_days=30, max_bytes=10 ** 9,
                                 today="2026-11-05")
    assert set(pruned) == {old, old_with_journal}
    assert not old.exists()
    assert [p.name for p in old_with_journal.iterdir()] == ["2026-09-01_1500_live.csv"]
    assert recent.exists() and today.exists()


def test_prune_trims_oldest_first_past_the_size_cap(tmp_path):
    root = tmp_path / "devices"
    first = _day(root, "Camera 1", "2026-11-01", size=100)
    second = _day(root, "Camera 1", "2026-11-02", size=100)
    _day(root, "Camera 1", "2026-11-05", size=100)
    pruned = capture_files.prune(root, keep_days=30, max_bytes=250,
                                 today="2026-11-05")
    assert pruned == [first]
    assert second.exists()


def test_folder_names_are_safe_on_windows():
    assert capture_files.folder_name("COM7") == "_COM7"
    assert capture_files.folder_name("2026-11-05") == "_2026-11-05"
    assert capture_files.folder_name("Gate/2 *LPR*") == "Gate_2 _LPR_"
    assert capture_files.folder_name("..hidden") == "hidden"
