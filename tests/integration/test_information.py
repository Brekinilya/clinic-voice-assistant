"""T09 через настоящее API: факты без МИС, безопасное продолжение записи и приоритет safety."""

from datetime import timedelta

import pytest
from fixture_bridge import pipeline, turn
from test_budget_dialogue import reserve
from test_cancellation import SETUP as CANCEL_SETUP
from test_cancellation import prepare as prepare_cancel
from test_rescheduling import SETUP as MOVE_SETUP
from test_rescheduling import prepare as prepare_move

from app.dialogue.models import DialogueState, Phase
from app.handoffs.memory import MemoryHandoffStore
from app.handoffs.service import HandoffService, HandoffSettings
from app.information.catalog import information_service
from app.mis.budget import MemoryBudget
from tests.voice.fakes import Recognizer, phrase
from tests.voice.test_session import answered, session, until


def spoken(result):
    return " ".join(result["utterances"])


@pytest.mark.parametrize(
    ("question", "topic", "required"),
    [
        ("До скольки вы работаете?", "clinic_hours", "семи тридцати до двадцати"),
        ("Работаете в субботу?", "clinic_hours", "дежурные терапевт и педиатр"),
        ("Когда работает регистратура?", "operator_hours", "восьми тридцати"),
        ("Когда работают операторы?", "operator_hours", "семи до двадцати"),
        ("Когда можно сдать кровь?", "blood_hours", "десяти тридцати"),
        ("Где получить результаты анализов?", "lab_collection", "окне номер три"),
        ("Когда выдача результатов?", "lab_collection", "По субботам и воскресеньям выдачи нет"),
        ("Как подготовиться к анализу крови?", "blood_preparation", "у назначившего врача"),
        ("Можно узнать результаты по телефону?", "lab_phone", "по телефону не сообщаются"),
        ("Как к вам прикрепиться?", "attachment", "до трёх рабочих дней"),
        ("Какие документы для прикрепления?", "attachment", "СНИЛС"),
        ("Пока не прикрепился, можно к врачу?", "before_attachment", "помощь оказывают всем"),
        ("Какой у вас адрес?", "address", "улица Центральная"),
        ("Что взять с собой на приём?", "visit_documents", "паспорт родителя"),
        ("Как называется поликлиника?", "clinic_name", "Городская поликлиника номер семь"),
        ("Что у вас есть в справке?", "broad_help", "что именно вас интересует"),
        (
            "Когда работает кабинет неотложной помощи?",
            "emergency_office_hours",
            "с восьми до двадцати",
        ),
        ("Как дозвониться до стоматологии?", "dental_transfer", "добавочный номер два"),
        ("Как дослониться до стоматологии?", "dental_transfer", "добавочный номер два"),
        ("Как дозвониться до платного отделения?", "paid_transfer", "добавочный номер три"),
        ("Можно записаться через Госуслуги?", "online_booking", "круглосуточно"),
        ("Почему нет талонов?", "no_slots", "обновляется ежедневно"),
        ("Можно попасть без записи?", "walk_in", "кабинете неотложной помощи"),
        ("Можно выбрать участкового врача?", "choose_doctor", "не чаще одного раза в год"),
        ("Я опаздываю на приём, что делать?", "late_for_visit", "пятнадцать минут"),
        ("Можно к специалисту без направления?", "referral_required", "нужно направление"),
        ("Во сколько придёт врач?", "home_visit_time", "не позднее восемнадцати"),
        ("Уже поздно, а врач нужен сегодня", "home_visit_late", "оформить запрос на завтра"),
        (
            "В каком кабинете принимает врач?",
            "doctor_schedule_unavailable",
            "подтверждённого графика",
        ),
        ("Сколько ждать приёма у специалиста?", "waiting_time", "четырнадцати рабочих дней"),
        ("Как получить или заменить полис?", "insurance_policy", "страховая компания"),
        ("Полис у меня из другого региона", "regional_policy", "действует по всей России"),
        ("Как оплатить услугу?", "payment", "наличными или картой"),
        ("Как оплачивать услугу?", "payment", "наличными или картой"),
        ("Как оплачивать приём?", "payment", "наличными или картой"),
        ("Как оплачивать?", "payment", "наличными или картой"),
        ("Где оплачивать платную услугу?", "payment", "наличными или картой"),
        ("Как внести оплату за приём?", "payment", "наличными или картой"),
        ("Можно рассчитаться картой?", "payment", "наличными или картой"),
        ("Какие есть способы оплаты?", "payment", "наличными или картой"),
        ("Как происходит оплата услуги?", "payment", "наличными или картой"),
        ("Нужна справка в бассейн", "pool_certificate", "шестьсот рублей"),
        ("Можно сделать флюорографию без направления?", "fluorography", "пятнадцати лет"),
        ("Как сделать киографию?", "fluorography", "пятнадцати лет"),
        ("Можно ли сделать клюорографию без направления?", "fluorography", "пятнадцати лет"),
        ("Как продлить больничный?", "sick_leave", "после осмотра"),
        ("Нужна копия документов", "record_copy", "тридцати календарных дней"),
        ("Мне нужен рецепт", "prescription", "выписывает врач"),
        ("Нужна справка для налогового вычета", "tax_certificate", "платном отделении"),
        ("Как пройти диспансеризацию?", "checkup", "раз в три года"),
        ("Как сделать прививку?", "vaccination", "через терапевта"),
        ("Есть ли вакцина от гриппа?", "vaccine_availability", "уточнить наличие вакцины"),
    ],
)
async def test_anonymous_information_never_reads_mis(question, topic, required):
    async with pipeline() as (client, service, emulator, *_):
        # Любое случайное чтение справочника/пациента обнаружится по журналу и этому запрету.
        async def forbidden(*args, **kwargs):
            pytest.fail("общая справка не должна запускать NLU со справочником МИС")

        service.conversation.input.parse = forbidden
        result = await turn(client, 1, question)
        if topic == "vaccine_availability":
            assert result["code"] == "TRANSFER_REQUESTED", result
            assert result["transfer"]["route"] == "R-08", result
        else:
            assert result["code"] == "INFO_GIVEN", result
        assert required.casefold() in spoken(result).casefold()
        assert result["information"] == {
            "document": "РГ-ТЕЛ-01",
            "version": "1.0 от 25.09.2026",
            "topic": topic,
            "sections": list(information_service().fact(topic).source),
        }
        if topic == "vaccine_availability":
            assert result["expect"] == "free"
        else:
            assert result["expect"] == "yes_no"
            assert result["transfer"] is None
        assert "дату рождения" not in spoken(result) and "фамилию" not in spoken(result)
        assert emulator.log == [] and emulator.write_count == emulator.patch_count == 0
        assert (await service.store.inspect("scenario")).client_id is None


