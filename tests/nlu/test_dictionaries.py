import pytest

from app.nlu.catalog import load_catalog
from app.nlu.dictionaries import (
    FLAGS_PATH,
    FORM_KEYS,
    RED_FLAGS_PATH,
    SYNONYMS_PATH,
    action_lexicon,
    day_part_lexicon,
    load_flags,
    load_red_flags,
    load_specialties,
    read_yaml,
    service_lexicon,
    specialty_lexicon,
)
from app.policies.safety import check
from app.voice.phrases import load_phrasebook

# Все профили расписания стенда (Расписание_врачей_25.06-31.12.2026.xlsx) в виде ключа backend:
# нижний регистр, «Лор (отоларинголог)» → «лор», «Детский отоларинголог» → «детский лор»
STAND_SPECIALTIES = {
    "терапевт",
    "кардиолог",
    "гастроэнтеролог",
    "невролог",
    "эндокринолог",
    "нефролог",
    "аллерголог-иммунолог",
    "дерматовенеролог",
    "лор",
    "офтальмолог",
    "пульмонолог",
    "ревматолог",
    "уролог",
    "хирург",
    "травматолог",
    "ортопед",
    "флеболог",
    "врач узи-диагностики",
    "педиатр",
    "неонатолог",
    "детский невролог",
    "детский кардиолог",
    "детский эндокринолог",
    "детский аллерголог-иммунолог",
    "детский лор",
    "детский офтальмолог",
    "детский хирург",
    "детский ортопед-травматолог",
    "детский уролог-андролог",
    "акушер-гинеколог",
    "гинеколог-эндокринолог",
    "репродуктолог",
    "маммолог",
    "врач узи-диагностики (акушерство)",
    "стоматолог-терапевт",
    "стоматолог-хирург",
    "стоматолог-ортопед",
    "ортодонт",
    "детский стоматолог",
    "пародонтолог",
}
# Как специальности записаны в истории записей поликлиники (data/appointments.csv)
HISTORY_SPECIALTIES = {
    "акушер-гинеколог": "акушер-гинеколог",
    "гастроэнтеролог": "гастроэнтеролог",
    "дерматовенеролог": "дерматовенеролог",
    "кардиолог": "кардиолог",
    "невролог": "невролог",
    "оториноларинголог": "лор",
    "офтальмолог": "офтальмолог",
    "педиатр": "педиатр",
    "терапевт": "терапевт",
    "травматолог-ортопед": "травматолог",
    "уролог": "уролог",
    "хирург": "хирург",
    "эндокринолог": "эндокринолог",
}


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def red_flags():
    return load_red_flags()


@pytest.fixture(scope="module")
def flags():
    return load_flags()


@pytest.fixture(scope="module")
def specialties():
    return specialty_lexicon()


def keys(lexicon, text):
    return {hit.key for hit in lexicon.find(text)}


def check_examples(lexicon):
    for entry in lexicon.entries:
        for example in entry.examples:
            assert entry.key in keys(lexicon, example), (entry.key, example)
        for phrase in entry.negatives:
            assert entry.key not in keys(lexicon, phrase), (entry.key, phrase)


def test_red_flag_examples(red_flags):
    check_examples(red_flags)


def test_emergency_intent_examples_are_red_a(catalog, red_flags):
    for example in catalog.intents["I-11"].examples:
        assert any(k.startswith("red_a.") for k in keys(red_flags, example)), example


def test_ordinary_requests_raise_no_red_flags(catalog, red_flags):
    phrases = [e for code, i in catalog.intents.items() if code != "I-11" for e in i.examples]
    phrases += [e for i in catalog.service_intents.values() for e in i.examples]
    phrases += [
        e
        for entry in load_flags().entries
        if entry.key != "dental_acute"
        for e in entry.examples + entry.negatives
    ]
    for phrase in phrases:
        assert keys(red_flags, phrase) == set(), phrase


REGULATION_RED_A = [
    "боль в груди", "давит за грудиной", "жжёт в груди", "отдаёт в левую руку",
    "сердечный приступ", "инфаркт", "не может дышать", "задыхается", "синеет",
    "хрипит", "подавился", "потерял сознание", "без сознания", "не приходит в себя",
    "не могу разбудить", "упал в обморок и не встаёт", "перекосило лицо", "отнялась рука",
    "отнялась нога", "не может говорить", "речь невнятная", "онемела половина тела",
    "судороги", "трясёт, не останавливается", "припадок", "сильное кровотечение",
    "кровь не останавливается", "рвота кровью", "кровь изо рта", "ДТП",
    "сильно ударился головой", "упал с высоты", "ожог большой", "отравился",
    "выпил химию", "проглотил таблетки", "укусила змея", "отекает горло", "отекает лицо",
    "не может глотать", "анафилактический шок", "младенец вялый, не просыпается",
    "у грудного ребёнка температура", "ребёнок посинел", "ребёнок проглотил",
    "беременная, кровотечение", "отошли воды", "сильная боль в животе при беременности",
    "начались схватки", "не хочу жить", "хочу покончить с собой", "наглотался таблеток",
    "угрожает убить",
]

