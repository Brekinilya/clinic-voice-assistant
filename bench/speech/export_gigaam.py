"""Модель распознавания для голосового шлюза: GigaAM v3 CTC → ONNX → веса в 8 бит.

Запускать в окружении замера (torch, gigaam, onnxruntime), не в окружении приложения:

    python bench/speech/download.py gigaam vad silero
    python bench/speech/export_gigaam.py

Шлюзу нужны models/gigaam/v3_ctc_onnx/v3_ctc_int8.onnx и v3_ctc.yaml (словарь CTC).
Скрипт печатает версии пакетов и SHA-256 файлов — по ним сверяем модели на разных машинах.
"""

import hashlib
from importlib.metadata import version

from engines import MODELS, GigaAMOnnx

ONNX_DIR = MODELS / "gigaam" / "v3_ctc_onnx"


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    # Экспорт и квантизация — в конструкторе, если файлов ещё нет
    GigaAMOnnx("v3_ctc", int8=True)
    for package in ("gigaam", "torch", "onnxruntime"):
        print(f"{package} {version(package)}")
    for name in ("v3_ctc_int8.onnx", "v3_ctc.yaml"):
        path = ONNX_DIR / name
        print(
            f"{path.relative_to(MODELS.parent)}  {path.stat().st_size / 2**20:.0f} МБ  {sha256(path)}"
        )


if __name__ == "__main__":
    main()
