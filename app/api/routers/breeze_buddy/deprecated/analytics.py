"""
Deprecated analytics endpoints.

The old template-specific analytics endpoints have been moved here for backward compatibility.
They are now part of dashboard.py in the deprecated folder.

Please use the new POST /analytics endpoint instead.
"""

from fastapi import APIRouter

router = APIRouter()

# Note: The old analytics endpoints are in deprecated/dashboard.py
# (GET /breeze/order-confirmation/analytics and GET /breeze/order-confirmation/call-details)
