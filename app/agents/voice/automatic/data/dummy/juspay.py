import json

dummy_juspay_analytics_today = json.dumps({
        "overall_success_rate_data": {
            "success_rate": 64.33
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "WALLET",
                "success_rate": 66.67,
            },
            {
                "payment_method_type": "CARD",
                "success_rate": 78.95
            },
            {
                "payment_method_type": "CONSUMER_FINANCE",
                "success_rate": 47.37
            },
            {
                "payment_method_type": "CASH",
                "success_rate": 100.0
            },
            {
                "payment_method_type": "UPI",
                "success_rate": 53.92
            },
            {
                "payment_method_type": "NB",
                "success_rate": 0.0
            }
        ],
        "failure_details": [
            {
                "error_message": "FAILED",
                "payment_method_type": "CONSUMER_FINANCE",
                "count": 3
            },
            {
                "error_message": "COD initiated successfully",
                "payment_method_type": "CASH",
                "count": 196
            },
            {
                "error_message": "unable_to_process",
                "payment_method_type": "WALLET",
                "count": 1
            },
            {
                "error_message": "Transaction failed due to insufficient funds.",
                "payment_method_type": "UPI",
                "count": 1
            },
            {
                "error_message": "payment_authorization_error. High response time for remitter bank",
                "payment_method_type": "UPI",
                "count": 1
            },
            {
                "error_message": "Payment was unsuccessful as you could not complete it in time.",
                "payment_method_type": "UPI",
                "count": 48
            },
            {
                "error_message": "payment_authentication_error. Transaction timed out at issuer ACS",
                "payment_method_type": "CARD",
                "count": 1
            },
            {
                "error_message": "payment_processing_error. Collect expired",
                "payment_method_type": "UPI",
                "count": 2
            },
            {
                "error_message": "pending. Transaction Pending",
                "payment_method_type": "UPI",
                "count": 7
            },
            {
                "error_message": "Payment was unsuccessful as the phone number linked to this UPI ID is changed/removed. Try using another method.",
                "payment_method_type": "UPI",
                "count": 1
            },
            {
                "error_message": "invalid_payment_credentials. Expired virtual address",
                "payment_method_type": "UPI",
                "count": 1
            },
            {
                "error_message": "payment_authorization_error. Do not honour",
                "payment_method_type": "CARD",
                "count": 2
            },
            {
                "error_message": "payment_authentication_error. Transaction not permitted to cardholder",
                "payment_method_type": "CARD",
                "count": 1
            },
            {
                "error_message": "invalid_payment_credentials. Invalid MPIN",
                "payment_method_type": "UPI",
                "count": 1
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "WALLET",
                "transaction_count": 6
            },
            {
                "payment_method_type": "CARD",
                "transaction_count": 15
            },
            {
                "payment_method_type": "CONSUMER_FINANCE",
                "transaction_count": 9
            },
            {
                "payment_method_type": "CASH",
                "transaction_count": 196
            },
            {
                "payment_method_type": "UPI",
                "transaction_count": 328
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "WALLET",
                "gmv": 4415.48
            },
            {
                "payment_method_type": "REWARD",
                "gmv": 0.0
            },
            {
                "payment_method_type": "CARD",
                "gmv": 22624.67
            },
            {
                "payment_method_type": "CONSUMER_FINANCE",
                "gmv": 8814.15
            },
            {
                "payment_method_type": "CASH",
                "gmv": 199788.61
            },
            {
                "payment_method_type": "UPI",
                "gmv": 320001.85
            },
            {
                "payment_method_type": "NB",
                "gmv": 0.0
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 735.0
            },
            {
                "payment_method_type": "REWARD",
                "average_ticket_size": 0.0
            },
            {
                "payment_method_type": "CARD",
                "average_ticket_size": 1508.0
            },
            {
                "payment_method_type": "CONSUMER_FINANCE",
                "average_ticket_size": 979.0
            },
            {
                "payment_method_type": "CASH",
                "average_ticket_size": 1019.0
            },
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 911.0
            },
            {
                "payment_method_type": "NB",
                "average_ticket_size": 0.0
            }
        ],
        "errors": []
})