REGULATION_RED_B = [
    "очень сильная боль", "невыносимо болит живот", "невыносимо болит спина",
    "невыносимо болит голова", "боль усиливается", "температура сорок",
    "температура не сбивается", "температура третий день и хуже", "стало намного хуже",
    "резко поднялось давление", "кружится голова, не могу встать",
    "подвернул ногу, распухла", "порезался глубоко",
]


@pytest.mark.parametrize("phrase", REGULATION_RED_A)
def test_regulation_red_a_phrases_always_route_to_emergency(phrase):
    assert check(phrase).rule == "R-01", phrase


@pytest.mark.parametrize("phrase", REGULATION_RED_B)
def test_regulation_red_b_phrases_raise_acute_flag(phrase):
    assert "red_b" in check(phrase).flags, phrase


def test_acute_tooth_pain_is_also_red_b(flags, red_flags):
    # R-09 стоит в матрице выше R-11, поэтому такой звонок уйдёт в стоматологию
    for phrase in ("Сильно болит зуб", "Острая зубная боль"):
        assert "dental_acute" in keys(flags, phrase)
        assert keys(red_flags, phrase) == {"red_b.acute_pain"}


# перефразировки, которых нет в регламенте дословно
@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("у мужа болит сердце и немеет левая рука", {"red_a.heart", "red_a.stroke"}),
        ("мама упала, лежит и не отвечает", {"red_a.consciousness"}),
        ("задыхаюсь", {"red_a.breathing"}),
        ("ребёнок не дышит", {"red_a.breathing"}),
        ("кажется, у папы инсульт", {"red_a.stroke"}),
        ("кровь хлещет из ноги", {"red_a.bleeding"}),
        ("сын выпил таблетки бабушки", {"red_a.trauma"}),
        ("дочка проглотила батарейку", {"red_a.trauma", "red_a.children"}),
        ("сердце колет", {"red_a.heart"}),
        ("хочу умереть", {"red_a.self_harm"}),
        ("жить не хочется", {"red_a.self_harm"}),
        ("очень сильно болит голова и рвота", {"red_b.acute_pain"}),
        ("температура тридцать девять и пять, не сбивается", {"red_b.fever"}),
        ("давление двести", {"red_b.worsening"}),
    ],
)
def test_red_flag_paraphrases(red_flags, phrase, expected):
    assert keys(red_flags, phrase) == expected


@pytest.mark.parametrize(
    "phrase",
    [
        "запишите к кардиологу, у меня давление",
        "у меня болит горло, нужен лор",
        "хочу сдать кровь",
        "почему у вас никто не отвечает на звонки",
        "можно записать на флюорографию грудной клетки",
        "давление сто сорок на девяносто, запишите к терапевту",
        "мне нужно к врачу, плохо слышу на одно ухо",
        "Девушка, а когда принимает хирург?",
        "хочу к врачу по поводу давления",
        "Какие анализы нужно сдать перед приёмом?",
        "у меня сахар высокий, к эндокринологу",
        "анализы готовы? хорошо, спасибо",
        "я сейчас не могу говорить, перезвоните",
        "не могу прийти, перенесите",
        "запишите меня к терапевту, пожалуйста, на утро",
        "нет, спасибо, ничего не нужно",
    ],
)
def test_ordinary_phrases_are_quiet(red_flags, flags, phrase):
    assert keys(red_flags, phrase) == set()
    assert keys(flags, phrase) == set()


def test_flag_examples(flags):
    check_examples(flags)


def test_flags_match_contract(flags):
    assert [e.key for e in flags.entries] == [
        "operator",
        "complaint",
        "medical",
        "robot",
        "no_recording",
        "dental_acute",
        "cant_cope",
    ]


@pytest.mark.parametrize(
    "phrase, expected_flags, expected_red",
    [
        ("Запишите к терапевту", set(), set()),
        ("ой, у меня вообще-то сильно давит в груди", set(), {"red_a.heart"}),
        ("Что мне выпить от головной боли?", {"medical"}, set()),
        ("то мне выпить от головной боли", {"medical"}, set()),
        ("Какой у меня холестерин в последнем анализе?", {"medical"}, set()),
        ("Соедините с человеком", {"operator"}, set()),
        ("Вы робот?", {"robot"}, set()),
        ("Я против записи разговора", {"no_recording"}, set()),
        ("Хочу пожаловаться на регистратуру", {"complaint"}, set()),
    ],
)
def test_control_dialogs(flags, red_flags, phrase, expected_flags, expected_red):
    # приложение Д, диалоги 1, 2, 3, 5, 14, 15, 16
    assert keys(flags, phrase) == expected_flags
    assert keys(red_flags, phrase) == expected_red


