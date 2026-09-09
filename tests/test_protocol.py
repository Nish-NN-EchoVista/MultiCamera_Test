"""Protocol parser tests.

The negative cases matter as much as the positive ones here: the firmware emits
several lines that a naive parser would misread (see `_CLEAN_NEAR_MISSES` and
the command echo in `protocol.py`).
"""

from __future__ import annotations

import pytest

from emcc.backend.protocol import (
    AUTO_OFF_ACK,
    AUTO_ON_ACK,
    CLEAN_COMPLETE,
    CMD_CLEAN,
    LineAssembler,
    MessageKind,
    encode_command,
    parse_line,
    split_tag,
)


# ---------------------------------------------------------------------------
# Command framing
# ---------------------------------------------------------------------------


def test_command_is_crlf_terminated():
    # EVS2 dispatches only on CR immediately followed by LF.
    assert encode_command(CMD_CLEAN) == b"start_evap_sweep\r\n"


def test_command_encodes_to_ascii_bytes():
    assert isinstance(encode_command("dis_auto"), bytes)


# ---------------------------------------------------------------------------
# Tag splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,channel,payload",
    [
        ("[ESV2-1] Evaporation Sweep Completed", 1, "Evaporation Sweep Completed"),
        ("[ESV2-2] AutoClean enabled", 2, "AutoClean enabled"),
        ("[ESV2-1]No space after tag", 1, "No space after tag"),
        ("PZT Temp: 43.7 C", None, "PZT Temp: 43.7 C"),
        ("", None, ""),
    ],
)
def test_split_tag(line, channel, payload):
    assert split_tag(line) == (channel, payload)


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("PZT Temp: 43.7 C", 43.7),
        ("PZT Temp: 65.0 C", 65.0),
        ("PZT Temp: 0.0 C", 0.0),
        ("PZT Temp: -2.4 C", -2.4),
        ("PZT Temp: 100 C", 100.0),          # no decimal part
        ("PZT Temp:43.7 C", 43.7),           # no space after colon
        ("PZT Temp:  43.7  C", 43.7),        # extra whitespace
    ],
)
def test_parses_pzt_temperature(line, expected):
    msg = parse_line(line)
    assert msg.kind is MessageKind.PZT_TEMPERATURE
    assert msg.value == pytest.approx(expected)


def test_pzt_temperature_is_untagged_so_has_no_channel():
    # The thermocouple belongs to the AdapterController, not to a board.
    assert parse_line("PZT Temp: 43.7 C").channel is None


@pytest.mark.parametrize(
    "line,expected",
    [
        ("PZT Temp: 240.10 C", 241.0),   # observed on 192.168.2.253
        ("PZT Temp: 40.10 C", 41.0),
        ("PZT Temp: 0.10 C", 1.0),
        ("PZT Temp: -5.10 C", -6.0),
    ],
)
def test_tenths_of_ten_carry_a_whole_degree(line, expected):
    """Firmware rounds tenths without carrying into the integer part.

    `temp_dec = (int)((temp - temp_int) * 10 + 0.5f)` reaches 10 when the
    fraction is >= 0.95, so 241.0 is printed as "240.10". Reading the text as
    a decimal would give 240.1 -- wrong by 0.9. Seen live on .253, which
    alternated between 242.x and an exactly-repeating "240.10".
    """
    assert parse_line(line).value == pytest.approx(expected)


def test_ordinary_tenths_are_unaffected_by_the_carry_fix():
    for tenths in range(10):
        line = f"PZT Temp: 42.{tenths} C"
        assert parse_line(line).value == pytest.approx(42 + tenths / 10)


def test_implausible_readings_still_parse():
    """An open-circuit sensor reads hundreds of degrees; parse it, don't clamp.

    The ADC conversion is ~200 C per volt, so a disconnected thermocouple
    produces a large but well-formed number. Two of the three live units do
    exactly this. Hiding it in the parser would hide a hardware fault.
    """
    assert parse_line("PZT Temp: 404.4 C").value == pytest.approx(404.4)


@pytest.mark.parametrize(
    "line",
    [
        "Board Temperature = 43.7",             # EVS2 sequences.c:804, on the wire
        "Temperature  = 43.700000  C",          # EVS2 board temp (dead code)
        "TI TMP451 Temperature  = 43.7  oC",
        "PSU_0 Temperature  = 41.2  oC",
        "PZT: 2048",                            # legacy raw ADC, not a temperature
    ],
)
def test_other_temperature_lines_are_not_pzt(line):
    assert parse_line(line).kind is not MessageKind.PZT_TEMPERATURE


# ---------------------------------------------------------------------------
# Clean completion, and the near-misses that must not match
# ---------------------------------------------------------------------------


def test_clean_completion_untagged():
    assert parse_line(CLEAN_COMPLETE).kind is MessageKind.CLEAN_COMPLETE


@pytest.mark.parametrize("channel", [1, 2])
def test_clean_completion_tagged_carries_channel(channel):
    msg = parse_line(f"[ESV2-{channel}] {CLEAN_COMPLETE}")
    assert msg.kind is MessageKind.CLEAN_COMPLETE
    assert msg.channel == channel


