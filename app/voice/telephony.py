"""Звонки через Asterisk: ARI управляет каналами, звук идёт по RTP через внешний медиаканал.

Звонок на 100 попадает в Stasis(clinic, inbound). Шлюз отвечает, собирает мост из канала пациента
и внешнего медиаканала slin16 к своему UDP-порту и ведёт разговор через CallSession — так же, как
звонок из браузера. Перевод: исходящий канал к регистратору в тот же мост, пока он не ответил —
музыка; ответил — ассистент из моста уходит. Не ответил — по разделу 7.5.
"""

import asyncio
import base64
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.voice.rtp import CODECS, RATE, Codec, RtpEndpoint, Upsampler, open_endpoint

log = logging.getLogger(__name__)

# Столько без RTP — и шлюз считает, что пациент молчит
SILENCE_GAP_S = 0.1
# Раздел 7.5: сколько ждать ответа регистратора по приоритету перевода
OPERATOR_TIMEOUT_S = {"P1": 20, "P2": 30, "P3": 90}
# Сколько ждать ответа пациента на исходящее напоминание
OUTBOUND_RING_S = 30
# Причины Q.850, при которых регистратор был досягаем, но не взял трубку
NOT_ANSWERED = {16, 17, 18, 19, 21}

TransferOutcome = Literal["connected", "no_answer", "failed", "aborted"]


@dataclass(frozen=True)
class TransferResult:
    """Чем кончилась одна попытка перевода. Одна попытка — один результат.

    attempt_id — ключ идемпотентности для получателя (заявка T08): повтор того же события
    транспортом не должен создать вторую заявку. reason — технический код без данных пациента.
    """

    attempt_id: str
    call_id: str
    route: str | None
    priority: str
    outcome: TransferOutcome
    reason: str | None = None


class TelephonySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEPHONY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = False
    ari_url: str = "http://127.0.0.1:8088"
    # Те же ARI_USER и ARI_PASSWORD, что подставляются в конфигурацию Asterisk
    ari_user: str = Field(
        default="clinic", validation_alias=AliasChoices("TELEPHONY_ARI_USER", "ARI_USER")
    )
    ari_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("TELEPHONY_ARI_PASSWORD", "ARI_PASSWORD"),
    )
    app: str = "clinic"
    # Куда Asterisk шлёт звук пациента: адрес шлюза, как его видит Asterisk
    media_host: str = "host.docker.internal"
    media_bind: str = "0.0.0.0"
    media_ports: tuple[int, int] = (40000, 40099)
    # Куда шлюз шлёт ответ: порты RTP Asterisk опубликованы на этом адресе
    asterisk_rtp_host: str = "127.0.0.1"
    # Формат звука внешнего медиаканала. slin16 Asterisk 20 из пакетов Ubuntu принимать не умеет
    media_format: Literal["ulaw", "slin16"] = "ulaw"
    operator_endpoint: str = "PJSIP/operator"


class AriError(Exception):
    pass


