"""Xyne's action face — what a run can DO through a merchant's Xyne app.

One action today, ``post_message``: post text (and optionally an interactive
FlowJSON form) to a Xyne channel. The plan names ``{connector: "xyne",
action: "post_message"}`` and the channel; the base URL and token come from
the vault row behind the merchant's Xyne door, never from the plan.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

from typing import Any, ClassVar, Dict, Optional, Type

from pydantic import BaseModel, Field, model_validator

from app.core.logger import logger
from app.crm.connectivity.accounts import AccountError, bundle_for
from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.xyne import client
from app.crm.connectivity.providers.xyne.onboard import API_TOKEN, BASE_URL
from app.crm.connectivity.schemas.connector import ConnectorInstallation


class PostMessageArgs(BaseModel):
    """What posting to Xyne means: which channel, what text, which form."""

    channel_id: str = Field(..., min_length=1, description="The Xyne channel id")
    text: str = Field("", description="Message text; may be a {placeholder}")
    flow: Optional[Dict[str, Any]] = Field(
        None, description="A FlowJSON form to render under the message"
    )

    @model_validator(mode="after")
    def _says_something(self) -> "PostMessageArgs":
        """A post with neither text nor a form is an author's typo, refused at
        publish rather than sent as an empty bubble."""
        if not self.text.strip() and not self.flow:
            raise ValueError("post_message needs text, a flow, or both")
        return self


def _door(installation: Optional[ConnectorInstallation]) -> ConnectorInstallation:
    """The merchant's Xyne app, or a refusal."""
    if installation is None:
        raise ActionError("this merchant has no Xyne app connected — connect one first")
    return installation


class PostMessage:
    """Post one message to a Xyne channel."""

    args_model: ClassVar[Type[BaseModel]] = PostMessageArgs

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(args, PostMessageArgs):
            raise ActionError("post_message was handed the wrong argument model")
        door = _door(installation)
        try:
            bundle = await bundle_for(door)
        except AccountError as e:
            # Dead, deactivated or undecryptable: a defect, not a moment.
            raise ActionError(f"xyne credential unusable: {e}") from e
        base_url = bundle.secret(BASE_URL)
        api_token = bundle.secret(API_TOKEN)
        if base_url is None or api_token is None:
            raise ActionError("xyne credential is missing base_url or api_token")

        body: Dict[str, Any] = {"channelId": args.channel_id, "text": args.text}
        if args.flow:
            body["flow"] = args.flow
        try:
            answer = await client.post_message(base_url, api_token, body)
        except (client.XyneRefused, ValueError) as e:
            # ValueError: a stored base_url that no longer passes the host
            # check — a defect an operator must fix, never a retry.
            raise ActionError(str(e)) from e

        logger.info(
            f"xyne: posted to channel for app {door.external_account_id} "
            f"(run {context.get('run_id', '')}, node {context.get('node_id', '')})"
        )
        # The action's own facts, flat: Xyne's message id when it names one.
        message_id = answer.get("messageId") or answer.get("id")
        return {"ok": True, "message_id": str(message_id) if message_id else None}


#: The words a plan document may say for this connector, and nothing else.
XYNE_ACTIONS: Dict[str, ConnectorAction] = {"post_message": PostMessage()}

__all__ = ["PostMessage", "PostMessageArgs", "XYNE_ACTIONS"]
