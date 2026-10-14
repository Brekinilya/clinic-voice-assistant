from datetime import date, time

import pytest

from app.contracts import DialogOption
from app.voice.phrases import MAX_WORDS, load_phrasebook
from app.voice.render import (
    Pronunciation,
    Renderer,
    RenderError,
    load_pronunciation,
    mark_focus,
    plain,
    say_date,
    say_options,
    say_person,
    say_specialty,
    say_time,
)

TODAY = date(2026, 9, 30)  # среда
RENDERER = Renderer()
PHRASEBOOK = load_phrasebook()


def option(day: date, at: time, doctor: str | None = None, specialty: str | None = None):
    return DialogOption(id=f"{day}-{at}", day=day, at=at, doctor=doctor, specialty=specialty)


@pytest.mark.parametrize(
    "day, form, words",
    [
        (date(2026, 10, 2), "", "в пятницу, второго октября"),
        (date(2026, 10, 2), "nomn", "пятница, второе октября"),
        (date(2026, 10, 2), "accs", "пятницу, второе октября"),
        (date(2026, 10, 2), "gent", "второго октября"),
        (date(2026, 10, 2), "short", "в пятницу"),
        (date(2026, 10, 2), "short_accs", "пятницу"),
        (date(2026, 10, 1), "", "завтра, первого октября"),
        (date(2026, 10, 1), "short", "завтра"),
        (date(2026, 9, 30), "short_accs", "сегодня"),
        (date(2026, 10, 6), "short", "во вторник"),
        # дальше недели одного дня недели мало
        (date(2026, 10, 12), "short", "двенадцатого октября"),
        (date(2026, 10, 12), "short_accs", "двенадцатое октября"),
        # год — только если без него непонятно
        (date(2027, 6, 1), "gent", "первого июня две тысячи двадцать седьмого года"),
        (date(2025, 12, 30), "gent", "тридцатого декабря две тысячи двадцать пятого года"),
    ],
)
def test_date(day, form, words):
    assert say_date(day, form, TODAY) == words


def test_new_year_without_year():
    assert say_date(date(2027, 1, 4), "", date(2026, 12, 28)) == "в понедельник, четвёртого января"


@pytest.mark.parametrize(
    "at, form, words",
    [
        (time(9, 30), "", "в девять тридцать"),
        (time(12), "nomn", "двенадцать ноль-ноль"),
        (time(9, 5), "", "в девять ноль пять"),
        (time(10, 21), "", "в десять двадцать одна"),
        (time(8), "gent", "восьми"),
        (time(9, 30), "gent", "девяти тридцати"),
    ],
)
def test_time(at, form, words):
    assert say_time(at, form) == words


@pytest.mark.parametrize(
    "name, case, words",
    [
        ("Кузнецова Анна Сергеевна", "datv", "Кузнецовой Анне Сергеевне"),
        ("Волков Артём Сергеевич", "gent", "Волкова Артёма Сергеевича"),
        ("Ольга Викторовна", "ablt", "Ольгой Викторовной"),
        ("Илья Андреевич", "datv", "Илье Андреевичу"),
        # несклоняемые фамилии остаются как есть
        ("Шевчук Мария Петровна", "datv", "Шевчук Марии Петровне"),
        ("Гончаренко Олег Ильич", "datv", "Гончаренко Олегу Ильичу"),
        ("Римский-Корсаков Николай Андреевич", "datv", "Римскому-Корсакову Николаю Андреевичу"),
    ],
)
def test_person(name, case, words):
    assert say_person(name, case) == words


def test_doctor_by_surname():
    assert say_person("Кузнецова Анна Сергеевна", "datv", surname_only=True) == "Кузнецовой"
    assert say_person("Шевчук Иван Петрович", "datv", surname_only=True) == "Шевчуку"


@pytest.mark.parametrize(
    "name, form, words",
    [
        ("оториноларинголог", "datv", "лору"),
        ("Терапевт", "plur", "терапевты"),
        ("лор", "gent", "лора"),
        ("детский хирург", "datv", "детскому хирургу"),
        ("врач общей практики", "datv", "врачу общей практики"),
    ],
)
def test_specialty(name, form, words):
    assert say_specialty(name, form) == words


