"""Dynamic virtual surface tests."""

from __future__ import annotations

import pytest

from companion import constants
from companion.models import (
    CompanionAddress,
    CompanionCapabilities,
    CompanionConfigError,
)
from companion.surface import DynamicSurface, SurfaceGeometry

CAPS_RAW = CompanionCapabilities(subscriptions=True)
CAPS_PNG = CompanionCapabilities(subscriptions=True, bitmap_formats=frozenset({"png"}))


class SendLog:
    """Captures serialized messages so tests can assert on the wire form."""

    def __init__(self, accept: bool = True) -> None:
        self.sent: list[str] = []
        self.accept = accept

    def __call__(self, payload: bytes) -> bool:
        if self.accept:
            self.sent.append(payload.decode().strip())
        return self.accept

    def commands(self) -> list[str]:
        return [line.split(" ")[0] for line in self.sent]

    def last(self) -> str:
        return self.sent[-1]

    def clear(self) -> None:
        self.sent.clear()


def _surface(send: SendLog, connected: bool = True) -> DynamicSurface:
    surface = DynamicSurface("streamcontroller-companion-test", send)
    if connected:
        surface.on_connected(CAPS_RAW)
    return surface


def _dyn(row: int, column: int) -> CompanionAddress:
    """A dynamic address with 0-based row and column."""
    return CompanionAddress.from_ui(dynamic_page=True, row=row, column=column)


# --- Geometry --------------------------------------------------------------


