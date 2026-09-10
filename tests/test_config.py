"""ConfigManager tests: resilience, atomicity, and the device list."""

from __future__ import annotations

import json
import pathlib

import pytest

from emcc.backend.config_manager import (
    SCHEMA_VERSION,
    ConfigManager,
    DeviceConfig,
    Settings,
)


@pytest.fixture()
def config_path(tmp_path):
    return tmp_path / "config.json"


# ---------------------------------------------------------------------------
# Missing / fresh install
# ---------------------------------------------------------------------------


def test_missing_config_creates_defaults(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    assert config_path.exists()
    assert len(manager.devices) == 3
    assert [d.device_name for d in manager.devices] == [
        "Camera 1", "Camera 2", "Camera 3"
    ]
    # Seeded with the live bench systems.
    assert [d.ip_address for d in manager.devices] == [
        "192.168.2.250", "192.168.2.251", "192.168.2.253"
    ]


def test_default_port_is_nport_data_port(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.settings.tcp_port == 4001


def test_defaults_match_spec(config_path):
    settings = Settings()
    assert settings.temperature_warning_c == 65.0
    assert settings.connection_timeout_s == 3.0
    assert settings.clean_timeout_s == 10.0
    assert settings.reconnect_attempts == 3
    assert settings.reconnect_interval_s == 10.0


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_name_and_ip(config_path):
    first = ConfigManager(config_path)
    first.load()
    first.devices[0].device_name = "Left Nozzle"
    first.devices[0].ip_address = "192.168.2.99"
    assert first.save_now()

    second = ConfigManager(config_path)
    second.load()
    assert second.devices[0].device_name == "Left Nozzle"
    assert second.devices[0].ip_address == "192.168.2.99"


def test_ids_are_stable_across_reload(config_path):
    first = ConfigManager(config_path)
    first.load()
    ids = [d.id for d in first.devices]

    second = ConfigManager(config_path)
    second.load()
    assert [d.id for d in second.devices] == ids


def test_saved_file_has_version(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    data = json.loads(config_path.read_text())
    assert data["version"] == SCHEMA_VERSION


def test_transient_state_is_never_persisted(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    manager.save_now()
    entry = json.loads(config_path.read_text())["devices"][0]
    assert set(entry) == {"device_name", "ip_address", "id"}
    for forbidden in ("connection", "auto", "clean", "temperature", "error"):
        assert not any(forbidden in key for key in entry)


# ---------------------------------------------------------------------------
# Malformed / hostile input
# ---------------------------------------------------------------------------


def test_malformed_json_falls_back_and_quarantines(config_path):
    config_path.write_text("{ this is not json ")
    manager = ConfigManager(config_path)
    manager.load()
    assert len(manager.devices) == 3
    assert config_path.with_suffix(".json.corrupt").exists()


def test_truncated_json_does_not_crash(config_path):
    config_path.write_text('{"settings": {"tcp_port": 4001}, "devices": [{"dev')
    manager = ConfigManager(config_path)
    manager.load()
    assert len(manager.devices) == 3


def test_top_level_not_an_object(config_path):
    config_path.write_text("[1, 2, 3]")
    manager = ConfigManager(config_path)
    manager.load()
    assert len(manager.devices) == 3


def test_missing_keys_use_defaults(config_path):
    config_path.write_text(json.dumps({"devices": [{"device_name": "Solo"}]}))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.settings.tcp_port == 4001
    assert len(manager.devices) == 1
    assert manager.devices[0].ip_address == ""


def test_invalid_setting_value_falls_back(config_path):
    config_path.write_text(json.dumps({
        "settings": {"tcp_port": "not a number", "clean_timeout_s": -5},
        "devices": [],
    }))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.settings.tcp_port == 4001       # bad type -> default
    assert manager.settings.clean_timeout_s == 10.0  # non-positive -> default


def test_unknown_settings_are_ignored(config_path):
    config_path.write_text(json.dumps({
        "settings": {"tcp_port": 4002, "future_option": True},
        "devices": [],
    }))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.settings.tcp_port == 4002


def test_older_schema_without_version_loads(config_path):
    config_path.write_text(json.dumps({
        "settings": {"tcp_port": 4001},
        "devices": [{"device_name": "Camera 1", "ip_address": "192.168.2.250"}],
    }))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.devices[0].device_name == "Camera 1"
    assert manager.devices[0].id          # generated on the fly


def test_device_entry_of_wrong_type_is_skipped(config_path):
    config_path.write_text(json.dumps({
        "devices": ["not an object", {"device_name": "Good"}],
    }))
    manager = ConfigManager(config_path)
    manager.load()
    assert [d.device_name for d in manager.devices] == ["Good"]


def test_nameless_device_gets_a_name(config_path):
    config_path.write_text(json.dumps({"devices": [{"ip_address": "1.2.3.4"}]}))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.devices[0].device_name == "Camera 1"


def test_duplicate_ids_are_reassigned(config_path):
    config_path.write_text(json.dumps({"devices": [
        {"device_name": "A", "id": "same"},
        {"device_name": "B", "id": "same"},
    ]}))
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.devices[0].id != manager.devices[1].id


def test_devices_not_a_list(config_path):
    config_path.write_text(json.dumps({"devices": {"oops": True}}))
    manager = ConfigManager(config_path)
    manager.load()
    assert len(manager.devices) == 3


# ---------------------------------------------------------------------------
# Add / remove / naming
# ---------------------------------------------------------------------------


def test_add_and_remove_device(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    device = DeviceConfig(device_name="Camera 4")
    manager.add_device(device)
    manager.flush()

    reloaded = ConfigManager(config_path)
    reloaded.load()
    assert len(reloaded.devices) == 4

    assert manager.remove_device(device.id)
    manager.flush()
    reloaded.load()
    assert len(reloaded.devices) == 3


def test_remove_unknown_id_is_a_noop(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    assert manager.remove_device("nope") is False


def test_next_name_avoids_collision_after_rename(config_path):
    """Renaming a card must not let the next default duplicate it."""
    manager = ConfigManager(config_path)
    manager.load()
    manager.devices[0].device_name = "Camera 4"   # user renamed card 1
    assert manager.next_device_name() == "Camera 5"


def test_next_name_is_case_insensitive(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    manager.devices[0].device_name = "camera 4"
    assert manager.next_device_name() == "Camera 5"


# ---------------------------------------------------------------------------
# Save behaviour
# ---------------------------------------------------------------------------


def test_debounced_save_collapses_bursts(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    config_path.unlink()
    for index in range(20):          # simulate typing
        manager.devices[0].device_name = f"Cam{index}"
        manager.request_save()
    assert not config_path.exists()  # nothing written yet
    manager.flush()
    assert json.loads(config_path.read_text())["devices"][0]["device_name"] == "Cam19"


def test_no_temp_files_left_behind(config_path):
    manager = ConfigManager(config_path)
    manager.load()
    manager.save_now()
    leftovers = [p.name for p in config_path.parent.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_failure_is_reported_not_raised(config_path, monkeypatch):
    manager = ConfigManager(config_path)
    manager.load()

    seen: list[Exception] = []
    manager.on_save_error = seen.append

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("emcc.backend.config_manager.tempfile.mkstemp", boom)
    for _ in range(3):
        assert manager.save_now() is False
    assert seen                      # escalated after repeated failure


def test_existing_config_is_intact_after_failed_save(config_path, monkeypatch):
    manager = ConfigManager(config_path)
    manager.load()
    original = config_path.read_text()

    monkeypatch.setattr(
        "emcc.backend.config_manager.tempfile.mkstemp",
        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
    )
    manager.devices[0].device_name = "Should not persist"
    assert manager.save_now() is False
    assert config_path.read_text() == original


# ---------------------------------------------------------------------------
# A quarantine that fails must not lead to the original being overwritten
# ---------------------------------------------------------------------------


@pytest.mark.allow_contained_exceptions
def test_a_failed_quarantine_disables_saving(config_path, monkeypatch):
    """The original bytes must still be on disk afterwards.

    This is the property that matters, and it is asserted on the file rather
    than on a flag or a log line: an unreadable config that could not be
    backed up is the **only** copy of the operator's device list, so writing
    defaults over it destroys it with nothing to recover from.

    Before the fix, `_quarantine` swallowed the `OSError` and `load` carried
    on to defaults; the next save -- any device edit, or the shutdown flush --
    replaced the file. The operator saw an application that had come up with
    three default devices and no indication anything had been lost.

    Marked `allow_contained_exceptions` because the product now *deliberately*
    logs the failed backup with `exc_info` at ERROR. That is the fix, not a
    latent bug: the exception is caught, reported, and acted on by refusing to
    save. The autouse guard cannot distinguish "caught and acted on" from
    "caught and swallowed", so the marker says which this is.

    Dies on: `_quarantine` swallowing the error again, or `save_now` writing
    while `_preserve_failed` is set.
    """
    original = '{ this is not json '
    config_path.write_text(original)

    def refuse_backup(*args, **kwargs):
        raise OSError(13, "Permission denied")

    # Only the .corrupt write fails; the real save path stays intact so the
    # test proves saving is *refused* rather than merely broken.
    monkeypatch.setattr(pathlib.Path, "write_text", refuse_backup)

    manager = ConfigManager(config_path)
    manager.load()
    monkeypatch.undo()

    assert len(manager.devices) == 3, "still falls back to defaults"
    assert not config_path.with_suffix(".json.corrupt").exists(), (
        "the backup was supposed to fail in this test"
    )

    assert manager.save_now() is False, "saving must be refused"
    assert config_path.read_text() == original, (
        "the unreadable original was overwritten -- this is the data loss the "
        "fix exists to prevent"
    )


@pytest.mark.allow_contained_exceptions
def test_a_failed_quarantine_reaches_the_operator(config_path, monkeypatch):
    """A log line is not surfacing; the callback is.

    Nothing in the suite configures a file handler and the operator is not
    reading stderr, so `on_save_error` is the only route by which a refusal
    becomes visible. Without it the fix would protect the file and still leave
    someone wondering why their edits vanish.

    Dies on: `save_now` returning False without notifying.
    """
    config_path.write_text('{ this is not json ')
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied")))
    manager = ConfigManager(config_path)
    manager.load()
    monkeypatch.undo()

    seen: list[Exception] = []
    manager.on_save_error = seen.append
    manager.save_now()

    assert len(seen) == 1, "the operator was never told"
    assert "unreadable" in str(seen[0]) and str(config_path) in str(seen[0])


def test_a_successful_quarantine_still_allows_saving(config_path):
    """The negative control: the refusal must be specific to the failure.

    Without this, disabling saves unconditionally on any malformed config
    would pass the two tests above while breaking every normal recovery --
    the file *was* preserved, so there is nothing left to protect.
    """
    config_path.write_text('{ this is not json ')
    manager = ConfigManager(config_path)
    manager.load()

    assert config_path.with_suffix(".json.corrupt").exists()
    assert manager.save_now() is True, (
        "the original was preserved, so saving must be allowed"
    )
    assert json.loads(config_path.read_text())["devices"], "defaults written"
