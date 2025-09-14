import json

dummy_juspay_analytics_today = json.dumps(
    {
        "overall_success_rate_data": {"success_rate": 93.5},
        "payment_method_success_rates": [
            {
                "payment_method_type": "WALLET",
                "success_rate": 95.1,
            },
            {"payment_method_type": "DEBIT_CARD", "success_rate": 92.5},
            {"payment_method_type": "UPI", "success_rate": 97.2},
            {"payment_method_type": "CREDIT_CARD", "success_rate": 94.8},
            {"payment_method_type": "NB", "success_rate": 90.3},
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 125,
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 78,
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 29,
            },
            {
                "error_message": "PAYMENT_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 65,
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 54,
            },
            {
                "error_message": "AUTHENTICATION_FAILED",
                "payment_method_type": "DEBIT_CARD",
                "count": 42,
            },
            {
                "error_message": "GATEWAY_ERROR",
                "payment_method_type": "ALL",
                "count": 1,
            },
            {
                "error_message": "OTP_VALIDATION_FAILED",
                "payment_method_type": "CREDIT_CARD",
                "count": 27,
            },
        ],
        "success_volume_by_payment_method": [
            {"payment_method_type": "WALLET", "transaction_count": 980},
            {"payment_method_type": "CREDIT_CARD", "transaction_count": 2780},
            {"payment_method_type": "DEBIT_CARD", "transaction_count": 1950},
            {"payment_method_type": "BUY_NOW_PAY_LATER", "transaction_count": 780},
            {"payment_method_type": "UPI", "transaction_count": 3250},
            {"payment_method_type": "OTHERS", "transaction_count": 320},
        ],
        "gmv_by_payment_method": [
            {"payment_method_type": "WALLET", "gmv": 2180000},
            {"payment_method_type": "REWARD", "gmv": 0.0},
            {"payment_method_type": "CREDIT_CARD", "gmv": 12850000},
            {"payment_method_type": "DEBIT_CARD", "gmv": 6320000},
            {"payment_method_type": "UPI", "gmv": 8750000},
            {"payment_method_type": "NB", "gmv": 4120000},
            {"payment_method_type": "BUY_NOW_PAY_LATER", "gmv": 3950000},
            {"payment_method_type": "OTHERS", "gmv": 980000},
        ],
        "average_ticket_size_by_payment_method": [
            {"payment_method_type": "WALLET", "average_ticket_size": 2224.49},
            {"payment_method_type": "CREDIT_CARD", "average_ticket_size": 4620.14},
            {"payment_method_type": "UPI", "average_ticket_size": 2692.31},
            {"payment_method_type": "DEBIT_CARD", "average_ticket_size": 3241.03},
            {"payment_method_type": "NB", "average_ticket_size": 2901.41},
            {"payment_method_type": "BUY_NOW_PAY_LATER", "average_ticket_size": 5064.1},
        ],
        "errors": [],
    }
)

