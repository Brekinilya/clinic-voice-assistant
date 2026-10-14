"""RTP для внешнего медиаканала Asterisk: ulaw 8 кГц или slin16.

Шлюз внутри работает с PCM 16 бит в порядке байт x86 (little-endian). Кодек переводит звук
пакета в такой PCM и обратно. По умолчанию ulaw: Asterisk 20 из пакетов Ubuntu принимает от
внешнего медиаканала только его, slin16 молча отбрасывает. Да и звонок по телефону — те же 8 кГц.
"""

import asyncio
import random
import time
from array import array
from collections.abc import Callable

RATE = 16000  # частота, с которой работает шлюз
PACKET_MS = 20


def swap_bytes(pcm: bytes) -> bytes:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    samples.byteswap()
    return samples.tobytes()


def _ulaw_to_linear(code: int) -> int:
    code = ~code & 0xFF
    magnitude = (((code & 0x0F) << 3) + 0x84) << ((code >> 4) & 0x07)
    return 0x84 - magnitude if code & 0x80 else magnitude - 0x84


def _linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), 32635) + 0x84
    exponent = 7
    while exponent and not sample & (0x4000 >> (7 - exponent)):
        exponent -= 1
    return ~(sign | exponent << 4 | (sample >> (exponent + 3)) & 0x0F) & 0xFF


# G.711 μ-закон таблицами: 256 значений на разбор и по значению на каждый 16-битный отсчёт
_ULAW_DECODE = array("h", (_ulaw_to_linear(code) for code in range(256)))
_ULAW_ENCODE = bytes(
    _linear_to_ulaw(sample - 65536 if sample > 32767 else sample) for sample in range(65536)
)


class Codec:
    def __init__(self, name: str, payload_type: int, rate: int):
        self.name = name
        self.payload_type = payload_type
        self.rate = rate

    @property
    def packet_bytes(self) -> int:
        """Сколько байт PCM уходит в пакет на 20 мс."""
        return self.rate * PACKET_MS // 1000 * 2

    def decode(self, payload: bytes) -> bytes:
        raise NotImplementedError

    def encode(self, pcm: bytes) -> bytes:
        raise NotImplementedError


class Ulaw(Codec):
    def __init__(self):
        super().__init__("ulaw", 0, 8000)

    def decode(self, payload: bytes) -> bytes:
        return array("h", (_ULAW_DECODE[code] for code in payload)).tobytes()

    def encode(self, pcm: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        return bytes(_ULAW_ENCODE[sample & 0xFFFF] for sample in samples)


class Slin16(Codec):
    def __init__(self):
        # Так slin16 обозначает Asterisk
        super().__init__("slin16", 118, 16000)

    def decode(self, payload: bytes) -> bytes:
        return swap_bytes(payload)

    def encode(self, pcm: bytes) -> bytes:
        return swap_bytes(pcm)


CODECS: dict[str, Codec] = {"ulaw": Ulaw(), "slin16": Slin16()}


class Upsampler:
    """8 → 16 кГц для входящего звука: между отсчётами — среднее. Последний отсчёт пакета
    помнит, чтобы на стыке пакетов не было щелчка."""

    def __init__(self):
        self.previous = 0

    def __call__(self, pcm: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        result = array("h", bytes(len(samples) * 4))
        previous = self.previous
        for i, sample in enumerate(samples):
            result[2 * i] = (previous + sample) // 2
            result[2 * i + 1] = sample
            previous = sample
        self.previous = previous
        return result.tobytes()


def parse(packet: bytes) -> tuple[int, bytes] | None:
    """Номер формата и полезная нагрузка пакета RTP; None — не RTP версии 2."""
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
    return packet[1] & 0x7F, packet[offset:end]


class RtpSender:
    def __init__(self, codec: Codec):
        self.codec = codec
        self.payload_type = codec.payload_type
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
        return header + self.codec.encode(pcm)


class RtpEndpoint(asyncio.DatagramProtocol):
    """UDP-порт одного звонка: звук пациента — в on_audio (PCM с частотой кодека),
    ответ ассистента (PCM той же частоты) уходит пакетами по 20 мс."""

    def __init__(
        self,
        on_audio: Callable[[bytes], None],
        codec: Codec,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.on_audio = on_audio
        self.codec = codec
        self.clock = clock
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self.source: tuple[str, int] | None = None
        self.sender = RtpSender(codec)
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
        self.on_audio(self.codec.decode(parsed[1]))

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
        size = self.codec.packet_bytes
        step = PACKET_MS / 1000
        next_at = self.clock()
        talking = False
        while True:
            target = self.remote or self.source
            if self._buffer and target is not None and self.transport is not None:
                chunk = bytes(self._buffer[:size]).ljust(size, b"\0")
                del self._buffer[:size]
                self.transport.sendto(self.sender.packet(chunk, marker=not talking), target)
                talking = True
                if not self._buffer:
                    self._idle.set()
                next_at += step
            else:
                talking = False
                next_at = self.clock() + step
            await asyncio.sleep(max(0.0, next_at - self.clock()))


async def open_endpoint(
    port: int, on_audio: Callable[[bytes], None], codec: Codec, host: str = "0.0.0.0"
) -> RtpEndpoint:
    loop = asyncio.get_running_loop()
    _, endpoint = await loop.create_datagram_endpoint(
        lambda: RtpEndpoint(on_audio, codec), local_addr=(host, port)
    )
    return endpoint
