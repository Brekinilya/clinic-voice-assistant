import asyncio
import time

from app.voice.rtp import PACKET_BYTES, RtpEndpoint, RtpSender, open_endpoint, parse, swap_bytes

PCM = (bytes(range(256)) * 3)[:640]  # 20 мс


def test_packet_round_trip():
    sender = RtpSender(payload_type=118)
    first = sender.packet(PCM, marker=True)
    second = sender.packet(PCM)
    assert parse(first) == (118, PCM)
    assert first[1] & 0x80 and not second[1] & 0x80
    sequence = lambda p: int.from_bytes(p[2:4], "big")  # noqa: E731
    timestamp = lambda p: int.from_bytes(p[4:8], "big")  # noqa: E731
    assert sequence(second) == (sequence(first) + 1) & 0xFFFF
    assert timestamp(second) == (timestamp(first) + 320) & 0xFFFFFFFF
    # в сети порядок байт отсчётов обратный
    assert first[12:] == swap_bytes(PCM)


def test_parse_skips_csrc_extension_and_padding():
    payload = swap_bytes(PCM[:8])
    header = bytes([0x80 | 0x20 | 0x10 | 1, 118]) + bytes(10)
    packet = header + bytes(4) + bytes([0, 0, 0, 1]) + bytes(4) + payload + bytes([0, 0, 3])
    assert parse(packet) == (118, PCM[:8])
    assert parse(b"\x00" * 20) is None
    assert parse(b"\x80") is None


async def test_endpoint_paces_answer_and_flushes():
    heard: list[bytes] = []
    receiver = await open_endpoint(0, heard.append, "127.0.0.1")
    port = receiver.transport.get_extra_info("sockname")[1]
    sender = await open_endpoint(0, lambda pcm: None, "127.0.0.1")
    sender.remote = ("127.0.0.1", port)

    started = time.monotonic()
    sender.play(PCM * 5)  # 100 мс
    await asyncio.wait_for(sender.drained(), 1)
    assert time.monotonic() - started >= 0.07
    await asyncio.sleep(0.05)
    assert b"".join(heard) == PCM * 5

    sender.play(PCM * 50)
    await asyncio.sleep(0.05)
    sender.flush()
    await asyncio.wait_for(sender.drained(), 0.1)
    count = len(heard)
    await asyncio.sleep(0.1)
    assert len(heard) == count < 5 + 50
    sender.close()
    receiver.close()


def test_answer_goes_where_audio_came_from_when_port_unknown():
    endpoint = RtpEndpoint(lambda pcm: None)
    endpoint.datagram_received(RtpSender(payload_type=11).packet(PCM), ("10.0.0.5", 12000))
    assert endpoint.source == ("10.0.0.5", 12000)
    assert endpoint.sender.payload_type == 11
    assert len(PCM) == PACKET_BYTES
