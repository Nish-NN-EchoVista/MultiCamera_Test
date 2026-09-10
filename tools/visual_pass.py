"""Screenshot every UI state that assertions cannot judge.

Development tool. Nothing in `emcc/` imports it.

The test suite asserts on widget *configuration*, which is the right check for
behaviour but blind to a widget that is configured correctly and still renders
wrongly. This pass exists for that gap, and found three such bugs -- see
`docs/PLACEHOLDERS.md` section 8.

Driven entirely by the mock NPort, so it needs no hardware and cannot disturb
a bench unit. Every reading is pushed by hand rather than streamed, so no
state can expire before the shutter.

Captures use `tools/window_capture.py` (`PrintWindow`), not a screen grab, so
the machine can be in use while it runs.

Usage:

    python tools/visual_pass.py out_dir
"""

import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import customtkinter as ctk
from PIL import Image

from tools.window_capture import (
    BlankCaptureError,
    capture,
    capture_stable,
    capture_widget,
)

ctk.set_appearance_mode("dark")
from emcc.app import App
from emcc.backend.config_manager import ConfigManager
from emcc.backend.logging_setup import setup_logging
from tools.mock_nport import MockConfig, MockNPort

if len(sys.argv) < 2:
    sys.exit(f"usage: python {pathlib.Path(__file__).name} out_dir")
OUT = pathlib.Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

#: The app's own Clean fuse, raised so it cannot fire mid-pass. See the note
#: at the mock setup. Overridable rather than hard-coded so the mechanism can
#: be reproduced on demand: `EMCC_VISUAL_CLEAN_TIMEOUT=0.5` makes the timer
#: fire during the pass and the affected states render Clean-off, which is the
#: positive control for the diagnosis.
CLEAN_TIMEOUT_S = float(os.environ.get("EMCC_VISUAL_CLEAN_TIMEOUT", "9999"))
setup_logging(console=False, log_dir=pathlib.Path(tempfile.mkdtemp()))

work = pathlib.Path(tempfile.mkdtemp())
(work / "config.json").write_text(json.dumps({
    "settings": {"connection_timeout_s": 2.0, "reconnect_interval_s": 30.0,
                 "temperature_update_interval_s": 0.0},
    "devices": [
        {"device_name": "Camera 1", "ip_address": "127.0.0.1"},
        {"device_name": "Camera 2", "ip_address": "127.0.0.1"},
        {"device_name": "Camera 3", "ip_address": "127.0.0.1"},
        {"device_name": "Unreachable", "ip_address": "192.0.2.1"},
    ],
}))
cfg = ConfigManager(work / "config.json")
cfg.load()
app = App(config=cfg)
app.geometry("1340x860+30+30")


def pump(n=8):
    for _ in range(n):
        app.update_idletasks()
        app.update()


