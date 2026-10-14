"""Движки шлюза. Без группы зависимостей voice тесты пропускаются, без файлов моделей — их часть."""

import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")

from app.voice.engines import (  # noqa: E402
    GigaAmRecognizer,
    SileroVad,
    float_to_pcm,
    log_mel,
    mel_filterbank,
    pcm_to_float,
)
from app.voice.settings import VoiceSettings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = VoiceSettings(_env_file=None, models_dir=ROOT / "models")
PATIENT = ROOT / "data" / "bench" / "patient" / "silero_v5_5_ru_aidar" / "p11.wav"


def models(*names: str):
    missing = [n for n in names if not SETTINGS.path(n).exists()]
    return pytest.mark.skipif(bool(missing), reason=f"нет моделей: {missing}")


def test_pcm_round_trip():
    audio = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    assert np.allclose(pcm_to_float(float_to_pcm(audio)), audio, atol=1e-4)


def test_mel_filterbank_covers_spectrum():
    bank = mel_filterbank()
    assert bank.shape == (161, 64)
    assert (bank.max(axis=0) > 0).all()


def test_log_mel_shape_matches_gigaam():
    # GigaAM без центрирования: (длина − 320) // 160 + 1 кадров
    features = log_mel(np.zeros(16000, dtype=np.float32))
    assert features.shape == (64, 99)
    assert np.allclose(features, np.log(1e-9))


@models(SETTINGS.vad_model)
def test_vad_hears_silence_as_silence():
    stream = SileroVad(SETTINGS.path(SETTINGS.vad_model)).stream()
    assert max(stream.probability(bytes(1024)) for _ in range(10)) < 0.2


@models(SETTINGS.asr_model, SETTINGS.asr_config)
@pytest.mark.skipif(not PATIENT.exists(), reason="нет записей замера: bench/speech/bench.py")
def test_recognizer_on_bench_phrase():
    recognizer = GigaAmRecognizer(
        SETTINGS.path(SETTINGS.asr_model), SETTINGS.path(SETTINGS.asr_config)
    )
    with wave.open(str(PATIENT)) as w:
        pcm = w.readframes(w.getnframes())
    assert recognizer.transcribe(pcm) == "мне бы записаться к терапевту"
    assert recognizer.transcribe(bytes(1000)) == ""
