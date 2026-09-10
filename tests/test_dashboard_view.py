"""Dashboard view: the 5-column density grid and the List/Dashboard toggle.

Same conventions as test_ui_integration.py -- real Tk widgets, no mocking,
assertions on widget configuration rather than pixels, and colours compared
against `theme.*` tokens rather than literal hex.

These build a `DashboardView` directly on a bare root rather than through
`App`, because the view is self-contained: it takes a `DeviceManager` and two
callbacks and owns everything else. That also keeps the tests fast, since a
full `App` costs a splash and ~900 widgets.
"""

from __future__ import annotations

import gc
import inspect
import json
import time
import tkinter

import customtkinter as ctk
import pytest

from emcc import fonts, theme
from emcc.backend.config_manager import ConfigManager
from emcc.backend.device_manager import DeviceManager
from emcc.backend.events import ConnectionState
from emcc.views.base import DeviceView
from emcc.widgets.dashboard_view import DashboardCard, DashboardView
from emcc.widgets.view_toggle import DASHBOARD, LIST, ViewToggle

#: What `fonts.tracked` inserts between characters (U+200A HAIR SPACE). Its
#: presence in a string means letter-spacing has already been applied.
HAIR_SPACE = " "


def _config(tmp_path, count: int) -> ConfigManager:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "devices": [
                {"device_name": f"Camera {i + 1}", "ip_address": ""}
                for i in range(count)
            ]
        }),
        encoding="utf-8",
    )
    config = ConfigManager(path)
    config.load()
    return config


def _root(attempts: int = 5) -> ctk.CTk:
    """A sized root, retrying Tk's transient start-up failure.

    Same environmental problem `conftest.make_app` documents: this module
    creates a root per test, so it hits the "invalid command name
    tcl_findLibrary" / "Can't find a usable init.tcl" churn on Windows. Only
    root creation is retried -- once it succeeds, nothing here is transient.
    """
    ctk.set_appearance_mode("dark")
    last: tkinter.TclError | None = None
    for attempt in range(attempts):
        try:
            root = ctk.CTk(fg_color=theme.APP_BG)
            break
        except tkinter.TclError as exc:
            last = exc
            gc.collect()
            time.sleep(0.5 * (attempt + 1))
    else:
        raise AssertionError(
            f"Tk failed to initialise after {attempts} attempts "
            f"(environmental, not a product failure): {last}"
        )
    root.geometry(f"{theme.WINDOW_W}x{theme.WINDOW_H}+40+40")
    fonts.init()
    return root


def _pump(root, times: int = 4) -> None:
    for _ in range(times):
        root.update_idletasks()
        root.update()


def _view(tmp_path, count: int, *, complete: bool = True):
    """A packed DashboardView with `count` devices.

    `complete=True` finishes the progressive build so geometry can be
    measured; pass False to inspect the streaming behaviour itself.
    """
    root = _root()
    config = _config(tmp_path, count)
    manager = DeviceManager(config, root.after, root.after_cancel)
    cleaned: list[str] = []
    autoed: list[str] = []
    view = DashboardView(root, manager=manager,
                         on_clean=cleaned.append, on_auto=autoed.append)
    view.pack(fill="both", expand=True, padx=theme.PAGE_PAD_X,
              pady=theme.PAGE_PAD_Y)
    if complete:
        # build_now cancels the pending timer, so the window can be mapped
        # afterwards without the streaming build racing the geometry pass.
        view.build_now()
        _pump(root, 2)
    else:
        root.update_idletasks()
    return root, manager, view, cleaned, autoed


# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------

