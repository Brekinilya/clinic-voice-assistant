"""Единая схема backend; версия 8 добавляет зашифрованный журнал диагностики."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
SCHEMA_VERSION = 9
schema_versions = Table("backend_schema", metadata, Column("version", Integer, primary_key=True))
debug_events = Table(
    "backend_debug_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("call_id", String(128), nullable=False),
    Column("turn_id", Integer),
    Column("kind", String(64), nullable=False),
    Column("producer", String(12), nullable=False),
    Column("source", String(8), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", Text, nullable=False),
)
Index("backend_debug_call", debug_events.c.call_id, debug_events.c.id)
Index("backend_debug_expiry", debug_events.c.created_at)
recovery_operations = Table(
    "backend_recovery_operations",
    metadata,
    Column("kind", String(20), primary_key=True),
    Column("action_id", String(128), primary_key=True),
    Column("state", String(20), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("data", JSONB, nullable=False),
    CheckConstraint("kind IN ('booking','cancellation','move')"),
    CheckConstraint("state IN ('pending','running','watching','manual','resolved')"),
)
Index("backend_recovery_due", recovery_operations.c.state, recovery_operations.c.due_at)
actions = Table(
    "backend_booking_actions",
    metadata,
    Column("action_id", String(32), primary_key=True),
    Column("call_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("doctor_id", Integer, nullable=False),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("specialty", String(100), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(20), nullable=False),
    Column("confirmed_turn", Integer),
    Column("visit_id", Integer),
    CheckConstraint("revision > 0 AND client_id > 0 AND doctor_id > 0"),
    CheckConstraint(
        "state IN ('held','committing','reconciling','booked','archived','rejected','expired','cancelled')"
    ),
    CheckConstraint("(state IN ('booked','archived')) = (visit_id IS NOT NULL)"),
)
active = actions.c.state.in_(["held", "committing", "reconciling", "booked"])
Index(
    "backend_active_slot",
    actions.c.doctor_id,
    actions.c.scheduled_at,
    unique=True,
    postgresql_where=active,
)
Index(
    "backend_active_specialty",
    actions.c.client_id,
    actions.c.specialty,
    unique=True,
    postgresql_where=active,
)
Index("backend_call_revision", actions.c.call_id, actions.c.revision, unique=True)
budget_blocks = Table(
    "mis_budget_blocks",
    metadata,
    Column("scope", String(64), primary_key=True),
    Column("blocked_until", DateTime(timezone=True), nullable=False),
)
budget_requests = Table(
    "mis_budget_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("scope", String(64), nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    # Талон: место окна, закреплённое за ждущим звонком до hold_until.
    Column("owner", String(128)),
    Column("hold_until", DateTime(timezone=True)),
)
Index("mis_budget_window", budget_requests.c.scope, budget_requests.c.requested_at)
dialogue_sessions = Table(
    "backend_dialogue_sessions",
    metadata,
    Column("call_id", String(128), primary_key=True),
    Column("state", JSONB, nullable=False),
    Column("pending_turn", Integer),
    CheckConstraint("pending_turn IS NULL OR pending_turn > 0"),
)
dialogue_receipts = Table(
    "backend_dialogue_receipts",
    metadata,
    Column(
        "call_id", String(128), ForeignKey("backend_dialogue_sessions.call_id"), primary_key=True
    ),
    Column("turn_id", Integer, primary_key=True),
    Column("digest", String(64), nullable=False),
    Column("reply", JSONB),
    CheckConstraint("turn_id > 0"),
)
cancellations = Table(
    "backend_visit_cancellations",
    metadata,
    Column("action_id", String(32), primary_key=True),
    Column("call_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("visit_id", Integer, nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(20), nullable=False),
    Column("confirmed_turn", Integer),
    Column("reason", String(30)),
    Column("late", Integer, nullable=False, default=0),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint("revision > 0 AND visit_id > 0 AND client_id > 0 AND late IN (0,1)"),
    CheckConstraint(
        "state IN ('prepared','committing','reconciling','cancelled','rejected','aborted','expired')"
    ),
    CheckConstraint("(state = 'cancelled') = (completed_at IS NOT NULL)"),
)
Index(
    "backend_active_cancellation",
    cancellations.c.visit_id,
    unique=True,
    postgresql_where=cancellations.c.state.in_(["prepared", "committing", "reconciling"]),
)
Index("backend_cancel_revision", cancellations.c.call_id, cancellations.c.revision, unique=True)

outbox_jobs = Table(
    "backend_outbox_jobs",
    metadata,
    Column("event_id", String(32), primary_key=True),
    Column("source_id", String(32), ForeignKey(cancellations.c.action_id), nullable=False),
    Column("kind", String(30), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("state", String(20), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("lease_token", String(32)),
    Column("lease_until", DateTime(timezone=True)),
    Column("last_error", String(40)),
    # SQL NULL означает отсутствие квитанции; JSON null нарушил бы проверку состояния.
    Column("receipt", JSONB(none_as_null=True)),
    CheckConstraint(
        "kind IN ('cancellation_sms','slot_released','reschedule_sms')",
        name="backend_outbox_kind_allowed",
    ),
    CheckConstraint(
        "state IN ('pending','processing','retry','unknown','blocked','acknowledged','failed')"
    ),
    CheckConstraint("attempts >= 0 AND attempts <= 5"),
    CheckConstraint(
        "(state = 'processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)"
    ),
    CheckConstraint("(lease_token IS NULL) = (lease_until IS NULL)"),
    CheckConstraint("(state = 'acknowledged') = (receipt IS NOT NULL)"),
)
Index("backend_outbox_source_kind", outbox_jobs.c.source_id, outbox_jobs.c.kind, unique=True)
Index("backend_outbox_ready", outbox_jobs.c.state, outbox_jobs.c.available_at)
delivery_journal = Table(
    "backend_demo_delivery_journal",
    metadata,
    Column("event_id", String(32), ForeignKey(outbox_jobs.c.event_id), primary_key=True),
    Column("entry", JSONB, nullable=False),
)

moves = Table(
    "backend_reschedule_actions",
    metadata,
    Column("action_id", String(32), primary_key=True),
    Column("call_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("old_visit_id", Integer, nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("doctor_id", Integer, nullable=False),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("specialty", String(100), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(20), nullable=False),
    Column("new_visit_id", Integer),
    Column("cancellation_action_id", String(32), ForeignKey(cancellations.c.action_id)),
    Column("data", JSONB, nullable=False),
    CheckConstraint("revision > 0 AND old_visit_id > 0 AND client_id > 0 AND doctor_id > 0"),
    CheckConstraint(
        "state IN ('held','creating','create_unknown','new_created','cancelling','cancel_unknown','rescheduled','manual','rejected','aborted','expired')"
    ),
    CheckConstraint(
        "(state IN ('new_created','cancelling','cancel_unknown','rescheduled','manual')) = (new_visit_id IS NOT NULL)"
    ),
    CheckConstraint("(state = 'rescheduled') = (cancellation_action_id IS NOT NULL)"),
)
active_moves = moves.c.state.in_(
    ["held", "creating", "create_unknown", "new_created", "cancelling", "cancel_unknown", "manual"]
)
Index("backend_move_old_visit", moves.c.old_visit_id, unique=True, postgresql_where=active_moves)
Index(
    "backend_move_new_slot",
    moves.c.doctor_id,
    moves.c.scheduled_at,
    unique=True,
    postgresql_where=active_moves,
)
Index(
    "backend_move_specialty",
    moves.c.client_id,
    moves.c.specialty,
    unique=True,
    postgresql_where=active_moves,
)
Index("backend_move_revision", moves.c.call_id, moves.c.revision, unique=True)

# Напоминания С-10 (T11). Только технические ссылки: ни ФИО, ни текста разговора.
# Номер версии схемы назначается при сборке; таблицы создаёт общий migrate.
reminder_days = Table("reminder_days", metadata, Column("call_day", Date, primary_key=True))
reminder_jobs = Table(
    "reminder_jobs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("visit_id", Integer, nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("doctor_id", Integer, nullable=False),
    Column("scheduled_at", DateTime(timezone=True), nullable=False),
    Column("call_day", Date, nullable=False),
    Column("state", String(16), nullable=False),
    Column("next_at", DateTime(timezone=True), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("outcome", String(16)),
    Column("mis_status", String(16)),
    Column("reason", String(64)),
    Column("sms_pending", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    CheckConstraint(
        "visit_id > 0 AND client_id > 0 AND doctor_id > 0 AND attempts BETWEEN 0 AND 2"
    ),
    CheckConstraint("state IN ('planned','calling','waiting_retry','done','skipped','manual')"),
)
# Одно задание на визит и его время: перенесённый визит — новое задание
Index(
    "reminder_job_visit",
    reminder_jobs.c.visit_id,
    reminder_jobs.c.scheduled_at,
    reminder_jobs.c.doctor_id,
    unique=True,
)
Index("reminder_job_due", reminder_jobs.c.state, reminder_jobs.c.next_at)
reminder_attempts = Table(
    "reminder_attempts",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("job_id", String(32), ForeignKey(reminder_jobs.c.id), nullable=False),
    Column("number", Integer, nullable=False),
    Column("fence", Integer, nullable=False),
    Column("owner", String(128), nullable=False),
    Column("lease_until", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False),
    Column("channel_id", String(64), nullable=False),
    Column("result", String(16)),
    Column("mis_status", String(16)),
    Column("reason", String(64)),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("number BETWEEN 1 AND 2 AND fence > 0"),
    CheckConstraint("state IN ('claimed','dialing','answered','finished','unknown','abandoned')"),
)
# Несостоявшаяся попытка номер не занимает; у задания не больше одной активной попытки
Index(
    "reminder_attempt_number",
    reminder_attempts.c.job_id,
    reminder_attempts.c.number,
    unique=True,
    postgresql_where=reminder_attempts.c.state != "abandoned",
)
Index(
    "reminder_attempt_active",
    reminder_attempts.c.job_id,
    unique=True,
    postgresql_where=reminder_attempts.c.state.in_(["claimed", "dialing", "answered"]),
)
Index("reminder_attempt_lease", reminder_attempts.c.state, reminder_attempts.c.lease_until)
Index("reminder_attempt_fence", reminder_attempts.c.job_id, reminder_attempts.c.fence, unique=True)
Index("reminder_attempt_channel", reminder_attempts.c.channel_id, unique=True)
reminder_contacts = Table(
    "reminder_contacts",
    metadata,
    Column("client_id", Integer, primary_key=True),
    Column("endpoint", String(200), nullable=False),
    Column("opt_out_at", DateTime(timezone=True)),
    CheckConstraint("client_id > 0"),
)
reminder_confirmations = Table(
    "reminder_confirmations",
    metadata,
    Column("visit_id", Integer, primary_key=True),
    Column("doctor_id", Integer, primary_key=True),
    Column("scheduled_at", DateTime(timezone=True), primary_key=True),
    Column("confirmed_at", DateTime(timezone=True), nullable=False),
)
# Журнал отметок в комментарии клиента: только наша строка, без прежнего текста комментария
reminder_marks = Table(
    "reminder_marks",
    metadata,
    Column("id", String(160), primary_key=True),
    Column("attempt_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("client_id", Integer, nullable=False),
    Column("visit_id", Integer),
    Column("line", String(200), nullable=False),
    Column("state", String(16), nullable=False),
    Column("checks", Integer, nullable=False),
    Column("reason", String(64)),
    Column("next_check_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("kind IN ('confirm','opt_out')"),
    CheckConstraint("state IN ('prepared','sending','unknown','applied','manual')"),
)
Index("reminder_mark_check", reminder_marks.c.state, reminder_marks.c.next_check_at)
# Наши писатели комментария одного клиента не теряют правки друг друга
reminder_client_locks = Table(
    "reminder_client_locks",
    metadata,
    Column("client_id", Integer, primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("until", DateTime(timezone=True), nullable=False),
)
# Журнал заявок на соединение со специалистом (T08). Текст карточки не хранится:
# только маркер в описании и отпечаток — по ним сверяется неизвестный исход POST.
escalation_requests = Table(
    "escalation_requests",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("call_id", String(128), nullable=False),
    Column("route", String(8), nullable=False),
    Column("priority", String(2), nullable=False),
    Column("client_id", Integer),
    Column("technical", Boolean, nullable=False),
    Column("marker", String(32), nullable=False),
    Column("length", Integer, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("escalation_id", Integer),
    Column("checks", Integer, nullable=False),
    Column("reason", String(64)),
    Column("next_check_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("state IN ('prepared','sending','applied','unknown','manual')"),
    CheckConstraint("priority IN ('P1','P2','P3')"),
    CheckConstraint("(state = 'applied') = (escalation_id IS NOT NULL)"),
)
Index("escalation_check", escalation_requests.c.state, escalation_requests.c.next_check_at)