@pytest.mark.parametrize(
    ("question", "code", "required"),
    [
        ("Что вы умеете?", "CAPABILITIES_GIVEN", "проверкой ваших приёмов"),
        ("Вы кто?", "CAPABILITIES_GIVEN", "виртуальный помощник"),
        ("Как вас зовут?", "CAPABILITIES_GIVEN", "виртуальный помощник"),
        (
            "У вас можно на массаж записаться?",
            "SERVICE_UNAVAILABLE",
            "нет расписания массажиста",
        ),
        ("Как записаться к врачу?", "BOOKING_GUIDANCE_GIVEN", "начать запись"),
        ("Как попасть к врачу?", "BOOKING_GUIDANCE_GIVEN", "начать запись"),
        (
            "Я хочу узнать, как записаться к врачу",
            "BOOKING_GUIDANCE_GIVEN",
            "личные данные понадобятся",
        ),
        ("Справочная информация", "GENERAL_HELP_REQUIRED", "Что именно вас интересует?"),
    ],
)
async def test_opening_help_questions_do_not_identify_or_query_mis(question, code, required):
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, question)
        assert result["code"] == code, result
        assert required.casefold() in spoken(result).casefold()
        assert result["phase"] == "ready"
        assert result["transfer"] is None
        assert result["visits"] == []
        assert emulator.log == [] and emulator.write_count == emulator.patch_count == 0
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.phase == Phase.READY


