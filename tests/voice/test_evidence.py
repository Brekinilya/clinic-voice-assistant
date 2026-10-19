"""Голосовые доказательства T07: R-17, D-13 и D-15 через настоящую CallSession и диалог."""

import json

from app.voice.settings import VoiceSettings
from bench.voice import evidence

FAST = VoiceSettings(_env_file=None, silence_prompt_s=1.0, wait_phrase_s=0.1, goodbye_hangup_s=0.05)


async def test_silence_wait_prompt_and_recording_are_proven_by_the_voice_session():
    results = await evidence.collect(FAST, 0.05)
    by_id = {r.label: r for r in results}
    assert {r.scenario_id for r in results} == {"R-17", "D-13", "D-15"}
    assert all(r.verdict == "ok" for r in results), [r.detail for r in results]
    silence = by_id["R-17"]
    assert silence.said[-2:] == ["silence.first", "silence.final"]
    assert (silence.end, silence.result_code) == ("silence", "DROPPED")
    for result in results:
        if result.scenario_id == "D-15":
            assert "recording.off" in result.said and result.files_written == 0
            assert result.end is None


async def test_wait_prompt_is_not_credited_when_the_stand_answers_in_time():
    # Стенд отвечает быстрее порога фразы ожидания: доказательства нет, итог fail
    slow_threshold = FAST.model_copy(update={"wait_phrase_s": 5.0})
    [first, *_] = await evidence.collect(slow_threshold, 0.05, ["D-13"])
    assert first.verdict == "fail"
    assert "wait.schedule" not in first.said


def test_cli_writes_the_report_for_the_scenario_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "VoiceSettings", lambda **_: FAST)
    real = evidence.collect
    monkeypatch.setattr(
        evidence, "collect", lambda settings, scale, only: real(settings, 0.05, only)
    )
    assert evidence.main(["--output-dir", str(tmp_path), "--scenario", "R-17"]) == 0
    document = json.loads((tmp_path / "voice_evidence.json").read_text("utf-8"))
    [row] = document["results"]
    assert (row["scenario_id"], row["requirement"], row["verdict"]) == (
        "R-17",
        "event:silence",
        "ok",
    )
    assert document["engines"]["sip"] is None and document["source"]["commit"]
    assert "R-17" in (tmp_path / "voice_evidence.md").read_text("utf-8")
