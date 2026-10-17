"""Строгое сравнение известных проблем; это не список пройденных требований.

Любое новое несовпадение и любое улучшение требуют пересмотра записи. Ошибки
runner и нарушения безопасности в этот список включать нельзя.
"""

import json
from dataclasses import asdict

from tests.scenarios.scenario_plan import HERE


def load_known_issues():
    return json.loads((HERE / "known_issues.json").read_text(encoding="utf-8"))["dialogs"]


def checked_known_issue(result):
    expected = load_known_issues().get(result.label)
    if any(f.kind in {"critical", "error"} for f in result.findings):
        raise AssertionError("Нарушение безопасности или ошибка runner: " + result.label)
    if result.status == "pass":
        assert expected is None, (
            "Проблема исправлена: удалите устаревшее исключение " + result.label
        )
        return None
    assert expected is not None, "Новое расхождение: " + result.label
    assert result.status == expected["status"]
    assert [asdict(f) for f in result.findings] == expected["findings"], (
        "Изменились доказательства известной проблемы: " + result.label
    )
    return expected["reason"]
