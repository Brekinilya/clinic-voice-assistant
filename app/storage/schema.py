"""Единая схема backend; версия 4 добавляет очередь и демонстрационный журнал."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
SCHEMA_VERSION = 4
schema_versions = Table("backend_schema", metadata, Column("version", Integer, primary_key=True))
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
    CheckConstraint("kind IN ('cancellation_sms','slot_released')"),
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
