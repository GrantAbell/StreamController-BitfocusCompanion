"""Decoding Companion's rendered button imagery into Pillow images.

Companion is the authoritative source of what a button looks like; this module
only converts its bytes faithfully and never invents or approximates imagery.

Two payload encodings exist:

* **raw ``rgb``** — base64 of ``size x size x 3`` RGB24 bytes. This is the
  primary path: Companion only offers compressed formats from Satellite API
  1.12, and the target Companion is 1.10.1, so this is what actually arrives.
* **compressed** (currently only ``png``) — either bare base64 or a ``data:``
  URL, which is how Companion sends compressed bitmaps.

Every decode treats its input as untrusted. Malformed data yields ``None`` and
a log line; it must never raise into the connection manager or reach
StreamController.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import math

from PIL import Image, UnidentifiedImageError

from . import constants

log = logging.getLogger(__name__)

# Companion sends square button bitmaps; this is the expected raw byte count.
_BYTES_PER_PIXEL = 3

_DATA_URL_PREFIX = "data:"


class ImageDecodeError(Exception):
    """Raised internally when a payload cannot be decoded. Never escapes."""


def decode_button_image(
    payload: str,
    bitmap_format: str,
    expected_size: int = constants.BITMAP_SIZE,
) -> Image.Image | None:
    """Decode one button bitmap, or return None if it cannot be trusted.

    ``bitmap_format`` is the encoding negotiated during CAPS. A ``data:`` URL is
    detected regardless, because Companion sends compressed bitmaps that way
    and mislabelling one would only produce a confusing failure.
    """
    if not payload:
        return None

    try:
        if payload.startswith(_DATA_URL_PREFIX):
            return _decode_data_url(payload)
        if bitmap_format == constants.RAW_BITMAP_FORMAT:
            return _decode_raw_rgb(payload, expected_size)
        return _decode_compressed(payload)
    except ImageDecodeError as exc:
        log.warning("Discarding malformed Companion image: %s", exc)
        return None
    except Exception:  # noqa: BLE001 - a decoder bug must not kill the worker
        log.error("Unexpected failure decoding Companion image", exc_info=True)
        return None


# --- Raw RGB ---------------------------------------------------------------


def _decode_raw_rgb(payload: str, expected_size: int) -> Image.Image:
    """Decode base64 RGB24 pixel data into an image.

    The dimensions are not carried in the payload, so they are derived from the
    byte count and cross-checked against the size we requested. A payload that
    is not a whole number of square RGB pixels is rejected rather than being
    reshaped into a skewed image.
    """
    raw = _decode_base64(payload)

    if len(raw) % _BYTES_PER_PIXEL != 0:
        raise ImageDecodeError(
            f"raw payload of {len(raw)} bytes is not a whole number of RGB pixels"
        )

    pixels = len(raw) // _BYTES_PER_PIXEL
    side = int(math.isqrt(pixels))
    if side * side != pixels:
        raise ImageDecodeError(
            f"raw payload of {pixels} pixels is not square "
            f"(expected {expected_size}x{expected_size})"
        )
    if side == 0:
        raise ImageDecodeError("raw payload is empty")

    if side != expected_size:
        # Not fatal: Companion may honour a different size than requested. Use
        # what actually arrived rather than corrupting it into the wrong shape.
        log.debug(
            "Companion sent a %dx%d bitmap where %dx%d was requested",
            side,
            side,
            expected_size,
            expected_size,
        )

    try:
        return Image.frombytes("RGB", (side, side), raw)
    except ValueError as exc:
        raise ImageDecodeError(f"could not build a {side}x{side} image: {exc}") from exc


# --- Compressed ------------------------------------------------------------


def _decode_data_url(payload: str) -> Image.Image:
    """Decode a ``data:image/png;base64,...`` URL."""
    separator = payload.find(",")
    if separator == -1:
        raise ImageDecodeError("data url has no comma separator")

    header = payload[:separator]
    if "base64" not in header:
        raise ImageDecodeError(f"unsupported data url encoding: {header!r}")

    return _decode_compressed(payload[separator + 1 :])


def _decode_compressed(payload: str) -> Image.Image:
    """Decode base64 image data that carries its own format header."""
    raw = _decode_base64(payload)

    try:
        with Image.open(io.BytesIO(raw)) as image:
            # load() forces decoding inside this try, so a truncated file
            # fails here rather than later during rendering.
            image.load()
            return image.convert("RGB") if image.mode != "RGB" else image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDecodeError(f"undecodable compressed image: {exc}") from exc


# --- Shared ----------------------------------------------------------------


def _decode_base64(payload: str) -> bytes:
    """Base64-decode with a hard size ceiling.

    The ceiling is applied to the encoded text first, so an enormous payload is
    rejected before any memory is allocated for the decoded form.
    """
    if len(payload) > constants.MAX_IMAGE_BYTES:
        raise ImageDecodeError(
            f"payload of {len(payload)} chars exceeds the "
            f"{constants.MAX_IMAGE_BYTES} byte limit"
        )

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageDecodeError(f"invalid base64: {exc}") from exc

    if not raw:
        raise ImageDecodeError("payload decoded to zero bytes")
    return raw


def expected_raw_payload_chars(size: int = constants.BITMAP_SIZE) -> int:
    """Base64 length of a full raw bitmap, for logs and tests."""
    raw_bytes = size * size * _BYTES_PER_PIXEL
    return (raw_bytes + 2) // 3 * 4
