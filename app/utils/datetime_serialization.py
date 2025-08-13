from datetime import datetime
import json


class DateTimeEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles datetime objects by converting them to ISO format strings.
    """

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
        return super().default(obj)


def json_dumps_with_datetime(obj):
    """
    Serialize obj to a JSON formatted string with datetime support.
    """
    return json.dumps(obj, cls=DateTimeEncoder)