@pytest.mark.parametrize(
    "line",
    [
        "Evaporation Sweep Low Power Completed",
        "[ESV2-1] Evaporation Sweep Low Power Completed",
        "AutoClean triggered Evaporation Sweep",
        "[ESV2-2] AutoClean triggered Evaporation Sweep",
    ],
)
def test_clean_near_misses_do_not_complete(line):
    """A substring test for 'Evaporation Sweep' + 'Completed' matches the
    low-power line. It must not be treated as our completion."""
    assert parse_line(line).kind is not MessageKind.CLEAN_COMPLETE


def test_command_echo_is_not_a_clean_command():
    """The echo contains the command text; it must not be acted on."""
    msg = parse_line("TX to ESV2-1: start_evap_sweep")
    assert msg.kind is MessageKind.COMMAND_ECHO


# ---------------------------------------------------------------------------
# Auto acknowledgements
# ---------------------------------------------------------------------------


def test_auto_on_ack():
    assert parse_line(f"[ESV2-1] {AUTO_ON_ACK}").kind is MessageKind.AUTO_ON_ACK


def test_auto_off_ack():
    assert parse_line(f"[ESV2-2] {AUTO_OFF_ACK}").kind is MessageKind.AUTO_OFF_ACK


# ---------------------------------------------------------------------------
# Unrelated telemetry is ignored safely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "I: Pump enabled (PB12 gated by sequence)",
        "[Router] F401 online. Map: GUI=USART1, ESV2-1=USART2, ESV2-2=USART6",
        "Received clean trigger from ESV2-1",
        "I: Autodetection sweep starting, Driving Voltage: 40.000000",
        "AutoTrigger",
        "InsertionLoss: 1.234",
        "garbage \x00\xff bytes",
        "   ",
    ],
)
def test_unrelated_lines_parse_as_other(line):
    assert parse_line(line).kind is MessageKind.OTHER


# ---------------------------------------------------------------------------
# Line assembly over a byte stream
# ---------------------------------------------------------------------------


def test_single_complete_line():
    assert LineAssembler().feed(b"PZT Temp: 43.7 C\r\n") == ["PZT Temp: 43.7 C"]


def test_multiple_lines_in_one_packet():
    lines = LineAssembler().feed(
        b"PZT Temp: 43.7 C\r\n[ESV2-1] AutoClean enabled\r\nI: something\r\n"
    )
    assert lines == [
        "PZT Temp: 43.7 C",
        "[ESV2-1] AutoClean enabled",
        "I: something",
    ]


def test_fragmented_line_across_packets():
    """A temperature split mid-number must still parse."""
    a = LineAssembler()
    assert a.feed(b"PZT Te") == []
    assert a.feed(b"mp: 4") == []
    assert a.feed(b"3.") == []
    assert a.feed(b"7 C") == []
    lines = a.feed(b"\r\n")
    assert lines == ["PZT Temp: 43.7 C"]
    assert parse_line(lines[0]).value == pytest.approx(43.7)


def test_terminator_split_across_packets():
    a = LineAssembler()
    assert a.feed(b"PZT Temp: 43.7 C\r") == []
    assert a.feed(b"\n") == ["PZT Temp: 43.7 C"]


def test_incomplete_tail_is_retained_not_emitted():
    a = LineAssembler()
    assert a.feed(b"complete\r\npartial") == ["complete"]
    assert a.feed(b" now done\r\n") == ["partial now done"]


def test_bare_lf_is_accepted():
    assert LineAssembler().feed(b"one\ntwo\n") == ["one", "two"]


def test_blank_lines_are_dropped():
    assert LineAssembler().feed(b"\r\n\r\nreal\r\n") == ["real"]


def test_undecodable_bytes_do_not_raise():
    lines = LineAssembler().feed(b"\xff\xfe bad bytes\r\n")
    assert len(lines) == 1  # mojibake, but no exception


def test_reset_discards_partial_line():
    a = LineAssembler()
    a.feed(b"half a line")
    a.reset()
    assert a.feed(b"fresh\r\n") == ["fresh"]


def test_runaway_stream_without_terminator_is_bounded():
    a = LineAssembler()
    for _ in range(40):
        a.feed(b"x" * 1024)
    assert a.overflow_count > 0
    # Still functional afterwards.
    assert a.feed(b"\r\ngood\r\n")[-1] == "good"


def test_realistic_burst():
    """A plausible packet: temperature, a tagged ack, an echo and a completion."""
    payload = (
        b"PZT Temp: 64.9 C\r\n"
        b"TX to ESV2-1: start_evap_sweep\r\n"
        b"[ESV2-1] Evaporation Sweep Low Power Completed\r\n"
        b"[ESV2-2] Evaporation Sweep Completed\r\n"
    )
    kinds = [parse_line(l).kind for l in LineAssembler().feed(payload)]
    assert kinds == [
        MessageKind.PZT_TEMPERATURE,
        MessageKind.COMMAND_ECHO,
        MessageKind.OTHER,            # low-power near-miss
        MessageKind.CLEAN_COMPLETE,
    ]
