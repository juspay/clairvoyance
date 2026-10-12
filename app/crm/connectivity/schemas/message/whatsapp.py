"""WhatsApp's free-form reply bodies — text, a few reply buttons, a list,
an image — the shapes a reply inside the customer-service window carries.

They say what the reply IS, never how Meta spells it: the payload builder in
providers/whatsapp/ adds Meta's keys. Structure is checked here (non-empty,
unique ids, https); the channel's LIMITS (three buttons, 20-character
titles) are its facts and are checked against its ConversationLimits at the
send door."""

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class _ReplyBodyBase(BaseModel):
    #: The provider's id for the message this reply quotes (Meta's wamid),
    #: or None. Quoting is presentation only: it never changes who the reply
    #: goes to.
    quoted_message_id: Optional[str] = Field(None, min_length=1, max_length=512)


class TextBody(_ReplyBodyBase):
    kind: Literal["text"] = "text"
    text: str = Field(..., min_length=1)
    #: Let the channel render a link preview for the first URL in the text.
    show_link_preview: bool = False


class ReplyButton(BaseModel):
    """One tappable reply. ``id`` comes back on the customer's tap."""

    id: str = Field(..., min_length=1, max_length=256)
    title: str = Field(..., min_length=1)


class ButtonsBody(_ReplyBodyBase):
    kind: Literal["buttons"] = "buttons"
    text: str = Field(..., min_length=1)
    buttons: List[ReplyButton] = Field(..., min_length=1)
    header: Optional[str] = Field(None, min_length=1)
    footer: Optional[str] = Field(None, min_length=1)

    @field_validator("buttons")
    @classmethod
    def _ids_are_unique(cls, buttons: List[ReplyButton]) -> List[ReplyButton]:
        """Two buttons with one id make the tap ambiguous — which one did
        she mean? — so the reply is refused rather than guessed."""
        ids = [button.id for button in buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("button ids must be unique")
        return buttons


class ListRow(BaseModel):
    """One pickable row. ``id`` comes back on the customer's choice."""

    id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1)
    description: Optional[str] = Field(None, min_length=1)


class ListBody(_ReplyBodyBase):
    kind: Literal["list"] = "list"
    text: str = Field(..., min_length=1)
    #: The label on the control that opens the list.
    button_label: str = Field(..., min_length=1)
    rows: List[ListRow] = Field(..., min_length=1)
    section_title: Optional[str] = Field(None, min_length=1)
    header: Optional[str] = Field(None, min_length=1)
    footer: Optional[str] = Field(None, min_length=1)

    @field_validator("rows")
    @classmethod
    def _ids_are_unique(cls, rows: List[ListRow]) -> List[ListRow]:
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("row ids must be unique")
        return rows


class ImageBody(_ReplyBodyBase):
    kind: Literal["image"] = "image"
    #: A public https URL the provider fetches. Plain http is refused: the
    #: provider fetches it from the open internet on the customer's behalf.
    url: str = Field(..., min_length=1, max_length=2048)
    caption: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _https_only(self) -> "ImageBody":
        if not self.url.lower().startswith("https://"):
            raise ValueError("image url must be https")
        return self


#: Every free-form reply shape — the one spelling of the union. Adding a
#: body kind is this line plus its model; everything else imports it.
ReplyBody = Union[TextBody, ButtonsBody, ListBody, ImageBody]

#: The same union, as a request body parses it (by ``kind``).
ReplyBodyByKind = Annotated[ReplyBody, Field(discriminator="kind")]
