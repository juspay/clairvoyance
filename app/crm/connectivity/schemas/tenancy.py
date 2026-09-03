"""The one field every request body that names a merchant declares.

``merchant_scope`` (app/crm/auth.py) reads it from the body of a POST or
PATCH exactly as it reads ``merchant_id`` from a GET's query — so a route
never spells the tenancy check itself, and a request model that forgets
to inherit this base is a body the dependency cannot scope (a 400, never a
silent pass).
"""

from pydantic import BaseModel, Field


class TenantScoped(BaseModel):
    """Base for request bodies: the merchant the caller must be allowed to
    touch. Checked BEFORE the handler runs, by the route's dependency."""

    merchant_id: str = Field(..., description="Tenant scope — required")
