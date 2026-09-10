"""End-to-end UI tests: real App, real widgets, real sockets to a mock NPort.

These assert on widget configuration rather than rendered pixels, so they work
headlessly (and on a locked workstation) and they check the thing that actually
matters: that the UI renders backend state faithfully.

Every test drives the app the way the operator does -- through the card's
callbacks -- and then pumps the event loop, so the worker -> queue -> pump ->
render path is exercised for real.
"""

from __future__ import annotations

import pathlib
import re
import time

import pytest

import customtkinter as ctk

from emcc import app as app_module
from emcc import fonts, icons, theme
from emcc.app import App
from emcc.backend.config_manager import ConfigManager
from emcc.backend.events import ConnectionState
from emcc.backend.protocol import (
    CLEAN_COMPLETE,
    CMD_AUTO_OFF,
    CMD_AUTO_ON,
    CMD_CLEAN,
)

from tools.mock_nport import MockConfig, MockNPort

from .conftest import make_app


@pytest.fixture()
def app(tmp_path):
    ctk.set_appearance_mode("dark")
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

    application = make_app(config)
    application.geometry("1280x760+40+40")
    _pump(application, 8)
    yield application
    try:
        application.shutdown()
    except Exception:
        pass


def _pump(app: App, times: int = 4) -> None:
    for _ in range(times):
        app.update_idletasks()
        app.update()


