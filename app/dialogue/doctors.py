"""Имена врачей для понимания и озвучивания без обращения к МИС.

Источники по порядку: каталог индекса расписания (снят с МИС при импорте) и последний
прочитанный справочник. Для записи этого мало: активность врача и занятость перед
записью читаются из МИС заново.
"""

from app.mis.budget import immediate
from app.mis.models import Doctor
from app.mis.ports import MedicalSystem, MisError


class DoctorNames:
    def __init__(self, mis: MedicalSystem, schedule: object) -> None:
        self.mis = mis
        self.schedule = schedule

    def local(self) -> list[Doctor]:
        catalog = getattr(self.schedule, "doctors", None)
        if catalog:
            return list(catalog)
        return self.mis.known_doctors() or []

    async def understanding(self) -> list[Doctor]:
        """Для разбора реплики: отказ или лимит МИС не мешают понять «да» или новую дату."""
        local = self.local()
        if local:
            return local
        try:
            # Ждать окна ради имён нельзя: понимание не зависит от справочника.
            with immediate():
                return await self.mis.doctors()
        except MisError:
            return []

    async def speaking(self) -> list[Doctor]:
        """Для фразы с фамилией врача: локально, а без каталога — из справочника МИС."""
        return self.local() or await self.mis.doctors()