dummy_breeze_analytics_today = json.dumps(
    {
        "businessTotalSalesBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total sales",
                "value": 481462.77,
                "bottomContainerItems": [
                    {"metric": "PREPAID", "rate": 259428.41, "subUnit": "AMOUNT"},
                    {"metric": "COD", "rate": 222034.36000000002, "subUnit": "AMOUNT"},
                    {"metric": "PREPAID(%)", "rate": 53.88, "subUnit": "PERCENTAGE"},
                ],
                "slotProperties": {
                    "componentType": "DONUT_CHART",
                    "value": {
                        "other": 126399.26,
                        "adyogi | CPC_fb": 118874.68,
                        "google | product_sync": 33994.43,
                        "adyogi | CPC_ig": 74847.33,
                        "adyogi | google-performancemax": 79295.01,
                        "facebook | paid": 2256.25,
                        "bik | whatsapp": 7801.37,
                        "bio | ig": 1575.1,
                        "google | search": 24161.48,
                        "JioHotstar | Product1": 1028,
                        "D2C | website": 673,
                    },
                    "unit": "AMOUNT",
                    "toolTipText": "Contribution of each marketing channel (UTM source) to total sales",
                    "title": "Sales breakdown",
                    "subTitle": "Total Sales",
                },
            },
            "unit": "AMOUNT",
        },
        "businessTotalOrdersBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total orders",
                "value": 441,
                "bottomContainerItems": [
                    {"metric": "PREPAID", "rate": 262, "subUnit": "NUMBER"},
                    {"metric": "COD", "rate": 179, "subUnit": "NUMBER"},
                    {"metric": "PREPAID(%)", "rate": 59.41, "subUnit": "PERCENTAGE"},
                ],
            },
            "unit": "NUMBER",
        },
        "businessConversionBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Conversion rate",
                "value": 35.3,
                "bottomContainerItems": [
                    {"metric": "SESSIONS", "rate": 1252, "subUnit": "NUMBER"},
                    {"metric": "ORDERS", "rate": 442, "subUnit": "NUMBER"},
                    {"metric": "TIME TAKEN", "rate": 224, "subUnit": "TIME"},
                ],
                "slotProperties": {
                    "componentType": "BAR_GRAPH_CHART",
                    "value": {
                        "toolTipText": "Conversion Rate",
                        "toolTipDescription": "Track user progression from interaction to purchase, across key stages",
                        "clickedCheckoutButton": 2219,
                        "loggedIn": 1252,
                        "submittedAddress": 1082,
                        "clickedProceedToBuyButton": 688,
                        "placedOrder": 442,
                    },
                },
            },
            "unit": "PERCENTAGE",
        },
        "paymentSuccessRate": {
            "componentType": "STATISTICS_CARD",
            "value": 73.54,
            "unit": "PERCENTAGE",
            "toolTipText": "Successful transactions over total attempted transactions",
        },
        "exitSurveyResponseBreakdown": {
            "componentType": "DONUT_CHART",
            "value": {},
            "unit": "NUMBER",
            "toolTipText": "Response received by the people exiting the checkout without placing order",
            "title": "Exit Survey Breakdown",
            "subTitle": "Total Response",
        },
        "averageOrderValue": {
            "componentType": "STATISTICS_CARD",
            "value": 1091.75,
            "unit": "AMOUNT",
            "toolTipText": "Total sales over total number of orders",
        },
        "prepaidShare": {
            "componentType": "ADVANCED_STATISTICS_CARD",
            "value": [
                {
                    "metric": "Of total GMV is prepaid",
                    "rate": 53.88,
                    "prepaidSales": 259428.41,
                    "totalSales": 481462.77,
                    "subUnit": "AMOUNT",
                },
                {
                    "metric": "Of total orders placed are prepaid",
                    "rate": 59.41,
                    "prepaidOrders": 262,
                    "totalOrders": 441,
                    "subUnit": "NUMBER",
                },
            ],
            "unit": "PERCENTAGE",
            "toolTipText": "Prepaid share metrics excluding Partial COD orders",
        },
        "prepaidShareWithPartialCOD": {
            "componentType": "ADVANCED_STATISTICS_CARD",
            "value": [
                {
                    "metric": "Of total GMV is prepaid",
                    "rate": 53.88,
                    "prepaidSales": 259428.41,
                    "totalSales": 481462.77,
                    "subUnit": "AMOUNT",
                },
                {
                    "metric": "Of total orders placed are prepaid",
                    "rate": 59.41,
                    "prepaidOrders": 262,
                    "totalOrders": 441,
                    "subUnit": "NUMBER",
                },
            ],
            "unit": "PERCENTAGE",
            "toolTipText": "Prepaid share metrics including Partial COD orders",
        },
        "platformBarGraphData": {
            "componentType": "BAR_GRAPH_CHART",
            "value": {
                "toolTipText": "Conversion Rate by platform",
                "toolTipDescription": "Calculated by ratio of payment started to checkout completed",
                "Facebook": 73.08,
                "Chrome": 72.49,
                "Safari": 83.72,
                "Firefox": 100,
                "Unknown": 0,
                "Instagram": 72.37,
            },
        },
        "deviceSubTypeBarGraphData": {
            "componentType": "BAR_GRAPH_CHART",
            "value": {
                "toolTipText": "Conversion Rate by device_sub_type",
                "toolTipDescription": "Calculated by ratio of payment started to checkout completed",
                "Android": 72.28,
                "iPhone": 81.52,
                "Windows": 69.57,
                "Mac": 72.73,
                "Unknown": 100,
            },
        },
    }
)

