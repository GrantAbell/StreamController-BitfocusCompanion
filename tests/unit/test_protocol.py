"""Satellite protocol tests.

The `TestBitfocusParserParity` class is a direct port of Bitfocus's own
`parser.test.ts`. Those cases are the compatibility contract: if one of them
fails we are no longer speaking Companion's dialect, regardless of whether our
own tests pass.
"""

import pytest

from companion import constants, protocol
from companion.protocol import (
    Inbound,
    LineFramer,
    ProtocolError,
    parse_line_parameters,
    parse_message,
    parse_stream,
    serialize,
)


# --- Framing ---------------------------------------------------------------


class TestLineFraming:
    def test_single_complete_line(self):
        framer = LineFramer()
        assert framer.feed(b"PING\n") == ["PING"]

    def test_multiple_messages_in_one_read(self):
        framer = LineFramer()
        assert framer.feed(b"PING\nPONG\nKEYS-CLEAR\n") == ["PING", "PONG", "KEYS-CLEAR"]

    def test_message_split_across_reads(self):
        """Never assume one recv() equals one message."""
        framer = LineFramer()

        assert framer.feed(b"BEGIN Api") == []
        assert framer.feed(b'Version="1.10') == []
        assert framer.feed(b'.1"\n') == ['BEGIN ApiVersion="1.10.1"']

    def test_line_plus_partial_tail_retains_the_tail(self):
        framer = LineFramer()

        assert framer.feed(b"PING\nPO") == ["PING"]
        assert framer.pending_bytes == 2
        assert framer.feed(b"NG\n") == ["PONG"]
        assert framer.pending_bytes == 0

    def test_byte_at_a_time_delivery(self):
        """The pathological case: one byte per read."""
        framer = LineFramer()
        payload = b"CAPS SUBSCRIPTIONS=1\n"

        result = []
        for index in range(len(payload)):
            result.extend(framer.feed(payload[index : index + 1]))

        assert result == ["CAPS SUBSCRIPTIONS=1"]

    def test_blank_lines_are_dropped(self):
        framer = LineFramer()
        assert framer.feed(b"\n\nPING\n\n") == ["PING"]

    def test_carriage_returns_are_stripped(self):
        framer = LineFramer()
        assert framer.feed(b"PING\r\n") == ["PING"]

    def test_undecodable_bytes_do_not_kill_the_stream(self):
        framer = LineFramer()
        lines = framer.feed(b"KEY=\xff\xfe\nPING\n")

        assert len(lines) == 2
        assert lines[1] == "PING"

    def test_oversized_unterminated_input_is_discarded(self):
        """A peer sending endless data without a newline must not exhaust memory."""
        framer = LineFramer(max_line_bytes=64)

        assert framer.feed(b"x" * 100) == []
        assert framer.pending_bytes == 0, "buffer must be cleared, not left full"
        assert framer.take_overflow() == 100
        assert framer.take_overflow() == 0, "the counter resets once reported"

    def test_oversized_tail_does_not_discard_good_messages(self):
        """A valid message followed by garbage must still be delivered."""
        framer = LineFramer(max_line_bytes=64)

        lines = framer.feed(b"PING\n" + b"x" * 100)

        assert lines == ["PING"]
        assert framer.take_overflow() == 100

    def test_framer_recovers_after_overflow(self):
        framer = LineFramer(max_line_bytes=64)
        framer.feed(b"x" * 100)

        assert framer.feed(b"PONG\n") == ["PONG"]

    def test_reset_discards_partial_state(self):
        framer = LineFramer()
        framer.feed(b"partial")
        framer.reset()

        assert framer.pending_bytes == 0
        assert framer.feed(b"PING\n") == ["PING"]


# --- Parity with Bitfocus's own parser tests -------------------------------


