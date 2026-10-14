"""Модели голосового шлюза: Silero VAD и GigaAM на onnxruntime, синтез Silero на torch.

Модели лежат в models/ (в git не попадают), скачивает их bench/speech/download.py.
GigaAM int8 в ONNX получается из чекпойнта так же, как в замере (bench/speech/engines.py).
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from app.voice.endpoint import FRAME_SAMPLES, SAMPLE_RATE

# Признаки GigaAM v3: лог-мел 64 полосы, окно 20 мс, шаг 10 мс, без центрирования
N_FFT = 320
HOP = 160
N_MELS = 64


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _onnx_session(path: Path, threads: int):
    import onnxruntime as rt

    options = rt.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
    options.log_severity_level = 3
    return rt.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


class SileroVad:
    """Вероятность речи в кадре 512 отсчётов. Состояние модели своё у каждого звонка."""

    def __init__(self, path: Path):
        self.session = _onnx_session(path, 1)

    def stream(self) -> "VadStream":
        return VadStream(self.session)


class VadStream:
    CONTEXT = 64

    def __init__(self, session):
        self.session = session
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self.rate = np.array(SAMPLE_RATE, dtype=np.int64)

    def probability(self, frame: bytes) -> float:
        audio = pcm_to_float(frame)[None, :FRAME_SAMPLES]
        x = np.concatenate([self.context, audio], axis=1)
        out, self.state = self.session.run(None, {"input": x, "state": self.state, "sr": self.rate})
        self.context = x[:, -self.CONTEXT :]
        return float(out[0, 0])


def mel_filterbank(
    n_freqs: int = N_FFT // 2 + 1, n_mels: int = N_MELS, rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Треугольные фильтры в шкале HTK, как torchaudio.functional.melscale_fbanks без нормировки."""

    def to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    freqs = np.linspace(0, rate // 2, n_freqs)
    points = to_hz(np.linspace(to_mel(0.0), to_mel(rate / 2), n_mels + 2))
    widths = np.diff(points)
    slopes = points[None, :] - freqs[:, None]
    down = -slopes[:, :-2] / widths[:-1]
    up = slopes[:, 2:] / widths[1:]
    return np.maximum(0.0, np.minimum(down, up))


_FILTERBANK = mel_filterbank()
_WINDOW = np.hanning(N_FFT + 1)[:-1]  # периодическое окно Ханна, как torch.hann_window


def log_mel(audio: np.ndarray) -> np.ndarray:
    """Признаки (64, кадры) для GigaAM: то же, что gigaam.preprocess.FeatureExtractor."""
    if len(audio) < N_FFT:
        audio = np.pad(audio, (0, N_FFT - len(audio)))
    count = (len(audio) - N_FFT) // HOP + 1
    index = np.arange(N_FFT)[None, :] + HOP * np.arange(count)[:, None]
    frames = audio.astype(np.float64)[index] * _WINDOW
    power = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2
    mel = power @ _FILTERBANK
    return np.log(np.clip(mel, 1e-9, 1e9)).T.astype(np.float32)


class GigaAmRecognizer:
    """GigaAM v3 CTC в ONNX: признаки на numpy, жадное декодирование CTC."""

    def __init__(self, model: Path, config: Path, threads: int = 2):
        with open(config, encoding="utf-8") as f:
            vocabulary = yaml.safe_load(f)["decoding"]["vocabulary"]
        self.vocabulary = list(vocabulary)
        self.blank = len(self.vocabulary)
        self.session = _onnx_session(model, threads)

    def transcribe(self, pcm: bytes) -> str:
        audio = pcm_to_float(pcm)
        if len(audio) < SAMPLE_RATE // 10:
            return ""
        features = log_mel(audio)[None]
        lengths = np.array([features.shape[-1]], dtype=np.int64)
        log_probs, encoded = self.session.run(
            None, {"features": features, "feature_lengths": lengths}
        )
        labels = log_probs[0, : int(encoded[0])].argmax(-1)
        text, previous = [], None
        for label in labels:
            if label != previous and label != self.blank:
                text.append(self.vocabulary[label])
            previous = label
        return " ".join("".join(text).split())


class SileroSynthesizer:
    """Silero v5_5_ru: разметка из speech.render, кэш частых реплик, один синтез за раз."""

    def __init__(
        self,
        model: Path,
        speaker: str,
        rate: int = 24000,
        intensity: int = 3,
        threads: int = 2,
        cache_size: int = 256,
    ):
        import torch

        torch.set_num_threads(threads)
        importer = torch.package.PackageImporter(str(model))
        self.model = importer.load_pickle("tts_models", "model")
        self.model.to("cpu")
        self.speaker = speaker
        self.rate = rate
        self.intensity = intensity
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def synthesize(self, speech: str) -> tuple[bytes, int]:
        with self._lock:
            if speech in self._cache:
                self._cache.move_to_end(speech)
                return self._cache[speech], self.rate
            audio = self.model.apply_tts(
                text=speech,
                speaker=self.speaker,
                sample_rate=self.rate,
                put_accent=True,
                put_yo=True,
                intensity=self.intensity,
            )
            pcm = float_to_pcm(audio.numpy())
            self._cache[speech] = pcm
            if len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return pcm, self.rate


@dataclass(frozen=True)
class Engines:
    vad: SileroVad
    recognizer: GigaAmRecognizer
    synthesizer: SileroSynthesizer
