"""Рецепт deploy/voice не расходится с кодом: модели, секреты, порты."""

from pathlib import Path

import yaml

from app.voice.settings import VoiceSettings

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "deploy" / "voice"


def test_model_checksums_cover_exactly_the_models_the_gateway_loads():
    settings = VoiceSettings(_env_file=None)
    listed = {
        line.split(maxsplit=1)[1]
        for line in (RECIPE / "models.sha256").read_text("utf-8").splitlines()
        if line.strip()
    }
    assert listed == {
        settings.asr_model,
        settings.asr_config,
        settings.vad_model,
        settings.tts_model,
    }


def test_voice_secrets_never_reach_the_image_or_git():
    assert "deploy/voice/voice.env" in (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert "deploy/voice/voice.env" in (ROOT / ".gitignore").read_text("utf-8").splitlines()
    example = (RECIPE / "voice.env.example").read_text("utf-8")
    assert "ARI_PASSWORD=\n" in example and "REMINDERS_ENABLED=false" in example


def test_compose_keeps_voice_local_and_on_the_backend_network():
    compose = yaml.safe_load((RECIPE / "compose.yaml").read_text("utf-8"))
    voice = compose["services"]["voice"]
    assert voice["ports"] == ["127.0.0.1:8001:8001"]
    assert (ROOT / voice["build"]["dockerfile"]).is_file()
    assert compose["networks"]["clinic"] == {"external": True, "name": "clinic-vm_default"}
    assert voice["environment"]["WEB_ACCESS_ENABLED"] == "true"