def test_routes_have_phrases():
    routes = {t.route for t in load_phrasebook().templates.values()}
    red = read_yaml(RED_FLAGS_PATH)
    assert red["red_a"]["route"] in routes and red["red_b"]["route"] in routes
    for name, data in read_yaml(FLAGS_PATH)["flags"].items():
        assert data.get("route") is None or data["route"] in routes, name


def test_specialty_examples(specialties):
    for entry in specialties.entries:
        for example in entry.examples:
            hit = specialties.best(example)
            assert hit is not None and hit.key == entry.key, (entry.key, example)


def test_spoken_forms_resolve_back(specialties):
    for name, spec in load_specialties().items():
        assert specialties.best(spec.forms["nomn"]).key == name
        assert specialties.best("запишите к " + spec.forms["datv"]).key == name


def test_every_stand_specialty_is_known():
    assert set(load_specialties()) == STAND_SPECIALTIES


def test_history_names_resolve(specialties):
    for name, key in HISTORY_SPECIALTIES.items():
        assert specialties.best(name).key == key, name


def test_direct_booking_as_in_regulation():
    # Раздел 2.3: без направления — участковые, хирург, офтальмолог, лор (и детские),
    # акушер-гинеколог, стоматолог-терапевт и стоматолог-хирург
    direct = {name for name, spec in load_specialties().items() if not spec.referral}
    assert direct == {
        "терапевт",
        "педиатр",
        "хирург",
        "офтальмолог",
        "лор",
        "детский хирург",
        "детский офтальмолог",
        "детский лор",
        "акушер-гинеколог",
        "стоматолог-терапевт",
        "стоматолог-хирург",
        "детский стоматолог",
    }


@pytest.mark.parametrize(
    "phrase, specialty",
    [
        # детский профиль — по слову «детский» или по ребёнку рядом
        ("Запишите к неврологу", "невролог"),
        ("Нужен детский невролог", "детский невролог"),
        ("Хочу записать сына к неврологу", "детский невролог"),
        ("Дочку к лору можно?", "детский лор"),
        ("Ребёнку проверить зрение", "детский офтальмолог"),
        ("Внучку к аллергологу", "детский аллерголог-иммунолог"),
        # похожие профили
        ("Нужно сделать УЗИ", "врач узи-диагностики"),
        ("УЗИ при беременности", "врач узи-диагностики (акушерство)"),
        ("К травматологу, у меня ушиб", "травматолог"),
        ("Мне к ортопеду насчёт плоскостопия", "ортопед"),
        ("Сына к ортопеду", "детский ортопед-травматолог"),
        ("Гинеколог эндокринолог принимает?", "гинеколог-эндокринолог"),
        ("Запишите к эндокринологу", "эндокринолог"),
        # стоматология
        ("Запишите к зубному", "стоматолог-терапевт"),
        ("Надо удалить зуб", "стоматолог-хирург"),
        ("Хочу поставить коронку", "стоматолог-ортопед"),
        ("Ребёнку нужны брекеты", "ортодонт"),
        ("Сыну к зубному", "детский стоматолог"),
        ("Кровят дёсны, к пародонтологу", "пародонтолог"),
    ],
)
def test_similar_specialties_apart(specialties, phrase, specialty):
    assert specialties.best(phrase).key == specialty


def test_slot_examples_resolve_specialty(catalog, specialties):
    for case in catalog.slot_examples:
        if "specialty" in case["slots"]:
            assert specialties.best(case["say"]).key == case["slots"]["specialty"], case["say"]


@pytest.mark.parametrize(
    "phrase, specialty",
    [
        ("Запишите к терапевту", "терапевт"),
        ("Нужен талон к лору", "лор"),
        ("Хочу к гинекологу на следующей неделе", "акушер-гинеколог"),
        ("Можно на приём к окулисту?", "офтальмолог"),
        ("Запишите меня к зубному на завтра", "стоматолог-терапевт"),
        ("Мне нужно записать дочку к педиатру", "педиатр"),
        ("На когда есть номерок к дерматологу?", "дерматовенеролог"),
        ("Хочу записаться к врачу", "терапевт"),
        ("Мне к врачу по сердцу", "кардиолог"),
        ("Запишите к глазному врачу", "офтальмолог"),
    ],
)
def test_booking_phrases_resolve_specialty(specialties, phrase, specialty):
    assert specialties.best(phrase).key == specialty


def test_phrase_forms_exist():
    for t in load_phrasebook().templates.values():
        for name, form in t.placeholders():
            if name == "specialty" and form:
                assert form in FORM_KEYS, t.id


def test_service_examples():
    check_examples(service_lexicon())


def test_action_examples():
    check_examples(action_lexicon())


def test_day_part_examples():
    check_examples(day_part_lexicon())


def test_day_parts_agree_with_catalog(catalog):
    expected = {case["say"]: case["slots"] for case in catalog.slot_examples}
    lexicon = day_part_lexicon()
    for name, data in read_yaml(SYNONYMS_PATH)["day_parts"].items():
        for example in data["examples"]:
            assert lexicon.best(example).key == name, example
            if example in expected:
                assert expected[example] == data["slots"], example
