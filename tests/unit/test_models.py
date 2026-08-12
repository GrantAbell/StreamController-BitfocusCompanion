"""Domain model tests."""

import pytest

from companion import constants
from companion.models import (
    CompanionAddress,
    CompanionCapabilities,
    CompanionConfigError,
    CompanionConnectionSettings,
    ConnectionState,
    SubscriptionEntry,
)


# --- Coordinate conversion -------------------------------------------------


def test_ui_coordinates_are_passed_through_unchanged():
    """Row and column are 0-based end-to-end; no conversion occurs."""
    address = CompanionAddress.from_ui(dynamic_page=False, page=4, row=2, column=3)

    assert address.page == 4  # page numbering is left alone
    assert address.row == 2
    assert address.column == 3


def test_ui_origin_maps_to_internal_origin():
    address = CompanionAddress.from_ui(dynamic_page=False, page=1, row=0, column=0)
    assert (address.row, address.column) == (0, 0)


def test_dynamic_address_has_no_page():
    address = CompanionAddress.from_ui(dynamic_page=True, page=7, row=0, column=0)

    assert address.dynamic_page is True
    assert address.page is None, "a configured page must be ignored in dynamic mode"


def test_static_address_keeps_its_page():
    address = CompanionAddress.from_ui(dynamic_page=False, page=3, row=2, column=4)
    assert address.page == 3


def test_coordinates_accept_strings_from_settings():
    """Settings arrive from JSON and UI widgets, so strings are normal."""
    address = CompanionAddress.from_ui(
        dynamic_page=False, page="4", row="2", column="3"
    )
    assert (address.page, address.row, address.column) == (4, 2, 3)


# --- Validation ------------------------------------------------------------


@pytest.mark.parametrize("bad_row", [-1, -2, -100])
def test_row_below_zero_is_rejected(bad_row):
    with pytest.raises(CompanionConfigError, match="Row"):
        CompanionAddress.from_ui(dynamic_page=True, row=bad_row, column=0)


@pytest.mark.parametrize("bad_column", [-1, -2, -100])
def test_column_below_zero_is_rejected(bad_column):
    with pytest.raises(CompanionConfigError, match="Column"):
        CompanionAddress.from_ui(dynamic_page=True, row=0, column=bad_column)


@pytest.mark.parametrize("bad_page", [0, -1])
def test_page_below_one_is_rejected(bad_page):
    with pytest.raises(CompanionConfigError, match="Page"):
        CompanionAddress.from_ui(dynamic_page=False, page=bad_page, row=0, column=0)


@pytest.mark.parametrize("bad_value", ["", "  ", "abc", "1.5", None, [], {}])
def test_non_numeric_coordinates_are_rejected(bad_value):
    with pytest.raises(CompanionConfigError):
        CompanionAddress.from_ui(dynamic_page=True, row=bad_value, column=0)


def test_booleans_are_not_accepted_as_coordinates():
    """bool is an int subclass; True must not silently become row 1."""
    with pytest.raises(CompanionConfigError, match="boolean"):
        CompanionAddress.from_ui(dynamic_page=True, row=True, column=0)


def test_page_beyond_sanity_limit_is_rejected():
    with pytest.raises(CompanionConfigError, match="Page"):
        CompanionAddress.from_ui(
            dynamic_page=False, page=constants.MAX_PAGE + 1, row=0, column=0
        )


def test_coordinates_beyond_sanity_limit_are_rejected():
    with pytest.raises(CompanionConfigError, match="Row"):
        CompanionAddress.from_ui(
            dynamic_page=True, row=constants.MAX_SURFACE_ROWS, column=0
        )


def test_static_address_without_page_is_rejected():
    with pytest.raises(CompanionConfigError, match="requires a page"):
        CompanionAddress(dynamic_page=False, page=None, row=0, column=0)


def test_dynamic_address_with_page_is_rejected():
    with pytest.raises(CompanionConfigError, match="must not carry a page"):
        CompanionAddress(dynamic_page=True, page=3, row=0, column=0)


# --- Hashability and identity ---------------------------------------------


def test_equal_addresses_share_a_dict_slot():
    """This is what makes subscription deduplication work."""
    a = CompanionAddress.from_ui(dynamic_page=False, page=3, row=2, column=4)
    b = CompanionAddress.from_ui(dynamic_page=False, page=3, row=2, column=4)

    assert a == b
    assert hash(a) == hash(b)
    assert len({a: 1, b: 2}) == 1


