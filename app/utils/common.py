from datetime import datetime
from typing import Optional, Dict, Any
import json
from app.core.logger import logger
from app.core.config import AWS_BREEZE_PORTAL_URL, GCP_BREEZE_PORTAL_URL

def parse_iso_datetime(iso_string: Optional[str]) -> Optional[datetime]:
    """
    Parse ISO 8601 datetime string to datetime object.
    Handles various ISO format variations including:
    - 2023-12-25T10:30:00Z
    - 2023-12-25T10:30:00+00:00
    - 2023-12-25T10:30:00.123Z
    - 2023-12-25T10:30:00.123456+05:30
    
    Args:
        iso_string: ISO 8601 formatted datetime string
        
    Returns:
        datetime object or None if parsing fails
    """
    if not iso_string:
        return None
    
    try:
        # Handle 'Z' suffix by replacing with '+00:00'
        if iso_string.endswith('Z'):
            iso_string = iso_string[:-1] + '+00:00'
        
        # Use fromisoformat which handles most ISO 8601 variations
        return datetime.fromisoformat(iso_string)
    except ValueError as e:
        logger.error(f"Failed to parse datetime string '{iso_string}': {e}")
        # Fallback: try to parse without timezone info
        try:
            # Remove timezone info and parse as naive datetime
            if '+' in iso_string:
                iso_string = iso_string.split('+')[0]
            elif iso_string.count('-') > 2:  # Has timezone with minus
                # Find the last occurrence of '-' which should be timezone
                parts = iso_string.rsplit('-', 1)
                if len(parts) == 2 and ':' in parts[1]:
                    iso_string = parts[0]
            
            return datetime.fromisoformat(iso_string)
        except ValueError:
            logger.error(f"Failed to parse datetime string even without timezone: '{iso_string}'")
            return None

def parse_json(row, key) -> Optional[Dict[str, Any]]:
    return row[key] if isinstance(row[key], dict) else json.loads(row[key]) if row[key] else None


def get_breeze_portal_url(reseller_id: str | None = None) -> str:
    """
    Get the appropriate Breeze portal base URL based on reseller ID.
    
    Args:
        reseller_id: The reseller identifier. If "super_reseller", returns the SDK store URL.
                    Otherwise, returns the standard portal URL.
    
    Returns:
        str: The base URL for the Breeze portal
    """
    if reseller_id == "super_reseller":
        return GCP_BREEZE_PORTAL_URL
    else:
        return AWS_BREEZE_PORTAL_URL