def test_options_name_only_what_differs():
    same_doctor = (option(date(2026, 10, 1), time(8, 45)), option(date(2026, 10, 2), time(10, 30)))
    assert (
        say_options(same_doctor, TODAY)
        == "завтра в восемь сорок пять или в пятницу в десять тридцать"
    )

    doctors = (
        option(date(2026, 10, 2), time(9), "Кузнецова Анна Сергеевна"),
        option(date(2026, 10, 2), time(10), "Белова Ирина Олеговна"),
        option(date(2026, 10, 5), time(11), "Волков Артём Сергеевич"),
    )
    assert say_options(doctors, TODAY) == (
        "к Кузнецовой в пятницу в девять ноль-ноль, к Беловой в десять ноль-ноль "
        "или к Волкову в понедельник в одиннадцать ноль-ноль"
    )

    visits = (
        option(date(2026, 10, 2), time(9, 30), specialty="терапевт"),
        option(date(2026, 10, 6), time(11), specialty="оториноларинголог"),
    )
    assert say_options(visits, TODAY) == (
        "к терапевту в пятницу в девять тридцать или к лору во вторник в одиннадцать ноль-ноль"
    )


# Реплики из примеров диалогов раздела 5
def test_regulation_offer_after_lunch():
    options = (option(date(2026, 10, 2), time(15, 15)), option(date(2026, 10, 5), time(16)))
    said = RENDERER.render("booking.offer_period", TODAY, period="после обеда", options=options)
    assert said.text == (
        "После обеда: в пятницу в пятнадцать пятнадцать или в понедельник в шестнадцать ноль-ноль. "
        "Что удобнее?"
    )
    assert said.expect == "choice"


def test_regulation_confirm_booking():
    said = RENDERER.render(
        "booking.confirm",
        TODAY,
        specialty="терапевт",
        doctor="Кузнецова Анна Сергеевна",
        date=date(2026, 10, 2),
        time=time(15, 15),
        room="214",
    )
    assert said.text == (
        "Записываю: терапевт Кузнецова, пятница, второе октября, пятнадцать пятнадцать, "
        "кабинет двести четырнадцать. Верно?"
    )
    assert said.speech.endswith("*Верно*?")


def test_regulation_confirm_reschedule():
    said = RENDERER.render(
        "reschedule.confirm",
        date(2026, 9, 28),
        date=date(2026, 10, 6),
        time=time(12),
        room=214,
        old_date=date(2026, 10, 1),
    )
    assert said.text == (
        "Переношу на вторник, шестое октября, двенадцать ноль-ноль, кабинет двести четырнадцать. "
        "Запись на четверг отменяю. Верно?"
    )


def test_regulation_my_visit():
    said = RENDERER.render(
        "my_visits.one",
        TODAY,
        patient="Нина Андреевна",
        specialty="офтальмолог",
        doctor="Лебедев Павел Игоревич",
        date=date(2026, 10, 5),
        time=time(13, 20),
        room=118,
    )
    assert said.text.startswith(
        "Нина Андреевна, вы записаны к офтальмологу Лебедеву в понедельник, пятого октября, "
        "в тринадцать двадцать, кабинет сто восемнадцать."
    )


def test_explicit_focus_stays_out_of_text():
    said = RENDERER.render("remind.greeting", TODAY, patient="Ольга Викторовна")
    assert said.text.endswith("Я говорю с Ольгой Викторовной?")
    assert said.speech.endswith("Я говорю с *Ольгой Викторовной*?")


@pytest.mark.parametrize(
    "minutes, words",
    [
        (15, "в течение пятнадцати минут"),
        (1, "в течение одной минуты"),
        (2, "в течение двух минут"),
    ],
)
def test_number_agrees_with_noun(minutes, words):
    assert words in RENDERER.render("callback.offer", TODAY, minutes=minutes).text