class TestBitfocusParserParity:
    """Ported verbatim from companion-satellite's parser.test.ts."""

    def test_single_pair(self):
        assert parse_line_parameters("KEY=value") == {"KEY": "value"}

    def test_multiple_pairs(self):
        assert parse_line_parameters("A=1 B=2 C=3") == {"A": "1", "B": "2", "C": "3"}

    def test_empty_value_is_empty_string(self):
        assert parse_line_parameters("KEY=")["KEY"] == ""

    def test_values_are_not_coerced(self):
        params = parse_line_parameters("N=42 F=0")
        assert params["N"] == "42"
        assert params["F"] == "0"

    def test_valueless_token_is_a_flag(self):
        assert parse_line_parameters("FLAG") == {"FLAG": True}

    def test_flags_mix_with_pairs(self):
        assert parse_line_parameters("A=1 FLAG B=2") == {"A": "1", "FLAG": True, "B": "2"}

    def test_base64_padding_survives(self):
        """Splitting on every '=' would corrupt bitmap payloads."""
        value = "iVBORw0KGgoAAAANSUhEUg=="
        assert parse_line_parameters(f"BITMAP={value}")["BITMAP"] == value

    def test_only_the_first_equals_splits(self):
        assert parse_line_parameters("X=a=b=c")["X"] == "a=b=c"

    def test_quoted_value_keeps_spaces(self):
        assert parse_line_parameters('TEXT="hello world"')["TEXT"] == "hello world"

    def test_quotes_prevent_equals_splitting(self):
        assert parse_line_parameters('PATH=0/0 X="a=b=c"') == {"PATH": "0/0", "X": "a=b=c"}

    def test_mid_token_quotes_are_stripped(self):
        assert parse_line_parameters('KEY=va"lue"')["KEY"] == "value"

    def test_quoted_key_may_contain_spaces(self):
        assert parse_line_parameters('"quoted key"=v')["quoted key"] == "v"

    def test_escaped_quote_inside_quoted_value(self):
        assert parse_line_parameters('KEY="a\\"b"')["KEY"] == 'a"b'

    def test_escaped_space_does_not_split(self):
        assert parse_line_parameters("KEY=a\\ b")["KEY"] == "a b"

    def test_dangling_backslash_is_ignored(self):
        assert parse_line_parameters("KEY=a\\")["KEY"] == "a"
        assert parse_line_parameters("FLAG\\") == {"FLAG": True}

    def test_tabs_are_not_separators(self):
        assert parse_line_parameters("A=1\tB=2")["A"] == "1\tB=2"

    def test_consecutive_spaces_produce_no_empty_key(self):
        assert parse_line_parameters("A=1  B=2") == {"A": "1", "B": "2"}

    def test_leading_and_trailing_spaces_ignored(self):
        assert parse_line_parameters("  A=1 B=2  ") == {"A": "1", "B": "2"}

    def test_dangerous_keys_are_dropped(self):
        result = parse_line_parameters(
            "__proto__=injected constructor=bad prototype=x "
            "__defineGetter__=y normal=ok"
        )
        assert "__proto__" not in result
        assert "constructor" not in result
        assert "prototype" not in result
        assert "__defineGetter__" not in result
        assert result["normal"] == "ok"

    def test_dangerous_key_with_equals_in_value_is_dropped(self):
        result = parse_line_parameters("__proto__=a=b normal=ok")
        assert "__proto__" not in result
        assert result["normal"] == "ok"

    def test_empty_line(self):
        assert parse_line_parameters("") == {}

    def test_whitespace_only_line(self):
        assert parse_line_parameters("   ") == {}

    def test_base64_with_slashes_survives(self):
        leds = "/wAAAP8A"
        params = parse_line_parameters(
            f"DEVICEID=abc123 CONTROLID=0/0 LEDS={leds} PRESSED=1"
        )
        assert params["LEDS"] == leds
        assert params["CONTROLID"] == "0/0"

    def test_quoted_data_url_keeps_padding(self):
        data_url = "data:image/png;base64,iVBORw0KGgo="
        assert parse_line_parameters(f'BITMAP="{data_url}"')["BITMAP"] == data_url

    def test_realistic_key_state_line(self):
        params = parse_line_parameters(
            "DEVICEID=surface-1 KEY=5 COLOR=#ff0000 TEXT=\"Play Clip\" "
            "BITMAP=aGVsbG8= PRESSED=0"
        )
        assert params == {
            "DEVICEID": "surface-1",
            "KEY": "5",
            "COLOR": "#ff0000",
            "TEXT": "Play Clip",
            "BITMAP": "aGVsbG8=",
            "PRESSED": "0",
        }


# --- Message parsing -------------------------------------------------------


