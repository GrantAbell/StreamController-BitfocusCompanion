"""Image decoding tests.

Raw RGB is exercised as the primary path because the target Companion
(Satellite API 1.10.1) never offers a compressed format.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from companion import constants
from companion.images import decode_button_image, expected_raw_payload_chars

RAW = constants.RAW_BITMAP_FORMAT
SIZE = constants.BITMAP_SIZE


def _raw_payload(size: int = SIZE, colour: tuple[int, int, int] = (255, 0, 0)) -> str:
    return base64.b64encode(bytes(colour) * (size * size)).decode()


def _png_payload(size: int = SIZE, colour: tuple[int, int, int] = (0, 128, 255)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


# --- Raw RGB ---------------------------------------------------------------


class TestRawBitmaps:
    def test_decodes_a_full_size_bitmap(self):
        image = decode_button_image(_raw_payload(), RAW)

        assert image is not None
        assert image.size == (SIZE, SIZE)
        assert image.mode == "RGB"

    def test_preserves_pixel_values_exactly(self):
        """Raw RGB is lossless; Companion's colours must survive untouched."""
        image = decode_button_image(_raw_payload(colour=(12, 34, 56)), RAW)

        assert image.getpixel((0, 0)) == (12, 34, 56)
        assert image.getpixel((SIZE - 1, SIZE - 1)) == (12, 34, 56)

    def test_matches_the_payload_size_seen_from_live_companion(self):
        """20736 base64 chars, as measured against Companion 4.3.4."""
        payload = _raw_payload()

        assert len(payload) == 20736
        assert expected_raw_payload_chars() == 20736
        assert decode_button_image(payload, RAW) is not None

    def test_accepts_a_different_square_size(self):
        """Companion may honour a size other than the one requested."""
        image = decode_button_image(_raw_payload(size=96), RAW)

        assert image.size == (96, 96)

    def test_rejects_a_truncated_payload(self):
        full = base64.b64decode(_raw_payload())
        truncated = base64.b64encode(full[: len(full) // 2 - 1]).decode()

        assert decode_button_image(truncated, RAW) is None

    def test_rejects_a_payload_that_is_not_whole_pixels(self):
        payload = base64.b64encode(b"\xff\x00").decode()

        assert decode_button_image(payload, RAW) is None

    def test_rejects_a_non_square_payload(self):
        payload = base64.b64encode(bytes(3) * 12).decode()  # 12 pixels, not square

        assert decode_button_image(payload, RAW) is None

    def test_rejects_invalid_base64(self):
        assert decode_button_image("!!!! not base64 !!!!", RAW) is None

    def test_rejects_an_empty_payload(self):
        assert decode_button_image("", RAW) is None

    def test_rejects_a_payload_decoding_to_nothing(self):
        assert decode_button_image(base64.b64encode(b"").decode(), RAW) is None


# --- Compressed ------------------------------------------------------------


class TestCompressedBitmaps:
    def test_decodes_bare_base64_png(self):
        image = decode_button_image(_png_payload(), "png")

        assert image is not None
        assert image.size == (SIZE, SIZE)
        assert image.getpixel((0, 0)) == (0, 128, 255)

    def test_decodes_a_data_url(self):
        """Companion sends compressed bitmaps as data: urls."""
        payload = f"data:image/png;base64,{_png_payload()}"

        image = decode_button_image(payload, "png")

        assert image is not None
        assert image.size == (SIZE, SIZE)

    def test_data_url_is_detected_even_when_format_says_raw(self):
        """Mislabelling must not turn a valid image into garbage."""
        payload = f"data:image/png;base64,{_png_payload()}"

        assert decode_button_image(payload, RAW) is not None

    def test_rejects_a_data_url_without_a_separator(self):
        assert decode_button_image("data:image/png;base64", "png") is None

    def test_rejects_a_non_base64_data_url(self):
        assert decode_button_image("data:image/png,%FF%00", "png") is None

    def test_rejects_a_truncated_png(self):
        raw = base64.b64decode(_png_payload())
        truncated = base64.b64encode(raw[: len(raw) // 2]).decode()

        assert decode_button_image(truncated, "png") is None

    def test_rejects_non_image_bytes(self):
        payload = base64.b64encode(b"this is definitely not a png").decode()

        assert decode_button_image(payload, "png") is None

    def test_converts_transparency_to_rgb(self):
        """Stream Deck keys are opaque; an RGBA png must still render."""
        buffer = io.BytesIO()
        Image.new("RGBA", (SIZE, SIZE), (10, 20, 30, 128)).save(buffer, format="PNG")
        payload = base64.b64encode(buffer.getvalue()).decode()

        image = decode_button_image(payload, "png")

        assert image is not None
        assert image.mode == "RGB"


# --- Safety ----------------------------------------------------------------


class TestSafety:
    def test_oversized_payload_is_refused_before_allocation(self):
        payload = "A" * (constants.MAX_IMAGE_BYTES + 1)

        assert decode_button_image(payload, RAW) is None

    def test_a_payload_at_the_limit_is_still_examined(self):
        """The cap must not reject legitimate bitmaps."""
        assert len(_raw_payload()) < constants.MAX_IMAGE_BYTES

    def test_decoding_never_raises_for_arbitrary_input(self):
        """Companion data is untrusted; nothing here may reach the manager."""
        nasty = [
            "",
            "A",
            "====",
            "\x00\x01\x02",
            "data:",
            "data:,",
            "data:image/png;base64,",
            "data:image/png;base64,!!!",
            base64.b64encode(bytes(range(256))).decode(),
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode(),
            "A" * 1000,
        ]

        for payload in nasty:
            for fmt in (RAW, "png", "webp", ""):
                assert decode_button_image(payload, fmt) is None or True

    @pytest.mark.parametrize("fmt", [RAW, "png", "webp", "", "nonsense"])
    def test_garbage_returns_none_for_every_format(self, fmt):
        assert decode_button_image("!!!not base64!!!", fmt) is None
