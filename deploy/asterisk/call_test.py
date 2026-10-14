"""Звонок в регистратуру без софтфона: «пациент» — Local-канал, которым управляет этот скрипт.

Скрипт звонит на 100, ждёт, пока ассистент договорит (TALK_DETECT), и проигрывает реплики
из data/telephony/sounds (*.wav16, 16 кГц). Разговор в обе стороны пишется в
/var/spool/asterisk/recording/<имя>.wav. Запускать на машине с Asterisk:

    python deploy/asterisk/call_test.py want name first yes no
    python deploy/asterisk/call_test.py want barge:name     — перебить ассистента репликой name
"""

import argparse
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx
import websockets

APP = "patient"
ROOT = Path(__file__).resolve().parents[2]


def credentials() -> tuple[str, str]:
    values = dict(os.environ)
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip())
    return values["ARI_USER"], values["ARI_PASSWORD"]


async def main(steps: list[str], url: str, name: str, wait: float) -> None:
    user, password = credentials()
    http = httpx.AsyncClient(base_url=f"{url}/ari/", auth=(user, password), timeout=5)
    header = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    events: asyncio.Queue[dict] = asyncio.Queue()
    started = time.monotonic()

    def note(text: str) -> None:
        print(f"{time.monotonic() - started:6.2f}  {text}", flush=True)

    async with websockets.connect(
        f"{url.replace('http', 'ws', 1)}/ari/events?app={APP}",
        additional_headers={"Authorization": header},
    ) as socket:

        async def listen() -> None:
            async for message in socket:
                await events.put(json.loads(message))

        listener = asyncio.create_task(listen())

        async def until(*kinds: str, timeout: float = 40) -> dict | None:
            deadline = time.monotonic() + timeout
            while (left := deadline - time.monotonic()) > 0:
                try:
                    event = await asyncio.wait_for(events.get(), left)
                except TimeoutError:
                    return None
                if event["type"] in kinds and event.get("channel", {}).get("id") in (patient, None):
                    return event
                if event["type"] == "StasisEnd" and event["channel"]["id"] == patient:
                    return event
            return None

        patient = None
        response = await http.post(
            "channels",
            params={
                "endpoint": "Local/100@from-phones",
                "context": "test-patient",
                "extension": "s",
                "priority": 1,
                "callerId": "Test <patient>",
            },
        )
        response.raise_for_status()
        first = await until("StasisStart")
        patient = first["channel"]["id"]
        note(f"на линии, канал {patient}")
        await http.post(
            f"channels/{patient}/variable",
            params={"variable": "TALK_DETECT(set)", "value": "1200"},
        )
        snoop = (
            await http.post(
                f"channels/{patient}/snoop",
                params={"spy": "both", "app": APP, "snoopId": f"{name}-snoop"},
            )
        ).json()["id"]
        await http.post(
            f"channels/{snoop}/record",
            params={
                "name": name,
                "format": "wav",
                "ifExists": "overwrite",
                "maxDurationSeconds": 300,
            },
        )

        hung_up = False
        for step in steps:
            barge = step.startswith("barge:")
            sound = step.split(":")[-1]
            event = await until("ChannelTalkingStarted" if barge else "ChannelTalkingFinished")
            if event is None or event["type"] == "StasisEnd":
                hung_up = event is not None
                break
            note("ассистент заговорил" if barge else "ассистент договорил")
            await asyncio.sleep(1.5 if barge else 0.3)
            note(f"пациент: {sound}")
            await http.post(f"channels/{patient}/play", params={"media": f"sound:clinic/{sound}"})
            await until("PlaybackFinished")
        if not hung_up:
            event = await until("ChannelTalkingFinished", timeout=wait)
            event = await until("StasisEnd", timeout=wait) if event else None
            hung_up = event is not None and event["type"] == "StasisEnd"
        note("шлюз положил трубку" if hung_up else "кладу трубку сам")
        if not hung_up:
            await http.delete(f"channels/{patient}")
        await http.delete(f"channels/{snoop}")
        await asyncio.sleep(0.5)
        listener.cancel()
    await http.aclose()
    note(f"запись: /var/spool/asterisk/recording/{name}.wav")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "steps", nargs="+", help="реплики из data/telephony/sounds; barge:<имя> — перебить"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--name", default=time.strftime("call-%H%M%S"))
    parser.add_argument("--wait", type=float, default=20, help="сколько ждать конца разговора, с")
    args = parser.parse_args()
    asyncio.run(main(args.steps, args.url, args.name, args.wait))
