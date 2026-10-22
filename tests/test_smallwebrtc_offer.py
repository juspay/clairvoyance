"""SmallWebRTC offer contract and the NAT'd-server SDP answer repair."""


from app.api.routers.breeze_buddy.smallwebrtc import handlers
from app.api.routers.breeze_buddy.smallwebrtc.sdp import (
    ensure_host_candidate,
    media_port,
)
from app.schemas.breeze_buddy.smallwebrtc import SmallWebRTCOfferRequest

ANSWER_NO_CANDIDATE = (
    "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
    "m=audio 51234 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\n"
    "a=rtpmap:111 opus/48000/2\r\na=fingerprint:sha-256 AB:CD\r\n"
    "a=ice-ufrag:x9Ab\r\na=ice-pwd:secret\r\na=setup:active\r\n"
)


def test_offer_accepts_lead_and_client_type_top_level_or_nested():
    top = SmallWebRTCOfferRequest(
        sdp="v=0", type="offer", lead_id="L1", client_type="esp32"
    )
    assert top.resolved_lead_id() == "L1" and top.resolved_client_type() == "esp32"
    nested = SmallWebRTCOfferRequest(
        sdp="v=0", type="offer", request_data={"lead_id": "L2", "client_type": "esp32"}
    )
    assert (
        nested.resolved_lead_id() == "L2" and nested.resolved_client_type() == "esp32"
    )
    assert SmallWebRTCOfferRequest(sdp="v=0", type="offer").resolved_lead_id() is None


def test_answer_without_candidates_gets_the_public_host_and_media_port():
    assert media_port(ANSWER_NO_CANDIDATE) == 51234
    repaired = ensure_host_candidate(ANSWER_NO_CANDIDATE, "34.93.169.100")
    assert "a=candidate:1 1 udp 2130706431 34.93.169.100 51234 typ host\r\n" in repaired
    assert repaired.endswith("a=end-of-candidates\r\n")
    assert repaired.startswith(ANSWER_NO_CANDIDATE)


def test_answer_with_a_candidate_or_a_hostname_is_left_alone():
    with_candidate = (
        ANSWER_NO_CANDIDATE
        + "a=candidate:1 1 udp 2130706431 10.160.0.4 51234 typ host\r\n"
    )
    assert ensure_host_candidate(with_candidate, "34.93.169.100") == with_candidate
    assert (
        ensure_host_candidate(ANSWER_NO_CANDIDATE, "vm.example.com")
        == ANSWER_NO_CANDIDATE
    )
    assert ensure_host_candidate(ANSWER_NO_CANDIDATE, None) == ANSWER_NO_CANDIDATE
    assert ensure_host_candidate("v=0\r\n", "34.93.169.100") == "v=0\r\n"  # no m=audio


def test_esp32_host_comes_from_the_dialled_host_unless_overridden(monkeypatch):
    monkeypatch.setattr(handlers, "BB_WEBRTC_ESP32_HOST", "")
    assert (
        handlers._resolve_esp32_host("esp32", "34.93.169.100:8000") == "34.93.169.100"
    )
    assert handlers._resolve_esp32_host("browser", "34.93.169.100:8000") is None
    assert handlers._resolve_esp32_host("esp32", "") is None
    assert handlers._resolve_esp32_host("esp32", "bad host!") is None
    monkeypatch.setattr(handlers, "BB_WEBRTC_ESP32_HOST", "10.0.0.9")
    assert handlers._resolve_esp32_host("esp32", "34.93.169.100:8000") == "10.0.0.9"