class Ari:
    """REST и события ARI. Только то, что нужно шлюзу."""

    def __init__(self, settings: TelephonySettings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.connected = asyncio.Event()
        auth = (settings.ari_user, settings.ari_password.get_secret_value())
        self.http = http or httpx.AsyncClient(
            base_url=f"{settings.ari_url.rstrip('/')}/ari/", auth=auth, timeout=5
        )

    async def _call(self, method: str, path: str, **params) -> dict | list | None:
        response = await self.http.request(method, path, params=params or None)
        if response.status_code == 404 and method == "DELETE":
            return None  # канал или мост уже закрыт
        if response.status_code >= 400:
            raise AriError(f"{method} {path}: {response.status_code}")
        return response.json() if response.content else None

    async def answer(self, channel: str) -> None:
        await self._call("POST", f"channels/{channel}/answer")

    async def hangup(self, channel: str) -> None:
        await self._call("DELETE", f"channels/{channel}")

    async def bridge(self) -> str:
        return (await self._call("POST", "bridges", type="mixing"))["id"]

    async def add(self, bridge: str, *channels: str) -> None:
        await self._call("POST", f"bridges/{bridge}/addChannel", channel=",".join(channels))

    async def remove(self, bridge: str, channel: str) -> None:
        await self._call("POST", f"bridges/{bridge}/removeChannel", channel=channel)

    async def destroy(self, bridge: str) -> None:
        await self._call("DELETE", f"bridges/{bridge}")

    async def music(self, bridge: str, on: bool) -> None:
        await self._call("POST" if on else "DELETE", f"bridges/{bridge}/moh")

    async def external_media(self, host: str) -> tuple[str, int | None]:
        """Внешний медиаканал slin16 к шлюзу; порт RTP Asterisk для ответа, если он его сообщил."""
        channel = await self._call(
            "POST",
            "channels/externalMedia",
            app=self.settings.app,
            external_host=host,
            format=self.settings.media_format,
            encapsulation="rtp",
            transport="udp",
            direction="both",
        )
        port = (channel.get("channelvars") or {}).get("UNICASTRTP_LOCAL_PORT")
        if port is None:
            variable = await self._call(
                "GET", f"channels/{channel['id']}/variable", variable="UNICASTRTP_LOCAL_PORT"
            )
            port = (variable or {}).get("value")
        return channel["id"], int(port) if port else None

    async def originate(
        self, endpoint: str, args: list[str], timeout: int, channel_id: str | None = None
    ) -> str:
        # Свой channelId: после сбоя процесса звонок можно найти в АТС по попытке
        extra = {"channelId": channel_id} if channel_id else {}
        channel = await self._call(
            "POST",
            "channels",
            endpoint=endpoint,
            app=self.settings.app,
            appArgs=",".join(args),
            timeout=timeout,
            **extra,
        )
        return channel["id"]

    async def events(self) -> AsyncIterator[dict]:
        import websockets

        url = self.settings.ari_url.replace("http", "ws", 1).rstrip("/")
        secret = f"{self.settings.ari_user}:{self.settings.ari_password.get_secret_value()}"
        header = "Basic " + base64.b64encode(secret.encode()).decode()
        async with websockets.connect(
            f"{url}/ari/events?app={self.settings.app}",
            additional_headers={"Authorization": header},
        ) as socket:
            self.connected.set()
            try:
                async for message in socket:
                    yield json.loads(message)
            finally:
                self.connected.clear()

    async def aclose(self) -> None:
        await self.http.aclose()


@dataclass
class PhoneCall:
    caller: str
    port: int
    rtp: RtpEndpoint | None = None
    bridge: str | None = None
    media: str | None = None
    session: object | None = None
    operator: str | None = None
    priority: str = "P3"
    route: str | None = None
    connected: bool = False
    # Разговор с ассистентом закончен: прощание или перевод уже начаты
    finishing: bool = False
    attempt: str | None = None
    reported: bool = False
    rate: int = 24000
    codec: Codec = field(default_factory=lambda: CODECS["ulaw"])
    tasks: set[asyncio.Task] = field(default_factory=set)


class PhoneChannel:
    """События CallSession для телефонного звонка: звук — в RTP, остановка — сброс очереди."""

    def __init__(self, telephony: "Telephony", call: PhoneCall):
        self.telephony = telephony
        self.call = call

    async def send_json(self, message: dict) -> None:
        kind = message["type"]
        # Текст разговора в журнал не пишем: только событие и номер реплики
        log.debug("звонок %s: %s %s", self.call.caller, kind, message.get("turn_id", ""))
        if kind == "speak":
            self.call.rate = message["sample_rate"]
        elif kind == "stop":
            self.call.rtp.flush()
        elif kind == "timing":
            log.info("звонок %s: задержки %s", self.call.caller, _numbers(message))
        elif kind == "end":
            self.telephony.spawn(self.call, self.telephony.finish(self.call, message))

    async def send_bytes(self, data: bytes) -> None:
        codec = self.call.codec
        self.call.rtp.play(self.telephony.resample(data, self.call.rate, codec.rate))


SessionFactory = Callable[[str, PhoneChannel], object]
# Исходящий звонок: контекст напоминания, признак автоответчика и куда сообщить исход разговора
OutboundFactory = Callable[[str, PhoneChannel, object, bool, Callable], object]


class OutboundEvents(Protocol):
    """Исполнитель напоминаний: ему шлюз сообщает ответ абонента и исход попытки."""

    async def answered(self, attempt_id: str) -> None: ...
    async def finished(
        self,
        attempt_id: str,
        result: str,
        reason: str | None = None,
        mis_status: str | None = None,
    ) -> bool: ...


@dataclass
class Outgoing:
    attempt_id: str
    context: object
    channel: str | None = None
    answered: bool = False


TransferSink = Callable[[TransferResult], Awaitable[None] | None]
# Реплика по шаблону: PCM и его частота
Say = Callable[[str], Awaitable[tuple[bytes, int]]]


class Telephony:
    def __init__(
        self,
        ari: Ari,
        settings: TelephonySettings,
        sessions: SessionFactory,
        say: Say,
        resample: Callable[[bytes, int, int], bytes],
        on_transfer: TransferSink | None = None,
        outbound: OutboundFactory | None = None,
        reminders: OutboundEvents | None = None,
    ):
        self.ari = ari
        self.on_transfer = on_transfer
        self.outbound = outbound
        self.reminders = reminders
        # Исходящие вызовы по номеру попытки и по каналу
        self._outgoing: dict[str, Outgoing] = {}
        self._outgoing_channels: dict[str, Outgoing] = {}
        self.settings = settings
        self.sessions = sessions
        self.say = say
        self.resample = resample
        self.calls: dict[str, PhoneCall] = {}
        self._media: dict[str, PhoneCall] = {}
        self._operators: dict[str, PhoneCall] = {}
        low, high = settings.media_ports
        self._ports = list(range(low, high + 1))

    async def run(self) -> None:
        """Слушать события ARI; соединение потеряно — переподключиться."""
        while True:
            try:
                async for event in self.ari.events():
                    await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("связь с ARI потеряна, переподключаюсь")
            await asyncio.sleep(2)

    async def handle(self, event: dict) -> None:
        kind = event.get("type")
        channel = (event.get("channel") or {}).get("id")
        try:
            if kind == "StasisStart":
                await self._started(channel, event.get("args") or [])
            elif kind in ("StasisEnd", "ChannelDestroyed"):
                await self._gone(channel, event.get("cause"))
        except Exception:
            log.exception("ошибка обработки %s", kind)

    async def _started(self, channel: str, args: list[str]) -> None:
        if channel in self._media:
            return
        if args[:1] == ["operator"]:
            call = self._operators.get(channel)
            if call is not None:
                await self._operator_answered(call, channel)
            return
        if args[:1] == ["outbound"]:
            await self._outbound_answered(channel, args[1:])
            return
        await self._incoming(channel)

    async def call_out(
        self, attempt_id: str, endpoint: str, context: object, channel_id: str | None = None
    ) -> None:
        """Исходящее напоминание. Исход придёт исполнителю через reminders."""
        from app.reminders.ports import DialRejected

        if self.outbound is None or self.reminders is None:
            raise DialRejected()
        outgoing = Outgoing(attempt_id, context)
        outgoing.channel = channel_id
        self._outgoing[attempt_id] = outgoing
        if channel_id:
            self._outgoing_channels[channel_id] = outgoing
        try:
            outgoing.channel = await self.ari.originate(
                endpoint, ["outbound", attempt_id], OUTBOUND_RING_S, channel_id
            )
        except AriError as error:
            # Только явный отказ 4xx означает, что звонка точно не было.
            # 5xx/потерянный ответ оставляют попытку dialing до unknown/manual.
            if any(str(error).endswith(": " + str(code)) for code in (400, 401, 403, 404, 422)):
                self._outgoing.pop(attempt_id, None)
                if channel_id:
                    self._outgoing_channels.pop(channel_id, None)
                raise DialRejected() from None
            raise
        self._outgoing_channels[outgoing.channel] = outgoing

    async def _outbound_answered(self, channel: str, args: list[str]) -> None:
        outgoing = self._outgoing.get(args[0]) if args else None
        if outgoing is None or outgoing.answered or channel in self.calls:
            # Чужой или повторный StasisStart: звонок не наш
            await self.ari.hangup(channel)
            return
        outgoing.answered = True
        outgoing.channel = channel
        self._outgoing_channels[channel] = outgoing
        if await self.reminders.answered(outgoing.attempt_id) is False:
            await self.ari.hangup(channel)
            return
        # Asterisk кладёт результат AMD() третьим аргументом, если dialplan его запускал
        machine = args[1:2] == ["MACHINE"]

        async def done(outcome: str | None, mis_status: str | None = None) -> None:
            await self.reminders.finished(
                outgoing.attempt_id, outcome or "INCOMPLETE", None, mis_status
            )

        await self._incoming(
            channel,
            lambda call_id, phone: self.outbound(call_id, phone, outgoing.context, machine, done),
            answer=False,
        )

    async def _incoming(
        self, caller: str, sessions: SessionFactory | None = None, answer: bool = True
    ) -> None:
        if not self._ports:
            log.error("нет свободных портов RTP")
            await self.ari.hangup(caller)
            return
        codec = CODECS[self.settings.media_format]
        call = PhoneCall(caller=caller, port=self._ports.pop(0), codec=codec)
        self.calls[caller] = call
        audio: asyncio.Queue[bytes] = asyncio.Queue()
        # Шлюз слушает 16 кГц; ulaw приходит с частотой 8 кГц
        upsample = Upsampler() if codec.rate != RATE else (lambda pcm: pcm)

        call.rtp = await open_endpoint(
            call.port, lambda pcm: audio.put_nowait(upsample(pcm)), codec, self.settings.media_bind
        )
        if answer:
            await self.ari.answer(caller)
        call.media, rtp_port = await self.ari.external_media(
            f"{self.settings.media_host}:{call.port}"
        )
        self._media[call.media] = call
        if rtp_port is not None:
            call.rtp.remote = (self.settings.asterisk_rtp_host, rtp_port)
        call.bridge = await self.ari.bridge()
        await self.ari.add(call.bridge, caller, call.media)

        call.session = (sessions or self.sessions)(caller, PhoneChannel(self, call))

        async def listen() -> None:
            # Пока пациент молчит, Asterisk может не слать RTP совсем. Без кадров тишины шлюз
            # не заметит конец фразы, поэтому паузы в потоке заполняем тишиной сами.
            while True:
                try:
                    pcm = await asyncio.wait_for(audio.get(), SILENCE_GAP_S)
                except TimeoutError:
                    pcm = bytes(int(RATE * SILENCE_GAP_S) * 2)
                await call.session.feed(pcm)

        self.spawn(call, listen())
        await call.session.start()

    async def finish(self, call: PhoneCall, end: dict) -> None:
        # Сессия шлёт end один раз; повтор события не должен второй раз звать регистратора
        if call.finishing:
            return
        call.finishing = True
        await call.rtp.drained()
        if end.get("reason") == "transfer":
            await self.transfer(call, end.get("priority") or "P3", end.get("route"))
        else:
            await self.ari.hangup(call.caller)

    async def transfer(self, call: PhoneCall, priority: str, route: str | None = None) -> None:
        call.priority = priority
        call.route = route
        # Намерение перевести ещё не перевод: исход попытки сообщает transfer_result
        call.attempt = uuid.uuid4().hex
        timeout = OPERATOR_TIMEOUT_S.get(priority, OPERATOR_TIMEOUT_S["P3"])
        await self._music(call, True)
        try:
            call.operator = await self.ari.originate(
                self.settings.operator_endpoint, ["operator", call.caller], timeout
            )
        except AriError as error:
            # Обычно софтфон регистратора не в сети: это не сбой шлюза, а неотвеченный перевод
            log.warning("регистратор недоступен: %s", error)
            await self._report(call, "failed", "originate_rejected")
            await self._operator_failed(call)
            return
        self._operators[call.operator] = call

    async def _operator_answered(self, call: PhoneCall, operator: str) -> None:
        if call.caller not in self.calls or call.operator != operator:
            # Пациент уже положил трубку: соединять регистратора не с кем
            self._operators.pop(operator, None)
            await self.ari.hangup(operator)
            return
        await self._music(call, False)
        try:
            await self.ari.add(call.bridge, call.operator)
        except AriError:
            log.warning("не удалось добавить регистратора в разговор")
            self._operators.pop(operator, None)
            await self.ari.hangup(operator)
            await self._report(call, "failed", "bridge_rejected")
            self.spawn(call, self._operator_failed(call))
            return
        call.connected = True
        await self._report(call, "connected")
        if call.media is not None:
            await self.ari.remove(call.bridge, call.media)
            await self.ari.hangup(call.media)
        log.info("звонок %s передан регистратору", call.caller)

    async def _report(self, call: PhoneCall, outcome: TransferOutcome, reason=None) -> None:
        if call.attempt is None or call.reported:
            return
        call.reported = True
        result = TransferResult(
            call.attempt, call.caller, call.route, call.priority, outcome, reason
        )
        log.info("звонок %s: перевод %s %s", call.caller, outcome, reason or "")
        if self.on_transfer is None:
            return
        try:
            delivered = self.on_transfer(result)
            if inspect.isawaitable(delivered):
                await delivered
        except Exception:
            # Получатель результата не должен ломать сам звонок
            log.exception("не удалось передать исход перевода")

    async def _operator_failed(self, call: PhoneCall) -> None:
        # Раздел 7.5: на P1 повторяем 103/112 и завершаем. Обратный звонок для P2 и P3 должен
        # предложить диалог; пока он не знает о неответе, ничего не обещаем
        await self._music(call, False)
        template = "emergency.no_answer" if call.priority == "P1" else "transfer.no_answer"
        pcm, rate = await self.say(template)
        call.rtp.play(self.resample(pcm, rate, call.codec.rate))
        await call.rtp.drained()
        await self.ari.hangup(call.caller)

    async def _gone(self, channel: str, cause: int | None = None) -> None:
        outgoing = self._outgoing_channels.pop(channel, None)
        if outgoing is not None:
            self._outgoing.pop(outgoing.attempt_id, None)
            if not outgoing.answered:
                # Не взяли трубку или не дозвонились: разговора не было
                result = "NO_ANSWER" if cause is None or cause in NOT_ANSWERED else "FAILED"
                await self.reminders.finished(
                    outgoing.attempt_id, result, f"q850_{cause}" if cause else None
                )
                return
        if channel in self._operators:
            call = self._operators.pop(channel)
            if call.connected:
                await self.ari.hangup(call.caller)
            elif call.caller in self.calls:
                outcome = "no_answer" if cause is None or cause in NOT_ANSWERED else "failed"
                await self._report(call, outcome, f"q850_{cause}" if cause else None)
                # Сообщение о неответе — отдельной задачей: события ARI (отбой пациента)
                # не должны ждать, пока оно доиграет
                self.spawn(call, self._operator_failed(call))
            return
        call = self.calls.pop(channel, None)
        if call is not None:
            await self._close(call)

    async def _close(self, call: PhoneCall, reason: str = "caller_hung_up") -> None:
        # Пациент ушёл (или шлюз остановлен), пока ждали регистратора
        await self._report(call, "aborted", reason)
        for task in call.tasks:
            task.cancel()
        if call.session is not None:
            await call.session.close()
        if call.operator is not None and not call.connected:
            await self.ari.hangup(call.operator)
        if call.media is not None:
            self._media.pop(call.media, None)
            await self.ari.hangup(call.media)
        if call.bridge is not None:
            await self.ari.destroy(call.bridge)
        if call.rtp is not None:
            call.rtp.close()
        self._ports.append(call.port)

    async def _music(self, call: PhoneCall, on: bool) -> None:
        try:
            await self.ari.music(call.bridge, on)
        except AriError:
            log.warning("музыка ожидания недоступна")

    def spawn(self, call: PhoneCall, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        call.tasks.add(task)
        task.add_done_callback(call.tasks.discard)

    async def close(self) -> None:
        for call in list(self.calls.values()):
            await self.ari.hangup(call.caller)
            await self._close(call, "gateway_stopped")
        self.calls.clear()


def _numbers(message: dict) -> dict:
    return {k: v for k, v in message.items() if isinstance(v, int)}