@pytest.mark.parametrize("phrase", ["Хорошо, спасибо", "Хорошо, спасибо, говорю"])
async def test_idle_thanks_get_a_polite_reply_without_mis_access(phrase):
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, phrase)

        assert result["code"] == "ACKNOWLEDGED", result
        assert "пожалуйста" in spoken(result).casefold()
        assert "ещё чем-то помочь" in spoken(result).casefold()
        assert result["phase"] == "ready"
        assert result["transfer"] is None and result["visits"] == []
        assert emulator.log == [] and emulator.write_count == emulator.patch_count == 0
        assert (await service.store.inspect("scenario")).client_id is None


async def test_user_can_stop_booking_during_identity_without_a_mis_lookup():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        result = await turn(client, 2, "Я не хочу записываться")

        state = await service.store.inspect("scenario")
        assert result["code"] == "BOOKING_TASK_ABANDONED"
        assert result["phase"] == "ready"
        assert "что именно вас интересует" in spoken(result).casefold()
        assert state.client_id is None and state.identity_draft is None
        assert not any(entry[2] == "clients" for entry in emulator.log)
        assert emulator.write_count == emulator.patch_count == 0


async def test_user_can_abandon_booking_and_ask_a_known_question_in_same_turn():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту")
        result = await turn(
            client,
            2,
            "Я не хочу записываться, хочу узнать можно ли выбрать врача",
        )

        state = await service.store.inspect("scenario")
        assert result["code"] == "INFO_GIVEN", result
        assert "не чаще одного раза в год" in spoken(result)
        assert result["phase"] == "ready"
        assert state.client_id is None and state.identity_draft is None
        assert state.specialty is None and state.purpose == "booking"
        assert not any(entry[2] == "clients" for entry in emulator.log)
        assert emulator.write_count == emulator.patch_count == 0


async def test_abandoning_a_held_booking_releases_local_hold_without_mis_write():
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        held = await service.store.inspect("scenario")
        action = await service.booking.store.get(held.proposal.action_id)
        writes = emulator.write_count + emulator.patch_count

        result = await turn(client, 4, "Я не хочу записываться")
        released = await service.booking.store.get(action.action_id)
        state = await service.store.inspect("scenario")

        assert result["code"] == "BOOKING_TASK_ABANDONED"
        assert result["phase"] == "ready"
        assert released.state.value == "cancelled"
        assert state.proposal is None and state.client_id is None
        assert emulator.write_count + emulator.patch_count == writes


async def test_paid_service_without_a_configured_price_uses_paid_department_route():
    async with pipeline() as (client, service, emulator, *_):
        result = await turn(client, 1, "Сколько у вас стоит УЗИ щитовидки?")

        state = await service.store.inspect("scenario")
        assert result["transfer"]["route"] == "R-10"
        assert result["phase"] == "handoff"
        assert state.client_id is None
        assert not any(entry[2] == "clients" for entry in emulator.log)
        assert emulator.write_count == emulator.patch_count == 0


