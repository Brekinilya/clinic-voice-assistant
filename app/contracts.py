"""Общие сообщения NLU и голоса; соответствуют каталогу Ильи a41bb8f.

Локальные Parsed и SafetyVerdict Ильи остаются в его модулях. При стыковке
адаптер переведёт их в эти сообщения с идентификаторами звонка и реплики.
Эти модели проверяют формат данных, а разрешение операций остаётся в домене.
"""

from datetime import date as Date
from datetime import time as Time
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

MessageId = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
TurnId = Annotated[int, Field(strict=True, ge=1)]
ExpectedAnswer = Literal["yes_no", "choice", "full_name", "birth_date", "date_time", "free"]
IntentCode = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:I-(?:0[1-9]|1[0-4])|yes|no|choice|repeat|maybe|opt_out|unknown)$"
    ),
]
RouteCode = Annotated[str, StringConstraints(pattern=r"^R-(?:0[1-9]|1[0-7])$")]
SafetyFlag = Literal[
    "red_a",
    "red_b",
    "operator",
    "complaint",
    "medical",
    "robot",
    "no_recording",
    "dental_acute",
    "cant_cope",
]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class VisitReference(Message):
    specialty: str | None = None
    doctor: str | None = None
    date: Date | None = None
    time: Time | None = None


class PublicSlots(Message):
    """Параметры организационного запроса; поля идентификации здесь запрещены."""

    specialty: str | None = None
    doctor: str | None = None
    date_from: Date | None = None
    date_to: Date | None = None
    time_from: Time | None = None
    time_to: Time | None = None
    time_pref: Literal["early", "late"] | None = None
    exclude_weekdays: tuple[Annotated[int, Field(strict=True, ge=1, le=7)], ...] = ()
    choice_index: Annotated[int, Field(strict=True, ge=1)] | None = None
    visit_ref: VisitReference | None = None
    on_behalf: Literal["self", "child", "other"] | None = None
    service: str | None = None

    @model_serializer(mode="wrap")
    def serialize_supplied_slots(self, handler):
        # Неизвлечённые поля не добавляем в JSON: иначе после передачи им потребовалось бы evidence.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class NluSlots(PublicSlots):
    """Полный локальный результат. Сырые ФИО/полис не включаем в repr и логи."""

    last_name: str | None = Field(default=None, repr=False)
    first_name: str | None = Field(default=None, repr=False)
    middle_name: str | None = Field(default=None, repr=False)
    birth_date: Date | None = Field(default=None, repr=False)
    policy_number: Annotated[str, StringConstraints(pattern=r"^[0-9]{16}$")] | None = Field(
        default=None, repr=False
    )


class DialogOption(Message):
    """Выбор по устойчивому ID. Позиция с единицы соответствует parse_choice Ильи."""

    id: MessageId
    day: Date | None = None
    at: Time | None = None
    doctor: str | None = None
    specialty: str | None = None


class NluContext(Message):
    intent: IntentCode | None = None
    slots: PublicSlots = Field(default_factory=PublicSlots)

    @model_validator(mode="after")
    def only_public_slots(self) -> Self:
        # Pydantic принимает подклассы: дополнительно исключаем передачу NluSlots с ФИО.
        if type(self.slots) is not PublicSlots:
            raise ValueError("в контекст передаются только PublicSlots без полей идентификации")
        return self


class NluRequest(Message):
    """Сырой текст предназначен локальному NLU; перед внешним LLM нужна отдельная маскировка."""

    call_id: MessageId
    turn_id: TurnId
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ] = Field(repr=False)
    expect: ExpectedAnswer = "free"
    today: Date
    options: tuple[DialogOption, ...] = Field(default=(), max_length=3)
    context: NluContext = Field(default_factory=NluContext)


class NluResult(Message):
    call_id: MessageId
    turn_id: TurnId
    intents: tuple[IntentCode, ...] = ()
    slots: NluSlots = Field(default_factory=NluSlots)
    flags: frozenset[SafetyFlag] = frozenset()
    evidence: dict[str, str] = Field(default_factory=dict, repr=False)
    # Для даты рождения локальный парсер Ильи может вернуть два допустимых столетия.
    alternatives: dict[Literal["birth_date"], tuple[Date, ...]] = Field(
        default_factory=dict, repr=False
    )
    source: Literal["rules", "llm"]

    @model_validator(mode="after")
    def evidence_matches_slots(self) -> Self:
        fields = self.slots.model_fields_set
        if set(self.evidence) != fields:
            raise ValueError(
                "для каждого извлечённого параметра нужна фраза-основание, без лишних ключей"
            )
        if any(not phrase.strip() for phrase in self.evidence.values()):
            raise ValueError("фраза-основание не должна быть пустой")
        if self.alternatives and "birth_date" not in fields:
            raise ValueError("альтернативные даты рождения требуют извлечённой birth_date")
        return self


class SafetyDecision(Message):
    """Обёртка решения safety.check. Незавершённая проверка не разрешает изменение МИС."""

    call_id: MessageId
    turn_id: TurnId
    flags: frozenset[SafetyFlag] = frozenset()
    rule: RouteCode | None = None
    final: bool = Field(default=False, strict=True)


class VoiceInput(Message):
    call_id: MessageId
    turn_id: TurnId
    text: str = Field(repr=False, max_length=4000)
    is_final: bool = Field(strict=True)
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Self:
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("конец реплики не может быть раньше начала")
        return self


class CallEvent(Message):
    call_id: MessageId
    turn_id: TurnId | None = None
    kind: Literal[
        "started", "ended", "silence", "barge_in", "transfer_connected", "transfer_failed"
    ]


class ActionConfirmation(Message):
    """Ссылки для будущего барьера действия; сама модель ещё не выполняет операцию."""

    call_id: MessageId
    action_id: MessageId
    revision: Annotated[int, Field(strict=True, ge=1)]
    final_turn_id: TurnId
