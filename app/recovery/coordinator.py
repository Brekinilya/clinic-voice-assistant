"""Согласовать обычную операцию с последующим фоновым чтением её результата."""

from app.ports import Clock
from app.recovery.models import Outcome, OwnershipLost
from app.recovery.ownership import Scope, current
from app.recovery.ports import RecoveryStore


def outcome_for(kind, action):
    state = str(action.state)
    if kind == "booking" and state == "booked":
        return Outcome(state="watching", delay_s=300)
    if kind == "move" and state in {"new_created", "manual"}:
        return Outcome(
            state="manual",
            reason="FRESH_CONFIRMATION_REQUIRED"
            if state == "new_created"
            else "MOVE_REQUIRES_REVIEW",
        )
    if state in {
        "committing",
        "reconciling",
        "creating",
        "create_unknown",
        "cancelling",
        "cancel_unknown",
    }:
        return Outcome(state="pending", reason="OUTCOME_UNKNOWN")
    return Outcome(state="resolved")


class OperationCoordinator:
    def __init__(self, store: RecoveryStore, clock: Clock, *, lease_s=120):
        if not 10 <= lease_s <= 300:
            raise ValueError("аренда операции должна быть от 10 до 300 секунд")
        self.store, self.clock, self.lease_s = store, clock, lease_s

    async def run(self, kind, action_id, operation):
        scope = current.get()
        if scope is not None and (scope.lease.kind, scope.lease.action_id) == (kind, action_id):
            return await operation()
        lease = await self.store.acquire(kind, action_id, self.clock.now(), self.lease_s)
        if lease is None:
            raise OwnershipLost()
        token = current.set(Scope(self.store, lease, self.clock))
        try:
            try:
                result = await operation()
            except BaseException:
                try:
                    await self.store.finish(
                        lease,
                        self.clock.now,
                        Outcome(state="pending", reason="INTERRUPTED", delay_s=self.lease_s),
                    )
                except OwnershipLost:
                    pass
                raise
            await self.store.finish(lease, self.clock.now, outcome_for(kind, result))
            return result
        finally:
            current.reset(token)


async def coordinate(coordinator, kind, action_id, operation):
    return (
        await operation()
        if coordinator is None
        else await coordinator.run(kind, action_id, operation)
    )