dummy_juspay_analytics_weekly = json.dumps(
    {
        "overall_success_rate_data": {"success_rate": 72.5},
        "payment_method_success_rates": [
            {"payment_method_type": "WALLET", "success_rate": 75.0},
            {"payment_method_type": "CARD", "success_rate": 85.0},
            {"payment_method_type": "CONSUMER_FINANCE", "success_rate": 60.0},
            {"payment_method_type": "CASH", "success_rate": 100.0},
            {"payment_method_type": "UPI", "success_rate": 70.0},
            {"payment_method_type": "NB", "success_rate": 50.0},
        ],
        "failure_details": [
            {
                "error_message": "FAILED",
                "payment_method_type": "CONSUMER_FINANCE",
                "count": 80,
            },
            {
                "error_message": "COD initiated successfully",
                "payment_method_type": "CASH",
                "count": 1400,
            },
            {
                "error_message": "payment_processing_error. Collect expired",
                "payment_method_type": "UPI",
                "count": 150,
            },
            {
                "error_message": "Payment was unsuccessful as you could not complete it in time.",
                "payment_method_type": "UPI",
                "count": 300,
            },
        ],
        "success_volume_by_payment_method": [
            {"payment_method_type": "WALLET", "transaction_count": 420},
            {"payment_method_type": "CARD", "transaction_count": 1050},
            {"payment_method_type": "CONSUMER_FINANCE", "transaction_count": 630},
            {"payment_method_type": "CASH", "transaction_count": 1400},
            {"payment_method_type": "UPI", "transaction_count": 2296},
        ],
        "gmv_by_payment_method": [
            {"payment_method_type": "WALLET", "gmv": 30908.36},
            {"payment_method_type": "CARD", "gmv": 158372.69},
            {"payment_method_type": "CONSUMER_FINANCE", "gmv": 61699.05},
            {"payment_method_type": "CASH", "gmv": 1398520.27},
            {"payment_method_type": "UPI", "gmv": 2240012.95},
        ],
        "average_ticket_size_by_payment_method": [
            {"payment_method_type": "WALLET", "average_ticket_size": 735.91},
            {"payment_method_type": "CARD", "average_ticket_size": 1508.31},
            {"payment_method_type": "CONSUMER_FINANCE", "average_ticket_size": 979.35},
            {"payment_method_type": "CASH", "average_ticket_size": 998.94},
            {"payment_method_type": "UPI", "average_ticket_size": 975.61},
        ],
        "errors": [],
    }
)

dummy_breeze_analytics_weekly = json.dumps(
    {
        "businessTotalSalesBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total sales",
                "value": 481462.77,
                "bottomContainerItems": [
                    {"metric": "PREPAID", "rate": 259428.41, "subUnit": "AMOUNT"},
                    {"metric": "COD", "rate": 222034.36000000002, "subUnit": "AMOUNT"},
                    {"metric": "PREPAID(%)", "rate": 53.88, "subUnit": "PERCENTAGE"},
                ],
                "slotProperties": {
                    "componentType": "DONUT_CHART",
                    "value": {
                        "other": 126399.26,
                        "adyogi | CPC_fb": 118874.68,
                        "google | product_sync": 33994.43,
                        "adyogi | CPC_ig": 176584.38,
                        "adyogi | google-performancemax": 79295.01,
                        "facebook | paid": 2256.25,
                        "bik | whatsapp": 7081.37,
                        "bio | ig": 11025.7,
                        "google | search": 24161.48,
                        "JioHotstar | Product1": 7196,
                        "D2C | website": 673,
                    },
                    "unit": "AMOUNT",
                    "toolTipText": "Contribution of each marketing channel (UTM source) to total sales",
                    "title": "Sales breakdown",
                    "subTitle": "Total Sales",
                },
            },
            "unit": "AMOUNT",
        },
        "businessTotalOrdersBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total orders",
                "value": 441,
                "bottomContainerItems": [
                    {"metric": "PREPAID", "rate": 262, "subUnit": "NUMBER"},
                    {"metric": "COD", "rate": 179, "subUnit": "NUMBER"},
                    {"metric": "PREPAID(%)", "rate": 59.41, "subUnit": "PERCENTAGE"},
                ],
            },
            "unit": "NUMBER",
        },
        "businessConversionBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Conversion rate",
                "value": 35.3,
                "bottomContainerItems": [
                    {"metric": "SESSIONS", "rate": 1252, "subUnit": "NUMBER"},
                    {"metric": "ORDERS", "rate": 442, "subUnit": "NUMBER"},
                    {"metric": "TIME TAKEN", "rate": 224, "subUnit": "TIME"},
                ],
                "slotProperties": {
                    "componentType": "BAR_GRAPH_CHART",
                    "value": {
                        "toolTipText": "Conversion Rate",
                        "toolTipDescription": "Track user progression from interaction to purchase, across key stages",
                        "clickedCheckoutButton": 2219,
                        "loggedIn": 1252,
                        "submittedAddress": 1082,
                        "clickedProceedToBuyButton": 688,
                        "placedOrder": 442,
                    },
                },
            },
            "unit": "PERCENTAGE",
        },
        "paymentSuccessRate": {
            "componentType": "STATISTICS_CARD",
            "value": 73.54,
            "unit": "PERCENTAGE",
            "toolTipText": "Successful transactions over total attempted transactions",
        },
        "averageOrderValue": {
            "componentType": "STATISTICS_CARD",
            "value": 1091.75,
            "unit": "AMOUNT",
            "toolTipText": "Total sales over total number of orders",
        },
        "adSpendAndRoas": {
            "componentType": "STATISTICS_CARD",
            "value": {"adSpend": 500000, "roas": 7.72},
            "unit": "MIXED",
            "toolTipText": "Total Ad Spend and Return on Ad Spend (ROAS)",
        },
    }
)
