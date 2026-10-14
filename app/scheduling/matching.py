"""Точное ФИО и явные синонимы профиля; похожие имена не угадываем."""

import re

from app.mis.models import Doctor, name_key
from app.scheduling.models import ScheduleImportError, WorkbookDoctor

ALIASES = {
    "лор (отоларинголог)": "лор",
    "отоларинголог": "лор",
    "оториноларинголог": "лор",
    "детский отоларинголог": "детский лор",
    "детский оториноларинголог": "детский лор",
}


def profile_key(value: str) -> str:
    key = re.sub(r"\s*-\s*", "-", name_key(value).replace("–", "-").replace("—", "-"))
    return ALIASES.get(key, key)


def match_doctors(
    workbook: tuple[WorkbookDoctor, ...], catalog: tuple[Doctor, ...]
) -> dict[str, Doctor]:
    result = {}
    seen_names, used_ids = set(), set()
    for source in workbook:
        key = name_key(source.full_name)
        if key in seen_names or source.excel_id in result:
            raise ScheduleImportError("DUPLICATE_WORKBOOK_DOCTOR")
        seen_names.add(key)
        candidates = [d for d in catalog if name_key(d.full_name) == key]
        if len(candidates) != 1:
            raise ScheduleImportError("DOCTOR_NOT_UNIQUELY_MATCHED")
        doctor = candidates[0]
        if profile_key(source.specialty) != profile_key(doctor.specialty):
            raise ScheduleImportError("DOCTOR_PROFILE_MISMATCH")
        if doctor.id in used_ids:
            raise ScheduleImportError("DUPLICATE_MIS_DOCTOR")
        used_ids.add(doctor.id)
        result[source.excel_id] = doctor
    return result
