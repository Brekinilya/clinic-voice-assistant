"""Звонки через Asterisk: ARI управляет каналами, звук идёт по RTP через внешний медиаканал.

Звонок на 100 попадает в Stasis(clinic, inbound). Шлюз отвечает, собирает мост из канала пациента
и внешнего медиаканала slin16 к своему UDP-порту и ведёт разговор через CallSession — так же, как
звонок из браузера. Перевод: исходящий канал к регистратору в тот же мост, пока он не ответил —
музыка; ответил — ассистент из моста уходит. Не ответил — по разделу 7.5.
"""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.voice.rtp import RATE, RtpEndpoint, open_endpoint

log = logging.getLogger(__name__)

# Раздел 7.5: сколько ждать ответа регистратора по приоритету перевода
OPERATOR_TIMEOUT_S = {"P1": 20, "P2": 30, "P3": 90}


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
    operator_endpoint: str = "PJSIP/operator"


class AriError(Exception):
    pass


class Ari:
    """REST и события ARI. Только то, что нужно шлюзу."""

    def __init__(self, settings: TelephonySettings, http: httpx.AsyncClient | None = None):
        self.settings = settings
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
            format="slin16",
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

    async def originate(self, endpoint: str, args: list[str], timeout: int) -> str:
        channel = await self._call(
            "POST",
            "channels",
            endpoint=endpoint,
            app=self.settings.app,
            appArgs=",".join(args),
            timeout=timeout,
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
            async for message in socket:
                yield json.loads(message)

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
    connected: bool = False
    rate: int = 24000
    tasks: set[asyncio.Task] = field(default_factory=set)


class PhoneChannel:
    """События CallSession для телефонного звонка: звук — в RTP, остановка — сброс очереди."""

    def __init__(self, telephony: "Telephony", call: PhoneCall):
        self.telephony = telephony
        self.call = call

    async def send_json(self, message: dict) -> None:
        kind = message["type"]
        if kind == "speak":
            self.call.rate = message["sample_rate"]
        elif kind == "stop":
            self.call.rtp.flush()
        elif kind == "timing":
            log.info("звонок %s: задержки %s", self.call.caller, _numbers(message))
        elif kind == "end":
            self.telephony.spawn(self.call, self.telephony.finish(self.call, message))

    async def send_bytes(self, data: bytes) -> None:
        self.call.rtp.play(self.telephony.resample(data, self.call.rate, RATE))


SessionFactory = Callable[[str, PhoneChannel], object]
Say = Callable[[str], Awaitable[bytes]]


class Telephony:
    def __init__(
        self,
        ari: Ari,
        settings: TelephonySettings,
        sessions: SessionFactory,
        say: Say,
        resample: Callable[[bytes, int, int], bytes],
    ):
        self.ari = ari
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
                await self._gone(channel)
        except Exception:
            log.exception("ошибка обработки %s", kind)

    async def _started(self, channel: str, args: list[str]) -> None:
        if channel in self._media:
            return
        if args[:1] == ["operator"]:
            call = self._operators.get(channel)
            if call is not None:
                await self._operator_answered(call)
            return
        await self._incoming(channel)

    async def _incoming(self, caller: str) -> None:
        if not self._ports:
            log.error("нет свободных портов RTP")
            await self.ari.hangup(caller)
            return
        call = PhoneCall(caller=caller, port=self._ports.pop(0))
        self.calls[caller] = call
        audio: asyncio.Queue[bytes] = asyncio.Queue()

        call.rtp = await open_endpoint(call.port, audio.put_nowait, self.settings.media_bind)
        await self.ari.answer(caller)
        call.media, rtp_port = await self.ari.external_media(
            f"{self.settings.media_host}:{call.port}"
        )
        self._media[call.media] = call
        if rtp_port is not None:
            call.rtp.remote = (self.settings.asterisk_rtp_host, rtp_port)
        call.bridge = await self.ari.bridge()
        await self.ari.add(call.bridge, caller, call.media)

        call.session = self.sessions(caller, PhoneChannel(self, call))

        async def listen() -> None:
            while True:
                await call.session.feed(await audio.get())

        self.spawn(call, listen())
        await call.session.start()

    async def finish(self, call: PhoneCall, end: dict) -> None:
        await call.rtp.drained()
        if end.get("reason") == "transfer":
            await self.transfer(call, end.get("priority") or "P3")
        else:
            await self.ari.hangup(call.caller)

    async def transfer(self, call: PhoneCall, priority: str) -> None:
        call.priority = priority
        timeout = OPERATOR_TIMEOUT_S.get(priority, OPERATOR_TIMEOUT_S["P3"])
        await self._music(call, True)
        try:
            call.operator = await self.ari.originate(
                self.settings.operator_endpoint, ["operator", call.caller], timeout
            )
        except AriError:
            log.exception("не удалось вызвать регистратора")
            await self._operator_failed(call)
            return
        self._operators[call.operator] = call

    async def _operator_answered(self, call: PhoneCall) -> None:
        call.connected = True
        await self._music(call, False)
        await self.ari.add(call.bridge, call.operator)
        if call.media is not None:
            await self.ari.remove(call.bridge, call.media)
            await self.ari.hangup(call.media)
        log.info("звонок %s передан регистратору", call.caller)

    async def _operator_failed(self, call: PhoneCall) -> None:
        # Раздел 7.5: на P1 повторяем 103/112 и завершаем; иначе — обратный звонок по запросу,
        # который диалог уже подготовил для регистратора
        await self._music(call, False)
        template = "emergency.no_answer" if call.priority == "P1" else "callback.created"
        call.rtp.play(await self.say(template))
        await call.rtp.drained()
        await self.ari.hangup(call.caller)

    async def _gone(self, channel: str) -> None:
        if channel in self._operators:
            call = self._operators.pop(channel)
            if call.connected:
                await self.ari.hangup(call.caller)
            elif call.caller in self.calls:
                await self._operator_failed(call)
            return
        call = self.calls.pop(channel, None)
        if call is not None:
            await self._close(call)

    async def _close(self, call: PhoneCall) -> None:
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
            await self._close(call)
        self.calls.clear()


def _numbers(message: dict) -> dict:
    return {k: v for k, v in message.items() if isinstance(v, int)}
