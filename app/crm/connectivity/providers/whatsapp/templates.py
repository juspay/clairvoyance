"""WhatsApp's template-registry face — Meta's message_templates endpoints,
and the one place Meta's SHOUTING becomes the canon's lowercase.

Reached only through connectivity/connectors.py (boundary rule 11).

Why the normaliser lives here and nowhere else: Meta answers `APPROVED`,
canon T23 spells the dictionary `approved`, and every rule in templates.py
compares lowercase. A registry that stored both had `APPROVED`, `PENDING`,
`submitting` and `deleted` sitting side by side; `?status=approved` returned
zero rows while the row was plainly approved, and editing it was refused
with "is 'APPROVED' — edit not supported from this status". Spending the
quirk at the boundary is the only place it cannot leak.
"""

from typing import Any, Dict, List, Mapping, Optional

from app.core.logger import logger
from app.crm.connectivity.providers.base import TemplateProviderError
from app.crm.connectivity.providers.meta.graph import GraphError, call, segment
from app.crm.connectivity.providers.whatsapp import TOKEN_KEY
from app.crm.connectivity.schemas import (
    CredentialBundle,
    ProviderTemplateState,
    TemplateDraft,
)
from app.crm.connectivity.topics import (
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)


class WhatsappTemplateError(TemplateProviderError):
    """Meta refused a template operation.

    Subclasses the port's declared type so templates.py can tell this — a
    refusal describing the merchant's own components — from an unexpected
    exception whose text must not reach the API response.
    """


def _from_meta(word: Optional[str]) -> Optional[str]:
    """Meta's vocabulary -> canon's.

    Lowercasing, not a lookup table. Canon T23 names seven words "+ whatever
    Meta adds", so an unrecognised one (PENDING_DELETION, FLAGGED) passes
    through as itself rather than being dropped or mapped to a guess: an
    unknown status is not 'approved', which is the only judgement anything
    downstream actually makes.
    """
    if word is None:
        return None
    normalised = str(word).strip().lower()
    return normalised or None


async def _graph(method: str, path: str, **kwargs) -> Dict[str, Any]:
    """One Graph call, with the transport error translated at this boundary.

    GraphError is meta/graph.py's type and stays inside providers/ (rule 11),
    so templates.py could not catch it even if it wanted to. Converting here
    keeps its detail — which for a template operation is Meta describing the
    merchant's own components, the actionable half of the refusal — while
    letting the generic code catch exactly one declared type.
    """
    try:
        return await call(method, path, **kwargs)
    except GraphError as e:
        raise WhatsappTemplateError(
            e.detail or "Meta refused this template operation"
        ) from e


def _token(bundle: CredentialBundle) -> str:
    token = bundle.secret(TOKEN_KEY)
    if not token:
        raise WhatsappTemplateError("the stored credential has no usable token")
    return token


class WhatsappTemplates:
    """The TemplateProvider for WhatsApp."""

    #: Meta re-reviews an edited template on the SAME row (approved, rejected
    #: or paused -> pending). SMS-DLT cannot, and would have to re-register
    #: under a new id — which is why templates.py asks the provider instead
    #: of assuming Meta's behaviour is universal.
    edits_in_place = True

    async def submit(
        self, bundle: CredentialBundle, account_ref: str, draft: TemplateDraft
    ) -> ProviderTemplateState:
        """POST /{waba}/message_templates — register a draft for review."""
        body = await _graph(
            "POST",
            f"{segment(account_ref)}/message_templates",
            access_token=_token(bundle),
            json_body={
                "name": draft.name,
                "language": draft.language,
                "category": draft.category,
                "components": draft.components,
            },
        )
        provider_template_id = body.get("id")
        if not provider_template_id:
            # Without an id we cannot ever match this template to a webhook,
            # so a 200 without one is a failure, not a success.
            raise WhatsappTemplateError("Meta accepted the template but returned no id")
        return ProviderTemplateState(
            provider_template_id=str(provider_template_id),
            name=draft.name,
            language=draft.language,
            # Meta may assign a DIFFERENT category from the one requested.
            # Theirs is what the merchant is billed at, so theirs is stored.
            category=body.get("category") or draft.category,
            status=_from_meta(body.get("status")) or "pending",
        )

    async def edit(
        self,
        bundle: CredentialBundle,
        account_ref: str,
        provider_template_id: str,
        components: List[Dict[str, Any]],
    ) -> ProviderTemplateState:
        """POST /{template_id} — Meta addresses an edit by the template's own
        id, not nested under the account.

        The answer carries no status, so the transition is stated rather than
        read: an edit sends the template back for review. The webhook that
        follows is what confirms it.
        """
        await _graph(
            "POST",
            segment(provider_template_id),
            access_token=_token(bundle),
            json_body={"components": components},
        )
        return ProviderTemplateState(
            provider_template_id=provider_template_id, status="pending"
        )

    async def retire(
        self,
        bundle: CredentialBundle,
        account_ref: str,
        provider_template_id: str,
        name: str,
        language: str,
    ) -> None:
        """DELETE /{waba}/message_templates — withdraw ONE template.

        ``hsm_id`` is the load-bearing parameter. Meta's delete takes a name,
        and a name alone deletes EVERY LANGUAGE VARIANT of it: retiring
        order_update/en_US would silently delete order_update/hi_IN on Meta
        while our hi_IN row still reads 'approved', and the first send on it
        would fail at Meta with 132001. Passing the provider's id alongside
        the name is Meta's documented single-language form.
        """
        await _graph(
            "DELETE",
            f"{segment(account_ref)}/message_templates",
            access_token=_token(bundle),
            params={"name": name, "hsm_id": provider_template_id},
        )

    def normalize_event(
        self, topic: str, value: Mapping[str, Any]
    ) -> Optional[ProviderTemplateState]:
        """One Meta webhook `value` object -> the registry's vocabulary.

        Returns None when the letter carries nothing this registry stores —
        which is an ordinary outcome, not an error: the same bay delivers
        fields we do not consume yet.
        """
        provider_template_id = value.get("message_template_id")
        state = ProviderTemplateState(
            provider_template_id=(
                str(provider_template_id) if provider_template_id else None
            ),
            name=value.get("message_template_name"),
            language=value.get("message_template_language"),
        )

        if topic == TOPIC_TEMPLATE_STATUS:
            status = _from_meta(value.get("event"))
            if status is None:
                return None
            state.status = status
            # Meta spells it 'reason' and sends the literal string "NONE"
            # when there is none — storing that would show a merchant the
            # word NONE as their rejection reason.
            reason = value.get("reason")
            if reason and str(reason).upper() != "NONE":
                state.rejection_reason = str(reason)
            return state

        if topic == TOPIC_TEMPLATE_CATEGORY:
            category = value.get("new_category")
            if not category:
                return None
            state.category = str(category)
            return state

        if topic == TOPIC_TEMPLATE_QUALITY:
            quality = value.get("new_quality_score")
            if not quality:
                return None
            state.quality = str(quality)
            return state

        logger.debug(f"whatsapp templates: nothing to apply for topic '{topic}'")
        return None