async def test_paid_service_reads_price_from_configured_catalog_and_offers_more_help():
    async with pipeline(paid_prices={"УЗИ щитовидной железы": 1300}) as (client, _, emulator, *_):
        result = await turn(client, 1, "Сколько стоит УЗИ щитовидной железы?")
        assert result["code"] == "INFO_GIVEN"
        assert "тысячу триста" in spoken(result) and result["transfer"] is None

        declined = await turn(client, 2, "Нет")
        assert declined["code"] == "HELP_OFFERED" and "чем-то помочь" in spoken(declined)
        assert declined["phase"] != "ended"
        ended = await turn(client, 3, "Нет")
        assert ended["code"] == "CALL_ENDED" and ended["phase"] == "ended"
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize(
    ("question", "term"),
    [
        ("Срок готовности общего анализа крови", "один рабочий день"),
        ("Сколько дней делают общий анализ мочи?", "один рабочий день"),
        ("Срок биохимического анализа крови", "одного до двух"),
        ("Сколько делают биохимию?", "одного до двух"),
        ("Срок коагулограммы", "одного до двух"),
        ("Срок анализа на глюкозу", "одного до двух"),
        ("Срок анализа на гликированный гемоглобин", "одного до двух"),
        ("Срок гормональных исследований", "трёх до пяти"),
        ("Сколько дней делают ПЦР?", "двух до трёх"),
        ("Срок бактериологического посева", "пяти до семи"),
        ("Срок цитологического исследования", "семи до десяти"),
        ("Срок гистологического исследования", "десяти до четырнадцати"),
    ],
)
async def test_laboratory_terms_are_general_working_days(question, term):
    async with pipeline() as (client, _, emulator, *_):
        result = await turn(client, 1, question)
        assert result["code"] == "INFO_GIVEN", result
        assert term in spoken(result) and "рабочи" in spoken(result)
        assert emulator.log == []


async def test_unspecified_blood_test_is_clarified_and_does_not_mean_blood_count():
    async with pipeline() as (client, service, emulator, *_):
        first = await turn(client, 1, "Сколько дней делают анализ крови?")
        assert first["code"] == "INFORMATION_REQUIRED" and first["expect"] == "free"
        assert "один рабочий день" not in spoken(first)
        second = await turn(client, 2, "Биохимический")
        assert second["code"] == "INFO_GIVEN" and "одного до двух" in spoken(second)
        assert not (await service.store.inspect("scenario")).information_clarifying
        assert emulator.log == []


async def test_unknown_laboratory_term_has_a_bounded_honest_answer():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Сколько делают анализ крови?")
        result = await turn(client, 2, "На витамин Д")
        assert result["code"] == "INFO_UNAVAILABLE"
        assert "подтверждённого общего срока" in spoken(result)
        assert not (await service.store.inspect("scenario")).information_clarifying
        assert (await turn(client, 3, "Нет, спасибо"))["code"] == "CALL_ENDED"
        assert emulator.log == []


@pytest.mark.parametrize("question", ["Работаете в праздники?"])
async def test_unavailable_personal_status_and_holidays_are_not_invented(question):
    async with pipeline() as (client, _, emulator, *_):
        result = await turn(client, 1, question)
        assert result["code"] == "INFO_UNAVAILABLE"
        assert "у меня нет" in spoken(result) or "проверить не могу" in spoken(result)
        assert emulator.log == []


async def test_personal_lab_status_requires_identity_then_creates_manual_handoff():
    async with pipeline() as (client, service, emulator, clock, *_):
        handoff_store = MemoryHandoffStore()
        service.handoffs = HandoffService(
            handoff_store,
            service.booking.mis,
            clock,
            b"x" * 32,
            HandoffSettings(enabled=True),
        )
        first = await turn(client, 1, "Готовы ли мои анализы?")
        assert first["code"] == "IDENTIFICATION_REQUIRED"
        assert "фамилию, имя и отчество" in spoken(first)
        assert (await service.store.inspect("scenario")).lab_identity_stage == "name"
        assert emulator.log == []

        second = await turn(client, 2, "Смирнова Ольга Викторовна")
        assert second["code"] == "IDENTIFICATION_REQUIRED"
        assert "полную дату рождения" in spoken(second)

        answer = await turn(client, 3, "12 марта 1978 года")
        assert answer["code"] == "INFO_GIVEN", (answer, emulator.log)
        assert "личный статус" in spoken(answer).casefold()
        assert "сохранила запрос" in spoken(answer).casefold()
        assert "результат пока не подтверждён" in spoken(answer).casefold()
        assert "от одного до четырнадцати рабочих дней" in spoken(answer)
        assert emulator.write_count == emulator.patch_count == 0
        state = await service.store.inspect("scenario")
        assert state.client_id is None and state.phase.value == "ready"
        assert state.transfer is None
        request = await handoff_store.request_for_call("scenario")
        operation = handoff_store.mis_operations[request.mis_operation_id]
        assert "личный статус результата анализа" in operation.description
        assert "готов" not in operation.description.casefold()


