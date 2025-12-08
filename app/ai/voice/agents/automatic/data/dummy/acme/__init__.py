"""
ACME Store Demo Data Module
Provides time-based analytics data for merchant_id="acme-store-demo"
"""

from . import breeze_parser, juspay_parser
from .aggregator import aggregate_complete_structure, aggregate_data, get_data_indices
from .breeze_data import ACME_BREEZE_DATA
from .juspay_data import ACME_JUSPAY_DATA

__all__ = [
    "ACME_BREEZE_DATA",
    "ACME_JUSPAY_DATA",
    "aggregate_data",
    "get_data_indices",
    "aggregate_complete_structure",
    "breeze_parser",
    "juspay_parser",
]
