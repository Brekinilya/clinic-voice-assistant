"""Прогон проверок одной командой (compose.test.yaml): те же шаги, что в CI, без .env и чужой базы."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return yaml.safe_load((ROOT / name).read_text("utf-8"))


def test_recipe_runs_the_same_checks_as_ci():
    script = load("compose.test.yaml")["services"]["tests"]["command"][-1]
    ci = load(".github/workflows/ci.yml")["jobs"]["quality"]["steps"]
    for step in ("ruff check .", "ruff format --check .", "pytest"):
        assert step in script and any(step in str(s.get("run", "")) for s in ci)
    assert "uv sync --locked --extra voice" in script


def test_recipe_is_isolated_from_the_stand_settings_and_local_database():
    services = load("compose.test.yaml")["services"]
    tests, postgres = services["tests"], services["postgres"]
    assert ".:/src:ro" in tests["volumes"]
    # Настройки стенда и модели в копию не попадают
    for excluded in ("./.env", "./.venv", "./models", "./data", "./deploy/voice/voice.env"):
        assert f"--exclude={excluded}" in tests["command"][-1]
    assert "@postgres:5432/" in tests["environment"]["TEST_DATABASE_URL"]
    assert "ports" not in postgres and postgres["tmpfs"]