def _pump_until(app: App, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._pump()                 # drain backend events now, not on the timer
        _pump(app, 2)
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _connect(app: App, mock: MockNPort, index: int = 0):
    device = app.manager.devices[index]
    app.config_manager.settings.tcp_port = mock.port
    app.manager.set_ip(device.id, "127.0.0.1")
    app._connect_device(device.id)
    assert _pump_until(app, lambda: device.is_connected), "never reached CONNECTED"
    return device


# ---------------------------------------------------------------------------
# Scenario A -- fresh start
# ---------------------------------------------------------------------------


def test_scenario_a_three_cards_all_disconnected(app):
    assert len(app.manager.devices) == 3
    assert len(app._cards) == 3
    names = [d.name for d in app.manager.devices]
    assert names == ["Camera 1", "Camera 2", "Camera 3"]
    for device in app.manager.devices:
        assert device.connection is ConnectionState.DISCONNECTED
        assert device.auto_enabled is False
        assert device.clean_active is False
        assert device.latest_temperature is None


def test_scenario_a_no_auto_connect(app):
    """Nothing may open a socket at startup."""
    assert app.manager._workers == {}


def test_connect_button_starts_blue_with_no_data_temperature(app):
    card = app._cards[app.manager.devices[0].id]
    assert card._connection._style == "idle"
    assert card._connection._label.cget("text") == "Connect"
    assert theme.TEMP_PLACEHOLDER in card._temp._value.cget("text")


def test_temperature_placeholder_uses_ghost_colour(app):
    card = app._cards[app.manager.devices[0].id]
    assert card._temp._value.cget("text_color") == theme.TEXT_GHOST


def test_trash_icon_blends_into_the_name_field(app):
    """It is placed over the entry but parented to the column.

    A transparent label therefore paints the *column's* colour, which a visual
    pass caught as a lighter square sitting on the darker input.
    """
    card = app._cards[app.manager.devices[0].id]
    assert card._trash.cget("bg_color") == theme.INPUT_BG


# ---------------------------------------------------------------------------
# Scenario B -- persistence
# ---------------------------------------------------------------------------


def test_scenario_b_name_and_ip_survive_restart(app, tmp_path):
    device = app.manager.devices[0]
    app.manager.rename_device(device.id, "Left Nozzle")
    app.manager.set_ip(device.id, "192.168.2.100")
    app.config_manager.flush()

    reloaded = ConfigManager(tmp_path / "config.json")
    reloaded.load()
    assert reloaded.devices[0].device_name == "Left Nozzle"
    assert reloaded.devices[0].ip_address == "192.168.2.100"


def test_editing_the_entry_widget_persists(app, tmp_path):
    """Typing into the field, not just calling the API."""
    card = app._cards[app.manager.devices[0].id]
    entry = card._build_name.__self__._sections[0].body.winfo_children()[0]
    entry = entry.winfo_children()[0] if not isinstance(entry, ctk.CTkEntry) else entry
    # Locate the CTkEntry robustly.
    holder = card._sections[0].body.winfo_children()[0]
    entries = [w for w in holder.winfo_children() if isinstance(w, ctk.CTkEntry)]
    assert entries, "no entry found in the Device Name column"
    entries[0].delete(0, "end")
    entries[0].insert(0, "Renamed By Typing")
    _pump(app)
    app.config_manager.flush()

    reloaded = ConfigManager(tmp_path / "config.json")
    reloaded.load()
    assert reloaded.devices[0].device_name == "Renamed By Typing"


# ---------------------------------------------------------------------------
# Scenario C -- connect, dis_auto, streaming temperature
# ---------------------------------------------------------------------------


def test_scenario_c_connect_turns_card_green(app):
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        card = app._cards[device.id]
        assert card._connection._style == "connected"
        assert card._connection._label.cget("text") == "Connected"
        assert card._connection._caption.cget("text") == "Stream active"


def test_scenario_c_auto_reset_is_sent_after_connect(app):
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        # Fire the scheduled reset immediately rather than waiting 2s.
        app.manager._on_auto_reset(device.id)
        assert _pump_until(
            app, lambda: CMD_AUTO_OFF in mock.received_commands, timeout=3.0
        ), f"dis_auto not sent; saw {mock.received_commands}"


def test_scenario_c_streaming_temperature_reaches_the_card(app):
    with MockNPort(MockConfig(temp_start=44.2, temp_drift=0.0)) as mock:
        device = _connect(app, mock)
        assert _pump_until(app, lambda: device.latest_temperature is not None)
        card = app._cards[device.id]
        assert "44.2" in card._temp._value.cget("text")
        assert card._temp._value.cget("text_color") == theme.TEMP_OK_TEXT


def test_temperature_goes_to_the_right_card_only(app):
    with MockNPort(MockConfig(temp_start=50.0, temp_drift=0.0)) as mock:
        device = _connect(app, mock, index=1)     # the middle card
        assert _pump_until(app, lambda: device.latest_temperature is not None)
        others = [d for d in app.manager.devices if d.id != device.id]
        assert all(d.latest_temperature is None for d in others)


# ---------------------------------------------------------------------------
# Scenario D -- threshold
# ---------------------------------------------------------------------------


def test_scenario_d_over_threshold_turns_red(app):
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT Temp: 66.5 C")
        assert _pump_until(app, lambda: device.temperature_alert)
        card = app._cards[device.id]
        assert card._temp._value.cget("text_color") == theme.TEMP_ALERT_TEXT
        assert card._temp._badge.winfo_ismapped()


def test_scenario_d_back_below_threshold_returns_white(app):
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT Temp: 66.5 C")
        assert _pump_until(app, lambda: device.temperature_alert)
        mock.push("PZT Temp: 64.0 C")
        assert _pump_until(app, lambda: not device.temperature_alert)
        card = app._cards[device.id]
        assert card._temp._value.cget("text_color") == theme.TEMP_OK_TEXT


def test_scenario_d_only_the_hot_device_reddens(app):
    mocks = [MockNPort(MockConfig(temp_interval_s=99)).start() for _ in range(2)]
    try:
        first = _connect(app, mocks[0], index=0)
        app.config_manager.settings.tcp_port = mocks[1].port
        second = app.manager.devices[1]
        app.manager.set_ip(second.id, "127.0.0.1")
        app._connect_device(second.id)
        assert _pump_until(app, lambda: second.is_connected)

        mocks[0].push("PZT Temp: 80.0 C")
        mocks[1].push("PZT Temp: 40.0 C")
        assert _pump_until(app, lambda: first.temperature_alert
                           and second.latest_temperature is not None)
        assert first.temperature_alert is True
        assert second.temperature_alert is False
    finally:
        for mock in mocks:
            mock.stop()


def test_threshold_appears_in_the_title_bar_count(app):
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT Temp: 90.0 C")
        assert _pump_until(app, lambda: device.temperature_alert)
        assert app.manager.alert_count() == 1
        assert app.title_bar._alerts_shown is True
        assert app.title_bar._alert_wrap.winfo_ismapped()


def test_title_bar_alert_chip_disappears_when_temperature_returns(app):
    """The chip must come back *off*, not just go on.

    A visual pass caught it staying on screen after the count returned to
    zero: `_relayout` only unpacked the widgets it was about to re-pack, so
    hiding never actually unpacked the chip.
    """
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT Temp: 90.0 C")
        assert _pump_until(app, lambda: device.temperature_alert)
        assert app.title_bar._alerts_shown is True

        mock.push("PZT Temp: 40.0 C")
        assert _pump_until(app, lambda: not device.temperature_alert)
        assert app.manager.alert_count() == 0
        assert app.title_bar._alerts_shown is False
        _pump(app, 4)
        assert not app.title_bar._alert_wrap.winfo_ismapped()
        assert not app.title_bar._alert_pip.winfo_ismapped()


def test_title_bar_alert_chip_clears_on_disconnect(app):
    """Disconnecting drops the reading, so the chip must go too."""
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT Temp: 90.0 C")
        assert _pump_until(app, lambda: device.temperature_alert)

        app._connect_device(device.id)          # a second click disconnects
        assert _pump_until(app, lambda: not device.connection.is_live)
        assert app.manager.alert_count() == 0
        _pump(app, 4)
        assert not app.title_bar._alert_wrap.winfo_ismapped()


# ---------------------------------------------------------------------------
# Scenario E -- Clean
# ---------------------------------------------------------------------------


def test_scenario_e_clean_sends_command_and_turns_green(app):
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        app._clean_device(device.id)
        _pump(app)
        assert device.clean_active is True
        card = app._cards[device.id]
        assert card._clean.cget("fg_color") in (theme.ON_BG, theme.ON_BG_HOVER)
        assert _pump_until(app, lambda: CMD_CLEAN in mock.received_commands)


def test_scenario_e_completion_returns_clean_to_blue(app):
    # The sweep is held open (99s) and completion is pushed by hand rather than
    # timed. A short duration would let the completion land before the
    # `clean_active is True` assertion under load, failing on timing alone.
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        app._clean_device(device.id)
        _pump(app)
        assert device.clean_active is True
        mock.push(f"[ESV2-1] {CLEAN_COMPLETE}")
        assert _pump_until(app, lambda: not device.clean_active, timeout=5.0)
        card = app._cards[device.id]
        assert card._clean.cget("fg_color") in (
            theme.CLEAN_OFF_BG, theme.CLEAN_OFF_BG_HOVER
        )


def test_clean_tick_is_removed_on_completion(app):
    """The colours reverting is not enough -- the tick must actually go.

    `CTkLabel.configure(image=None)` records the None without applying it, so
    the tick used to stay on screen over the reverted blue fill. A visual pass
    caught it during a reconnect.
    """
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        card = app._cards[device.id]
        app._clean_device(device.id)
        _pump(app)
        assert card._clean._icon.cget("image") is icons.get(
            "check", theme.ON_TEXT, 11
        )

        mock.push(f"[ESV2-1] {CLEAN_COMPLETE}")
        assert _pump_until(app, lambda: not device.clean_active)
        assert card._clean._icon.cget("image") is icons.blank(11)


def test_clean_tick_is_removed_when_the_link_drops(app):
    """A dropped link ends the sweep, so the tick must go with it."""
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        card = app._cards[device.id]
        app._clean_device(device.id)
        _pump(app)
        assert device.clean_active is True

    # Mock closed by the context manager: the worker sees the peer go away.
    assert _pump_until(app, lambda: not device.clean_active, timeout=8.0)
    assert card._clean._icon.cget("image") is icons.blank(11)


def test_scenario_e_timeout_returns_clean_to_blue(app):
    """The Clean timeout clears the flag and repaints the card.

    The timeout is fired directly rather than waited out. Shortening
    `clean_timeout_s` to 300ms and racing it was flaky -- baseline recorded
    1 failure in 3 passes, on the slowest one: `_pump` can itself exceed
    300ms under load, so the timer fired before `clean_active is True` was
    read. Third instance of one defect shape in this file -- a test asserting
    a transient whose lifetime it does not control -- so it uses the idiom
    already established at `test_scenario_c_auto_reset_is_sent_after_connect`
    and drives the timer instead of the clock.

    Dies on: removing `state.clean_active = False` from `_on_clean_timeout`;
    and, separately, removing its `_notify_ui(device_id)` call -- that one is
    caught only by the colour and tick assertions, because the flag would
    clear without the card ever repainting.
    """
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        app._clean_device(device.id)
        _pump(app)
        assert device.clean_active is True
        # The timer was actually armed. Firing the handler by hand proves the
        # handler; this proves the arming, so nothing is lost by not waiting
        # out the clock. What is deliberately *not* asserted is the computed
        # delay -- that would mirror the implementation, and "Tk invokes a
        # callback after N ms" is Tk's contract, not this product's.
        assert device._clean_timer is not None

        app.manager._on_clean_timeout(device.id)
        assert _pump_until(app, lambda: not device.clean_active, timeout=2.0)

        card = app._cards[device.id]
        assert card._clean.cget("fg_color") in (
            theme.CLEAN_OFF_BG, theme.CLEAN_OFF_BG_HOVER
        )
        # The completion route asserts this; the timeout route runs the same
        # `_paint`, so without it a regression to `configure(image=None)`
        # would keep the tick here and nothing would notice.
        assert card._clean._icon.cget("image") is icons.blank(11)


def test_clean_button_width_is_stable_across_state(app):
    """The icon slot is reserved, so Auto must not shift when Clean activates."""
    with MockNPort(MockConfig(clean_duration_s=99)) as mock:
        device = _connect(app, mock)
        card = app._cards[device.id]
        _pump(app, 4)
        idle_width = card._clean.winfo_width()
        auto_x = card._auto.winfo_x()

        app._clean_device(device.id)
        _pump(app, 4)
        assert card._clean.winfo_width() == idle_width
        assert card._auto.winfo_x() == auto_x


def test_clean_while_disconnected_is_rejected(app):
    device = app.manager.devices[0]
    assert app.manager.start_clean(device.id) is False
    assert device.clean_active is False


# ---------------------------------------------------------------------------
# Scenario F -- Auto
# ---------------------------------------------------------------------------


def test_scenario_f_auto_toggles_and_sends_commands(app):
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        app._toggle_auto(device.id)
        _pump(app)
        assert device.auto_enabled is True
        card = app._cards[device.id]
        assert card._auto.cget("fg_color") in (theme.ON_BG, theme.ON_BG_HOVER)

        app._toggle_auto(device.id)
        _pump(app)
        assert device.auto_enabled is False
        assert card._auto.cget("fg_color") == theme.AUTO_OFF_BG

        assert _pump_until(
            app, lambda: CMD_AUTO_ON in mock.received_commands
            and CMD_AUTO_OFF in mock.received_commands
        )


def test_auto_is_never_persisted(app, tmp_path):
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        app._toggle_auto(device.id)
        _pump(app)
        assert device.auto_enabled is True
        app.config_manager.flush()

        reloaded = ConfigManager(tmp_path / "config.json")
        reloaded.load()
        entry = vars(reloaded.devices[0])
        assert "auto" not in " ".join(entry.keys()).lower()


# ---------------------------------------------------------------------------
# Scenario G -- drop and reconnect
# ---------------------------------------------------------------------------


def test_scenario_g_drop_shows_reconnecting_then_recovers(app):
    """RECONNECTING has to be made observable, not merely triggered.

    The worker's first retry is immediate (by design -- most drops are
    transient). Against a healthy mock, CONNECTION_LOST, RECONNECTING and
    CONNECTED therefore all land in one `drain_events()` batch, and the
    observable state never leaves CONNECTED. Dropping the listener as well
    makes that first retry fail, so the state persists long enough to assert
    on; restoring the listener then lets a later attempt succeed.
    """
    app.config_manager.settings.reconnect_interval_s = 0.5
    app.config_manager.settings.reconnect_attempts = 5

    mock = MockNPort(MockConfig()).start()
    device = _connect(app, mock)
    port = mock.port
    mock.stop()                      # client dropped *and* listener removed

    assert _pump_until(
        app, lambda: device.connection is ConnectionState.RECONNECTING,
        timeout=5.0,
    )
    card = app._cards[device.id]
    assert card._connection._style == "fault"
    assert "Reconnect" in card._connection._label.cget("text")

    # Same port, so the next attempt lands somewhere real.
    with MockNPort(MockConfig(port=port)) as revived:
        assert revived.port == port
        assert _pump_until(app, lambda: device.is_connected, timeout=8.0)
        assert card._connection._style == "connected"


def test_scenario_g_exhaustion_leaves_error_state(app):
    # No drop_after_s: stopping the mock both drops the client and removes the
    # listener, so there is no race between observing CONNECTED and the drop.
    mock = MockNPort(MockConfig()).start()
    device = _connect(app, mock)
    mock.stop()

    assert _pump_until(
        app, lambda: device.connection is ConnectionState.ERROR, timeout=12.0
    )
    card = app._cards[device.id]
    assert card._connection._style == "fault"
    assert device.last_error


def test_no_temperature_shown_after_a_drop(app):
    """A stale reading on a dead device would be worse than none.

    The mock is stopped rather than just dropping its client: leaving the
    listener up lets the worker reconnect within milliseconds and resume
    streaming, so the cleared reading would be repopulated before it could be
    observed.
    """
    mock = MockNPort(MockConfig(temp_start=50.0, temp_drift=0.0)).start()
    device = _connect(app, mock)
    assert _pump_until(app, lambda: device.latest_temperature is not None)
    mock.stop()
    assert _pump_until(app, lambda: device.latest_temperature is None,
                       timeout=6.0)
    card = app._cards[device.id]
    assert theme.TEMP_PLACEHOLDER in card._temp._value.cget("text")


# ---------------------------------------------------------------------------
# Scenario H -- isolation
# ---------------------------------------------------------------------------


def test_scenario_h_one_bad_device_does_not_affect_others(app):
    """A device pointed at an unroutable address must not stall the rest."""
    with MockNPort(MockConfig(temp_start=42.0, temp_drift=0.0)) as mock:
        good = _connect(app, mock, index=0)

        bad = app.manager.devices[1]
        app.manager.set_ip(bad.id, "192.0.2.1")       # TEST-NET-1, never routes
        app.config_manager.settings.connection_timeout_s = 0.5
        app._connect_device(bad.id)

        # The good device keeps streaming while the bad one is timing out.
        started = time.monotonic()
        assert _pump_until(app, lambda: good.latest_temperature is not None,
                           timeout=4.0)
        assert good.is_connected
        # And the UI stayed responsive throughout.
        assert time.monotonic() - started < 4.0


# ---------------------------------------------------------------------------
# Scenario I -- removal
# ---------------------------------------------------------------------------


def test_scenario_i_removing_connected_device_closes_socket(app, tmp_path):
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        device_id = device.id

        app._remove_device(device_id)
        _pump(app)

        assert device_id not in app._cards
        assert device_id not in app.manager._workers
        assert app.manager.get(device_id) is None
        assert len(app.manager.devices) == 2

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and mock.has_client:
            time.sleep(0.05)
        assert not mock.has_client, "socket left open after removal"

        app.config_manager.flush()
        reloaded = ConfigManager(tmp_path / "config.json")
        reloaded.load()
        assert len(reloaded.devices) == 2


def test_removal_leaves_no_orphan_thread(app):
    import threading
    with MockNPort(MockConfig()) as mock:
        device = _connect(app, mock)
        before = {t.name for t in threading.enumerate()}
        app._remove_device(device.id)
        _pump(app)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            live = {t.name for t in threading.enumerate() if "emcc-conn" in t.name}
            if not live:
                break
            time.sleep(0.05)
        assert not [t for t in threading.enumerate() if "emcc-conn" in t.name]


def test_first_card_keeps_flush_top_margin_after_removal(app):
    """Removing the top card must not leave the new first card indented."""
    first_id = app.manager.devices[0].id
    app._remove_device(first_id)
    _pump(app)
    new_first = app._cards[app._order[0]]
    assert new_first.pack_info()["pady"] in (0, "0", (0, 0))


# ---------------------------------------------------------------------------
# Add device
# ---------------------------------------------------------------------------


def test_add_device_appends_card_and_persists(app, tmp_path):
    app._add_device()
    _pump(app)
    assert len(app._cards) == 4
    assert app.manager.devices[-1].name == "Camera 4"
    app.config_manager.flush()
    reloaded = ConfigManager(tmp_path / "config.json")
    reloaded.load()
    assert len(reloaded.devices) == 4


def test_added_card_labels_are_aligned(app):
    """Regression: added cards used to top-jam their labels."""
    app._add_device()
    _pump(app, 6)
    app.update_idletasks()
    card = app._cards[app._order[-1]]
    offsets = {round(s.group.winfo_y(), 1) for s in card._sections}
    assert len(offsets) == 1, f"labels not aligned: {offsets}"
    assert next(iter(offsets)) > 5, "labels are jammed to the top"


def test_adding_does_not_rebuild_existing_cards(app):
    before = {i: id(c) for i, c in app._cards.items()}
    app._add_device()
    _pump(app)
    for device_id, identity in before.items():
        assert id(app._cards[device_id]) == identity, "existing card was rebuilt"


def test_no_device_cap(app):
    for _ in range(30):
        app._add_device()
    _pump(app, 2)
    assert len(app.manager.devices) == 33


# ---------------------------------------------------------------------------
# Scenario J -- shutdown
# ---------------------------------------------------------------------------


def test_scenario_j_shutdown_closes_everything(app, tmp_path):
    mocks = [MockNPort(MockConfig()).start() for _ in range(2)]
    try:
        for index, mock in enumerate(mocks):
            device = app.manager.devices[index]
            app.config_manager.settings.tcp_port = mock.port
            app.manager.set_ip(device.id, "127.0.0.1")
            app._connect_device(device.id)
            assert _pump_until(app, lambda d=device: d.is_connected)

        started = time.monotonic()
        app.shutdown()
        elapsed = time.monotonic() - started

        assert elapsed < 6.0, f"shutdown took {elapsed:.1f}s"
        assert app.manager._workers == {}

        for mock in mocks:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and mock.has_client:
                time.sleep(0.05)
            assert not mock.has_client

        reloaded = ConfigManager(tmp_path / "config.json")
        reloaded.load()
        assert len(reloaded.devices) == 3
    finally:
        for mock in mocks:
            mock.stop()


def test_shutdown_is_idempotent(app):
    app.shutdown()
    app.shutdown()      # must not raise


def test_a_raising_log_call_still_flushes_the_handlers(app, monkeypatch):
    """F7: `shutdown_logging()` must not share a `try` with the log line.

    A handler can raise while the app is going down -- closed stream, full
    disk, rotation mid-roll. When it did, `logging.shutdown()` was skipped and
    nothing was flushed, so the run that failed at shutdown was exactly the
    run whose log tail never reached disk.

    Covers BOTH guards, because a raising `logger.info` hits both sites: the
    "shutdown requested" line on `shutdown()`'s first statement and the
    "shutdown complete" line beside `shutdown_logging()`. Reaching the
    assertion at all requires the first to be guarded; the assertion passing
    requires the second not to share its `try`.

    Dies on: putting `logger.info(...)` and `shutdown_logging()` back in one
    `try` block, AND separately on un-guarding the "shutdown requested" line
    -- two distinct mutations, two distinct failures.
    """
    called = []
    monkeypatch.setattr(app_module, "shutdown_logging",
                        lambda: called.append(True))

    def boom(*args, **kwargs):
        raise OSError("handler is closed")

    monkeypatch.setattr(app_module.logger, "info", boom)

    app.shutdown()      # must not raise

    assert called == [True], (
        "shutdown_logging() was skipped because the log call raised")


def test_wm_delete_window_is_bound_to_shutdown(app):
    """Alt+F4 must not bypass teardown."""
    handler = app.protocol("WM_DELETE_WINDOW")
    assert handler, "WM_DELETE_WINDOW is not bound"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_ip_does_not_start_a_worker(app):
    device = app.manager.devices[0]
    app.manager.set_ip(device.id, "999.1.1.1")
    app._connect_device(device.id)
    _pump(app)
    assert app.manager._workers == {}
    assert device.connection is ConnectionState.DISCONNECTED


def test_blank_ip_caption_tells_the_operator(app):
    device = app.manager.devices[0]
    app.manager.set_ip(device.id, "")
    app._render_device(device.id)
    card = app._cards[device.id]
    assert "No IP" in card._connection._caption.cget("text")


# ---------------------------------------------------------------------------
# Progressive startup
# ---------------------------------------------------------------------------


def test_large_list_paints_before_all_cards_are_built(tmp_path):
    """Startup must not block on building every card.

    Building 25 cards up front measured 11.1s of blank window. Only enough to
    fill the viewport are built synchronously; the rest stream in on a timer.

    Note it boots 25 devices and so constructs the *above-threshold* chrome
    state -- deliberately, and it asserts nothing whatever about the hint or
    the scrollbar. That belongs to
    `test_chrome_is_correct_when_booted_at_the_threshold`; this test is about
    card streaming. Do not add chrome assertions here, and do not read its
    silence on chrome as coverage of it.
    """
    import json
    from emcc.app import INITIAL_CARDS

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "settings": {"tcp_port": 4001},
        "devices": [{"device_name": f"Camera {i+1}", "ip_address": ""}
                    for i in range(25)],
    }))
    config = ConfigManager(path)
    config.load()

    application = make_app(config)
    try:
        # Idle tasks only: layout and repaint, without running after() timers.
        application.update_idletasks()
        assert len(application._cards) == INITIAL_CARDS
        assert len(application._pending_devices) == 25 - INITIAL_CARDS

        # Drain the stream.
        guard = 0
        while application._pending_devices and guard < 200:
            application._build_remaining()
            application.update_idletasks()
            guard += 1
        assert len(application._cards) == 25
    finally:
        application.shutdown()


