import json

from app.core.config import ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY
from app.core.logger import logger
from app.core.security.sha import calculate_hmac_sha256
from app.schemas import LeadCallOutcome

# Mapping dictionary for outcome strings to LeadCallOutcome enum values
OUTCOME_TO_ENUM = {
    "confirmed": LeadCallOutcome.CONFIRM,
    "cancelled": LeadCallOutcome.CANCEL,
    "busy": LeadCallOutcome.BUSY,
    "address_updated": LeadCallOutcome.ADDRESS_UPDATED,
    "no_answer": LeadCallOutcome.NO_ANSWER,
    "unknown": LeadCallOutcome.UNKNOWN,
}


def indian_number_to_speech(number: int) -> str:
    if number < 100:
        return f"{number} rupees"

    parts = []
    num_str = str(number)
    n = len(num_str)

    # Process last 3 digits (hundreds)
    if n >= 3:
        last_three = int(num_str[-3:])
        if last_three:
            parts.append(f"{last_three}")

    # Process thousands
    if n > 3:
        thousand = int(num_str[-5:-3]) if n >= 5 else int(num_str[-4:-3])
        if thousand:
            parts.insert(0, f"{thousand} thousand")

    # Process lakhs
    if n > 5:
        lakh = int(num_str[-7:-5]) if n >= 7 else int(num_str[-6:-5])
        if lakh:
            parts.insert(0, f"{lakh} lakh")

    # Process crores
    if n > 7:
        crore = int(num_str[:-7])
        if crore:
            parts.insert(0, f"{crore} crore")

    # Adjust hundreds format for last part
    if parts and int(parts[-1]) >= 100:
        h = int(parts[-1])
        h_part = f"{h // 100} hundred"
        rest = h % 100
        if rest:
            h_part += f" {rest}"
        parts[-1] = h_part

    return " ".join(parts) + " rupees"


async def send_webhook_with_retry(
    session, url: str, data: dict, max_retries: int = 3
) -> bool:
    """
    Sends a webhook with retry logic up to max_retries attempts.
    Returns True if any attempt succeeds (status 200), False otherwise.

    Args:
        session: aiohttp session
        url: webhook URL
        data: payload data
        max_retries: maximum number of attempts (default 3)

    Returns:
        bool: True if successful, False if all attempts failed
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    signature = calculate_hmac_sha256(payload, ORDER_CONFIRMATION_WEBHOOK_SECRET_KEY)
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["checksum"] = signature

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Webhook attempt {attempt}/{max_retries} to {url}")
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    logger.info(f"Webhook succeeded on attempt {attempt}")
                    return True
                else:
                    response_text = await response.text()
                    logger.warning(
                        f"Webhook attempt {attempt} failed. Status: {response.status}, Body: {response_text}"
                    )
        except Exception as e:
            logger.error(f"Webhook attempt {attempt} error: {e}", exc_info=True)

        # Don't sleep after the last attempt
        if attempt < max_retries:
            logger.info(f"Retrying webhook (attempt {attempt + 1}/{max_retries})...")

    logger.error(f"All {max_retries} webhook attempts failed for {url}")
    return False
