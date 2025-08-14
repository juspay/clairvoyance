from datetime import datetime, timedelta
from app.core.logger import logger



def convert_utc_to_ist_in_qapi_response(
    response_json: list, time_field: str = "order_created_at_time"
) -> list:
    """
    Convert UTC timestamps to IST in JSON response objects.

    Args:
        response_json: List of JSON objects
        time_field: The field name containing the UTC timestamp to convert

    Returns:
        List of JSON objects with converted timestamps
    """
    try:
        from ..time import utc_to_ist
        
        for row in response_json:
            if time_field in row:
                row[time_field] = utc_to_ist(row[time_field])
                logger.info(
                    f"Converted {time_field} from utc to ist: {row[time_field]}"
                )
        return response_json
    except Exception as e:
        logger.error(f"Error converting {time_field} in JSON response: {str(e)}")
        return response_json
