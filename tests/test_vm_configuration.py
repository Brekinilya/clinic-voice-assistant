"""Начальная конфигурация не подменяет стенд и не теряет рабочие секреты."""

import os
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from app.settings import Settings
from deploy.vm.configure_env import environment, main, mis_url, write_environment


def test_public_vm_uses_platform_auth_without_app_password():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/vm/compose.yaml").read_text("utf-8")
    )
    assert compose["services"]["app"]["environment"]["WEB_ACCESS_ENABLED"] == "false"


@pytest.mark.parametrize("suffix", ["", "/", "/api/v1", "/api/v1/"])
def test_platform_url_becomes_api_root(suffix):
    assert mis_url("https://crm-med-mock.dbi-testing.ru" + suffix).endswith("/api/v1")


@pytest.mark.parametrize(
    "url",
    [
        "http://crm-med-mock.dbi-testing.ru",
        "https://other.test/api/v1",
        "https://crm-med-mock.dbi-testing.ru@other.test",
        "https://user:password@crm-med-mock.dbi-testing.ru",
        "https://crm-med-mock.dbi-testing.ru/api/v1?token=secret",
        "https://crm-med-mock.dbi-testing.ru/#secret",
        "https://crm-med-mock.dbi-testing.ru/api/v1/doctors",
    ],
)
def test_no_credentials_are_redirected_to_an_unexpected_endpoint(url):
    with pytest.raises(ValueError):
        mis_url(url)


@pytest.mark.parametrize("mode", ["offline_test", "hackathon"])
def test_created_dotenv_is_accepted_by_application(tmp_path, mode):
    values = environment(
        mode,
        {
            "DBI_API_TOKEN": "fictional-$token#'value",
            "DBI_API_URL": "https://crm-med-mock.dbi-testing.ru",
        },
    )
    path = tmp_path / ".env"
    write_environment(path, values)
    restored = dotenv_values(path, interpolate=False)
    assert restored == values
    # Явные аргументы исключают влияние личного окружения запускающего тест.
    settings = Settings(_env_file=None, **{key.lower(): value for key, value in restored.items()})
    assert settings.app_mode == mode
    assert settings.backend_demo_enabled == (mode == "offline_test")
    assert settings.dialogue_enabled == (mode == "hackathon")
    assert settings.dialogue_storage == ("postgres" if mode == "hackathon" else "memory")
    assert settings.web_access_enabled
    assert len(settings.web_access_password.get_secret_value()) >= 24
    assert len(settings.dialogue_hash_key.get_secret_value()) >= 32
    if mode == "hackathon":
        assert settings.mis_token.get_secret_value() == "fictional-$token#'value"
    else:
        assert settings.mis_token.get_secret_value() == ""


def test_repeat_does_not_rotate_password_or_dialogue_key(tmp_path):
    path = tmp_path / ".env"
    write_environment(path, environment("offline_test", {}))
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_environment(path, environment("offline_test", {}))
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="Unix-права проверяются на Linux ВМ")
def test_file_private_and_symlink_cannot_replace_another_file(tmp_path):
    target = tmp_path / "original"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_environment(link, environment("offline_test", {}))
    assert target.read_text() == "keep"
    private = tmp_path / "private.env"
    write_environment(private, environment("offline_test", {}))
    assert private.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("token", ["", " \n ", "one\ntwo", "one\rtwo", "one\x00two"])
def test_missing_or_multiline_token_fails_instead_of_falling_back(token):
    with pytest.raises(ValueError):
        environment("hackathon", {"DBI_API_TOKEN": token})


def test_command_does_not_print_or_write_invalid_secret(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "app").mkdir()
    (tmp_path / "app/settings.py").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIS_TOKEN", raising=False)
    monkeypatch.delenv("MIS_BASE_URL", raising=False)
    monkeypatch.setenv("DBI_API_TOKEN", "fictional-private-token")
    monkeypatch.setenv("DBI_API_URL", "https://wrong.test/?private-token")
    with pytest.raises(SystemExit):
        main(["--mode", "hackathon"])
    output = capsys.readouterr()
    assert "private-token" not in output.out + output.err
    assert not (tmp_path / ".env").exists()


def test_config_command_does_not_leak_secret_and_refuses_second_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "app").mkdir()
    (tmp_path / "app/settings.py").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIS_TOKEN", raising=False)
    monkeypatch.delenv("MIS_BASE_URL", raising=False)
    monkeypatch.delenv("DBI_API_URL", raising=False)
    monkeypatch.setenv("DBI_API_TOKEN", "fictional-private-token")
    assert main(["--mode", "hackathon"]) == 0
    original = (tmp_path / ".env").read_bytes()
    with pytest.raises(SystemExit):
        main(["--mode", "hackathon"])
    assert (tmp_path / ".env").read_bytes() == original
    output = capsys.readouterr()
    assert "fictional-private-token" not in output.out + output.err
