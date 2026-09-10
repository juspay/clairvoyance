"""Wire shapes for the Buddy Assist virtual try-on route.

The request is multipart (a photo file plus form fields), so it has no
Pydantic model — FastAPI binds the parts directly, exactly as the
push-to-talk transcribe route does. Only the response is modelled here.
"""

from pydantic import BaseModel, Field


class WidgetTryOnResponse(BaseModel):
    """Body of ``POST /widget/session/{session_id}/try-on``.

    ``image`` is an ``<img src>`` the widget renders directly: a provider
    URL, or a data URI when the provider returned raw bytes. It is not
    persisted anywhere on this side beyond the short reload-recovery cache.
    """

    image: str = Field(..., description="Generated try-on image as an <img src>.")
    request_id: str = Field(
        ...,
        description=(
            "Echo of the caller's idempotency key. Re-posting it within the "
            "cache window returns this same image without charging again."
        ),
    )
    cached: bool = Field(
        False,
        description="True when this image came from the reload-recovery cache.",
    )
    credits_charged: int = Field(
        0,
        description="Credits deducted for this request. Zero for a cache hit.",
    )
