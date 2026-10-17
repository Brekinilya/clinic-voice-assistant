"""Звук для замера: чтение и запись WAV, передискретизация, телефонный канал и шум."""

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

RATE = 16000


def read(path: Path, rate: int = RATE) -> np.ndarray:
    audio, source_rate = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return resample(audio, source_rate, rate)


def write(path: Path, audio: np.ndarray, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.clip(audio, -1, 1), rate, subtype="PCM_16")


def resample(audio: np.ndarray, source_rate: int, rate: int) -> np.ndarray:
    if source_rate == rate:
        return audio.astype(np.float32)
    divisor = np.gcd(source_rate, rate)
    return resample_poly(audio, rate // divisor, source_rate // divisor).astype(np.float32)


def phone(audio: np.ndarray, rate: int = RATE) -> np.ndarray:
    """Как в звонке через SIP: 8 кГц и кодек G.711 (мю-закон), потом обратно в 16 кГц."""
    narrow = resample(audio, rate, 8000)
    mu = 255.0
    encoded = np.sign(narrow) * np.log1p(mu * np.abs(np.clip(narrow, -1, 1))) / np.log1p(mu)
    quantized = np.round((encoded + 1) / 2 * 255) / 255 * 2 - 1
    decoded = np.sign(quantized) * ((1 + mu) ** np.abs(quantized) - 1) / mu
    return resample(decoded.astype(np.float32), 8000, rate)


def noisy(audio: np.ndarray, snr_db: float = 15.0, seed: int = 7) -> np.ndarray:
    """Белый шум с заданным отношением сигнал/шум: улица, кухня, телевизор за стеной."""
    rng = np.random.default_rng(seed)
    power = np.mean(audio**2) + 1e-12
    noise = rng.normal(0, np.sqrt(power / 10 ** (snr_db / 10)), size=audio.shape)
    return (audio + noise).astype(np.float32)


CONDITIONS = {
    "clean": lambda a: a,
    "phone": phone,
    "phone_noise": lambda a: phone(noisy(a)),
}
