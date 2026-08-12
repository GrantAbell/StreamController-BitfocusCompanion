"""Central constants for the Companion integration.

Protocol values live here rather than being scattered through the code so that
Companion's protocol evolving means editing one file. Verified against upstream
``io.bitfocus.companion-plugin`` and the Satellite Wire Protocol.
"""

from typing import Final

# --- Settings schema ------------------------------------------------------

SCHEMA_VERSION: Final[int] = 1

# --- Connection defaults --------------------------------------------------

MODE_SATELLITE_TCP: Final[str] = "satellite_tcp"
MODE_SATELLITE_WS: Final[str] = "satellite_ws"
MODE_LEGACY_WS: Final[str] = "legacy_ws"

DEFAULT_MODE: Final[str] = MODE_SATELLITE_TCP
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 16622

# Phase 2 defaults, declared now so the settings schema is stable.
DEFAULT_SATELLITE_WS_URL: Final[str] = "ws://127.0.0.1:16623"
DEFAULT_LEGACY_HOST: Final[str] = "127.0.0.1"
DEFAULT_LEGACY_PORT: Final[int] = 28492

# --- Device identity ------------------------------------------------------

DEVICE_ID_PREFIX: Final[str] = "streamcontroller-companion"
DEVICE_SUFFIX_BYTES: Final[int] = 3  # 6 hex characters, e.g. "8f4c91"

# The name Companion shows for this surface.
PRODUCT_NAME: Final[str] = "StreamController"

# --- Satellite protocol ---------------------------------------------------

# Confirmed against upstream src/satellite/client.ts. Do not raise without
# checking upstream first; do not duplicate this value anywhere else.
MINIMUM_SATELLITE_API_VERSION: Final[str] = "1.10.0"

# Companion renders square button bitmaps at this edge length.
BITMAP_SIZE: Final[int] = 72

# Compressed encodings we can decode, most preferred first. Anything not listed
# here is never requested, so Companion can only send us something we can read.
# Raw "rgb" is the always-available fallback.
PREFERRED_BITMAP_FORMATS: Final[tuple[str, ...]] = ("png",)
RAW_BITMAP_FORMAT: Final[str] = "rgb"

# --- Dynamic surface geometry ---------------------------------------------

# A minimum, never a maximum: the surface grows to cover any configured
# coordinate.
MIN_SURFACE_ROWS: Final[int] = 4
MIN_SURFACE_COLUMNS: Final[int] = 8

# --- Page navigation ------------------------------------------------------

# Sent as CAN_CHANGE_PAGE on ADD-DEVICE. Companion requires a non-empty *string*
# here, not a boolean: it uses the text verbatim as the label of a per-surface
# checkbox which defaults to unticked. Until the user ticks it, Companion
# answers CHANGE-PAGE with OK and then discards it (Surface/IP/Satellite.ts,
# `doChangePage`), so declaring this grants nothing on its own.
CAN_CHANGE_PAGE_LABEL: Final[str] = "Allow StreamController to change this surface page"

# --- Coordinate sanity limits ---------------------------------------------
#
# These are not protocol limits — Satellite imposes none. They exist so that a
# typo cannot request a 100000-key surface and exhaust memory. Generous enough
# that no realistic Companion layout hits them.

MAX_PAGE: Final[int] = 9999
MAX_SURFACE_ROWS: Final[int] = 32
MAX_SURFACE_COLUMNS: Final[int] = 32

# --- Timing ---------------------------------------------------------------

# The heartbeat is not optional and is not merely liveness detection: measured
# against Companion 4.3.4, an idle connection that sends no PING is closed by
# Companion after ~5.2 s. Pinging must therefore begin as soon as the socket is
# up and continue for the life of the connection.
#
# 1 s was verified to hold a connection open indefinitely (45/45 pings answered)
# and leaves five pings inside Companion's ~5 s window. Upstream pings at 100 ms,
# which is 10x/second for no measurable benefit.
PING_INTERVAL_SECONDS: Final[float] = 1.0

# Missed pongs tolerated before declaring the connection dead. At a 1 s interval
# this detects a black-hole connection in ~5 s.
PING_UNACKED_LIMIT: Final[int] = 5

# Companion may send CAPS just after BEGIN. If it never arrives, proceed
# without subscription support rather than hanging.
CAPS_TIMEOUT_SECONDS: Final[float] = 0.5

CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0

# Minimum gap between forced surface refreshes. A refresh makes Companion
# resend every key, so a page full of controls appearing at once must not
# trigger one per control.
SURFACE_REFRESH_MIN_INTERVAL: Final[float] = 2.0

# Bounded exponential backoff.
RECONNECT_DELAYS_SECONDS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 10.0)

# --- Safety limits --------------------------------------------------------
#
# Companion protocol data is untrusted input.

# A 72x72 RGB bitmap is ~15.5 KiB raw, ~21 KiB base64. One megabyte per line is
# far above any legitimate payload while still bounding memory.
MAX_MESSAGE_BYTES: Final[int] = 1024 * 1024
MAX_IMAGE_BYTES: Final[int] = 1024 * 1024