def wait(predicate, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        app._pump()
        pump(2)
        if predicate():
            return True
        time.sleep(0.04)
    return False


#: States whose capture came back flat. Reported at the end rather than
#: raised, so one unusable state cannot hide the eleven that worked.
failures: list[tuple[str, str]] = []


def _with_margin(image, pad: int):
    """A presentation gutter around a whole-window capture.

    `PrintWindow` renders the window and nothing behind it, so `pad` cannot
    mean "capture more" here -- there is nothing outside the window to see.
    It is a margin composited around the saved image, which is what the call
    sites want it for: legibility in the review sheet.
    """
    if not pad:
        return image
    canvas = Image.new("RGB", (image.width + pad * 2, image.height + pad * 2),
                       "#12141b")
    canvas.paste(image, (pad, pad))
    return canvas


def grab(name, widget=None, pad=0):
    """Capture the window's own pixels, immune to whatever sits on top of it.

    A flat frame is a failed capture, not a capture -- `window_capture`
    raises. Recorded and reported here rather than propagated: this pass
    captures twelve states, and aborting on the seventh would lose the last
    five and tell us less than a list of which ones failed.

    `pad` on a whole window is a *presentation margin* around the saved
    image, not extra captured content: `PrintWindow` renders the window and
    nothing behind it, so there is no way to see outside its bounds. On a
    child widget it expands the crop within the window, which is real.
    """
    pump(10)
    try:
        if isinstance(widget, ctk.CTkToplevel):
            # Freshly mapped: require a settled frame. `capture` alone
            # would accept a half-painted dialog, which is how
            # 08_error_dialog came back with an unrendered button label.
            image = _with_margin(capture_stable(widget), pad)
        elif widget is None:
            # Settled-frame check. Available unconditionally since the
            # status pulse was removed; it used to need the freeze below,
            # because a tweening dot never lets two frames agree.
            image = _with_margin(capture_stable(app), pad)
        else:
            image = capture_widget(widget, pad)
    except BlankCaptureError as exc:
        failures.append((name, str(exc)))
        print(f"  !! {name}.png FAILED: flat frame, nothing saved")
        return
    image.save(OUT / f"{name}.png")
    print(f"  {name}.png  {image.width}x{image.height}")


def dismiss_any():
    """Clear any dialog before a card capture, so it does not sit over them."""
    for _ in range(4):
        dialog = top_dialog()
        if dialog is None:
            break
        dialog.destroy()
        pump(3)


def top_dialog():
    kids = [c for c in app.winfo_children() if isinstance(c, ctk.CTkToplevel)]
    return kids[-1] if kids else None


results = []


def run():
    # No freeze step any more: the status pulse was removed on 2026-09-10,
    # so nothing here animates endlessly and every whole-window state gets
    # `capture_stable` -- the two-frames-agree check -- without preparation.
    #
    # What the freeze bought, for the record: two captures of the same idle
    # window differed by ~1216 of 5.8M pixels with the pulse running and by
    # zero with it stopped, and without it ten of twelve states fell back to
    # the blank-only check, which proved blind to partial rendering. Both
    # properties now hold by construction rather than by a call that could be
    # forgotten.
    pump(4)

    # Nothing may expire between a state being set up and its shutter, and
    # there are TWO clocks to stop, not one. Stopping only the mock's is what
    # left four states nondeterministic.
    #
    # The mock's clocks first: no periodic temperature stream and no
    # self-completing sweep, so every reading is pushed by hand.
    #
    # But `clean_duration_s=99` does not hold Clean active for 99s -- it
    # guarantees the DEVICE never finishes, which hands the ending to the
    # APP's own fuse, `clean_timeout_s`, default 10s:
    # `device_manager._restart_clean_timer` schedules `_on_clean_timeout`,
    # which clears `clean_active` and repaints. State 04 starts the clean, so
    # 05, both 06 crops and 03b rendered `ON_*` or `CLEAN_OFF_*` depending on
    # where the pass crossed 10s of wall clock. Bistable, 12,884 px in a
    # 182x72 box, and it MOVED BETWEEN STATES as the pass got faster -- which
    # is why it read as a different finding each time it was measured.
    #
    # The comment this replaces asserted a state "cannot expire before the
    # shutter". It was the app's own timer that expired them.
    mock = MockNPort(MockConfig(temp_interval_s=99, clean_duration_s=99)).start()
    app.config_manager.settings.tcp_port = mock.port
    app.config_manager.settings.clean_timeout_s = CLEAN_TIMEOUT_S
    cams = app.manager.devices[:3]
    unreachable = app.manager.devices[3]

    # 01 -- the transient CONNECTING caption, never seen by eye before.
    app._connect_device(cams[0].id)
    pump(2)
    grab("01_connecting")

    # 02 -- connected, showing a plausible temperature.
    wait(lambda: cams[0].is_connected)
    mock.push("PZT Temp: 41.5 C")
    results.append(("temperature_shown",
                    wait(lambda: cams[0].latest_temperature is not None)))
    dismiss_any()
    grab("02_connected_streaming")

    # 03 -- over threshold: red text plus the HIGH badge.
    mock.push("PZT Temp: 71.4 C")
    results.append(("alert_raised", wait(lambda: cams[0].temperature_alert)))
    results.append(("alert_chip_shown", app.title_bar._alert_wrap.winfo_ismapped()))
    dismiss_any()
    grab("03_over_temperature")

    # 04 -- Clean active on the first card, Auto on for the second.
    app._clean_device(cams[0].id)
    app._toggle_auto(cams[0].id)
    pump(6)
    dismiss_any()
    results.append(("clean_active", cams[0].clean_active))
    results.append(("auto_enabled", cams[0].auto_enabled))
    grab("04_clean_and_auto_active")

    # 05 -- every hover treatment at once, including the trash icon.
    card = app._cards[cams[0].id]
    card._set_hover(True)
    for part in (card._clean, card._auto, card._connection, card._temp):
        part._set_hover(True)
    grab("05_hover_states")
    for part in (card._clean, card._auto, card._connection, card._temp):
        part._set_hover(False)
    card._set_hover(False)

    # 06 -- the trash control close up; its rendering was never checked.
    # It sits in the Device Name column, in the slot the Figma pencil occupied.
    grab("06_trash_idle", card._sections[0].body, pad=8)
    card._set_trash_hover(True)
    grab("06_trash_hover", card._sections[0].body, pad=8)
    card._set_trash_hover(False)

    # 03b -- the chip must come back off once the reading returns to normal.
    mock.push("PZT Temp: 39.8 C")
    wait(lambda: not cams[0].temperature_alert)
    dismiss_any()
    results.append(("alert_chip_hidden", not app.title_bar._alert_wrap.winfo_ismapped()))
    # Checked here, at the LAST state that depends on it: `_on_clean_timeout`
    # clears the flag permanently, so a fire anywhere in 04..03b is still
    # visible at this point. One check covers the whole window.
    results.append(("clean_still_active", cams[0].clean_active))
    grab("03b_alert_cleared")

    # 07 -- confirm-remove dialog layout.
    app._confirm_remove(cams[2].id)
    pump(8)
    dialog = top_dialog()
    results.append(("confirm_dialog", dialog is not None))
    if dialog is not None:
        grab("07_confirm_dialog", dialog, pad=12)
        dialog.destroy()
    pump(4)

    # 08 -- the real coalesced error dialog, from a genuinely dead address.
    app._connect_device(unreachable.id)
    appeared = wait(lambda: top_dialog() is not None, timeout=25.0)
    results.append(("error_dialog", appeared))
    dialog = top_dialog()
    if dialog is not None:
        grab("08_error_dialog", dialog, pad=12)
        dialog.destroy()
    pump(4)

    # 09 -- RECONNECTING caption, only reproducible by killing the mock.
    mock.stop()
    reconnecting = wait(
        lambda: any(c.connection.name == "RECONNECTING" for c in cams), timeout=10.0
    )
    results.append(("reconnecting", reconnecting))
    grab("09_reconnecting")
    dialog = top_dialog()
    if dialog is not None:
        dialog.destroy()

    # 10 -- a long list, scrolled to the bottom.
    for _ in range(6):
        app._add_device()
    pump(12)
    app._scroll_to_end()
    grab("10_many_devices_scrolled")

    app.shutdown()


app.after(1500, run)
app.mainloop()
print()
for name, value in results:
    print(f"  {name}: {value}")

# A fired Clean fuse is not a failed capture -- the images exist and look
# entirely plausible. They are simply of a different state, and four of them
# will not match a baseline taken with the fuse held. That is exactly how it
# went unnoticed: a wrong-state image reads as a pass, and then as a diff
# against the next run. So it exits non-zero like a blank frame does, rather
# than printing `False` in a list nobody reads twice.
if any(name == "clean_still_active" and value is False for name, value in results):
    print()
    print("  INVALID PASS -- the app's Clean fuse fired before the shutter.")
    print(f"    clean_timeout_s was {CLEAN_TIMEOUT_S}s and state 04 starts the")
    print("    clean. 05, both 06 crops and 03b render Clean-OFF and will")
    print("    differ from any baseline captured with it held, by ~12,884px in")
    print("    a 182x72 box. Raise EMCC_VISUAL_CLEAN_TIMEOUT and re-run.")
    sys.exit(1)

if failures:
    print()
    print(f"  {len(failures)} CAPTURE(S) FAILED -- no image for these:")
    for name, why in failures:
        print(f"    {name}: {why}")
    # Non-zero exit, so a blank capture can never again read as a pass.
    sys.exit(1)
print()
print("  all captures contained content")
