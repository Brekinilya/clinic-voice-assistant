"""Единая схема backend; версия 2 добавляет разговоры и квитанции команд."""

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
SCHEMA_VERSION = 2
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
