"""Автономная реализация той же аренды и монотонного fencing для тестов."""

import asyncio
from contextlib import asynccontextmanager

from app.recovery.models import RecoveryRecord, claim, owned, settle


class MemoryRecoveryStore:
    def __init__(self, ledger=None):
        self.ledger = ledger
        self.records = {}
        self.lock = asyncio.Lock()

    async def seed(self, now, limit=100):
        if self.ledger is None:
            return 0
        count = 0
        async with self.lock:
            for kind, actions, states in (
                ("booking", self.ledger.bookings, {"committing", "reconciling", "booked"}),
                ("cancellation", self.ledger.cancellations, {"committing", "reconciling"}),
                (
                    "move",
                    self.ledger.moves,
                    {
                        "creating",
                        "create_unknown",
                        "new_created",
                        "cancelling",
                        "cancel_unknown",
                        "manual",
                    },
                ),
            ):
                for action in actions.values():
                    key = kind, action.action_id
                    if action.state in states and key not in self.records:
                        self.records[key] = RecoveryRecord(
                            kind=kind,
                            action_id=action.action_id,
                            due_at=now,
                            state="watching"
                            if kind == "booking" and action.state == "booked"
                            else "pending",
                        )
                        count += 1
                        if count >= limit:
                            return count
        return count

    async def acquire(self, kind, action_id, now, seconds=60):
        async with self.lock:
            key = kind, action_id
            record = self.records.get(key) or RecoveryRecord(
                kind=kind, action_id=action_id, due_at=now
            )
            result = claim(record, now, seconds)
            if result is None:
                return None
            self.records[key], lease = result
            return lease

    async def claim_next(self, now, seconds=60):
        async with self.lock:
            candidates = sorted(
                (
                    r
                    for r in self.records.values()
                    if r.state in {"pending", "watching", "running"} and r.due_at <= now
                ),
                key=lambda r: (r.due_at, r.kind, r.action_id),
            )
            for record in candidates:
                result = claim(record, now, seconds, read_only=True)
                if result is not None:
                    updated, lease = result
                    self.records[record.kind, record.action_id] = updated
                    return lease
        return None

    @asynccontextmanager
    async def fence(self, lease, now, connection=None):
        async with self.lock:
            owned(self.records[lease.kind, lease.action_id], lease, now)
            yield

    async def finish(self, lease, now, outcome):
        async with self.lock:
            key = lease.kind, lease.action_id
            record = settle(self.records[key], lease, now, outcome)
            self.records[key] = record
            return record

    async def list(self, limit=100, state=None):
        async with self.lock:
            return sorted(
                (r for r in self.records.values() if state is None or r.state == state),
                key=lambda r: (r.due_at, r.kind, r.action_id),
            )[:limit]