def test_dynamic_and_static_addresses_are_distinct():
    dynamic = CompanionAddress.from_ui(dynamic_page=True, row=2, column=4)
    static = CompanionAddress.from_ui(dynamic_page=False, page=1, row=2, column=4)

    assert dynamic != static
    assert len({dynamic, static}) == 2


def test_addresses_are_immutable():
    address = CompanionAddress.from_ui(dynamic_page=True, row=0, column=0)
    with pytest.raises(Exception):
        address.row = 5  # type: ignore[misc]


# --- Wire representations --------------------------------------------------


def test_sub_id_uses_companion_convention():
    """page is 1-based, row/column 0-based — matches upstream's SUBID."""
    address = CompanionAddress.from_ui(dynamic_page=False, page=3, row=1, column=4)
    assert address.sub_id == "3/1/4"


def test_dynamic_address_has_no_sub_id():
    address = CompanionAddress.from_ui(dynamic_page=True, row=0, column=0)
    with pytest.raises(CompanionConfigError, match="no subscription id"):
        _ = address.sub_id


def test_key_index_is_row_major():
    address = CompanionAddress.from_ui(dynamic_page=True, row=2, column=3)
    # row 2, column 3 on an 8-wide surface
    assert address.key_index(8) == 2 * 8 + 3


def test_key_index_round_trips():
    original = CompanionAddress.from_ui(dynamic_page=True, row=2, column=3)
    index = original.key_index(8)

    assert CompanionAddress.from_key_index(index, 8) == original


def test_key_index_rejects_column_wider_than_surface():
    address = CompanionAddress.from_ui(dynamic_page=True, row=0, column=9)
    with pytest.raises(CompanionConfigError, match="does not fit"):
        address.key_index(8)


def test_key_index_depends_on_surface_width():
    """A resize renumbers every key — the same address maps elsewhere."""
    address = CompanionAddress.from_ui(dynamic_page=True, row=1, column=0)
    assert address.key_index(8) == 8
    assert address.key_index(11) == 11


def test_describe_is_log_friendly():
    assert (
        CompanionAddress.from_ui(dynamic_page=False, page=3, row=1, column=4).describe()
        == "static/3/1/4"
    )
    assert (
        CompanionAddress.from_ui(dynamic_page=True, row=1, column=4).describe()
        == "dynamic/1/4"
    )


# --- Capabilities ----------------------------------------------------------


def test_caps_parsing_from_live_companion():
    """Exactly what Companion 4.3.4 / API 1.10.1 sends: no BITMAP_FORMATS."""
    caps = CompanionCapabilities.from_caps_params({"SUBSCRIPTIONS": "1"})

    assert caps.subscriptions is True
    assert caps.bitmap_formats == frozenset()
    assert caps.negotiated_bitmap_format == constants.RAW_BITMAP_FORMAT
    assert caps.uses_raw_bitmaps is True


def test_caps_negotiates_png_when_offered():
    caps = CompanionCapabilities.from_caps_params(
        {"SUBSCRIPTIONS": "1", "BITMAP_FORMATS": "png,webp"}
    )

    assert caps.negotiated_bitmap_format == "png"
    assert caps.uses_raw_bitmaps is False


def test_caps_never_negotiates_a_format_we_cannot_decode():
    """webp is offered but unsupported, so we must fall back to raw."""
    caps = CompanionCapabilities.from_caps_params({"BITMAP_FORMATS": "webp,jpeg"})
    assert caps.negotiated_bitmap_format == constants.RAW_BITMAP_FORMAT


def test_caps_tolerates_whitespace_and_case():
    caps = CompanionCapabilities.from_caps_params({"BITMAP_FORMATS": " PNG , webp "})
    assert "png" in caps.bitmap_formats
    assert caps.negotiated_bitmap_format == "png"


def test_missing_subscriptions_capability_defaults_off():
    caps = CompanionCapabilities.from_caps_params({})
    assert caps.subscriptions is False


def test_subscriptions_flag_requires_exactly_one():
    assert CompanionCapabilities.from_caps_params({"SUBSCRIPTIONS": "0"}).subscriptions is False


# --- Connection state ------------------------------------------------------


def test_only_connected_is_usable():
    usable = [s for s in ConnectionState if s.is_usable]
    assert usable == [ConnectionState.CONNECTED]


