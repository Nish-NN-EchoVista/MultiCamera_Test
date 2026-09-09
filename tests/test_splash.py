"""The launch splash: artwork, frame bank, and PyGUI readiness detection.

Everything here is Tk-free. The splash draws with Pillow and pushes bytes to a
Windows layered window, so its artwork and its readiness probe can both be
tested without creating a window -- which is also what lets the frames be
pre-rendered on a background thread.
"""

from __future__ import annotations

import sys

import pytest

from PIL import Image

from emcc import splash
from emcc.integrations import PyGuiState, pygui_state, pygui_windows


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------


def test_card_is_canvas_sized_and_has_transparent_margin():
    card = splash._compose_card()
    assert card.size == (splash.WIDTH, splash.HEIGHT)
    assert card.mode == "RGBA"
    # The margin carries the shadow and glow, so it must not be fully opaque:
    # an opaque corner would mean the card had been drawn edge to edge and the
    # "floating" look lost.
    assert card.getpixel((1, 1))[3] < 40


def test_sweep_stays_within_the_track():
    """The highlight must not run past the track and over the card's edge.

    It used to be drawn straight onto the full canvas, so head and tail
    spilled outside the track -- visible as a blue bar hanging off the card.
    """
    track_x = (splash.WIDTH - splash.TRACK_W) // 2
    left_limit = track_x - splash.SWEEP_BLEED
    right_limit = track_x + splash.TRACK_W + splash.SWEEP_BLEED

    for step in range(0, splash.FRAME_COUNT, 5):
        layer = splash._sweep_layer(step / splash.FRAME_COUNT)
        bbox = layer.getchannel("A").getbbox()
        if bbox is None:
            continue                 # off-track entirely, which is allowed
        assert bbox[0] >= left_limit, f"sweep spilled left at step {step}"
        assert bbox[2] <= right_limit, f"sweep spilled right at step {step}"


def test_sweep_moves_across_the_loop():
    """Consecutive frames must differ, or the animation is a still image."""
    first = splash._sweep_layer(0.35).tobytes()
    later = splash._sweep_layer(0.65).tobytes()
    assert first != later


def test_sweep_is_visible_for_most_of_the_loop():
    lit = sum(
        1 for step in range(splash.FRAME_COUNT)
        if splash._sweep_layer(step / splash.FRAME_COUNT).getchannel("A").getbbox()
    )
    assert lit > splash.FRAME_COUNT * 0.6


# ---------------------------------------------------------------------------
# Frame bank
# ---------------------------------------------------------------------------


def test_frame_bank_builds_premultiplied_frames_of_the_right_size():
    bank = splash.FrameBank()
    bank._build()                    # synchronously, no thread needed
    assert bank.ready
    frames, card = bank.get()
    assert len(frames) == splash.FRAME_COUNT
    expected = splash.WIDTH * splash.HEIGHT * 4
    assert all(len(frame) == expected for frame in frames)
    # Mutable, so the status band can be written in place.
    assert isinstance(frames[0], bytearray)
    assert card.size == (splash.WIDTH, splash.HEIGHT)


def test_status_band_is_row_aligned_and_within_the_canvas():
    assert splash.BAND_Y + splash.BAND_H <= splash.HEIGHT
    assert (splash.BAND_Y * splash.WIDTH * 4) % 4 == 0


def test_status_band_clears_every_lit_sweep_pixel():
    """The band overwrites whole rows, so it must clear the sweep's *bloom*.

    Asserted against the rendered pixels, not against `TRACK_Y + TRACK_H`.
    The earlier version of this test compared the band to the track, which
    ignores the blurred bloom -- it passed while the band was erasing the
    bottom 8 rows of the glow in every frame at the original 540x240 size.
    """
    lowest = 0
    for step in range(splash.FRAME_COUNT):
        bbox = splash._sweep_layer(step / splash.FRAME_COUNT).getchannel("A").getbbox()
        if bbox is not None:
            lowest = max(lowest, bbox[3])
    assert lowest > 0, "the sweep never rendered a lit pixel"
    assert lowest <= splash.BAND_Y, (
        f"sweep reaches row {lowest}, band starts at {splash.BAND_Y} -- "
        "the band would erase the bloom"
    )


def test_status_text_sits_inside_the_card():
    """Derived centring, so a size change cannot push the text off the card."""
    text_y = splash.BAND_Y + splash.STATUS_CENTRE_Y
    assert splash.TRACK_Y + splash.TRACK_H < text_y < splash.HEIGHT - splash.MARGIN


def test_band_render_matches_the_slice_it_replaces():
    bank = splash.FrameBank()
    bank._build()
    frames, card = bank.get()

    window = splash.LaunchSplash.__new__(splash.LaunchSplash)
    window._card = card
    band = window._render_band("Launching PyGUI…")
    assert len(band) == splash.BAND_H * splash.WIDTH * 4

    # Writing it must not change a frame's length.
    before = len(frames[0])
    offset = splash.BAND_Y * splash.WIDTH * 4
    frames[0][offset:offset + len(band)] = band
    assert len(frames[0]) == before


def test_different_statuses_render_differently():
    bank = splash.FrameBank()
    bank._build()
    _frames, card = bank.get()
    window = splash.LaunchSplash.__new__(splash.LaunchSplash)
    window._card = card
    assert window._render_band("Launching…") != window._render_band("Starting…")


# ---------------------------------------------------------------------------
# PyGUI readiness
# ---------------------------------------------------------------------------


def test_no_ip_is_absent():
    assert pygui_state("") is PyGuiState.ABSENT
    assert pygui_state("   ") is PyGuiState.ABSENT
    assert pygui_windows("") == []


def test_unknown_device_is_absent():
    # TEST-NET-1: no PyGUI will ever be titled with this.
    assert pygui_state("192.0.2.199") is PyGuiState.ABSENT


@pytest.mark.skipif(sys.platform != "win32", reason="win32 window probing")
def test_window_enumeration_returns_handles_not_booleans():
    handles = pygui_windows("192.0.2.199")
    assert isinstance(handles, list)
    assert all(isinstance(h, int) for h in handles)


@pytest.mark.skipif(sys.platform != "win32", reason="win32 window probing")
def test_ignoring_every_window_reports_absent(monkeypatch):
    """A pre-existing PyGUI must not be mistaken for the one just launched."""
    monkeypatch.setattr("emcc.integrations.pygui_windows", lambda _ip: [4242])
    assert pygui_state("10.0.0.1", ignore=[4242]) is PyGuiState.ABSENT
    # Without the ignore set it would be judged on its own merits instead.
    assert pygui_state("10.0.0.1") is not PyGuiState.ABSENT
