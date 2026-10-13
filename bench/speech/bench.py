"""Замер распознавания и синтеза речи.

python bench/speech/bench.py run      — всё по порядку, каждый движок в своём процессе
python bench/speech/bench.py report   — пересобрать RESULTS.md из сохранённых замеров

Звук и сырые результаты — в data/bench/ (не попадают в git), сводка — в bench/speech/RESULTS.md.
Свои записи реплик пациента можно положить в data/bench/real/<id>.wav (id из phrases.yaml) —
они пойдут в замер распознавания отдельным условием.
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import jiwer
import psutil
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from audio import CONDITIONS, read, write  # noqa: E402
from engines import ASR, PATIENT_VOICES, TTS, to_16k  # noqa: E402

OUT = ROOT / "data" / "bench"
RESULTS = OUT / "results"
PHRASES = yaml.safe_load((HERE / "phrases.yaml").read_text(encoding="utf-8"))


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 2**20


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е").replace("-", " ")
    return " ".join(re.findall(r"[а-яa-z0-9]+", text))


# Синтез

def run_tts(name: str) -> dict:
    before = rss_mb()
    started = time.perf_counter()
    engine = TTS[name]()
    load_s = time.perf_counter() - started
    memory = rss_mb() - before

    engine.synthesize("Прогрев.")
    timings = []
    for item in PHRASES["assistant"]:
        started = time.perf_counter()
        audio, rate = engine.synthesize(item["text"])
        spent = time.perf_counter() - started
        timings.append({"id": item["id"], "seconds": spent, "audio_s": len(audio) / rate})
        write(OUT / "tts" / name / f"{item['id']}.wav", to_16k(audio, rate))

    if name in PATIENT_VOICES:
        for item in PHRASES["patient"]:
            audio, rate = engine.synthesize(item["text"])
            write(OUT / "patient" / name / f"{item['id']}.wav", to_16k(audio, rate))

    return {"kind": "tts", "name": name, "load_s": load_s, "rss_mb": memory, "phrases": timings}


# Распознавание

def patient_sets() -> dict[str, Path]:
    sets = {voice: OUT / "patient" / voice for voice in PATIENT_VOICES}
    if (OUT / "real").exists():
        sets["real"] = OUT / "real"
    return {k: v for k, v in sets.items() if v.exists()}


def run_asr(name: str) -> dict:
    before = rss_mb()
    started = time.perf_counter()
    engine = ASR[name]()
    load_s = time.perf_counter() - started
    memory = rss_mb() - before

    rows = []
    for voice, folder in patient_sets().items():
        for item in PHRASES["patient"]:
            path = folder / f"{item['id']}.wav"
            if not path.exists():
                continue
            clean = read(path)
            for condition, transform in CONDITIONS.items():
                audio = transform(clean)
                started = time.perf_counter()
                text = engine.transcribe(audio)
                spent = time.perf_counter() - started
                rows.append({
                    "voice": voice, "condition": condition, "id": item["id"], "text": text,
                    "seconds": spent, "audio_s": len(audio) / 16000,
                    # у потоковых движков ждать после конца фразы нужно только финальный шаг
                    "final_s": getattr(engine, "last_final_s", spent),
                })

    # разборчивость синтеза: распознаём реплики ассистента каждым синтезатором
    roundtrip = []
    if name == "gigaam_v3_ctc":
        for tts_name in TTS:
            for item in PHRASES["assistant"]:
                path = OUT / "tts" / tts_name / f"{item['id']}.wav"
                if path.exists():
                    roundtrip.append({"tts": tts_name, "id": item["id"], "text": engine.transcribe(read(path))})

    return {"kind": "asr", "name": name, "load_s": load_s, "rss_mb": memory, "rows": rows, "roundtrip": roundtrip}


# Оценка

def check_item(item: dict, text: str) -> dict:
    """Что из ожидаемого получилось извлечь из распознанного текста."""
    from app.nlu.parsers import parse_digits, parse_expected
    from app.policies.safety import check

    result = {}
    today = date.fromisoformat(PHRASES["reference_date"])
    if "slots" in item or "intent" in item:
        parsed = parse_expected(text, item["expect"], today, doctors=["Кузнецова"])
        same = lambda a, b: normalize(str(a)) == normalize(str(b))  # noqa: E731
        if "slots" in item:
            result["slots_ok"] = all(same(parsed.slots.get(k), v) for k, v in item["slots"].items())
        if "intent" in item:
            result["intent_ok"] = parsed.intent == item["intent"]
    flags = check(text).flags
    result["flags_ok"] = set(item.get("flags", [])) <= flags
    result["false_alarm"] = bool({"red_a", "red_b"} & flags) and not ({"red_a", "red_b"} & set(item.get("flags", [])))
    if "digits" in item:
        result["digits_ok"] = parse_digits(text) == item["digits"]
    return result


def summarize_asr(data: dict) -> list[dict]:
    items = {i["id"]: i for i in PHRASES["patient"]}
    groups: dict[tuple, list] = {}
    for row in data["rows"]:
        groups.setdefault((row["voice"] == "real", row["condition"]), []).append(row)
    summary = []
    for (real, condition), rows in sorted(groups.items()):
        refs = [normalize(items[r["id"]]["text"]) for r in rows]
        hyps = [normalize(r["text"]) for r in rows]
        checks = [check_item(items[r["id"]], r["text"]) for r in rows]
        share = lambda key: (  # noqa: E731
            sum(c[key] for c in checks if key in c) / max(1, sum(key in c for c in checks))
        )
        latency = [r.get("final_s", r["seconds"]) for r in rows]
        summary.append({
            "engine": data["name"], "audio": "свои записи" if real else "синтез", "condition": condition,
            "n": len(rows),
            "wer": jiwer.wer(refs, hyps), "cer": jiwer.cer(refs, hyps),
            "slots": share("slots_ok"), "intent": share("intent_ok"), "flags": share("flags_ok"),
            "false_alarm": share("false_alarm"), "digits": share("digits_ok"),
            "latency_p50": statistics.median(latency),
            "latency_p95": sorted(latency)[int(len(latency) * 0.95) - 1],
            "rtf": sum(r["seconds"] for r in rows) / sum(r["audio_s"] for r in rows),
        })
    return summary


def report() -> str:
    asr = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RESULTS.glob("asr_*.json"))]
    tts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RESULTS.glob("tts_*.json"))]
    texts = {i["id"]: i["text"] for i in PHRASES["assistant"]}
    lines = ["# Замер распознавания и синтеза речи", "", "Собрано `python bench/speech/bench.py run`. Все движки работают в 2 потока на процессоре, как на машине организаторов (2 ядра, 4 ГБ).", ""]

    lines += ["## Распознавание", "",
              "| Движок | Звук | Условие | Фраз | WER | CER | Параметры | Ответ да/нет | Флаги | Ложная тревога | Полис | Ждать после конца фразы p50 / p95, с | RTF |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for data in asr:
        for s in summarize_asr(data):
            lines.append(
                f"| {s['engine']} | {s['audio']} | {s['condition']} | {s['n']} | {s['wer']:.1%} | {s['cer']:.1%} | "
                f"{s['slots']:.0%} | {s['intent']:.0%} | {s['flags']:.0%} | {s['false_alarm']:.0%} | {s['digits']:.0%} | "
                f"{s['latency_p50']:.2f} / {s['latency_p95']:.2f} | {s['rtf']:.2f} |"
            )
    lines += ["", "| Движок | Загрузка, с | Память, МБ |", "|---|---|---|"]
    lines += [f"| {d['name']} | {d['load_s']:.1f} | {d['rss_mb']:.0f} |" for d in asr]

    lines += ["", "## Синтез", "",
              "| Голос | Загрузка, с | Память, МБ | Реплика p50 / max, с | RTF | Разборчивость (CER распознавания) |",
              "|---|---|---|---|---|---|"]
    roundtrip = next((d["roundtrip"] for d in asr if d["roundtrip"]), [])
    for data in tts:
        spent = [p["seconds"] for p in data["phrases"]]
        rtf = sum(spent) / sum(p["audio_s"] for p in data["phrases"])
        mine = [r for r in roundtrip if r["tts"] == data["name"]]
        cer = jiwer.cer([normalize(texts[r["id"]]) for r in mine], [normalize(r["text"]) for r in mine]) if mine else float("nan")
        lines.append(
            f"| {data['name']} | {data['load_s']:.1f} | {data['rss_mb']:.0f} | "
            f"{statistics.median(spent):.2f} / {max(spent):.2f} | {rtf:.2f} | {cer:.1%} |"
        )

    from errors import markdown

    lines += ["", "## Где ошибается распознавание", "",
              "Фразы, по которым разбор дал не тот результат, что по эталонному тексту (телефонный канал)."]
    for engine in ("gigaam_v3_ctc_onnx_int8", "vosk_small_ru"):
        if (RESULTS / f"asr_{engine}.json").exists():
            lines += ["", markdown(engine, "phone")]
    return "\n".join(lines) + "\n"


def run_all() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    jobs = [("tts", n) for n in TTS] + [("asr", n) for n in ASR]
    for kind, name in jobs:
        print(f"{kind} {name}...", flush=True)
        done = subprocess.run([sys.executable, __file__, kind, name], cwd=ROOT)
        if done.returncode != 0:
            print(f"  не получилось: {kind} {name}", flush=True)
    (HERE / "RESULTS.md").write_bytes(report().encode("utf-8"))


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    command = sys.argv[1]
    if command == "run":
        run_all()
    elif command == "report":
        (HERE / "RESULTS.md").write_bytes(report().encode("utf-8"))
    elif command in {"tts", "asr"}:
        name = sys.argv[2]
        data = run_tts(name) if command == "tts" else run_asr(name)
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / f"{command}_{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
