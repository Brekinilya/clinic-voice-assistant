"""Фоновый исполнитель только читает МИС; согласие пациента он не продлевает."""

import asyncio
from datetime import timedelta

from app.booking.models import ActionState, matches_visit
from app.cancellation.models import CancelState, same_visit
from app.mis.budget import Priority, demand
from app.mis.models import VisitStatus
from app.mis.ports import BudgetExhausted, MisError
from app.recovery.coordinator import outcome_for
from app.recovery.models import Outcome, OwnershipLost
from app.recovery.ownership import Scope, current
from app.rescheduling.models import MoveState, matches_new


class RecoveryWorker:
    def __init__(
        self, store, mis, bookings, cancellations, moves, clock, *, wait_s=8, timeout_s=25
    ):
        self.store, self.mis, self.clock = store, mis, clock
        self.bookings, self.cancellations, self.moves = bookings, cancellations, moves
        self.wait_s, self.timeout_s = wait_s, timeout_s

    async def run_once(self):
        await self.store.seed(self.clock.now())
        lease = await self.store.claim_next(self.clock.now())
        if lease is None:
            return None
        token = current.set(Scope(self.store, lease, self.clock))
        try:
            with demand(
                Priority.BACKGROUND if lease.watching else Priority.RECONCILE,
                until=self.clock.now() + timedelta(seconds=self.wait_s),
                owner=f"recovery-{lease.kind}-{lease.action_id}",
            ):
                try:
                    outcome = await asyncio.wait_for(self._resolve(lease), self.timeout_s)
                except BudgetExhausted as error:
                    outcome = Outcome(
                        state="pending",
                        reason="RATE_LIMIT",
                        delay_s=min(86400, max(1, error.retry_after or 5)),
                    )
                except MisError as error:
                    outcome = (
                        Outcome(state="manual", reason="VISIT_NOT_FOUND")
                        if error.code == "NOT_FOUND"
                        else Outcome(state="pending", reason="MIS_UNAVAILABLE", delay_s=30)
                    )
                except TimeoutError:
                    outcome = Outcome(state="pending", reason="READ_TIMEOUT", delay_s=30)
                except OwnershipLost:
                    raise
                except Exception:
                    # Технический сбой виден в ручной очереди без текста исключения/ПДн.
                    outcome = Outcome(state="manual", reason="RECOVERY_FAILED")
            return await self.store.finish(lease, self.clock.now, outcome)
        except OwnershipLost:
            return None
        finally:
            current.reset(token)

    async def _resolve(self, lease):
        if (
            lease.uncertain_since is not None
            and self.clock.now() >= lease.uncertain_since + timedelta(seconds=600)
        ):
            return Outcome(state="manual", reason="RECONCILIATION_EXHAUSTED")
        if lease.kind == "booking":
            return await self._booking(lease.action_id)
        if lease.kind == "cancellation":
            return await self._cancellation(lease.action_id)
        return await self._move(lease.action_id)

    async def _booking(self, action_id):
        action = await self.bookings.get(action_id)
        if action.state == ActionState.BOOKED:
            if action.slot.scheduled_at <= self.clock.now():
                await self.bookings.archive_external(action_id, action.visit_id)
                return Outcome(state="resolved", reason="LOCAL_PROJECTION_PAST")
            visit = await self.mis.visit(action.visit_id)
            if visit.client_id != action.client_id:
                return Outcome(state="manual", reason="VISIT_OWNER_CHANGED")
            if not matches_visit(action, visit):
                await self.bookings.archive_external(action_id, action.visit_id)
                return Outcome(state="resolved", reason="EXTERNAL_VISIT_CHANGED")
            return Outcome(state="watching", delay_s=300)
        if action.state not in {ActionState.COMMITTING, ActionState.RECONCILING}:
            return outcome_for("booking", action)
        found = await self.mis.visits(
            client_id=action.client_id,
            doctor_id=action.slot.doctor_id,
            date_from=action.slot.scheduled_at.date(),
            date_to=action.slot.scheduled_at.date(),
            fresh=True,
        )
        matches = [v for v in found if matches_visit(action, v)]
        if len(matches) == 1:
            await self.bookings.finish(action_id, ActionState.BOOKED, matches[0].id)
            return Outcome(state="watching", reason="BOOKING_CONFIRMED", delay_s=300)
        if matches:
            return Outcome(state="manual", reason="AMBIGUOUS_VISITS")
        # Ноль совпадений не доказывает, что прежний POST не выполняется до сих пор.
        return Outcome(
            state="pending", reason="NO_CONFIRMED_RESULT", delay_s=30, count_attempt=True
        )

    async def _cancellation(self, action_id):
        action = await self.cancellations.get(action_id)
        if action.state not in {CancelState.COMMITTING, CancelState.RECONCILING}:
            return outcome_for("cancellation", action)
        visit = await self.mis.visit(action.before.id)
        if not same_visit(action.before, visit):
            return Outcome(state="manual", reason="VISIT_CHANGED")
        if visit.status == VisitStatus.CANCELLED:
            await self.cancellations.finish(action_id, CancelState.CANCELLED, self.clock.now())
            return Outcome(state="resolved", reason="CANCELLATION_CONFIRMED")
        return Outcome(
            state="pending", reason="NO_CONFIRMED_RESULT", delay_s=30, count_attempt=True
        )

    async def _move(self, action_id):
        action = await self.moves.get(action_id)
        if action.state in {MoveState.CREATING, MoveState.CREATE_UNKNOWN}:
            found = await self.mis.visits(
                client_id=action.before.client_id,
                doctor_id=action.slot.doctor_id,
                date_from=action.slot.scheduled_at.date(),
                date_to=action.slot.scheduled_at.date(),
                fresh=True,
            )
            matches = [v for v in found if matches_new(action, v)]
            if len(matches) == 1:
                await self.moves.created(action_id, matches[0])
                return Outcome(state="manual", reason="FRESH_CONFIRMATION_REQUIRED")
            if matches:
                return Outcome(state="manual", reason="AMBIGUOUS_VISITS")
            return Outcome(
                state="pending", reason="NO_CONFIRMED_RESULT", delay_s=30, count_attempt=True
            )
        if action.state not in {
            MoveState.NEW_CREATED,
            MoveState.CANCELLING,
            MoveState.CANCEL_UNKNOWN,
            MoveState.MANUAL,
        }:
            return outcome_for("move", action)
        if action.new_visit is None:
            return Outcome(state="manual", reason="NEW_VISIT_NOT_CONFIRMED")
        old = await self.mis.visit(action.before.id)
        new = await self.mis.visit(action.new_visit.id)
        if (
            not same_visit(action.before, old)
            or not same_visit(action.new_visit, new)
            or not matches_new(action, new)
        ):
            return Outcome(state="manual", reason="VISIT_CHANGED")
        if old.status == VisitStatus.CANCELLED:
            await self.moves.complete(action_id, old, new, self.clock.now())
            return Outcome(state="resolved", reason="MOVE_CONFIRMED")
        if action.state == MoveState.NEW_CREATED:
            return Outcome(state="manual", reason="FRESH_CONFIRMATION_REQUIRED")
        return Outcome(
            state="pending", reason="NO_CONFIRMED_RESULT", delay_s=30, count_attempt=True
        )
