"""Настройки голосового шлюза: пути к моделям и пороги разговора (переменные VOICE_*)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VOICE_", env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    models_dir: Path = Path("models")
    vad_model: str = "silero/silero_vad.onnx"
    asr_model: str = "gigaam/v3_ctc_onnx/v3_ctc_int8.onnx"
    asr_config: str = "gigaam/v3_ctc_onnx/v3_ctc.yaml"
    tts_model: str = "silero/v5_5_ru.pt"
    tts_speaker: str = "xenia"
    tts_rate: int = 24000
    # Как на машине организаторов: два ядра на всё
    threads: int = Field(default=2, ge=1, le=16)

    speech_threshold: float = Field(default=0.5, gt=0, lt=1)
    silence_threshold: float = Field(default=0.35, gt=0, lt=1)
    # Столько речи нужно, чтобы считать фразу начатой и перебить ассистента (0,12–0,2 с по плану)
    min_speech_ms: int = Field(default=192, ge=64)
    end_silence_ms: int = Field(default=640, ge=256)
    # ФИО, дату рождения и номера диктуют с паузами
    dictation_silence_ms: int = Field(default=1200, ge=256)
    max_phrase_ms: int = Field(default=15000, ge=1000)
    # Промежуточное распознавание — чтобы красный флаг сработал, не дожидаясь конца фразы
    partial_ms: int = Field(default=1000, ge=256)

    # Раздел 3.2: 5 секунд тишины — «Вы меня слышите?», ещё 5 — завершение
    silence_prompt_s: float = 5.0
    # Раздел 3.6: после прощания звонок завершается через 3 секунды
    goodbye_hangup_s: float = 3.0
    # Раздел 3.7: ответ дольше 1,5 секунды — «Одну секунду, проверяю расписание»
    wait_phrase_s: float = 1.5
    # Событие начала речи не должно надолго останавливать приём звука.
    input_started_timeout_s: float = Field(default=0.3, gt=0, le=2)

    def path(self, relative: str) -> Path:
        return self.models_dir / relative