class TestMessageParsing:
    def test_command_without_parameters(self):
        message = parse_message("KEYS-CLEAR")
        assert message.command == "KEYS-CLEAR"
        assert message.params == {}

    def test_live_begin_line_from_real_companion(self):
        """Exactly what 192.168.50.245 sent, trailing space included."""
        message = parse_message(
            'BEGIN CompanionVersion="4.3.4+9244-stable-c14e5e3334" ApiVersion="1.10.1" '
        )

        assert message.command == Inbound.BEGIN
        assert message.text("ApiVersion") == "1.10.1"
        assert message.text("CompanionVersion") == "4.3.4+9244-stable-c14e5e3334"

    def test_live_caps_line_from_real_companion(self):
        message = parse_message("CAPS SUBSCRIPTIONS=1")

        assert message.command == Inbound.CAPS
        assert message.flag("SUBSCRIPTIONS") is True
        assert message.text("BITMAP_FORMATS") is None

    def test_add_sub_ok_is_a_flag(self):
        message = parse_message("ADD-SUB OK")
        assert message.flag("OK") is True
        assert message.flag("ERROR") is False

    def test_add_sub_error_carries_details(self):
        message = parse_message('ADD-SUB ERROR SUBID=3/1/4 MESSAGE="No such page"')

        assert message.flag("ERROR") is True
        assert message.text("SUBID") == "3/1/4"
        assert message.text("MESSAGE") == "No such page"

    def test_unknown_commands_parse_without_raising(self):
        """Tolerating unknown commands lets a newer Companion stay compatible."""
        message = parse_message("SOME-FUTURE-COMMAND FOO=bar")
        assert message.command == "SOME-FUTURE-COMMAND"
        assert message.text("FOO") == "bar"

    def test_empty_line_is_rejected(self):
        with pytest.raises(ProtocolError):
            parse_message("   ")

    def test_integer_accessor(self):
        message = parse_message("KEY-STATE KEY=13 BITMAP=abc")
        assert message.integer("KEY") == 13

    def test_integer_accessor_rejects_nonsense(self):
        assert parse_message("KEY-STATE KEY=abc").integer("KEY") is None
        assert parse_message("KEY-STATE").integer("KEY") is None

    def test_text_accessor_never_returns_a_flag_as_text(self):
        message = parse_message("ADD-SUB OK")
        assert message.text("OK") is None

    def test_flag_accepts_string_forms(self):
        assert parse_message("X PRESSED=1").flag("PRESSED") is True
        assert parse_message("X PRESSED=true").flag("PRESSED") is True
        assert parse_message("X PRESSED=0").flag("PRESSED") is False

    def test_describe_never_leaks_payload_bytes(self):
        """Debug logging must not dump image data."""
        payload = "A" * 5000
        described = parse_message(f"KEY-STATE KEY=1 BITMAP={payload}").describe()

        assert payload not in described
        assert "<5000 chars>" in described
        assert "KEY=1" in described


# --- Streaming -------------------------------------------------------------


class TestParseStream:
    def test_yields_every_message_in_a_chunk(self):
        framer = LineFramer()
        messages = list(parse_stream(framer, b"PING\nPONG\nKEYS-CLEAR\n"))

        assert [m.command for m in messages] == ["PING", "PONG", "KEYS-CLEAR"]

    def test_handles_a_realistic_fragmented_handshake(self):
        framer = LineFramer()
        blob = (
            b'BEGIN CompanionVersion="4.3.4" ApiVersion="1.10.1"\n'
            b"CAPS SUBSCRIPTIONS=1\nKEY-STATE KEY=0 BITMAP=aGk=\n"
        )

        messages = []
        for index in range(0, len(blob), 7):
            messages.extend(parse_stream(framer, blob[index : index + 7]))

        assert [m.command for m in messages] == ["BEGIN", "CAPS", "KEY-STATE"]
        assert messages[2].text("BITMAP") == "aGk="


# --- Serialization ---------------------------------------------------------


