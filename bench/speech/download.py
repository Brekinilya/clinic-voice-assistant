"""Скачивает модели для замера в models/ (папка не попадает в git).

python bench/speech/download.py            — всё из списка
python bench/speech/download.py vosk piper — только выбранные группы
"""

import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"

GIGAAM = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM"
PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"

FILES = {
    "gigaam": [
        (f"{GIGAAM}/v3_ctc.ckpt", "gigaam/v3_ctc.ckpt"),
        (f"{GIGAAM}/v3_e2e_ctc.ckpt", "gigaam/v3_e2e_ctc.ckpt"),
        (f"{GIGAAM}/v3_e2e_ctc_tokenizer.model", "gigaam/v3_e2e_ctc_tokenizer.model"),
    ],
    "vosk": [
        ("https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip", "vosk/vosk-model-small-ru-0.22.zip"),
    ],
    "silero": [
        ("https://models.silero.ai/models/tts/ru/v5_5_ru.pt", "silero/v5_5_ru.pt"),
        ("https://models.silero.ai/models/tts/ru/v5_cis_base.pt", "silero/v5_cis_base.pt"),
    ],
    "piper": [
        (f"{PIPER}/irina/medium/ru_RU-irina-medium.onnx", "piper/ru_RU-irina-medium.onnx"),
        (f"{PIPER}/irina/medium/ru_RU-irina-medium.onnx.json", "piper/ru_RU-irina-medium.onnx.json"),
        (f"{PIPER}/dmitri/medium/ru_RU-dmitri-medium.onnx", "piper/ru_RU-dmitri-medium.onnx"),
        (f"{PIPER}/dmitri/medium/ru_RU-dmitri-medium.onnx.json", "piper/ru_RU-dmitri-medium.onnx.json"),
    ],
}


def fetch(url: str, target: Path) -> None:
    if target.exists():
        print(f"есть: {target.relative_to(ROOT)}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"качаю {url}")
    urllib.request.urlretrieve(url, partial)
    partial.rename(target)
    if target.suffix == ".zip":
        with zipfile.ZipFile(target) as archive:
            archive.extractall(target.parent)


def main(groups: list[str]) -> None:
    for group in groups or list(FILES):
        for url, relative in FILES[group]:
            fetch(url, MODELS / relative)


if __name__ == "__main__":
    main(sys.argv[1:])
