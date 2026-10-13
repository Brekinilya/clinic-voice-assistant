"""Страница для прослушивания синтеза: data/bench/listen.html, реплики с фамилиями, датами и числами."""

from html import escape

from bench import OUT, PHRASES
from engines import TTS

PICK = ["a03", "a04", "a07", "a09", "a10", "a15", "a16"]


def main() -> None:
    texts = {i["id"]: i["text"] for i in PHRASES["assistant"]}
    rows = []
    for phrase_id in PICK:
        cells = "".join(
            f'<td><audio controls preload="none" src="tts/{name}/{phrase_id}.wav"></audio></td>' for name in TTS
        )
        rows.append(f"<tr><td>{escape(texts[phrase_id])}</td>{cells}</tr>")
    head = "".join(f"<th>{escape(name)}</th>" for name in TTS)
    page = (
        '<!doctype html><meta charset="utf-8"><title>Синтез: на слух</title>'
        "<style>body{font:14px sans-serif;margin:16px}td,th{border:1px solid #ccc;padding:6px;vertical-align:top}"
        "table{border-collapse:collapse}audio{width:180px}</style>"
        "<h1>Синтез: ударения, даты, числа</h1>"
        f"<table><tr><th>Реплика</th>{head}</tr>{''.join(rows)}</table>"
    )
    (OUT / "listen.html").write_text(page, encoding="utf-8")
    print(OUT / "listen.html")


if __name__ == "__main__":
    main()