def test_streamed_cards_are_aligned(tmp_path):
    """Cards built from the cached offset must not be top-jammed."""
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "settings": {"tcp_port": 4001},
        "devices": [{"device_name": f"Camera {i+1}", "ip_address": ""}
                    for i in range(9)],
    }))
    config = ConfigManager(path)
    config.load()
    application = make_app(config)
    try:
        application.update_idletasks()
        guard = 0
        while application._pending_devices and guard < 100:
            application._build_remaining()
            application.update_idletasks()
            guard += 1
        application.update_idletasks()
        last = application._cards[application._order[-1]]
        offsets = {round(s.group.winfo_y(), 1) for s in last._sections}
        assert len(offsets) == 1, f"streamed card misaligned: {offsets}"
        assert next(iter(offsets)) > 5, "streamed card labels jammed to the top"
    finally:
        application.shutdown()


def test_build_remaining_stops_on_shutdown(app):
    app._pending_devices = list(app.manager.devices)
    app._shutting_down = True
    app._build_remaining()
    assert app._pending_devices == []


# ---------------------------------------------------------------------------
# Thermal lockout
# ---------------------------------------------------------------------------


def test_thermal_lockout_aborts_a_running_clean(app):
    """The AdapterController sends `stop` to both boards on lockout, so a
    Clean is already dead; the button must not sit green for its timeout."""
    with MockNPort(MockConfig(clean_duration_s=99, temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        app._clean_device(device.id)
        _pump(app)
        assert device.clean_active is True

        mock.push("PZT overheat detected. Entering cooldown lockout")
        assert _pump_until(app, lambda: not device.clean_active, timeout=4.0)
        assert device.thermal_lockout is True
        assert device.auto_enabled is False


def test_thermal_lockout_clears(app):
    with MockNPort(MockConfig(temp_interval_s=99)) as mock:
        device = _connect(app, mock)
        mock.push("PZT overheat detected. Entering cooldown lockout")
        assert _pump_until(app, lambda: device.thermal_lockout)
        mock.push("PZT temperature normalized. Re-enabling system.")
        assert _pump_until(app, lambda: not device.thermal_lockout)


# ---------------------------------------------------------------------------
# Banner wordmark
# ---------------------------------------------------------------------------


def test_logo_is_present_and_sized_from_the_theme(app):
    from emcc import icons

    assert app._logo is not None
    logo = icons.load_logo(theme.BANNER_LOGO_H)
    assert logo is not None, "logo asset failed to load"
    width, height = logo.cget("size")
    assert height == theme.BANNER_LOGO_H
    # Aspect comes from the alpha-cropped mark, not the padded canvas.
    assert 2.5 < width / height < 3.4, f"unexpected aspect {width / height:.2f}"


def test_logo_is_truly_centred_not_the_gap_midpoint(app):
    """The wordmark must not drift when the caption text changes length.

    The gap midpoint between the heading block and the legend moves as the
    device count changes ("1 device configured" vs "25 devices configured"),
    so the logo is placed at the true centre of the row instead.
    """
    from emcc.widgets.canvas_util import scaling

    _pump(app, 6)
    app.update_idletasks()
    factor = scaling(app)
    row = app._logo.master

    def centre_of_logo() -> float:
        return (app._logo.winfo_x() + app._logo.winfo_width() / 2) / factor

    before = centre_of_logo()
    row_centre = row.winfo_width() / factor / 2
    assert abs(before - row_centre) < 2.0, "logo is not at the row's true centre"

    # Add devices: the caption grows, so a midpoint-anchored logo would move.
    for _ in range(6):
        app._add_device()
    _pump(app, 6)
    app.update_idletasks()
    assert abs(centre_of_logo() - before) < 2.0, "logo drifted when devices changed"


def test_logo_does_not_collide_with_its_neighbours(app):
    """Clear space either side, even at the minimum window width."""
    from emcc.widgets.canvas_util import scaling

    app.geometry(f"{theme.WINDOW_MIN_W}x{theme.WINDOW_MIN_H}")
    _pump(app, 8)
    app.update_idletasks()
    factor = scaling(app)

    logo = app._logo
    row = logo.master
    siblings = [w for w in row.winfo_children() if w is not logo]
    logo_left = logo.winfo_x() / factor
    logo_right = (logo.winfo_x() + logo.winfo_width()) / factor

    for widget in siblings:
        left = widget.winfo_x() / factor
        right = (widget.winfo_x() + widget.winfo_width()) / factor
        overlaps = logo_left < right and left < logo_right
        assert not overlaps, "logo overlaps a banner sibling at minimum width"


def test_missing_logo_asset_does_not_break_startup(monkeypatch, tmp_path):
    """A missing asset should cost the banner its logo, not the app."""
    from emcc import icons

    icons.clear_cache()
    monkeypatch.setattr(icons, "LOGO_FILE", tmp_path / "nope.png")
    assert icons.load_logo(40) is None

    config = ConfigManager(tmp_path / "config.json")
    config.load()
    application = make_app(config)
    try:
        _pump(application, 4)
        assert len(application._cards) == 3     # app is fully functional
    finally:
        application.shutdown()
        icons.clear_cache()


# ---------------------------------------------------------------------------
# Sub-header chrome: the scroll hint, the scrollbar and the caption
#
# ADDITIONS, not modifications -- no existing assertion is touched by these.
#
# `_refresh_chrome` mixed four concerns and none of them was asserted anywhere
# when these tests were written. Slice 4 split it -- hint and scrollbar to
# ListView, caption wording to the active view placed by SubHeader, counts to
# title_bar -- and these tests survived the split unchanged because they assert
# the behaviour rather than where it lives. The concerns were:
# hint text, hint visibility (with its re-pack guard), scrollbar visibility,
# and the sub-header caption. The refactor splits those three ways -- hint and
# scrollbar to `ListView`, caption to `SubHeader`, counts to `title_bar` -- so
# without these the split would be judged by an image comparison.
#
# Each asserts the *observable* result rather than whoever computes it, so it
# holds before the split and after it. A guard test rewritten during the slice
# it guards is worthless.
#
# Every test names the mutation it must die on, for the Tester to apply.
# ---------------------------------------------------------------------------


def test_scroll_hint_text_tracks_the_device_count(app):
    """Dies on: replacing the f-string with a fixed string, or dropping the
    `self._hint.configure(...)` call from `_refresh_chrome`."""
    app._add_device()
    _pump(app, 6)
    total = len(app.manager.devices)
    assert app._hint.cget("text") == f"Scroll to view all {total} devices"


def test_scroll_hint_appears_only_at_the_threshold(app):
    """The hint is for a list that actually scrolls.

    Dies on: inverting `total >= theme.SCROLL_THRESHOLD` to `>`, or deleting
    the `pack_forget()` branch so the hint never goes away again.
    """
    assert len(app.manager.devices) < theme.SCROLL_THRESHOLD
    _pump(app, 6)
    assert not app._hint.winfo_ismapped()

    while len(app.manager.devices) < theme.SCROLL_THRESHOLD:
        app._add_device()
    _pump(app, 6)
    assert app._hint.winfo_ismapped()

    app._remove_device(app.manager.devices[-1].id)
    _pump(app, 6)
    assert not app._hint.winfo_ismapped()


def test_scroll_hint_is_not_repacked_on_every_refresh(app, monkeypatch):
    """The `_hint_shown` guard, which nothing else asserts.

    Re-`pack`ing an already-packed widget re-runs its geometry. `_refresh_chrome`
    runs on every device change and every pump tick that changed something, so
    the no-op case has to be a genuine no-op. The failure is a rendering
    artefact rather than an exception, which is why the guard needs a test to
    explain it -- otherwise a future reader deletes it as redundant.

    Dies on: removing `if scrolling != self._hint_shown:` so `pack()` runs on
    every refresh. **That guard moved to `ListView.on_device_count_changed`
    in slice 4** -- the rules belong to the view that owns the widget. The
    test still drives it through `app._refresh_chrome()`, which is the
    behaviour rather than the location, so it needed no change beyond this
    note.
    """
    while len(app.manager.devices) < theme.SCROLL_THRESHOLD:
        app._add_device()
    _pump(app, 6)
    assert app._hint.winfo_ismapped(), "hint should be shown at the threshold"

    packs = []
    original = app._hint.pack

    def counting_pack(*args, **kwargs):
        packs.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app._hint, "pack", counting_pack)
    app._refresh_chrome()
    app._refresh_chrome()
    assert packs == [], "already shown -- the guard must suppress re-packing"


def test_scrollbar_appears_only_at_the_threshold(app):
    """The design shows the scrollbar only once the list scrolls.

    Dies on: deleting the `_set_scrollbar_visible(scrolling)` call, which
    slice 4 moved into `ListView.on_device_count_changed`.

    It does **not** catch removal of that method's early-return guard, and an
    earlier version of this docstring claimed it did. Measured: with the guard
    removed the scrollbar stays gridded, so `grid_info()` is unchanged and
    this test passes while issuing three `grid()` calls across three
    refreshes. The claim described what the test was meant to catch rather
    than what its assertion can see -- see
    `test_scrollbar_is_not_regridded_on_every_refresh`, which does catch it.

    **This test now carries two declared mutations, not one.** It is also the
    only test that catches `_scrollbar_visible` initialised `False` -- the
    boundary test booting at the threshold cannot see that one, because the
    early return leaves the bar correctly gridded there. So a failure here is
    two signals, and a future "fix" to this test that weakens either
    assertion drops both mutations at once. Correlated coverage, deliberately
    recorded: do not read one red test as one escaped mutation.
    """
    bar = getattr(app._list, "_scrollbar", None)
    assert bar is not None, "CTkScrollableFrame has no _scrollbar to assert on"

    # Mirrors the precondition its sibling hint test already carries. Without
    # it, growing `DEFAULT_DEVICES` past the threshold would make the next
    # assertion *silently invert* -- claiming "hidden below the threshold"
    # while sitting above it, with the mutations this test catches walking
    # free and the test still green. With it, the test refuses to run.
    assert len(app.manager.devices) < theme.SCROLL_THRESHOLD

    _pump(app, 6)
    assert not bar.grid_info(), "hidden below the threshold"

    while len(app.manager.devices) < theme.SCROLL_THRESHOLD:
        app._add_device()
    _pump(app, 6)
    assert bar.grid_info(), "shown at the threshold"


def test_scrollbar_is_not_regridded_on_every_refresh(app, monkeypatch):
    """The early-return guard in `_set_scrollbar_visible`.

    Its rationale is identical to the hint guard's: re-applying `grid()` to an
    already-gridded widget re-runs its geometry and triggers
    `CTkScrollbar._draw`. `_refresh_chrome` runs on every device change, so the
    no-op case has to be a genuine no-op.

    This needs a call counter for the same reason the hint guard did: the
    guard's only effect is *how often* geometry recomputes. Nothing in widget
    state changes, so no state assertion can see it -- which is exactly why
    the sibling visibility test does not catch this mutation despite once
    claiming to.

    Dies on: removing `if visible == self._scrollbar_visible: return`,
    which moved to `ListView._set_scrollbar_visible` in slice 4 along with the
    cached flag. Still driven through `app._refresh_chrome()`.
    """
    while len(app.manager.devices) < theme.SCROLL_THRESHOLD:
        app._add_device()
    _pump(app, 6)

    bar = getattr(app._list, "_scrollbar", None)
    assert bar is not None, "CTkScrollableFrame has no _scrollbar to assert on"
    assert bar.grid_info(), "scrollbar should be shown at the threshold"

    grids = []
    original = bar.grid

    def counting_grid(*args, **kwargs):
        grids.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bar, "grid", counting_grid)
    app._refresh_chrome()
    app._refresh_chrome()
    assert grids == [], "already shown -- the guard must suppress re-gridding"


