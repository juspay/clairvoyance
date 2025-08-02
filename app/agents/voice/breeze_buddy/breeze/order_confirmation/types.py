from pydantic import BaseModel

class BreezeOrderData(BaseModel):
    customer_mobile_number: str
    shop_name: str
    order_data: dict
    total_price: float
    customer_name: str
    customer_address: str
    order_id: str
    identity: str = None
    reporting_webhook_url: str = None
