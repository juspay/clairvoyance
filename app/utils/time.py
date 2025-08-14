from datetime import datetime, timedelta
from app.core.logger import logger



def is_valid_timestamp(date_string):
    try:
        datetime.strptime(str(date_string), "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def ist_to_utc(ist_time_string, format="%Y-%m-%dT%H:%M:%SZ"):
    """Convert IST time to UTC time.

    Args:
        ist_time_string: Can be either a string in format "%Y-%m-%dT%H:%M:%SZ" or a datetime object
        format: Output format for the returned timestamp

    Returns:
        A string in the specified format
    """
    try:
        # Handle both string and datetime inputs
        if isinstance(ist_time_string, datetime):
            ist_time = ist_time_string
        else:
            ist_time = datetime.strptime(ist_time_string, "%Y-%m-%dT%H:%M:%SZ")

        ist_offset = timedelta(hours=5, minutes=30)
        utc_time = ist_time - ist_offset

        # Check if the UTC time is exactly 18:29:00 and adjust if necessary
        if utc_time.time() == datetime.strptime("18:29:00", "%H:%M:%S").time():
            utc_time += timedelta(seconds=59)

        return utc_time.strftime(format)
    except Exception as e:
        logging.error(f"Error converting ist to utc: {str(e)}")
        # If it's already a datetime, try to return a formatted string
        if isinstance(ist_time_string, datetime):
            return ist_time_string.strftime(format)
        return str(ist_time_string)


def utc_to_ist(utc_time_string: str) -> str:
    try:
        # Try parsing with T separator first
        try:
            utc_time = datetime.strptime(utc_time_string, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # If that fails, try parsing with space separator
            utc_time = datetime.strptime(utc_time_string, "%Y-%m-%d %H:%M:%S")

        ist_offset = timedelta(hours=5, minutes=30)
        ist_time = utc_time + ist_offset
        return ist_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as e:
        logging.error(f"Error converting utc to ist: {str(e)}")
        return utc_time_string


