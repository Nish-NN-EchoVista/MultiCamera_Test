"""The PyGUI hand-off splash: show, poll for readiness, dismiss.

Relocated from `app.py`'s "Launch splash" section unchanged. `App` keeps
`_show_launch_splash`, `_poll_pygui` and `_on_splash_dismissed` as one-line
methods -- `_on_splash_dismissed` is handed to `LaunchSplash` as a bound
callback, and the two recursive `after` schedules re-enter through `App` so
that the dispatch stays exactly what it is today.

The splash state itself (`_splash`, `_splash_job`, `_splash_deadline`,
`_splash_ready_polls`) stays on `App`. Moving it here would make this module
stateful and give the shell two places to look during shutdown, which is the
opposite of what the extraction is for.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .. import splash as splash_module
from ..integrations import PyGuiState, pygui_state
from ..splash import LaunchSplash

if TYPE_CHECKING:
    from ..app import App

# Deliberately "emcc.app" and not `__name__`: these lines logged under
# that name before the extraction, and a pure move should not change what
# appears in logs/emcc.log. It also matches the codebase convention of
# explicit domain names (emcc.device, emcc.ui, emcc.integration).
logger = logging.getLogger("emcc.app")


#: How long to wait for PyGUI's window before the splash reports failure.
#: PyGUI measures 7-8s from click to ready, so this is generous without
#: leaving the operator staring at an animation after a crash.
SPLASH_TIMEOUT_S = 20.0

#: Consecutive READY polls before the splash is dismissed. PyGUI's
#: responsiveness probe flaps while its config panel builds in batches, so one
#: poll is not evidence the loop is free.
SPLASH_READY_POLLS = 2

#: If the frame bank is still building when a hand-off starts, wait for it
#: rather than silently going without a splash. Pre-rendering takes ~720ms on
#: a background thread and starts 500ms after first paint, so a click inside
#: the first ~1.2s can arrive early. Waiting briefly covers that; PyGUI takes
#: ~8s to be ready, so a splash raised 0.5s late still covers the wait.
SPLASH_WAIT_MS = 150
SPLASH_WAIT_ATTEMPTS = 10


def show_launch_splash(
    app: "App", name: str, ip: str, existing: list[int], attempt: int = 0
) -> None:
    """Cover PyGUI's ~7-8s startup, dismissing when it is actually up.

    Retries while the frame bank is still pre-rendering. Going without a
    splash is a valid fallback for a *failed* build, but not for one that
    simply has not finished -- that would show up as an intermittently
    missing animation depending on how soon after launch the operator
    clicked, which is the kind of flakiness nobody manages to reproduce.
    """
    if app._shutting_down:
        return
    if not splash_module.FRAMES.ready and attempt < SPLASH_WAIT_ATTEMPTS:
        app.after(
            SPLASH_WAIT_MS,
            lambda: app._show_launch_splash(name, ip, existing, attempt + 1),
        )
        return

    if app._splash is not None and app._splash.alive:
        app._splash.close()

    splash = LaunchSplash(app, on_dismiss=app._on_splash_dismissed)
    if not splash.show(f"Launching PyGUI for {name}…"):
        return                      # frames not ready; go without
    app._splash = splash
    app._splash_deadline = time.monotonic() + SPLASH_TIMEOUT_S
    app._splash_ready_polls = 0
    app._poll_pygui(ip, existing)


def poll_pygui(app: "App", ip: str, existing: list[int]) -> None:
    """Watch for PyGUI, then dismiss. Never leaves the splash stranded."""
    app._splash_job = None
    splash = app._splash
    if splash is None or not splash.alive or app._shutting_down:
        return

    state = pygui_state(ip, ignore=existing)
    if state is PyGuiState.READY:
        # Debounced: the responsiveness probe alternates while PyGUI's
        # config panel builds in batches, so one READY poll can land in a
        # gap between them. Two in a row means the loop is genuinely free.
        app._splash_ready_polls += 1
        if app._splash_ready_polls >= SPLASH_READY_POLLS:
            logger.info("PyGUI is up for %s; dismissing the splash", ip)
            splash.close()
            return
    else:
        app._splash_ready_polls = 0
        if state is PyGuiState.STARTING:
            splash.set_status(f"Starting interface — {ip}…")

    if time.monotonic() >= app._splash_deadline:
        # Report rather than vanish: a crash and a slow start look
        # identical otherwise, and the operator is left guessing.
        logger.warning("PyGUI did not appear for %s within %.0fs",
                       ip, SPLASH_TIMEOUT_S)
        splash.fail("PyGUI did not start — see logs/emcc.log")
        return

    app._splash_job = app.after(
        splash_module.POLL_INTERVAL_MS,
        lambda: app._poll_pygui(ip, existing),
    )


def on_splash_dismissed(app: "App") -> None:
    app._splash = None
    if app._splash_job is not None:
        try:
            app.after_cancel(app._splash_job)
        except Exception:
            pass
        app._splash_job = None
