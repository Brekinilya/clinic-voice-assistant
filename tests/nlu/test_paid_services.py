from datetime import date

from app.contracts import NluRequest
from app.nlu.service import RulesNlu


def test_paid_ultrasound_question_resolves_the_catalog_service():
    result = RulesNlu().parse(
        NluRequest(
            call_id="paid-price-test",
            turn_id=1,
            text="Сколько у вас стоит УЗИ щитовидки?",
            today=date(2026, 10, 21),
        )
    )
    assert "I-08" in result.intents
    assert result.slots.service == "УЗИ щитовидной железы"
    assert "service" in result.evidence