def test_terminal_states_do_not_invite_reconnection():
    assert ConnectionState.INCOMPATIBLE.is_terminal
    assert ConnectionState.CONFIG_ERROR.is_terminal
    assert not ConnectionState.DISCONNECTED.is_terminal


# --- Connection settings ---------------------------------------------------


def test_settings_round_trip_from_plugin_dict():
    settings = CompanionConnectionSettings.from_settings(
        {
            "connection_mode": "satellite_tcp",
            "satellite_host": "192.168.50.245",
            "satellite_port": 16622,
            "device_suffix": "7a7557",
            "debug_logging": True,
        }
    )

    assert settings.endpoint == "192.168.50.245:16622"
    assert settings.device_id == "streamcontroller-companion-7a7557"
    assert settings.debug_logging is True


def test_settings_apply_defaults_when_empty():
    settings = CompanionConnectionSettings.from_settings({})

    assert settings.host == constants.DEFAULT_HOST
    assert settings.port == constants.DEFAULT_PORT
    assert settings.mode == constants.MODE_SATELLITE_TCP


def test_settings_compare_by_value_so_changes_are_detectable():
    """Reconnect-on-change is a plain equality check."""
    a = CompanionConnectionSettings.from_settings({"satellite_host": "10.0.0.1"})
    b = CompanionConnectionSettings.from_settings({"satellite_host": "10.0.0.1"})
    c = CompanionConnectionSettings.from_settings({"satellite_host": "10.0.0.2"})

    assert a == b
    assert a != c


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 999999])
def test_invalid_ports_are_rejected(bad_port):
    with pytest.raises(CompanionConfigError, match="Port"):
        CompanionConnectionSettings.from_settings({"satellite_port": bad_port})


@pytest.mark.parametrize("bad_host", ["", "   "])
def test_empty_host_is_rejected(bad_host):
    with pytest.raises(CompanionConfigError, match="Host"):
        CompanionConnectionSettings.from_settings({"satellite_host": bad_host})


def test_non_numeric_port_is_rejected():
    with pytest.raises(CompanionConfigError, match="Port"):
        CompanionConnectionSettings.from_settings({"satellite_port": "not-a-port"})


def test_hostnames_are_accepted():
    """localhost, hostnames and LAN IPs must all work."""
    for host in ("localhost", "127.0.0.1", "companion.local", "192.168.1.50"):
        assert CompanionConnectionSettings.from_settings({"satellite_host": host}).host == host


# --- Subscription entries --------------------------------------------------


def _entry() -> SubscriptionEntry:
    return SubscriptionEntry(
        address=CompanionAddress.from_ui(dynamic_page=False, page=3, row=2, column=4)
    )


def test_first_listener_requests_a_network_subscription():
    entry = _entry()
    assert entry.add_listener("a") is True
    assert entry.listener_count == 1


def test_second_and_third_listeners_do_not():
    """Three actions on one address, one subscription."""
    entry = _entry()
    entry.add_listener("a")

    assert entry.add_listener("b") is False
    assert entry.add_listener("c") is False
    assert entry.listener_count == 3


def test_removing_a_middle_listener_keeps_the_subscription():
    entry = _entry()
    for name in ("a", "b", "c"):
        entry.add_listener(name)

    assert entry.remove_listener("b") is False
    assert entry.remove_listener("a") is False
    assert entry.listener_count == 1


def test_removing_the_final_listener_drops_the_subscription():
    entry = _entry()
    entry.add_listener("a")
    assert entry.remove_listener("a") is True
    assert entry.listener_count == 0


def test_removing_an_unknown_listener_is_harmless():
    entry = _entry()
    entry.add_listener("a")
    assert entry.remove_listener("never-added") is False
    assert entry.listener_count == 1


def test_adding_the_same_listener_twice_counts_once():
    """Guards against a re-registered action leaking the listener count."""
    entry = _entry()
    entry.add_listener("a")
    entry.add_listener("a")

    assert entry.listener_count == 1
    assert entry.remove_listener("a") is True


def test_invalidating_the_image_leaves_listeners_alone():
    entry = _entry()
    entry.add_listener("a")
    entry.cached_image = object()
    entry.last_update = 123.0

    entry.invalidate_image()

    assert entry.cached_image is None
    assert entry.last_update is None
    assert entry.listener_count == 1
