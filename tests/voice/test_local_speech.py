"""Распознавание и синтез только локально: облачный движок нельзя ни выбрать, ни подключить."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.voice.settings import VoiceSettings

APP = Path(__file__).resolve().parents[2] / "app"
# Облачные речевые сервисы и SDK, которые могли бы унести голос пациента из контура
CLOUD = (
    "google.cloud",
    "azure",
    "boto3",
    "botocore",
    "openai",
    "yandex",
    "speechkit",
    "deepgram",
    "assemblyai",
    "elevenlabs",
    "salute",
    "tinkoff",
    "vk_cloud",
)
NETWORK = {"httpx", "requests", "urllib", "aiohttp", "websockets", "socket", "http"}
# Сеть в голосе — только телефония: ARI и RTP своего Asterisk, без звука в чужие сервисы
NETWORKED_VOICE = {"telephony.py"}


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def test_speech_processing_is_local_by_default_and_nothing_else_is_accepted():
    assert VoiceSettings(_env_file=None).speech_processing == "local"
    for value in ("cloud", "yandex", "remote", ""):
        with pytest.raises(ValidationError):
            VoiceSettings(_env_file=None, speech_processing=value)


def test_speech_processing_from_the_environment_cannot_switch_to_a_cloud(monkeypatch):
    monkeypatch.setenv("VOICE_SPEECH_PROCESSING", "cloud")
    with pytest.raises(ValidationError):
        VoiceSettings(_env_file=None)


@pytest.mark.parametrize(
    "field", ["models_dir", "vad_model", "asr_model", "asr_config", "tts_model"]
)
def test_models_are_read_only_from_disk(field):
    with pytest.raises(ValidationError):
        VoiceSettings(_env_file=None, **{field: "https://models.example/asr.onnx"})


def test_no_cloud_speech_sdk_anywhere_in_the_application():
    found = {
        f"{path.relative_to(APP).as_posix()}: {name}"
        for path in APP.rglob("*.py")
        for name in imports(path)
        if any(name == cloud or name.startswith(cloud + ".") for cloud in CLOUD)
    }
    assert found == set()


def test_voice_network_code_is_only_the_telephony_leg():
    networked = {
        path.name
        for path in (APP / "voice").glob("*.py")
        if {name.split(".")[0] for name in imports(path)} & NETWORK
    }
    assert networked == NETWORKED_VOICE


def test_recognition_runs_on_the_cpu_provider_only():
    source = (APP / "voice" / "engines.py").read_text("utf-8")
    assert 'providers=["CPUExecutionProvider"]' in source
    assert "torch.hub" not in source
