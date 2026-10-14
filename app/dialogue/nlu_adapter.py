"""RulesNlu остаётся неизменным: здесь переводим его результат в шаги нашего backend."""

from app.clock import CLINIC_TIMEZONE
from app.contracts import DialogOption, NluContext, NluRequest, NluResult, PublicSlots
from app.dialogue.models import DialogueState, Phase, Turn
from app.mis.ports import MedicalSystem
from app.nlu.service import RulesNlu
from app.ports import Clock


class RulesInput:
    def __init__(self, mis: MedicalSystem, clock: Clock) -> None:
        self.mis = mis
        self.clock = clock

    async def parse(self, state: DialogueState, turn: Turn) -> NluResult:
        doctors = await self.mis.doctors()
        names = {d.id: d.full_name for d in doctors}
        parser = RulesNlu(names.values())
        expect = (
            "yes_no"
            if state.medical_boundary_pending
            else "full_name"
            if state.phase == Phase.IDENTIFYING
            else "choice"
            if state.phase in {Phase.OFFERED, Phase.CANCEL_CHOOSING}
            else "yes_no"
            if state.phase
            in {Phase.HELD, Phase.VERIFYING_OWNER, Phase.VISITS_SHOWN, Phase.CANCEL_CONFIRM}
            else "free"
        )
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
        if state.phase == Phase.CANCEL_CHOOSING:
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
            intent="I-03"
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
            expect=expect,
            today=self.clock.now().astimezone(CLINIC_TIMEZONE).date(),
            options=options,
            context=context,
        )
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