async def test_under_15_cannot_receive_personal_lab_information_or_be_transferred():
    async with pipeline() as (client, _, emulator, *_):
        await turn(client, 1, "Готовы ли мои анализы?")
        await turn(client, 2, "Смирнова Алиса Дмитриевна")
        answer = await turn(client, 3, "7 апреля 2019 года")
        assert answer["code"] == "MINOR_RESULTS_REFUSED"
        assert "до пятнадцати лет" in spoken(answer).casefold()
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize(
    "name,birth_date",
    [
        ("Смирнова Анна Дмитриевна", "30 сентября 2011 года"),
        ("Смирнова Елена Дмитриевна", "30 сентября 2009 года"),
    ],
)
async def test_ages_15_to_17_can_ask_about_their_own_lab_status(name, birth_date):
    async with pipeline() as (client, service, emulator, clock, *_):
        service.handoffs = HandoffService(
            MemoryHandoffStore(),
            service.booking.mis,
            clock,
            b"x" * 32,
            HandoffSettings(enabled=True),
        )
        await turn(client, 1, "Готовы ли мои анализы?")
        await turn(client, 2, name)
        answer = await turn(client, 3, birth_date)
        assert answer["code"] == "INFO_GIVEN"
        assert "сохранила запрос" in spoken(answer).casefold()
        assert "результат пока не подтверждён" in spoken(answer).casefold()
        assert emulator.write_count == emulator.patch_count == 0


async def test_overdue_lab_result_creates_callback_task_and_posts_required_escalation():
    async with pipeline() as (client, service, emulator, clock, *_):
        handoff_store = MemoryHandoffStore()
        service.handoffs = HandoffService(
            handoff_store,
            service.booking.mis,
            clock,
            b"x" * 32,
            HandoffSettings(enabled=True),
        )

        first = await turn(client, 1, "Результат анализов задерживается уже три недели")
        assert first["code"] == "IDENTIFICATION_REQUIRED"
        await turn(client, 2, "Смирнова Ольга Викторовна")
        result = await turn(client, 3, "12 марта 1978 года")
        assert result["code"] == "CALLBACK"
        assert "срок результата превышен" in spoken(result).casefold()
        assert "не подтверждены" in spoken(result).casefold()
        request = await handoff_store.request_for_call("scenario")
        operation = handoff_store.mis_operations[request.mis_operation_id]
        assert operation.description.splitlines()[0] == "ПЕРЕВОД R-08 P3"

        assert await service.handoffs.deliver_mis_once() == 1
        assert emulator.write_count == 1
        assert emulator.patch_count == 0
        assert emulator.escalation_records["demo"][1].description == operation.description


async def test_information_followup_yes_repeat_and_no_are_local():
    async with pipeline() as (client, _, emulator, *_):
        original = await turn(client, 1, "Как прикрепиться?")
        repeated = await turn(client, 2, "Повторите")
        assert original["utterances"] == repeated["utterances"]
        assert repeated["information"] == original["information"]
        more = await turn(client, 3, "Да")
        assert more["code"] == "MORE_HELP_REQUIRED" and more["expect"] == "free"
        await turn(client, 4, "Какой у вас адрес?")
        assert (await turn(client, 5, "Нет, спасибо"))["code"] == "CALL_ENDED"
        assert emulator.log == []


