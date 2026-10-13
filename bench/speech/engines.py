"""Обвязка движков распознавания и синтеза с единым интерфейсом.

Распознавание: transcribe(audio 16 кГц float32) -> текст.
Синтез: synthesize(text) -> (audio float32, частота).
Все движки работают в 2 потока — как на машине организаторов.
"""

import io
import json
import time
import wave
from pathlib import Path

import numpy as np

from audio import RATE, resample

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"
THREADS = 2


def _onnx_options():
    import onnxruntime as rt

    options = rt.SessionOptions()
    options.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    return options


def _load_gigaam(name: str):
    import gigaam
    import gigaam.model
    import torch

    torch.set_num_threads(THREADS)
    # GigaAM читает файлы через ffmpeg; мы отдаём ему уже готовый массив 16 кГц
    gigaam.model.load_audio = lambda wav, sample_rate=RATE: torch.from_numpy(np.asarray(wav, dtype=np.float32))
    return gigaam.load_model(name, device="cpu", fp16_encoder=False, download_root=str(MODELS / "gigaam"))


class GigaAM:
    def __init__(self, name: str):
        self.model = _load_gigaam(name)

    def transcribe(self, audio: np.ndarray) -> str:
        return self.model.transcribe(audio).text


class GigaAMOnnx:
    """Как модель будет работать на машине: onnxruntime, без torch в цикле распознавания."""

    def __init__(self, name: str, int8: bool = False):
        import hydra
        import omegaconf
        import onnxruntime as rt

        onnx_dir = MODELS / "gigaam" / f"{name}_onnx"
        model_path = onnx_dir / f"{name}.onnx"
        if not model_path.exists():
            model = _load_gigaam(name)
            model.to_onnx(dir_path=str(onnx_dir))
            del model
        if int8:
            # веса в 8 бит: модель в 4 раза меньше — важно при 4 ГБ на всю машину
            quantized = onnx_dir / f"{name}_int8.onnx"
            if not quantized.exists():
                from onnxruntime.quantization import QuantType, quantize_dynamic

                quantize_dynamic(str(model_path), str(quantized), weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul", "Gemm"])
            model_path = quantized
        session = rt.InferenceSession(str(model_path), sess_options=_onnx_options(), providers=["CPUExecutionProvider"])
        self.sessions = [session]
        self.cfg = omegaconf.OmegaConf.load(onnx_dir / f"{name}.yaml")
        self.preprocessor = hydra.utils.instantiate(self.cfg.preprocessor)
        self.tokenizer = hydra.utils.instantiate(self.cfg.decoding).tokenizer

    def transcribe(self, audio: np.ndarray) -> str:
        from gigaam.onnx_utils import infer_onnx

        result = infer_onnx(
            [audio], self.cfg, self.sessions,
            preprocessor=self.preprocessor, tokenizer=self.tokenizer, batch_size=1, progress=False,
        )
        return result[0]


class Vosk:
    def __init__(self, name: str):
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        self.model = Model(str(MODELS / "vosk" / name))

    def transcribe(self, audio: np.ndarray) -> str:
        """Потоком, кусками по 0,2 с, как в звонке; last_final_s — сколько ждать после конца фразы."""
        from vosk import KaldiRecognizer

        recognizer = KaldiRecognizer(self.model, RATE)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        chunk = int(RATE * 0.2) * 2
        for start in range(0, len(pcm), chunk):
            recognizer.AcceptWaveform(pcm[start:start + chunk])
        started = time.perf_counter()
        text = json.loads(recognizer.FinalResult()).get("text", "")
        self.last_final_s = time.perf_counter() - started
        return text


class Silero:
    """Модели Silero: v5_5_ru сама ставит ударения; v5_cis_base ждёт «+» перед ударной гласной."""

    def __init__(self, name: str, speaker: str, accentor: bool = False):
        import torch

        torch.set_num_threads(THREADS)
        importer = torch.package.PackageImporter(str(MODELS / "silero" / f"{name}.pt"))
        self.model = importer.load_pickle("tts_models", "model")
        self.model.to("cpu")
        self.speaker = speaker
        self.rate = 24000
        self.accentor = None
        if accentor:
            from silero_stress import load_accentor

            self.accentor = load_accentor()
        self.auto_stress = name.endswith("_ru")

    def prepare(self, text: str) -> str:
        return self.accentor(text) if self.accentor else text

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        kwargs = {"put_accent": True, "put_yo": True} if self.auto_stress else {}
        audio = self.model.apply_tts(text=self.prepare(text), speaker=self.speaker, sample_rate=self.rate, **kwargs)
        return audio.numpy().astype(np.float32), self.rate


class Piper:
    def __init__(self, voice: str):
        from piper import PiperVoice

        import onnxruntime as rt

        path = str(MODELS / "piper" / f"{voice}.onnx")
        self.voice = PiperVoice.load(path)
        self.voice.session = rt.InferenceSession(path, sess_options=_onnx_options(), providers=["CPUExecutionProvider"])

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            self.voice.synthesize_wav(text, wav)
        buffer.seek(0)
        with wave.open(buffer, "rb") as wav:
            rate = wav.getframerate()
            frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return (frames.astype(np.float32) / 32768), rate


ASR = {
    "gigaam_v3_ctc": lambda: GigaAM("v3_ctc"),
    "gigaam_v3_ctc_onnx": lambda: GigaAMOnnx("v3_ctc"),
    "gigaam_v3_ctc_onnx_int8": lambda: GigaAMOnnx("v3_ctc", int8=True),
    "gigaam_v3_e2e_ctc": lambda: GigaAM("v3_e2e_ctc"),
    "vosk_small_ru": lambda: Vosk("vosk-model-small-ru-0.22"),
}

TTS = {
    "silero_v5_5_ru_xenia": lambda: Silero("v5_5_ru", "xenia"),
    "silero_v5_5_ru_aidar": lambda: Silero("v5_5_ru", "aidar"),
    "silero_cis_base_stress": lambda: Silero("v5_cis_base", "ru_ekaterina", accentor=True),
    "piper_irina": lambda: Piper("ru_RU-irina-medium"),
    "piper_dmitri": lambda: Piper("ru_RU-dmitri-medium"),
}

# Голоса, которыми озвучиваем реплики пациента для проверки распознавания
PATIENT_VOICES = ["silero_v5_5_ru_xenia", "silero_v5_5_ru_aidar", "piper_irina", "piper_dmitri"]


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    return resample(audio, rate, RATE)
