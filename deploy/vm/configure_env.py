"""Создать начальный .env ВМ без печати секретов и перезаписи рабочего файла."""

import argparse
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

MIS_ORIGIN = "https://crm-med-mock.dbi-testing.ru"


def mis_url(value: str) -> str:
    """Платформа выдаёт origin, клиенту нужен /api/v1; другой хост не принимаем."""
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.netloc != "crm-med-mock.dbi-testing.ru"
        or parts.path not in ("", "/", "/api/v1", "/api/v1/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("неожиданный адрес DBI_API_URL/MIS_BASE_URL")
    return MIS_ORIGIN + "/api/v1"


def environment(mode: str, source: Mapping[str, str]) -> dict[str, str]:
    if mode not in ("offline_test", "hackathon"):
        raise ValueError("неподдерживаемый режим")
    values = {
        "APP_MODE": mode,
        "MIS_BASE_URL": MIS_ORIGIN + "/api/v1",
        "MIS_TOKEN": "",
        "MIS_RATE_LIMIT_PER_MIN": "8",
        "CLINIC_UTC_OFFSET": "+04:00",
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "clinic",
        "POSTGRES_USER": "clinic",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "DIALOGUE_HASH_KEY": secrets.token_urlsafe(48),
        "DIALOGUE_MODE": "natural",
        "DIALOGUE_STORAGE": "postgres",
        "BACKEND_DEMO_ENABLED": "true" if mode == "offline_test" else "false",
        "DIALOGUE_ENABLED": "true" if mode == "hackathon" else "false",
        "TELEPHONY_ENABLED": "false",
    }
    if mode == "hackathon":
        token = (source.get("MIS_TOKEN") or source.get("DBI_API_TOKEN") or "").strip()
        if not token or any(char in token for char in '\r\n\x00"\\'):
            raise ValueError("нет корректного DBI_API_TOKEN/MIS_TOKEN в окружении")
        values["MIS_TOKEN"] = token
        values["MIS_BASE_URL"] = mis_url(
            source.get("MIS_BASE_URL") or source.get("DBI_API_URL") or MIS_ORIGIN
        )
        values["SCHEDULE_INDEX_PATH"] = "data/schedule.sqlite"
    return values


def write_environment(path: Path, values: Mapping[str, str]) -> None:
    # O_EXCL отказывает также для символической ссылки; рабочие ключи не теряются.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("# Создано для ВМ. Секреты не публиковать; постоянные ключи сохранять.\n")
        for key, value in values.items():
            # Одинарные кавычки предотвращают интерполяцию $ со стороны Compose.
            value = value.replace("'", "\\'")
            output.write(f"{key}='{value}'\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline_test", "hackathon"), required=True)
    args = parser.parse_args(argv)
    if not Path("pyproject.toml").is_file() or not Path("app/settings.py").is_file():
        parser.exit(1, "Запустите скрипт из корня проекта.\n")
    try:
        values = environment(args.mode, os.environ)
        write_environment(Path(".env"), values)
    except FileExistsError:
        parser.exit(1, ".env уже существует: файл сохранён, изменения не выполнялись.\n")
    except (OSError, ValueError):
        # Исключение URL/файловой системы может содержать исходные данные.
        parser.exit(1, "Конфигурация не создана: проверьте права и переменные DBI/MIS.\n")
    print(f".env создан, режим {args.mode}. Значения секретов не выводятся.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
