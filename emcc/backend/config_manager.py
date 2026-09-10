"""Configuration load/save.

`config.json` holds global settings and the persisted device list. Only
`device_name`, `ip_address` and a stable `id` are persisted -- never connection,
Auto, Clean, temperature or error state.

Three properties this module guarantees:

* **It never crashes the app.** A missing, malformed, truncated or
  older-schema file degrades to defaults, and the reason is logged.
* **Writes are atomic.** Content goes to a temporary file in the same
  directory and is then `os.replace`d over the target, which is atomic on
  Windows and POSIX. A crash mid-write cannot leave a half-written config.
* **Writes are debounced and serialised.** Typing a device name fires a save
  per keystroke; the debounce collapses those into one write ~400 ms after
  typing stops. All writes go through one lock, so the UI thread and the
  shutdown path cannot interleave.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Written into the file so a future schema change can migrate deliberately.
SCHEMA_VERSION = 1

#: Debounce window for edits. Long enough to collapse a burst of typing, short
#: enough that saving feels immediate.
SAVE_DEBOUNCE_S = 0.4


@dataclass
class Settings:
    """Global application settings, all overridable from `config.json`."""

    #: Moxa NPort data port for serial port 1. NPort exposes 4001..4016.
    tcp_port: int = 4001
    temperature_warning_c: float = 65.0
    connection_timeout_s: float = 3.0
    clean_timeout_s: float = 10.0
    temperature_update_interval_s: float = 1.0
    reconnect_attempts: int = 3
    reconnect_interval_s: float = 10.0
    #: Delay before the post-connect Auto reset is sent. Cancelled if the
    #: operator toggles Auto first -- see DeviceManager.
    auto_reset_delay_s: float = 2.0

    #: Where the PyGUI diagnostic tool lives, for the temperature hand-off.
    #: An absolute bench path as the default is deliberate -- this is
    #: site-specific rather than derivable -- and it is overridable in
    #: config.json like any other setting. Forward slashes work on Windows.
    pygui_path: str = (
        "C:/Users/NishathNawaz/OneDrive - Echovista Limited"
        "/Documents/Github/PyGUI"
    )

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        """Build from untrusted data, ignoring unknown and invalid keys."""
        settings = cls()
        if not isinstance(data, dict):
            if data is not None:
                logger.warning("config: 'settings' is not an object, using defaults")
            return settings

        for name, default in vars(cls()).items():
            if name not in data:
                continue
            value = data[name]
            try:
                coerced = type(default)(value)
            except (TypeError, ValueError):
                logger.warning(
                    "config: setting %r has invalid value %r, using default %r",
                    name, value, default,
                )
                continue
            if isinstance(coerced, (int, float)) and coerced <= 0:
                logger.warning(
                    "config: setting %r must be positive (got %r), using default %r",
                    name, value, default,
                )
                continue
            setattr(settings, name, coerced)

        unknown = set(data) - set(vars(cls()))
        if unknown:
            logger.info("config: ignoring unknown settings %s", sorted(unknown))
        return settings


@dataclass
class DeviceConfig:
    """One persisted device.

    `id` is the stable internal identity. Device names are user-editable and so
    cannot be used as a key; persisting the id keeps log correlation and card
    identity intact across restarts.
    """

    device_name: str
    ip_address: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def from_dict(cls, data: Any, index: int) -> "DeviceConfig | None":
        if not isinstance(data, dict):
            logger.warning("config: device entry %d is not an object, skipped", index)
            return None
        name = data.get("device_name")
        if not isinstance(name, str) or not name.strip():
            name = f"Camera {index + 1}"
            logger.warning("config: device entry %d has no name, using %r", index, name)
        ip = data.get("ip_address", "")
        if not isinstance(ip, str):
            logger.warning("config: device %r has a non-string IP, cleared", name)
            ip = ""
        device_id = data.get("id")
        if not isinstance(device_id, str) or not device_id:
            device_id = str(uuid.uuid4())
        return cls(device_name=name.strip(), ip_address=ip.strip(), id=device_id)


#: Seeded on first run. These are the live systems on the bench, so a fresh
#: install is immediately usable. Startup still leaves them disconnected.
DEFAULT_DEVICES: tuple[tuple[str, str], ...] = (
    ("Camera 1", "192.168.2.250"),
    ("Camera 2", "192.168.2.251"),
    ("Camera 3", "192.168.2.253"),
)


def default_devices() -> list[DeviceConfig]:
    return [DeviceConfig(device_name=n, ip_address=ip) for n, ip in DEFAULT_DEVICES]


class ConfigManager:
    """Owns `config.json`. The single place that touches it."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *,
                 on_save_error: Callable[[Exception], None] | None = None):
        self.path = Path(path) if path is not None else Path("config.json")
        self.settings = Settings()
        self.devices: list[DeviceConfig] = []

        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        self._save_failures = 0
        #: Set when an unreadable config could not be preserved. While it is
        #: true, `save_now` refuses: the original bytes exist only in
        #: `self.path`, and saving defaults over them would destroy the
        #: operator's device list with nothing left to recover from.
        self._preserve_failed = False
        #: Taken at construction rather than assigned afterwards. `None` is
        #: a legitimate state -- a headless `ConfigManager` (the tools, most
        #: of `tests/`) has no operator to tell -- so this cannot be made
        #: mandatory. What it can do is close the window in which the object
        #: exists unwired, which is what a post-hoc assignment leaves open.
        self.on_save_error: Callable[[Exception], None] | None = on_save_error

    # -- load --------------------------------------------------------------

    def load(self) -> None:
        """Load config, falling back to defaults for anything unusable."""
        if not self.path.exists():
            logger.info("config: %s not found, creating defaults", self.path)
            self.settings = Settings()
            self.devices = default_devices()
            self.save_now()
            return

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("config: cannot read %s (%s), using defaults", self.path, exc)
            self._use_defaults()
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Keep the bad file so it can be inspected rather than silently
            # lost -- and if that could not be done, refuse to save over it.
            if not self._quarantine(raw):
                self._preserve_failed = True
            logger.error("config: %s is malformed (%s), using defaults", self.path, exc)
            self._use_defaults()
            return

        if not isinstance(data, dict):
            logger.error("config: top level is not an object, using defaults")
            self._use_defaults()
            return

        version = data.get("version", 0)
        if isinstance(version, int) and version > SCHEMA_VERSION:
            logger.warning(
                "config: file version %s is newer than supported %s; "
                "unknown fields will be dropped on next save",
                version, SCHEMA_VERSION,
            )

        self.settings = Settings.from_dict(data.get("settings"))

        entries = data.get("devices")
        if not isinstance(entries, list):
            logger.warning("config: 'devices' missing or not a list, seeding defaults")
            self.devices = default_devices()
        else:
            parsed = [
                device
                for index, entry in enumerate(entries)
                if (device := DeviceConfig.from_dict(entry, index)) is not None
            ]
            self.devices = parsed
            self._dedupe_ids()

        logger.info(
            "config: loaded %d device(s) from %s (port %d, warn %.1f C)",
            len(self.devices), self.path,
            self.settings.tcp_port, self.settings.temperature_warning_c,
        )

    def _use_defaults(self) -> None:
        self.settings = Settings()
        self.devices = default_devices()

    def _dedupe_ids(self) -> None:
        """Two devices sharing an id would collide in every lookup."""
        seen: set[str] = set()
        for device in self.devices:
            if device.id in seen:
                device.id = str(uuid.uuid4())
                logger.warning("config: duplicate device id, reassigned for %r",
                               device.device_name)
            seen.add(device.id)

    def _quarantine(self, raw: str) -> bool:
        """Preserve an unreadable config. True if it is safely on disk.

        **The return value is load-bearing.** If this fails, the operator's
        original bytes exist in exactly one place -- the file we are about to
        replace with defaults -- so the caller must stop the overwrite rather
        than continue and hope.

        This used to swallow the `OSError` entirely, which lost the config
        unrecoverably and said nothing: the operator saw an application that
        had come up with three default devices.

        JUSTIFIED BY THE CONSEQUENCE, not by the likelihood. A config that
        could not be read and could not be backed up is the operator's only
        copy of their device list, so writing defaults over it destroys it
        with nothing to recover from. That holds at any failure rate,
        including one nobody has measured -- which is the situation here.

        An earlier version argued from likelihood instead, and did it twice
        over. It asserted this tree was "OneDrive-hosted with Defender live":
        false since the move to C:/Nish/MultiCam. And it cited
        `tests/conftest.py` for the cause, quoting "handle churn, or a scanner
        holding it" -- a phrase since deleted from that file as an unmeasured
        hypothesis, so the citation outlived the text it pointed at and named
        a withdrawn cause as authority.

        A justification resting on an environment, or on another file's
        wording, has two ways to go stale. One resting on the consequence of
        failing has none.
        """
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        try:
            backup.write_text(raw, encoding="utf-8")
        except OSError as exc:
            logger.error(
                "config: could not preserve the unreadable %s as %s (%s). "
                "Saving is disabled for this session so the original is not "
                "overwritten -- copy it aside by hand before changing any "
                "device.",
                self.path, backup, exc, exc_info=True,
            )
            return False
        logger.info("config: unreadable file preserved as %s", backup)
        return True

    # -- save --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "settings": asdict(self.settings),
            "devices": [asdict(device) for device in self.devices],
        }

    def request_save(self) -> None:
        """Schedule a debounced save. Safe to call on every keystroke."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(SAVE_DEBOUNCE_S, self.save_now)
            self._timer.name = "config-save-debounce"
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Write any pending debounced save immediately. Used on shutdown."""
        with self._timer_lock:
            pending = self._timer is not None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if pending:
            self.save_now()

    def save_now(self) -> bool:
        """Write the config atomically. Returns True on success.

        Refuses outright while `_preserve_failed` is set. That trade is
        deliberate: losing this session's edits is recoverable, and
        overwriting an unreadable original whose only copy is on disk is not.
        """
        if self._preserve_failed:
            logger.error(
                "config: refusing to save. %s could not be read and could not "
                "be preserved either, so it is the only copy of the "
                "operator's device list -- writing defaults over it would "
                "destroy it. Copy the file aside, then restart.",
                self.path,
            )
            self._notify_save_error(RuntimeError(
                f"{self.path} is unreadable and could not be backed up. "
                f"Saving is disabled so the file is not overwritten. Copy it "
                f"aside and restart."
            ))
            return False

        with self._lock:
            payload = json.dumps(self.to_dict(), indent=2) + "\n"
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Same directory, so os.replace is a true atomic rename.
                handle, temp_path = tempfile.mkstemp(
                    dir=str(self.path.parent),
                    prefix=self.path.name + ".",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as file:
                        file.write(payload)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temp_path, self.path)
                except BaseException:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                self._save_failures += 1
                logger.error("config: save to %s failed (%s)", self.path, exc)
                # Only escalate to the operator once it is clearly persistent.
                if self._save_failures in (3, 30):
                    self._notify_save_error(exc)
                return False

            if self._save_failures:
                logger.info("config: save recovered after %d failure(s)",
                            self._save_failures)
            self._save_failures = 0
            logger.debug("config: saved %d device(s)", len(self.devices))
            return True

    def _notify_save_error(self, exc: Exception) -> None:
        """Tell the operator, if anyone is listening.

        Extracted so the refusal above reaches the same dialog as a repeated
        write failure. A log line alone is not surfacing: nothing in the suite
        configures a file handler, and the operator is not reading stderr.
        """
        if self.on_save_error is None:
            # Recorded rather than dropped. A headless `ConfigManager` -- the
            # tools, most of `tests/` -- legitimately has no operator to tell,
            # so an empty sink is not an error in itself. In the application it
            # means the wiring in `App.__init__` is missing and the operator is
            # being told nothing at all, which is the F12 failure.
            #
            # WARNING **without** `exc_info`, deliberately: `conftest`'s
            # containment fixture keys on WARNING-or-above *carrying* exception
            # info, and this is a missing sink rather than a swallowed
            # exception. Measured -- attaching `exc_info` makes that fixture
            # error on every test that provokes an empty-sink notification.
            #
            # An earlier version of this comment named
            # `test_existing_config_is_intact_after_failed_save` as the one it
            # would break. Wrong, and checked only after writing it: that test
            # calls `save_now()` ONCE, and the caller above escalates only at
            # `_save_failures in (3, 30)`, so it never reaches this line. The
            # test actually affected is the one asserting this behaviour.
            logger.warning("config: save error with no operator sink: %s", exc)
            return
        try:
            self.on_save_error(exc)
        except Exception:
            logger.exception("config: save-error callback raised")

    # -- device list -------------------------------------------------------

    def add_device(self, device: DeviceConfig) -> None:
        self.devices.append(device)
        self.request_save()

    def remove_device(self, device_id: str) -> bool:
        before = len(self.devices)
        self.devices = [d for d in self.devices if d.id != device_id]
        if len(self.devices) != before:
            self.request_save()
            return True
        return False

    def find(self, device_id: str) -> DeviceConfig | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def next_device_name(self) -> str:
        """A default name that does not collide with existing ones.

        Counts up from the device count rather than reusing `Camera N` blindly,
        so renaming existing cards cannot produce a duplicate.
        """
        existing = {d.device_name.strip().casefold() for d in self.devices}
        index = len(self.devices) + 1
        while f"camera {index}" in existing:
            index += 1
        return f"Camera {index}"

    def close(self) -> None:
        """Cancel any pending timer and flush. Idempotent."""
        self.flush()
