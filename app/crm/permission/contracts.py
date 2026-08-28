"""permission module — public surface (module rules §1).

The ONLY file other modules may import from app/crm/permission.
"""

from app.crm.permission.consent import CustomerNotInMerchant, record_consent
from app.crm.permission.decisions import log_decision

__all__ = ["record_consent", "log_decision", "CustomerNotInMerchant"]
