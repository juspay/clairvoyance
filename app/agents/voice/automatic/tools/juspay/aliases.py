"""
Alias definitions for Q API metrics and dimensions.
This module contains all the alias mappings used to translate between 
LLM-friendly names and actual system field names.
"""

from typing import Dict

# Map from LLM-known names to actual metric names in the system
METRIC_ALIASES: Dict[str, str] = {
    # Revenue/Financial metrics
    "revenue": "total_amount",
    "sales": "total_amount",
    "gmv": "total_amount",
    "count_of_orders_saved_due_to_silent_retry": "saved_orders_volume",
    "count_of_orders_saved_due_to_health_based_routing": "saved_orders_volume_gateway",
    "success_gmv_of_orders_saved_due_to_silent_retry": "saved_orders_amount",
    "success_gmv_of_orders_saved_due_to_health_based_routing": "saved_orders_amount_gateway"
}

# Map from LLM-known names to actual dimension names in the system
DIMENSION_ALIASES: Dict[str, str] = {
    # Payment related
    "payment_provider": "payment_gateway",
    "gateway": "payment_gateway",
    "card_network": "card_brand",
    "card_provider": "card_brand",
    "is_stored_card_transaction": "token_repeat",
    "transaction_via_saved_card": "token_repeat",
}

# Reverse maps for converting back from actual names to LLM-known names
METRIC_ALIASES_REVERSE: Dict[str, str] = {v: k for k, v in METRIC_ALIASES.items()}
DIMENSION_ALIASES_REVERSE: Dict[str, str] = {v: k for k, v in DIMENSION_ALIASES.items()}

def resolve_metric_alias(metric_name: str) -> str:
    """Convert LLM-known metric name to actual system metric name."""
    return METRIC_ALIASES.get(metric_name, metric_name)

def resolve_dimension_alias(dimension_name: str) -> str:
    """Convert LLM-known dimension name to actual system dimension name."""
    return DIMENSION_ALIASES.get(dimension_name, dimension_name)

def reverse_metric_alias(metric_name: str) -> str:
    """Convert actual system metric name back to LLM-known name."""
    return METRIC_ALIASES_REVERSE.get(metric_name, metric_name)

def reverse_dimension_alias(dimension_name: str) -> str:
    """Convert actual system dimension name back to LLM-known name."""
    return DIMENSION_ALIASES_REVERSE.get(dimension_name, dimension_name)
