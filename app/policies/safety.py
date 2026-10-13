"""Проверка каждой реплики пациента: красные флаги (раздел 7.4) и сигналы для маршрутизации (раздел 7.3)."""

from dataclasses import dataclass

from app.nlu.dictionaries import load_flags, load_red_flags
from app.nlu.lexicon import Hit

FLAGS = (
    "red_a",
    "red_b",
    "operator",
    "complaint",
    "medical",
    "robot",
    "no_recording",
    "dental_acute",
    "cant_cope",
)

# Матрица 7.3 проверяется сверху вниз, но красный флаг Б здесь стоит выше просьбы оператора и жалобы:
# по разделу 7.1 красный флаг прерывает любой сценарий, а R-11 уходит к тому же регистратору с приоритетом P1.
# medical сам по себе не переводит: сначала граничная фраза, R-04 — только если пациент настаивает.
# robot и no_recording обрабатывает диалог (robot.answer, recording.off).
RULES = (
    ("red_a", "R-01"),
    ("red_b", "R-11"),
    ("operator", "R-02"),
    ("complaint", "R-03"),
    ("dental_acute", "R-09"),
    ("cant_cope", "R-15"),
)


@dataclass(frozen=True)
class SafetyVerdict:
    flags: frozenset[str] = frozenset()
    rule: str | None = None
    # False — по промежуточной расшифровке: на красный флаг можно реагировать сразу,
    # но изменения в стенде ждут итогового вердикта по реплике
    final: bool = True
    hits: tuple[Hit, ...] = ()

    @property
    def emergency(self) -> bool:
        return self.rule == "R-01"


def check(text: str, final: bool = True, operators_on_duty: bool = True) -> SafetyVerdict:
    hits = tuple(load_red_flags().find(text)) + tuple(load_flags().find(text))
    flags = frozenset(hit.key.split(".")[0] for hit in hits)
    return SafetyVerdict(flags, _rule(flags, hits, operators_on_duty), final, hits)


def with_flags(
    verdict: SafetyVerdict,
    extra: set[str] | frozenset[str],
    final: bool = True,
    operators_on_duty: bool = True,
) -> SafetyVerdict:
    """Добавить флаги общего разбора фразы. Снять флаг словаря нельзя: при сомнении флаг считается сработавшим."""
    unknown = set(extra) - set(FLAGS)
    if unknown:
        raise ValueError(f"неизвестные флаги: {sorted(unknown)}")
    flags = verdict.flags | frozenset(extra)
    return SafetyVerdict(flags, _rule(flags, verdict.hits, operators_on_duty), final, verdict.hits)


def _rule(flags: frozenset[str], hits: tuple[Hit, ...], operators_on_duty: bool) -> str | None:
    active = set(flags)
    red_b_groups = {hit.key for hit in hits if hit.key.startswith("red_b.")}
    if "dental_acute" in active and red_b_groups == {"red_b.acute_pain"}:
        # «сильно болит зуб» — острая боль, но маршрут для неё отдельный: стоматология в день обращения
        active.discard("red_b")
    if "red_b" in active and not operators_on_duty:
        # вне часов работы регистратуры категория Б обрабатывается как А (раздел 7.4)
        return "R-01"
    return next((rule for flag, rule in RULES if flag in active), None)
