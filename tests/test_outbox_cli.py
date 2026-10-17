"""Служебные команды не открывают внешнюю доставку и читаются в русской Windows."""

import os
import subprocess
import sys
from argparse import Namespace

import pytest

from app.outbox import cli
from app.settings import Settings


def test_cli_help_is_utf8_even_without_python_io_override():
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    result = subprocess.run(
        [sys.executable, "-m", "app.outbox.cli", "--help"],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    text = " ".join(result.stdout.split())
    assert "Очередь уведомлений" in text and "SMS не отправляется" in text


async def test_demo_adapter_cannot_be_enabled_in_external_mis_mode(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "backend_engine", lambda settings: opened.append(True))
    with pytest.raises(ValueError, match="offline_test"):
        await cli.execute(
            Namespace(command="run-once", demo_journal=True, limit=2),
            Settings(_env_file=None, app_mode="hackathon", mis_token="test-only"),
        )
    assert opened == []
