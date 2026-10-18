"""Стыковка настоящего диалога с заданиями и журналом отметок С-10."""

from app.dialogue.outbound import ReminderBinding
from app.recovery.models import OwnershipLost


class ReminderActionAdapter:
    def __init__(self, executor, marks, store, clock):
        if not marks.require_dialogue_guard:
            raise ValueError("для настоящего диалога нужен guard журнала отметок")
        self.executor, self.marks, self.store, self.clock = executor, marks, store, clock

    async def check_attempt(self, binding: ReminderBinding) -> None:
        if not await self.executor.guard(binding.attempt_id, binding.fence):
            raise OwnershipLost()
        attempt = await self.store.attempt(binding.attempt_id)
        job = await self.store.job(attempt.job_id)
        if (
            job.client_id != binding.client_id
            or job.visit_id != binding.visit.visit_id
            or job.doctor_id != binding.visit.doctor_id
            or job.scheduled_at != binding.visit.scheduled_at
        ):
            raise OwnershipLost()

    async def stop_calls(self, binding):
        await self.check_attempt(binding)
        await self.store.opt_out(binding.client_id, self.clock.now())

    async def confirm(self, binding, approval):
        await self.check_attempt(binding)
        return await self.marks.confirm(binding, approval)

    async def opt_out(self, binding, approval):
        await self.check_attempt(binding)
        return await self.marks.opt_out(binding, approval)
