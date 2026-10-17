from app.voice.endpoint import FRAME_BYTES, FRAME_MS, Endpointer, EndpointSettings

SPEECH = b"\x10\x00" * (FRAME_BYTES // 2)
QUIET = b"\x00" * FRAME_BYTES


def run(endpointer, probabilities):
    events = []
    for p in probabilities:
        event = endpointer.push(SPEECH if p >= 0.5 else QUIET, p)
        if event:
            events.append(event)
    return events


def frames(ms: int) -> int:
    return ms // FRAME_MS


def test_phrase_starts_after_min_speech_and_ends_after_silence():
    e = Endpointer(EndpointSettings(min_speech_ms=192, end_silence_ms=640))
    assert run(e, [0.9] * (frames(192) - 1)) == []
    assert run(e, [0.9]) == ["start"]
    assert e.in_phrase
    assert run(e, [0.9] * 10 + [0.1] * (frames(640) - 1)) == []
    assert run(e, [0.1]) == ["end"]
    assert not e.in_phrase


def test_short_blip_is_not_a_phrase():
    e = Endpointer(EndpointSettings(min_speech_ms=192))
    assert run(e, [0.9, 0.9, 0.1, 0.1, 0.9, 0.1]) == []
    assert not e.in_phrase


def test_phrase_keeps_preroll_and_trims_long_tail():
    s = EndpointSettings(min_speech_ms=192, end_silence_ms=640, preroll_ms=320, tail_ms=192)
    e = Endpointer(s)
    run(e, [0.1] * 30 + [0.9] * 20 + [0.1] * frames(640))
    audio = e.take()
    assert audio.count(SPEECH) == 20
    assert len(audio) // FRAME_BYTES == frames(320) + 20 + frames(192)
    assert e.take() == b""


def test_pause_between_thresholds_does_not_end_phrase():
    e = Endpointer(EndpointSettings(end_silence_ms=320))
    run(e, [0.9] * 10)
    assert run(e, [0.4] * 50) == []
    assert e.in_phrase


def test_dictation_waits_longer():
    e = Endpointer(EndpointSettings(end_silence_ms=640))
    e.end_silence_ms = 1200
    run(e, [0.9] * 10)
    assert run(e, [0.1] * frames(640)) == []
    # 1200 мс — это 38 кадров по 32 мс
    assert run(e, [0.1] * 17) == []
    assert run(e, [0.1]) == ["end"]


def test_endless_phrase_is_cut():
    e = Endpointer(EndpointSettings(max_phrase_ms=1000, min_speech_ms=192))
    assert run(e, [0.9] * 37) == ["start", "end"]
    # монолог продолжается — это уже следующая фраза
    assert run(e, [0.9]) == ["start"]


def test_partial_audio_grows_with_phrase():
    e = Endpointer()
    run(e, [0.9] * 20)
    assert e.phrase_ms == len(e.audio()) // FRAME_BYTES * FRAME_MS > 0


def test_uncertain_vad_before_speech_does_not_accumulate_unbounded_audio():
    e = Endpointer()
    assert run(e, [0.9] + [0.4] * 3000) == []
    assert not e.in_phrase
    assert len(e._pending) * FRAME_MS <= 2 * max(e.settings.preroll_ms, e.settings.min_speech_ms)
    assert run(e, [0.9] * frames(192)) == ["start"]
    assert len(e.audio()) <= FRAME_BYTES * (frames(640) + frames(192))
