"""Row -> schema translation for crm_customer. DB-side only — paired with
accessor/queries; nothing above the accessor imports this file."""

import json

import asyncpg

from app.crm.identity.schemas import CrmCustomer, CrmCustomerSummary


def decode_crm_customer_summary(row: asyncpg.Record) -> CrmCustomerSummary:
    return CrmCustomerSummary(**dict(row))


def decode_crm_customer(row: asyncpg.Record) -> CrmCustomer:
    data = dict(row)
    attributes = data.get("attributes")
    if isinstance(attributes, str):
        data["attributes"] = json.loads(attributes)
    return CrmCustomer(**data)