@pytest.mark.parametrize(
    "price, words", [(1500, "тысячу пятьсот рублей"), (1, "один рубль"), (3, "три рубля")]
)
def test_price(price, words):
    said = RENDERER.render("paid.price", TODAY, service="УЗИ брюшной полости", price=price)
    assert said.text == f"УЗИ брюшной полости стоит {words}. Записать вас?"
    assert said.speech.startswith("уз+и брюшной")


def test_sentence_starts_with_capital():
    said = RENDERER.render(
        "schedule.specialty",
        TODAY,
        specialty="хирург",
        days="каждый будний день",
        schedule="Волков — с восьми до четырнадцати",
    )
    assert said.text.startswith("Хирурги принимают каждый будний день: Волков")


def test_retry_wording():
    said = RENDERER.render("ident.ask", TODAY, retry=True)
    assert said.text == PHRASEBOOK.get("ident.ask").retry


def test_utterance_carries_template_fields():
    said = RENDERER.render("emergency.call_103", TODAY)
    template = PHRASEBOOK.get("emergency.call_103")
    assert (said.template, said.route, said.end) == (
        "emergency.call_103",
        template.route,
        template.end,
    )


def test_missing_value_and_too_many_options():
    with pytest.raises(RenderError, match="нет значения"):
        RENDERER.render("booking.confirm", TODAY, specialty="терапевт")
    three = tuple(option(date(2026, 10, d), time(9)) for d in (1, 2, 5))
    with pytest.raises(RenderError, match="вариантов 3"):
        RENDERER.render("booking.offer_period", TODAY, period="утром", options=three)


def test_long_reply_drops_address_then_shortens_date():
    said = RENDERER.render(
        "my_visits.one",
        TODAY,
        patient="Александра Константиновна",
        specialty="травматолог-ортопед",
        doctor="Белоусова Анна Сергеевна",
        date=date(2026, 10, 28),
        time=time(19, 45),
        room="214а",
    )
    assert said.words <= MAX_WORDS
    assert said.text.startswith("Вы записаны к травматологу-ортопеду Белоусовой")


def test_fitting_options():
    late = tuple(option(date(2026, 10, d), time(19, 45)) for d in (5, 6, 7))
    assert RENDERER.fitting_options("booking.offer", TODAY, late) == 2
    early = tuple(option(date(2026, 10, d), time(9)) for d in (1, 2, 5))
    assert RENDERER.fitting_options("booking.offer", TODAY, early) == 3


TYPICAL = dict(
    patient="Ольга Викторовна",
    doctor="Кузнецова Анна Сергеевна",
    substitute="Белова Ирина Олеговна",
    specialty="терапевт",
    date=date(2026, 10, 2),
    old_date=date(2026, 10, 1),
    next_day=date(2026, 10, 1),
    time=time(10, 30),
    start=time(8),
    end=time(14),
    room=214,
    price=1500,
    minutes=15,
    period="после обеда",
    days="по будням",
    preferences="утро, любой будний день",
    service="справка для бассейна",
    test="общий анализ крови",
    schedule="Волков — с восьми до четырнадцати",
    summary="записала вас к терапевту",
)
# Длинные, но реальные значения: так выглядит худший звонок
LONGEST = dict(
    TYPICAL,
    patient="Александра Константиновна",
    doctor="Белоусова Анна Сергеевна",
    substitute="Никитина Ирина Олеговна",
    specialty="травматолог-ортопед",
    date=date(2026, 10, 28),
    old_date=date(2026, 10, 12),
    time=time(19, 45),
    start=time(8, 30),
    end=time(14, 45),
    room="214а",
    price=2450,
    period="на следующей неделе",
    days="по понедельникам и средам",
    service="УЗИ брюшной полости",
    schedule="Волков — с восьми до четырнадцати, Никитина — с четырнадцати до двадцати",
    summary="записала к терапевту и отменила запись к лору",
)
TYPICAL_OPTIONS = tuple(option(date(2026, 10, d), time(10, 30)) for d in (1, 2, 5))
LONGEST_OPTIONS = tuple(option(date(2026, 10, d), time(19, 45)) for d in (12, 13, 14))
CASES = [
    (t.id, retry)
    for t in PHRASEBOOK.templates.values()
    for retry in (False, True)
    if not retry or t.retry
]


