"""Регрессия 44 вопросов FAQ из регламента РГ-ТЕЛ-01, разделы 6.1–6.9."""

import pytest
from fixture_bridge import pipeline, turn


def spoken(result):
    return " ".join(result["utterances"])


# Каждый FAQ-XX из таблицы 6 регламента проверяется через настоящее HTTP-приложение.
# Для вопросов, требующих действия, проверяется именно выбранный маршрут, а не только текст.
CASES = [
    pytest.param(
        "FAQ-01",
        "До скольки вы работаете?",
        "info",
        "clinic_hours",
        "семи тридцати до двадцати",
        id="FAQ-01",
    ),
    pytest.param(
        "FAQ-02",
        "Вы работаете в праздники?",
        "info_unavailable",
        "holidays",
        "подтверждённого праздничного графика",
        id="FAQ-02",
    ),
    pytest.param(
        "FAQ-03",
        "Как пройти в детское отделение?",
        "info",
        "address",
        "отдельный вход с торца",
        id="FAQ-03",
    ),
    pytest.param(
        "FAQ-04",
        "В каком кабинете принимает врач?",
        "info",
        "doctor_schedule_unavailable",
        "подтверждённого графика",
        id="FAQ-04",
    ),
    pytest.param(
        "FAQ-05",
        "Как дозвониться до стоматологии?",
        "info",
        "dental_transfer",
        "добавочный номер два",
        id="FAQ-05-dental",
    ),
    pytest.param(
        "FAQ-05",
        "Как дозвониться до платного отделения?",
        "info",
        "paid_transfer",
        "добавочный номер три",
        id="FAQ-05-paid",
    ),
    pytest.param(
        "FAQ-06",
        "Что взять с собой на приём?",
        "info",
        "visit_documents",
        "паспорт и полис",
        id="FAQ-06",
    ),
    pytest.param(
        "FAQ-07",
        "Можно записаться через Госуслуги?",
        "info",
        "online_booking",
        "круглосуточно",
        id="FAQ-07",
    ),
    pytest.param(
        "FAQ-08", "Почему нет талонов?", "info", "no_slots", "обновляется ежедневно", id="FAQ-08"
    ),
    pytest.param(
        "FAQ-09",
        "Можно попасть без записи?",
        "info",
        "walk_in",
        "кабинете неотложной помощи",
        id="FAQ-09",
    ),
    pytest.param(
        "FAQ-10",
        "Можно выбрать участкового врача?",
        "info",
        "choose_doctor",
        "не чаще одного раза в год",
        id="FAQ-10",
    ),
    pytest.param(
        "FAQ-11",
        "Я опаздываю на приём, что делать?",
        "info",
        "late_for_visit",
        "пятнадцать минут",
        id="FAQ-11",
    ),
    pytest.param(
        "FAQ-12",
        "Сколько ждать приёма у специалиста?",
        "waiting",
        "waiting_time",
        "четырнадцати рабочих дней",
        id="FAQ-12-specialist",
    ),
    pytest.param(
        "FAQ-12",
        "Сколько ждать приёма у терапевта?",
        "waiting",
        "waiting_time",
        "не более суток",
        id="FAQ-12-therapist",
    ),
    pytest.param(
        "FAQ-13",
        "Можно к кардиологу без направления?",
        "info",
        "referral_required",
        "нужно направление",
        id="FAQ-13",
    ),
    pytest.param(
        "FAQ-13",
        "Как попасть к кардиологу без направления?",
        "info",
        "referral_required",
        "нужно направление",
        id="FAQ-13-how-to",
    ),
    pytest.param("FAQ-14", "Как отменить запись?", "cancel", "", "дату рождения", id="FAQ-14"),
    pytest.param(
        "FAQ-15", "Как вызвать врача на дом?", "transfer", "R-08", "регистратору", id="FAQ-15"
    ),
    pytest.param(
        "FAQ-16",
        "Во сколько придёт врач?",
        "info",
        "home_visit_time",
        "не позднее восемнадцати",
        id="FAQ-16",
    ),
    pytest.param(
        "FAQ-17",
        "Уже поздно, а врач нужен сегодня",
        "info",
        "home_visit_late",
        "запрос на завтра",
        id="FAQ-17",
    ),
    pytest.param(
        "FAQ-18",
        "Нужно ли вызывать врача, если высокая температура?",
        "transfer",
        "R-08",
        "регистратору",
        id="FAQ-18",
    ),
    pytest.param(
        "FAQ-19", "Как сдать анализы?", "info", "blood_hours", "по направлению врача", id="FAQ-19"
    ),
    pytest.param(
        "FAQ-20",
        "Как подготовиться к анализу крови?",
        "info",
        "blood_preparation",
        "утром натощак",
        id="FAQ-20",
    ),
    pytest.param(
        "FAQ-21",
        "Готовы ли мои анализы?",
        "info_unavailable",
        "lab_status",
        "проверить не могу",
        id="FAQ-21",
    ),
    pytest.param(
        "FAQ-22",
        "Можно узнать результат по телефону?",
        "info",
        "lab_phone",
        "по телефону не сообщаются",
        id="FAQ-22",
    ),
    pytest.param(
        "FAQ-23",
        "Можно сделать флюорографию без направления?",
        "info",
        "fluorography",
        "пятнадцати лет",
        id="FAQ-23",
    ),
    pytest.param(
        "FAQ-23",
        "Как сделать киографию?",
        "info",
        "fluorography",
        "пятнадцати лет",
        id="FAQ-23-asr-pronunciation",
    ),
    pytest.param(
        "FAQ-24",
        "Можно сдать общий анализ крови платно без направления?",
        "paid_blood",
        "",
        "четыреста пятьдесят",
        id="FAQ-24",
    ),
    pytest.param(
        "FAQ-25", "Как продлить больничный?", "info", "sick_leave", "после осмотра", id="FAQ-25"
    ),
    pytest.param(
        "FAQ-26",
        "Нужна справка в бассейн для ребёнка",
        "info",
        "pool_certificate",
        "педиатр",
        id="FAQ-26",
    ),
    pytest.param(
        "FAQ-27",
        "Нужна медкомиссия на права",
        "transfer",
        "R-10",
        "платным отделением",
        id="FAQ-27",
    ),
    pytest.param(
        "FAQ-28",
        "Нужна выписка из медкарты",
        "info",
        "record_copy",
        "тридцати календарных дней",
        id="FAQ-28",
    ),
    pytest.param(
        "FAQ-28",
        "Как получить выписку из медкарты?",
        "info",
        "record_copy",
        "тридцати календарных дней",
        id="FAQ-28-how-to",
    ),
    pytest.param(
        "FAQ-29", "Нужен льготный рецепт", "info", "prescription", "выписывает врач", id="FAQ-29"
    ),
    pytest.param(
        "FAQ-30", "Как прикрепиться к поликлинике?", "info", "attachment", "снилс", id="FAQ-30"
    ),
    pytest.param(
        "FAQ-31",
        "Я не прикреплён, можно ли на приём?",
        "info",
        "before_attachment",
        "неотложную и экстренную помощь",
        id="FAQ-31",
    ),
    pytest.param(
        "FAQ-32",
        "У меня полис другого региона",
        "info",
        "regional_policy",
        "по всей россии",
        id="FAQ-32",
    ),
    pytest.param(
        "FAQ-33",
        "Как получить или заменить полис?",
        "info",
        "insurance_policy",
        "страховая компания",
        id="FAQ-33",
    ),
    pytest.param(
        "FAQ-34", "Работаете по ДМС?", "transfer", "R-10", "платным отделением", id="FAQ-34"
    ),
    pytest.param(
        "FAQ-35",
        "Сколько стоит УЗИ щитовидки?",
        "paid_ultrasound",
        "",
        "тысячу триста",
        id="FAQ-35",
    ),
    pytest.param("FAQ-36", "Как оплатить?", "info", "payment", "наличными или картой", id="FAQ-36"),
    pytest.param(
        "FAQ-37",
        "Нужна справка для налогового вычета",
        "tax_certificate",
        "tax_certificate",
        "платном отделении",
        id="FAQ-37",
    ),
    pytest.param(
        "FAQ-38", "Как пройти диспансеризацию?", "info", "checkup", "раз в три года", id="FAQ-38"
    ),
    pytest.param(
        "FAQ-39", "Как сделать прививку ребёнку?", "info", "vaccination", "педиатра", id="FAQ-39"
    ),
    pytest.param(
        "FAQ-39", "Как сделать прививку?", "info", "vaccination", "терапевта", id="FAQ-39-how-to"
    ),
    pytest.param(
        "FAQ-40",
        "Есть ли вакцина от гриппа?",
        "transfer",
        "R-08",
        "уточнить наличие вакцины",
        id="FAQ-40",
    ),
    pytest.param("FAQ-41", "Что выпить от температуры?", "medical", "", "только врач", id="FAQ-41"),
    pytest.param(
        "FAQ-42", "К какому врачу идти с головной болью?", "medical", "", "терапевту", id="FAQ-42"
    ),
    pytest.param(
        "FAQ-43",
        "Нормальные ли у меня анализы?",
        "medical",
        "",
        "не могу их оценивать",
        id="FAQ-43",
    ),
    pytest.param(
        "FAQ-44", "Вы робот? Соедините с человеком", "transfer", "R-02", "регистратору", id="FAQ-44"
    ),
]