dummy_breeze_analytics_today = json.dumps({
        "businessTotalSalesBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total sales",
                "value": 550432.17,
                "bottomContainerItems": [
                    {
                        "metric": "PREPAID",
                        "rate": 340341.56,
                        "subUnit": "AMOUNT"
                    },
                    {
                        "metric": "COD",
                        "rate": 210090.61,
                        "subUnit": "AMOUNT"
                    },
                    {
                        "metric": "PREPAID(%)",
                        "rate": 61.83,
                        "subUnit": "PERCENTAGE"
                    }
                ],
                "slotProperties": {
                    "componentType": "DONUT_CHART",
                    "value": {
                        "other": 425008.99,
                        "adyogi | CPC_fb": 46011.95,
                        "google | product_sync": 34576.40,
                        "adyogi | CPC_ig": 25226.34,
                        "adyogi | google-performancemax": 6908.65,
                        "facebook | paid": 5557.5,
                        "bik | whatsapp": 2370.13,
                        "bio | ig": 1575.1,
                        "google | search": 1461.1,
                        "JioHotstar | Product1": 1028,
                        "D2C | website": 708
                    },
                    "unit": "AMOUNT",
                    "toolTipText": "Contribution of each marketing channel (UTM source) to total sales",
                    "title": "Sales breakdown",
                    "subTitle": "Total Sales"
                }
            },
            "unit": "AMOUNT"
        },
        "businessTotalOrdersBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Total orders",
                "value": 578,
                "bottomContainerItems": [
                    {
                        "metric": "PREPAID",
                        "rate": 381,
                        "subUnit": "NUMBER"
                    },
                    {
                        "metric": "COD",
                        "rate": 197,
                        "subUnit": "NUMBER"
                    },
                    {
                        "metric": "PREPAID(%)",
                        "rate": 65.92,
                        "subUnit": "PERCENTAGE"
                    }
                ]
            },
            "unit": "NUMBER"
        },
        "businessConversionBreakdown": {
            "componentType": "STATISTICS_CARD_WITH_SLOT",
            "value": {
                "title": "Conversion rate",
                "value": 36.09,
                "bottomContainerItems": [
                    {
                        "metric": "SESSIONS",
                        "rate": 1582,
                        "subUnit": "NUMBER"
                    },
                    {
                        "metric": "ORDERS",
                        "rate": 571,
                        "subUnit": "NUMBER"
                    },
                    {
                        "metric": "TIME TAKEN",
                        "rate": 235,
                        "subUnit": "TIME"
                    }
                ],
                "slotProperties": {
                    "componentType": "BAR_GRAPH_CHART",
                    "value": {
                        "toolTipText": "Conversion Rate",
                        "toolTipDescription": "Track user progression from interaction to purchase, across key stages",
                        "clickedCheckoutButton": 2701,
                        "loggedIn": 1582,
                        "submittedAddress": 1306,
                        "clickedProceedToBuyButton": 820,
                        "placedOrder": 571
                    }
                }
            },
            "unit": "PERCENTAGE"
        },
        "paymentSuccessRate": {
            "componentType": "STATISTICS_CARD",
            "value": 79.09,
            "unit": "PERCENTAGE",
            "toolTipText": "Successful transactions over total attempted transactions"
        },
        "exitSurveyResponseBreakdown": {
            "componentType": "DONUT_CHART",
            "value": {},
            "unit": "NUMBER",
            "toolTipText": "Response received by the people exiting the checkout without placing order",
            "title": "Exit Survey Breakdown",
            "subTitle": "Total Response"
        },
        "averageOrderValue": {
            "componentType": "STATISTICS_CARD",
            "value": 952.3,
            "unit": "AMOUNT",
            "toolTipText": "Total sales over total number of orders"
        },
        "prepaidShare": {
            "componentType": "ADVANCED_STATISTICS_CARD",
            "value": [
                {
                    "metric": "Of total GMV is prepaid",
                    "rate": 61.83,
                    "prepaidSales": 340341.56,
                    "totalSales": 550432.17,
                    "subUnit": "AMOUNT"
                },
                {
                    "metric": "Of total orders placed are prepaid",
                    "rate": 65.92,
                    "prepaidOrders": 381,
                    "totalOrders": 578,
                    "subUnit": "NUMBER"
                }
            ],
            "unit": "PERCENTAGE",
            "toolTipText": "Prepaid share metrics excluding Partial COD orders"
        },
        "prepaidShareWithPartialCOD": {
            "componentType": "ADVANCED_STATISTICS_CARD",
            "value": [
                {
                    "metric": "Of total GMV is prepaid",
                    "rate": 61.83,
                    "prepaidSales": 340341.56,
                    "totalSales": 550432.17,
                    "subUnit": "AMOUNT"
                },
                {
                    "metric": "Of total orders placed are prepaid",
                    "rate": 65.92,
                    "prepaidOrders": 381,
                    "totalOrders": 578,
                    "subUnit": "NUMBER"
                }
            ],
            "unit": "PERCENTAGE",
            "toolTipText": "Prepaid share metrics including Partial COD orders"
        },
        "platformBarGraphData": {
            "componentType": "BAR_GRAPH_CHART",
            "value": {
                "toolTipText": "Conversion Rate by platform",
                "toolTipDescription": "Calculated by ratio of payment started to checkout completed",
                "Facebook": 77.36,
                "Chrome": 80.39,
                "Safari": 79.69,
                "Firefox": 75,
                "Unknown": 0,
                "Instagram": 77.44
            }
        },
        "deviceSubTypeBarGraphData": {
            "componentType": "BAR_GRAPH_CHART",
            "value": {
                "toolTipText": "Conversion Rate by device_sub_type",
                "toolTipDescription": "Calculated by ratio of payment started to checkout completed",
                "Android": 75.83,
                "iPhone": 86.62,
                "Windows": 88.33,
                "Mac": 100,
                "Unknown": 60
            }
        }
})

