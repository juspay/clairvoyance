"""
ACME Store Demo Data Module
Provides time-based analytics data for merchant_id="acme-store-demo"
"""

from .breeze_data import ACME_BREEZE_DATA
from .juspay_data import ACME_JUSPAY_DATA
from .aggregator import aggregate_data, get_data_indices, aggregate_complete_structure
from . import breeze_parser
from . import juspay_parser

__all__ = [
    "ACME_BREEZE_DATA",
    "ACME_JUSPAY_DATA",
    "aggregate_data",
    "get_data_indices",
    "aggregate_complete_structure",
    "breeze_parser",
    "juspay_parser",
]