def _boot_with(tmp_path, count: int):
    """Construct an `App` with exactly `count` devices, from a written config.

    The `app` fixture cannot serve the test below. Its device count is
    whatever `DEFAULT_DEVICES` holds, and the only way to change it is
    `_add_device` -- which triggers the very refresh that hides the bug that
    test exists to catch.
    """
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "settings": {"tcp_port": 4001},
        "devices": [{"device_name": f"Camera {i+1}", "ip_address": ""}
                    for i in range(count)],
    }))
    config = ConfigManager(path)
    config.load()
    return make_app(config)


def test_chrome_is_correct_when_booted_at_the_threshold(tmp_path):
    """Booting *at* the threshold, which nothing else in the suite does.

    Every other threshold test *crosses* into the scrolling state: the fixture
    boots at `DEFAULT_DEVICES` and adds devices until it is there. That hides a
    class of bug outright. The first `_refresh_chrome` runs with
    `scrolling=False`, so `scrolling != self._hint_shown` is true for the wrong
    reason, the `else` branch runs, and a wrongly-initialised flag is
    **self-corrected on the way past**. The later add then packs the hint
    normally and every assertion downstream passes.

    Booting at the threshold gives exactly one refresh, with `scrolling=True`
    and nothing before it to launder the flag: the guard is false, the branch
    is skipped, and the hint is never packed at all.

    Dies on: `self._hint_shown = True` in the initialiser (`app.py:314`).

    `update_idletasks()` is here for the hint, not the bar -- measured,
    `winfo_ismapped()` is 0 before any pump and 1 after idle tasks alone, while
    `grid_info()` is a geometry-manager registry read that needs no pump at
    all. Do not remove it as ceremony.
    """
    application = _boot_with(tmp_path, theme.SCROLL_THRESHOLD)
    try:
        application.update_idletasks()
        bar = getattr(application._list, "_scrollbar", None)
        assert bar is not None, "CTkScrollableFrame has no _scrollbar"
        assert application._hint.winfo_ismapped(), (
            "hint must be packed when the app boots at the threshold"
        )
        assert bool(bar.grid_info()), (
            "scrollbar must be gridded when the app boots at the threshold"
        )
    finally:
        application.shutdown()


