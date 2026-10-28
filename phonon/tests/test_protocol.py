import pytest
from stt.frontend.model_link import coalesce
from stt.protocol import deepgram


def test_connect_params_pipecat_sends_are_accepted():
    # A Deepgram-style connect URL with extra options; the extras are ignored.
    config = deepgram.parse_query(
        {
            "encoding": "linear16",
            "sample_rate": "8000",
            "channels": "1",
            "multichannel": "false",
            "model": "nova-3-general",
            "language": "en",
            "interim_results": "true",
            "punctuate": "true",
            "call_id": "C-1.a:b_c",
        }
    )
    assert config == deepgram.StreamConfig(8000, "C-1.a:b_c")


@pytest.mark.parametrize(
    "params",
    [
        {},  # no sample_rate
        {"sample_rate": "7000"},
        {"sample_rate": "8000.0"},
        {"sample_rate": "8000", "encoding": "mulaw"},
        {"sample_rate": "8000", "channels": "2"},
        {"sample_rate": "8000", "multichannel": "true"},
        {"sample_rate": "8000", "call_id": "bad\nline"},
        {"sample_rate": "8000", "call_id": "x" * 200},
    ],
)
def test_bad_connect_params_are_rejected(params):
    with pytest.raises(deepgram.ProtocolError):
        deepgram.parse_query(params)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"type":"Nope"}',
        '{"type":"Finalize","seq":true}',  # strict ints, nothing coerced
        '{"type":"Finalize","seq":"1"}',
        '{"type":"Finalize","seq":-1}',
    ],
)
def test_bad_client_messages_are_rejected(text):
    with pytest.raises(deepgram.ProtocolError):
        deepgram.parse_client(text)


def test_client_messages():
    assert isinstance(deepgram.parse_client('{"type":"KeepAlive"}'), deepgram.KeepAlive)
    assert isinstance(
        deepgram.parse_client('{"type":"CloseStream"}'), deepgram.CloseStream
    )
    msg = deepgram.parse_client('{"type":"Finalize","seq":3}')
    assert isinstance(msg, deepgram.Finalize) and msg.seq == 3


def test_close_reason_fits_a_websocket_close_frame():
    assert len(deepgram.close_reason("x", "é" * 200).encode()) <= 123


def test_coalesce_merges_audio_per_stream_across_other_streams():
    events = [("audio", 1, b"a"), ("audio", 2, b"x"), ("audio", 1, b"b")]
    assert coalesce(events) == [("audio", 1, b"ab"), ("audio", 2, b"x")]


def test_coalesce_never_merges_across_a_finalize():
    events = [("audio", 1, b"a"), ("finalize", 1, None), ("audio", 1, b"b")]
    assert coalesce(events) == events


def test_coalesce_keeps_close_last():
    events = [
        ("audio", 1, b"a"),
        ("close", 1, None),
        ("audio", 2, b"z"),
        ("audio", 2, b"y"),
    ]
    assert coalesce(events) == [
        ("audio", 1, b"a"),
        ("close", 1, None),
        ("audio", 2, b"zy"),
    ]
