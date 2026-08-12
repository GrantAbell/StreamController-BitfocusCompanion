"""The virtual Companion surface used by dynamic-page buttons.

Dynamic mode means "this control is row X / column Y on whatever page Companion
is currently showing". Companion delivers that imagery to a registered surface,
so the plugin registers one device covering every dynamic coordinate in use.

Geometry rules:

* 4x8 is a **minimum**, never a maximum.
* The surface grows to cover any configured coordinate and is re-registered.
* It never shrinks — matching upstream, which considers shrinking disruptive.

Resizing is `REMOVE-DEVICE` followed by a fresh `ADD-DEVICE`; the protocol has
no in-place resize (confirmed against upstream, Q5).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import constants, protocol
from .models import CompanionAddress, CompanionCapabilities, CompanionConfigError

log = logging.getLogger(__name__)

Sender = Callable[[bytes], bool]


@dataclass(frozen=True)
class SurfaceGeometry:
    rows: int
    columns: int

    @property
    def key_count(self) -> int:
        return self.rows * self.columns

    def covers(self, address: CompanionAddress) -> bool:
        return address.row < self.rows and address.column < self.columns

    def describe(self) -> str:
        return f"{self.rows}x{self.columns}"


class DynamicSurface:
    """Tracks which dynamic coordinates are needed and keeps Companion in sync."""

    def __init__(self, device_id: str, send: Sender) -> None:
        self._device_id = device_id
        self._send = send

        self._lock = threading.RLock()
        self._required: set[CompanionAddress] = set()

        # Geometry Companion currently knows about. None until ADD-DEVICE is
        # sent; KEY-STATE cannot be decoded before that because the flat key
        # index depends on the registered width.
        self._registered: SurfaceGeometry | None = None
        self._capabilities = CompanionCapabilities()
        self._connected = False
        self._last_refresh = 0.0

    # --- State -------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def registered_geometry(self) -> SurfaceGeometry | None:
        with self._lock:
            return self._registered

    @property
    def is_registered(self) -> bool:
        return self.registered_geometry is not None

    @property
    def required_addresses(self) -> set[CompanionAddress]:
        with self._lock:
            return set(self._required)

    def set_device_id(self, device_id: str) -> None:
        with self._lock:
            self._device_id = device_id

    # --- Geometry ----------------------------------------------------------

    def _needed_geometry(self) -> SurfaceGeometry:
        """Smallest surface covering every required address, at least 4x8.

        Also never smaller than what is already registered, so growth is
        monotonic and previously delivered key indexes stay meaningful.
        """
        rows = constants.MIN_SURFACE_ROWS
        columns = constants.MIN_SURFACE_COLUMNS

        for address in self._required:
            rows = max(rows, address.row + 1)
            columns = max(columns, address.column + 1)

        if self._registered is not None:
            rows = max(rows, self._registered.rows)
            columns = max(columns, self._registered.columns)

        return SurfaceGeometry(rows=rows, columns=columns)

    # --- Requirements ------------------------------------------------------

    def require(self, address: CompanionAddress) -> None:
        """Declare that a dynamic coordinate is in use, growing if needed."""
        if not address.dynamic_page:
            raise CompanionConfigError(
                "Only dynamic addresses belong to the virtual surface"
            )

        with self._lock:
            if address in self._required:
                return
            self._required.add(address)
            log.debug("Dynamic surface requires %s", address.describe())
            self._sync_locked()

    def release(self, address: CompanionAddress) -> None:
        """Stop requiring a coordinate.

        The surface is deliberately not shrunk: re-registering a smaller device
        would churn every key index and make Companion redraw everything, for
        no benefit.
        """
        with self._lock:
            self._required.discard(address)

    def clear_requirements(self) -> None:
        with self._lock:
            self._required.clear()

    # --- Connection lifecycle ---------------------------------------------

    def on_connected(self, capabilities: CompanionCapabilities) -> None:
        """Register the surface from scratch on a new connection.

        Called after every successful handshake, including reconnects, so the
        device is always recreated with correct dimensions.
        """
        with self._lock:
            self._capabilities = capabilities
            self._connected = True
            self._last_refresh = 0.0
            # Forget the old registration: this is a new connection and
            # Companion knows nothing about our previous device.
            self._registered = None
            if self._required:
                self._register_locked(self._needed_geometry())

    def on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._registered = None

    # --- Registration ------------------------------------------------------

    def _sync_locked(self) -> None:
        if not self._connected:
            return

        needed = self._needed_geometry()
        current = self._registered

        if current is None:
            self._register_locked(needed)
            return

        if needed.rows > current.rows or needed.columns > current.columns:
            log.info(
                "Growing Companion surface %s -> %s",
                current.describe(),
                needed.describe(),
            )
            # No in-place resize exists; drop and re-add.
            self._send(protocol.remove_device(self._device_id))
            self._register_locked(needed)

    def _register_locked(self, geometry: SurfaceGeometry) -> None:
        bitmap_format = self._capabilities.negotiated_bitmap_format
        payload = protocol.add_device(
            self._device_id,
            rows=geometry.rows,
            columns=geometry.columns,
            bitmap_format=bitmap_format,
        )
        if not self._send(payload):
            log.warning("Could not register Companion surface; not connected")
            return

        self._registered = geometry
        log.info(
            "Registered Companion surface %s as %s (%d keys, %s bitmaps)",
            self._device_id,
            geometry.describe(),
            geometry.key_count,
            bitmap_format,
        )

    def refresh(self) -> bool:
        """Make Companion resend imagery for the whole surface.

        Companion only pushes KEY-STATE when a button changes or when a device
        is registered. If the surface already covers a newly required address,
        nothing is sent and no imagery arrives, leaving that control stuck in
        its loading state until the button happens to change. Re-registering is
        the only way to ask for a fresh set, so this drops and re-adds the
        device, rate-limited so a page full of controls cannot cause a storm.
        """
        with self._lock:
            if not self._connected or self._registered is None:
                return False

            now = time.monotonic()
            if now - self._last_refresh < constants.SURFACE_REFRESH_MIN_INTERVAL:
                return False
            self._last_refresh = now

            geometry = self._registered
            log.debug("Refreshing Companion surface to re-request imagery")
            self._send(protocol.remove_device(self._device_id))
            self._registered = None
            self._register_locked(geometry)
            return True

    def unregister(self) -> None:
        """Remove the surface from Companion, e.g. on shutdown."""
        with self._lock:
            if self._registered is None:
                return
            self._registered = None
        self._send(protocol.remove_device(self._device_id))

    # --- Address mapping ---------------------------------------------------

    def address_for_key_index(self, key_index: int) -> CompanionAddress | None:
        """Decode a KEY-STATE key index using the registered width.

        Returns None when nothing is registered yet — a message from before our
        ADD-DEVICE cannot be interpreted, and guessing a width would put the
        image on the wrong button.
        """
        geometry = self.registered_geometry
        if geometry is None:
            return None
        try:
            return CompanionAddress.from_key_index(key_index, geometry.columns)
        except CompanionConfigError:
            log.debug("Ignoring out-of-range KEY-STATE index %s", key_index)
            return None

    def key_index_for(self, address: CompanionAddress) -> int | None:
        """Encode a dynamic address as a flat key index for KEY-PRESS/ROTATE."""
        geometry = self.registered_geometry
        if geometry is None or not geometry.covers(address):
            return None
        try:
            return address.key_index(geometry.columns)
        except CompanionConfigError:
            return None