dummy_juspay_analytics_weekly = json.dumps({
    "overall_success_rate_data": {
        "success_rate": 72.5
    },
    "payment_method_success_rates": [
        {"payment_method_type": "WALLET", "success_rate": 75.0},
        {"payment_method_type": "CARD", "success_rate": 85.0},
        {"payment_method_type": "CONSUMER_FINANCE", "success_rate": 60.0},
        {"payment_method_type": "CASH", "success_rate": 100.0},
        {"payment_method_type": "UPI", "success_rate": 70.0},
        {"payment_method_type": "NB", "success_rate": 50.0}
    ],
    "failure_details": [
        {"error_message": "FAILED", "payment_method_type": "CONSUMER_FINANCE", "count": 80},
        {"error_message": "COD initiated successfully", "payment_method_type": "CASH", "count": 1400},
        {"error_message": "payment_processing_error. Collect expired", "payment_method_type": "UPI", "count": 150},
        {"error_message": "Payment was unsuccessful as you could not complete it in time.", "payment_method_type": "UPI", "count": 300}
    ],
    "success_volume_by_payment_method": [
        {"payment_method_type": "WALLET", "transaction_count": 420},
        {"payment_method_type": "CARD", "transaction_count": 1050},
        {"payment_method_type": "CONSUMER_FINANCE", "transaction_count": 630},
        {"payment_method_type": "CASH", "transaction_count": 1400},
        {"payment_method_type": "UPI", "transaction_count": 2296}
    ],
    "gmv_by_payment_method": [
        {"payment_method_type": "WALLET", "gmv": 30908.36},
        {"payment_method_type": "CARD", "gmv": 158372.69},
        {"payment_method_type": "CONSUMER_FINANCE", "gmv": 61699.05},
        {"payment_method_type": "CASH", "gmv": 1398520.27},
        {"payment_method_type": "UPI", "gmv": 2240012.95}
    ],
    "average_ticket_size_by_payment_method": [
        {"payment_method_type": "WALLET", "average_ticket_size": 735.91},
        {"payment_method_type": "CARD", "average_ticket_size": 1508.31},
        {"payment_method_type": "CONSUMER_FINANCE", "average_ticket_size": 979.35},
        {"payment_method_type": "CASH", "average_ticket_size": 998.94},
        {"payment_method_type": "UPI", "average_ticket_size": 975.61}
    ],
    "errors": []
})

