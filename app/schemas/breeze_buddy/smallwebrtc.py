"""Request/response schemas for the SmallWebRTC transport endpoints.

Wire-format notes:
- The JS SmallWebRTCTransport nests custom connect params under ``requestData``
  (camelCase); the ESP32/pipecat-esp32 client sends ``lead_id``/``client_type``
  top-level. Both spellings/placements are accepted.
- ICE candidate fields arrive snake_case from pipecat-style clients but
  camelCase from raw browser RTCIceCandidate JSON; aliases accept both.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class SmallWebRTCOfferRequest(BaseModel):
    """SDP offer posted by a SmallWebRTC client (browser test or ESP32 device)."""

    # Tolerate future transport-added fields instead of 422-ing the handshake.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sdp: str
    type: Literal["offer", "answer"]
    pc_id: Optional[str] = Field(
        default=None, description="Set on renegotiation/reconnect of a live PC"
    )
    restart_pc: bool = False
    lead_id: Optional[str] = Field(
        default=None, description="Top-level placement used by device clients"
    )
    client_type: Optional[str] = Field(
        default=None, description='e.g. "esp32" — enables SDP munging'
    )
    request_data: Optional[Dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("request_data", "requestData"),
        description="JS transport nests custom connect params here",
    )

    def resolved_lead_id(self) -> Optional[str]:
        """lead_id, top-level or nested under request_data."""
        if self.lead_id:
            return str(self.lead_id)
        if self.request_data and self.request_data.get("lead_id"):
            return str(self.request_data["lead_id"])
        return None

    def resolved_client_type(self) -> Optional[str]:
        """client_type, top-level or nested under request_data."""
        if self.client_type:
            return str(self.client_type)
        if self.request_data and self.request_data.get("client_type"):
            return str(self.request_data["client_type"])
        return None


class SmallWebRTCAnswerResponse(BaseModel):
    """SDP answer returned to the client (mirrors pipecat's get_answer())."""

    sdp: str
    type: str
    pc_id: str


class SmallWebRTCIceCandidate(BaseModel):
    """One trickled ICE candidate (snake_case or browser camelCase)."""

    model_config = ConfigDict(populate_by_name=True)

    candidate: str
    sdp_mid: str = Field(validation_alias=AliasChoices("sdp_mid", "sdpMid"))
    sdp_mline_index: int = Field(
        validation_alias=AliasChoices("sdp_mline_index", "sdpMLineIndex")
    )


class SmallWebRTCPatchRequestBody(BaseModel):
    """ICE trickle / renegotiation PATCH for an existing peer connection."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    pc_id: str
    candidates: List[SmallWebRTCIceCandidate] = Field(default_factory=list)


class SmallWebRTCPatchResponse(BaseModel):
    """Acknowledgement for a processed PATCH."""

    status: Literal["ok"] = "ok"
