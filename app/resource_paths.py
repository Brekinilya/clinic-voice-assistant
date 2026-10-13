"""Один выбор путей для словарей: checkout при разработке, ресурсы пакета после установки."""

from pathlib import Path


def config_directory() -> Path:
    packaged = Path(__file__).resolve().parent / "resources"
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent / "config"
