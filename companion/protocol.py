"""Satellite protocol framing, parsing and serialization.

Deliberately free of sockets, threads and StreamController so it can be tested
in isolation. The layering is:

    socket bytes -> LineFramer -> parse_message -> SatelliteMessage -> manager

The parser is a direct port of Bitfocus's own ``parseLineParameters`` from
``companion-satellite/satellite/src/client/parser.ts``, not of the simplified
copy vendored into the Stream Deck plugin. That distinction matters: the
simplified copy splits values on every ``=`` and ignores backslash escapes,
which corrupts base64 bitmap payloads. Using the real parser's semantics is
what keeps us wire-compatible with Companion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from . import constants


class ProtocolError(Exception):
    """Raised for malformed protocol input that cannot be recovered from."""


# Keys Bitfocus's parser drops as prototype-pollution hardening. Python dicts
# cannot be polluted this way, so this is purely for behavioural parity: a real
# Companion client silently discards these, and we should agree with it.
_BANNED_KEYS = frozenset(
    {
        "__proto__",
        "constructor",
        "prototype",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
    }
)


# --- Framing ---------------------------------------------------------------


class LineFramer:
    """Reassembles newline-delimited messages from arbitrary byte chunks.

    A ``recv()`` never corresponds to one protocol message: it may deliver half
    a line, several lines, or a line plus a fragment. This holds the incomplete
    tail until the rest arrives.
    """

    def __init__(self, max_line_bytes: int = constants.MAX_MESSAGE_BYTES) -> None:
        self._buffer = bytearray()
        self._max_line_bytes = max_line_bytes
        self._overflow_bytes = 0

    def feed(self, chunk: bytes) -> list[str]:
        """Add received bytes and return every complete line they finished.

        Lines are decoded as UTF-8 with replacement, so undecodable bytes
        degrade a single message rather than killing the connection.

        This deliberately does not raise. A chunk can contain perfectly good
        messages *and* an oversized unterminated tail; raising would throw away
        the good ones. The tail is dropped and recorded in
        :meth:`take_overflow` for the caller to log instead.
        """
        self._buffer.extend(chunk)
        lines: list[str] = []

        while True:
            index = self._buffer.find(b"\n")
            if index == -1:
                break
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                lines.append(text)

        # An over-long tail with no newline means the peer is sending garbage or
        # something enormous. Drop it rather than growing without bound.
        if len(self._buffer) > self._max_line_bytes:
            self._overflow_bytes += len(self._buffer)
            self._buffer.clear()

        return lines

    def take_overflow(self) -> int:
        """Return and clear the count of bytes dropped for exceeding the limit.

        Non-zero means the peer sent something malformed and oversized; the
        connection manager logs it.
        """
        dropped, self._overflow_bytes = self._overflow_bytes, 0
        return dropped

    def reset(self) -> None:
        """Discard buffered bytes. Called when a connection is replaced."""
        self._buffer.clear()
        self._overflow_bytes = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


# --- Parsing ---------------------------------------------------------------


def parse_line_parameters(line: str) -> dict[str, str | bool]:
    """Parse the parameter portion of a Satellite line.

    Ported from Bitfocus's ``parseLineParameters``. The rules, which are not
    obvious and are all load-bearing:

    * A backslash escapes the next character, whatever it is. A dangling
      trailing backslash contributes nothing.
    * A double quote toggles "inside quotes" state and is never itself emitted.
      Quotes may appear mid-token, so ``KEY=va"lue"`` yields ``value``.
    * An unquoted space separates tokens; a quoted space is literal.
    * Tabs are **not** separators.
    * A token is split on its **first** ``=`` only, so a value keeps any further
      ``=`` — essential for base64 padding in bitmap payloads.
    * A token with no ``=`` is a boolean flag set to ``True``, which is how
      ``ADD-SUB OK`` and ``ADD-SUB ERROR`` are represented.
    """
    fragments: list[str] = [""]
    in_quotes = False

    i = 0
    length = len(line)
    while i < length:
        char = line[i]

        if char == "\\":
            # Escape: take the next character literally, if there is one.
            if i + 1 < length:
                fragments[-1] += line[i + 1]
            i += 2
            continue

        if char == '"':
            in_quotes = not in_quotes
        elif char == " " and not in_quotes:
            fragments.append("")
        else:
            fragments[-1] += char
        i += 1

    params: dict[str, str | bool] = {}
    for fragment in fragments:
        split_at = fragment.find("=")
        if split_at == -1:
            if fragment == "" or fragment in _BANNED_KEYS:
                continue
            params[fragment] = True
        else:
            key = fragment[:split_at]
            if key == "" or key in _BANNED_KEYS:
                continue
            params[key] = fragment[split_at + 1 :]

    return params


@dataclass(frozen=True)
class SatelliteMessage:
    """One parsed protocol line."""

    command: str
    params: dict[str, str | bool] = field(default_factory=dict)

    def text(self, key: str) -> str | None:
        """Return a parameter only if it is a string.

        Valueless flags parse to ``True``; treating one as text would be a bug,
        so this returns ``None`` instead of ``"True"``.
        """
        value = self.params.get(key)
        return value if isinstance(value, str) else None

    def flag(self, key: str) -> bool:
        """True when a key is present as a bare flag or as ``=1``/``=true``."""
        value = self.params.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return False

    def integer(self, key: str) -> int | None:
        value = self.text(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def describe(self) -> str:
        """Log-safe summary that never includes payload data.

        Bitmap values are replaced with their length so that debug logging can
        never dump image bytes.
        """
        parts = [self.command]
        for key, value in self.params.items():
            if key in _OPAQUE_PARAMS and isinstance(value, str):
                parts.append(f"{key}=<{len(value)} chars>")
            else:
                parts.append(f"{key}={value}")
        return " ".join(parts)


# Parameters whose values are payload blobs and must never be logged verbatim.
_OPAQUE_PARAMS = frozenset({"BITMAP", "LEDS", "CONFIG", "LAYOUT_MANIFEST", "VARIABLES"})


def parse_message(line: str) -> SatelliteMessage:
    """Split a line into its command and parameters.

    Unknown commands parse fine — tolerating them is required so that a newer
    Companion cannot break us.
    """
    stripped = line.strip()
    if not stripped:
        raise ProtocolError("Cannot parse an empty line")

    space_at = stripped.find(" ")
    if space_at == -1:
        return SatelliteMessage(command=stripped, params={})

    command = stripped[:space_at]
    params = parse_line_parameters(stripped[space_at + 1 :])
    return SatelliteMessage(command=command, params=params)


def parse_stream(framer: LineFramer, chunk: bytes) -> Iterator[SatelliteMessage]:
    """Feed bytes and yield every complete message they produced.

    A line that fails to parse is skipped rather than aborting the batch, so one
    malformed message cannot discard the valid ones alongside it.
    """
    for line in framer.feed(chunk):
        try:
            yield parse_message(line)
        except ProtocolError:
            continue


# --- Serialization ---------------------------------------------------------


def _format_value(value: object) -> str:
    """Render one parameter value using Bitfocus's own conventions.

    Booleans become ``1``/``0`` and numbers are bare; everything else is quoted.
    This follows the official ``companion-satellite`` client rather than the
    Stream Deck plugin, which sends ``true``/``false`` — both are accepted by
    Companion, but matching Bitfocus's own surface client is the safer bet.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return f'"{_escape(str(value))}"'