class TestGeometry:
    def test_minimum_surface_is_four_by_eight(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))

        assert surface.registered_geometry == SurfaceGeometry(4, 8)
        assert "KEYS_TOTAL=32" in send.last()
        assert "KEYS_PER_ROW=8" in send.last()

    def test_small_coordinates_do_not_shrink_below_the_minimum(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.require(_dyn(1, 1))

        assert surface.registered_geometry == SurfaceGeometry(4, 8)

    def test_surface_grows_to_cover_a_larger_coordinate(self):
        """Worked example: row index 5, column index 10 needs >= 6x11."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        surface.require(_dyn(5, 10))

        geometry = surface.registered_geometry
        assert geometry.rows >= 6
        assert geometry.columns >= 11
        assert geometry == SurfaceGeometry(6, 11)

    def test_growth_removes_then_re_adds_the_device(self):
        """The protocol has no in-place resize (Q5)."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        surface.require(_dyn(5, 10))

        assert send.commands() == ["REMOVE-DEVICE", "ADD-DEVICE"]
        assert "KEYS_PER_ROW=11" in send.last()
        assert "KEYS_TOTAL=66" in send.last()

    def test_growth_happens_only_once_for_the_same_size(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))
        send.clear()

        surface.require(_dyn(1, 2))
        surface.require(_dyn(5, 10))

        assert send.sent == [], "no re-registration for coordinates already covered"

    def test_growth_is_monotonic_across_dimensions(self):
        """Growing rows must not silently shrink columns."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 19))
        assert surface.registered_geometry.columns == 20

        surface.require(_dyn(8, 0))

        geometry = surface.registered_geometry
        assert geometry.rows == 9
        assert geometry.columns == 20, "columns must not regress"

    def test_releasing_a_coordinate_does_not_shrink(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))
        send.clear()

        surface.release(_dyn(5, 10))

        assert send.sent == []
        assert surface.registered_geometry == SurfaceGeometry(6, 11)

    def test_key_count_matches_geometry(self):
        assert SurfaceGeometry(4, 8).key_count == 32
        assert SurfaceGeometry(6, 11).key_count == 66


# --- Registration ----------------------------------------------------------


class TestRegistration:
    def test_nothing_is_registered_before_a_coordinate_is_required(self):
        send = SendLog()
        surface = _surface(send)

        assert send.sent == []
        assert not surface.is_registered

    def test_nothing_is_sent_while_disconnected(self):
        send = SendLog()
        surface = _surface(send, connected=False)

        surface.require(_dyn(0, 0))

        assert send.sent == []
        assert not surface.is_registered

    def test_requirements_are_registered_on_connect(self):
        """Coordinates configured while offline must register once connected."""
        send = SendLog()
        surface = _surface(send, connected=False)
        surface.require(_dyn(5, 10))

        surface.on_connected(CAPS_RAW)

        assert send.commands() == ["ADD-DEVICE"]
        assert "KEYS_PER_ROW=11" in send.last()

    def test_reconnect_re_registers_the_surface(self):
        """The device must be recreated after every reconnect."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))
        surface.on_disconnected()
        assert not surface.is_registered
        send.clear()

        surface.on_connected(CAPS_RAW)

        assert send.commands() == ["ADD-DEVICE"]
        assert "KEYS_PER_ROW=11" in send.last(), "dimensions must be recalculated"

    def test_reconnect_does_not_send_remove_device(self):
        """Companion has no memory of our old device on a fresh connection."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.on_disconnected()
        send.clear()

        surface.on_connected(CAPS_RAW)

        assert "REMOVE-DEVICE" not in send.commands()

    def test_negotiated_bitmap_format_is_requested(self):
        send = SendLog()
        surface = DynamicSurface("dev", send)
        surface.on_connected(CAPS_PNG)
        surface.require(_dyn(0, 0))

        assert 'BITMAP_FORMAT="png"' in send.last()

    def test_raw_format_omits_the_bitmap_format_argument(self):
        """Omitting it keeps Companion on its default rgb encoding."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))

        assert "BITMAP_FORMAT" not in send.last()

    def test_failed_send_leaves_the_surface_unregistered(self):
        """A send refused because we are disconnected must not be recorded."""
        send = SendLog(accept=False)
        surface = DynamicSurface("dev", send)
        surface.on_connected(CAPS_RAW)

        surface.require(_dyn(0, 0))

        assert not surface.is_registered

    def test_unregister_removes_the_device(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        surface.unregister()

        assert send.commands() == ["REMOVE-DEVICE"]
        assert not surface.is_registered

    def test_unregister_is_a_no_op_when_not_registered(self):
        send = SendLog()
        surface = _surface(send)

        surface.unregister()

        assert send.sent == []

    def test_page_navigation_registers_a_surface_with_no_dynamic_buttons(self):
        """A layout whose only Companion control is a page key still needs a device.

        Companion resolves CHANGE-PAGE by DEVICEID, so without this the key
        would be inert for reasons invisible to the user.
        """
        send = SendLog()
        surface = _surface(send)

        surface.require_page_navigation(object())

        assert surface.is_registered
        assert send.commands() == ["ADD-DEVICE"]

    def test_page_navigation_hold_is_idempotent(self):
        send = SendLog()
        surface = _surface(send)
        client = object()

        surface.require_page_navigation(client)
        send.clear()
        surface.require_page_navigation(client)

        assert send.sent == []

    def test_page_navigation_survives_a_reconnect(self):
        """Companion forgets our device, so the hold must recreate it."""
        send = SendLog()
        surface = _surface(send)
        surface.require_page_navigation(object())
        surface.on_disconnected()
        send.clear()

        surface.on_connected(CAPS_RAW)

        assert surface.is_registered
        assert send.commands() == ["ADD-DEVICE"]

    def test_releasing_the_last_hold_stops_recreating_the_surface(self):
        send = SendLog()
        surface = _surface(send)
        client = object()
        surface.require_page_navigation(client)

        surface.release_page_navigation(client)
        surface.on_disconnected()
        send.clear()
        surface.on_connected(CAPS_RAW)

        assert not surface.is_registered
        assert send.sent == []

    def test_a_remaining_hold_keeps_the_surface(self):
        send = SendLog()
        surface = _surface(send)
        first, second = object(), object()
        surface.require_page_navigation(first)
        surface.require_page_navigation(second)

        surface.release_page_navigation(first)
        surface.on_disconnected()
        send.clear()
        surface.on_connected(CAPS_RAW)

        assert surface.is_registered

    def test_change_page_sends_the_direction(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        assert surface.change_page(True) is True
        assert send.last() == (
            'CHANGE-PAGE DEVICEID="streamcontroller-companion-test" DIRECTION=1'
        )

        assert surface.change_page(False) is True
        assert "DIRECTION=0" in send.last()

    def test_change_page_needs_a_registered_device(self):
        """Companion errors on a DEVICEID it has never seen; do not send one."""
        send = SendLog()
        surface = _surface(send)

        assert surface.change_page(True) is False
        assert send.sent == []

    def test_change_page_is_dropped_while_disconnected(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.on_disconnected()
        send.clear()

        assert surface.change_page(True) is False
        assert send.sent == []

    def test_static_addresses_are_rejected(self):
        send = SendLog()
        surface = _surface(send)
        static = CompanionAddress.from_ui(dynamic_page=False, page=3, row=0, column=0)

        with pytest.raises(CompanionConfigError, match="dynamic"):
            surface.require(static)


# --- Address mapping -------------------------------------------------------


class TestAddressMapping:
    def test_key_index_decodes_row_major(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))

        assert surface.address_for_key_index(0) == _dyn(0, 0)
        assert surface.address_for_key_index(7) == _dyn(0, 7)
        assert surface.address_for_key_index(8) == _dyn(1, 0)
        assert surface.address_for_key_index(31) == _dyn(3, 7)

    def test_decoding_uses_the_registered_width_after_growth(self):
        """A resize renumbers every key; decoding must follow."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        assert surface.address_for_key_index(8) == _dyn(1, 0)

        surface.require(_dyn(0, 10))  # widen to 11 columns

        assert surface.address_for_key_index(8) == _dyn(0, 8)
        assert surface.address_for_key_index(11) == _dyn(1, 0)

    def test_decoding_before_registration_returns_none(self):
        """A KEY-STATE from before our ADD-DEVICE cannot be placed."""
        send = SendLog()
        surface = _surface(send)

        assert surface.address_for_key_index(3) is None

    def test_decoding_a_negative_index_is_rejected(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))

        assert surface.address_for_key_index(-1) is None

    def test_encoding_round_trips(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))

        for address in (_dyn(0, 0), _dyn(2, 4), _dyn(5, 10)):
            index = surface.key_index_for(address)
            assert index is not None
            assert surface.address_for_key_index(index) == address

    def test_encoding_before_registration_returns_none(self):
        send = SendLog()
        surface = _surface(send)

        assert surface.key_index_for(_dyn(0, 0)) is None

    def test_encoding_an_uncovered_address_returns_none(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))  # 4x8

        assert surface.key_index_for(_dyn(8, 0)) is None
        assert surface.key_index_for(_dyn(0, 19)) is None


# --- Requirement bookkeeping ----------------------------------------------


class TestRequirements:
    def test_required_addresses_are_tracked(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.require(_dyn(1, 2))

        assert surface.required_addresses == {_dyn(0, 0), _dyn(1, 2)}

    def test_releasing_removes_the_requirement(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.release(_dyn(0, 0))

        assert surface.required_addresses == set()

    def test_releasing_an_unknown_address_is_harmless(self):
        send = SendLog()
        surface = _surface(send)

        surface.release(_dyn(3, 3))

    def test_requirements_survive_a_disconnect(self):
        """They are what the reconnect re-registers from."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))

        surface.on_disconnected()

        assert surface.required_addresses == {_dyn(5, 10)}

    def test_clear_requirements_empties_the_set(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))

        surface.clear_requirements()

        assert surface.required_addresses == set()


# --- Refresh ---------------------------------------------------------------


class TestRefresh:
    """Companion only pushes imagery on change or on device registration, so a
    control appearing on an already-registered surface needs an explicit nudge
    or it never receives its first image."""

    def test_refresh_re_registers_the_device(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        assert surface.refresh() is True
        assert send.commands() == ["REMOVE-DEVICE", "ADD-DEVICE"]

    def test_refresh_keeps_the_same_geometry(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(5, 10))
        geometry = surface.registered_geometry
        send.clear()

        surface.refresh()

        assert surface.registered_geometry == geometry
        assert "KEYS_PER_ROW=11" in send.last()

    def test_refresh_is_rate_limited(self):
        """A page of 32 controls appearing at once must not cause 32 refreshes."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        send.clear()

        assert surface.refresh() is True
        for _ in range(10):
            assert surface.refresh() is False

        assert send.commands() == ["REMOVE-DEVICE", "ADD-DEVICE"]

    def test_refresh_does_nothing_when_unregistered(self):
        send = SendLog()
        surface = _surface(send)

        assert surface.refresh() is False
        assert send.sent == []

    def test_refresh_does_nothing_while_disconnected(self):
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.on_disconnected()
        send.clear()

        assert surface.refresh() is False
        assert send.sent == []

    def test_reconnect_clears_the_rate_limit(self):
        """A fresh connection must be able to refresh immediately."""
        send = SendLog()
        surface = _surface(send)
        surface.require(_dyn(0, 0))
        surface.refresh()

        surface.on_disconnected()
        surface.on_connected(CAPS_RAW)
        send.clear()

        assert surface.refresh() is True
