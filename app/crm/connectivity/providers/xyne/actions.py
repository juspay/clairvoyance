"""Xyne's action face — one verb, ``request``, configured by the workflow.

The plan says WHAT to call: ``{connector: "xyne", action: "request", args:
{url: "/api/apps/chat/postMessage", method: "POST", body: {...}}}``. The
door says WHERE: ``url`` is a path under the app's ``door_data.base_url``,
and the API token comes from the vault row behind the merchant's Xyne door —
neither is ever written in a plan. A new Xyne endpoint is a plan change, not
a deploy.

Values in ``body`` may be ``{placeholders}``, resolved by the action square
from the run's facts (nested objects included) before this file is called.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

from typing import Any, ClassVar, Dict, Literal, Optional, Type

from pydantic import BaseModel, Field, field_validator

from app.core.logger import logger
from app.crm.connectivity.accounts import AccountError, bundle_for
from app.crm.connectivity.providers.base import ActionError, ConnectorAction
from app.crm.connectivity.providers.xyne import client
from app.crm.connectivity.providers.xyne.onboard import API_TOKEN, BASE_URL
from app.crm.connectivity.schemas.connector import ConnectorInstallation


class RequestArgs(BaseModel):
    """One call to the merchant's Xyne app, as the workflow configured it."""

    url: str = Field(
        ...,
        min_length=1,
        description="Path under the app's base URL, e.g. /api/apps/chat/postMessage",
    )
    method: Literal["GET", "POST", "PUT", "PATCH"] = "POST"
    body: Dict[str, Any] = Field(
        default_factory=dict, description="JSON body; values may be {facts}"
    )

    @field_validator("url")
    @classmethod
    def _a_path_not_an_address(cls, value: str) -> str:
        """The host is the door's, never the plan's: a scheme, a host or a
        protocol-relative ``//`` would let a plan send the token elsewhere."""
        if "://" in value or not value.startswith("/") or value.startswith("//"):
            raise ValueError(
                "url must be a path starting with / (the base URL is the door's)"
            )
        return value


def _door(installation: Optional[ConnectorInstallation]) -> ConnectorInstallation:
    """The merchant's Xyne app, or a refusal."""
    if installation is None:
        raise ActionError("this merchant has no Xyne app connected — connect one first")
    return installation


class Request:
    """Send one workflow-configured request to the merchant's Xyne app."""

    args_model: ClassVar[Type[BaseModel]] = RequestArgs

    async def perform(
        self,
        merchant_id: str,
        installation: Optional[ConnectorInstallation],
        args: BaseModel,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(args, RequestArgs):
            raise ActionError("request was handed the wrong argument model")
        door = _door(installation)
        base_url = door.door_data.get(BASE_URL)
        if not isinstance(base_url, str) or not base_url:
            raise ActionError(
                "xyne door has no door_data.base_url — re-onboard the app"
            )
        try:
            bundle = await bundle_for(door)
        except AccountError as e:
            # Dead, deactivated or undecryptable: a defect, not a moment.
            raise ActionError(f"xyne credential unusable: {e}") from e
        api_token = bundle.secret(API_TOKEN)
        if api_token is None:
            raise ActionError("xyne credential is missing api_token")

        try:
            status, _answer = await client.request(
                base_url, api_token, args.method, args.url, args.body
            )
        except (client.XyneRefused, ValueError) as e:
            # ValueError: a stored base_url that no longer passes the host
            # check — a defect an operator must fix, never a retry.
            raise ActionError(str(e)) from e

        logger.info(
            f"xyne: {args.method} {args.url} -> {status} for app "
            f"{door.external_account_id} (run {context.get('run_id', '')}, "
            f"node {context.get('node_id', '')})"
        )
        return {"ok": True, "status": status}


#: The words a plan document may say for this connector, and nothing else.
XYNE_ACTIONS: Dict[str, ConnectorAction] = {"request": Request()}

__all__ = ["Request", "RequestArgs", "XYNE_ACTIONS"]