def test_grid_wraps_every_five_devices(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 12)
    try:
        expected = [(i // 5, i % 5) for i in range(12)]
        actual = [
            (view.cards[d.id].grid_info()["row"],
             view.cards[d.id].grid_info()["column"])
            for d in manager.devices
        ]
        assert actual == expected
        assert theme.DASH_COLS == 5
    finally:
        root.destroy()


def test_unpopulated_slots_are_left_empty(tmp_path):
    """Three devices means three cards -- no placeholder, no ghost outline."""
    root, _, view, _, _ = _view(tmp_path, 3)
    try:
        assert len(view.cards) == 3
        children = view._grid.winfo_children()
        cards = [c for c in children if isinstance(c, DashboardCard)]
        assert len(cards) == 3
        # Nothing else is gridded into the remaining 22 slots.
        gridded = [c for c in children if c.grid_info()]
        assert gridded == cards
    finally:
        root.destroy()


def test_rows_take_card_height_not_an_equal_share(tmp_path):
    """Three cards keep full height, with empty space beneath.

    Regression guard: giving the rows weight would stretch three cards over
    the whole viewport instead of leaving the rest of the grid blank.
    """
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        card = view.cards[manager.devices[0].id]
        scaling = ctk.ScalingTracker.get_widget_scaling(card)
        assert round(card.winfo_height() / scaling) == theme.DASH_CARD_H
        assert card.winfo_height() < view.winfo_height() / 2
    finally:
        root.destroy()


def test_twenty_five_cards_fit_the_default_window(tmp_path):
    """The stated design target: 25 devices visible at once, no scrolling."""
    root, manager, view, _, _ = _view(tmp_path, 25)
    try:
        _pump(root, 2)
        last = view.cards[manager.devices[-1].id]
        content = last.winfo_y() + last.winfo_height()
        assert last.grid_info()["row"] == 4
        assert content <= view.winfo_height(), (
            f"grid content {content}px exceeds viewport {view.winfo_height()}px"
        )
    finally:
        root.destroy()


def test_grid_scrolls_rather_than_capping_the_device_count(tmp_path):
    """A 26th device gets a card on a sixth row, not a refusal."""
    root, manager, view, _, _ = _view(tmp_path, 26)
    try:
        assert len(view.cards) == 26
        assert view.cards[manager.devices[25].id].grid_info()["row"] == 5
        assert view.slots == 25
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Progressive build
# ---------------------------------------------------------------------------

def test_first_row_is_built_before_the_rest_streams(tmp_path):
    """A card costs ~70ms, so 25 up front would block for seconds."""
    root, _, view, _, _ = _view(tmp_path, 25, complete=False)
    try:
        assert len(view.cards) == theme.DASH_INITIAL_CARDS
        assert len(view._pending) == 25 - theme.DASH_INITIAL_CARDS
        view.build_now()
        assert len(view.cards) == 25
        assert view._pending == []
    finally:
        root.destroy()


def test_pending_build_is_cancelled_on_destroy(tmp_path):
    root, _, view, _, _ = _view(tmp_path, 25, complete=False)
    try:
        assert view._build_job is not None
        view.destroy()
        assert view._build_job is None
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Card content -- what the dashboard drops, and what it keeps
# ---------------------------------------------------------------------------

def test_card_omits_ip_and_connect(tmp_path):
    """The density view trades these for count; see the module docstring."""
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        card = view.cards[manager.devices[0].id]
        assert not hasattr(card, "_ip")
        assert not hasattr(card, "_connection")
        # but it keeps the two controls
        assert hasattr(card, "_clean")
        assert hasattr(card, "_auto")
    finally:
        root.destroy()


def test_card_shows_the_device_name(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 2)
    try:
        card = view.cards[manager.devices[0].id]
        assert card._name.cget("text") == "Camera 1"
    finally:
        root.destroy()


def test_clean_and_auto_callbacks_carry_the_device_id(tmp_path):
    root, manager, view, cleaned, autoed = _view(tmp_path, 3)
    try:
        target = manager.devices[1]
        view.cards[target.id]._clean._interactive._on_click()
        view.cards[target.id]._auto._interactive._on_click()
        assert cleaned == [target.id]
        assert autoed == [target.id]
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Connection state -- the two views must agree on colour
# ---------------------------------------------------------------------------

def test_border_and_dot_follow_connection_state(tmp_path):
    """Blue idle, green connected, red for a loss nobody asked for.

    The dashboard reads `theme.CONN` through `connection_visual`, the same
    source the list view's Connect button uses, so one toggle click cannot
    change what a device's colour means.
    """
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]

        assert card.cget("border_color") == theme.CONN["idle"]["ring"]
        assert card._dot.cget("fg_color") == theme.CONN["idle"]["dot"]
        assert card._status.cget("text") == "Disconnected"

        device.connection = ConnectionState.CONNECTED
        card.render()
        assert card.cget("border_color") == theme.CONN["connected"]["ring"]
        assert card._dot.cget("fg_color") == theme.CONN["connected"]["dot"]
        assert card._status.cget("text") == "Connected"

        device.connection = ConnectionState.ERROR
        device.last_error = "Connection refused"
        card.render()
        assert card.cget("border_color") == theme.CONN["fault"]["ring"]
        assert card._dot.cget("fg_color") == theme.CONN["fault"]["dot"]
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------

def test_temperature_reads_as_no_data_before_the_first_sample(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        card = view.cards[manager.devices[0].id]
        assert card._value.cget("text") == theme.TEMP_PLACEHOLDER
        assert card._value.cget("text_color") == theme.TEXT_GHOST
        assert card._caption.cget("text") == ""
        assert card._badge_visible is False
    finally:
        root.destroy()


def test_nominal_temperature_shows_the_value_and_caption(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        device.latest_temperature = 42.4
        card.render()
        assert card._value.cget("text") == "42°"
        assert card._value.cget("text_color") == theme.TEMP_OK_TEXT
        assert card._caption.cget("text") == theme.DASH_TEMP_NOMINAL
        assert card._panel.cget("border_color") == theme.TEMP_OK_BORDER
        assert card._badge_visible is False
    finally:
        root.destroy()


def test_alert_turns_the_panel_red_and_shows_the_high_chip(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        device.latest_temperature = 78.0
        device.temperature_alert = True
        card.render()

        assert card._value.cget("text") == "78°"
        assert card._value.cget("text_color") == theme.TEMP_ALERT_TEXT
        assert card._panel.cget("border_color") == theme.TEMP_ALERT_BORDER
        assert card._caption.cget("text") == theme.DASH_TEMP_ALERT
        assert card._badge_visible is True
        # pack_info() raises TclError once forgotten, so ask the manager.
        assert card._badge.winfo_manager() == "pack"

        device.temperature_alert = False
        card.render()
        assert card._badge_visible is False
        assert card._badge.winfo_manager() == ""
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_adding_a_device_does_not_rebuild_existing_cards(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        before = {d.id: id(view.cards[d.id]) for d in manager.devices}
        view.add_device(manager.add_device())
        view.build_now()
        assert len(view.cards) == 4
        for device_id, widget_id in before.items():
            assert id(view.cards[device_id]) == widget_id
    finally:
        root.destroy()


def test_removing_a_device_regrids_the_survivors(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 7)
    try:
        victim = manager.devices[0].id
        survivor = manager.devices[6].id
        widget_id = id(view.cards[survivor])

        manager.remove_device(victim)
        view.remove_device(victim)
        view.build_now()

        assert victim not in view.cards
        assert len(view.cards) == 6
        # The last device moves from slot 6 to slot 5 without being rebuilt.
        assert id(view.cards[survivor]) == widget_id
        assert view.cards[survivor].grid_info()["row"] == 1
        assert view.cards[survivor].grid_info()["column"] == 0
    finally:
        root.destroy()


def test_render_device_reports_an_unknown_id(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        assert view.render_device(manager.devices[0].id) is True
        assert view.render_device("no-such-device") is False
    finally:
        root.destroy()


def test_caption_counts_devices_and_slots(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        total = len(manager.devices)
        assert view.caption(total) == "3 devices · 0 connected · 3 of 25 slots"
        manager.devices[0].connection = ConnectionState.CONNECTED
        assert view.caption(total) == "3 devices · 1 connected · 3 of 25 slots"

        # `total` is accepted and ignored: both halves come from _devices so
        # they cannot disagree. A wrong total must not reach the output.
        assert view.caption(999) == "3 devices · 1 connected · 3 of 25 slots"
    finally:
        root.destroy()


def test_caption_singular_for_one_device(tmp_path):
    root, _, view, _, _ = _view(tmp_path, 1)
    try:
        assert view.caption(1).startswith("1 device ·")
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# The List / Dashboard toggle
# ---------------------------------------------------------------------------

@pytest.fixture
def toggle():
    root = _root()
    changes: list[str] = []
    control = ViewToggle(root, view=LIST, on_change=changes.append)
    control.pack()
    root.update_idletasks()
    yield root, control, changes
    root.destroy()


def test_toggle_starts_on_the_given_view(toggle):
    _, control, changes = toggle
    assert control.view == LIST
    assert control._segments[LIST]._selected is True
    assert control._segments[DASHBOARD]._selected is False
    assert changes == []


def test_selecting_the_other_view_fires_once(toggle):
    _, control, changes = toggle
    control._segments[DASHBOARD]._interactive._on_click()
    assert changes == [DASHBOARD]
    assert control.view == DASHBOARD
    assert control._segments[DASHBOARD]._selected is True
    assert control._segments[LIST]._selected is False


def test_reselecting_the_active_view_is_a_no_op(toggle):
    """Otherwise every stray click on the label would rebuild the view."""
    _, control, changes = toggle
    control._segments[LIST]._interactive._on_click()
    assert changes == []


def test_set_view_moves_the_selection_without_calling_back(toggle):
    _, control, changes = toggle
    control.set_view(DASHBOARD)
    assert control.view == DASHBOARD
    assert control._segments[DASHBOARD]._selected is True
    assert changes == []


def test_set_view_rejects_an_unknown_view(toggle):
    _, control, _ = toggle
    with pytest.raises(ValueError):
        control.set_view("gallery")


def test_selected_segment_carries_the_accent(toggle):
    _, control, _ = toggle
    selected = control._segments[LIST]
    other = control._segments[DASHBOARD]
    assert selected.cget("fg_color") == theme.SEG_ON_BG
    assert selected.cget("border_color") == theme.SEG_ON_BORDER
    assert selected._label.cget("text_color") == theme.SEG_ON_TEXT
    assert other.cget("fg_color") == theme.SEG_BG
    assert other._label.cget("text_color") == theme.SEG_OFF_TEXT


def test_hover_only_repaints_the_unselected_segment(toggle):
    """Hover is a colour change; the selected segment already has the accent."""
    _, control, _ = toggle
    other = control._segments[DASHBOARD]
    other._set_hover(True)
    assert other._label.cget("text_color") == theme.SEG_OFF_TEXT_HOVER
    other._set_hover(False)
    assert other._label.cget("text_color") == theme.SEG_OFF_TEXT


# ---------------------------------------------------------------------------
# Paint diffing
#
# `configure()` on a CustomTkinter widget redraws its canvas whether the value
# changed or not, so an unconditional repaint measured ~44ms per card against
# a 100ms pump. These assert the observable consequence -- a repaint that
# changes nothing touches no widget -- so removing the diff fails them.
# ---------------------------------------------------------------------------

def _count_configures(card):
    """Wrap the card's painted widgets and count configure() calls."""
    calls: list[str] = []

    def wrap(name, widget):
        original = widget.configure

        def counted(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)

        widget.configure = counted

    wrap("card", card)
    wrap("dot", card._dot)
    wrap("status", card._status)
    wrap("name", card._name)
    wrap("value", card._value)
    wrap("panel", card._panel)
    wrap("caption", card._caption)
    return calls


def test_a_repaint_that_changes_nothing_touches_no_widget(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        card = view.cards[manager.devices[0].id]
        calls = _count_configures(card)
        card.render()
        card.render()
        assert calls == []
    finally:
        root.destroy()


def test_a_temperature_change_repaints_only_the_temperature(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        calls = _count_configures(card)

        device.latest_temperature = 41.0
        card.render()

        assert "value" in calls
        assert "caption" in calls
        # The connection did not move, so its widgets must not be redrawn.
        assert "card" not in calls
        assert "dot" not in calls
        assert "name" not in calls
    finally:
        root.destroy()


def test_a_connection_change_repaints_only_the_status(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        device.latest_temperature = 41.0
        card.render()
        calls = _count_configures(card)

        device.connection = ConnectionState.CONNECTED
        card.render()

        assert "card" in calls        # the border ring
        assert "dot" in calls
        assert "status" in calls
        assert "value" not in calls
    finally:
        root.destroy()


def test_wording_updates_when_the_state_changes_within_one_treatment(tmp_path):
    """CONNECTING and STOPPING share the "idle" colour but not the wording."""
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        assert card._status.cget("text") == "Disconnected"

        device.connection = ConnectionState.CONNECTING
        card.render()
        assert card._status.cget("text") == "Connecting…"
        assert card.cget("border_color") == theme.CONN["idle"]["ring"]
    finally:
        root.destroy()


def test_force_repaints_a_screen_that_may_be_stale(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        card = view.cards[manager.devices[0].id]
        calls = _count_configures(card)
        card.render(force=True)
        assert calls, "force must repaint regardless of the diff"
    finally:
        root.destroy()


def test_a_renamed_device_repaints_its_name(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 1)
    try:
        device = manager.devices[0]
        card = view.cards[device.id]
        manager.rename_device(device.id, "Gantry East")
        card.render()
        assert card._name.cget("text") == "Gantry East"
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# View-host lifecycle
# ---------------------------------------------------------------------------

def test_a_hidden_view_declines_to_render(tmp_path):
    """A hidden 25-card grid must not repaint for nobody to see."""
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        device = manager.devices[0]
        view.hide()
        calls = _count_configures(view.cards[device.id])

        device.latest_temperature = 55.0
        assert view.render_device(device.id) is False
        assert calls == []
    finally:
        root.destroy()


def test_show_repaints_what_was_declined_while_hidden(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        device = manager.devices[0]
        view.hide()
        device.latest_temperature = 55.0
        view.render_device(device.id)      # declined, marks stale

        view.show()
        assert view.cards[device.id]._value.cget("text") == "55°"
    finally:
        root.destroy()


def test_show_picks_up_devices_added_while_hidden(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        view.hide()
        view.add_device(manager.add_device())
        view.on_device_count_changed(len(manager.devices))
        assert len(view.cards) == 3, "no rebuild while hidden"

        view.show()
        view.build_now()
        assert len(view.cards) == 4
    finally:
        root.destroy()


def test_on_device_count_changed_reflows_the_grid_while_visible(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 7)
    try:
        survivor = manager.devices[6].id
        victim = manager.devices[0].id
        manager.remove_device(victim)
        view.remove_device(victim)
        view.on_device_count_changed(len(manager.devices))
        view.build_now()
        assert view.cards[survivor].grid_info()["column"] == 0
        assert view.cards[survivor].grid_info()["row"] == 1
    finally:
        root.destroy()


def test_the_view_declares_its_padding_for_the_host(tmp_path):
    """Geometry is the host's to apply, the padding is the view's to choose.

    The shape is asserted by test_padding_is_a_mapping_the_host_can_splat;
    this one asserts the *reason* padding is per-view rather than a host
    constant -- the two views deliberately want different values, because
    ListView insets by 8 so its scrollbar sits in the page margin and this
    grid has no scrollbar to make room for.
    """
    from emcc.views.list_view import ListView
    root, _, view, _, _ = _view(tmp_path, 1)
    try:
        assert view.padding == {"padx": theme.PAGE_PAD_X,
                                "pady": theme.PAGE_PAD_Y}
        assert ListView.padding.fget(None) != view.padding
    finally:
        root.destroy()


def test_a_streamed_build_stops_when_the_shell_is_shutting_down(tmp_path):
    """Otherwise an `after` job fires into a torn-down interpreter."""
    root = _root()
    config = _config(tmp_path, 25)
    manager = DeviceManager(config, root.after, root.after_cancel)
    shutting = False
    view = DashboardView(root, manager=manager, on_clean=lambda i: None,
                         on_auto=lambda i: None,
                         is_shutting_down=lambda: shutting)
    view.pack(fill="both", expand=True)
    try:
        built = len(view.cards)
        assert view._pending
        shutting = True
        view._build_remaining()
        assert len(view.cards) == built, "no card built during shutdown"
        assert view._build_job is None
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# DeviceView contract conformance
#
# The host accepts any structurally-matching object and deliberately has no
# defensive `hasattr`, so a missing member raises inside the event pump
# rather than at registration. These fail at import-time cost instead.
# See .claude/parallel/VIEW_CONTRACT.md.
# ---------------------------------------------------------------------------

# The expected members are DERIVED from `DeviceView`, never transcribed.
# A hardcoded list is what let `caption` go unchecked: the list held eight
# names, `caption` was not among them, and the assertion was `callable(...)`
# -- presence where the contract specifies a *signature*. So the real defect
# (`caption(self)` against `caption(self, total)`) passed `hasattr` and
# `callable` and would have raised TypeError only at mount time.
#
# Deriving means a new contract method, or a changed signature, is caught
# without anyone remembering to update this file.


def _contract_members():
    """-> ({method name: Signature}, {property names}) read from DeviceView."""
    methods, properties = {}, set()
    for name, member in vars(DeviceView).items():
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            properties.add(name)
        elif callable(member):
            methods[name] = inspect.signature(member)
    return methods, properties


def test_the_contract_is_readable_and_non_trivial():
    """Guard the guard: an empty derivation would pass everything below."""
    methods, properties = _contract_members()
    assert len(methods) >= 8, methods
    assert "caption" in methods, "derivation missed caption -- the original bug"
    assert properties >= {"widget", "padding"}, properties


def test_every_contract_method_matches_the_contract_signature():
    methods, _ = _contract_members()
    for name, expected in methods.items():
        actual = getattr(DashboardView, name, None)
        assert callable(actual), f"{name} missing"
        got = inspect.signature(actual)
        # Parameter names and order, not annotations: an annotation that
        # differs is not a wiring fault, a missing parameter is.
        assert list(got.parameters) == list(expected.parameters), (
            f"{name}{got} does not match contract {name}{expected}"
        )


def test_every_contract_property_is_present():
    _, properties = _contract_members()
    for name in properties:
        assert isinstance(getattr(DashboardView, name, None), property), name


def test_padding_is_a_mapping_the_host_can_splat(tmp_path):
    """The host packs with `**view.padding` (views/host.py:112).

    A two-tuple satisfies "has a padding attribute" and then raises
    `TypeError: argument after ** must be a mapping` inside the host's pack
    call. It was a tuple until the seam landed.
    """
    root, _, view, _, _ = _view(tmp_path, 1)
    try:
        assert dict(**view.padding) == {"padx": theme.PAGE_PAD_X,
                                       "pady": theme.PAGE_PAD_Y}
    finally:
        root.destroy()


def test_the_view_is_its_own_widget(tmp_path):
    """The host packs `view.widget`; for this view that is the view."""
    root, _, view, _, _ = _view(tmp_path, 1)
    try:
        assert view.widget is view
    finally:
        root.destroy()


def test_set_devices_replaces_the_list_and_reconciles(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 5)
    try:
        keep = manager.devices[:2]
        view.set_devices(keep)
        view.build_now()
        assert len(view.cards) == 2
        assert [d.id for d in keep] == list(view.cards)
    finally:
        root.destroy()


def test_remove_device_drops_the_card_and_reflows(tmp_path):
    root, manager, view, _, _ = _view(tmp_path, 7)
    try:
        survivor = manager.devices[6].id
        view.remove_device(manager.devices[0].id)
        view.build_now()
        assert len(view.cards) == 6
        assert view.cards[survivor].grid_info()["row"] == 1
        assert view.cards[survivor].grid_info()["column"] == 0
    finally:
        root.destroy()


def test_add_device_is_idempotent(tmp_path):
    """The host may re-send on a dirty show; a duplicate must not double up."""
    root, manager, view, _, _ = _view(tmp_path, 2)
    try:
        extra = manager.add_device()
        view.add_device(extra)
        view.add_device(extra)
        view.build_now()
        assert len(view.cards) == 3
    finally:
        root.destroy()


def test_shutdown_cancels_the_build_without_destroying_the_widget(tmp_path):
    """`shutdown` is the host finishing with the view, not Tk tearing it down."""
    root, _, view, _, _ = _view(tmp_path, 25, complete=False)
    try:
        assert view._build_job is not None
        view.shutdown()
        assert view._build_job is None
        assert view.winfo_exists(), "shutdown must not destroy the widget"
    finally:
        root.destroy()


def test_the_build_job_is_scheduled_through_the_host_ctx(tmp_path):
    """So the host, not the widget, owns the timer the contract gave it."""
    root = _root()
    config = _config(tmp_path, 25)
    manager = DeviceManager(config, root.after, root.after_cancel)
    scheduled: list[int] = []
    cancelled: list[object] = []

    class Ctx:
        def is_shutting_down(self):
            return False

        def after(self, ms, fn):
            scheduled.append(ms)
            return root.after(ms, fn)

        def after_cancel(self, job):
            cancelled.append(job)
            root.after_cancel(job)

    view = DashboardView(root, Ctx(), manager=manager,
                         on_clean=lambda i: None, on_auto=lambda i: None)
    view.pack(fill="both", expand=True)
    try:
        assert scheduled == [theme.DASH_BUILD_INTERVAL_MS]
        view.shutdown()
        assert cancelled, "cancel must go through the host's ctx too"
    finally:
        root.destroy()


def test_ctx_supplies_the_shutdown_predicate(tmp_path):
    root = _root()
    config = _config(tmp_path, 25)
    manager = DeviceManager(config, root.after, root.after_cancel)

    class Ctx:
        stopping = False

        def is_shutting_down(self):
            return self.stopping

        def after(self, ms, fn):
            return root.after(ms, fn)

        def after_cancel(self, job):
            root.after_cancel(job)

    ctx = Ctx()
    view = DashboardView(root, ctx, manager=manager,
                         on_clean=lambda i: None, on_auto=lambda i: None)
    view.pack(fill="both", expand=True)
    try:
        built = len(view.cards)
        ctx.stopping = True
        view._build_remaining()
        assert len(view.cards) == built
    finally:
        root.destroy()


def test_a_view_built_without_a_host_still_works(tmp_path):
    """Tests and the capture scripts build it directly; it must not require a host."""
    root, _, view, _, _ = _view(tmp_path, 3)
    try:
        assert view._ctx.is_shutting_down() is False
        assert len(view.cards) == 3
    finally:
        root.destroy()


def test_a_long_device_name_cannot_push_the_status_chip_off_the_card(tmp_path):
    """Regression: a 40-character name used to take the whole header row.

    The status chip was packed after the expanding name frame, so it was
    shoved past the card's right edge and the device showed no connection
    state at all -- in the one view whose job is making that scannable.
    Found by the standalone preview, not by these tests.
    """
    root = _root()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": [
        {"device_name": "Gantry East Overhead Inspection Camera 12",
         "ip_address": ""},
        {"device_name": "C2", "ip_address": ""},
    ]}), encoding="utf-8")
    config = ConfigManager(path)
    config.load()
    manager = DeviceManager(config, root.after, root.after_cancel)
    view = DashboardView(root, manager=manager, on_clean=lambda i: None,
                         on_auto=lambda i: None)
    view.pack(fill="both", expand=True, padx=theme.PAGE_PAD_X,
              pady=theme.PAGE_PAD_Y)
    view.build_now()
    _pump(root, 3)
    try:
        long_card = view.cards[manager.devices[0].id]
        short_card = view.cards[manager.devices[1].id]

        # The chip is mapped, inside the card, and the same width as on a
        # card whose name is short.
        assert long_card._status.winfo_ismapped()
        assert long_card._status.cget("text") == "Disconnected"

        # The name is shortened rather than allowed to run on.
        shown = long_card._name.cget("text")
        assert shown != long_card._name_full
        assert shown.endswith("…")
        assert long_card._name_full.startswith(shown[:-1].rstrip())

        chip_right = (long_card._status.winfo_rootx()
                      + long_card._status.winfo_width())
        card_right = long_card.winfo_rootx() + long_card.winfo_width()
        assert chip_right <= card_right, (
            f"status chip runs {chip_right - card_right}px past the card edge"
        )
        # NOT an equality assertion on card width: two cards in adjacent
        # uniform columns differ by ~10px purely from how Tk distributes the
        # remainder across five columns. Measured with two SHORT names and
        # the same 532/522 split appears, so equality would be testing the
        # grid's arithmetic, not this card's behaviour.
        #
        # The real property is that the name never demands more width than
        # it has been given -- that is what would press on the column. With
        # the full name it requested 600 against 317 available.
        assert (long_card._name.winfo_reqwidth()
                <= long_card._name.winfo_width()), (
            "the name label is requesting more width than it was allocated"
        )
        assert (short_card._name.winfo_reqwidth()
                <= short_card._name.winfo_width())
    finally:
        root.destroy()


def test_caption_is_coherent_after_a_device_is_added_while_hidden(tmp_path):
    """Regression: the two halves of the caption came from different lists.

    `_order` is only rewritten by `sync()`, and `add_device` deliberately
    does not sync while hidden -- so the total lagged while the connected
    count did not, and the caption could read "3 devices, 4 connected".
    Both existing caption tests miss it because both read a freshly-synced
    visible view, which is the one state where the two sources agree.
    """
    root, manager, view, _, _ = _view(tmp_path, 3)
    try:
        view.hide()
        # Every device connected, so the stale total is genuinely exceeded by
        # the live connected count. With only one connected device the bug
        # still produced a wrong total, but the invariant below would not
        # have tripped -- 1 <= 3 -- so the test would have rested entirely on
        # the exact wording.
        for device in manager.devices:
            device.connection = ConnectionState.CONNECTED
        extra = manager.add_device()
        extra.connection = ConnectionState.CONNECTED
        view.add_device(extra)

        # No sync has run. The caption must still describe one fleet.
        caption = view.caption(len(manager.devices))
        assert caption == "4 devices · 4 connected · 4 of 25 slots", caption

        # The invariant, independent of the wording: there cannot be more
        # connected devices than devices. Before the fix this read
        # "3 devices · 4 connected".
        total = int(caption.split(" ", 1)[0])
        connected = int(caption.split("·")[1].strip().split(" ", 1)[0])
        assert connected <= total
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# hide() releases the streamed build
#
# Per VIEW_CONTRACT.md: anything on a timer stops when a view goes off screen.
# The Dashboard has no animation to release -- it registers no Pulse token --
# but a build in progress is the same waste, ~70ms per card for a grid nobody
# is looking at, competing with the view that IS on screen.
# ---------------------------------------------------------------------------

def test_hide_releases_the_streamed_build(tmp_path):
    root, _, view, _, _ = _view(tmp_path, 25, complete=False)
    try:
        assert view._build_job is not None
        built = len(view.cards)

        view.hide()
        assert view._build_job is None, "hide must cancel the queued build"

        # A job already queued when hide landed must not build one more card,
        # and must not re-schedule -- which would undo the cancellation.
        view._build_remaining()
        assert len(view.cards) == built
        assert view._build_job is None
    finally:
        root.destroy()


def test_show_resumes_a_build_that_hide_interrupted(tmp_path):
    """Asserted rather than assumed: `show` does not re-schedule directly,
    it relies on `sync()` recomputing the queue and calling `_schedule_build`.
    """
    root, _, view, _, _ = _view(tmp_path, 25, complete=False)
    try:
        view.hide()
        interrupted = len(view.cards)
        assert view._pending, "precondition: the build was not finished"

        view.show()
        assert view._build_job is not None, "show must resume the build"
        view.build_now()
        assert len(view.cards) == 25
        assert len(view.cards) > interrupted
    finally:
        root.destroy()


def test_heading_is_the_dashboard_subhead(tmp_path):
    """The view owns the heading words; the shell owns how they look.

    Added before `DeviceView` declares `heading`, so nothing derives a
    requirement for it yet -- this is the direct test that it exists and is
    right until the contract catches up, after which the derived property
    check covers its presence and this covers its value.
    """
    root, _, view, _, _ = _view(tmp_path, 1)
    try:
        assert view.heading == theme.DASH_SUBHEAD
        assert view.heading == "DASHBOARD OVERVIEW"

        # Raw text, NOT letter-spaced: shell/subheader.py applies
        # fonts.tracked(..., 0.12) at the render site. Returning a tracked
        # string here would double-space it once the shell reads this.
        assert HAIR_SPACE not in view.heading
        assert view.heading != fonts.tracked(view.heading, 0.12)

        # A property, so the derived conformance test can enforce it once
        # DeviceView declares it. An annotated attribute could not be.
        assert isinstance(getattr(DashboardView, "heading", None), property)
    finally:
        root.destroy()
