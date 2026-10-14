"""RTP для внешнего медиаканала Asterisk: slin16 — PCM 16 бит, 16 кГц, порядок байт сети.

Шлюз внутри работает с PCM в порядке байт x86 (little-endian), поэтому на входе и выходе
байты в каждом отсчёте переставляем.
"""

import asyncio
import random
import time
from array import array
from collections.abc import Callable

RATE = 16000
PACKET_SAMPLES = 320  # 20 мс
PACKET_BYTES = PACKET_SAMPLES * 2
# Так Asterisk обозначает slin16; если он пришлёт другой номер, отвечаем тем же
SLIN16_PAYLOAD = 118


def swap_bytes(pcm: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    samples.byteswap()
    return samples.tobytes()


def parse(packet: bytes) -> tuple[int, bytes] | None:
    """Номер формата и звук из пакета RTP; None — не RTP версии 2."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        return None
    offset = 12 + 4 * (packet[0] & 0x0F)
    if packet[0] & 0x10:
        if len(packet) < offset + 4:
            return None
        offset += 4 + 4 * int.from_bytes(packet[offset + 2 : offset + 4], "big")
    end = len(packet)
    if packet[0] & 0x20:
        end -= packet[-1]
    if end < offset:
        return None
    return packet[1] & 0x7F, swap_bytes(packet[offset:end])


class RtpSender:
    def __init__(self, payload_type: int = SLIN16_PAYLOAD):
        self.payload_type = payload_type
        self.sequence = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self.ssrc = random.getrandbits(32)

    def packet(self, pcm: bytes, marker: bool = False) -> bytes:
        header = bytes(
            [
                0x80,
                (0x80 if marker else 0) | self.payload_type,
                *self.sequence.to_bytes(2, "big"),
                *self.timestamp.to_bytes(4, "big"),
                *self.ssrc.to_bytes(4, "big"),
            ]
        )
        self.sequence = (self.sequence + 1) & 0xFFFF
        self.timestamp = (self.timestamp + len(pcm) // 2) & 0xFFFFFFFF
        return header + swap_bytes(pcm)


class RtpEndpoint(asyncio.DatagramProtocol):
    """UDP-порт одного звонка: звук пациента — в on_audio, ответ ассистента уходит с темпом 20 мс."""

    def __init__(
        self, on_audio: Callable[[bytes], None], clock: Callable[[], float] = time.monotonic
    ):
        self.on_audio = on_audio
        self.clock = clock
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self.source: tuple[str, int] | None = None
        self.sender = RtpSender()
        self._buffer = bytearray()
        self._pacer: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    def connection_made(self, transport) -> None:
        self.transport = transport
        self._pacer = asyncio.get_running_loop().create_task(self._pace())

    def datagram_received(self, data: bytes, addr) -> None:
        parsed = parse(data)
        if parsed is None:
            return
        self.source = addr
        self.sender.payload_type = parsed[0]
        self.on_audio(parsed[1])

    def play(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)
        self._idle.clear()

    def flush(self) -> None:
        """Пациент перебил: всё, что не успело уйти, выбрасываем."""
        self._buffer.clear()
        self._idle.set()

    async def drained(self) -> None:
        await self._idle.wait()

    def close(self) -> None:
        if self._pacer is not None:
            self._pacer.cancel()
        if self.transport is not None:
            self.transport.close()
        self._idle.set()

    async def _pace(self) -> None:
        next_at = self.clock()
        talking = False
        while True:
            target = self.remote or self.source
            if self._buffer and target is not None and self.transport is not None:
                chunk = bytes(self._buffer[:PACKET_BYTES]).ljust(PACKET_BYTES, b"\0")
                del self._buffer[:PACKET_BYTES]
                self.transport.sendto(self.sender.packet(chunk, marker=not talking), target)
                talking = True
                if not self._buffer:
                    self._idle.set()
                next_at += PACKET_SAMPLES / RATE
            else:
                talking = False
                next_at = self.clock() + PACKET_SAMPLES / RATE
            await asyncio.sleep(max(0.0, next_at - self.clock()))


async def open_endpoint(
    port: int, on_audio: Callable[[bytes], None], host: str = "0.0.0.0"
) -> RtpEndpoint:
    loop = asyncio.get_running_loop()
    _, endpoint = await loop.create_datagram_endpoint(
        lambda: RtpEndpoint(on_audio), local_addr=(host, port)
    )
    return endpoint