def test_subheader_caption_counts_and_pluralises(app):
    """Dies on: dropping the `'' if total == 1 else 's'` conditional, or the
    `self._caption.configure(...)` call."""
    _pump(app, 6)
    assert app._caption.cget("text").startswith("3 devices configured")

    while len(app.manager.devices) > 1:
        app._remove_device(app.manager.devices[-1].id)
    _pump(app, 6)
    assert app._caption.cget("text").startswith("1 device configured")


# ---------------------------------------------------------------------------
# The App facade
#
# `App` is not being replaced; the four-slice modularisation moves code out
# from under it while every name that tests and tools already reach stays
# put. `_FACADE` is that contract, and these tests are what stop it drifting
# silently -- which it did three times in one day, with 19, 24 and 26 all in
# circulation and nothing ever failing.
# ---------------------------------------------------------------------------


#: Regex matches that are not attribute accesses at all. `app.py:314` appears
#: in docstrings in this very file, and `app.X` appears in prose below as a
#: placeholder -- neither `py` nor `X` is an attribute of anything. Explicit
#: rather than filtered by heuristic, because the next person to cite a line
#: of `app.py` in a docstring will hit exactly this.
_NOT_ATTRIBUTES = {"py", "X"}

#: Names that *are* real attribute accesses on `app` and must nonetheless
#: stay out of `_FACADE`, because a test accesses them precisely to assert
#: they raise. Kept separate from `_NOT_ATTRIBUTES` deliberately: the two are
#: different claims -- "this was never an attribute" versus "this is an
#: attribute lookup that must fail" -- and a genuinely missing facade name
#: put in either set would be silently excused. If you are about to add a
#: name here, the question to answer is whether a test asserts it *raises*.
_DELIBERATELY_ABSENT = {"_definitely_not_a_facade_name"}


