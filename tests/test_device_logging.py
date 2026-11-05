"""The Log button, the Settings view, and what reaches a device's log.

Covers the wiring around `capture.DeviceCapture` (tested on its own in
`test_capture.py`): the connection worker feeding it, `DeviceManager`
switching it per device, and the List, Dashboard and Settings views.
"""

from __future__ import annotations

import queue
import time

import customtkinter as ctk
import pytest

from emcc.backend.capture import DeviceCapture, LogLevel
from emcc.backend.config_manager import ConfigManager, Settings
from emcc.backend.connection_worker import ConnectionWorker
from emcc.backend.device_manager import DeviceManager
from emcc.backend.events import EventType
from emcc.backend.protocol import CMD_CLEAN
from emcc.widgets import view_toggle
from emcc.widgets.canvas_util import scaling
from tools.mock_nport import MockConfig, MockNPort

from .conftest import make_app

openpyxl = pytest.importorskip("openpyxl")


def _rows(path) -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return [tuple(r) for r in wb["All"].iter_rows(min_row=2, values_only=True)]
    finally:
        wb.close()


# ---------------------------------------------------------------------------
# Worker -> capture, against the mock NPort (no hardware)
# ---------------------------------------------------------------------------


def test_a_logged_device_records_lines_commands_and_connection(tmp_path):
    capture = DeviceCapture(tmp_path / "devices", level="full")
    capture.enable("dev-1", "Camera 1")
    with MockNPort(MockConfig()) as mock:
        settings = Settings(tcp_port=mock.port, connection_timeout_s=3.0,
                            temperature_update_interval_s=0.0)
        events: queue.Queue = queue.Queue()
        worker = ConnectionWorker("dev-1", "Camera 1", "127.0.0.1", settings,
                                  events, capture=capture)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if events.get(timeout=1).type is EventType.TEMPERATURE:
                break
        worker.send(CMD_CLEAN)
        time.sleep(0.6)            # a temperature line or two more
        worker.disconnect()
        worker.join(timeout=3)
    capture.disable("dev-1")
    assert capture.sync() and capture.wait_saved()

    (log,) = sorted((tmp_path / "devices" / "Camera 1").rglob("*_log.xlsx"))
    messages = [r[2] for r in _rows(log)]
    assert messages[0] == "Logging started (Full logs)"
    assert any(m.startswith("Connected to 127.0.0.1:") for m in messages)
    assert any(m.startswith("PZT Temp:") for m in messages)
    assert "> start_evap_sweep" in messages
    assert "Disconnected by operator" in messages
    assert messages[-1] == "Logging stopped"


def test_a_device_not_logged_costs_nothing_on_disk(tmp_path):
    capture = DeviceCapture(tmp_path / "devices")
    with MockNPort(MockConfig()) as mock:
        settings = Settings(tcp_port=mock.port, temperature_update_interval_s=0.0)
        events: queue.Queue = queue.Queue()
        worker = ConnectionWorker("dev-1", "Camera 1", "127.0.0.1", settings,
                                  events, capture=capture)
        worker.start()
        assert events.get(timeout=5).type is EventType.CONNECTED
        time.sleep(0.6)
        worker.stop()
        worker.join(timeout=3)
    assert capture.sync()
    assert not (tmp_path / "devices").exists()


# ---------------------------------------------------------------------------
# DeviceManager
# ---------------------------------------------------------------------------


