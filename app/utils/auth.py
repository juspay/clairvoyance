from typing import Dict, Any, Optional, List


def get_authorized_merchants(token_response: Dict[str, Any]) -> Optional[List[str]]:
    """
    Determines the list of authorized merchant IDs based on the user's token response context.

    Args:
        token_response (dict): The token response dictionary received from /ec/v1/validate/token

    Returns:
        list or None: A list of authorized merchant IDs, or None if all merchants are authorized.
    """
    context = token_response.get("context")
    if context == "JUSPAY":
        return None
    elif context == "MERCHANT":
        user_merchant_id = token_response.get("merchantId")
        return [user_merchant_id]
    elif context == "TENANT":
        tenant_info = token_response.get("tenantInfo", {})
        return tenant_info.get("merchants", [])
    elif context == "RESELLER":
        reseller_info = token_response.get("resellerInfo", {})
        return reseller_info.get("merchants", [])
    else:
        # Default to no authorized merchants if context is unknown
        return []


def validate_and_extract_fields(filter_data: dict, allowed_values: dict) -> dict:
    """
    Recursively extracts field names and validates their values from the provided filter data.

    Args:
        filter_data (dict): The filter data to process.
        allowed_values (dict): A dictionary specifying allowed values for each field.

    Returns:
        dict: A dictionary of fields with invalid values. The keys are the field names, and the values are the invalid values.
    """
    if not isinstance(filter_data, dict):
        return {}

    invalid_fields = {}

    if "field" in filter_data and "val" in filter_data:
        field = filter_data["field"]
        val = filter_data["val"]

        ignored_fields = [
            "run_day_ist",
            "run_hour_ist",
            "run_month_ist",
            "run_week_ist",
            "error_message",
            "juspay_error_message",
            "udf1",
            "udf10",
            "udf2",
            "udf3",
            "udf4",
            "udf5",
            "udf6",
            "udf7",
            "udf8",
            "udf9",
        ]

        if field not in ignored_fields:
            if field in allowed_values:
                allowed = allowed_values[field]
                if isinstance(val, list):
                    invalid_values = [v for v in val if v not in allowed]
                elif isinstance(val, dict):
                    invalid_values = []
                else:
                    invalid_values = [val] if val not in allowed else []

                if invalid_values:
                    invalid_fields[field] = invalid_values

    if "or" in filter_data and isinstance(filter_data["or"], dict):
        invalid_fields.update(
            validate_and_extract_fields(
                filter_data["or"].get("left", {}), allowed_values
            )
        )
        invalid_fields.update(
            validate_and_extract_fields(
                filter_data["or"].get("right", {}), allowed_values
            )
        )
    elif "and" in filter_data and isinstance(filter_data["and"], dict):
        invalid_fields.update(
            validate_and_extract_fields(
                filter_data["and"].get("left", {}), allowed_values
            )
        )
        invalid_fields.update(
            validate_and_extract_fields(
                filter_data["and"].get("right", {}), allowed_values
            )
        )

    return invalid_fields
