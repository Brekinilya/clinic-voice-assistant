"""Проверка настоящих моделей без МИС, микрофона и создания записей."""

import argparse
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version

from app.voice.failures import failure
from app.voice.settings import VoiceSettings


def check(settings: VoiceSettings, *, warm_up=False):
    packages = {}
    for name in ("numpy", "onnxruntime", "torch", "websockets"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    files = []
    for relative in (
        settings.vad_model,
        settings.asr_model,
        settings.asr_config,
        settings.tts_model,
    ):
        path = settings.path(relative)
        item = {"file": relative, "present": False}
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            item.update(present=True, bytes=path.stat().st_size, sha256=digest.hexdigest())
        except OSError:
            pass
        files.append(item)
    result = {
        "packages": packages,
        "models": files,
        "inputs_present": False,
        "inference_checked": False,
    }
    result["inputs_present"] = all(packages.values()) and all(
        f["present"] and f["bytes"] for f in files
    )
    if warm_up and result["inputs_present"]:
        try:
            from app.voice.server import load_engines

            load_engines(settings)
            result["inference_checked"] = True
        except Exception as error:
            # Граница проверки: любая ошибка загрузки — ответ «модели не готовы», а не падение
            failure("voice.preflight_failed", error, trace=True)
            result.update(inference_error=type(error).__name__)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверка файлов и загрузки голосовых моделей")
    parser.add_argument(
        "--warm-up", action="store_true", help="загрузить модели и выполнить прогрев"
    )
    args = parser.parse_args(argv)
    result = check(VoiceSettings(), warm_up=args.warm_up)
    print(json.dumps(result, ensure_ascii=False))
    return (
        0 if result["inputs_present"] and (not args.warm_up or result["inference_checked"]) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