async def test_information_survives_closed_budget_and_repeat_receipt():
    async with pipeline() as (client, service, emulator, clock, *_):
        budget = MemoryBudget(clock)
        await budget.defer(60)
        service.booking.mis._budget = budget
        first = await turn(client, 1, "Где забрать результаты анализов?")
        assert first["code"] == "INFO_GIVEN"
        assert await turn(client, 1, "Где забрать результаты анализов?") == first
        assert emulator.log == []


async def test_information_preserves_identity_draft_and_returns_to_birth_date():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Запишите к терапевту на пятницу")
        await turn(client, 2, "Смирнова Ольга Викторовна")
        before = await service.store.inspect("scenario")
        assert before.identity_draft
        mark = len(emulator.log)
        answer = await turn(client, 3, "Какие документы нужны для прикрепления?")
        assert answer["code"] == "INFO_GIVEN" and answer["expect"] == "birth_date"
        assert answer["utterances"][-1] == "Теперь назовите дату рождения пациента."
        after = await service.store.inspect("scenario")
        assert before.identity_draft == after.identity_draft and before.specialty == after.specialty
        assert before.preferences == after.preferences and emulator.log[mark:] == []
        offered = await turn(client, 4, "двенадцатое марта семьдесят восьмого")
        assert offered["phase"] == "offered", offered


async def test_information_at_booking_confirmation_preserves_hold_and_never_confirms():
    async with pipeline() as (client, service, emulator, *_):
        held = await reserve(client)
        before = await service.store.inspect("scenario")
        mark = len(emulator.log)
        answer = await turn(client, 4, "До скольки вы работаете?")
        after = await service.store.inspect("scenario")
        assert answer["code"] == "INFO_GIVEN" and answer["phase"] == "held"
        assert answer["proposal"] == held["proposal"] and after.proposal == before.proposal
        assert after.authorization is None and after.client_id == before.client_id
        assert "Подтверждаете" in spoken(answer) or "Верно" in spoken(answer)
        assert emulator.log[mark:] == [] and emulator.write_count == 0
        assert (await turn(client, 5, "Да"))["code"] == "BOOKED"
        assert emulator.write_count == 1


async def test_lab_clarification_yes_cannot_confirm_a_booking():
    async with pipeline() as (client, _, emulator, *_):
        await reserve(client)
        first = await turn(client, 4, "Сколько делают анализ крови?")
        assert first["expect"] == "free"
        answer = await turn(client, 5, "Да")
        assert answer["code"] == "INFO_UNAVAILABLE" and answer["phase"] == "held"
        assert emulator.write_count == 0
        assert (await turn(client, 6, "Да"))["code"] == "BOOKED"


async def test_information_does_not_extend_a_hold_or_book_after_expiry():
    async with pipeline() as (client, service, emulator, clock, *_):
        held = await reserve(client)
        clock.value += timedelta(seconds=89)
        answer = await turn(client, 4, "Какой у вас адрес?")
        assert answer["proposal"]["expires_at"] == held["proposal"]["expires_at"]
        clock.value += timedelta(seconds=2)
        expired = await turn(client, 5, "Да")
        assert expired["code"] == "PROPOSAL_EXPIRED" and emulator.write_count == 0
        assert (await service.store.inspect("scenario")).proposal is None


async def test_information_after_booking_does_not_repeat_the_booking():
    async with pipeline() as (client, _, emulator, *_):
        await reserve(client)
        assert (await turn(client, 4, "Да"))["code"] == "BOOKED"
        mark = len(emulator.log)
        result = await turn(client, 5, "Какие документы взять на приём?")
        assert result["code"] == "INFO_GIVEN" and result["phase"] == "booked"
        repeated = await turn(client, 6, "Повторите")
        assert repeated["utterances"] == result["utterances"]
        assert (await turn(client, 7, "Да"))["code"] == "MORE_HELP_REQUIRED"
        assert emulator.log[mark:] == [] and emulator.write_count == 1