@pytest.mark.parametrize(
    "template_id, retry", CASES, ids=[f"{t}{'-retry' if r else ''}" for t, r in CASES]
)
def test_every_template_fits(template_id, retry):
    template = PHRASEBOOK.get(template_id)
    typical = dict(TYPICAL, options=TYPICAL_OPTIONS[: template.options])
    assert RENDERER.render(template_id, TODAY, retry=retry, **typical).words <= MAX_WORDS
    if template.options:
        # в худшем случае реплика покажет меньше вариантов, но хотя бы один
        assert RENDERER.fitting_options(
            template_id, TODAY, LONGEST_OPTIONS[: template.options], **LONGEST
        )
    else:
        assert RENDERER.render(template_id, TODAY, retry=retry, **LONGEST).words <= MAX_WORDS


@pytest.mark.parametrize(
    "text, speech",
    [
        ("Записать вас?", "*Записать* вас?"),
        ("Вы меня слышите?", "Вы меня *слышите*?"),
        ("На пятницу, верно?", "На пятницу, *верно*?"),
        ("Могу ещё чем-то помочь?", "Могу ещё чем-то *помочь*?"),
        ("Хотите её перенести?", "Хотите её *перенести*?"),
        ("Удобно ли вам?", "*Удобно* ли вам?"),
        ("Продолжим запись к врачу?", "*Продолжим* запись к врачу?"),
        ("Что-нибудь ещё?", "Что-нибудь *ещё*?"),
        ("Телефон для связи этот?", "Телефон для связи *этот*?"),
        # центр уже выделен — не трогаем
        ("Это *временный* адрес?", "Это *временный* адрес?"),
        # специальный и альтернативный вопрос модель интонирует сама
        ("Чем могу помочь?", "Чем могу помочь?"),
        (
            "Какой вариант удобнее — завтра или в пятницу?",
            "Какой вариант удобнее — завтра или в пятницу?",
        ),
        ("Перенести или отменить?", "Перенести или отменить?"),
        ("Готово. Записать вас?", "Готово. *Записать* вас?"),
        ("Готово, вы записаны.", "Готово, вы записаны."),
    ],
)
def test_question_focus(text, speech):
    assert mark_focus(text) == speech
    assert plain(mark_focus(text)) == plain(text)


@pytest.mark.parametrize(
    "text, speech",
    [
        (
            "Подтвердите: терапевт, Кузнецова Анна Сергеевна, 02.10.2026 15:15. Всё верно?",
            "Подтвердите: терапевт, Кузнецова Анна Сергеевна, пятница, второе октября, "
            "пятнадцать пятнадцать. Всё *верно*?",
        ),
        (
            "Немедленно позвоните в скорую помощь: 103 или 112.",
            "Немедленно позвоните в скорую помощь: сто три или сто двенадцать.",
        ),
        (
            "Поликлиника № 7: запись перенесена на 06.10 в 12:00, кабинет 214.",
            "Поликлиника номер семь: запись перенесена на вторник, шестое октября, "
            "в двенадцать ноль-ноль, кабинет двести четырнадцать.",
        ),
        ("Врач придёт до 18:00.", "Врач придёт до восемнадцати."),
        ("Через 1 минуту.", "Через одну минуту."),
        ("SMS придёт на ваш номер.", "эсэм+эс придёт на ваш номер."),
    ],
)
def test_free_text(text, speech):
    assert RENDERER.speak(text, TODAY) == speech


def test_stress_dictionary_covers_word_forms():
    renderer = Renderer(pronunciation=Pronunciation(3, {}, {"лебедев": "Л+ебедев"}))
    assert renderer.speech("К Лебедеву, к Лебедеву.") == "К Л+ебедеву, к Л+ебедеву."
    assert renderer.speech("Лебедевская улица.") == "Лебедевская улица."


def test_pronunciation_config():
    assert load_pronunciation().intensity == 3
