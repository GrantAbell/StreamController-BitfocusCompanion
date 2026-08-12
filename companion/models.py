"""Domain models for the Companion integration.

Pure data and validation — no sockets, no StreamController, no I/O. Everything
here is independently testable.

Coordinate conventions:

* **Page** is 1-based throughout (Companion's own convention).
* **Row and column** are 0-based throughout — both in the UI and on the wire.
  No conversion occurs anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import constants


class CompanionConfigError(ValueError):
    """Raised when user-supplied configuration cannot form a valid address.

    Callers surface this as an "invalid configuration" visual state rather than
    sending a malformed command.
    """


def _coerce_int(value: Any, field_name: str) -> int:
    """Convert a settings value to ``int``, rejecting anything ambiguous.

    Settings arrive from JSON and from UI widgets, so a coordinate may be a
    string. ``bool`` is rejected explicitly because it is an ``int`` subclass
    and ``True`` silently becoming column 1 would be a nasty bug.
    """
    if isinstance(value, bool):
        raise CompanionConfigError(f"{field_name} must be a number, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise CompanionConfigError(f"{field_name} must not be empty")
        try:
            return int(text)
        except ValueError:
            raise CompanionConfigError(
                f"{field_name} must be a whole number, got {value!r}"
            ) from None
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise CompanionConfigError(f"{field_name} must be a whole number, got {value!r}")
    raise CompanionConfigError(f"{field_name} must be a number, got {type(value).__name__}")


# --- Address ---------------------------------------------------------------


@dataclass(frozen=True)
class CompanionAddress:
    """One logical Companion button location.

    Immutable and hashable so it can key the subscription registry directly —
    two actions pointing at the same button produce equal addresses and
    therefore share one subscription.

    ``row`` and ``column`` are 0-based. ``page`` is Companion's own 1-based page
    number, or ``None`` when following the active page.
    """

    dynamic_page: bool
    page: int | None
    row: int
    column: int

    def __post_init__(self) -> None:
        if self.dynamic_page:
            if self.page is not None:
                raise CompanionConfigError(
                    "A dynamic-page address must not carry a page number"
                )
        else:
            if self.page is None:
                raise CompanionConfigError("A static address requires a page number")
            if self.page < 1:
                raise CompanionConfigError(f"Page must be at least 1, got {self.page}")
            if self.page > constants.MAX_PAGE:
                raise CompanionConfigError(
                    f"Page must be at most {constants.MAX_PAGE}, got {self.page}"
                )

        if self.row < 0:
            raise CompanionConfigError(f"Row index must not be negative, got {self.row}")
        if self.column < 0:
            raise CompanionConfigError(
                f"Column index must not be negative, got {self.column}"
            )
        if self.row >= constants.MAX_SURFACE_ROWS:
            raise CompanionConfigError(
                f"Row must be at most {constants.MAX_SURFACE_ROWS}, got row index {self.row}"
            )
        if self.column >= constants.MAX_SURFACE_COLUMNS:
            raise CompanionConfigError(
                f"Column must be at most {constants.MAX_SURFACE_COLUMNS}, "
                f"got column index {self.column}"
            )

    @classmethod
    def from_ui(
        cls,
        *,
        dynamic_page: bool,
        page: Any = 1,
        row: Any = 0,
        column: Any = 0,
    ) -> CompanionAddress:
        """Build an address from user-facing coordinates.

        Row and column are 0-based (matching Companion's wire convention).
        Page is 1-based.
        """
        ui_row = _coerce_int(row, "Row")
        ui_column = _coerce_int(column, "Column")

        if ui_row < 0:
            raise CompanionConfigError(f"Row must be at least 0, got {ui_row}")
        if ui_column < 0:
            raise CompanionConfigError(f"Column must be at least 0, got {ui_column}")

        if dynamic_page:
            resolved_page = None
        else:
            resolved_page = _coerce_int(page, "Page")

        return cls(
            dynamic_page=dynamic_page,
            page=resolved_page,
            row=ui_row,
            column=ui_column,
        )

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> CompanionAddress:
        """Build an address from a persisted action-settings dict."""
        return cls.from_ui(
            dynamic_page=bool(settings.get("dynamic_page", True)),
            page=settings.get("page", 1),
            row=settings.get("row", 0),
            column=settings.get("column", 0),
        )

    # --- Wire representations ---------------------------------------------

    @property
    def sub_id(self) -> str:
        """The ``page/row/column`` identifier used by ADD-SUB, SUB-STATE etc.

        Only meaningful for static addresses; dynamic buttons are addressed by
        flat key index on the registered surface instead.
        """
        if self.dynamic_page:
            raise CompanionConfigError(
                "Dynamic addresses have no subscription id; they use a key index"
            )
        return f"{self.page}/{self.row}/{self.column}"

    @classmethod
    def from_sub_id(cls, sub_id: str) -> CompanionAddress:
        """Parse a ``page/row/column`` identifier from SUB-STATE or ADD-SUB.

        The values are already in Companion's convention, so no coordinate
        conversion happens here.
        """
        parts = sub_id.split("/")
        if len(parts) != 3:
            raise CompanionConfigError(f"Malformed subscription id: {sub_id!r}")
        try:
            page, row, column = (int(part) for part in parts)
        except ValueError:
            raise CompanionConfigError(
                f"Non-numeric subscription id: {sub_id!r}"
            ) from None
        return cls(dynamic_page=False, page=page, row=row, column=column)

    def key_index(self, surface_columns: int) -> int:
        """The flat key index for this address on a surface of the given width.

        Companion numbers surface keys row-major, so this must be recomputed
        whenever the surface is resized.
        """
        if surface_columns < 1:
            raise CompanionConfigError(
                f"Surface width must be at least 1, got {surface_columns}"
            )
        if self.column >= surface_columns:
            raise CompanionConfigError(
                f"Column index {self.column} does not fit a surface {surface_columns} wide"
            )
        return self.row * surface_columns + self.column

    @classmethod
    def from_key_index(cls, key_index: int, surface_columns: int) -> CompanionAddress:
        """Inverse of :meth:`key_index`, for decoding KEY-STATE messages."""
        if surface_columns < 1:
            raise CompanionConfigError(
                f"Surface width must be at least 1, got {surface_columns}"
            )
        if key_index < 0:
            raise CompanionConfigError(f"Key index must not be negative, got {key_index}")
        return cls(
            dynamic_page=True,
            page=None,
            row=key_index // surface_columns,
            column=key_index % surface_columns,
        )

    def describe(self) -> str:
        """Short, log-friendly form: ``dynamic/1/3`` or ``static/3/1/4``."""
        if self.dynamic_page:
            return f"dynamic/{self.row}/{self.column}"
        return f"static/{self.page}/{self.row}/{self.column}"


# --- Connection state ------------------------------------------------------


class ConnectionState(Enum):
    """Explicit connection lifecycle."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    NEGOTIATING = "negotiating"
    CONNECTED = "connected"
    INCOMPATIBLE = "incompatible"
    CONFIG_ERROR = "config_error"
    STOPPING = "stopping"

    @property
    def is_usable(self) -> bool:
        """True when commands can actually reach Companion."""
        return self is ConnectionState.CONNECTED

    @property
    def is_terminal(self) -> bool:
        """True for states that reconnecting will not fix on its own.

        An incompatible Companion or a bad configuration must not drive a
        reconnect loop.
        """
        return self in (ConnectionState.INCOMPATIBLE, ConnectionState.CONFIG_ERROR)


# --- Capabilities ----------------------------------------------------------


@dataclass(frozen=True)
class CompanionCapabilities:
    """What the connected Companion says it can do.

    Kept as structured state so no string lookups leak into action code.
    """

    subscriptions: bool = False
    bitmap_formats: frozenset[str] = frozenset()

    @classmethod
    def from_caps_params(cls, params: dict[str, str]) -> CompanionCapabilities:
        """Parse a CAPS message's parameters.

        Real-world note: Companion 4.3.4 (Satellite API 1.10.1) sends only
        ``SUBSCRIPTIONS=1``. ``BITMAP_FORMATS`` is API 1.12+, so its absence is
        normal and means raw RGB.
        """
        formats_raw = params.get("BITMAP_FORMATS", "")
        formats = {
            part.strip().lower() for part in formats_raw.split(",") if part.strip()
        }
        return cls(
            subscriptions=params.get("SUBSCRIPTIONS") == "1",
            bitmap_formats=frozenset(formats),
        )

    @property
    def negotiated_bitmap_format(self) -> str:
        """The encoding to request, preferring compressed formats we can decode.

        Never returns a format the decoder cannot handle.
        """
        for candidate in constants.PREFERRED_BITMAP_FORMATS:
            if candidate in self.bitmap_formats:
                return candidate
        return constants.RAW_BITMAP_FORMAT

    @property
    def uses_raw_bitmaps(self) -> bool:
        return self.negotiated_bitmap_format == constants.RAW_BITMAP_FORMAT

    def describe(self) -> str:
        parts = [f"SUBSCRIPTIONS={'yes' if self.subscriptions else 'no'}"]
        parts.append(f"BITMAP_FORMAT={self.negotiated_bitmap_format}")
        if self.bitmap_formats:
            parts.append(f"offered={','.join(sorted(self.bitmap_formats))}")
        return " ".join(parts)


# --- Connection settings ---------------------------------------------------


@dataclass(frozen=True)
class CompanionConnectionSettings:
    """Where and how to reach Companion.

    Frozen and comparable, so detecting "did the user change anything that
    requires reconnecting?" is a plain equality check.
    """

    mode: str = constants.DEFAULT_MODE
    host: str = constants.DEFAULT_HOST
    port: int = constants.DEFAULT_PORT
    device_id: str = ""
    debug_logging: bool = False

    # Phase 2 fields, carried through settings so the schema stays stable.
    satellite_ws_url: str = constants.DEFAULT_SATELLITE_WS_URL
    legacy_host: str = constants.DEFAULT_LEGACY_HOST
    legacy_port: int = constants.DEFAULT_LEGACY_PORT

    def __post_init__(self) -> None:
        if not self.host or not self.host.strip():
            raise CompanionConfigError("Host must not be empty")
        if not 1 <= self.port <= 65535:
            raise CompanionConfigError(
                f"Port must be between 1 and 65535, got {self.port}"
            )

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> CompanionConnectionSettings:
        """Build from a persisted plugin-settings dict, validating as we go."""
        host = str(settings.get("satellite_host", constants.DEFAULT_HOST)).strip()
        try:
            port = _coerce_int(
                settings.get("satellite_port", constants.DEFAULT_PORT), "Port"
            )
        except CompanionConfigError:
            raise
        suffix = str(settings.get("device_suffix", "")).strip()
        return cls(
            mode=str(settings.get("connection_mode", constants.DEFAULT_MODE)),
            host=host,
            port=port,
            device_id=f"{constants.DEVICE_ID_PREFIX}-{suffix}" if suffix else "",
            debug_logging=bool(settings.get("debug_logging", False)),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def describe(self) -> str:
        return f"{self.mode} {self.endpoint}"


# --- Subscriptions ---------------------------------------------------------


@dataclass
class SubscriptionEntry:
    """One logical subscription, shared by every listener on the same address.

    Listener count drives the network subscription: 0 to 1 creates it, 1 to 0
    removes it, and everything in between is a no-op.
    """

    address: CompanionAddress
    listeners: set[Any] = field(default_factory=set)
    cached_image: Any = None
    active: bool = False
    subscription_id: str | None = None
    generation: int = 0
    last_update: float | None = None
    error: str | None = None

    @property
    def listener_count(self) -> int:
        return len(self.listeners)

    def add_listener(self, listener: Any) -> bool:
        """Add a listener. Returns True if a network subscription is now needed."""
        was_empty = not self.listeners
        self.listeners.add(listener)
        return was_empty

    def remove_listener(self, listener: Any) -> bool:
        """Remove a listener. Returns True if the network subscription should go."""
        self.listeners.discard(listener)
        return not self.listeners

    def invalidate_image(self) -> None:
        """Forget the cached image without disturbing listeners.

        Used by KEYS-CLEAR and after reconnect, where continuing to show the old
        picture would be showing state we can no longer vouch for.
        """
        self.cached_image = None
        self.last_update = None
