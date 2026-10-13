import pytest

from app.nlu.lexicon import Entry, Lexicon, LexiconError, Pattern, tokenize


def find(pattern, text, window=6):
    return Pattern.parse(pattern).find(tokenize(text), window)


def test_tokenize():
    assert tokenize("Ухо-горло-нос, ЁЛКИ! 39,5") == ["ухо-горло-нос", "елки", "39", "5"]


def test_matches_by_normal_form():
    assert find("боль + грудь", "болью в груди")
    assert find("давить + грудина", "давит за грудиной")


def test_parts_in_any_order_within_window():
    assert find("грудь + боль", "в груди боль")
    assert find("боль + грудь", "боль раз два три четыре пять шесть грудь") is None


def test_alternative_of_several_words_is_consecutive():
    assert find("ухо горло нос", "к ухо горло нос")
    assert find("ухо горло нос", "ухо и горло и нос") is None


def test_exact_form():
    assert find("не + =может + говорить", "он не может говорить")
    assert find("не + =может + говорить", "я не могу говорить") is None


def test_prefix():
    assert find("гастро*", "гастроэнтерологу")


def test_parts_do_not_share_words():
    assert find("врач + врач", "к врачу") is None


@pytest.mark.parametrize("bad", ["а||б", "а + ", "=", "*"])
def test_empty_parts_rejected(bad):
    with pytest.raises(LexiconError):
        Pattern.parse(bad)


def test_exceptions_and_short_patterns():
    lexicon = Lexicon(
        [
            Entry.from_dict(
                "operator",
                {"patterns": ["оператор"], "short": ["человек"], "except": ["оператор + связь"]},
            )
        ],
        window=5,
    )
    assert lexicon.find("человека, пожалуйста")
    assert not lexicon.find("запишите меня, я пожилой человек")
    assert not lexicon.find("оператор сотовой связи")


def test_best_prefers_longer_match_and_uses_fallback_last():
    lexicon = Lexicon(
        [
            Entry.from_dict("терапевт", {"patterns": ["участковый"], "fallback": ["врач"]}),
            Entry.from_dict("педиатр", {"patterns": ["участковый педиатр", "врач + ребенок"]}),
        ],
        window=4,
    )
    assert lexicon.best("к участковому педиатру").key == "педиатр"
    assert lexicon.best("к врачу с ребёнком").key == "педиатр"
    assert lexicon.best("к врачу").key == "терапевт"
    assert lexicon.best("на завтра") is None


def test_duplicate_keys_rejected():
    entry = Entry.from_dict("a", {"patterns": ["x"]})
    with pytest.raises(LexiconError):
        Lexicon([entry, entry], window=3)
