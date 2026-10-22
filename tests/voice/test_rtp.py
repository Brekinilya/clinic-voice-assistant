import asyncio
import math
import time
from array import array

import pytest

from app.voice.rtp import (
    CODECS,
    RtpEndpoint,
    RtpSender,
    Upsampler,
    open_endpoint,
    parse,
    swap_bytes,
)

ULAW, SLIN16 = CODECS["ulaw"], CODECS["slin16"]


def tone(samples: int, rate: int, amplitude: int = 8000) -> bytes:
    return array(
        "h", (int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(samples))
    ).tobytes()


def samples(pcm: bytes) -> array:
    result = array("h")
    result.frombytes(pcm)
    return result


def test_ulaw_round_trip_keeps_the_signal():
    pcm = tone(160, 8000)
    payload = ULAW.encode(pcm)
    assert len(payload) == 160
    back = samples(ULAW.decode(payload))
    original = samples(pcm)
    # μ-закон теряет точность пропорционально громкости, но не больше нескольких процентов
    assert max(abs(a - b) for a, b in zip(back, original)) <= 0.04 * 8000
    assert ULAW.decode(ULAW.encode(bytes(320))) == bytes(320)


def test_ulaw_known_codes():
    # тишина кодируется 0xFF, крайние значения — 0x80 и 0x00
    assert ULAW.encode(array("h", [0, 32767, -32768]).tobytes()) == bytes([0xFF, 0x80, 0x00])


def test_slin16_is_big_endian_on_the_wire():
    pcm = tone(320, 16000)
    assert SLIN16.encode(pcm) == swap_bytes(pcm)
    assert SLIN16.decode(SLIN16.encode(pcm)) == pcm


def test_packet_round_trip():
    sender = RtpSender(ULAW)
    pcm = tone(160, 8000)
    first = sender.packet(pcm, marker=True)
    second = sender.packet(pcm)
    assert parse(first) == (0, ULAW.encode(pcm))
    assert first[1] & 0x80 and not second[1] & 0x80
    sequence = lambda p: int.from_bytes(p[2:4], "big")  # noqa: E731
    timestamp = lambda p: int.from_bytes(p[4:8], "big")  # noqa: E731
    assert sequence(second) == (sequence(first) + 1) & 0xFFFF
    # отметка времени — в отсчётах кодека: 160 на 20 мс при 8 кГц
    assert timestamp(second) == (timestamp(first) + 160) & 0xFFFFFFFF


def test_parse_skips_csrc_extension_and_padding():
    payload = bytes(range(8))
    header = bytes([0x80 | 0x20 | 0x10 | 1, 0]) + bytes(10)
    packet = header + bytes(4) + bytes([0, 0, 0, 1]) + bytes(4) + payload + bytes([0, 0, 3])
    assert parse(packet) == (0, payload)
    assert parse(b"\x00" * 20) is None
    assert parse(b"\x80") is None


def test_upsampler_doubles_rate_without_clicks_between_packets():
    upsample = Upsampler()
    first = samples(upsample(array("h", [0, 100, 200]).tobytes()))
    second = samples(upsample(array("h", [300]).tobytes()))
    assert list(first) == [0, 0, 50, 100, 150, 200]
    # первый отсчёт следующего пакета — между последним прошлым и новым
    assert list(second) == [250, 300]


@pytest.mark.parametrize("codec", [ULAW, SLIN16], ids=["ulaw", "slin16"])
async def test_endpoint_paces_answer_and_flushes(codec):
    heard: list[bytes] = []
    receiver = await open_endpoint(0, heard.append, codec, "127.0.0.1")
    port = receiver.transport.get_extra_info("sockname")[1]
    sender = await open_endpoint(0, lambda pcm: None, codec, "127.0.0.1")
    sender.remote = ("127.0.0.1", port)

    pcm = tone(codec.rate // 10, codec.rate)  # 100 мс
    started = time.monotonic()
    sender.play(pcm)
    await asyncio.wait_for(sender.drained(), 1)
    assert time.monotonic() - started >= 0.07
    await asyncio.sleep(0.05)
    assert len(heard) == 5
    assert len(b"".join(heard)) == len(pcm)

    sequence_before_flush = sender.sender.sequence
    sender.play(pcm * 10)
    await asyncio.sleep(0.05)
    sender.flush()
    await asyncio.wait_for(sender.drained(), 0.1)
    sent_at_drain = sender.sender.sequence
    await asyncio.sleep(0.1)
    # drained() reports the local queue, not UDP receive completion. A packet
    # already handed to the OS may arrive later; the sender must stop queuing
    # additional packets after flush.
    assert sender.sender.sequence == sent_at_drain
    assert len(heard) <= 5 + sent_at_drain - sequence_before_flush
    sender.close()
    receiver.close()


def test_answer_goes_where_audio_came_from_when_port_unknown():
    endpoint = RtpEndpoint(lambda pcm: None, ULAW)
    endpoint.datagram_received(RtpSender(ULAW).packet(tone(160, 8000)), ("10.0.0.5", 12000))
    assert endpoint.source == ("10.0.0.5", 12000)