async def test_information_with_unknown_operation_keeps_its_journal_and_proposal():
    async with pipeline() as (client, service, emulator, *_):
        await reserve(client)
        emulator.next_failure = "lost_response"
        assert (await turn(client, 4, "Да"))["phase"] == "reconciling"
        before = await service.store.inspect("scenario")
        action = await service.booking.store.get(before.proposal.action_id)
        mark = len(emulator.log)
        result = await turn(client, 5, "Какой у вас адрес?")
        after = await service.store.inspect("scenario")
        assert result["phase"] == "reconciling" and result["code"] == "INFO_GIVEN"
        assert before.proposal == after.proposal
        assert await service.booking.store.get(before.proposal.action_id) == action
        assert emulator.log[mark:] == [] and emulator.write_count == 1


@pytest.mark.parametrize(
    ("setup", "prepare", "expected"),
    [(CANCEL_SETUP, prepare_cancel, "cancel_confirm"), (MOVE_SETUP, prepare_move, "move_confirm")],
)
async def test_information_during_cancel_and_move_repeats_the_correct_confirmation(
    setup, prepare, expected
):
    async with pipeline(setup) as (client, service, emulator, *_):
        await prepare(client)
        before = await service.store.inspect("scenario")
        mark = len(emulator.log)
        result = await turn(client, before.last_turn_id + 1, "До скольки вы работаете?")
        after = await service.store.inspect("scenario")
        assert result["phase"] == expected and result["code"] == "INFO_GIVEN"
        assert before.cancel_proposal == after.cancel_proposal
        assert before.move_proposal == after.move_proposal and after.authorization is None
        assert (
            "Подтвердите отмену" in spoken(result)
            if expected == "cancel_confirm"
            else "Подтвердите перенос" in spoken(result)
        )
        assert emulator.log[mark:] == [] and emulator.write_count == emulator.patch_count == 0


async def test_doctor_change_during_move_is_not_replaced_by_general_faq():
    async with pipeline(MOVE_SETUP) as (client, service, emulator, *_):
        await prepare_move(client)
        before = await service.store.inspect("scenario")
        result = await turn(client, before.last_turn_id + 1, "Можно сменить врача на завтра?")
        after = await service.store.inspect("scenario")
        assert result["information"] is None, result
        assert result["code"] == "NO_SLOTS", result
        assert result["transfer"] is None, result
        assert after.move_proposal is None
        assert after.phase.value == "move_offered"
        assert emulator.write_count == emulator.patch_count == 0


async def test_new_booking_leaves_laboratory_clarification():
    async with pipeline() as (client, service, _, *_):
        await turn(client, 1, "Сколько делают анализ крови?")
        result = await turn(client, 2, "Лучше запишите меня к терапевту")
        assert result["phase"] == "identifying"
        current = await service.store.inspect("scenario")
        assert current.specialty == "терапевт" and not current.information_clarifying


@pytest.mark.parametrize(
    ("question", "route"),
    [
        ("Как вы работаете, сильно давит в груди", "R-01"),
        ("До скольки вы работаете, позовите оператора", "R-02"),
    ],
)
async def test_safety_preempts_information_even_in_a_clarification(question, route):
    async with pipeline() as (client, _, emulator, *_):
        await turn(client, 1, "Сколько делают анализ крови?")
        result = await turn(client, 2, question, final=False)
        assert result["phase"] == "handoff" and result["transfer"]["route"] == route
        assert result["information"] is None and emulator.log == []


async def test_result_interpretation_still_uses_medical_boundary():
    async with pipeline() as (client, service, emulator, *_):
        await turn(client, 1, "Сколько делают анализ крови?")
        result = await turn(client, 2, "Анализы хорошие?")
        assert (
            result["code"] == "MEDICAL_BOUNDARY" and result["information"]["topic"] == "lab_values"
        )
        assert "не могу их оценивать" in spoken(result) and "Госуслугах" in spoken(result)
        assert "Записать вас к терапевту?" in result["utterances"][-1]
        assert not (await service.store.inspect("scenario")).information_clarifying
        assert emulator.log == []