def _escape(value: str) -> str:
    """Escape characters that would otherwise break framing or tokenizing.

    Backslash first, so that escaping the quotes does not double-escape it.
    Newlines would split one message into two and must never appear raw.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def serialize(command: str, params: dict[str, object] | None = None) -> bytes:
    """Build a complete protocol line, newline included.

    Parameter order is preserved, because Companion's own clients emit a stable
    order and diverging makes packet captures harder to compare.
    """
    if not command or " " in command or "\n" in command:
        raise ProtocolError(f"Invalid command name: {command!r}")

    chunks = [command]
    for key, value in (params or {}).items():
        if not key or " " in key or "=" in key:
            raise ProtocolError(f"Invalid parameter name: {key!r}")
        chunks.append(f"{key}={_format_value(value)}")

    return (" ".join(chunks) + "\n").encode("utf-8")


# --- Outbound message builders --------------------------------------------
#
# Argument names and order follow the legacy (non-surface-manifest) Satellite
# form used by the Stream Deck plugin, which is what Companion API 1.10.x
# expects. The newer companion-satellite client uses LAYOUT_MANIFEST and
# CONTROLID instead; that path requires a higher API version than we declare.


def ping() -> bytes:
    return serialize("PING")


def pong() -> bytes:
    return serialize("PONG")


def add_device(
    device_id: str,
    rows: int,
    columns: int,
    bitmap_format: str | None = None,
) -> bytes:
    """Register the virtual surface used for dynamic-page buttons."""
    params: dict[str, object] = {
        "DEVICEID": device_id,
        "PRODUCT_NAME": constants.PRODUCT_NAME,
        "KEYS_TOTAL": rows * columns,
        "KEYS_PER_ROW": columns,
        "BITMAPS": constants.BITMAP_SIZE,
        "BRIGHTNESS": 0,
        # Opts this surface into CHANGE-PAGE. Must be a non-empty string, which
        # Companion shows as a checkbox label in the surface's settings; the box
        # is unticked by default, so this is a request for permission rather
        # than a grant of it.
        "CAN_CHANGE_PAGE": constants.CAN_CHANGE_PAGE_LABEL,
    }
    # Omitted entirely when no compressed format was negotiated, which keeps
    # Companion on its default raw rgb encoding.
    if bitmap_format and bitmap_format != constants.RAW_BITMAP_FORMAT:
        params["BITMAP_FORMAT"] = bitmap_format
    return serialize("ADD-DEVICE", params)


def remove_device(device_id: str) -> bytes:
    return serialize("REMOVE-DEVICE", {"DEVICEID": device_id})


def key_press(device_id: str, key_index: int, pressed: bool) -> bytes:
    return serialize(
        "KEY-PRESS",
        {"DEVICEID": device_id, "KEY": key_index, "PRESSED": pressed},
    )


def key_rotate(device_id: str, key_index: int, clockwise: bool) -> bytes:
    """One rotation event. Companion reads the sign of DIRECTION."""
    return serialize(
        "KEY-ROTATE",
        {"DEVICEID": device_id, "KEY": key_index, "DIRECTION": 1 if clockwise else 0},
    )


def change_page(device_id: str, forward: bool) -> bytes:
    """Move the registered surface to the next or previous Companion page.

    This is the only way for a surface to page itself: Companion gives each
    surface its own current page, and browsing pages in the web UI's Buttons tab
    is an editor view that moves nothing.

    Requires ``CAN_CHANGE_PAGE`` on the ADD-DEVICE that registered the surface.
    Companion replies OK whether or not it acted, so a reply is not evidence
    that the page moved.
    """
    return serialize(
        "CHANGE-PAGE",
        {"DEVICEID": device_id, "DIRECTION": 1 if forward else 0},
    )


def add_sub(sub_id: str, bitmap_format: str | None = None) -> bytes:
    """Subscribe to a fixed page/row/column, independent of the active page."""
    params: dict[str, object] = {
        "SUBID": sub_id,
        "LOCATION": sub_id,
        "BITMAP": constants.BITMAP_SIZE,
    }
    if bitmap_format and bitmap_format != constants.RAW_BITMAP_FORMAT:
        params["BITMAP_FORMAT"] = bitmap_format
    return serialize("ADD-SUB", params)


def remove_sub(sub_id: str) -> bytes:
    return serialize("REMOVE-SUB", {"SUBID": sub_id})


def sub_press(sub_id: str, pressed: bool) -> bytes:
    return serialize("SUB-PRESS", {"SUBID": sub_id, "PRESSED": pressed})


def sub_rotate(sub_id: str, clockwise: bool) -> bytes:
    return serialize(
        "SUB-ROTATE", {"SUBID": sub_id, "DIRECTION": 1 if clockwise else 0}
    )


# --- Inbound command names ------------------------------------------------


class Inbound:
    """Commands Companion sends us. Anything else is ignored safely."""

    BEGIN = "BEGIN"
    CAPS = "CAPS"
    KEY_STATE = "KEY-STATE"
    SUB_STATE = "SUB-STATE"
    ADD_SUB = "ADD-SUB"
    KEYS_CLEAR = "KEYS-CLEAR"
    PING = "PING"
    PONG = "PONG"
    ADD_DEVICE = "ADD-DEVICE"
    REMOVE_DEVICE = "REMOVE-DEVICE"
    KEY_PRESS = "KEY-PRESS"
    KEY_ROTATE = "KEY-ROTATE"
    CHANGE_PAGE = "CHANGE-PAGE"
    SUB_PRESS = "SUB-PRESS"
    SUB_ROTATE = "SUB-ROTATE"
    BRIGHTNESS = "BRIGHTNESS"
