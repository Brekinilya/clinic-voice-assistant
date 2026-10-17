"""Адаптер safety Ильи и наша организационная маршрутизация, без диагностики симптомов."""

from app.clock import CLINIC_TIMEZONE
from app.contracts import SafetyDecision
from app.dialogue.models import TransferRequest, Turn
from app.policies.safety import decide, operators_on_duty
from app.ports import Clock


class RulesSafety:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def evaluate(self, call_id: str, turn: Turn, extra=frozenset()) -> SafetyDecision:
        decision = decide(
            call_id,
            turn.turn_id,
            turn.text,
            final=turn.is_final,
            operators_on_duty=operators_on_duty(self.clock.now().astimezone(CLINIC_TIMEZONE)),
            extra=extra,
        )
        if (
            decision.call_id != call_id
            or decision.turn_id != turn.turn_id
            or decision.final != turn.is_final
        ):
            raise ValueError("safety вернул решение не для этой реплики")
        return decision


def route_for(decision: SafetyDecision) -> TransferRequest | None:
    # Сам флаг medical не означает перевод: сначала сценарий даёт граничную фразу.
    route = decision.rule
    if route is None:
        return None
    priority = (
        "P1"
        if route in {"R-01", "R-11"}
        else "P2"
        if route in {"R-02", "R-03", "R-09", "R-15"}
        else "P3"
    )
    return TransferRequest(route=route, priority=priority)