async def test_contextual_result_evaluation_has_no_values_and_returns_to_a_doctor_offer():
    async with pipeline() as (client, _, emulator, *_):
        await turn(client, 1, "Готовы ли мои анализы?")
        general = await turn(client, 2, "Общий анализ крови")
        assert general["code"] == "INFO_GIVEN"
        result = await turn(client, 3, "А там всё нормально?")
        assert result["code"] == "MEDICAL_BOUNDARY"
        assert "не могу их оценивать" in spoken(result)
        assert "Госуслугах" in spoken(result) and result["expect"] == "yes_no"
        assert (await turn(client, 4, "Нет, спасибо"))["code"] == "CALL_ENDED"
        assert emulator.log == []


async def test_yes_after_information_answers_the_pending_doctor_offer():
    async with pipeline() as (client, _, emulator, *_):
        await turn(client, 1, "Какой у меня холестерин в последнем анализе?")
        info = await turn(client, 2, "Срок готовности общего анализа крови")
        assert info["code"] == "INFO_GIVEN"
        assert info["utterances"][-1] == "Записать вас к терапевту?"
        result = await turn(client, 3, "Да")
        assert result["phase"] == "identifying" and result["code"] != "MORE_HELP_REQUIRED"
        assert emulator.write_count == emulator.patch_count == 0


@pytest.mark.parametrize(
    "question",
    [
        "Когда работает регистратура, позовите оператора",
        "Когда работают операторы, соедините с человеком",
        "До скольки работает регистратура, мне нужен оператор",
    ],
)
async def test_explicit_operator_request_is_not_suppressed_by_a_question_about_hours(question):
    async with pipeline() as (client, _, emulator, *_):
        result = await turn(client, 1, question)
        assert result["transfer"]["route"] == "R-02" and emulator.log == []


@pytest.mark.parametrize(
    "question",
    [
        "Запишите в поликлинику к терапевту на час",
        "Когда принимает хирург?",
        "До скольки работает хирург?",
        "Отмените мою запись",
        "Перенесите запись к хирургу",
        "Вы работаете по ДМС?",
    ],
)
def test_information_does_not_replace_actions_or_invent_a_doctors_schedule(question):
    if question == "Когда принимает хирург?":
        assert information_service().find(question) == "doctor_schedule_unavailable"
        return
    assert information_service().find(question) is None


@pytest.mark.postgres
async def test_information_clarification_is_persisted_in_postgres(pg_engine):
    async with pipeline(pg_engine=pg_engine) as (client, service, emulator, *_):
        await turn(client, 1, "Сколько делают анализ крови?")
        state = await service.store.inspect("scenario")
        assert state.information_clarifying and state.information_topic == "lab_terms"
        assert (await turn(client, 2, "Общий анализ крови"))["code"] == "INFO_GIVEN"
        assert not (await service.store.inspect("scenario")).information_clarifying
        assert emulator.log == []


def test_old_dialogue_json_remains_compatible():
    state = DialogueState.model_validate({"call_id": "old", "phase": Phase.READY})
    assert state.information_topic is None and not state.information_clarifying


async def test_voice_channel_speaks_information_and_accepts_the_followup_no():
    async with pipeline() as (_, service, emulator, *_):
        call, channel = session(
            service, Recognizer({9: "До скольки вы работаете?", 10: "Нет, спасибо"})
        )
        try:
            await answered(call)
            await call.feed(phrase(9))
            await until(lambda: any(r["code"] == "INFO_GIVEN" for r in channel.of("reply")))
            # Событие reply приходит до синтеза: ждём саму реплику и завершение ответа.
            await until(lambda: any("семи тридцати до двадцати" in text for text in channel.said()))
            await until(lambda: call._responder is not None and call._responder.done())
            await until(lambda: not call.speaking())
            assert any("семи тридцати до двадцати" in text for text in channel.said())
            await call.feed(phrase(10))
            await until(lambda: any(r["code"] == "CALL_ENDED" for r in channel.of("reply")))
            assert emulator.log == []
        finally:
            await call.close()