class _Recorder:
    """Stands in for `DeviceCapture`, recording what the manager asks of it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.level = None

    def enable(self, device_id, name):
        self.calls.append(("enable", device_id, name))

    def disable(self, device_id):
        self.calls.append(("disable", device_id))

    def rename(self, device_id, name):
        self.calls.append(("rename", device_id, name))

    def set_level(self, level):
        self.level = level

    def close(self, timeout=5.0):
        self.calls.append(("close",))
        return True


@pytest.fixture
def manager(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    config.load()
    recorder = _Recorder()
    mgr = DeviceManager(config, schedule=lambda ms, fn: None,
                        cancel=lambda job: None, capture=recorder)
    mgr.recorder = recorder
    yield mgr
    mgr.shutdown(timeout_s=1.0)
    config.close()


def test_log_button_switches_one_device(manager):
    first, second = manager.devices[:2]
    assert not any(d.logging_enabled for d in manager.devices)  # always starts off
    assert manager.toggle_logging(first.id) is True
    assert first.logging_enabled and not second.logging_enabled
    assert manager.toggle_logging(first.id) is False
    assert manager.recorder.calls == [("enable", first.id, first.name),
                                      ("disable", first.id)]


def test_logging_is_never_saved(manager, tmp_path):
    manager.toggle_logging(manager.devices[0].id)
    manager.config.save_now()
    assert "logging" not in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_a_shared_name_gets_its_ip_and_a_rename_repoints_the_log(manager):
    first, second = manager.devices[:2]
    manager.toggle_logging(first.id)
    manager.rename_device(second.id, first.name)       # now both "Camera 1"
    assert manager.log_folder(first.id) == f"{first.name} ({first.ip})"
    assert ("rename", first.id, f"{first.name} ({first.ip})") in manager.recorder.calls


def test_a_nameless_device_logs_under_its_ip(manager):
    device = manager.devices[0]
    manager.rename_device(device.id, "")
    assert manager.log_folder(device.id) == device.ip


def test_removing_a_logged_device_stops_its_log(manager):
    device = manager.devices[0]
    manager.toggle_logging(device.id)
    manager.remove_device(device.id)
    assert manager.recorder.calls[-1] == ("disable", device.id)


def test_log_level_is_saved_and_applied(manager, tmp_path):
    assert manager.log_level is LogLevel.REDUCED
    manager.set_log_level("important")
    manager.config.save_now()
    assert manager.recorder.level is LogLevel.IMPORTANT
    assert '"log_level": "important"' in (tmp_path / "config.json").read_text(encoding="utf-8")


def test_shutdown_closes_the_device_logs(manager):
    manager.shutdown(timeout_s=1.0)
    assert ("close",) in manager.recorder.calls


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path):
    ctk.set_appearance_mode("dark")
    config = ConfigManager(tmp_path / "config.json")
    config.load()
    application = make_app(config)
    # Never the real logs folder from a test.
    application.manager.capture.root = tmp_path / "devices"
    _pump(application, 8)
    yield application
    application.shutdown()


def _pump(app, times: int = 4) -> None:
    for _ in range(times):
        app.update_idletasks()
        app.update()


def _centre_x(widget) -> float:
    return widget.winfo_rootx() + widget.winfo_width() / 2


def _assert_even(buttons, factor) -> None:
    """Same width (to a device pixel) and every button's content centred.

    For a worded button (with a tick slot) the content is the word, so the
    word itself is checked. Auto's content is its pill *and* its word, so it
    is that group which is centred -- the word alone sits right of centre by
    the pill's width, correctly.
    """
    widths = [b.winfo_width() for b in buttons]
    assert max(widths) - min(widths) <= 1, widths
    for button in buttons:
        content = button._label if hasattr(button, "_icon") else button._label.master
        offset = abs(_centre_x(content) - _centre_x(button))
        assert offset <= 1.5 * factor, (button._label.cget("text"), offset)


def test_list_rows_have_an_even_centred_button_row_with_log(app):
    card = app._cards[app.manager.devices[0].id]
    _assert_even([card._clean, card._deice, card._defog, card._log],
                 scaling(app))


def test_log_button_lights_on_the_list(app):
    device = app.manager.devices[0]
    card = app._cards[device.id]
    app._toggle_logging(device.id)
    assert device.logging_enabled and card._log._icon_visible
    assert app.manager.capture.is_enabled(device.id)
    app._toggle_logging(device.id)
    assert not card._log._icon_visible


def test_dashboard_buttons_are_one_size_with_words_centred(app):
    app._switch_view("dashboard")
    _pump(app, 8)
    card = app.views.active.cards[app.manager.devices[0].id]
    _assert_even([card._clean, card._auto, card._deice, card._defog],
                 scaling(app))
    heights = {b.winfo_height() for b in (card._clean, card._auto,
                                          card._deice, card._defog)}
    assert len(heights) == 1


def test_log_button_lights_on_the_dashboard(app):
    app._switch_view("dashboard")
    _pump(app, 8)
    device = app.manager.devices[0]
    card = app.views.active.cards[device.id]
    app._toggle_logging(device.id)
    assert card._log._icon_visible


def test_settings_is_the_third_view_and_sets_the_level(app):
    assert list(app._sub.view_toggle._segments) == [
        view_toggle.LIST, view_toggle.DASHBOARD, view_toggle.SETTINGS]
    app._switch_view("settings")
    _pump(app, 4)
    settings = app.views.active
    assert settings.heading == "SETTINGS"
    settings._select(LogLevel.IMPORTANT)
    assert app.config_manager.settings.log_level == "important"
    assert app.manager.capture.level is LogLevel.IMPORTANT
    assert "Important only" in settings.caption(len(app.manager.devices))
