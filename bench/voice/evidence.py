"""Голосовые доказательства для сценариев T07, которые текстовый канал проверить не может.

Прогон T07 идёт через текстовый API: в нём нет тишины на линии, промежуточной фразы ожидания
и записи звука. Здесь те же YAML-сценарии проходят через настоящую CallSession шлюза, настоящий
DialogueService и эмулятор МИС прогона T07 (ScenarioRuntime). Подставные только VAD, распознавание
и синтез: «пациент» — кадры речи, по числу которых распознавание выдаёт реплику из YAML.

    python -m bench.voice.evidence --output-dir ../evidence

По умолчанию тайминги шлюза — из VoiceSettings, задержки МИС — как в YAML, в реальном времени
(около минуты). Итог — voice_evidence.json для прогона T07 (--voice-evidence) и voice_evidence.md.
SIP, Asterisk, модели ASR/TTS и живая МИС здесь не участвуют: это отдельные живые проверки.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.voice.endpoint import FRAME_MS
from app.voice.render import Renderer
from app.voice.session import PLAYBACK_MARGIN_S, CallSession
from app.voice.settings import VoiceSettings
from tests.scenarios.run import source_receipt
from tests.scenarios.runtime import ScenarioRuntime
from tests.scenarios.scenario_plan import load_scenarios
from tests.voice.fakes import QUIET, SPEECH, Recognizer, Synthesizer, Vad

# Что именно прогон T07 не может проверить и какие диалоги сценариев это доказывают
REQUIREMENTS = {
    "R-17": "event:silence",
    "D-13": "intermediate_wait_prompt_T03_T05",
    "D-15": "recording_engine_T13",
}
# Раздел 3.2: разговор, законченный тишиной, учитывается как оборванный
RESULT_BY_END = {"silence": "DROPPED"}
# Первая фраза «пациента» — столько кадров речи, следующие — на кадр больше
FIRST_WORDS = 20
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
REPLY_TIMEOUT_S = 40.0


@dataclass
class Evidence:
    scenario_id: str
    label: str
    requirement: str
    verdict: str = "fail"
    said: list[str] = field(default_factory=list)
    end: str | None = None
    result_code: str | None = None
    audio_saved_bytes: int = 0
    files_written: int = 0
    detail: str = ""
    timeline: list[dict] = field(default_factory=list)


class Writes:
    """Открытия файлов на запись за время звонка: шлюз не должен сохранять звук."""

    def __init__(self):
        self.active = False
        self.paths: list[str] = []
        sys.addaudithook(self._hook)

    def _hook(self, event, args):
        if not self.active or event != "open":
            return
        path, mode, flags = args
        writing = any(c in mode for c in "wax+") if isinstance(mode, str) else False
        if writing or (isinstance(flags, int) and flags & WRITE_FLAGS):
            self.paths.append(str(path))


WRITES: Writes | None = None


class Line:
    """Канал шлюза с отметками времени: что и когда сказал ассистент."""

    def __init__(self, started: float):
        self.started = started
        self.events: list[dict] = []
        self.audio_bytes = 0

    async def send_json(self, message):
        self.events.append({**message, "t": round(time.monotonic() - self.started, 3)})

    async def send_bytes(self, data):
        self.audio_bytes += len(data)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


class Recorded:
    """Настоящий диалог; запоминаем время и итог каждой итоговой реплики."""

    def __init__(self, service, started: float):
        self.service = service
        self.started = started
        self.replies: list[dict] = []
        self.ended = None

    def __getattr__(self, name):
        return getattr(self.service, name)

    async def handle(self, call_id, turn):
        asked = time.monotonic()
        result = await self.service.handle(call_id, turn)
        if turn.is_final:
            self.replies.append(
                {
                    "turn_id": turn.turn_id,
                    "asked": round(asked - self.started, 3),
                    "answered": round(time.monotonic() - self.started, 3),
                    "code": result.code,
                    "phase": result.phase,
                }
            )
        return result

    async def end(self, call_id):
        self.ended = await self.service.end(call_id)
        return self.ended


def slow_mis(runtime, scale: float) -> None:
    """Задержка отказа МИС из YAML — настоящим ожиданием: эмулятор сдвигает только свои часы."""
    emulator = runtime.emulator
    handle = emulator.handle

    async def delayed(request):
        for fault in emulator.faults:
            if (
                fault.get("call") == "create_visit"
                and request.method == "POST"
                and request.url.path.rstrip("/").endswith("/visits")
                and fault.get("times", 1) > 0
            ):
                await asyncio.sleep(fault.get("delay_s", 0) * scale)
                break
        return await handle(request)

    emulator.handle = delayed


def speech(words: int, settings: VoiceSettings) -> bytes:
    """Кадры «речи» и тишина, после которой шлюз сочтёт фразу законченной, даже при диктовке."""
    pause = max(settings.end_silence_ms, settings.dictation_silence_ms) + 2 * FRAME_MS
    return SPEECH * words + QUIET * -(-pause // FRAME_MS)


def templates(step) -> list[str]:
    value = step.get("say", [])
    return [value] if isinstance(value, str) else list(value)


def heard(renderer: Renderer, today, line: Line, names) -> list[str]:
    """Какие шаблоны YAML прозвучали, по тексту, отрисованному тем же справочником фраз."""
    spoken = " ".join(e["text"] for e in line.of("speak")).casefold()
    found = []
    for name in names:
        try:
            text = renderer.render(name, today).text.casefold()
        except Exception:
            continue
        if text and text in spoken:
            found.append(name)
    return found


async def settle(call, recorded, line, replies: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Ответ получен и произнесён до конца: следующая фраза его не перебивает
        spoken = line.of("speak")
        voiced = bool(spoken) and (
            not recorded.replies or spoken[-1]["t"] >= recorded.replies[-1]["answered"]
        )
        done = len(recorded.replies) >= replies and voiced and not call.speaking()
        if done:
            await asyncio.sleep(PLAYBACK_MARGIN_S)
            done = not call.speaking() and len(line.of("speak")) == len(spoken)
        if call.finished.is_set() or done:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("шлюз не ответил вовремя")


async def run_dialog(scenario_id, dialog, settings: VoiceSettings, scale: float) -> Evidence:
    evidence = Evidence(scenario_id, dialog.label, REQUIREMENTS[scenario_id])
    runtime = ScenarioRuntime(dialog)
    slow_mis(runtime, scale)
    patient = [s for s in dialog.steps if "patient" in s]
    texts = {FIRST_WORDS + i: s["patient"] for i, s in enumerate(patient)}
    started = time.monotonic()
    line = Line(started)
    today = runtime.clock.now().date()
    async with runtime.application():
        recorded = Recorded(runtime.service, started)
        call = CallSession(
            "voice-evidence",
            line,
            recorded,
            Vad(),
            Recognizer(texts),
            Synthesizer(),
            today=lambda: today,
            settings=settings,
        )
        WRITES.paths.clear()
        WRITES.active = True
        try:
            await call.start()
            await settle(call, recorded, line, 0, REPLY_TIMEOUT_S)
            for number, step in enumerate(patient):
                await call.feed(speech(FIRST_WORDS + number, settings))
                await settle(call, recorded, line, number + 1, REPLY_TIMEOUT_S)
            if scenario_id == "R-17":
                await asyncio.wait_for(call.finished.wait(), REPLY_TIMEOUT_S)
            state = await runtime.dialogue_store.inspect(call.call_id)
        finally:
            WRITES.active = False
            await call.close()
    expected = [name for step in dialog.steps for name in templates(step)]
    evidence.said = heard(Renderer(), today, line, expected)
    ends = line.of("end")
    evidence.end = ends[0]["reason"] if ends else None
    evidence.result_code = RESULT_BY_END.get(evidence.end) or (
        recorded.replies[-1]["code"] if recorded.replies else None
    )
    evidence.files_written = len(WRITES.paths)
    evidence.timeline = [
        {"t": e["t"], "type": e["type"], **({"text": e["text"]} if "text" in e else {})}
        for e in line.events
        if e["type"] in {"speak", "end", "question"}
    ] + [{"t": r["answered"], "type": "dialogue", **r} for r in recorded.replies]
    evidence.timeline.sort(key=lambda e: e["t"])
    check = {"R-17": silence, "D-13": wait_prompt, "D-15": recording}[scenario_id]
    ok, evidence.detail = check(evidence, line, recorded, state, settings, Renderer(), today)
    evidence.verdict = "ok" if ok else "fail"
    return evidence


def spoken_at(line, renderer, today, name) -> list[float]:
    text = renderer.render(name, today).text.casefold()
    return [e["t"] for e in line.of("speak") if text in e["text"].casefold()]


def silence(evidence, line, recorded, state, settings, renderer, today):
    first = spoken_at(line, renderer, today, "silence.first")
    final = spoken_at(line, renderer, today, "silence.final")
    greeting = spoken_at(line, renderer, today, "greeting.inbound")
    if not (greeting and first and final):
        return False, "не прозвучали приветствие и обе фразы тишины"
    gaps = (first[0] - greeting[0], final[0] - first[0])
    order = greeting[0] < first[0] < final[0]
    # Отсчёт идёт от конца воспроизведения: пауза не короче настроенной
    paused = all(gap >= settings.silence_prompt_s * 0.9 for gap in gaps)
    ended = evidence.end == "silence" and recorded.ended is not None
    detail = (
        f"«Вы меня слышите?» через {gaps[0]:.1f} с, «Вас не слышно» ещё через {gaps[1]:.1f} с "
        f"(настройка {settings.silence_prompt_s:g} с), завершение: {evidence.end}"
    )
    return order and paused and ended, detail


def wait_prompt(evidence, line, recorded, state, settings, renderer, today):
    waits = spoken_at(line, renderer, today, "wait.schedule")
    slow = [r for r in recorded.replies if r["answered"] - r["asked"] > settings.wait_phrase_s]
    if not slow:
        return False, "ни один ход диалога не ждал МИС дольше порога фразы ожидания"
    turn = next(
        (
            candidate
            for candidate in reversed(slow)
            if any(candidate["asked"] < wait < candidate["answered"] for wait in waits)
        ),
        slow[-1],
    )
    during = [t for t in waits if turn["asked"] < t < turn["answered"]]
    detail = (
        f"ход {turn['turn_id']} ждал МИС {turn['answered'] - turn['asked']:.1f} с; "
        f"«{renderer.render('wait.schedule', today).text}» "
        + (f"на {during[0] - turn['asked']:.1f} с, до ответа" if during else "не прозвучала")
        + f"; итог диалога {turn['code']}"
    )
    return bool(during), detail


def recording(evidence, line, recorded, state, settings, renderer, today):
    off = "recording.off" in evidence.said
    codes = [r["code"] for r in recorded.replies]
    after = codes[codes.index("RECORDING_OPT_OUT") + 1 :] if "RECORDING_OPT_OUT" in codes else []
    # Обслуживание продолжается: следующая реплика пациента получила обычный ответ
    continued = evidence.end is None and (bool(after) or codes[-1:] == ["RECORDING_OPT_OUT"])
    detail = (
        f"отказ сохранён: {state.recording_opt_out}, «запись отключена» прозвучала: {off}, "
        f"разговор продолжился: {continued}, файлов открыто на запись: {evidence.files_written}"
    )
    ok = state.recording_opt_out and off and continued and evidence.files_written == 0
    return ok, detail


async def collect(settings: VoiceSettings, scale: float, only=None) -> list[Evidence]:
    global WRITES
    WRITES = WRITES or Writes()
    results = []
    for scenario in load_scenarios():
        if scenario.id not in REQUIREMENTS or (only and scenario.id not in only):
            continue
        for dialog in scenario.dialogs:
            results.append(await run_dialog(scenario.id, dialog, settings, scale))
    return results


def markdown(document) -> str:
    rows = "\n".join(
        f"| {r['label']} | `{r['requirement']}` | {r['verdict']} | {r['detail']} |"
        for r in document["results"]
    )
    source = document["source"]
    return f"""# Голосовые доказательства T07

Commit `{source.get("commit")}`, dirty `{source.get("dirty")}`. Настоящие CallSession,
DialogueService и эмулятор МИС прогона T07; подставные VAD, распознавание и синтез. SIP, Asterisk,
модели и живая МИС не участвуют.

| Диалог | Требование | Итог | Что наблюдалось |
|---|---|---|---|
{rows}
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scenario", action="append", choices=sorted(REQUIREMENTS))
    args = parser.parse_args(argv)
    settings = VoiceSettings(_env_file=None)
    results = asyncio.run(collect(settings, 1.0, args.scenario))
    document = {
        "schema_version": 1,
        "source": source_receipt(),
        "engines": {"vad": "fake", "asr": "fake", "tts": "fake", "sip": None},
        "settings": {
            "silence_prompt_s": settings.silence_prompt_s,
            "wait_phrase_s": settings.wait_phrase_s,
            "dialogue_timeout_s": settings.dialogue_timeout_s,
            "mis_delay_scale": 1.0,
        },
        "results": [asdict(r) for r in results],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "voice_evidence.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.output_dir / "voice_evidence.md").write_text(markdown(document), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(document))
    return 0 if all(r.verdict == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
