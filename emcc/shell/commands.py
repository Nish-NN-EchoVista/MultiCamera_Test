"""Device actions invoked from a card: connect, clean, auto, temperature.

Relocated from `app.py`'s "Device actions" section unchanged. `App` keeps
`_connect_device`, `_clean_device`, `_toggle_auto` and `_open_temperature` as
one-line methods, because `DeviceCard` is handed them as bound callbacks
(`on_connect=self._connect_device`, ...).

`open_temperature` reaches the splash through `app._show_launch_splash` rather
than importing `shell.handoff` directly. That is deliberate: it preserves
today's dispatch exactly, so anything that replaces the method on an `App`
instance still takes effect, and it leaves these two modules independent of
each other instead of encoding a cross-module call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..integrations import launch_pygui, pygui_windows

if TYPE_CHECKING:
    from ..app import App

# Deliberately "emcc.app" and not `__name__`: these lines logged under
# that name before the extraction, and a pure move should not change what
# appears in logs/emcc.log. It also matches the codebase convention of
# explicit domain names (emcc.device, emcc.ui, emcc.integration).
logger = logging.getLogger("emcc.app")


def connect_device(app: "App", device_id: str) -> None:
    device = app.manager.get(device_id)
    if device is None:
        return
    # Clear any previous announcement so a fresh failure pops again.
    device.fault_announced = False
    error = app.manager.toggle_connection(device_id)
    if error is not None:
        app._errors.show_now(device.name, device.ip, error)
    app._render_device(device_id)
    app._refresh_chrome()


def clean_device(app: "App", device_id: str) -> None:
    if not app.manager.start_clean(device_id):
        device = app.manager.get(device_id)
        if device is not None and not device.is_connected:
            app._errors.show_now(
                device.name, device.ip,
                "Connect the device before starting a Clean")
        return
    app._render_device(device_id)


def toggle_auto(app: "App", device_id: str) -> None:
    device = app.manager.get(device_id)
    if device is None:
        return
    if not app.manager.set_auto(device_id, not device.auto_enabled):
        if not device.is_connected:
            app._errors.show_now(
                device.name, device.ip,
                "Connect the device before enabling Auto")
        return
    app._render_device(device_id)


def open_temperature(app: "App", device_id: str) -> None:
    """Hand this device over to PyGUI for detailed diagnostics.

    Order is load-bearing: the NPort accepts one client, so EMCC must
    release the device before PyGUI can claim it. PyGUI waits ~1.5s before
    connecting, which covers the socket actually closing.

    The launch is attempted *first* so that a failed hand-off (PyGUI moved,
    no IP set) does not drop a working connection for nothing.

    EMCC does not reattach afterwards: closing PyGUI frees the device and
    the operator reconnects here by hand.
    """
    device = app.manager.get(device_id)
    if device is None:
        return

    # Windows already on screen for this device. A second hand-off would
    # otherwise match the *existing* PyGUI and dismiss the splash at once.
    existing = pygui_windows(device.ip)

    launch = launch_pygui(device.ip, app.config_manager.settings.pygui_path)
    if launch.error is not None:
        app._errors.show_now(device.name, device.ip, launch.error)
        return

    # Held so `poll_pygui` can tell an early exit from a slow start. Replaced
    # on every hand-off rather than accumulated: only the newest launch has a
    # splash waiting on it, and an earlier PyGUI is expected to outlive EMCC.
    app._pygui_proc = launch.process

    if device.connection.is_live:
        logger.info("%s released for the PyGUI hand-off", device.name)
        app.manager.disconnect(device_id)
        app._render_device(device_id)
        app._refresh_chrome()

    app._show_launch_splash(device.name, device.ip, existing)