dummy_breeze_analytics_weekly = json.dumps({
    "businessTotalSalesBreakdown": {
        "componentType": "STATISTICS_CARD_WITH_SLOT",
        "value": {
            "title": "Total sales",
            "value": 3859513.32,
            "bottomContainerItems": [
                {"metric": "PREPAID", "rate": 2460993.05, "subUnit": "AMOUNT"},
                {"metric": "COD", "rate": 1398520.27, "subUnit": "AMOUNT"},
                {"metric": "PREPAID(%)", "rate": 63.76, "subUnit": "PERCENTAGE"}
            ],
            "slotProperties": {
                "componentType": "DONUT_CHART",
                "value": {
                    "other": 2975062.93,
                    "adyogi | CPC_fb": 322083.65,
                    "google | product_sync": 242034.8,
                    "adyogi | CPC_ig": 176584.38,
                    "adyogi | google-performancemax": 48360.55,
                    "facebook | paid": 38902.5,
                    "bik | whatsapp": 16590.91,
                    "bio | ig": 11025.7,
                    "google | search": 10227.7,
                    "JioHotstar | Product1": 7196,
                    "D2C | website": 4956
                },
                "unit": "AMOUNT",
                "toolTipText": "Contribution of each marketing channel (UTM source) to total sales",
                "title": "Sales breakdown",
                "subTitle": "Total Sales"
            }
        },
        "unit": "AMOUNT"
    },
    "businessTotalOrdersBreakdown": {
        "componentType": "STATISTICS_CARD_WITH_SLOT",
        "value": {
            "title": "Total orders",
            "value": 4046,
            "bottomContainerItems": [
                {"metric": "PREPAID", "rate": 2646, "subUnit": "NUMBER"},
                {"metric": "COD", "rate": 1400, "subUnit": "NUMBER"},
                {"metric": "PREPAID(%)", "rate": 65.4, "subUnit": "PERCENTAGE"}
            ]
        },
        "unit": "NUMBER"
    },
    "businessConversionBreakdown": {
        "componentType": "STATISTICS_CARD_WITH_SLOT",
        "value": {
            "title": "Conversion rate",
            "value": 35.0,
            "bottomContainerItems": [
                {"metric": "SESSIONS", "rate": 11560, "subUnit": "NUMBER"},
                {"metric": "ORDERS", "rate": 4046, "subUnit": "NUMBER"},
                {"metric": "TIME TAKEN", "rate": 240, "subUnit": "TIME"}
            ],
            "slotProperties": {
                "componentType": "BAR_GRAPH_CHART",
                "value": {
                    "toolTipText": "Conversion Rate",
                    "toolTipDescription": "Track user progression from interaction to purchase, across key stages",
                    "clickedCheckoutButton": 18907,
                    "loggedIn": 11074,
                    "submittedAddress": 9142,
                    "clickedProceedToBuyButton": 5740,
                    "placedOrder": 4046
                }
            }
        },
        "unit": "PERCENTAGE"
    },
    "paymentSuccessRate": {
        "componentType": "STATISTICS_CARD",
        "value": 82.5,
        "unit": "PERCENTAGE",
        "toolTipText": "Successful transactions over total attempted transactions"
    },
    "averageOrderValue": {
        "componentType": "STATISTICS_CARD",
        "value": 953.91,
        "unit": "AMOUNT",
        "toolTipText": "Total sales over total number of orders"
    },
    "adSpendAndRoas": {
        "componentType": "STATISTICS_CARD",
        "value": {
            "adSpend": 500000,
            "roas": 7.72
        },
        "unit": "MIXED",
        "toolTipText": "Total Ad Spend and Return on Ad Spend (ROAS)"
    }
})