def _names_reached_from_outside():
    """Every `app.X` / `application.X` name in `tests/` and the visual pass.

    A regex over source, which is the same blind instrument that once counted
    41 tests against pytest's 49. It is acceptable **here and not there**
    because its blindness is one-directional: a false match can only ever
    demand a name be *added* to `_FACADE`, never allow one to be dropped. It
    cannot produce a false all-clear. That property, not the regex's accuracy,
    is why this test can be trusted -- and it is not self-evident, so do not
    "improve" this into something that can under-count.

    `tools/visual_pass.py` is load-bearing in this list rather than
    thoroughness: `_confirm_remove` and `_scroll_to_end` are reached from **no
    test at all**, only from the visual pass, so dropping the tool would
    silently shrink the required set and nothing would fail.

    Note also that reached-by-name and *exercised* are different questions,
    and this instrument answers only the first. Of those same two names,
    `_scroll_to_end` runs constantly via `after_idle` on the add path, while
    `_confirm_remove` is invoked by nothing but the visual pass. Do not read a
    name's presence here as coverage of it.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    sources = sorted((root / "tests").glob("*.py"))
    sources.append(root / "tools" / "visual_pass.py")
    pattern = re.compile(r"\b(?:app|application)\.([A-Za-z_][A-Za-z0-9_]*)")

    names: set[str] = set()
    for source in sources:
        names |= set(pattern.findall(source.read_text(encoding="utf-8")))
    names -= _NOT_ATTRIBUTES | _DELIBERATELY_ABSENT
    # Names resolving on CTk never reach `App.__getattr__`, so the facade owes
    # nothing for them. Derived by lookup, not by a hand-kept list.
    return {n for n in names if not hasattr(ctk.CTk, n)}


def test_facade_covers_every_name_reached_from_outside():
    """`_FACADE` must not drift behind the suite that defines it.

    Subset, not equality: a stale extra entry in `_FACADE` is inert, whereas
    equality would fail every time an unrelated test was deleted.

    Dies on: dropping a name from `_FACADE`.

    It does **not** die on removing `tools/visual_pass.py` from the sources,
    and an earlier version of this docstring claimed it did. Measured: with
    the tool dropped, all seven facade tests still passed.

    The reason is worth stating precisely, because the guarantee is
    per-dimension and only one dimension was ever covered:

    - **name dimension -- safe.** Over-inclusion can only ever demand that a
      name be *added* to `_FACADE`. A false regex match cannot let a real name
      be dropped, so there is no false all-clear.
    - **source dimension -- blind.** Dropping a source only *shrinks*
      `reached`, and a smaller set is trivially still a subset. Nothing about
      the subset relation notices that a whole file stopped being read.

    So the property that makes this test trustworthy in the first dimension is
    exactly what blinds it in the second. Not a symmetry, and describing it as
    "cutting both ways" understates it: one direction is guaranteed, the other
    is unguarded until pinned separately.

    So the tool's presence is pinned separately below, by a name reachable
    from nowhere else.
    """
    reached = _names_reached_from_outside()

    # `_confirm_remove` is invoked only by `tools/visual_pass.py:238` -- no
    # test touches it. If it is absent from `reached`, the tool is no longer
    # being scanned, and the subset assertion below would not notice.
    assert "_confirm_remove" in reached, (
        "tools/visual_pass.py appears to have been dropped from the scan "
        "sources: _confirm_remove is reached from no test, so its absence "
        "here means the tool is not being read"
    )

    missing = sorted(reached - App._FACADE)
    assert not missing, (
        f"reached from outside but absent from App._FACADE: {missing}. Add "
        "them to _FACADE -- or, if one is a false regex match, to "
        "_NOT_ATTRIBUTES, or if a test asserts it raises, to "
        "_DELIBERATELY_ABSENT. With the reason, in all three cases."
    )


def test_every_facade_name_resolves_on_a_live_app(app):
    """The other direction: `_FACADE` must not promise what `App` lacks.

    This is what fails on a slice that moves a name out without leaving a
    property behind -- and it fails naming the attribute, rather than
    surfacing later as a Tcl error from whichever test happened to touch it
    first.
    """
    _pump(app, 4)
    unresolvable = sorted(n for n in App._FACADE if not hasattr(app, n))
    assert not unresolvable, f"in _FACADE but missing from App: {unresolvable}"


def test_facade_containers_keep_identity_and_write_through(app):
    """A facade attribute must be the object itself, not a fresh copy.

    Trivially true today, because these are plain attributes. It becomes
    load-bearing the moment a slice turns one into a property: a getter
    returning `dict(self._view.cards)` or `list(...)` would satisfy every
    existing assertion about *contents* while breaking `app._cards[id] = card`
    and every identity comparison, and nothing else in the suite looks.

    `_pending_devices` and `_shutting_down` are written from outside, so they
    need write-through and not merely read-through.
    """
    _pump(app, 4)

    assert app._cards is app._cards
    assert app._order is app._order
    assert app._hint is app._hint

    order = app._order
    order.append("facade-probe")
    assert "facade-probe" in app._order, "_order is a copy, not the list itself"
    app._order.remove("facade-probe")

    app._pending_devices = []
    assert app._pending_devices == []
    previous = app._shutting_down
    app._shutting_down = True
    assert app._shutting_down is True
    app._shutting_down = previous


def test_lost_facade_attribute_names_app_not_the_tcl_interpreter(app, monkeypatch):
    """The whole point of the `__getattr__` override.

    Without it, `tkinter.Tk.__getattr__` delegates to the interpreter and a
    missing `_hint` raises an error naming `_tkinter.tkapp` -- which never
    mentions `App` and gives a reader no reason to suspect the
    modularisation. Measured on a bare `CTk`, not inferred.

    **The mechanism changed at slice 2** and the reason is the point of the
    test. `_hint` used to be an instance attribute, so this deleted it from
    `app.__dict__`; it is now a facade *property* over `ListView`, so it is
    not in the instance dict at all and that deletion raised `KeyError`.
    Removing the property from the class is also the realistic failure now:
    post-extraction, the way `_hint` gets lost is a slice moving it out and
    forgetting to leave a property behind.

    Dies on: deleting `App.__getattr__`, or dropping either marker from its
    message.
    """
    _pump(app, 4)
    monkeypatch.delattr(type(app), "_hint")

    with pytest.raises(AttributeError) as excinfo:
        app._hint
    message = str(excinfo.value)
    assert "'App'" in message, f"must name App, got: {message}"
    assert "_FACADE" in message, "must point at the facade contract"
    assert "tkapp" not in message, "must not surface the Tcl interpreter"


def test_non_facade_names_keep_tk_delegation(app):
    """The override must intercept facade names only.

    CTk internals reach real Tcl methods through `Tk.__getattr__`, so
    intercepting every unknown name would break the toolkit rather than
    protect the facade.

    Dies on: raising for any unknown name instead of only `_FACADE` ones.
    """
    for tcl_name in ("call", "eval", "createcommand"):
        assert callable(getattr(app, tcl_name)), f"{tcl_name} no longer delegates"

    with pytest.raises(AttributeError) as excinfo:
        app._definitely_not_a_facade_name
    assert "_FACADE" not in str(excinfo.value)


def test_facade_properties_raise_runtimeerror_not_attributeerror(app):
    """`VIEW_CONTRACT.md:151` -- the backstop, asserted rather than assumed.

    A property whose getter raises `AttributeError` is swallowed by Python's
    attribute machinery and routed to `__getattr__`, where it is reported as a
    *missing* attribute -- so an unwired collaborator gets blamed on the
    modularisation having lost a name. Facade properties must raise
    `RuntimeError`, which propagates.

    **Live since slice 2.** `_cards`, `_order`, `_hint`, `_list`,
    `_add_button` and `_pending_devices` became facade properties over
    `ListView`, so the loop below now exercises six real descriptors against a
    bare instance built with `__new__`. It was vacuous before that, and a
    companion test asserted its own vacuity so a passing-because-empty run
    could not be mistaken for coverage; that companion was written to fail
    here and has been retired.
    """
    properties = sorted(
        name for name in App._FACADE
        if isinstance(getattr(type(app), name, None), property)
    )
    if not properties:
        return

    for name in properties:
        descriptor = getattr(type(app), name)
        probe = type(app).__new__(type(app))      # no __init__, no collaborators
        with pytest.raises(RuntimeError):
            descriptor.fget(probe)


# ---------------------------------------------------------------------------
# The view host and the toggle
# ---------------------------------------------------------------------------


def test_the_chrome_follows_the_active_view(app):
    """Switching views must re-compose the heading and the caption.

    This is the test the mount commit lacked. The shell owns the heading,
    caption and counts, and composes each from the **active view** -- so
    changing which view is active changes all three. But the host does not
    call back into the shell, and only `ListView` reports movement through
    `on_changed`, so `_switch_view` has to refresh the chrome itself.

    Without that call the sub-header keeps the *previous* view's text. That
    shipped: on the Dashboard the caption read "3 devices configured - click
    Connect to establish TCP connection" and the heading read "DEVICE
    CONTROLLERS", and the whole suite passed over it because nothing asserted
    this.

    Dies on: dropping `self._refresh_chrome()` from `_switch_view`.
    """
    _pump(app, 4)
    assert app.views.active_name == "list"
    assert app._sub.heading.cget("text") == fonts.tracked("DEVICE CONTROLLERS", 0.12)
    assert app._caption.cget("text").startswith("3 devices configured")

    app._switch_view("dashboard")
    _pump(app, 4)
    assert app.views.active_name == "dashboard"
    assert app._sub.heading.cget("text") == fonts.tracked("DASHBOARD OVERVIEW", 0.12), (
        "the heading still shows the previous view's text"
    )
    assert not app._caption.cget("text").startswith("3 devices configured"), (
        "the caption still shows the list view's wording"
    )

    app._switch_view("list")
    _pump(app, 4)
    assert app._sub.heading.cget("text") == fonts.tracked("DEVICE CONTROLLERS", 0.12)
    assert app._caption.cget("text").startswith("3 devices configured")


def test_the_heading_is_tracked_exactly_once(app):
    """The view returns raw words; the shell applies the letter-spacing.

    A view that pre-tracked its own heading would be tracked twice here and
    render with doubled hair spaces -- visible, and nothing else would catch
    it. Asserted against what `fonts.tracked` actually inserts rather than
    against a typed codepoint, because `tracked` uses `max(1, ...)` spacers
    and a literal is easy to get wrong.

    Dies on: tracking in the view instead of the shell, or dropping the
    `fonts.tracked` call from `SubHeader.set_heading`.
    """
    _pump(app, 4)
    once = fonts.tracked("DEVICE CONTROLLERS", 0.12)
    twice = fonts.tracked(once, 0.12)
    assert once != twice, "fonts.tracked is idempotent, so this test proves nothing"

    rendered = app._sub.heading.cget("text")
    assert rendered == once
    assert rendered != twice
    assert app.views.active.heading == "DEVICE CONTROLLERS", (
        "the view must return raw words, not a tracked string"
    )


def test_the_host_refreshes_the_chrome_on_every_show():
    """The invariant, pinned where it lives rather than where it is called.

    `test_the_chrome_follows_the_active_view` covers the *switch* path and
    structurally cannot cover the *boot* path -- it asserts the boot heading
    is "DEVICE CONTROLLERS", which is what a missing boot refresh also
    produces. So the boot site went unfixed by the commit titled "fix the
    chrome not following the view", 70 lines from a docstring saying the
    refresh was not optional.

    Asserting on `ViewHost.show` instead covers every call site there is,
    including ones nobody has written yet. Needs no Tk: the fake view is the
    smallest thing the host will accept.

    Dies on: dropping `self._on_shown()` from `ViewHost.show`.
    """
    from emcc.views.host import ViewHost

    class _Widget:
        def pack(self, **kwargs): pass
        def pack_forget(self): pass

    class _View:
        def __init__(self, name):
            self.name = name
            self.widget = _Widget()
            self.padding = {}
        def set_devices(self, devices): pass
        def show(self): pass
        def hide(self): pass
        def shutdown(self): pass

    shown: list[int] = []
    host = ViewHost(object(), None, on_shown=lambda: shown.append(1))
    host.register("first", lambda parent, ctx: _View("first"))
    host.register("second", lambda parent, ctx: _View("second"))

    host.show("first", [])
    assert shown == [1], "the FIRST show -- the boot path -- must refresh too"

    host.show("second", [])
    assert shown == [1, 1], "a switch must refresh"

    host.show("first", [])
    assert shown == [1, 1, 1], "switching back must refresh"


def test_the_host_refresh_is_not_contained():
    """A failing chrome refresh must propagate, not be swallowed.

    `_call` contains view exceptions because the pump delivers to views that
    are *not* active and a faulty Dashboard must not freeze the List. The
    shell's own callback is not foreign code, and the chrome path is the one
    that surfaced a missing `_hint_shown` during slice 2 while the identical
    fault inside containment was invisible.

    Dies on: routing `_on_shown` through `_call`.
    """
    from emcc.views.host import ViewHost

    class _Widget:
        def pack(self, **kwargs): pass
        def pack_forget(self): pass

    class _View:
        name = "only"
        def __init__(self):
            self.widget = _Widget()
            self.padding = {}
        def set_devices(self, devices): pass
        def show(self): pass
        def hide(self): pass
        def shutdown(self): pass

    def explode():
        raise RuntimeError("chrome refresh failed")

    host = ViewHost(object(), None, on_shown=explode)
    host.register("only", lambda parent, ctx: _View())

    with pytest.raises(RuntimeError, match="chrome refresh failed"):
        host.show("only", [])

    assert host.incidents == (), (
        "the failure was contained and recorded rather than raised -- the "
        "chrome path must stay loud"
    )


def _reseeds(app):
    """Record which views get `set_devices`, without disturbing the host."""
    seen: list[str] = []
    original = app.views._call

    def watched(name, method, *args):
        if method == "set_devices":
            seen.append(name)
        return original(name, method, *args)

    app.views._call = watched
    return seen, (lambda: setattr(app.views, "_call", original))


def test_an_idle_switch_does_not_reseed(app):
    """Switching with nothing changed must not rebuild anything.

    `_hide_active` used to dirty unconditionally, so every switch back ran a
    full `set_devices`. For `ListView` that is a teardown and progressive
    rebuild of every card -- measured at 3596-3626ms of blocked call at 25
    devices and 790-928ms at four, with the fleet idle. The comment above it
    described the intent ("anything arriving while hidden marks it dirty")
    while the code dirtied whether or not anything had arrived.

    Settling time is not quoted here either, and see `ViewHost._hide_active`
    for why: an earlier draft cited a settle figure that the measuring
    harness could not actually support.

    Dies on: `_hide_active` dirtying again.
    """
    _pump(app, 4)
    app._switch_view("dashboard")
    _pump(app, 4)

    seen, restore = _reseeds(app)
    try:
        app._switch_view("list")
        _pump(app, 4)
        app._switch_view("dashboard")
        _pump(app, 4)
        app._switch_view("list")
        _pump(app, 4)
    finally:
        restore()

    assert seen == [], f"reseeded {seen} across three idle switches"


def test_the_stale_view_is_the_one_reseeded_not_the_active_one(app):
    """The view that changed is not necessarily the view on screen.

    `_add_device` appends through `App._view()`, which is hardwired to the
    list view whatever is displayed. `note_device_set_changed` excluded the
    ACTIVE view, so with the Dashboard up it left the Dashboard -- the stale
    one -- clean, and dirtied the List, which had just been updated in place.

    Measured before the fix: manager 4 devices, list view 4 cards, Dashboard
    3, `_dirty == {"list"}`, and the Dashboard still at 3 after a round trip.
    Two faults from one wrong premise -- the Dashboard permanently stale, and
    a full `ListView` rebuild on the way back for a view that needed nothing.

    Dies on: excluding `self._active_name` instead of the named `mutated`
    view -- which is what it did until this test was written.
    """
    app._switch_view("dashboard")
    _pump(app, 12)
    dashboard = app.views._views["dashboard"]

    seen, restore = _reseeds(app)
    try:
        app._add_device()
        _pump(app, 12)
        app._switch_view("list")
        _pump(app, 12)
        app._switch_view("dashboard")
        _pump(app, 12)
    finally:
        restore()

    assert len(dashboard._devices) == len(app.manager.devices), (
        f"Dashboard holds {len(dashboard._devices)} of "
        f"{len(app.manager.devices)} devices: it was active when the device "
        f"was added, so it was the stale view -- and it was left clean"
    )
    assert "list" not in seen, (
        "the list view was rebuilt despite having been updated in place"
    )


def test_a_device_added_while_hidden_still_reseeds(app):
    """The other half: a view that missed a change must be reseeded.

    Dropping the dirty-on-hide without this would be a regression rather than
    a fix. Measured before the change: with the list active, `_add_device`
    left the hidden Dashboard's `_devices` at the old count -- nothing informs
    it, because the shell mutates the list view directly through `new_card`
    and the host's own `add_device` routing has no callers. The reseed on
    switch-back was the only thing correcting it.

    So the dirty now comes from the change rather than from the hide.

    Dies on: dropping `note_device_set_changed` from `_add_device`, or
    narrowing it to skip inactive views.
    """
    _pump(app, 4)
    app._switch_view("dashboard")
    _pump(app, 4)
    dashboard = app.views.active
    before = len(dashboard._devices)

    app._switch_view("list")
    _pump(app, 4)
    app._add_device()
    _pump(app, 4)

    assert "dashboard" in app.views._dirty, (
        "the hidden view was not told the device set changed, so it will "
        "render a stale grid when shown"
    )

    seen, restore = _reseeds(app)
    try:
        app._switch_view("dashboard")
        _pump(app, 4)
    finally:
        restore()

    assert seen == ["dashboard"], f"expected a dashboard reseed, got {seen}"
    assert len(dashboard._devices) == before + 1


def test_the_mutated_view_is_not_dirtied_by_its_own_change(app):
    """The view that received the change is already correct.

    Dirtying it too would reinstate the rebuild this fix removes -- the next
    switch away and back would reseed even though nothing was missed.

    Renamed from `test_the_active_view_is_not_dirtied_by_its_own_change`. The
    old name asserted the premise that turned out to be false: the mutated
    view and the active view are not the same thing, because `App._view()` is
    hardwired to the list view. Here they coincide -- the list IS active --
    which is why this passed while the Dashboard case was broken. See
    `test_the_stale_view_is_the_one_reseeded_not_the_active_one` for the case
    that separates them.

    Dies on: `note_device_set_changed` dirtying every view rather than
    excluding the one named by `mutated`.
    """
    _pump(app, 4)
    assert app.views.active_name == "list"

    app._add_device()
    _pump(app, 4)

    assert "list" not in app.views._dirty, (
        "the mutated view was dirtied by a change it received itself"
    )
