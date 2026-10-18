"""RulesNlu остаётся неизменным: здесь переводим его результат в шаги нашего backend."""

from app.clock import CLINIC_TIMEZONE
from app.contracts import DialogOption, NluContext, NluRequest, NluResult, PublicSlots
from app.dialogue.doctors import DoctorNames
from app.dialogue.expectation import expected_answer
from app.dialogue.models import DialogueState, Phase, Turn
from app.mis.models import name_key
from app.mis.ports import MedicalSystem
from app.nlu.lexicon import tokenize
from app.nlu.service import RulesNlu
from app.ports import Clock

IDENTITY_WORDS = frozenset(
    {
        "я",
        "меня",
        "зовут",
        "это",
        "пациент",
        "пациентка",
        "другой",
        "другая",
        "нет",
        "да",
        "пожалуйста",
        "фамилия",
        "имя",
        "отчество",
        "дата",
        "рождения",
        "родился",
        "родилась",
        "год",
        "года",
        "году",
        "г",
        "правильно",
        "исправляю",
    }
)


def explicit_identity(parser: RulesNlu, request: NluRequest, doctor_names) -> NluResult | None:
    """Полное ФИО — явное исправление личности, а не дата приёма.

    Узкая граница: три имени и только слова представления/даты. Просьба «к врачу
    Иванову Ивану Ивановичу на пятницу» остаётся выбором врача, не сменой пациента.
    """
    if request.expect in {"full_name", "birth_date"} or len(tokenize(request.text)) < 3:
        return None
    candidate = parser.parse(request.model_copy(update={"expect": "full_name"}))
    fields = ("last_name", "first_name", "middle_name")
    if not all(getattr(candidate.slots, field) for field in fields):
        return None
    allowed = set(IDENTITY_WORDS)
    for field in (*fields, "birth_date"):
        allowed.update(tokenize(candidate.evidence.get(field, "")))
    if any(word not in allowed and not word.isdigit() for word in tokenize(request.text)):
        return None
    full_name = name_key(" ".join(getattr(candidate.slots, field) for field in fields))
    introduced = bool(set(tokenize(request.text)) & {"меня", "зовут", "пациент", "пациентка"})
    if (
        candidate.slots.birth_date is None
        and not introduced
        and full_name in {name_key(name) for name in doctor_names}
    ):
        return None
    return candidate


class RulesInput:
    def __init__(self, mis: MedicalSystem, clock: Clock, schedule: object = None) -> None:
        self.mis = mis
        self.clock = clock
        self.names = DoctorNames(mis, schedule)

    async def parse(self, state: DialogueState, turn: Turn) -> NluResult:
        # Понимание локальное: «да», «оператора» и новая дата не ждут справочника МИС.
        doctors = await self.names.understanding()
        names = {d.id: d.full_name for d in doctors}
        parser = RulesNlu(names.values())
        expect = expected_answer(state)
        options = tuple(
            DialogOption(
                id=f"doctor-{s.doctor_id}-{s.scheduled_at.strftime('%Y%m%d%H%M')}",
                day=s.scheduled_at.date(),
                at=s.scheduled_at.time(),
                doctor=names.get(s.doctor_id),
                specialty=s.specialty,
            )
            for s in state.options
        )
        if state.phase in {Phase.CANCEL_CHOOSING, Phase.MOVE_CHOOSING}:
            options = tuple(
                DialogOption(
                    id=f"visit-{v.visit_id}",
                    day=v.scheduled_at.date(),
                    at=v.scheduled_at.time(),
                    doctor=v.doctor_name,
                    specialty=v.specialty,
                )
                for v in state.visit_items
            )
        context = NluContext(
            intent="I-02"
            if state.purpose == "reschedule"
            else "I-03"
            if state.purpose == "cancel"
            else "I-04"
            if state.purpose == "visits"
            else "I-01"
            if state.specialty
            else None,
            slots=PublicSlots(**({"specialty": state.specialty} if state.specialty else {})),
        )
        request = NluRequest(
            call_id=state.call_id,
            turn_id=turn.turn_id,
            text=turn.text,
            # При ожидании даты пациент может также исправить ранее названное ФИО.
            expect="full_name" if expect == "birth_date" else expect,
            today=self.clock.now().astimezone(CLINIC_TIMEZONE).date(),
            options=options,
            context=context,
        )
        identity = explicit_identity(parser, request, names.values())
        if identity is not None:
            return identity
        result = parser.parse(request)
        if result.call_id != state.call_id or result.turn_id != turn.turn_id:
            raise RuntimeError("NLU вернул другую реплику")
        # В yes/no и choice парсер отвечает на вопрос. Отдельно извлекаем исправления параметров.
        # «Да, но в пятницу» не должно подтверждать прежний день.
        if expect in {"yes_no", "choice"} and "choice" not in result.intents:
            refinement = parser.parse(request.model_copy(update={"expect": "free"}))
            if refinement.call_id != state.call_id or refinement.turn_id != turn.turn_id:
                raise RuntimeError("NLU вернул другую реплику")
            fields = refinement.slots.model_fields_set - {"choice_index"}
            if fields or refinement.intents:
                return NluResult(
                    call_id=result.call_id,
                    turn_id=result.turn_id,
                    intents=tuple(dict.fromkeys((*result.intents, *refinement.intents))),
                    slots=refinement.slots,
                    flags=result.flags | refinement.flags,
                    evidence=refinement.evidence,
                    alternatives=refinement.alternatives,
                    source="rules",
                )
        return result