class TestSerialization:
    def test_bare_commands(self):
        assert protocol.ping() == b"PING\n"
        assert protocol.pong() == b"PONG\n"

    def test_booleans_become_one_and_zero(self):
        """Matches Bitfocus's own client, which sends 1/0 rather than true/false."""
        assert b"PRESSED=1" in protocol.key_press("dev", 5, pressed=True)
        assert b"PRESSED=0" in protocol.key_press("dev", 5, pressed=False)

    def test_numbers_are_unquoted(self):
        assert b"KEY=5 " in protocol.key_press("dev", 5, pressed=True)

    def test_strings_are_quoted(self):
        assert b'DEVICEID="dev"' in protocol.key_press("dev", 5, pressed=True)

    def test_add_device_geometry(self):
        line = protocol.add_device("dev", rows=4, columns=8).decode()

        assert line.startswith("ADD-DEVICE ")
        assert 'DEVICEID="dev"' in line
        assert "KEYS_TOTAL=32" in line
        assert "KEYS_PER_ROW=8" in line
        assert f"BITMAPS={constants.BITMAP_SIZE}" in line
        assert "BRIGHTNESS=0" in line

    def test_add_device_omits_bitmap_format_for_raw(self):
        """Omitting it keeps Companion on its default rgb encoding."""
        for fmt in (None, constants.RAW_BITMAP_FORMAT):
            assert b"BITMAP_FORMAT" not in protocol.add_device(
                "dev", 4, 8, bitmap_format=fmt
            )

    def test_add_device_includes_negotiated_compressed_format(self):
        assert b'BITMAP_FORMAT="png"' in protocol.add_device(
            "dev", 4, 8, bitmap_format="png"
        )

    def test_surface_grows_beyond_the_minimum(self):
        line = protocol.add_device("dev", rows=6, columns=11).decode()
        assert "KEYS_TOTAL=66" in line
        assert "KEYS_PER_ROW=11" in line

    def test_rotation_direction(self):
        assert b"DIRECTION=1" in protocol.key_rotate("dev", 3, clockwise=True)
        assert b"DIRECTION=0" in protocol.key_rotate("dev", 3, clockwise=False)

    def test_subscription_messages(self):
        assert protocol.add_sub("3/1/4").decode() == (
            f'ADD-SUB SUBID="3/1/4" LOCATION="3/1/4" BITMAP={constants.BITMAP_SIZE}\n'
        )
        assert protocol.remove_sub("3/1/4") == b'REMOVE-SUB SUBID="3/1/4"\n'

    def test_subscription_press_and_rotate(self):
        assert protocol.sub_press("3/1/4", pressed=True) == (
            b'SUB-PRESS SUBID="3/1/4" PRESSED=1\n'
        )
        assert protocol.sub_rotate("3/1/4", clockwise=False) == (
            b'SUB-ROTATE SUBID="3/1/4" DIRECTION=0\n'
        )

    def test_remove_device(self):
        assert protocol.remove_device("dev") == b'REMOVE-DEVICE DEVICEID="dev"\n'

    def test_quotes_in_values_are_escaped(self):
        line = serialize("TEST", {"V": 'say "hi"'}).decode()
        assert line == 'TEST V="say \\"hi\\""\n'

    def test_backslashes_are_escaped_before_quotes(self):
        line = serialize("TEST", {"V": "back\\slash"}).decode()
        assert line == 'TEST V="back\\\\slash"\n'

    def test_newlines_can_never_break_framing(self):
        """A raw newline in a value would split one message into two."""
        line = serialize("TEST", {"V": "a\nb"}).decode()

        assert line.count("\n") == 1
        assert line.endswith("\n")

    def test_invalid_command_names_are_rejected(self):
        for bad in ("", "HAS SPACE", "HAS\nNEWLINE"):
            with pytest.raises(ProtocolError):
                serialize(bad)

    def test_invalid_parameter_names_are_rejected(self):
        for bad in ("", "HAS SPACE", "HAS=EQUALS"):
            with pytest.raises(ProtocolError):
                serialize("TEST", {bad: "v"})


# --- Round trip ------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize(
        "payload",
        [
            protocol.ping(),
            protocol.pong(),
            protocol.add_device("streamcontroller-companion-7a7557", 4, 8),
            protocol.add_device("dev", 6, 11, bitmap_format="png"),
            protocol.remove_device("dev"),
            protocol.key_press("dev", 0, pressed=True),
            protocol.key_press("dev", 31, pressed=False),
            protocol.key_rotate("dev", 7, clockwise=True),
            protocol.add_sub("3/1/4"),
            protocol.remove_sub("3/1/4"),
            protocol.sub_press("3/1/4", pressed=True),
            protocol.sub_rotate("3/1/4", clockwise=False),
        ],
    )
    def test_everything_we_send_parses_back(self, payload):
        """Our serializer and our parser must agree on every command."""
        framer = LineFramer()
        messages = list(parse_stream(framer, payload))

        assert len(messages) == 1
        assert framer.pending_bytes == 0

    def test_awkward_values_survive_a_round_trip(self):
        awkward = 'a "quoted" \\ back\\slash and  spaces'
        framer = LineFramer()

        (message,) = list(parse_stream(framer, serialize("TEST", {"V": awkward})))

        assert message.text("V") == awkward
