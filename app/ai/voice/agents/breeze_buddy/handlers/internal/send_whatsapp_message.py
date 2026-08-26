"""Built-in global function for merchant-scoped Meta WhatsApp messages."""

from typing import Any, Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import GlobalBuiltinFunction
from app.database.accessor.breeze_buddy.whatsapp import (
    get_active_merchant_whatsapp_connector,
    get_whatsapp_credential_secret,
    increment_merchant_whatsapp_message_counts,
)
from app.services.whatsapp import MetaWhatsAppService

_meta_whatsapp = MetaWhatsAppService()
_TEMPLATE_MESSAGE_TYPE = "template"
_TEXT_MESSAGE_TYPE = "text"


def _tenant_scope(context: TemplateContext) -> Tuple[Optional[str], Optional[str]]:
    template = getattr(context.bot, "template", None)
    lead = context.lead
    reseller_id = getattr(template, "reseller_id", None) or getattr(
        lead, "reseller_id", None
    )
    merchant_id = getattr(template, "merchant_id", None) or getattr(
        lead, "merchant_id", None
    )
    return reseller_id, merchant_id


def _template_values(value: Any) -> Optional[list[str]]:
    """Accept only the ordered string array Meta template parameters require."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _values_count_is_valid(
    values: list[str], function_config: Optional[GlobalBuiltinFunction]
) -> bool:
    """Require the function config to declare Meta's exact placeholder count."""
    if function_config is None:
        return False

    values_schema = function_config.properties.get("values")
    if not isinstance(values_schema, dict):
        return False

    min_items = values_schema.get("minItems")
    max_items = values_schema.get("maxItems")
    if (
        not isinstance(min_items, int)
        or not isinstance(max_items, int)
        or min_items != max_items
    ):
        return False
    return len(values) == min_items


def _payload_template_values(payload: Dict[str, Any]) -> Optional[list[str]]:
    """Build the payment-link template parameters from the lead payload so the
    LLM never has to supply them."""
    recovery_url = str(payload.get("recovery_url") or "").strip()
    if not recovery_url:
        return None
    customer_name = str(payload.get("customer_name") or "").strip() or "there"
    return [customer_name, "completing your purchase", recovery_url]


async def send_whatsapp_message(
    context: TemplateContext,
    args: Dict[str, Any],
    function_config: Optional[GlobalBuiltinFunction] = None,
) -> Dict[str, Any]:
    """Send a merchant-scoped Meta template or free-form text message."""
    reseller_id, merchant_id = _tenant_scope(context)
    if not reseller_id or not merchant_id:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": "WhatsApp is unavailable because the merchant scope is missing.",
        }

    for field, expected in (("reseller_id", reseller_id), ("merchant_id", merchant_id)):
        supplied = args.get(field)
        if supplied is not None and supplied != expected:
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": "WhatsApp cannot be sent outside this merchant scope.",
            }

    lead = context.lead
    lead_payload = (lead.payload or {}) if lead else {}

    raw_recipient = str(
        args.get("recipient_phone_number")
        or lead_payload.get("customer_mobile_number")
        or ""
    ).strip()
    message_type = (str(args.get("message_type") or "").strip().lower()) or (
        _TEMPLATE_MESSAGE_TYPE
    )
    requested_template_name = str(args.get("template_name") or "").strip() or None
    message = str(args.get("message") or "").strip() or None
    values = _template_values(args.get("values"))
    if not raw_recipient:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": "A recipient phone number is required.",
        }
    recipient = _meta_whatsapp.normalize_recipient(raw_recipient)
    if not recipient:
        digits = "".join(ch for ch in raw_recipient if ch.isdigit())
        if len(digits) == 10:
            recipient = _meta_whatsapp.normalize_recipient("91" + digits)
    if not recipient:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": (
                "Customer phone number must be in international format with "
                "country code."
            ),
        }
    if message_type not in {_TEMPLATE_MESSAGE_TYPE, _TEXT_MESSAGE_TYPE}:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": "message_type must be either 'template' or 'text'.",
        }

    connector = await get_active_merchant_whatsapp_connector(reseller_id, merchant_id)
    if not connector or not connector.credential_id:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": "No active WhatsApp connection is configured for this merchant.",
        }

    metadata = connector.metadata
    configured_template_name = str(metadata.get("template_name") or "").strip() or None
    language_code = str(metadata.get("language_code") or "en_US").strip()

    if message_type == _TEMPLATE_MESSAGE_TYPE:
        if not configured_template_name:
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": (
                    "No WhatsApp message template is configured for this merchant."
                ),
            }
        if (
            requested_template_name is not None
            and requested_template_name != configured_template_name
        ):
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": (
                    "The requested WhatsApp template is not configured for "
                    "this merchant."
                ),
            }
        if message is not None:
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": "message cannot be used when message_type is 'template'.",
            }
        if "values" in args:
            if values is None or not _values_count_is_valid(values, function_config):
                return {
                    "status": "error",
                    "whatsapp_sent": False,
                    "message": (
                        "Template values must be an ordered text array with the "
                        "configured number of values."
                    ),
                }
        else:
            values = _payload_template_values(lead_payload)
            if values is None:
                return {
                    "status": "error",
                    "whatsapp_sent": False,
                    "message": (
                        "The lead payload is missing the recovery URL required "
                        "for the WhatsApp template."
                    ),
                }
        template_name = configured_template_name
    else:
        if requested_template_name is not None or args.get("values") is not None:
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": (
                    "template_name and values cannot be used when message_type "
                    "is 'text'."
                ),
            }
        if not message:
            return {
                "status": "error",
                "whatsapp_sent": False,
                "message": "A free-form WhatsApp message is required for text mode.",
            }
        template_name = None
        values = None

    secret = await get_whatsapp_credential_secret(
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        credential_id=connector.credential_id,
    )
    phone_number_id = connector.metadata.get("phone_number_id")
    if not secret or not phone_number_id:
        return {
            "status": "error",
            "whatsapp_sent": False,
            "message": "The merchant WhatsApp connection is incomplete.",
        }

    result = await _meta_whatsapp.send_message(
        session=context.aiohttp_session,
        access_token=secret.access_token,
        phone_number_id=str(phone_number_id),
        recipient_phone_number=recipient,
        template_name=template_name,
        values=values,
        message=message,
        language_code=language_code or "en_US",
    )
    if result["success"]:
        await increment_merchant_whatsapp_message_counts(
            reseller_id, merchant_id, sent_increment=1
        )
        return {
            "status": "success",
            "whatsapp_sent": True,
            "message": "WhatsApp message sent successfully.",
            "message_id": result.get("message_id"),
        }

    await increment_merchant_whatsapp_message_counts(
        reseller_id, merchant_id, failed_increment=1
    )
    return {
        "status": "error",
        "whatsapp_sent": False,
        "message": "WhatsApp message could not be sent.",
        "error": result.get("error"),
        "error_code": result.get("error_code"),
    }
