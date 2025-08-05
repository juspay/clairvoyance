from pydantic import BaseModel
from typing import List

class OrderItem(BaseModel):
    product_name: str
    quantity: int

class OrderData(BaseModel):
    items: List[OrderItem]

class BreezeOrderData(BaseModel):
    customer_mobile_number: str
    shop_name: str
    order_data: OrderData
    total_price: float
    customer_name: str
    customer_address: str
    order_id: str
    identity: str = None
    reporting_webhook_url: str = None