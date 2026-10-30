import pytest
from stt.model.engine import StepKind, WorkItem
from stt.model.fake_engine import FakeEngine

FIRST = StepKind(first=True, final=False)
LATER = StepKind(first=False, final=False)
FINAL = StepKind(first=False, final=True)
KEY = (1, 7)


def _item(kind: StepKind, audio: bytes = b"\0\0") -> WorkItem:
    return WorkItem(KEY, kind, audio, 8000)


def test_returns_the_whole_utterance_text_and_resets_on_final():
    engine = FakeEngine()
    engine.open(KEY)
    assert engine.step(FIRST, [_item(FIRST)])[0].text == "s7c0"
    assert engine.step(LATER, [_item(LATER)])[0].text == "s7c0 s7c1"
    assert engine.step(FINAL, [_item(FINAL)])[0].text == "s7c0 s7c1 s7c2"
    # A fresh utterance starts again from c0.
    assert engine.step(FIRST, [_item(FIRST)])[0].text == "s7c0"


def test_a_final_with_no_audio_adds_no_token():
    engine = FakeEngine()
    engine.open(KEY)
    engine.step(FIRST, [_item(FIRST)])
    assert engine.step(FINAL, [_item(FINAL, b"")])[0].text == "s7c0"


def test_refuses_what_the_real_model_cannot_do():
    engine = FakeEngine()
    engine.open(KEY)
    with pytest.raises(ValueError):  # step kinds can't share a batch
        engine.step(FIRST, [_item(FIRST), _item(LATER)])
    with pytest.raises(KeyError):  # a stream must be opened first
        engine.step(FIRST, [WorkItem((9, 9), FIRST, b"\0\0", 8000)])


def test_release_forgets_the_stream():
    engine = FakeEngine()
    engine.open(KEY)
    engine.release(KEY)
    assert engine.open_streams == 0
    engine.release(KEY)  # releasing twice is harmless
