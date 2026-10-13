"""Поиск словарных шаблонов во фразе по начальным формам слов.

Шаблон — строка вида «боль|давить + грудь|грудина»:
  «|» — любой из вариантов, вариант из нескольких слов — эти слова подряд;
  «+» — все части в пределах окна в любом порядке;
  «=слово» — точное совпадение словоформы, «слово*» — по началу слова.
Остальные слова сравниваются по множеству начальных форм (pymorphy3), «ё» приравнена к «е».
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, product

import pymorphy3

TOKEN = re.compile(r"[а-яa-z0-9]+(?:-[а-яa-z0-9]+)*")
SHORT_PHRASE = 3  # для шаблонов short: фраза не длиннее стольких слов

_morph: pymorphy3.MorphAnalyzer | None = None


class LexiconError(ValueError):
    pass


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    return TOKEN.findall(normalize(text))


@lru_cache(maxsize=65536)
def lemmas(token: str) -> frozenset[str]:
    global _morph
    if _morph is None:
        _morph = pymorphy3.MorphAnalyzer()
    forms = {normalize(p.normal_form) for p in _morph.parse(token)}
    forms.add(token)
    return frozenset(forms)


@dataclass(frozen=True)
class Word:
    text: str
    exact: bool = False
    prefix: bool = False

    @classmethod
    def parse(cls, raw: str) -> "Word":
        if raw.startswith("="):
            return cls(raw[1:], exact=True)
        if raw.endswith("*"):
            return cls(raw[:-1], prefix=True)
        return cls(raw)

    def matches(self, token: str) -> bool:
        if self.exact:
            return token == self.text
        if self.prefix:
            return any(form.startswith(self.text) for form in lemmas(token))
        return not lemmas(self.text).isdisjoint(lemmas(token))


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    weight: int  # сколько слов шаблона совпало


@dataclass(frozen=True)
class Pattern:
    source: str
    parts: tuple[tuple[tuple[Word, ...], ...], ...]

    @classmethod
    def parse(cls, source: str) -> "Pattern":
        parts = []
        for part in normalize(source).split("+"):
            alternatives = []
            for alt in part.split("|"):
                words = tuple(Word.parse(w) for w in alt.split())
                if not words or any(not w.text for w in words):
                    raise LexiconError(f"пустой вариант в шаблоне «{source}»")
                alternatives.append(words)
            parts.append(tuple(alternatives))
        return cls(source, tuple(parts))

    def find(self, tokens: list[str], window: int) -> Match | None:
        spans = []
        for alternatives in self.parts:
            found = [
                (i, i + len(seq))
                for seq in alternatives
                for i in range(len(tokens) - len(seq) + 1)
                if all(w.matches(tokens[i + k]) for k, w in enumerate(seq))
            ]
            if not found:
                return None
            spans.append(found)

        best = None
        for combo in product(*spans):
            if any(a[0] < b[1] and b[0] < a[1] for a, b in combinations(combo, 2)):
                continue
            start = min(s for s, _ in combo)
            end = max(e for _, e in combo)
            if len(combo) > 1 and end - start > window:
                continue
            match = Match(start, end, sum(e - s for s, e in combo))
            if best is None or (match.weight, best.end - best.start) > (best.weight, match.end - match.start):
                best = match
        return best


@dataclass(frozen=True)
class Entry:
    key: str
    patterns: tuple[Pattern, ...]
    short: tuple[Pattern, ...] = ()
    exceptions: tuple[Pattern, ...] = ()
    fallback: tuple[Pattern, ...] = ()
    examples: tuple[str, ...] = ()
    negatives: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, key: str, data: dict) -> "Entry":
        def patterns(name):
            return tuple(Pattern.parse(str(p)) for p in data.get(name) or ())

        entry = cls(
            key=key,
            patterns=patterns("patterns"),
            short=patterns("short"),
            exceptions=patterns("except"),
            fallback=patterns("fallback"),
            examples=tuple(str(e) for e in data.get("examples") or ()),
            negatives=tuple(str(e) for e in data.get("negatives") or ()),
        )
        if not (entry.patterns or entry.short or entry.fallback):
            raise LexiconError(f"{key}: нет ни одного шаблона")
        return entry


@dataclass(frozen=True)
class Hit:
    key: str
    pattern: str
    phrase: str
    weight: int


class Lexicon:
    def __init__(self, entries: list[Entry], window: int):
        keys = [e.key for e in entries]
        if len(keys) != len(set(keys)):
            raise LexiconError("ключи словаря повторяются")
        self.entries = entries
        self.window = window

    def find(self, text: str) -> list[Hit]:
        """Все сработавшие записи, по одному лучшему совпадению на запись."""
        tokens = tokenize(text)
        hits = []
        for entry in self.entries:
            if any(p.find(tokens, self.window) for p in entry.exceptions):
                continue
            candidates = list(entry.patterns)
            if len(tokens) <= SHORT_PHRASE:
                candidates += entry.short
            hit = self._best_hit(entry.key, candidates, tokens)
            if hit:
                hits.append(hit)
        return hits

    def best(self, text: str) -> Hit | None:
        """Самое длинное совпадение; fallback-шаблоны — только если не нашлось ничего другого."""
        hits = self.find(text)
        if not hits:
            tokens = tokenize(text)
            hits = [h for e in self.entries if (h := self._best_hit(e.key, e.fallback, tokens))]
        return max(hits, key=lambda h: h.weight, default=None)

    def _best_hit(self, key: str, patterns, tokens: list[str]) -> Hit | None:
        best = None
        for pattern in patterns:
            match = pattern.find(tokens, self.window)
            if match and (best is None or match.weight > best.weight):
                phrase = " ".join(tokens[match.start:match.end])
                best = Hit(key, pattern.source, phrase, match.weight)
        return best