@pytest.mark.parametrize(("faq_id", "question", "mode", "expected", "required"), CASES)
async def test_all_regulation_faq_rows_use_the_expected_dialogue_path(
    faq_id, question, mode, expected, required
):
    prices = {}
    if mode == "paid_blood":
        prices["общий анализ крови"] = 450
    elif mode == "paid_ultrasound":
        prices["узи щитовидной железы"] = 1300

    async with pipeline(paid_prices=prices) as (client, service, emulator, *_):
        result = await turn(client, 1, question)
        text = spoken(result).casefold()

        if mode in {"info", "info_unavailable", "waiting"}:
            assert result["information"]["topic"] == expected, (faq_id, result)
            assert required.casefold() in text, (faq_id, result)
            assert result["code"] == (
                "INFO_UNAVAILABLE" if mode == "info_unavailable" else "INFO_GIVEN"
            )
            assert emulator.log == [] and emulator.write_count == emulator.patch_count == 0
        elif mode == "transfer":
            assert result["transfer"]["route"] == expected, (faq_id, result)
            assert required.casefold() in text, (faq_id, result)
            assert emulator.write_count == emulator.patch_count == 0
        elif mode in {"paid_blood", "paid_ultrasound"}:
            assert result["code"] == "INFO_GIVEN", (faq_id, result)
            assert required.casefold() in text, (faq_id, result)
            assert result["transfer"] is None
            assert emulator.write_count == emulator.patch_count == 0
        elif mode == "cancel":
            state = await service.store.inspect("scenario")
            assert state.purpose == "cancel" and state.client_id is None
            assert result["phase"] == "identifying" and required.casefold() in text
            assert emulator.write_count == emulator.patch_count == 0
        elif mode == "tax_certificate":
            assert result["information"]["topic"] == expected, (faq_id, result)
            assert required.casefold() in text
            connected = await turn(client, 2, "Да")
            assert connected["transfer"]["route"] == "R-10", (faq_id, connected)
        elif mode == "medical":
            assert result["code"] == "MEDICAL_BOUNDARY", (faq_id, result)
            assert required.casefold() in text, (faq_id, result)
            assert result["transfer"] is None and emulator.write_count == 0
        else:
            pytest.fail(f"Неописанный режим для {faq_id}: {mode}")
