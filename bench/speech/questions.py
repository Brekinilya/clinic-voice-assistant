"""Вопросительная интонация Silero v5_5_ru: варианты разметки для прослушивания.

В общих вопросах («Верно?», «Вы придёте?») модели нужно слово с интонационным центром:
его выделяют звёздочками, сила — intensity. Результат — data/bench/questions.html.
"""

from html import escape

import numpy as np
from audio import write
from engines import Silero

from bench import OUT

CONFIRM = "Записываю: терапевт Кузнецова, пятница, второе октября, пятнадцать пятнадцать."
VARIANTS = [
    ("Как сейчас", f"{CONFIRM} Верно?", {}),
    ("Выделено «верно»", f"{CONFIRM} *Верно*?", {}),
    ("Выделено, слабее", f"{CONFIRM} *Верно*?", {"intensity": 2}),
    ("«Всё верно?»", f"{CONFIRM} Всё *верно*?", {}),
    ("Вопрос отдельно", "*Верно*?", {}),
    ("Вы придёте — как сейчас", "Вы придёте?", {}),
    ("Вы придёте — выделено", "Вы *придёте*?", {}),
    ("Я говорю с… — выделено имя", "Я говорю с *Ольгой Викторовной*?", {}),
    ("Соединить — выделено", "*Соединить* с сотрудником?", {}),
    ("Что удобнее (вопросительное слово)", "Что удобнее?", {}),
    ("Перенести или отменить (альтернатива)", "Перенести или отменить?", {}),
]
SPEAKERS = ["xenia", "baya", "kseniya"]


def main() -> None:
    rows = []
    engines = {s: Silero("v5_5_ru", s) for s in SPEAKERS}
    for n, (title, text, extra) in enumerate(VARIANTS):
        cells = []
        for speaker, engine in engines.items():
            try:
                audio = engine.model.apply_tts(
                    text=text,
                    speaker=speaker,
                    sample_rate=24000,
                    put_accent=True,
                    put_yo=True,
                    **extra,
                )
            except Exception as exc:  # модель отказывается синтезировать некоторые варианты
                cells.append(f"<td>не синтезируется: {escape(str(exc))}</td>")
                continue
            path = OUT / "questions" / f"{n:02d}_{speaker}.wav"
            write(path, np.asarray(audio, dtype=np.float32), 24000)
            src = f"questions/{path.name}"
            cells.append(f'<td><audio controls preload="none" src="{src}"></audio></td>')
        note = f" <small>{escape(str(extra))}</small>" if extra else ""
        rows.append(
            f"<tr><td>{escape(title)}</td><td>{escape(text)}{note}</td>{''.join(cells)}</tr>"
        )
    head = "".join(f"<th>{s}</th>" for s in SPEAKERS)
    page = (
        '<!doctype html><meta charset="utf-8"><title>Вопросы: интонация</title>'
        "<style>body{font:14px sans-serif;margin:16px}td,th{border:1px solid #ccc;padding:6px;"
        "vertical-align:top}table{border-collapse:collapse}audio{width:170px}</style>"
        "<h1>Silero v5_5_ru: вопросительная интонация</h1>"
        f"<table><tr><th>Вариант</th><th>Текст</th>{head}</tr>{''.join(rows)}</table>"
    )
    (OUT / "questions.html").write_text(page, encoding="utf-8")
    print(OUT / "questions.html")


if __name__ == "__main__":
    main()
