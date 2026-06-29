from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.breeze_buddy.whatsapp import (
    MetaEmbeddedSignupCompleteRequest,
    WhatsAppRegisterPhoneRequest,
)


def test_complete_request_accepts_signup_payload() -> None:
    req = MetaEmbeddedSignupCompleteRequest(
        merchant_id="shop.example",
        code="exchange-code",
        signup_event={
            "type": "WA_EMBEDDED_SIGNUP",
            "event": "FINISH",
            "data": {
                "phone_number_id": "106540352242922",
                "waba_id": "524126980791429",
                "business_id": "2729063490586005",
            },
        },
    )

    assert req.signup_event.data.phone_number_id == "106540352242922"
    assert req.signup_event.data.waba_id == "524126980791429"


def test_phone_registration_pin_must_be_six_digits() -> None:
    WhatsAppRegisterPhoneRequest(pin="123456")

    with pytest.raises(ValidationError):
        WhatsAppRegisterPhoneRequest(pin="12345")

    with pytest.raises(ValidationError):
        WhatsAppRegisterPhoneRequest(pin="abcdef")
