"""Xyne's template-registry face — a form is registered with US, and Xyne
checks it by being sent it.

Xyne keeps no templates: there is no endpoint to register a form, and no
endpoint that only validates one (apps/backend/src/apps/routes/apps.ts,
chat.ts). Every postMessage carries its whole form and Xyne validates it as
it arrives. So a Xyne form lives in our registry alone (T23 — the one
deviation: T23 is written for providers that hold templates themselves),
and submit asks Xyne the only way Xyne can be asked: it posts a PREVIEW of
the form to the person the merchant named when connecting the app. Xyne's
own validator then judges the LAYOUT every later send will carry (the
per-person ``data`` it accepts as anything, so form.build_flow checks the
option lists itself at send time) —

  201  approved at once (Xyne has no review), and the merchant sees how it
       looks;
  4xx  refused in Xyne's own words (``components.0.props.label: Required``),
       and the row goes back to being a draft to fix.

No copy of Xyne's rules is kept here (form.py checks only what Xyne asks of
every component), so a rule Xyne adds or relaxes is honoured the day it
ships. An edit is checked the same way; retiring tells Xyne nothing, because
Xyne never knew.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from app.crm.connectivity.providers.base import TemplateProviderError
from app.crm.connectivity.providers.xyne import client, form
from app.crm.connectivity.providers.xyne.onboard import (
    API_TOKEN,
    BASE_URL,
    PREVIEW_USER_ID,
)
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.connectivity.schemas.template import ProviderTemplateState, TemplateDraft
from app.crm.connectivity.vocabulary.status import TEMPLATE_APPROVED
from app.crm.platform.contracts import is_suppressed


class XyneTemplateError(TemplateProviderError):
    """Xyne refused a form, or a form cannot be checked — said to the merchant
    verbatim (templates.py passes a TemplateProviderError through)."""


def template_id() -> str:
    """A fresh provider_template_id for a Xyne form.

    Xyne assigns none, and the column is unique across every row and
    status (061) — so the id is ours, and random. Built from the app and the
    name it would collide when an app changes hands: the merchant who let
    the app go keeps their rows, and the new holder could never register a
    form of the same name. Xyne never sees it."""
    return f"xyne:{uuid4().hex}"


class XyneTemplates:
    """The TemplateProvider for Xyne."""

    #: An edit is checked again and takes effect on the next send; nothing
    #: at Xyne holds the old one.
    edits_in_place = True

    async def submit(
        self, bundle: CredentialBundle, account_ref: str, draft: TemplateDraft
    ) -> ProviderTemplateState:
        await _preview(bundle, draft.components, draft.name)
        return ProviderTemplateState(
            provider_template_id=template_id(),
            name=draft.name,
            language=draft.language,
            category=draft.category,
            status=TEMPLATE_APPROVED,
        )

    async def edit(
        self,
        bundle: CredentialBundle,
        account_ref: str,
        provider_template_id: str,
        components: List[Dict[str, Any]],
    ) -> ProviderTemplateState:
        await _preview(bundle, components, "preview")
        return ProviderTemplateState(
            provider_template_id=provider_template_id, status=TEMPLATE_APPROVED
        )

    async def retire(
        self,
        bundle: CredentialBundle,
        account_ref: str,
        provider_template_id: str,
        name: str,
        language: str,
    ) -> None:
        """Nothing to withdraw: Xyne never held it."""
        return None

    def normalize_event(
        self, topic: str, value: Mapping[str, Any]
    ) -> Optional[ProviderTemplateState]:
        """Xyne sends no template events."""
        return None


async def _preview(
    bundle: CredentialBundle, components: List[Dict[str, Any]], name: str
) -> None:
    """Post the form, as written, to the app's preview user — or raise
    XyneTemplateError saying what to fix. Blanks stay as ``{name}`` and the
    ``"$key"`` lists are empty: the preview shows the layout, and Xyne
    judges its shape."""
    problem = form.layout_problem(components)
    if problem:
        raise XyneTemplateError(problem)
    token = bundle.secret(API_TOKEN)
    base_url = bundle.secret(BASE_URL)
    if not token or not base_url:
        raise XyneTemplateError("the stored Xyne connection has no usable token")
    preview_user = bundle.secret(PREVIEW_USER_ID)
    if not preview_user:
        raise XyneTemplateError(
            "this Xyne app has no preview user — connect it again with "
            "preview_user_id: a Xyne form is checked by sending its preview to "
            "that person before any workflow may use it"
        )
    flow = form.build_flow(components, {}, name, preview=True)
    if isinstance(flow, str):
        raise XyneTemplateError(flow)
    # The preview is a message to a person, so their STOP counts here too.
    # is_suppressed fails closed: an unreadable book also means no preview.
    if await is_suppressed({"xyne_user_id": preview_user}):
        raise XyneTemplateError(
            "the preview user cannot be sent the preview: they asked not to "
            "be contacted, or that could not be checked — try again, or "
            "connect the app again with another preview_user_id"
        )
    try:
        await client.post_message(
            base_url, token, {"channelId": preview_user, "flow": flow}
        )
    except client.XyneRefused as e:
        raise XyneTemplateError(str(e)) from e
    except Exception as e:
        raise XyneTemplateError(
            "Xyne could not be reached to check this form — try again"
        ) from e
