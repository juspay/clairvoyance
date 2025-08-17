import httpx
import json
from pipecat.services.llm_service import FunctionCallParams
from app.core.config import BREEZE_BUDDY_TEST_SHOPIFY_SHOP_URL, BREEZE_BUDDY_TEST_SHOPIFY_ADMIN_TOKEN
from app.core.logger import logger
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData, OrderData, OrderItem

def _transform_shopify_to_breeze(order_node: dict, shop_name: str) -> BreezeOrderData:
    """Transforms a Shopify order node into a BreezeOrderData object."""
    customer = order_node.get("customer", {})
    shipping_address = order_node.get("shippingAddress", {})
    line_items = order_node.get("lineItems", {}).get("edges", [])

    phone = customer.get("phone") or shipping_address.get("phone")
    return BreezeOrderData(
        order_id=order_node.get("name"),
        customer_name=f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip(),
        customer_mobile_number=f"+91{phone}" if phone else "",
        customer_address=", ".join(filter(None, [
            shipping_address.get("address1"),
            shipping_address.get("address2"),
            shipping_address.get("city"),
            shipping_address.get("province"),
            shipping_address.get("zip"),
            shipping_address.get("country"),
        ])),
        shop_name=shop_name,
        total_price=float(order_node.get("totalPriceSet", {}).get("shopMoney", {}).get("amount", 0.0)),
        order_data=OrderData(
            items=[
                OrderItem(
                    product_name=item["node"]["title"],
                    quantity=item["node"]["quantity"],
                ) for item in line_items
            ]
        ),
        identity="breeze"
    )

async def get_shopify_orders(params: FunctionCallParams):
    """
    Fetches order details from Shopify within a given date range.

    Args:
        params: The FunctionCallParams object containing the arguments.

    Returns:
        A list of dictionaries, where each dictionary represents an order
        formatted as BreezeOrderData.
    """
    start_date = params.arguments.get("start_date")
    end_date = params.arguments.get("end_date")
    url = f"{BREEZE_BUDDY_TEST_SHOPIFY_SHOP_URL}/admin/api/2025-07/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": BREEZE_BUDDY_TEST_SHOPIFY_ADMIN_TOKEN,
    }
    query = f"""
    {{
      shop {{
        name
      }}
      orders(
        first: 20
        query: "created_at:>=\\"{start_date}\\" created_at:<=\\"{end_date}\\""
      ) {{
        edges {{
          node {{
            id
            name
            createdAt
            displayFinancialStatus
            totalPriceSet {{
              shopMoney {{
                amount
                currencyCode
              }}
            }}
            customer {{
              firstName
              lastName
              email
              phone
            }}
            shippingAddress {{
              firstName
              lastName
              address1
              address2
              city
              province
              country
              zip
              phone
            }}
            lineItems(first: 10) {{
              edges {{
                node {{
                  title
                  quantity
                  originalUnitPriceSet {{
                    shopMoney {{
                      amount
                      currencyCode
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    data = {"query": query, "variables": {}}
    logger.info(f"Requesting Shopify orders with payload: {json.dumps(data)}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, data=json.dumps(data))
            response.raise_for_status()
            response_json = response.json()
            logger.info(f"Received Shopify API response: {response_json}")

            orders = response_json.get("data", {}).get("orders", {}).get("edges", [])
            shop_name = response_json.get("data", {}).get("shop", {}).get("name")
            
            transformed_orders = [
                _transform_shopify_to_breeze(order["node"], shop_name).model_dump()
                for order in orders
            ]
            
            await params.result_callback(transformed_orders)
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling Shopify API: {e.response.status_code} - {e.response.text}")
            await params.result_callback({"error": f"Shopify API error: {e.response.status_code}", "details": e.response.text})
        except Exception as e:
            logger.error(f"Unexpected error calling Shopify API: {e}")
            await params.result_callback({"error": f"An unexpected error occurred: {e}"})
