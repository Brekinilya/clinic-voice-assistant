"""Версионированные факты и локальный подбор ответа по существующему словарному движку."""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.nlu.intents import classify
from app.nlu.lexicon import Entry, Lexicon, Pattern, tokenize
from app.resource_paths import config_directory

ACTION_REQUEST = Pattern.parse(
    "записать|записаться|отменить|отменять|перенести|переносить|перезаписать|перезаписаться|вызвать|талон|талончик|запись"
)


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: tuple[str, ...] = Field(min_length=1)
    utterances: tuple[str, ...] = Field(min_length=1)
    patterns: tuple[str, ...] = ()

    @field_validator("utterances", "source")
    @classmethod
    def nonempty_short_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value.split()) > 20 for value in values):
            raise ValueError("нужен непустой текст не длиннее двадцати слов")
        return values


class InformationCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document: str = Field(min_length=1)
    version: str = Field(min_length=1)
    facts: dict[str, Fact]
    laboratory: dict[str, Fact]

    @classmethod
    def load(cls, path: Path) -> "InformationCatalog":
        result = cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        # Проверяем шаблоны при загрузке, до первого звонка.
        result.lexicon(result.facts)
        result.lexicon(result.laboratory)
        return result

    @staticmethod
    def lexicon(facts: dict[str, Fact]) -> Lexicon:
        return Lexicon(
            [
                Entry.from_dict(key, {"patterns": fact.patterns})
                for key, fact in facts.items()
                if fact.patterns
            ],
            window=24,
        )


class InformationService:
    def __init__(self, catalog: InformationCatalog):
        self.catalog = catalog
        self._topics = catalog.lexicon(catalog.facts)
        self._laboratory = catalog.lexicon(catalog.laboratory)

    def find(self, text: str, *, clarify_lab: bool = False) -> str | None:
        if ACTION_REQUEST.find(tokenize(text), 24):
            # Слово «час» в просьбе записать не превращает действие в справку о режиме.
            return None
        intent = classify(text, by_place=False)
        if intent is not None and intent[0] in {"I-08", "I-09"}:
            # «Вы работаете по ДМС?» не спрашивает часы работы поликлиники.
            return None
        hit = self._topics.best(text)
        if hit is not None:
            if hit.key == "lab_terms":
                return self._lab_topic(text)
            return hit.key
        if clarify_lab:
            lab_topic = self._lab_topic(text, unknown="lab_unknown")
            if lab_topic != "lab_unknown":
                return lab_topic
            # Явная новая цель выходит из уточнения; её обработает обычный диалог.
            intent = classify(text)
            if intent is not None and intent[0] != "I-07":
                return None
            return self._lab_topic(text, unknown="lab_unknown")
        return None

    def _lab_topic(self, text: str, *, unknown: str = "lab_terms") -> str:
        hits = self._laboratory.find(text)
        # «Кровь и моча» не превращаются в один произвольно выбранный анализ.
        if len(hits) == 1:
            return "lab:" + hits[0].key
        return "lab_terms" if hits else unknown

    def fact(self, topic: str) -> Fact:
        if topic.startswith("lab:"):
            return self.catalog.laboratory[topic.removeprefix("lab:")]
        return self.catalog.facts[topic]


@lru_cache(maxsize=1)
def information_service() -> InformationService:
    return InformationService(InformationCatalog.load(config_directory() / "information.yaml"))
