"""
ACME Store Juspay Analytics Data - 31 Days
Complete time-based payment data for merchant_id="acme-store-demo"
Each entry represents one day (index 0-30) with comprehensive payment analytics
"""

# 31 days of comprehensive Juspay analytics data (index 0-30)
ACME_JUSPAY_DATA = [
    {
        "overall_success_rate_data": {
            "success_rate": 69.34,
            "total_attempts": 3083,
            "successful_transactions": 2751,
            "failed_transactions": 332,
            "processing_time_avg": 42.5,
            "retry_success_rate": 34.2,
            "peak_hour_sr": 91.3,
            "off_peak_sr": 87.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.1,
                "total_attempts": 1079,
                "successful": 994,
                "failed": 85,
                "avg_processing_time": 28.5,
                "peak_volume_hour": "14:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 91.45,
                "total_attempts": 1233,
                "successful": 1128,
                "failed": 105,
                "avg_processing_time": 41.2,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.33,
                "total_attempts": 462,
                "successful": 403,
                "failed": 59,
                "avg_processing_time": 39.8,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 79.77,
                "total_attempts": 185,
                "successful": 148,
                "failed": 37,
                "avg_processing_time": 74.3,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 85.91,
                "total_attempts": 124,
                "successful": 107,
                "failed": 17,
                "avg_processing_time": 23.7,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 85,
                "percentage": 25.6,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 45,
                "percentage": 13.6,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 38,
                "percentage": 11.4,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 31,
                "percentage": 9.3,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 28,
                "percentage": 8.4,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 22,
                "percentage": 6.6,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 994,
                "percentage": 36.1,
                "peak_hour_volume": 127,
                "avg_transaction_value": 4142.33,
                "repeat_customer_rate": 68.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1128,
                "percentage": 41.0,
                "peak_hour_volume": 145,
                "avg_transaction_value": 5890.45,
                "repeat_customer_rate": 72.3
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 403,
                "percentage": 14.6,
                "peak_hour_volume": 52,
                "avg_transaction_value": 3456.78,
                "repeat_customer_rate": 58.9
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 148,
                "percentage": 5.4,
                "peak_hour_volume": 19,
                "avg_transaction_value": 6234.89,
                "repeat_customer_rate": 45.3
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 107,
                "percentage": 3.9,
                "peak_hour_volume": 14,
                "avg_transaction_value": 2890.12,
                "repeat_customer_rate": 61.7
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4117462,
                "percentage": 31.5,
                "avg_order_value": 4142.33,
                "growth_rate": 12.8,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6644468,
                "percentage": 50.8,
                "avg_order_value": 5890.45,
                "growth_rate": 8.4,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1393083,
                "percentage": 10.7,
                "avg_order_value": 3456.78,
                "growth_rate": 5.2,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 922764,
                "percentage": 7.1,
                "avg_order_value": 6234.89,
                "growth_rate": -2.1,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 309043,
                "percentage": 2.4,
                "avg_order_value": 2890.12,
                "growth_rate": 18.7,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4142.33,
                "median_ticket_size": 3890.5,
                "percentile_90": 7234.8,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5890.45,
                "median_ticket_size": 5234.2,
                "percentile_90": 11456.3,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3456.78,
                "median_ticket_size": 2987.4,
                "percentile_90": 6789.6,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6234.89,
                "median_ticket_size": 5678.9,
                "percentile_90": 12345.7,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 2890.12,
                "median_ticket_size": 2456.8,
                "percentile_90": 5234.6,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 93.4,
                "transaction_volume": 1541,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 45.2
            },
            "mobile": {
                "success_rate": 87.8,
                "transaction_volume": 1233,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 38.7
            },
            "tablet": {
                "success_rate": 89.1,
                "transaction_volume": 309,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 42.1
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "14:00-15:00",
                    "20:00-21:00",
                    "21:00-22:00"
                ],
                "success_rate_by_hour": {
                    "morning": 91.2,
                    "afternoon": 89.8,
                    "evening": 88.4,
                    "night": 85.6
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 89.7,
                "weekend_sr": 87.9,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.73,
            "total_attempts": 3215,
            "successful_transactions": 2898,
            "failed_transactions": 317,
            "processing_time_avg": 41.8,
            "retry_success_rate": 36.4,
            "peak_hour_sr": 92.1,
            "off_peak_sr": 88.5
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.2,
                "total_attempts": 1124,
                "successful": 1047,
                "failed": 77,
                "avg_processing_time": 27.8,
                "peak_volume_hour": "15:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.11,
                "total_attempts": 1287,
                "successful": 1185,
                "failed": 102,
                "avg_processing_time": 40.5,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 88.67,
                "total_attempts": 482,
                "successful": 427,
                "failed": 55,
                "avg_processing_time": 38.9,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 81.45,
                "total_attempts": 193,
                "successful": 157,
                "failed": 36,
                "avg_processing_time": 72.1,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.89,
                "total_attempts": 129,
                "successful": 113,
                "failed": 16,
                "avg_processing_time": 22.4,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 78,
                "percentage": 24.6,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 52,
                "percentage": 16.4,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 35,
                "percentage": 11.0,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 29,
                "percentage": 9.1,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 26,
                "percentage": 8.2,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 19,
                "percentage": 6.0,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1047,
                "percentage": 36.1,
                "peak_hour_volume": 134,
                "avg_transaction_value": 4198.67,
                "repeat_customer_rate": 69.2
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1185,
                "percentage": 40.9,
                "peak_hour_volume": 152,
                "avg_transaction_value": 5956.23,
                "repeat_customer_rate": 73.1
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 427,
                "percentage": 14.7,
                "peak_hour_volume": 55,
                "avg_transaction_value": 3512.45,
                "repeat_customer_rate": 59.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 157,
                "percentage": 5.4,
                "peak_hour_volume": 20,
                "avg_transaction_value": 6312.78,
                "repeat_customer_rate": 46.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 113,
                "percentage": 3.9,
                "peak_hour_volume": 15,
                "avg_transaction_value": 2934.56,
                "repeat_customer_rate": 62.3
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4396085,
                "percentage": 31.8,
                "avg_order_value": 4198.67,
                "growth_rate": 6.8,
                "regional_preference": {
                    "North": 39.1,
                    "South": 43.2,
                    "West": 36.8,
                    "East": 29.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 7058132,
                "percentage": 51.1,
                "avg_order_value": 5956.23,
                "growth_rate": 6.2,
                "regional_preference": {
                    "North": 53.1,
                    "South": 49.8,
                    "West": 55.2,
                    "East": 47.5
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1499816,
                "percentage": 10.9,
                "avg_order_value": 3512.45,
                "growth_rate": 7.7,
                "regional_preference": {
                    "North": 29.3,
                    "South": 32.1,
                    "West": 27.6,
                    "East": 34.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 991106,
                "percentage": 7.2,
                "avg_order_value": 6312.78,
                "growth_rate": 7.4,
                "regional_preference": {
                    "North": 16.1,
                    "South": 13.7,
                    "West": 19.8,
                    "East": 22.1
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 331605,
                "percentage": 2.4,
                "avg_order_value": 2934.56,
                "growth_rate": 7.3,
                "regional_preference": {
                    "North": 23.2,
                    "South": 26.9,
                    "West": 20.3,
                    "East": 17.1
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4198.67,
                "median_ticket_size": 3945.2,
                "percentile_90": 7345.6,
                "category_preference": {
                    "Electronics": 46.1,
                    "Fashion": 32.8,
                    "Home": 21.1
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5956.23,
                "median_ticket_size": 5298.4,
                "percentile_90": 11567.2,
                "category_preference": {
                    "Electronics": 59.3,
                    "Fashion": 27.8,
                    "Home": 12.9
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3512.45,
                "median_ticket_size": 3045.8,
                "percentile_90": 6890.3,
                "category_preference": {
                    "Electronics": 32.2,
                    "Fashion": 41.9,
                    "Home": 25.9
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6312.78,
                "median_ticket_size": 5789.6,
                "percentile_90": 12456.8,
                "category_preference": {
                    "Electronics": 68.5,
                    "Fashion": 18.2,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 2934.56,
                "median_ticket_size": 2512.3,
                "percentile_90": 5345.7,
                "category_preference": {
                    "Electronics": 29.1,
                    "Fashion": 46.3,
                    "Home": 24.6
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 94.1,
                "transaction_volume": 1608,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 44.8
            },
            "mobile": {
                "success_rate": 88.5,
                "transaction_volume": 1287,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 37.9
            },
            "tablet": {
                "success_rate": 89.8,
                "transaction_volume": 320,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 41.7
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "15:00-16:00",
                    "19:00-20:00",
                    "20:00-21:00"
                ],
                "success_rate_by_hour": {
                    "morning": 91.8,
                    "afternoon": 90.4,
                    "evening": 89.1,
                    "night": 86.2
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 90.3,
                "weekend_sr": 88.6,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 72.64,
            "total_attempts": 3156,
            "successful_transactions": 2798,
            "failed_transactions": 358,
            "processing_time_avg": 43.2,
            "retry_success_rate": 35.8,
            "peak_hour_sr": 91.4,
            "off_peak_sr": 87.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.8,
                "total_attempts": 1104,
                "successful": 1014,
                "failed": 90,
                "avg_processing_time": 29.2,
                "peak_volume_hour": "14:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.34,
                "total_attempts": 1263,
                "successful": 1141,
                "failed": 122,
                "avg_processing_time": 42.1,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 86.89,
                "total_attempts": 473,
                "successful": 411,
                "failed": 62,
                "avg_processing_time": 40.2,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 80.23,
                "total_attempts": 189,
                "successful": 152,
                "failed": 37,
                "avg_processing_time": 75.4,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 86.72,
                "total_attempts": 127,
                "successful": 110,
                "failed": 17,
                "avg_processing_time": 24.1,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 89,
                "percentage": 24.9,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 58,
                "percentage": 16.2,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 42,
                "percentage": 11.7,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 34,
                "percentage": 9.5,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 31,
                "percentage": 8.7,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 24,
                "percentage": 6.7,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1014,
                "percentage": 36.2,
                "peak_hour_volume": 129,
                "avg_transaction_value": 4234.56,
                "repeat_customer_rate": 68.9
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1141,
                "percentage": 40.8,
                "peak_hour_volume": 148,
                "avg_transaction_value": 6023.78,
                "repeat_customer_rate": 72.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 411,
                "percentage": 14.7,
                "peak_hour_volume": 53,
                "avg_transaction_value": 3567.89,
                "repeat_customer_rate": 60.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 152,
                "percentage": 5.4,
                "peak_hour_volume": 19,
                "avg_transaction_value": 6456.23,
                "repeat_customer_rate": 47.2
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 110,
                "percentage": 3.9,
                "peak_hour_volume": 14,
                "avg_transaction_value": 2987.45,
                "repeat_customer_rate": 63.1
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4293844,
                "percentage": 31.2,
                "avg_order_value": 4234.56,
                "growth_rate": 4.3,
                "regional_preference": {
                    "North": 39.8,
                    "South": 44.1,
                    "West": 37.5,
                    "East": 30.2
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6873102,
                "percentage": 49.9,
                "avg_order_value": 6023.78,
                "growth_rate": 3.4,
                "regional_preference": {
                    "North": 54.2,
                    "South": 50.3,
                    "West": 56.1,
                    "East": 48.2
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1466402,
                "percentage": 10.7,
                "avg_order_value": 3567.89,
                "growth_rate": 5.2,
                "regional_preference": {
                    "North": 30.1,
                    "South": 32.8,
                    "West": 28.4,
                    "East": 35.1
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 981347,
                "percentage": 7.1,
                "avg_order_value": 6456.23,
                "growth_rate": 6.3,
                "regional_preference": {
                    "North": 16.8,
                    "South": 14.2,
                    "West": 20.3,
                    "East": 22.7
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 328620,
                "percentage": 2.4,
                "avg_order_value": 2987.45,
                "growth_rate": 6.3,
                "regional_preference": {
                    "North": 24.1,
                    "South": 27.6,
                    "West": 21.2,
                    "East": 17.8
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4234.56,
                "median_ticket_size": 3895.79,
                "percentile_90": 7410.48,
                "category_preference": {
                    "Electronics": 46.8,
                    "Fashion": 31.5,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6023.78,
                "median_ticket_size": 5361.16,
                "percentile_90": 11746.37,
                "category_preference": {
                    "Electronics": 60.1,
                    "Fashion": 27.4,
                    "Home": 12.5
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3567.89,
                "median_ticket_size": 3104.06,
                "percentile_90": 7064.4,
                "category_preference": {
                    "Electronics": 33.1,
                    "Fashion": 41.2,
                    "Home": 25.7
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6456.23,
                "median_ticket_size": 6004.3,
                "percentile_90": 12847.9,
                "category_preference": {
                    "Electronics": 69.2,
                    "Fashion": 17.8,
                    "Home": 13.0
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 2987.45,
                "median_ticket_size": 2539.33,
                "percentile_90": 5526.78,
                "category_preference": {
                    "Electronics": 29.8,
                    "Fashion": 45.9,
                    "Home": 24.3
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 93.8,
                "transaction_volume": 1578,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 47.2
            },
            "mobile": {
                "success_rate": 86.4,
                "transaction_volume": 1262,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 40.7
            },
            "tablet": {
                "success_rate": 88.9,
                "transaction_volume": 316,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 44.2
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "14:00-15:00",
                    "21:00-22:00",
                    "22:00-23:00"
                ],
                "success_rate_by_hour": {
                    "morning": 91.5,
                    "afternoon": 89.9,
                    "evening": 87.9,
                    "night": 85.6
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 89.9,
                "weekend_sr": 87.4,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 68.63,
            "total_attempts": 3298,
            "successful_transactions": 3013,
            "failed_transactions": 285,
            "processing_time_avg": 40.8,
            "retry_success_rate": 38.2,
            "peak_hour_sr": 93.8,
            "off_peak_sr": 89.7
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.1,
                "total_attempts": 1154,
                "successful": 1086,
                "failed": 68,
                "avg_processing_time": 26.9,
                "peak_volume_hour": "15:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.67,
                "total_attempts": 1319,
                "successful": 1222,
                "failed": 97,
                "avg_processing_time": 39.8,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 89.12,
                "total_attempts": 494,
                "successful": 440,
                "failed": 54,
                "avg_processing_time": 38.6,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 82.89,
                "total_attempts": 197,
                "successful": 163,
                "failed": 34,
                "avg_processing_time": 73.2,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 89.55,
                "total_attempts": 134,
                "successful": 120,
                "failed": 14,
                "avg_processing_time": 22.8,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 72,
                "percentage": 25.3,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 46,
                "percentage": 16.1,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 31,
                "percentage": 10.9,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 26,
                "percentage": 9.1,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 24,
                "percentage": 8.4,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 18,
                "percentage": 6.3,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1086,
                "percentage": 36.0,
                "peak_hour_volume": 138,
                "avg_transaction_value": 4298.34,
                "repeat_customer_rate": 70.1
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1222,
                "percentage": 40.6,
                "peak_hour_volume": 158,
                "avg_transaction_value": 6189.45,
                "repeat_customer_rate": 74.2
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 440,
                "percentage": 14.6,
                "peak_hour_volume": 57,
                "avg_transaction_value": 3634.78,
                "repeat_customer_rate": 61.8
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 163,
                "percentage": 5.4,
                "peak_hour_volume": 21,
                "avg_transaction_value": 6578.9,
                "repeat_customer_rate": 48.9
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 120,
                "percentage": 4.0,
                "peak_hour_volume": 15,
                "avg_transaction_value": 3056.78,
                "repeat_customer_rate": 64.5
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4668031,
                "percentage": 32.1,
                "avg_order_value": 4298.34,
                "growth_rate": 8.7,
                "regional_preference": {
                    "North": 40.5,
                    "South": 45.3,
                    "West": 38.2,
                    "East": 31.1
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 7563482,
                "percentage": 52.0,
                "avg_order_value": 6189.45,
                "growth_rate": 10.0,
                "regional_preference": {
                    "North": 55.7,
                    "South": 51.8,
                    "West": 57.6,
                    "East": 49.1
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1599302,
                "percentage": 11.0,
                "avg_order_value": 3634.78,
                "growth_rate": 9.1,
                "regional_preference": {
                    "North": 31.2,
                    "South": 34.1,
                    "West": 29.3,
                    "East": 36.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1072361,
                "percentage": 7.4,
                "avg_order_value": 6578.9,
                "growth_rate": 9.3,
                "regional_preference": {
                    "North": 17.5,
                    "South": 15.1,
                    "West": 21.2,
                    "East": 23.8
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 366813,
                "percentage": 2.5,
                "avg_order_value": 3056.78,
                "growth_rate": 11.7,
                "regional_preference": {
                    "North": 25.2,
                    "South": 28.7,
                    "West": 22.1,
                    "East": 18.9
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4298.34,
                "median_ticket_size": 3954.45,
                "percentile_90": 7522.21,
                "category_preference": {
                    "Electronics": 47.5,
                    "Fashion": 30.8,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6189.45,
                "median_ticket_size": 5508.61,
                "percentile_90": 11999.66,
                "category_preference": {
                    "Electronics": 61.4,
                    "Fashion": 26.2,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3634.78,
                "median_ticket_size": 3163.06,
                "percentile_90": 7196.86,
                "category_preference": {
                    "Electronics": 34.8,
                    "Fashion": 40.1,
                    "Home": 25.1
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6578.9,
                "median_ticket_size": 6118.38,
                "percentile_90": 13089.97,
                "category_preference": {
                    "Electronics": 70.6,
                    "Fashion": 16.9,
                    "Home": 12.5
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3056.78,
                "median_ticket_size": 2598.26,
                "percentile_90": 5655.04,
                "category_preference": {
                    "Electronics": 30.5,
                    "Fashion": 46.8,
                    "Home": 22.7
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 95.2,
                "transaction_volume": 1649,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 44.8
            },
            "mobile": {
                "success_rate": 89.1,
                "transaction_volume": 1318,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 37.3
            },
            "tablet": {
                "success_rate": 91.6,
                "transaction_volume": 331,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 40.8
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "15:00-16:00",
                    "20:00-21:00",
                    "21:00-22:00"
                ],
                "success_rate_by_hour": {
                    "morning": 93.2,
                    "afternoon": 91.8,
                    "evening": 90.4,
                    "night": 88.1
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 92.1,
                "weekend_sr": 89.8,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.19,
            "total_attempts": 3089,
            "successful_transactions": 2715,
            "failed_transactions": 374,
            "processing_time_avg": 44.5,
            "retry_success_rate": 33.7,
            "peak_hour_sr": 90.2,
            "off_peak_sr": 86.4
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 90.45,
                "total_attempts": 1081,
                "successful": 978,
                "failed": 103,
                "avg_processing_time": 30.1,
                "peak_volume_hour": "16:00-17:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 88.78,
                "total_attempts": 1235,
                "successful": 1096,
                "failed": 139,
                "avg_processing_time": 43.4,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 85.23,
                "total_attempts": 463,
                "successful": 395,
                "failed": 68,
                "avg_processing_time": 41.3,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 79.12,
                "total_attempts": 185,
                "successful": 146,
                "failed": 39,
                "avg_processing_time": 76.8,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 84.78,
                "total_attempts": 125,
                "successful": 106,
                "failed": 19,
                "avg_processing_time": 25.4,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 96,
                "percentage": 25.7,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 63,
                "percentage": 16.8,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 45,
                "percentage": 12.0,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 37,
                "percentage": 9.9,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 33,
                "percentage": 8.8,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 26,
                "percentage": 7.0,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 978,
                "percentage": 36.0,
                "peak_hour_volume": 124,
                "avg_transaction_value": 4167.89,
                "repeat_customer_rate": 67.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1096,
                "percentage": 40.4,
                "peak_hour_volume": 142,
                "avg_transaction_value": 5923.45,
                "repeat_customer_rate": 71.5
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 395,
                "percentage": 14.6,
                "peak_hour_volume": 51,
                "avg_transaction_value": 3489.23,
                "repeat_customer_rate": 58.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 146,
                "percentage": 5.4,
                "peak_hour_volume": 19,
                "avg_transaction_value": 6334.67,
                "repeat_customer_rate": 45.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 106,
                "percentage": 3.9,
                "peak_hour_volume": 13,
                "avg_transaction_value": 2923.56,
                "repeat_customer_rate": 61.9
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4076199,
                "percentage": 31.4,
                "avg_order_value": 4167.89,
                "growth_rate": -12.7,
                "regional_preference": {
                    "North": 38.9,
                    "South": 43.7,
                    "West": 36.1,
                    "East": 29.8
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6492102,
                "percentage": 50.0,
                "avg_order_value": 5923.45,
                "growth_rate": -14.2,
                "regional_preference": {
                    "North": 53.4,
                    "South": 49.2,
                    "West": 55.8,
                    "East": 47.3
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1378246,
                "percentage": 10.6,
                "avg_order_value": 3489.23,
                "growth_rate": -13.8,
                "regional_preference": {
                    "North": 29.7,
                    "South": 32.4,
                    "West": 27.8,
                    "East": 34.9
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 924862,
                "percentage": 7.1,
                "avg_order_value": 6334.67,
                "growth_rate": -13.7,
                "regional_preference": {
                    "North": 16.2,
                    "South": 13.8,
                    "West": 19.7,
                    "East": 22.4
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 309897,
                "percentage": 2.4,
                "avg_order_value": 2923.56,
                "growth_rate": -15.5,
                "regional_preference": {
                    "North": 23.5,
                    "South": 27.1,
                    "West": 20.8,
                    "East": 17.4
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4167.89,
                "median_ticket_size": 3834.46,
                "percentile_90": 7293.81,
                "category_preference": {
                    "Electronics": 45.9,
                    "Fashion": 32.4,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5923.45,
                "median_ticket_size": 5271.87,
                "percentile_90": 11550.72,
                "category_preference": {
                    "Electronics": 58.2,
                    "Fashion": 29.1,
                    "Home": 12.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3489.23,
                "median_ticket_size": 3036.83,
                "percentile_90": 6908.65,
                "category_preference": {
                    "Electronics": 32.7,
                    "Fashion": 42.8,
                    "Home": 24.5
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6334.67,
                "median_ticket_size": 5891.24,
                "percentile_90": 12603.35,
                "category_preference": {
                    "Electronics": 68.9,
                    "Fashion": 18.4,
                    "Home": 12.7
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 2923.56,
                "median_ticket_size": 2485.03,
                "percentile_90": 5408.58,
                "category_preference": {
                    "Electronics": 28.9,
                    "Fashion": 46.2,
                    "Home": 24.9
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 92.1,
                "transaction_volume": 1544,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 48.5
            },
            "mobile": {
                "success_rate": 85.8,
                "transaction_volume": 1235,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 41.0
            },
            "tablet": {
                "success_rate": 87.4,
                "transaction_volume": 310,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 45.5
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "16:00-17:00",
                    "19:00-20:00",
                    "22:00-23:00"
                ],
                "success_rate_by_hour": {
                    "morning": 90.5,
                    "afternoon": 88.7,
                    "evening": 86.9,
                    "night": 84.2
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 88.6,
                "weekend_sr": 86.1,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.75,
            "total_attempts": 3387,
            "successful_transactions": 3159,
            "failed_transactions": 228,
            "processing_time_avg": 38.9,
            "retry_success_rate": 41.3,
            "peak_hour_sr": 95.7,
            "off_peak_sr": 91.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 95.8,
                "total_attempts": 1186,
                "successful": 1136,
                "failed": 50,
                "avg_processing_time": 25.4,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 94.12,
                "total_attempts": 1354,
                "successful": 1274,
                "failed": 80,
                "avg_processing_time": 37.2,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 91.78,
                "total_attempts": 508,
                "successful": 466,
                "failed": 42,
                "avg_processing_time": 36.1,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 85.45,
                "total_attempts": 203,
                "successful": 174,
                "failed": 29,
                "avg_processing_time": 69.8,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 92.34,
                "total_attempts": 136,
                "successful": 126,
                "failed": 10,
                "avg_processing_time": 20.9,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 58,
                "percentage": 25.4,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 36,
                "percentage": 15.8,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 23,
                "percentage": 10.1,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 19,
                "percentage": 8.3,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 17,
                "percentage": 7.5,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 12,
                "percentage": 5.3,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1136,
                "percentage": 36.0,
                "peak_hour_volume": 145,
                "avg_transaction_value": 4389.67,
                "repeat_customer_rate": 72.3
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1274,
                "percentage": 40.3,
                "peak_hour_volume": 165,
                "avg_transaction_value": 6267.89,
                "repeat_customer_rate": 75.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 466,
                "percentage": 14.7,
                "peak_hour_volume": 60,
                "avg_transaction_value": 3698.45,
                "repeat_customer_rate": 63.4
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 174,
                "percentage": 5.5,
                "peak_hour_volume": 22,
                "avg_transaction_value": 6723.56,
                "repeat_customer_rate": 50.2
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 126,
                "percentage": 4.0,
                "peak_hour_volume": 16,
                "avg_transaction_value": 3134.78,
                "repeat_customer_rate": 66.1
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4986745,
                "percentage": 32.5,
                "avg_order_value": 4389.67,
                "growth_rate": 22.4,
                "regional_preference": {
                    "North": 41.2,
                    "South": 46.8,
                    "West": 39.1,
                    "East": 32.4
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 7989290,
                "percentage": 52.1,
                "avg_order_value": 6267.89,
                "growth_rate": 23.1,
                "regional_preference": {
                    "North": 57.1,
                    "South": 53.4,
                    "West": 59.2,
                    "East": 50.8
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1723458,
                "percentage": 11.2,
                "avg_order_value": 3698.45,
                "growth_rate": 25.0,
                "regional_preference": {
                    "North": 32.5,
                    "South": 35.7,
                    "West": 30.1,
                    "East": 37.8
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1169900,
                "percentage": 7.6,
                "avg_order_value": 6723.56,
                "growth_rate": 26.5,
                "regional_preference": {
                    "North": 18.2,
                    "South": 15.8,
                    "West": 22.1,
                    "East": 24.9
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 394982,
                "percentage": 2.6,
                "avg_order_value": 3134.78,
                "growth_rate": 27.5,
                "regional_preference": {
                    "North": 26.3,
                    "South": 30.1,
                    "West": 23.4,
                    "East": 19.8
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4389.67,
                "median_ticket_size": 4038.5,
                "percentile_90": 7681.93,
                "category_preference": {
                    "Electronics": 48.2,
                    "Fashion": 30.1,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6267.89,
                "median_ticket_size": 5581.42,
                "percentile_90": 12222.41,
                "category_preference": {
                    "Electronics": 62.7,
                    "Fashion": 25.8,
                    "Home": 11.5
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3698.45,
                "median_ticket_size": 3220.06,
                "percentile_90": 7316.71,
                "category_preference": {
                    "Electronics": 35.4,
                    "Fashion": 39.8,
                    "Home": 24.8
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6723.56,
                "median_ticket_size": 6252.92,
                "percentile_90": 13379.1,
                "category_preference": {
                    "Electronics": 71.8,
                    "Fashion": 16.2,
                    "Home": 12.0
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3134.78,
                "median_ticket_size": 2664.56,
                "percentile_90": 5799.35,
                "category_preference": {
                    "Electronics": 31.2,
                    "Fashion": 47.1,
                    "Home": 21.7
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 96.8,
                "transaction_volume": 1694,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 42.9
            },
            "mobile": {
                "success_rate": 91.6,
                "transaction_volume": 1354,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 35.4
            },
            "tablet": {
                "success_rate": 93.2,
                "transaction_volume": 339,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 38.9
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "17:00-18:00",
                    "20:00-21:00",
                    "21:00-22:00"
                ],
                "success_rate_by_hour": {
                    "morning": 95.1,
                    "afternoon": 93.7,
                    "evening": 92.3,
                    "night": 90.8
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 94.2,
                "weekend_sr": 91.8,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 67.09,
            "total_attempts": 3234,
            "successful_transactions": 2903,
            "failed_transactions": 331,
            "processing_time_avg": 42.1,
            "retry_success_rate": 36.9,
            "peak_hour_sr": 92.4,
            "off_peak_sr": 88.2
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.6,
                "total_attempts": 1133,
                "successful": 1049,
                "failed": 84,
                "avg_processing_time": 28.7,
                "peak_volume_hour": "14:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.45,
                "total_attempts": 1294,
                "successful": 1170,
                "failed": 124,
                "avg_processing_time": 40.8,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.34,
                "total_attempts": 485,
                "successful": 424,
                "failed": 61,
                "avg_processing_time": 39.4,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 81.67,
                "total_attempts": 194,
                "successful": 158,
                "failed": 36,
                "avg_processing_time": 72.6,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 88.28,
                "total_attempts": 128,
                "successful": 113,
                "failed": 15,
                "avg_processing_time": 23.2,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 84,
                "percentage": 25.4,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 53,
                "percentage": 16.0,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 38,
                "percentage": 11.5,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 32,
                "percentage": 9.7,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 29,
                "percentage": 8.8,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 21,
                "percentage": 6.3,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1049,
                "percentage": 36.1,
                "peak_hour_volume": 133,
                "avg_transaction_value": 4289.34,
                "repeat_customer_rate": 70.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1170,
                "percentage": 40.3,
                "peak_hour_volume": 152,
                "avg_transaction_value": 6089.78,
                "repeat_customer_rate": 73.9
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 424,
                "percentage": 14.6,
                "peak_hour_volume": 55,
                "avg_transaction_value": 3598.67,
                "repeat_customer_rate": 61.8
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 158,
                "percentage": 5.4,
                "peak_hour_volume": 20,
                "avg_transaction_value": 6545.23,
                "repeat_customer_rate": 48.7
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 113,
                "percentage": 3.9,
                "peak_hour_volume": 14,
                "avg_transaction_value": 3067.89,
                "repeat_customer_rate": 64.2
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4499515,
                "percentage": 31.8,
                "avg_order_value": 4289.34,
                "growth_rate": -9.8,
                "regional_preference": {
                    "North": 40.1,
                    "South": 44.5,
                    "West": 37.8,
                    "East": 31.2
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 7125044,
                "percentage": 50.4,
                "avg_order_value": 6089.78,
                "growth_rate": -10.8,
                "regional_preference": {
                    "North": 54.8,
                    "South": 51.2,
                    "West": 57.1,
                    "East": 49.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1525837,
                "percentage": 10.8,
                "avg_order_value": 3598.67,
                "growth_rate": -11.5,
                "regional_preference": {
                    "North": 31.4,
                    "South": 34.2,
                    "West": 29.8,
                    "East": 36.1
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1034146,
                "percentage": 7.3,
                "avg_order_value": 6545.23,
                "growth_rate": -11.6,
                "regional_preference": {
                    "North": 17.1,
                    "South": 14.7,
                    "West": 20.8,
                    "East": 23.5
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 346672,
                "percentage": 2.5,
                "avg_order_value": 3067.89,
                "growth_rate": -12.2,
                "regional_preference": {
                    "North": 24.7,
                    "South": 28.4,
                    "West": 21.9,
                    "East": 18.6
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4289.34,
                "median_ticket_size": 3946.2,
                "percentile_90": 7506.32,
                "category_preference": {
                    "Electronics": 46.8,
                    "Fashion": 31.4,
                    "Home": 21.8
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6089.78,
                "median_ticket_size": 5420.12,
                "percentile_90": 11875.51,
                "category_preference": {
                    "Electronics": 60.4,
                    "Fashion": 27.9,
                    "Home": 11.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3598.67,
                "median_ticket_size": 3131.46,
                "percentile_90": 7117.37,
                "category_preference": {
                    "Electronics": 34.1,
                    "Fashion": 41.5,
                    "Home": 24.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6545.23,
                "median_ticket_size": 6087.06,
                "percentile_90": 13039.56,
                "category_preference": {
                    "Electronics": 70.3,
                    "Fashion": 17.2,
                    "Home": 12.5
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3067.89,
                "median_ticket_size": 2607.71,
                "percentile_90": 5675.6,
                "category_preference": {
                    "Electronics": 30.1,
                    "Fashion": 46.8,
                    "Home": 23.1
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 93.4,
                "transaction_volume": 1617,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 46.1
            },
            "mobile": {
                "success_rate": 87.9,
                "transaction_volume": 1294,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 38.6
            },
            "tablet": {
                "success_rate": 89.5,
                "transaction_volume": 323,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 42.1
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "14:00-15:00",
                    "19:00-20:00",
                    "20:00-21:00"
                ],
                "success_rate_by_hour": {
                    "morning": 92.1,
                    "afternoon": 90.3,
                    "evening": 88.7,
                    "night": 86.5
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 90.5,
                "weekend_sr": 88.1,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 72.41,
            "total_attempts": 3401,
            "successful_transactions": 3152,
            "failed_transactions": 249,
            "processing_time_avg": 39.4,
            "retry_success_rate": 40.1,
            "peak_hour_sr": 95.2,
            "off_peak_sr": 91.3
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.8,
                "total_attempts": 1190,
                "successful": 1128,
                "failed": 62,
                "avg_processing_time": 26.1,
                "peak_volume_hour": "15:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.45,
                "total_attempts": 1360,
                "successful": 1271,
                "failed": 89,
                "avg_processing_time": 38.6,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 90.23,
                "total_attempts": 510,
                "successful": 460,
                "failed": 50,
                "avg_processing_time": 37.8,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 84.12,
                "total_attempts": 204,
                "successful": 172,
                "failed": 32,
                "avg_processing_time": 71.2,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 91.24,
                "total_attempts": 137,
                "successful": 125,
                "failed": 12,
                "avg_processing_time": 21.7,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 63,
                "percentage": 25.3,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 40,
                "percentage": 16.1,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 28,
                "percentage": 11.2,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 22,
                "percentage": 8.8,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 19,
                "percentage": 7.6,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 15,
                "percentage": 6.0,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1128,
                "percentage": 35.8,
                "peak_hour_volume": 143,
                "avg_transaction_value": 4445.78,
                "repeat_customer_rate": 71.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1271,
                "percentage": 40.3,
                "peak_hour_volume": 164,
                "avg_transaction_value": 6334.56,
                "repeat_customer_rate": 75.2
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 460,
                "percentage": 14.6,
                "peak_hour_volume": 59,
                "avg_transaction_value": 3756.89,
                "repeat_customer_rate": 62.9
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 172,
                "percentage": 5.5,
                "peak_hour_volume": 22,
                "avg_transaction_value": 6789.34,
                "repeat_customer_rate": 49.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 125,
                "percentage": 4.0,
                "peak_hour_volume": 16,
                "avg_transaction_value": 3189.45,
                "repeat_customer_rate": 65.7
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5015259,
                "percentage": 32.2,
                "avg_order_value": 4445.78,
                "growth_rate": 11.5,
                "regional_preference": {
                    "North": 41.8,
                    "South": 47.2,
                    "West": 39.6,
                    "East": 32.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 8051229,
                "percentage": 51.7,
                "avg_order_value": 6334.56,
                "growth_rate": 13.0,
                "regional_preference": {
                    "North": 56.8,
                    "South": 53.1,
                    "West": 58.9,
                    "East": 50.5
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1728167,
                "percentage": 11.1,
                "avg_order_value": 3756.89,
                "growth_rate": 13.3,
                "regional_preference": {
                    "North": 32.1,
                    "South": 35.4,
                    "West": 29.8,
                    "East": 37.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1167766,
                "percentage": 7.5,
                "avg_order_value": 6789.34,
                "growth_rate": 12.9,
                "regional_preference": {
                    "North": 17.9,
                    "South": 15.4,
                    "West": 21.7,
                    "East": 24.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 398681,
                "percentage": 2.6,
                "avg_order_value": 3189.45,
                "growth_rate": 15.0,
                "regional_preference": {
                    "North": 25.8,
                    "South": 29.7,
                    "West": 22.9,
                    "East": 19.4
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4445.78,
                "median_ticket_size": 4090.12,
                "percentile_90": 7780.14,
                "category_preference": {
                    "Electronics": 48.9,
                    "Fashion": 29.4,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6334.56,
                "median_ticket_size": 5640.86,
                "percentile_90": 12367.49,
                "category_preference": {
                    "Electronics": 63.1,
                    "Fashion": 25.2,
                    "Home": 11.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3756.89,
                "median_ticket_size": 3269.5,
                "percentile_90": 7436.35,
                "category_preference": {
                    "Electronics": 36.1,
                    "Fashion": 39.2,
                    "Home": 24.7
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6789.34,
                "median_ticket_size": 6315.71,
                "percentile_90": 13516.54,
                "category_preference": {
                    "Electronics": 72.4,
                    "Fashion": 15.8,
                    "Home": 11.8
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3189.45,
                "median_ticket_size": 2711.03,
                "percentile_90": 5900.99,
                "category_preference": {
                    "Electronics": 31.8,
                    "Fashion": 47.6,
                    "Home": 20.6
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 96.1,
                "transaction_volume": 1700,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 43.4
            },
            "mobile": {
                "success_rate": 90.8,
                "transaction_volume": 1361,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 36.1
            },
            "tablet": {
                "success_rate": 92.5,
                "transaction_volume": 340,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 39.4
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "15:00-16:00",
                    "21:00-22:00",
                    "22:00-23:00"
                ],
                "success_rate_by_hour": {
                    "morning": 94.8,
                    "afternoon": 93.1,
                    "evening": 91.7,
                    "night": 89.9
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 93.4,
                "weekend_sr": 91.2,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 68.37,
            "total_attempts": 3145,
            "successful_transactions": 2779,
            "failed_transactions": 366,
            "processing_time_avg": 43.8,
            "retry_success_rate": 35.4,
            "peak_hour_sr": 91.1,
            "off_peak_sr": 86.7
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.3,
                "total_attempts": 1101,
                "successful": 1005,
                "failed": 96,
                "avg_processing_time": 29.5,
                "peak_volume_hour": "16:00-17:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 89.67,
                "total_attempts": 1259,
                "successful": 1129,
                "failed": 130,
                "avg_processing_time": 42.3,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 86.45,
                "total_attempts": 472,
                "successful": 408,
                "failed": 64,
                "avg_processing_time": 40.6,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 80.89,
                "total_attempts": 190,
                "successful": 154,
                "failed": 36,
                "avg_processing_time": 74.5,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.61,
                "total_attempts": 123,
                "successful": 108,
                "failed": 15,
                "avg_processing_time": 24.8,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 93,
                "percentage": 25.4,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 59,
                "percentage": 16.1,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 43,
                "percentage": 11.7,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 35,
                "percentage": 9.6,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 31,
                "percentage": 8.5,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 23,
                "percentage": 6.3,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1005,
                "percentage": 36.2,
                "peak_hour_volume": 127,
                "avg_transaction_value": 4278.9,
                "repeat_customer_rate": 69.4
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1129,
                "percentage": 40.6,
                "peak_hour_volume": 146,
                "avg_transaction_value": 6012.34,
                "repeat_customer_rate": 72.7
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 408,
                "percentage": 14.7,
                "peak_hour_volume": 53,
                "avg_transaction_value": 3623.78,
                "repeat_customer_rate": 60.5
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 154,
                "percentage": 5.5,
                "peak_hour_volume": 20,
                "avg_transaction_value": 6456.78,
                "repeat_customer_rate": 47.9
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 108,
                "percentage": 3.9,
                "peak_hour_volume": 14,
                "avg_transaction_value": 3034.56,
                "repeat_customer_rate": 63.8
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4300295,
                "percentage": 31.6,
                "avg_order_value": 4278.9,
                "growth_rate": -14.2,
                "regional_preference": {
                    "North": 39.7,
                    "South": 44.1,
                    "West": 37.4,
                    "East": 30.8
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6787913,
                "percentage": 49.9,
                "avg_order_value": 6012.34,
                "growth_rate": -15.7,
                "regional_preference": {
                    "North": 54.2,
                    "South": 50.6,
                    "West": 56.4,
                    "East": 48.9
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1478502,
                "percentage": 10.9,
                "avg_order_value": 3623.78,
                "growth_rate": -14.4,
                "regional_preference": {
                    "North": 30.8,
                    "South": 33.7,
                    "West": 29.1,
                    "East": 35.6
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 994344,
                "percentage": 7.3,
                "avg_order_value": 6456.78,
                "growth_rate": -14.8,
                "regional_preference": {
                    "North": 16.7,
                    "South": 14.3,
                    "West": 20.2,
                    "East": 23.1
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 327732,
                "percentage": 2.4,
                "avg_order_value": 3034.56,
                "growth_rate": -17.8,
                "regional_preference": {
                    "North": 24.3,
                    "South": 28.0,
                    "West": 21.6,
                    "East": 18.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4278.9,
                "median_ticket_size": 3936.6,
                "percentile_90": 7487.81,
                "category_preference": {
                    "Electronics": 46.5,
                    "Fashion": 31.8,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6012.34,
                "median_ticket_size": 5351.09,
                "percentile_90": 11723.57,
                "category_preference": {
                    "Electronics": 59.8,
                    "Fashion": 28.4,
                    "Home": 11.8
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3623.78,
                "median_ticket_size": 3154.69,
                "percentile_90": 7166.67,
                "category_preference": {
                    "Electronics": 33.9,
                    "Fashion": 41.8,
                    "Home": 24.3
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6456.78,
                "median_ticket_size": 5999.9,
                "percentile_90": 12850.51,
                "category_preference": {
                    "Electronics": 69.7,
                    "Fashion": 17.5,
                    "Home": 12.8
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3034.56,
                "median_ticket_size": 2579.38,
                "percentile_90": 5613.94,
                "category_preference": {
                    "Electronics": 29.7,
                    "Fashion": 47.1,
                    "Home": 23.2
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 92.7,
                "transaction_volume": 1573,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 47.8
            },
            "mobile": {
                "success_rate": 86.1,
                "transaction_volume": 1259,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 40.3
            },
            "tablet": {
                "success_rate": 88.2,
                "transaction_volume": 313,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 43.8
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "16:00-17:00",
                    "20:00-21:00",
                    "21:00-22:00"
                ],
                "success_rate_by_hour": {
                    "morning": 90.8,
                    "afternoon": 89.1,
                    "evening": 87.4,
                    "night": 85.2
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 89.1,
                "weekend_sr": 86.8,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 66.02,
            "total_attempts": 3456,
            "successful_transactions": 3253,
            "failed_transactions": 203,
            "processing_time_avg": 37.8,
            "retry_success_rate": 42.7,
            "peak_hour_sr": 96.5,
            "off_peak_sr": 92.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 96.2,
                "total_attempts": 1211,
                "successful": 1165,
                "failed": 46,
                "avg_processing_time": 24.9,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 95.78,
                "total_attempts": 1382,
                "successful": 1324,
                "failed": 58,
                "avg_processing_time": 36.4,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 92.45,
                "total_attempts": 518,
                "successful": 479,
                "failed": 39,
                "avg_processing_time": 35.7,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 86.34,
                "total_attempts": 207,
                "successful": 179,
                "failed": 28,
                "avg_processing_time": 68.9,
                "peak_volume_hour": "23:00-24:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 93.48,
                "total_attempts": 138,
                "successful": 129,
                "failed": 9,
                "avg_processing_time": 20.2,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 51,
                "percentage": 25.1,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 33,
                "percentage": 16.3,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 20,
                "percentage": 9.9,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 17,
                "percentage": 8.4,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 15,
                "percentage": 7.4,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 12,
                "percentage": 5.9,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1165,
                "percentage": 35.8,
                "peak_hour_volume": 148,
                "avg_transaction_value": 4534.67,
                "repeat_customer_rate": 73.2
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1324,
                "percentage": 40.7,
                "peak_hour_volume": 171,
                "avg_transaction_value": 6445.89,
                "repeat_customer_rate": 76.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 479,
                "percentage": 14.7,
                "peak_hour_volume": 62,
                "avg_transaction_value": 3823.45,
                "repeat_customer_rate": 64.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 179,
                "percentage": 5.5,
                "peak_hour_volume": 23,
                "avg_transaction_value": 6912.34,
                "repeat_customer_rate": 51.2
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 129,
                "percentage": 4.0,
                "peak_hour_volume": 16,
                "avg_transaction_value": 3267.89,
                "repeat_customer_rate": 67.4
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5282890,
                "percentage": 32.3,
                "avg_order_value": 4534.67,
                "growth_rate": 22.8,
                "regional_preference": {
                    "North": 42.4,
                    "South": 47.8,
                    "West": 40.2,
                    "East": 33.6
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 8534363,
                "percentage": 52.2,
                "avg_order_value": 6445.89,
                "growth_rate": 25.7,
                "regional_preference": {
                    "North": 58.1,
                    "South": 54.7,
                    "West": 60.3,
                    "East": 52.1
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1831633,
                "percentage": 11.2,
                "avg_order_value": 3823.45,
                "growth_rate": 23.9,
                "regional_preference": {
                    "North": 33.4,
                    "South": 36.8,
                    "West": 31.2,
                    "East": 38.7
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1237309,
                "percentage": 7.6,
                "avg_order_value": 6912.34,
                "growth_rate": 28.9,
                "regional_preference": {
                    "North": 18.6,
                    "South": 16.1,
                    "West": 22.5,
                    "East": 25.7
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 421558,
                "percentage": 2.6,
                "avg_order_value": 3267.89,
                "growth_rate": 28.7,
                "regional_preference": {
                    "North": 26.9,
                    "South": 31.3,
                    "West": 24.1,
                    "East": 20.3
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4534.67,
                "median_ticket_size": 4171.89,
                "percentile_90": 7934.66,
                "category_preference": {
                    "Electronics": 49.6,
                    "Fashion": 28.7,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6445.89,
                "median_ticket_size": 5740.72,
                "percentile_90": 12588.53,
                "category_preference": {
                    "Electronics": 64.3,
                    "Fashion": 24.6,
                    "Home": 11.1
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3823.45,
                "median_ticket_size": 3328.38,
                "percentile_90": 7565.84,
                "category_preference": {
                    "Electronics": 37.2,
                    "Fashion": 38.1,
                    "Home": 24.7
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6912.34,
                "median_ticket_size": 6427.58,
                "percentile_90": 13756.87,
                "category_preference": {
                    "Electronics": 73.1,
                    "Fashion": 15.2,
                    "Home": 11.7
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3267.89,
                "median_ticket_size": 2777.71,
                "percentile_90": 6045.61,
                "category_preference": {
                    "Electronics": 32.4,
                    "Fashion": 48.1,
                    "Home": 19.5
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 97.4,
                "transaction_volume": 1728,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 41.8
            },
            "mobile": {
                "success_rate": 92.3,
                "transaction_volume": 1382,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 34.2
            },
            "tablet": {
                "success_rate": 94.1,
                "transaction_volume": 346,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 37.8
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "17:00-18:00",
                    "22:00-23:00",
                    "23:00-24:00"
                ],
                "success_rate_by_hour": {
                    "morning": 96.2,
                    "afternoon": 94.8,
                    "evening": 93.4,
                    "night": 91.7
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 95.1,
                "weekend_sr": 92.8,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.42,
            "total_attempts": 3298,
            "successful_transactions": 2983,
            "failed_transactions": 315,
            "processing_time_avg": 41.2,
            "retry_success_rate": 37.8,
            "peak_hour_sr": 93.1,
            "off_peak_sr": 88.9
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.4,
                "total_attempts": 1154,
                "successful": 1078,
                "failed": 76,
                "avg_processing_time": 27.8,
                "peak_volume_hour": "14:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 91.89,
                "total_attempts": 1319,
                "successful": 1212,
                "failed": 107,
                "avg_processing_time": 39.7,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 88.67,
                "total_attempts": 494,
                "successful": 438,
                "failed": 56,
                "avg_processing_time": 38.1,
                "peak_volume_hour": "19:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 82.45,
                "total_attempts": 197,
                "successful": 162,
                "failed": 35,
                "avg_processing_time": 72.3,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 89.78,
                "total_attempts": 134,
                "successful": 120,
                "failed": 14,
                "avg_processing_time": 22.6,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 80,
                "percentage": 25.4,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 51,
                "percentage": 16.2,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 34,
                "percentage": 10.8,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 29,
                "percentage": 9.2,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 27,
                "percentage": 8.6,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 20,
                "percentage": 6.3,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1078,
                "percentage": 36.1,
                "peak_hour_volume": 137,
                "avg_transaction_value": 4356.78,
                "repeat_customer_rate": 71.2
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1212,
                "percentage": 40.6,
                "peak_hour_volume": 157,
                "avg_transaction_value": 6223.45,
                "repeat_customer_rate": 74.5
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 438,
                "percentage": 14.7,
                "peak_hour_volume": 57,
                "avg_transaction_value": 3689.23,
                "repeat_customer_rate": 62.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 162,
                "percentage": 5.4,
                "peak_hour_volume": 21,
                "avg_transaction_value": 6678.9,
                "repeat_customer_rate": 49.1
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 120,
                "percentage": 4.0,
                "peak_hour_volume": 15,
                "avg_transaction_value": 3134.56,
                "repeat_customer_rate": 65.8
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4696609,
                "percentage": 31.9,
                "avg_order_value": 4356.78,
                "growth_rate": -11.1,
                "regional_preference": {
                    "North": 40.8,
                    "South": 45.2,
                    "West": 38.6,
                    "East": 31.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 7546823,
                "percentage": 51.2,
                "avg_order_value": 6223.45,
                "growth_rate": -11.6,
                "regional_preference": {
                    "North": 55.4,
                    "South": 51.9,
                    "West": 57.8,
                    "East": 49.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1615882,
                "percentage": 11.0,
                "avg_order_value": 3689.23,
                "growth_rate": -11.8,
                "regional_preference": {
                    "North": 31.8,
                    "South": 34.9,
                    "West": 30.1,
                    "East": 37.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1081982,
                "percentage": 7.3,
                "avg_order_value": 6678.9,
                "growth_rate": -12.6,
                "regional_preference": {
                    "North": 17.4,
                    "South": 15.0,
                    "West": 21.1,
                    "East": 24.1
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 376147,
                "percentage": 2.6,
                "avg_order_value": 3134.56,
                "growth_rate": -10.8,
                "regional_preference": {
                    "North": 25.1,
                    "South": 29.2,
                    "West": 22.4,
                    "East": 19.0
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4356.78,
                "median_ticket_size": 4008.24,
                "percentile_90": 7624.18,
                "category_preference": {
                    "Electronics": 47.8,
                    "Fashion": 30.5,
                    "Home": 21.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6223.45,
                "median_ticket_size": 5540.87,
                "percentile_90": 12145.74,
                "category_preference": {
                    "Electronics": 61.7,
                    "Fashion": 26.8,
                    "Home": 11.5
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3689.23,
                "median_ticket_size": 3210.63,
                "percentile_90": 7293.11,
                "category_preference": {
                    "Electronics": 35.6,
                    "Fashion": 40.7,
                    "Home": 23.7
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6678.9,
                "median_ticket_size": 6207.94,
                "percentile_90": 13299.82,
                "category_preference": {
                    "Electronics": 70.9,
                    "Fashion": 16.7,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3134.56,
                "median_ticket_size": 2664.38,
                "percentile_90": 5799.44,
                "category_preference": {
                    "Electronics": 30.8,
                    "Fashion": 47.4,
                    "Home": 21.8
                }
            }
        ],
        "device_wise_analytics": {
            "desktop": {
                "success_rate": 94.7,
                "transaction_volume": 1649,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "NET_BANKING"
                ],
                "avg_processing_time": 45.2
            },
            "mobile": {
                "success_rate": 88.4,
                "transaction_volume": 1319,
                "preferred_payment_methods": [
                    "UPI",
                    "WALLET"
                ],
                "avg_processing_time": 37.8
            },
            "tablet": {
                "success_rate": 90.1,
                "transaction_volume": 330,
                "preferred_payment_methods": [
                    "CREDIT_CARD",
                    "UPI"
                ],
                "avg_processing_time": 41.2
            }
        },
        "time_based_analytics": {
            "hourly_patterns": {
                "peak_hours": [
                    "14:00-15:00",
                    "21:00-22:00",
                    "22:00-23:00"
                ],
                "success_rate_by_hour": {
                    "morning": 92.8,
                    "afternoon": 91.2,
                    "evening": 89.7,
                    "night": 87.4
                },
                "payment_method_preference_by_time": {
                    "morning": "UPI",
                    "afternoon": "CREDIT_CARD",
                    "evening": "CREDIT_CARD",
                    "night": "UPI"
                }
            },
            "weekly_patterns": {
                "weekday_sr": 91.2,
                "weekend_sr": 89.1,
                "monday_peak": False,
                "friday_peak": True
            }
        },
        "errors": []
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.62,
            "total_attempts": 3156,
            "successful_transactions": 2845,
            "failed_transactions": 311,
            "processing_time_avg": 40.2,
            "retry_success_rate": 36.8,
            "peak_hour_sr": 92.1,
            "off_peak_sr": 88.9
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.86,
                "total_attempts": 1106,
                "successful": 1038,
                "failed": 68,
                "avg_processing_time": 22.9,
                "peak_volume_hour": "17:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.86,
                "total_attempts": 1264,
                "successful": 1148,
                "failed": 116,
                "avg_processing_time": 39.2,
                "peak_volume_hour": "22:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 86.5,
                "total_attempts": 474,
                "successful": 410,
                "failed": 64,
                "avg_processing_time": 66.6,
                "peak_volume_hour": "20:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 78.94,
                "total_attempts": 189,
                "successful": 149,
                "failed": 40,
                "avg_processing_time": 31.2,
                "peak_volume_hour": "21:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 84.72,
                "total_attempts": 126,
                "successful": 106,
                "failed": 20,
                "avg_processing_time": 73.0,
                "peak_volume_hour": "15:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 89,
                    "percentage": 28.6
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 76,
                    "percentage": 24.4
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 58,
                    "percentage": 18.6
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 42,
                    "percentage": 13.5
                },
                {
                    "reason": "EXPIRED_CARD",
                    "count": 26,
                    "percentage": 8.4
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 12.8,
                "06-12": 9.2,
                "12-18": 8.9,
                "18-24": 10.7
            },
            "retry_attempts": {
                "single_retry": 198,
                "multiple_retries": 113,
                "success_after_retry": 114
            }
        },
        "gmv_breakdown": {
            "total_gmv": 523840.75,
            "successful_gmv": 472456.2,
            "failed_gmv": 51384.55,
            "payment_method_gmv": {
                "UPI": 186423.45,
                "CREDIT_CARD": 203187.3,
                "DEBIT_CARD": 58972.15,
                "NET_BANKING": 23873.3
            },
            "average_transaction_value": 165.82,
            "high_value_transactions": 42,
            "micro_transactions": 1205
        },
        "device_analytics": {
            "mobile_sr": 91.2,
            "desktop_sr": 89.8,
            "tablet_sr": 87.5,
            "mobile_percentage": 78.4,
            "desktop_percentage": 18.9,
            "tablet_percentage": 2.7,
            "ios_sr": 92.1,
            "android_sr": 90.8,
            "windows_sr": 89.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 91.8,
                    "volume": 892
                },
                {
                    "city": "Delhi",
                    "success_rate": 90.2,
                    "volume": 743
                },
                {
                    "city": "Bangalore",
                    "success_rate": 92.4,
                    "volume": 612
                },
                {
                    "city": "Chennai",
                    "success_rate": 89.7,
                    "volume": 456
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 91.1,
                    "volume": 453
                }
            ],
            "state_performance": {
                "Maharashtra": 91.8,
                "Karnataka": 92.4,
                "Delhi": 90.2,
                "Tamil Nadu": 89.7,
                "Telangana": 91.1
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "higher_evening_activity"
            },
            "success_rate_by_time_of_day": {
                "morning": 90.8,
                "afternoon": 91.2,
                "evening": 90.9,
                "night": 88.1
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 90.6,
            "weekend_sr": 89.2,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 34,
                "percentage": 11.0,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 34,
                "percentage": 11.0,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 39,
                "percentage": 12.7,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 26,
                "percentage": 8.4,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "CREDIT_CARD",
                "count": 37,
                "percentage": 12.0,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 49,
                "percentage": 15.9,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1038,
                "percentage": 36.4,
                "peak_hour_volume": 103,
                "avg_transaction_value": 3467.48,
                "repeat_customer_rate": 50.7
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1148,
                "percentage": 40.3,
                "peak_hour_volume": 107,
                "avg_transaction_value": 3483.74,
                "repeat_customer_rate": 52.3
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 410,
                "percentage": 14.4,
                "peak_hour_volume": 64,
                "avg_transaction_value": 3969.81,
                "repeat_customer_rate": 63.0
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 149,
                "percentage": 5.2,
                "peak_hour_volume": 38,
                "avg_transaction_value": 4876.61,
                "repeat_customer_rate": 66.1
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 106,
                "percentage": 3.7,
                "peak_hour_volume": 130,
                "avg_transaction_value": 3638.38,
                "repeat_customer_rate": 52.1
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 3599244,
                "percentage": 36.4,
                "avg_order_value": 3467.48,
                "growth_rate": 0.1,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3999333,
                "percentage": 40.3,
                "avg_order_value": 3483.74,
                "growth_rate": -0.4,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1627622,
                "percentage": 14.4,
                "avg_order_value": 3969.81,
                "growth_rate": 18.4,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 726614,
                "percentage": 5.2,
                "avg_order_value": 4876.61,
                "growth_rate": 2.9,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 385668,
                "percentage": 3.7,
                "avg_order_value": 3638.38,
                "growth_rate": 4.0,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 3467.48,
                "median_ticket_size": 2947.36,
                "percentile_90": 6241.46,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3483.74,
                "median_ticket_size": 2961.18,
                "percentile_90": 6270.73,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3969.81,
                "median_ticket_size": 3374.34,
                "percentile_90": 7145.66,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4876.61,
                "median_ticket_size": 4145.12,
                "percentile_90": 8777.9,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3638.38,
                "median_ticket_size": 3092.62,
                "percentile_90": 6549.08,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.71,
            "total_attempts": 2987,
            "successful_transactions": 2651,
            "failed_transactions": 336,
            "processing_time_avg": 43.8,
            "retry_success_rate": 33.2,
            "peak_hour_sr": 90.8,
            "off_peak_sr": 87.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.56,
                "total_attempts": 1291,
                "successful": 1194,
                "failed": 97,
                "avg_processing_time": 24.4,
                "peak_volume_hour": "19:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.69,
                "total_attempts": 1476,
                "successful": 1382,
                "failed": 94,
                "avg_processing_time": 48.8,
                "peak_volume_hour": "16:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 90.6,
                "total_attempts": 553,
                "successful": 501,
                "failed": 52,
                "avg_processing_time": 38.7,
                "peak_volume_hour": "14:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 84.23,
                "total_attempts": 221,
                "successful": 186,
                "failed": 35,
                "avg_processing_time": 43.0,
                "peak_volume_hour": "20:00-15:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 88.54,
                "total_attempts": 147,
                "successful": 130,
                "failed": 17,
                "avg_processing_time": 37.3,
                "peak_volume_hour": "18:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 94,
                    "percentage": 28.0
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 83,
                    "percentage": 24.7
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 72,
                    "percentage": 21.4
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 45,
                    "percentage": 13.4
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 42,
                    "percentage": 12.5
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 14.2,
                "06-12": 10.8,
                "12-18": 9.5,
                "18-24": 11.8
            },
            "retry_attempts": {
                "single_retry": 215,
                "multiple_retries": 121,
                "success_after_retry": 112
            }
        },
        "gmv_breakdown": {
            "total_gmv": 498375.4,
            "successful_gmv": 442384.85,
            "failed_gmv": 55990.55,
            "payment_method_gmv": {
                "UPI": 175623.2,
                "CREDIT_CARD": 189734.65,
                "DEBIT_CARD": 55482.3,
                "NET_BANKING": 21544.7
            },
            "average_transaction_value": 166.83,
            "high_value_transactions": 38,
            "micro_transactions": 1156
        },
        "device_analytics": {
            "mobile_sr": 89.8,
            "desktop_sr": 88.2,
            "tablet_sr": 86.1,
            "mobile_percentage": 79.2,
            "desktop_percentage": 18.1,
            "tablet_percentage": 2.7,
            "ios_sr": 91.3,
            "android_sr": 89.2,
            "windows_sr": 87.8
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 90.1,
                    "volume": 823
                },
                {
                    "city": "Delhi",
                    "success_rate": 88.7,
                    "volume": 698
                },
                {
                    "city": "Bangalore",
                    "success_rate": 91.2,
                    "volume": 567
                },
                {
                    "city": "Chennai",
                    "success_rate": 87.9,
                    "volume": 423
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 89.8,
                    "volume": 476
                }
            ],
            "state_performance": {
                "Maharashtra": 90.1,
                "Karnataka": 91.2,
                "Delhi": 88.7,
                "Tamil Nadu": 87.9,
                "Telangana": 89.8
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "13:00-15:00",
                    "20:00-22:00"
                ],
                "low_performance_hours": [
                    "03:00-07:00"
                ],
                "weekend_pattern": "consistent_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 89.2,
                "afternoon": 89.8,
                "evening": 89.1,
                "night": 86.8
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 89.1,
            "weekend_sr": 87.8,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 36,
                "percentage": 12.2,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 30,
                "percentage": 10.2,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "NET_BANKING",
                "count": 47,
                "percentage": 15.9,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 16,
                "percentage": 5.4,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "CREDIT_CARD",
                "count": 41,
                "percentage": 13.9,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 28,
                "percentage": 9.5,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1194,
                "percentage": 35.2,
                "peak_hour_volume": 49,
                "avg_transaction_value": 6333.06,
                "repeat_customer_rate": 54.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1382,
                "percentage": 40.7,
                "peak_hour_volume": 126,
                "avg_transaction_value": 2565.54,
                "repeat_customer_rate": 61.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 501,
                "percentage": 14.8,
                "peak_hour_volume": 65,
                "avg_transaction_value": 3549.1,
                "repeat_customer_rate": 60.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 186,
                "percentage": 5.5,
                "peak_hour_volume": 62,
                "avg_transaction_value": 5286.55,
                "repeat_customer_rate": 64.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 130,
                "percentage": 3.8,
                "peak_hour_volume": 39,
                "avg_transaction_value": 4946.12,
                "repeat_customer_rate": 59.9
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 7561673,
                "percentage": 35.2,
                "avg_order_value": 6333.06,
                "growth_rate": 12.6,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3545576,
                "percentage": 40.7,
                "avg_order_value": 2565.54,
                "growth_rate": 7.6,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1778099,
                "percentage": 14.8,
                "avg_order_value": 3549.1,
                "growth_rate": 7.2,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 983298,
                "percentage": 5.5,
                "avg_order_value": 5286.55,
                "growth_rate": 3.4,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 642995,
                "percentage": 3.8,
                "avg_order_value": 4946.12,
                "growth_rate": 6.1,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 6333.06,
                "median_ticket_size": 5383.1,
                "percentile_90": 11399.51,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 2565.54,
                "median_ticket_size": 2180.71,
                "percentile_90": 4617.97,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3549.1,
                "median_ticket_size": 3016.73,
                "percentile_90": 6388.38,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5286.55,
                "median_ticket_size": 4493.57,
                "percentile_90": 9515.79,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4946.12,
                "median_ticket_size": 4204.2,
                "percentile_90": 8903.02,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.11,
            "total_attempts": 3234,
            "successful_transactions": 2954,
            "failed_transactions": 280,
            "processing_time_avg": 39.7,
            "retry_success_rate": 38.5,
            "peak_hour_sr": 93.2,
            "off_peak_sr": 89.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.63,
                "total_attempts": 1050,
                "successful": 962,
                "failed": 88,
                "avg_processing_time": 23.9,
                "peak_volume_hour": "17:00-19:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.07,
                "total_attempts": 1200,
                "successful": 1116,
                "failed": 84,
                "avg_processing_time": 29.7,
                "peak_volume_hour": "22:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 88.78,
                "total_attempts": 450,
                "successful": 399,
                "failed": 51,
                "avg_processing_time": 68.9,
                "peak_volume_hour": "15:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 83.54,
                "total_attempts": 180,
                "successful": 150,
                "failed": 30,
                "avg_processing_time": 72.7,
                "peak_volume_hour": "21:00-18:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 84.87,
                "total_attempts": 120,
                "successful": 101,
                "failed": 19,
                "avg_processing_time": 20.3,
                "peak_volume_hour": "15:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 87,
                    "percentage": 31.1
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 64,
                    "percentage": 22.9
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 52,
                    "percentage": 18.6
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 38,
                    "percentage": 13.6
                },
                {
                    "reason": "EXPIRED_CARD",
                    "count": 39,
                    "percentage": 13.9
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 11.2,
                "06-12": 8.7,
                "12-18": 7.9,
                "18-24": 9.8
            },
            "retry_attempts": {
                "single_retry": 178,
                "multiple_retries": 102,
                "success_after_retry": 108
            }
        },
        "gmv_breakdown": {
            "total_gmv": 547829.6,
            "successful_gmv": 500421.35,
            "failed_gmv": 47408.25,
            "payment_method_gmv": {
                "UPI": 194867.45,
                "CREDIT_CARD": 218934.7,
                "DEBIT_CARD": 62384.2,
                "NET_BANKING": 24235.0
            },
            "average_transaction_value": 169.35,
            "high_value_transactions": 46,
            "micro_transactions": 1287
        },
        "device_analytics": {
            "mobile_sr": 92.4,
            "desktop_sr": 90.8,
            "tablet_sr": 89.2,
            "mobile_percentage": 77.8,
            "desktop_percentage": 19.4,
            "tablet_percentage": 2.8,
            "ios_sr": 93.5,
            "android_sr": 91.9,
            "windows_sr": 90.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 92.7,
                    "volume": 934
                },
                {
                    "city": "Delhi",
                    "success_rate": 91.3,
                    "volume": 789
                },
                {
                    "city": "Bangalore",
                    "success_rate": 93.1,
                    "volume": 645
                },
                {
                    "city": "Chennai",
                    "success_rate": 90.8,
                    "volume": 487
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 92.2,
                    "volume": 379
                }
            ],
            "state_performance": {
                "Maharashtra": 92.7,
                "Karnataka": 93.1,
                "Delhi": 91.3,
                "Tamil Nadu": 90.8,
                "Telangana": 92.2
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "01:00-05:00"
                ],
                "weekend_pattern": "steady_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 91.8,
                "afternoon": 92.1,
                "evening": 91.7,
                "night": 89.5
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 91.7,
            "weekend_sr": 90.8,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "DEBIT_CARD",
                "count": 32,
                "percentage": 11.8,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "DEBIT_CARD",
                "count": 24,
                "percentage": 8.8,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 18,
                "percentage": 6.6,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 20,
                "percentage": 7.4,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 33,
                "percentage": 12.1,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 33,
                "percentage": 12.1,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 962,
                "percentage": 35.3,
                "peak_hour_volume": 77,
                "avg_transaction_value": 4428.17,
                "repeat_customer_rate": 65.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1116,
                "percentage": 40.9,
                "peak_hour_volume": 48,
                "avg_transaction_value": 2507.81,
                "repeat_customer_rate": 45.3
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 399,
                "percentage": 14.6,
                "peak_hour_volume": 134,
                "avg_transaction_value": 4960.75,
                "repeat_customer_rate": 56.5
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 150,
                "percentage": 5.5,
                "peak_hour_volume": 103,
                "avg_transaction_value": 4880.98,
                "repeat_customer_rate": 62.3
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 101,
                "percentage": 3.7,
                "peak_hour_volume": 59,
                "avg_transaction_value": 4178.36,
                "repeat_customer_rate": 58.4
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4259899,
                "percentage": 35.3,
                "avg_order_value": 4428.17,
                "growth_rate": 17.1,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 2798715,
                "percentage": 40.9,
                "avg_order_value": 2507.81,
                "growth_rate": 17.8,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1979339,
                "percentage": 14.6,
                "avg_order_value": 4960.75,
                "growth_rate": 6.1,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 732146,
                "percentage": 5.5,
                "avg_order_value": 4880.98,
                "growth_rate": 17.9,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 422014,
                "percentage": 3.7,
                "avg_order_value": 4178.36,
                "growth_rate": 7.0,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4428.17,
                "median_ticket_size": 3763.94,
                "percentile_90": 7970.71,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 2507.81,
                "median_ticket_size": 2131.64,
                "percentile_90": 4514.06,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4960.75,
                "median_ticket_size": 4216.64,
                "percentile_90": 8929.35,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4880.98,
                "median_ticket_size": 4148.83,
                "percentile_90": 8785.76,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4178.36,
                "median_ticket_size": 3551.61,
                "percentile_90": 7521.05,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.88,
            "total_attempts": 2892,
            "successful_transactions": 2543,
            "failed_transactions": 349,
            "processing_time_avg": 45.1,
            "retry_success_rate": 31.8,
            "peak_hour_sr": 89.7,
            "off_peak_sr": 86.4
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.64,
                "total_attempts": 1245,
                "successful": 1178,
                "failed": 67,
                "avg_processing_time": 20.8,
                "peak_volume_hour": "19:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.01,
                "total_attempts": 1423,
                "successful": 1309,
                "failed": 114,
                "avg_processing_time": 32.6,
                "peak_volume_hour": "17:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 88.74,
                "total_attempts": 533,
                "successful": 473,
                "failed": 60,
                "avg_processing_time": 38.0,
                "peak_volume_hour": "14:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 81.99,
                "total_attempts": 213,
                "successful": 174,
                "failed": 39,
                "avg_processing_time": 43.3,
                "peak_volume_hour": "14:00-16:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 88.42,
                "total_attempts": 142,
                "successful": 125,
                "failed": 17,
                "avg_processing_time": 45.3,
                "peak_volume_hour": "14:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 98,
                    "percentage": 28.1
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 92,
                    "percentage": 26.4
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 73,
                    "percentage": 20.9
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 48,
                    "percentage": 13.8
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 38,
                    "percentage": 10.9
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 15.7,
                "06-12": 11.9,
                "12-18": 10.2,
                "18-24": 12.6
            },
            "retry_attempts": {
                "single_retry": 223,
                "multiple_retries": 126,
                "success_after_retry": 111
            }
        },
        "gmv_breakdown": {
            "total_gmv": 482934.8,
            "successful_gmv": 424681.25,
            "failed_gmv": 58253.55,
            "payment_method_gmv": {
                "UPI": 168734.2,
                "CREDIT_CARD": 175623.85,
                "DEBIT_CARD": 52847.3,
                "NET_BANKING": 27475.9
            },
            "average_transaction_value": 167.04,
            "high_value_transactions": 34,
            "micro_transactions": 1089
        },
        "device_analytics": {
            "mobile_sr": 88.9,
            "desktop_sr": 87.2,
            "tablet_sr": 85.6,
            "mobile_percentage": 78.9,
            "desktop_percentage": 18.3,
            "tablet_percentage": 2.8,
            "ios_sr": 90.1,
            "android_sr": 88.2,
            "windows_sr": 86.8
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 89.2,
                    "volume": 798
                },
                {
                    "city": "Delhi",
                    "success_rate": 87.8,
                    "volume": 673
                },
                {
                    "city": "Bangalore",
                    "success_rate": 90.1,
                    "volume": 567
                },
                {
                    "city": "Chennai",
                    "success_rate": 86.9,
                    "volume": 434
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 88.7,
                    "volume": 420
                }
            ],
            "state_performance": {
                "Maharashtra": 89.2,
                "Karnataka": 90.1,
                "Delhi": 87.8,
                "Tamil Nadu": 86.9,
                "Telangana": 88.7
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "18:00-20:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "lower_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 88.4,
                "afternoon": 88.9,
                "evening": 88.1,
                "night": 86.2
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 88.3,
            "weekend_sr": 87.1,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "UPI",
                "count": 42,
                "percentage": 14.1,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 38,
                "percentage": 12.8,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 45,
                "percentage": 15.2,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 30,
                "percentage": 10.1,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "WALLET",
                "count": 20,
                "percentage": 6.7,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 34,
                "percentage": 11.4,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1178,
                "percentage": 36.1,
                "peak_hour_volume": 112,
                "avg_transaction_value": 2567.39,
                "repeat_customer_rate": 74.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1309,
                "percentage": 40.2,
                "peak_hour_volume": 53,
                "avg_transaction_value": 6300.23,
                "repeat_customer_rate": 74.7
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 473,
                "percentage": 14.5,
                "peak_hour_volume": 101,
                "avg_transaction_value": 6160.33,
                "repeat_customer_rate": 63.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 174,
                "percentage": 5.3,
                "peak_hour_volume": 37,
                "avg_transaction_value": 5216.14,
                "repeat_customer_rate": 60.0
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 125,
                "percentage": 3.8,
                "peak_hour_volume": 89,
                "avg_transaction_value": 4193.75,
                "repeat_customer_rate": 49.3
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 3024385,
                "percentage": 36.1,
                "avg_order_value": 2567.39,
                "growth_rate": 1.9,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 8247001,
                "percentage": 40.2,
                "avg_order_value": 6300.23,
                "growth_rate": 0.6,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2913836,
                "percentage": 14.5,
                "avg_order_value": 6160.33,
                "growth_rate": -2.7,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 907608,
                "percentage": 5.3,
                "avg_order_value": 5216.14,
                "growth_rate": 12.7,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 524218,
                "percentage": 3.8,
                "avg_order_value": 4193.75,
                "growth_rate": -0.0,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 2567.39,
                "median_ticket_size": 2182.28,
                "percentile_90": 4621.3,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6300.23,
                "median_ticket_size": 5355.2,
                "percentile_90": 11340.41,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 6160.33,
                "median_ticket_size": 5236.28,
                "percentile_90": 11088.59,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5216.14,
                "median_ticket_size": 4433.72,
                "percentile_90": 9389.05,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4193.75,
                "median_ticket_size": 3564.69,
                "percentile_90": 7548.75,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 73.63,
            "total_attempts": 3487,
            "successful_transactions": 3259,
            "failed_transactions": 228,
            "processing_time_avg": 37.2,
            "retry_success_rate": 42.1,
            "peak_hour_sr": 95.3,
            "off_peak_sr": 92.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.61,
                "total_attempts": 1110,
                "successful": 1050,
                "failed": 60,
                "avg_processing_time": 47.9,
                "peak_volume_hour": "17:00-17:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.75,
                "total_attempts": 1269,
                "successful": 1177,
                "failed": 92,
                "avg_processing_time": 66.8,
                "peak_volume_hour": "19:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 85.74,
                "total_attempts": 476,
                "successful": 408,
                "failed": 68,
                "avg_processing_time": 37.5,
                "peak_volume_hour": "17:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 85.04,
                "total_attempts": 190,
                "successful": 161,
                "failed": 29,
                "avg_processing_time": 56.6,
                "peak_volume_hour": "22:00-19:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 85.89,
                "total_attempts": 126,
                "successful": 108,
                "failed": 18,
                "avg_processing_time": 72.8,
                "peak_volume_hour": "14:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 73,
                    "percentage": 32.0
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 58,
                    "percentage": 25.4
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 42,
                    "percentage": 18.4
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 31,
                    "percentage": 13.6
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 24,
                    "percentage": 10.5
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 8.9,
                "06-12": 6.2,
                "12-18": 5.8,
                "18-24": 7.1
            },
            "retry_attempts": {
                "single_retry": 145,
                "multiple_retries": 83,
                "success_after_retry": 96
            }
        },
        "gmv_breakdown": {
            "total_gmv": 628759.2,
            "successful_gmv": 587834.45,
            "failed_gmv": 40924.75,
            "payment_method_gmv": {
                "UPI": 221456.8,
                "CREDIT_CARD": 253892.65,
                "DEBIT_CARD": 75234.4,
                "NET_BANKING": 37250.6
            },
            "average_transaction_value": 180.28,
            "high_value_transactions": 58,
            "micro_transactions": 1398
        },
        "device_analytics": {
            "mobile_sr": 94.1,
            "desktop_sr": 93.2,
            "tablet_sr": 91.8,
            "mobile_percentage": 76.9,
            "desktop_percentage": 20.3,
            "tablet_percentage": 2.8,
            "ios_sr": 95.2,
            "android_sr": 93.7,
            "windows_sr": 92.8
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 94.5,
                    "volume": 1056
                },
                {
                    "city": "Delhi",
                    "success_rate": 93.2,
                    "volume": 867
                },
                {
                    "city": "Bangalore",
                    "success_rate": 95.1,
                    "volume": 723
                },
                {
                    "city": "Chennai",
                    "success_rate": 92.8,
                    "volume": 534
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 94.2,
                    "volume": 307
                }
            ],
            "state_performance": {
                "Maharashtra": 94.5,
                "Karnataka": 95.1,
                "Delhi": 93.2,
                "Tamil Nadu": 92.8,
                "Telangana": 94.2
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "13:00-15:00",
                    "20:00-22:00"
                ],
                "low_performance_hours": [
                    "03:00-06:00"
                ],
                "weekend_pattern": "high_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 93.8,
                "afternoon": 94.2,
                "evening": 93.9,
                "night": 92.1
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 93.8,
            "weekend_sr": 92.9,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 46,
                "percentage": 17.2,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 28,
                "percentage": 10.5,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 24,
                "percentage": 9.0,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 31,
                "percentage": 11.6,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 40,
                "percentage": 15.0,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "UPI",
                "count": 43,
                "percentage": 16.1,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1050,
                "percentage": 36.2,
                "peak_hour_volume": 41,
                "avg_transaction_value": 5669.93,
                "repeat_customer_rate": 71.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1177,
                "percentage": 40.5,
                "peak_hour_volume": 146,
                "avg_transaction_value": 5698.01,
                "repeat_customer_rate": 55.9
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 408,
                "percentage": 14.0,
                "peak_hour_volume": 147,
                "avg_transaction_value": 4406.18,
                "repeat_customer_rate": 68.2
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 161,
                "percentage": 5.5,
                "peak_hour_volume": 130,
                "avg_transaction_value": 6031.83,
                "repeat_customer_rate": 72.3
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 108,
                "percentage": 3.7,
                "peak_hour_volume": 95,
                "avg_transaction_value": 5278.11,
                "repeat_customer_rate": 57.6
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5953426,
                "percentage": 36.2,
                "avg_order_value": 5669.93,
                "growth_rate": 0.4,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6706557,
                "percentage": 40.5,
                "avg_order_value": 5698.01,
                "growth_rate": -4.3,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1797721,
                "percentage": 14.0,
                "avg_order_value": 4406.18,
                "growth_rate": 17.3,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 971124,
                "percentage": 5.5,
                "avg_order_value": 6031.83,
                "growth_rate": 18.0,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 570035,
                "percentage": 3.7,
                "avg_order_value": 5278.11,
                "growth_rate": -1.3,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 5669.93,
                "median_ticket_size": 4819.44,
                "percentile_90": 10205.87,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5698.01,
                "median_ticket_size": 4843.31,
                "percentile_90": 10256.42,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4406.18,
                "median_ticket_size": 3745.25,
                "percentile_90": 7931.12,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6031.83,
                "median_ticket_size": 5127.06,
                "percentile_90": 10857.29,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5278.11,
                "median_ticket_size": 4486.39,
                "percentile_90": 9500.6,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 67.47,
            "total_attempts": 3145,
            "successful_transactions": 2819,
            "failed_transactions": 326,
            "processing_time_avg": 41.8,
            "retry_success_rate": 35.7,
            "peak_hour_sr": 91.9,
            "off_peak_sr": 88.2
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 90.41,
                "total_attempts": 1226,
                "successful": 1108,
                "failed": 118,
                "avg_processing_time": 37.4,
                "peak_volume_hour": "22:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.33,
                "total_attempts": 1402,
                "successful": 1308,
                "failed": 94,
                "avg_processing_time": 56.7,
                "peak_volume_hour": "21:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.66,
                "total_attempts": 525,
                "successful": 460,
                "failed": 65,
                "avg_processing_time": 61.6,
                "peak_volume_hour": "16:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 83.35,
                "total_attempts": 210,
                "successful": 175,
                "failed": 35,
                "avg_processing_time": 20.3,
                "peak_volume_hour": "16:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 82.63,
                "total_attempts": 140,
                "successful": 115,
                "failed": 25,
                "avg_processing_time": 55.3,
                "peak_volume_hour": "16:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 89,
                    "percentage": 27.3
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 84,
                    "percentage": 25.8
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 67,
                    "percentage": 20.6
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 43,
                    "percentage": 13.2
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 43,
                    "percentage": 13.2
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 13.1,
                "06-12": 9.8,
                "12-18": 8.9,
                "18-24": 10.7
            },
            "retry_attempts": {
                "single_retry": 208,
                "multiple_retries": 118,
                "success_after_retry": 116
            }
        },
        "gmv_breakdown": {
            "total_gmv": 521673.45,
            "successful_gmv": 467542.3,
            "failed_gmv": 54131.15,
            "payment_method_gmv": {
                "UPI": 184623.7,
                "CREDIT_CARD": 203834.85,
                "DEBIT_CARD": 58967.4,
                "NET_BANKING": 20116.35
            },
            "average_transaction_value": 165.89,
            "high_value_transactions": 41,
            "micro_transactions": 1234
        },
        "device_analytics": {
            "mobile_sr": 90.7,
            "desktop_sr": 89.1,
            "tablet_sr": 87.8,
            "mobile_percentage": 78.1,
            "desktop_percentage": 19.2,
            "tablet_percentage": 2.7,
            "ios_sr": 91.8,
            "android_sr": 90.2,
            "windows_sr": 88.7
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 91.1,
                    "volume": 887
                },
                {
                    "city": "Delhi",
                    "success_rate": 89.5,
                    "volume": 734
                },
                {
                    "city": "Bangalore",
                    "success_rate": 92.3,
                    "volume": 612
                },
                {
                    "city": "Chennai",
                    "success_rate": 88.7,
                    "volume": 467
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 90.8,
                    "volume": 445
                }
            ],
            "state_performance": {
                "Maharashtra": 91.1,
                "Karnataka": 92.3,
                "Delhi": 89.5,
                "Tamil Nadu": 88.7,
                "Telangana": 90.8
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "moderate_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 90.1,
                "afternoon": 90.6,
                "evening": 90.2,
                "night": 87.9
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 90.1,
            "weekend_sr": 88.9,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "NET_BANKING",
                "count": 31,
                "percentage": 9.2,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 23,
                "percentage": 6.8,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 24,
                "percentage": 7.1,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 20,
                "percentage": 5.9,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "UPI",
                "count": 47,
                "percentage": 13.9,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "CREDIT_CARD",
                "count": 18,
                "percentage": 5.3,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1108,
                "percentage": 35.0,
                "peak_hour_volume": 123,
                "avg_transaction_value": 4251.77,
                "repeat_customer_rate": 65.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1308,
                "percentage": 41.3,
                "peak_hour_volume": 100,
                "avg_transaction_value": 2902.69,
                "repeat_customer_rate": 59.3
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 460,
                "percentage": 14.5,
                "peak_hour_volume": 134,
                "avg_transaction_value": 4841.01,
                "repeat_customer_rate": 57.3
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 175,
                "percentage": 5.5,
                "peak_hour_volume": 63,
                "avg_transaction_value": 6359.5,
                "repeat_customer_rate": 54.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 115,
                "percentage": 3.6,
                "peak_hour_volume": 100,
                "avg_transaction_value": 5598.93,
                "repeat_customer_rate": 66.7
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4710961,
                "percentage": 35.0,
                "avg_order_value": 4251.77,
                "growth_rate": -4.4,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3796718,
                "percentage": 41.3,
                "avg_order_value": 2902.69,
                "growth_rate": 13.1,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2226864,
                "percentage": 14.5,
                "avg_order_value": 4841.01,
                "growth_rate": 14.3,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1112912,
                "percentage": 5.5,
                "avg_order_value": 6359.5,
                "growth_rate": 9.9,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 643876,
                "percentage": 3.6,
                "avg_order_value": 5598.93,
                "growth_rate": 4.8,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4251.77,
                "median_ticket_size": 3614.0,
                "percentile_90": 7653.19,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 2902.69,
                "median_ticket_size": 2467.29,
                "percentile_90": 5224.84,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4841.01,
                "median_ticket_size": 4114.86,
                "percentile_90": 8713.82,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6359.5,
                "median_ticket_size": 5405.57,
                "percentile_90": 11447.1,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5598.93,
                "median_ticket_size": 4759.09,
                "percentile_90": 10078.07,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 66.44,
            "total_attempts": 3401,
            "successful_transactions": 3135,
            "failed_transactions": 266,
            "processing_time_avg": 38.5,
            "retry_success_rate": 39.8,
            "peak_hour_sr": 94.2,
            "off_peak_sr": 90.7
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.06,
                "total_attempts": 1296,
                "successful": 1206,
                "failed": 90,
                "avg_processing_time": 22.8,
                "peak_volume_hour": "14:00-17:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.24,
                "total_attempts": 1481,
                "successful": 1336,
                "failed": 145,
                "avg_processing_time": 62.8,
                "peak_volume_hour": "17:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.83,
                "total_attempts": 555,
                "successful": 487,
                "failed": 68,
                "avg_processing_time": 46.0,
                "peak_volume_hour": "19:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 75.0,
                "total_attempts": 222,
                "successful": 166,
                "failed": 56,
                "avg_processing_time": 32.3,
                "peak_volume_hour": "17:00-17:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 83.04,
                "total_attempts": 148,
                "successful": 122,
                "failed": 26,
                "avg_processing_time": 65.6,
                "peak_volume_hour": "21:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 78,
                    "percentage": 29.3
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 69,
                    "percentage": 25.9
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 54,
                    "percentage": 20.3
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 35,
                    "percentage": 13.2
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 30,
                    "percentage": 11.3
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 10.2,
                "06-12": 7.4,
                "12-18": 6.8,
                "18-24": 8.3
            },
            "retry_attempts": {
                "single_retry": 169,
                "multiple_retries": 97,
                "success_after_retry": 106
            }
        },
        "gmv_breakdown": {
            "total_gmv": 578934.65,
            "successful_gmv": 533621.4,
            "failed_gmv": 45313.25,
            "payment_method_gmv": {
                "UPI": 204834.7,
                "CREDIT_CARD": 234556.85,
                "DEBIT_CARD": 67834.2,
                "NET_BANKING": 26395.65
            },
            "average_transaction_value": 170.19,
            "high_value_transactions": 52,
            "micro_transactions": 1378
        },
        "device_analytics": {
            "mobile_sr": 93.1,
            "desktop_sr": 91.8,
            "tablet_sr": 90.4,
            "mobile_percentage": 77.3,
            "desktop_percentage": 20.1,
            "tablet_percentage": 2.6,
            "ios_sr": 94.3,
            "android_sr": 92.6,
            "windows_sr": 91.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 93.4,
                    "volume": 978
                },
                {
                    "city": "Delhi",
                    "success_rate": 91.8,
                    "volume": 821
                },
                {
                    "city": "Bangalore",
                    "success_rate": 94.2,
                    "volume": 689
                },
                {
                    "city": "Chennai",
                    "success_rate": 91.2,
                    "volume": 512
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 92.9,
                    "volume": 401
                }
            ],
            "state_performance": {
                "Maharashtra": 93.4,
                "Karnataka": 94.2,
                "Delhi": 91.8,
                "Tamil Nadu": 91.2,
                "Telangana": 92.9
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "20:00-22:00"
                ],
                "low_performance_hours": [
                    "01:00-05:00"
                ],
                "weekend_pattern": "strong_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 92.6,
                "afternoon": 93.1,
                "evening": 92.8,
                "night": 90.9
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 92.5,
            "weekend_sr": 91.6,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "UPI",
                "count": 31,
                "percentage": 8.1,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 33,
                "percentage": 8.6,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 48,
                "percentage": 12.5,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 49,
                "percentage": 12.7,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "CREDIT_CARD",
                "count": 22,
                "percentage": 5.7,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 44,
                "percentage": 11.4,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1206,
                "percentage": 36.4,
                "peak_hour_volume": 72,
                "avg_transaction_value": 6233.54,
                "repeat_customer_rate": 62.1
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1336,
                "percentage": 40.3,
                "peak_hour_volume": 142,
                "avg_transaction_value": 6097.44,
                "repeat_customer_rate": 56.0
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 487,
                "percentage": 14.7,
                "peak_hour_volume": 107,
                "avg_transaction_value": 5844.15,
                "repeat_customer_rate": 68.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 166,
                "percentage": 5.0,
                "peak_hour_volume": 23,
                "avg_transaction_value": 6089.47,
                "repeat_customer_rate": 69.0
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 122,
                "percentage": 3.7,
                "peak_hour_volume": 96,
                "avg_transaction_value": 3142.21,
                "repeat_customer_rate": 68.6
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 7517649,
                "percentage": 36.4,
                "avg_order_value": 6233.54,
                "growth_rate": -0.3,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 8146179,
                "percentage": 40.3,
                "avg_order_value": 6097.44,
                "growth_rate": 11.6,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2846101,
                "percentage": 14.7,
                "avg_order_value": 5844.15,
                "growth_rate": 3.0,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1010852,
                "percentage": 5.0,
                "avg_order_value": 6089.47,
                "growth_rate": -4.5,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 383349,
                "percentage": 3.7,
                "avg_order_value": 3142.21,
                "growth_rate": 2.0,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 6233.54,
                "median_ticket_size": 5298.51,
                "percentile_90": 11220.37,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 6097.44,
                "median_ticket_size": 5182.82,
                "percentile_90": 10975.39,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 5844.15,
                "median_ticket_size": 4967.53,
                "percentile_90": 10519.47,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 6089.47,
                "median_ticket_size": 5176.05,
                "percentile_90": 10961.05,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3142.21,
                "median_ticket_size": 2670.88,
                "percentile_90": 5655.98,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 65.83,
            "total_attempts": 2967,
            "successful_transactions": 2620,
            "failed_transactions": 347,
            "processing_time_avg": 44.3,
            "retry_success_rate": 32.6,
            "peak_hour_sr": 90.1,
            "off_peak_sr": 86.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.57,
                "total_attempts": 1181,
                "successful": 1081,
                "failed": 100,
                "avg_processing_time": 36.9,
                "peak_volume_hour": "14:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.32,
                "total_attempts": 1350,
                "successful": 1246,
                "failed": 104,
                "avg_processing_time": 37.7,
                "peak_volume_hour": "14:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 91.2,
                "total_attempts": 506,
                "successful": 461,
                "failed": 45,
                "avg_processing_time": 43.1,
                "peak_volume_hour": "18:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 85.4,
                "total_attempts": 202,
                "successful": 172,
                "failed": 30,
                "avg_processing_time": 60.6,
                "peak_volume_hour": "17:00-19:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 82.62,
                "total_attempts": 135,
                "successful": 111,
                "failed": 24,
                "avg_processing_time": 43.9,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 102,
                    "percentage": 29.4
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 95,
                    "percentage": 27.4
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 71,
                    "percentage": 20.5
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 47,
                    "percentage": 13.5
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 32,
                    "percentage": 9.2
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 15.8,
                "06-12": 11.2,
                "12-18": 9.7,
                "18-24": 13.1
            },
            "retry_attempts": {
                "single_retry": 221,
                "multiple_retries": 126,
                "success_after_retry": 113
            }
        },
        "gmv_breakdown": {
            "total_gmv": 492834.9,
            "successful_gmv": 435126.75,
            "failed_gmv": 57708.15,
            "payment_method_gmv": {
                "UPI": 172345.6,
                "CREDIT_CARD": 178923.85,
                "DEBIT_CARD": 56234.7,
                "NET_BANKING": 27622.6
            },
            "average_transaction_value": 166.09,
            "high_value_transactions": 36,
            "micro_transactions": 1145
        },
        "device_analytics": {
            "mobile_sr": 89.3,
            "desktop_sr": 87.8,
            "tablet_sr": 86.1,
            "mobile_percentage": 79.4,
            "desktop_percentage": 18.7,
            "tablet_percentage": 1.9,
            "ios_sr": 90.7,
            "android_sr": 88.6,
            "windows_sr": 87.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 89.8,
                    "volume": 812
                },
                {
                    "city": "Delhi",
                    "success_rate": 87.9,
                    "volume": 689
                },
                {
                    "city": "Bangalore",
                    "success_rate": 90.7,
                    "volume": 578
                },
                {
                    "city": "Chennai",
                    "success_rate": 86.8,
                    "volume": 443
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 89.1,
                    "volume": 445
                }
            ],
            "state_performance": {
                "Maharashtra": 89.8,
                "Karnataka": 90.7,
                "Delhi": 87.9,
                "Tamil Nadu": 86.8,
                "Telangana": 89.1
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "18:00-20:00"
                ],
                "low_performance_hours": [
                    "03:00-07:00"
                ],
                "weekend_pattern": "challenging_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 88.7,
                "afternoon": 89.2,
                "evening": 88.8,
                "night": 86.4
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 88.6,
            "weekend_sr": 87.4,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 16,
                "percentage": 5.3,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "DEBIT_CARD",
                "count": 28,
                "percentage": 9.2,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 29,
                "percentage": 9.6,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 22,
                "percentage": 7.3,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "UPI",
                "count": 16,
                "percentage": 5.3,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "NET_BANKING",
                "count": 50,
                "percentage": 16.5,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1081,
                "percentage": 35.2,
                "peak_hour_volume": 149,
                "avg_transaction_value": 5044.11,
                "repeat_customer_rate": 55.7
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1246,
                "percentage": 40.6,
                "peak_hour_volume": 22,
                "avg_transaction_value": 3248.4,
                "repeat_customer_rate": 59.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 461,
                "percentage": 15.0,
                "peak_hour_volume": 37,
                "avg_transaction_value": 2899.75,
                "repeat_customer_rate": 60.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 172,
                "percentage": 5.6,
                "peak_hour_volume": 116,
                "avg_transaction_value": 5914.68,
                "repeat_customer_rate": 60.6
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 111,
                "percentage": 3.6,
                "peak_hour_volume": 132,
                "avg_transaction_value": 5306.42,
                "repeat_customer_rate": 56.6
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5452682,
                "percentage": 35.2,
                "avg_order_value": 5044.11,
                "growth_rate": 13.7,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 4047506,
                "percentage": 40.6,
                "avg_order_value": 3248.4,
                "growth_rate": -4.5,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1336784,
                "percentage": 15.0,
                "avg_order_value": 2899.75,
                "growth_rate": 0.4,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 1017324,
                "percentage": 5.6,
                "avg_order_value": 5914.68,
                "growth_rate": 2.7,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 589012,
                "percentage": 3.6,
                "avg_order_value": 5306.42,
                "growth_rate": 6.4,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 5044.11,
                "median_ticket_size": 4287.49,
                "percentile_90": 9079.4,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3248.4,
                "median_ticket_size": 2761.14,
                "percentile_90": 5847.12,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 2899.75,
                "median_ticket_size": 2464.79,
                "percentile_90": 5219.55,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5914.68,
                "median_ticket_size": 5027.48,
                "percentile_90": 10646.42,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5306.42,
                "median_ticket_size": 4510.46,
                "percentile_90": 9551.56,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 68.96,
            "total_attempts": 3654,
            "successful_transactions": 3454,
            "failed_transactions": 200,
            "processing_time_avg": 35.8,
            "retry_success_rate": 44.5,
            "peak_hour_sr": 96.1,
            "off_peak_sr": 93.2
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.9,
                "total_attempts": 1125,
                "successful": 1045,
                "failed": 80,
                "avg_processing_time": 45.6,
                "peak_volume_hour": "15:00-19:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 91.21,
                "total_attempts": 1286,
                "successful": 1173,
                "failed": 113,
                "avg_processing_time": 62.0,
                "peak_volume_hour": "21:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 83.35,
                "total_attempts": 482,
                "successful": 401,
                "failed": 81,
                "avg_processing_time": 48.7,
                "peak_volume_hour": "18:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 85.17,
                "total_attempts": 192,
                "successful": 163,
                "failed": 29,
                "avg_processing_time": 71.0,
                "peak_volume_hour": "14:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 83.01,
                "total_attempts": 128,
                "successful": 106,
                "failed": 22,
                "avg_processing_time": 48.7,
                "peak_volume_hour": "20:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 65,
                    "percentage": 32.5
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 48,
                    "percentage": 24.0
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 38,
                    "percentage": 19.0
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 27,
                    "percentage": 13.5
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 22,
                    "percentage": 11.0
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 7.2,
                "06-12": 5.1,
                "12-18": 4.8,
                "18-24": 6.3
            },
            "retry_attempts": {
                "single_retry": 127,
                "multiple_retries": 73,
                "success_after_retry": 89
            }
        },
        "gmv_breakdown": {
            "total_gmv": 672834.55,
            "successful_gmv": 635723.9,
            "failed_gmv": 37110.65,
            "payment_method_gmv": {
                "UPI": 235467.8,
                "CREDIT_CARD": 278934.65,
                "DEBIT_CARD": 83456.7,
                "NET_BANKING": 37865.75
            },
            "average_transaction_value": 184.15,
            "high_value_transactions": 67,
            "micro_transactions": 1523
        },
        "device_analytics": {
            "mobile_sr": 95.2,
            "desktop_sr": 94.1,
            "tablet_sr": 92.8,
            "mobile_percentage": 75.8,
            "desktop_percentage": 21.4,
            "tablet_percentage": 2.8,
            "ios_sr": 96.4,
            "android_sr": 94.7,
            "windows_sr": 93.6
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 95.8,
                    "volume": 1134
                },
                {
                    "city": "Delhi",
                    "success_rate": 94.2,
                    "volume": 923
                },
                {
                    "city": "Bangalore",
                    "success_rate": 96.1,
                    "volume": 787
                },
                {
                    "city": "Chennai",
                    "success_rate": 93.9,
                    "volume": 598
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 95.3,
                    "volume": 212
                }
            ],
            "state_performance": {
                "Maharashtra": 95.8,
                "Karnataka": 96.1,
                "Delhi": 94.2,
                "Tamil Nadu": 93.9,
                "Telangana": 95.3
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "13:00-15:00",
                    "21:00-23:00"
                ],
                "low_performance_hours": [
                    "02:00-05:00"
                ],
                "weekend_pattern": "excellent_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 94.9,
                "afternoon": 95.2,
                "evening": 95.1,
                "night": 93.4
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 94.8,
            "weekend_sr": 94.1,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 24,
                "percentage": 7.4,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 46,
                "percentage": 14.2,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 35,
                "percentage": 10.8,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 16,
                "percentage": 4.9,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "UPI",
                "count": 19,
                "percentage": 5.8,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "WALLET",
                "count": 26,
                "percentage": 8.0,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1045,
                "percentage": 36.2,
                "peak_hour_volume": 61,
                "avg_transaction_value": 3491.43,
                "repeat_customer_rate": 59.2
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1173,
                "percentage": 40.6,
                "peak_hour_volume": 106,
                "avg_transaction_value": 5345.09,
                "repeat_customer_rate": 51.9
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 401,
                "percentage": 13.9,
                "peak_hour_volume": 145,
                "avg_transaction_value": 3086.94,
                "repeat_customer_rate": 70.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 163,
                "percentage": 5.6,
                "peak_hour_volume": 104,
                "avg_transaction_value": 5136.44,
                "repeat_customer_rate": 68.2
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 106,
                "percentage": 3.7,
                "peak_hour_volume": 115,
                "avg_transaction_value": 4355.3,
                "repeat_customer_rate": 51.3
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 3648544,
                "percentage": 36.2,
                "avg_order_value": 3491.43,
                "growth_rate": 16.5,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6269790,
                "percentage": 40.6,
                "avg_order_value": 5345.09,
                "growth_rate": 18.5,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1237862,
                "percentage": 13.9,
                "avg_order_value": 3086.94,
                "growth_rate": 6.2,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 837239,
                "percentage": 5.6,
                "avg_order_value": 5136.44,
                "growth_rate": -4.2,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 461661,
                "percentage": 3.7,
                "avg_order_value": 4355.3,
                "growth_rate": 4.6,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 3491.43,
                "median_ticket_size": 2967.72,
                "percentile_90": 6284.57,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5345.09,
                "median_ticket_size": 4543.33,
                "percentile_90": 9621.16,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3086.94,
                "median_ticket_size": 2623.9,
                "percentile_90": 5556.49,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5136.44,
                "median_ticket_size": 4365.97,
                "percentile_90": 9245.59,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4355.3,
                "median_ticket_size": 3702.01,
                "percentile_90": 7839.54,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.49,
            "total_attempts": 2834,
            "successful_transactions": 2458,
            "failed_transactions": 376,
            "processing_time_avg": 47.2,
            "retry_success_rate": 29.8,
            "peak_hour_sr": 88.9,
            "off_peak_sr": 85.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.84,
                "total_attempts": 1016,
                "successful": 953,
                "failed": 63,
                "avg_processing_time": 24.5,
                "peak_volume_hour": "20:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.53,
                "total_attempts": 1161,
                "successful": 1085,
                "failed": 76,
                "avg_processing_time": 51.6,
                "peak_volume_hour": "21:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 90.03,
                "total_attempts": 435,
                "successful": 391,
                "failed": 44,
                "avg_processing_time": 31.2,
                "peak_volume_hour": "19:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 83.93,
                "total_attempts": 174,
                "successful": 146,
                "failed": 28,
                "avg_processing_time": 68.4,
                "peak_volume_hour": "16:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.72,
                "total_attempts": 116,
                "successful": 101,
                "failed": 15,
                "avg_processing_time": 43.7,
                "peak_volume_hour": "21:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 112,
                    "percentage": 29.8
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 108,
                    "percentage": 28.7
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 84,
                    "percentage": 22.3
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 52,
                    "percentage": 13.8
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 20,
                    "percentage": 5.3
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 17.4,
                "06-12": 13.1,
                "12-18": 11.8,
                "18-24": 14.2
            },
            "retry_attempts": {
                "single_retry": 239,
                "multiple_retries": 137,
                "success_after_retry": 112
            }
        },
        "gmv_breakdown": {
            "total_gmv": 468923.4,
            "successful_gmv": 406734.85,
            "failed_gmv": 62188.55,
            "payment_method_gmv": {
                "UPI": 164523.7,
                "CREDIT_CARD": 167834.95,
                "DEBIT_CARD": 51456.3,
                "NET_BANKING": 22919.9
            },
            "average_transaction_value": 165.44,
            "high_value_transactions": 29,
            "micro_transactions": 1089
        },
        "device_analytics": {
            "mobile_sr": 87.8,
            "desktop_sr": 85.9,
            "tablet_sr": 84.2,
            "mobile_percentage": 80.1,
            "desktop_percentage": 17.8,
            "tablet_percentage": 2.1,
            "ios_sr": 89.1,
            "android_sr": 87.2,
            "windows_sr": 85.3
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 88.4,
                    "volume": 789
                },
                {
                    "city": "Delhi",
                    "success_rate": 86.1,
                    "volume": 654
                },
                {
                    "city": "Bangalore",
                    "success_rate": 89.7,
                    "volume": 542
                },
                {
                    "city": "Chennai",
                    "success_rate": 84.8,
                    "volume": 412
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 87.6,
                    "volume": 437
                }
            ],
            "state_performance": {
                "Maharashtra": 88.4,
                "Karnataka": 89.7,
                "Delhi": 86.1,
                "Tamil Nadu": 84.8,
                "Telangana": 87.6
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "17:00-19:00"
                ],
                "low_performance_hours": [
                    "04:00-08:00"
                ],
                "weekend_pattern": "difficult_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 87.2,
                "afternoon": 87.8,
                "evening": 87.1,
                "night": 84.9
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 87.1,
            "weekend_sr": 85.8,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 44,
                "percentage": 19.5,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 40,
                "percentage": 17.7,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 45,
                "percentage": 19.9,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "NET_BANKING",
                "count": 43,
                "percentage": 19.0,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "CREDIT_CARD",
                "count": 43,
                "percentage": 19.0,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "NET_BANKING",
                "count": 17,
                "percentage": 7.5,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 953,
                "percentage": 35.6,
                "peak_hour_volume": 143,
                "avg_transaction_value": 2660.63,
                "repeat_customer_rate": 68.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1085,
                "percentage": 40.5,
                "peak_hour_volume": 59,
                "avg_transaction_value": 4812.68,
                "repeat_customer_rate": 63.9
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 391,
                "percentage": 14.6,
                "peak_hour_volume": 143,
                "avg_transaction_value": 3467.14,
                "repeat_customer_rate": 45.4
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 146,
                "percentage": 5.5,
                "peak_hour_volume": 88,
                "avg_transaction_value": 2841.7,
                "repeat_customer_rate": 59.9
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 101,
                "percentage": 3.8,
                "peak_hour_volume": 30,
                "avg_transaction_value": 4886.09,
                "repeat_customer_rate": 54.0
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 2535580,
                "percentage": 35.6,
                "avg_order_value": 2660.63,
                "growth_rate": -0.8,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 5221757,
                "percentage": 40.5,
                "avg_order_value": 4812.68,
                "growth_rate": 12.5,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1355651,
                "percentage": 14.6,
                "avg_order_value": 3467.14,
                "growth_rate": -3.4,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 414888,
                "percentage": 5.5,
                "avg_order_value": 2841.7,
                "growth_rate": 7.3,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 493495,
                "percentage": 3.8,
                "avg_order_value": 4886.09,
                "growth_rate": 8.5,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 2660.63,
                "median_ticket_size": 2261.54,
                "percentile_90": 4789.13,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 4812.68,
                "median_ticket_size": 4090.78,
                "percentile_90": 8662.82,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3467.14,
                "median_ticket_size": 2947.07,
                "percentile_90": 6240.85,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 2841.7,
                "median_ticket_size": 2415.44,
                "percentile_90": 5115.06,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4886.09,
                "median_ticket_size": 4153.18,
                "percentile_90": 8794.96,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 68.05,
            "total_attempts": 3289,
            "successful_transactions": 2988,
            "failed_transactions": 301,
            "processing_time_avg": 40.6,
            "retry_success_rate": 37.2,
            "peak_hour_sr": 92.8,
            "off_peak_sr": 89.3
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 93.26,
                "total_attempts": 988,
                "successful": 921,
                "failed": 67,
                "avg_processing_time": 42.3,
                "peak_volume_hour": "15:00-20:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.01,
                "total_attempts": 1129,
                "successful": 1050,
                "failed": 79,
                "avg_processing_time": 54.2,
                "peak_volume_hour": "16:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.24,
                "total_attempts": 423,
                "successful": 369,
                "failed": 54,
                "avg_processing_time": 73.4,
                "peak_volume_hour": "16:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 80.55,
                "total_attempts": 169,
                "successful": 136,
                "failed": 33,
                "avg_processing_time": 43.3,
                "peak_volume_hour": "17:00-21:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 88.86,
                "total_attempts": 112,
                "successful": 99,
                "failed": 13,
                "avg_processing_time": 46.4,
                "peak_volume_hour": "18:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 89,
                    "percentage": 29.6
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 78,
                    "percentage": 25.9
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 62,
                    "percentage": 20.6
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 41,
                    "percentage": 13.6
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 31,
                    "percentage": 10.3
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 11.8,
                "06-12": 8.9,
                "12-18": 8.1,
                "18-24": 9.7
            },
            "retry_attempts": {
                "single_retry": 191,
                "multiple_retries": 110,
                "success_after_retry": 112
            }
        },
        "gmv_breakdown": {
            "total_gmv": 563784.2,
            "successful_gmv": 512334.75,
            "failed_gmv": 51449.45,
            "payment_method_gmv": {
                "UPI": 197834.6,
                "CREDIT_CARD": 221456.85,
                "DEBIT_CARD": 65723.4,
                "NET_BANKING": 27319.9
            },
            "average_transaction_value": 171.44,
            "high_value_transactions": 48,
            "micro_transactions": 1312
        },
        "device_analytics": {
            "mobile_sr": 91.8,
            "desktop_sr": 90.3,
            "tablet_sr": 88.9,
            "mobile_percentage": 78.4,
            "desktop_percentage": 19.1,
            "tablet_percentage": 2.5,
            "ios_sr": 92.9,
            "android_sr": 91.2,
            "windows_sr": 89.7
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 92.1,
                    "volume": 923
                },
                {
                    "city": "Delhi",
                    "success_rate": 90.6,
                    "volume": 784
                },
                {
                    "city": "Bangalore",
                    "success_rate": 93.2,
                    "volume": 656
                },
                {
                    "city": "Chennai",
                    "success_rate": 89.8,
                    "volume": 498
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 91.7,
                    "volume": 428
                }
            ],
            "state_performance": {
                "Maharashtra": 92.1,
                "Karnataka": 93.2,
                "Delhi": 90.6,
                "Tamil Nadu": 89.8,
                "Telangana": 91.7
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "solid_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 91.2,
                "afternoon": 91.6,
                "evening": 91.3,
                "night": 89.7
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 91.1,
            "weekend_sr": 90.2,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "NET_BANKING",
                "count": 24,
                "percentage": 9.8,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "WALLET",
                "count": 23,
                "percentage": 9.3,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 35,
                "percentage": 14.2,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 24,
                "percentage": 9.8,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "WALLET",
                "count": 32,
                "percentage": 13.0,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "NET_BANKING",
                "count": 27,
                "percentage": 11.0,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 921,
                "percentage": 35.8,
                "peak_hour_volume": 73,
                "avg_transaction_value": 6423.85,
                "repeat_customer_rate": 48.0
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1050,
                "percentage": 40.8,
                "peak_hour_volume": 46,
                "avg_transaction_value": 3392.58,
                "repeat_customer_rate": 67.5
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 369,
                "percentage": 14.3,
                "peak_hour_volume": 94,
                "avg_transaction_value": 4637.73,
                "repeat_customer_rate": 62.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 136,
                "percentage": 5.3,
                "peak_hour_volume": 105,
                "avg_transaction_value": 4243.27,
                "repeat_customer_rate": 69.6
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 99,
                "percentage": 3.8,
                "peak_hour_volume": 121,
                "avg_transaction_value": 2766.5,
                "repeat_customer_rate": 51.1
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5916365,
                "percentage": 35.8,
                "avg_order_value": 6423.85,
                "growth_rate": 1.7,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3562209,
                "percentage": 40.8,
                "avg_order_value": 3392.58,
                "growth_rate": 13.8,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1711322,
                "percentage": 14.3,
                "avg_order_value": 4637.73,
                "growth_rate": -0.4,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 577084,
                "percentage": 5.3,
                "avg_order_value": 4243.27,
                "growth_rate": 1.1,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 273883,
                "percentage": 3.8,
                "avg_order_value": 2766.5,
                "growth_rate": 3.1,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 6423.85,
                "median_ticket_size": 5460.27,
                "percentile_90": 11562.93,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3392.58,
                "median_ticket_size": 2883.69,
                "percentile_90": 6106.64,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4637.73,
                "median_ticket_size": 3942.07,
                "percentile_90": 8347.91,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4243.27,
                "median_ticket_size": 3606.78,
                "percentile_90": 7637.89,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 2766.5,
                "median_ticket_size": 2351.53,
                "percentile_90": 4979.7,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.73,
            "total_attempts": 3567,
            "successful_transactions": 3345,
            "failed_transactions": 222,
            "processing_time_avg": 36.4,
            "retry_success_rate": 41.4,
            "peak_hour_sr": 95.7,
            "off_peak_sr": 92.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 89.86,
                "total_attempts": 1276,
                "successful": 1146,
                "failed": 130,
                "avg_processing_time": 74.9,
                "peak_volume_hour": "17:00-16:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 89.98,
                "total_attempts": 1458,
                "successful": 1311,
                "failed": 147,
                "avg_processing_time": 35.9,
                "peak_volume_hour": "17:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 86.01,
                "total_attempts": 546,
                "successful": 469,
                "failed": 77,
                "avg_processing_time": 73.6,
                "peak_volume_hour": "18:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 78.0,
                "total_attempts": 218,
                "successful": 170,
                "failed": 48,
                "avg_processing_time": 33.2,
                "peak_volume_hour": "21:00-18:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 88.32,
                "total_attempts": 145,
                "successful": 128,
                "failed": 17,
                "avg_processing_time": 54.9,
                "peak_volume_hour": "16:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 71,
                    "percentage": 32.0
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 56,
                    "percentage": 25.2
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 44,
                    "percentage": 19.8
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 29,
                    "percentage": 13.1
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 22,
                    "percentage": 9.9
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 8.1,
                "06-12": 5.8,
                "12-18": 5.2,
                "18-24": 6.7
            },
            "retry_attempts": {
                "single_retry": 141,
                "multiple_retries": 81,
                "success_after_retry": 92
            }
        },
        "gmv_breakdown": {
            "total_gmv": 649834.75,
            "successful_gmv": 609187.4,
            "failed_gmv": 40647.35,
            "payment_method_gmv": {
                "UPI": 227456.8,
                "CREDIT_CARD": 259834.65,
                "DEBIT_CARD": 78923.4,
                "NET_BANKING": 42972.55
            },
            "average_transaction_value": 182.15,
            "high_value_transactions": 64,
            "micro_transactions": 1456
        },
        "device_analytics": {
            "mobile_sr": 94.6,
            "desktop_sr": 93.4,
            "tablet_sr": 92.1,
            "mobile_percentage": 76.2,
            "desktop_percentage": 21.1,
            "tablet_percentage": 2.7,
            "ios_sr": 95.8,
            "android_sr": 94.1,
            "windows_sr": 93.0
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 95.1,
                    "volume": 1087
                },
                {
                    "city": "Delhi",
                    "success_rate": 93.8,
                    "volume": 891
                },
                {
                    "city": "Bangalore",
                    "success_rate": 95.9,
                    "volume": 734
                },
                {
                    "city": "Chennai",
                    "success_rate": 92.7,
                    "volume": 567
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 94.6,
                    "volume": 288
                }
            ],
            "state_performance": {
                "Maharashtra": 95.1,
                "Karnataka": 95.9,
                "Delhi": 93.8,
                "Tamil Nadu": 92.7,
                "Telangana": 94.6
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "20:00-22:00"
                ],
                "low_performance_hours": [
                    "01:00-05:00"
                ],
                "weekend_pattern": "outstanding_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 94.1,
                "afternoon": 94.6,
                "evening": 94.3,
                "night": 92.8
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 94.1,
            "weekend_sr": 93.2,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 39,
                "percentage": 9.3,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "DEBIT_CARD",
                "count": 16,
                "percentage": 3.8,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "NET_BANKING",
                "count": 39,
                "percentage": 9.3,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 35,
                "percentage": 8.4,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "UPI",
                "count": 30,
                "percentage": 7.2,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "NET_BANKING",
                "count": 15,
                "percentage": 3.6,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1146,
                "percentage": 35.5,
                "peak_hour_volume": 24,
                "avg_transaction_value": 4148.14,
                "repeat_customer_rate": 58.8
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1311,
                "percentage": 40.7,
                "peak_hour_volume": 127,
                "avg_transaction_value": 3777.26,
                "repeat_customer_rate": 51.8
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 469,
                "percentage": 14.5,
                "peak_hour_volume": 87,
                "avg_transaction_value": 5378.21,
                "repeat_customer_rate": 61.8
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 170,
                "percentage": 5.3,
                "peak_hour_volume": 146,
                "avg_transaction_value": 4644.57,
                "repeat_customer_rate": 53.4
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 128,
                "percentage": 4.0,
                "peak_hour_volume": 129,
                "avg_transaction_value": 5231.21,
                "repeat_customer_rate": 64.2
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4753768,
                "percentage": 35.5,
                "avg_order_value": 4148.14,
                "growth_rate": -1.8,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 4951987,
                "percentage": 40.7,
                "avg_order_value": 3777.26,
                "growth_rate": 4.0,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2522380,
                "percentage": 14.5,
                "avg_order_value": 5378.21,
                "growth_rate": 11.7,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 789576,
                "percentage": 5.3,
                "avg_order_value": 4644.57,
                "growth_rate": 6.2,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 669594,
                "percentage": 4.0,
                "avg_order_value": 5231.21,
                "growth_rate": 5.5,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4148.14,
                "median_ticket_size": 3525.92,
                "percentile_90": 7466.65,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3777.26,
                "median_ticket_size": 3210.67,
                "percentile_90": 6799.07,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 5378.21,
                "median_ticket_size": 4571.48,
                "percentile_90": 9680.78,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4644.57,
                "median_ticket_size": 3947.88,
                "percentile_90": 8360.23,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5231.21,
                "median_ticket_size": 4446.53,
                "percentile_90": 9416.18,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 64.75,
            "total_attempts": 3024,
            "successful_transactions": 2660,
            "failed_transactions": 364,
            "processing_time_avg": 43.7,
            "retry_success_rate": 33.0,
            "peak_hour_sr": 90.2,
            "off_peak_sr": 86.4
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.37,
                "total_attempts": 1216,
                "successful": 1123,
                "failed": 93,
                "avg_processing_time": 24.8,
                "peak_volume_hour": "18:00-21:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.61,
                "total_attempts": 1390,
                "successful": 1259,
                "failed": 131,
                "avg_processing_time": 41.9,
                "peak_volume_hour": "14:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 87.96,
                "total_attempts": 521,
                "successful": 458,
                "failed": 63,
                "avg_processing_time": 44.3,
                "peak_volume_hour": "16:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 83.01,
                "total_attempts": 208,
                "successful": 172,
                "failed": 36,
                "avg_processing_time": 26.5,
                "peak_volume_hour": "16:00-16:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.97,
                "total_attempts": 139,
                "successful": 122,
                "failed": 17,
                "avg_processing_time": 50.1,
                "peak_volume_hour": "20:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 108,
                    "percentage": 29.7
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 101,
                    "percentage": 27.7
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 78,
                    "percentage": 21.4
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 47,
                    "percentage": 12.9
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 30,
                    "percentage": 8.2
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 15.2,
                "06-12": 11.8,
                "12-18": 10.1,
                "18-24": 12.9
            },
            "retry_attempts": {
                "single_retry": 232,
                "multiple_retries": 132,
                "success_after_retry": 120
            }
        },
        "gmv_breakdown": {
            "total_gmv": 503789.6,
            "successful_gmv": 443156.25,
            "failed_gmv": 60633.35,
            "payment_method_gmv": {
                "UPI": 176234.7,
                "CREDIT_CARD": 182934.85,
                "DEBIT_CARD": 58456.3,
                "NET_BANKING": 25530.4
            },
            "average_transaction_value": 166.61,
            "high_value_transactions": 37,
            "micro_transactions": 1178
        },
        "device_analytics": {
            "mobile_sr": 89.1,
            "desktop_sr": 87.4,
            "tablet_sr": 85.9,
            "mobile_percentage": 79.8,
            "desktop_percentage": 18.4,
            "tablet_percentage": 1.8,
            "ios_sr": 90.4,
            "android_sr": 88.6,
            "windows_sr": 86.9
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 89.6,
                    "volume": 834
                },
                {
                    "city": "Delhi",
                    "success_rate": 87.3,
                    "volume": 701
                },
                {
                    "city": "Bangalore",
                    "success_rate": 90.8,
                    "volume": 589
                },
                {
                    "city": "Chennai",
                    "success_rate": 86.2,
                    "volume": 456
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 89.1,
                    "volume": 444
                }
            ],
            "state_performance": {
                "Maharashtra": 89.6,
                "Karnataka": 90.8,
                "Delhi": 87.3,
                "Tamil Nadu": 86.2,
                "Telangana": 89.1
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "18:00-20:00"
                ],
                "low_performance_hours": [
                    "03:00-07:00"
                ],
                "weekend_pattern": "moderate_challenges"
            },
            "success_rate_by_time_of_day": {
                "morning": 88.4,
                "afternoon": 89.1,
                "evening": 88.7,
                "night": 86.1
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 88.3,
            "weekend_sr": 87.1,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "NET_BANKING",
                "count": 30,
                "percentage": 8.8,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 44,
                "percentage": 12.9,
                "avg_retry_attempts": 1.3,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "WALLET",
                "count": 36,
                "percentage": 10.6,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 32,
                "percentage": 9.4,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "UPI",
                "count": 21,
                "percentage": 6.2,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "UPI",
                "count": 35,
                "percentage": 10.3,
                "avg_retry_attempts": 1.0,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1123,
                "percentage": 35.8,
                "peak_hour_volume": 70,
                "avg_transaction_value": 3792.95,
                "repeat_customer_rate": 51.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1259,
                "percentage": 40.2,
                "peak_hour_volume": 87,
                "avg_transaction_value": 3604.46,
                "repeat_customer_rate": 64.2
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 458,
                "percentage": 14.6,
                "peak_hour_volume": 60,
                "avg_transaction_value": 3509.54,
                "repeat_customer_rate": 59.9
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 172,
                "percentage": 5.5,
                "peak_hour_volume": 104,
                "avg_transaction_value": 4005.21,
                "repeat_customer_rate": 55.0
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 122,
                "percentage": 3.9,
                "peak_hour_volume": 69,
                "avg_transaction_value": 4361.06,
                "repeat_customer_rate": 48.2
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 4259482,
                "percentage": 35.8,
                "avg_order_value": 3792.95,
                "growth_rate": 16.1,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 4538015,
                "percentage": 40.2,
                "avg_order_value": 3604.46,
                "growth_rate": -1.2,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1607369,
                "percentage": 14.6,
                "avg_order_value": 3509.54,
                "growth_rate": 3.5,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 688896,
                "percentage": 5.5,
                "avg_order_value": 4005.21,
                "growth_rate": 13.8,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 532049,
                "percentage": 3.9,
                "avg_order_value": 4361.06,
                "growth_rate": 7.6,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 3792.95,
                "median_ticket_size": 3224.01,
                "percentile_90": 6827.31,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3604.46,
                "median_ticket_size": 3063.79,
                "percentile_90": 6488.03,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 3509.54,
                "median_ticket_size": 2983.11,
                "percentile_90": 6317.17,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4005.21,
                "median_ticket_size": 3404.43,
                "percentile_90": 7209.38,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4361.06,
                "median_ticket_size": 3706.9,
                "percentile_90": 7849.91,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 66.93,
            "total_attempts": 3398,
            "successful_transactions": 3115,
            "failed_transactions": 283,
            "processing_time_avg": 39.1,
            "retry_success_rate": 38.9,
            "peak_hour_sr": 93.4,
            "off_peak_sr": 90.2
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.8,
                "total_attempts": 1277,
                "successful": 1172,
                "failed": 105,
                "avg_processing_time": 78.1,
                "peak_volume_hour": "15:00-21:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.14,
                "total_attempts": 1460,
                "successful": 1359,
                "failed": 101,
                "avg_processing_time": 37.6,
                "peak_volume_hour": "20:00-22:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 88.95,
                "total_attempts": 547,
                "successful": 486,
                "failed": 61,
                "avg_processing_time": 49.4,
                "peak_volume_hour": "18:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 78.49,
                "total_attempts": 219,
                "successful": 171,
                "failed": 48,
                "avg_processing_time": 44.8,
                "peak_volume_hour": "16:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.31,
                "total_attempts": 146,
                "successful": 127,
                "failed": 19,
                "avg_processing_time": 44.9,
                "peak_volume_hour": "18:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 84,
                    "percentage": 29.7
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 72,
                    "percentage": 25.4
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 58,
                    "percentage": 20.5
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 37,
                    "percentage": 13.1
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 32,
                    "percentage": 11.3
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 10.8,
                "06-12": 7.9,
                "12-18": 7.2,
                "18-24": 8.9
            },
            "retry_attempts": {
                "single_retry": 180,
                "multiple_retries": 103,
                "success_after_retry": 110
            }
        },
        "gmv_breakdown": {
            "total_gmv": 587234.8,
            "successful_gmv": 538465.7,
            "failed_gmv": 48769.1,
            "payment_method_gmv": {
                "UPI": 205634.5,
                "CREDIT_CARD": 232187.65,
                "DEBIT_CARD": 68923.4,
                "NET_BANKING": 32720.15
            },
            "average_transaction_value": 172.85,
            "high_value_transactions": 49,
            "micro_transactions": 1367
        },
        "device_analytics": {
            "mobile_sr": 92.7,
            "desktop_sr": 91.2,
            "tablet_sr": 89.8,
            "mobile_percentage": 77.6,
            "desktop_percentage": 19.7,
            "tablet_percentage": 2.7,
            "ios_sr": 93.9,
            "android_sr": 92.1,
            "windows_sr": 90.8
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 92.8,
                    "volume": 967
                },
                {
                    "city": "Delhi",
                    "success_rate": 91.4,
                    "volume": 823
                },
                {
                    "city": "Bangalore",
                    "success_rate": 93.6,
                    "volume": 678
                },
                {
                    "city": "Chennai",
                    "success_rate": 90.7,
                    "volume": 523
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 92.3,
                    "volume": 407
                }
            ],
            "state_performance": {
                "Maharashtra": 92.8,
                "Karnataka": 93.6,
                "Delhi": 91.4,
                "Tamil Nadu": 90.7,
                "Telangana": 92.3
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "strong_recovery"
            },
            "success_rate_by_time_of_day": {
                "morning": 92.1,
                "afternoon": 92.5,
                "evening": 92.2,
                "night": 90.4
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 92.0,
            "weekend_sr": 91.1,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 35,
                "percentage": 10.5,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 19,
                "percentage": 5.7,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "UPI",
                "count": 44,
                "percentage": 13.2,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "CREDIT_CARD",
                "count": 45,
                "percentage": 13.5,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "WALLET",
                "count": 40,
                "percentage": 12.0,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 31,
                "percentage": 9.3,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1172,
                "percentage": 35.4,
                "peak_hour_volume": 121,
                "avg_transaction_value": 4917.0,
                "repeat_customer_rate": 71.5
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1359,
                "percentage": 41.0,
                "peak_hour_volume": 17,
                "avg_transaction_value": 4704.54,
                "repeat_customer_rate": 68.9
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 486,
                "percentage": 14.7,
                "peak_hour_volume": 150,
                "avg_transaction_value": 4021.22,
                "repeat_customer_rate": 67.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 171,
                "percentage": 5.2,
                "peak_hour_volume": 126,
                "avg_transaction_value": 4799.01,
                "repeat_customer_rate": 54.9
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 127,
                "percentage": 3.8,
                "peak_hour_volume": 117,
                "avg_transaction_value": 5424.61,
                "repeat_customer_rate": 54.9
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5762724,
                "percentage": 35.4,
                "avg_order_value": 4917.0,
                "growth_rate": 19.8,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 6393469,
                "percentage": 41.0,
                "avg_order_value": 4704.54,
                "growth_rate": 4.1,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1954312,
                "percentage": 14.7,
                "avg_order_value": 4021.22,
                "growth_rate": 17.5,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 820630,
                "percentage": 5.2,
                "avg_order_value": 4799.01,
                "growth_rate": 2.2,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 688925,
                "percentage": 3.8,
                "avg_order_value": 5424.61,
                "growth_rate": 19.3,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4917.0,
                "median_ticket_size": 4179.45,
                "percentile_90": 8850.6,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 4704.54,
                "median_ticket_size": 3998.86,
                "percentile_90": 8468.17,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4021.22,
                "median_ticket_size": 3418.04,
                "percentile_90": 7238.2,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 4799.01,
                "median_ticket_size": 4079.16,
                "percentile_90": 8638.22,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5424.61,
                "median_ticket_size": 4610.92,
                "percentile_90": 9764.3,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 66.41,
            "total_attempts": 3789,
            "successful_transactions": 3608,
            "failed_transactions": 181,
            "processing_time_avg": 34.2,
            "retry_success_rate": 46.4,
            "peak_hour_sr": 97.1,
            "off_peak_sr": 94.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 90.24,
                "total_attempts": 1041,
                "successful": 939,
                "failed": 102,
                "avg_processing_time": 27.0,
                "peak_volume_hour": "18:00-17:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 91.66,
                "total_attempts": 1190,
                "successful": 1090,
                "failed": 100,
                "avg_processing_time": 60.3,
                "peak_volume_hour": "16:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 91.29,
                "total_attempts": 446,
                "successful": 407,
                "failed": 39,
                "avg_processing_time": 63.1,
                "peak_volume_hour": "15:00-23:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 80.68,
                "total_attempts": 178,
                "successful": 143,
                "failed": 35,
                "avg_processing_time": 77.1,
                "peak_volume_hour": "15:00-21:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 83.19,
                "total_attempts": 119,
                "successful": 98,
                "failed": 21,
                "avg_processing_time": 53.8,
                "peak_volume_hour": "16:00-21:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 58,
                    "percentage": 32.0
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 43,
                    "percentage": 23.8
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 32,
                    "percentage": 17.7
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 26,
                    "percentage": 14.4
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 22,
                    "percentage": 12.2
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 6.1,
                "06-12": 4.2,
                "12-18": 3.8,
                "18-24": 5.1
            },
            "retry_attempts": {
                "single_retry": 115,
                "multiple_retries": 66,
                "success_after_retry": 84
            }
        },
        "gmv_breakdown": {
            "total_gmv": 712456.85,
            "successful_gmv": 678234.2,
            "failed_gmv": 34222.65,
            "payment_method_gmv": {
                "UPI": 248956.75,
                "CREDIT_CARD": 294734.5,
                "DEBIT_CARD": 87234.6,
                "NET_BANKING": 47308.35
            },
            "average_transaction_value": 188.05,
            "high_value_transactions": 72,
            "micro_transactions": 1598
        },
        "device_analytics": {
            "mobile_sr": 96.1,
            "desktop_sr": 94.8,
            "tablet_sr": 93.5,
            "mobile_percentage": 74.9,
            "desktop_percentage": 22.3,
            "tablet_percentage": 2.8,
            "ios_sr": 97.2,
            "android_sr": 95.6,
            "windows_sr": 94.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 96.4,
                    "volume": 1234
                },
                {
                    "city": "Delhi",
                    "success_rate": 95.1,
                    "volume": 967
                },
                {
                    "city": "Bangalore",
                    "success_rate": 97.2,
                    "volume": 823
                },
                {
                    "city": "Chennai",
                    "success_rate": 94.8,
                    "volume": 623
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 96.1,
                    "volume": 142
                }
            ],
            "state_performance": {
                "Maharashtra": 96.4,
                "Karnataka": 97.2,
                "Delhi": 95.1,
                "Tamil Nadu": 94.8,
                "Telangana": 96.1
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "13:00-15:00",
                    "21:00-23:00"
                ],
                "low_performance_hours": [
                    "01:00-04:00"
                ],
                "weekend_pattern": "exceptional_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 95.7,
                "afternoon": 96.1,
                "evening": 95.9,
                "night": 94.2
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 95.6,
            "weekend_sr": 94.7,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "DEBIT_CARD",
                "count": 20,
                "percentage": 6.7,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "UPI",
                "count": 40,
                "percentage": 13.5,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 25,
                "percentage": 8.4,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "NET_BANKING",
                "count": 18,
                "percentage": 6.1,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "WALLET",
                "count": 44,
                "percentage": 14.8,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "UPI",
                "count": 48,
                "percentage": 16.2,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 939,
                "percentage": 35.1,
                "peak_hour_volume": 77,
                "avg_transaction_value": 3459.06,
                "repeat_customer_rate": 54.1
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1090,
                "percentage": 40.7,
                "peak_hour_volume": 17,
                "avg_transaction_value": 3541.68,
                "repeat_customer_rate": 57.5
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 407,
                "percentage": 15.2,
                "peak_hour_volume": 69,
                "avg_transaction_value": 2725.6,
                "repeat_customer_rate": 70.7
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 143,
                "percentage": 5.3,
                "peak_hour_volume": 62,
                "avg_transaction_value": 2638.14,
                "repeat_customer_rate": 74.6
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 98,
                "percentage": 3.7,
                "peak_hour_volume": 124,
                "avg_transaction_value": 4410.24,
                "repeat_customer_rate": 73.9
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 3248057,
                "percentage": 35.1,
                "avg_order_value": 3459.06,
                "growth_rate": 5.9,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3860431,
                "percentage": 40.7,
                "avg_order_value": 3541.68,
                "growth_rate": 10.0,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 1109319,
                "percentage": 15.2,
                "avg_order_value": 2725.6,
                "growth_rate": 0.1,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 377254,
                "percentage": 5.3,
                "avg_order_value": 2638.14,
                "growth_rate": 18.2,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 432203,
                "percentage": 3.7,
                "avg_order_value": 4410.24,
                "growth_rate": 0.2,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 3459.06,
                "median_ticket_size": 2940.2,
                "percentile_90": 6226.31,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3541.68,
                "median_ticket_size": 3010.43,
                "percentile_90": 6375.02,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 2725.6,
                "median_ticket_size": 2316.76,
                "percentile_90": 4906.08,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 2638.14,
                "median_ticket_size": 2242.42,
                "percentile_90": 4748.65,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 4410.24,
                "median_ticket_size": 3748.7,
                "percentile_90": 7938.43,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.74,
            "total_attempts": 3142,
            "successful_transactions": 2811,
            "failed_transactions": 331,
            "processing_time_avg": 42.1,
            "retry_success_rate": 35.3,
            "peak_hour_sr": 91.8,
            "off_peak_sr": 87.9
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.85,
                "total_attempts": 1269,
                "successful": 1203,
                "failed": 66,
                "avg_processing_time": 32.2,
                "peak_volume_hour": "17:00-15:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.17,
                "total_attempts": 1451,
                "successful": 1337,
                "failed": 114,
                "avg_processing_time": 22.2,
                "peak_volume_hour": "22:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 89.88,
                "total_attempts": 544,
                "successful": 488,
                "failed": 56,
                "avg_processing_time": 55.3,
                "peak_volume_hour": "19:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 83.47,
                "total_attempts": 217,
                "successful": 181,
                "failed": 36,
                "avg_processing_time": 68.9,
                "peak_volume_hour": "20:00-19:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 83.61,
                "total_attempts": 145,
                "successful": 121,
                "failed": 24,
                "avg_processing_time": 49.8,
                "peak_volume_hour": "16:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 97,
                    "percentage": 29.3
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 89,
                    "percentage": 26.9
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 68,
                    "percentage": 20.5
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 44,
                    "percentage": 13.3
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 33,
                    "percentage": 10.0
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 13.7,
                "06-12": 10.2,
                "12-18": 9.1,
                "18-24": 11.5
            },
            "retry_attempts": {
                "single_retry": 211,
                "multiple_retries": 120,
                "success_after_retry": 117
            }
        },
        "gmv_breakdown": {
            "total_gmv": 524739.8,
            "successful_gmv": 469456.25,
            "failed_gmv": 55283.55,
            "payment_method_gmv": {
                "UPI": 186234.7,
                "CREDIT_CARD": 196834.85,
                "DEBIT_CARD": 59467.4,
                "NET_BANKING": 26919.3
            },
            "average_transaction_value": 167.02,
            "high_value_transactions": 43,
            "micro_transactions": 1234
        },
        "device_analytics": {
            "mobile_sr": 90.6,
            "desktop_sr": 88.9,
            "tablet_sr": 87.4,
            "mobile_percentage": 78.7,
            "desktop_percentage": 19.1,
            "tablet_percentage": 2.2,
            "ios_sr": 91.9,
            "android_sr": 90.1,
            "windows_sr": 88.3
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 91.3,
                    "volume": 889
                },
                {
                    "city": "Delhi",
                    "success_rate": 88.9,
                    "volume": 734
                },
                {
                    "city": "Bangalore",
                    "success_rate": 92.1,
                    "volume": 612
                },
                {
                    "city": "Chennai",
                    "success_rate": 87.8,
                    "volume": 467
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 90.2,
                    "volume": 440
                }
            ],
            "state_performance": {
                "Maharashtra": 91.3,
                "Karnataka": 92.1,
                "Delhi": 88.9,
                "Tamil Nadu": 87.8,
                "Telangana": 90.2
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "03:00-07:00"
                ],
                "weekend_pattern": "variable_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 89.9,
                "afternoon": 90.4,
                "evening": 90.1,
                "night": 87.6
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 89.8,
            "weekend_sr": 88.7,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "DEBIT_CARD",
                "count": 30,
                "percentage": 10.1,
                "avg_retry_attempts": 1.5,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "CREDIT_CARD",
                "count": 48,
                "percentage": 16.2,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "DEBIT_CARD",
                "count": 30,
                "percentage": 10.1,
                "avg_retry_attempts": 1.9,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "WALLET",
                "count": 39,
                "percentage": 13.2,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "WALLET",
                "count": 24,
                "percentage": 8.1,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 33,
                "percentage": 11.1,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1203,
                "percentage": 36.1,
                "peak_hour_volume": 80,
                "avg_transaction_value": 3019.89,
                "repeat_customer_rate": 65.3
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1337,
                "percentage": 40.2,
                "peak_hour_volume": 97,
                "avg_transaction_value": 3835.11,
                "repeat_customer_rate": 68.0
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 488,
                "percentage": 14.7,
                "peak_hour_volume": 60,
                "avg_transaction_value": 6211.59,
                "repeat_customer_rate": 73.3
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 181,
                "percentage": 5.4,
                "peak_hour_volume": 59,
                "avg_transaction_value": 5153.11,
                "repeat_customer_rate": 70.8
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 121,
                "percentage": 3.6,
                "peak_hour_volume": 113,
                "avg_transaction_value": 5159.16,
                "repeat_customer_rate": 58.0
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 3632927,
                "percentage": 36.1,
                "avg_order_value": 3019.89,
                "growth_rate": 8.5,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 5127542,
                "percentage": 40.2,
                "avg_order_value": 3835.11,
                "growth_rate": 15.0,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 3031255,
                "percentage": 14.7,
                "avg_order_value": 6211.59,
                "growth_rate": 18.8,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 932712,
                "percentage": 5.4,
                "avg_order_value": 5153.11,
                "growth_rate": 17.1,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 624258,
                "percentage": 3.6,
                "avg_order_value": 5159.16,
                "growth_rate": 18.8,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 3019.89,
                "median_ticket_size": 2566.91,
                "percentile_90": 5435.8,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 3835.11,
                "median_ticket_size": 3259.84,
                "percentile_90": 6903.2,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 6211.59,
                "median_ticket_size": 5279.85,
                "percentile_90": 11180.86,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5153.11,
                "median_ticket_size": 4380.14,
                "percentile_90": 9275.6,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5159.16,
                "median_ticket_size": 4385.29,
                "percentile_90": 9286.49,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 68.33,
            "total_attempts": 3456,
            "successful_transactions": 3208,
            "failed_transactions": 248,
            "processing_time_avg": 37.9,
            "retry_success_rate": 40.7,
            "peak_hour_sr": 94.6,
            "off_peak_sr": 91.8
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 94.57,
                "total_attempts": 1232,
                "successful": 1165,
                "failed": 67,
                "avg_processing_time": 52.1,
                "peak_volume_hour": "20:00-22:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 91.91,
                "total_attempts": 1408,
                "successful": 1294,
                "failed": 114,
                "avg_processing_time": 35.0,
                "peak_volume_hour": "19:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 90.79,
                "total_attempts": 528,
                "successful": 479,
                "failed": 49,
                "avg_processing_time": 56.7,
                "peak_volume_hour": "21:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 76.96,
                "total_attempts": 211,
                "successful": 162,
                "failed": 49,
                "avg_processing_time": 59.9,
                "peak_volume_hour": "22:00-20:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.95,
                "total_attempts": 140,
                "successful": 123,
                "failed": 17,
                "avg_processing_time": 32.2,
                "peak_volume_hour": "18:00-19:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 78,
                    "percentage": 31.5
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 64,
                    "percentage": 25.8
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 51,
                    "percentage": 20.6
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 32,
                    "percentage": 12.9
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 23,
                    "percentage": 9.3
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 9.2,
                "06-12": 6.8,
                "12-18": 6.1,
                "18-24": 7.9
            },
            "retry_attempts": {
                "single_retry": 158,
                "multiple_retries": 90,
                "success_after_retry": 101
            }
        },
        "gmv_breakdown": {
            "total_gmv": 598734.6,
            "successful_gmv": 555621.85,
            "failed_gmv": 43112.75,
            "payment_method_gmv": {
                "UPI": 208456.8,
                "CREDIT_CARD": 241187.65,
                "DEBIT_CARD": 71923.4,
                "NET_BANKING": 34054.9
            },
            "average_transaction_value": 173.28,
            "high_value_transactions": 54,
            "micro_transactions": 1398
        },
        "device_analytics": {
            "mobile_sr": 93.7,
            "desktop_sr": 92.4,
            "tablet_sr": 91.1,
            "mobile_percentage": 77.2,
            "desktop_percentage": 20.1,
            "tablet_percentage": 2.7,
            "ios_sr": 94.8,
            "android_sr": 93.2,
            "windows_sr": 91.9
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 94.1,
                    "volume": 987
                },
                {
                    "city": "Delhi",
                    "success_rate": 92.6,
                    "volume": 834
                },
                {
                    "city": "Bangalore",
                    "success_rate": 94.9,
                    "volume": 689
                },
                {
                    "city": "Chennai",
                    "success_rate": 91.8,
                    "volume": 534
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 93.4,
                    "volume": 412
                }
            ],
            "state_performance": {
                "Maharashtra": 94.1,
                "Karnataka": 94.9,
                "Delhi": 92.6,
                "Tamil Nadu": 91.8,
                "Telangana": 93.4
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "20:00-22:00"
                ],
                "low_performance_hours": [
                    "01:00-05:00"
                ],
                "weekend_pattern": "consistently_strong"
            },
            "success_rate_by_time_of_day": {
                "morning": 93.2,
                "afternoon": 93.7,
                "evening": 93.4,
                "night": 91.6
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 93.1,
            "weekend_sr": 92.3,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "DEBIT_CARD",
                "count": 25,
                "percentage": 8.4,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "WALLET",
                "count": 30,
                "percentage": 10.1,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "NET_BANKING",
                "count": 36,
                "percentage": 12.2,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 19,
                "percentage": 6.4,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 27,
                "percentage": 9.1,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "UPI",
                "count": 31,
                "percentage": 10.5,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1165,
                "percentage": 36.1,
                "peak_hour_volume": 84,
                "avg_transaction_value": 5848.18,
                "repeat_customer_rate": 50.9
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1294,
                "percentage": 40.1,
                "peak_hour_volume": 85,
                "avg_transaction_value": 4188.82,
                "repeat_customer_rate": 60.4
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 479,
                "percentage": 14.9,
                "peak_hour_volume": 87,
                "avg_transaction_value": 4495.42,
                "repeat_customer_rate": 69.3
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 162,
                "percentage": 5.0,
                "peak_hour_volume": 47,
                "avg_transaction_value": 5442.7,
                "repeat_customer_rate": 73.3
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 123,
                "percentage": 3.8,
                "peak_hour_volume": 106,
                "avg_transaction_value": 6486.62,
                "repeat_customer_rate": 57.1
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 6813129,
                "percentage": 36.1,
                "avg_order_value": 5848.18,
                "growth_rate": 11.1,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 5420333,
                "percentage": 40.1,
                "avg_order_value": 4188.82,
                "growth_rate": -5.0,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2153306,
                "percentage": 14.9,
                "avg_order_value": 4495.42,
                "growth_rate": 0.7,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 881717,
                "percentage": 5.0,
                "avg_order_value": 5442.7,
                "growth_rate": 16.8,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 797854,
                "percentage": 3.8,
                "avg_order_value": 6486.62,
                "growth_rate": 6.6,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 5848.18,
                "median_ticket_size": 4970.95,
                "percentile_90": 10526.72,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 4188.82,
                "median_ticket_size": 3560.5,
                "percentile_90": 7539.88,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4495.42,
                "median_ticket_size": 3821.11,
                "percentile_90": 8091.76,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 5442.7,
                "median_ticket_size": 4626.3,
                "percentile_90": 9796.86,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 6486.62,
                "median_ticket_size": 5513.63,
                "percentile_90": 11675.92,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 69.47,
            "total_attempts": 2987,
            "successful_transactions": 2634,
            "failed_transactions": 353,
            "processing_time_avg": 44.6,
            "retry_success_rate": 32.0,
            "peak_hour_sr": 90.4,
            "off_peak_sr": 86.7
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 89.61,
                "total_attempts": 1257,
                "successful": 1126,
                "failed": 131,
                "avg_processing_time": 71.3,
                "peak_volume_hour": "18:00-23:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 92.41,
                "total_attempts": 1437,
                "successful": 1327,
                "failed": 110,
                "avg_processing_time": 41.5,
                "peak_volume_hour": "16:00-16:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 85.85,
                "total_attempts": 538,
                "successful": 461,
                "failed": 77,
                "avg_processing_time": 73.9,
                "peak_volume_hour": "20:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 75.0,
                "total_attempts": 215,
                "successful": 161,
                "failed": 54,
                "avg_processing_time": 56.2,
                "peak_volume_hour": "20:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 89.38,
                "total_attempts": 143,
                "successful": 127,
                "failed": 16,
                "avg_processing_time": 60.3,
                "peak_volume_hour": "17:00-18:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "CARD_DECLINED",
                    "count": 105,
                    "percentage": 29.7
                },
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 98,
                    "percentage": 27.8
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 76,
                    "percentage": 21.5
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 45,
                    "percentage": 12.7
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 29,
                    "percentage": 8.2
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 15.9,
                "06-12": 11.4,
                "12-18": 10.1,
                "18-24": 13.2
            },
            "retry_attempts": {
                "single_retry": 225,
                "multiple_retries": 128,
                "success_after_retry": 113
            }
        },
        "gmv_breakdown": {
            "total_gmv": 496734.85,
            "successful_gmv": 437923.4,
            "failed_gmv": 58811.45,
            "payment_method_gmv": {
                "UPI": 173456.7,
                "CREDIT_CARD": 179834.95,
                "DEBIT_CARD": 57234.6,
                "NET_BANKING": 27397.15
            },
            "average_transaction_value": 166.31,
            "high_value_transactions": 35,
            "micro_transactions": 1156
        },
        "device_analytics": {
            "mobile_sr": 89.2,
            "desktop_sr": 87.6,
            "tablet_sr": 85.9,
            "mobile_percentage": 79.6,
            "desktop_percentage": 18.7,
            "tablet_percentage": 1.7,
            "ios_sr": 90.5,
            "android_sr": 88.7,
            "windows_sr": 87.1
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 89.8,
                    "volume": 823
                },
                {
                    "city": "Delhi",
                    "success_rate": 87.4,
                    "volume": 697
                },
                {
                    "city": "Bangalore",
                    "success_rate": 90.6,
                    "volume": 578
                },
                {
                    "city": "Chennai",
                    "success_rate": 86.3,
                    "volume": 445
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 88.9,
                    "volume": 444
                }
            ],
            "state_performance": {
                "Maharashtra": 89.8,
                "Karnataka": 90.6,
                "Delhi": 87.4,
                "Tamil Nadu": 86.3,
                "Telangana": 88.9
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "18:00-20:00"
                ],
                "low_performance_hours": [
                    "03:00-07:00"
                ],
                "weekend_pattern": "modest_challenges"
            },
            "success_rate_by_time_of_day": {
                "morning": 88.6,
                "afternoon": 89.1,
                "evening": 88.8,
                "night": 86.2
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 88.5,
            "weekend_sr": 87.3,
            "monday_peak": True,
            "friday_peak": False
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "NET_BANKING",
                "count": 29,
                "percentage": 7.5,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "DEBIT_CARD",
                "count": 19,
                "percentage": 4.9,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "CREDIT_CARD",
                "count": 31,
                "percentage": 8.0,
                "avg_retry_attempts": 1.2,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "DEBIT_CARD",
                "count": 17,
                "percentage": 4.4,
                "avg_retry_attempts": 2.4,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "DEBIT_CARD",
                "count": 22,
                "percentage": 5.7,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "NET_BANKING",
                "count": 26,
                "percentage": 6.7,
                "avg_retry_attempts": 2.3,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1126,
                "percentage": 35.2,
                "peak_hour_volume": 67,
                "avg_transaction_value": 4475.22,
                "repeat_customer_rate": 46.1
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1327,
                "percentage": 41.4,
                "peak_hour_volume": 28,
                "avg_transaction_value": 2846.52,
                "repeat_customer_rate": 60.5
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 461,
                "percentage": 14.4,
                "peak_hour_volume": 107,
                "avg_transaction_value": 5111.21,
                "repeat_customer_rate": 48.6
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 161,
                "percentage": 5.0,
                "peak_hour_volume": 86,
                "avg_transaction_value": 3366.09,
                "repeat_customer_rate": 72.1
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 127,
                "percentage": 4.0,
                "peak_hour_volume": 119,
                "avg_transaction_value": 5949.32,
                "repeat_customer_rate": 59.5
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5039097,
                "percentage": 35.2,
                "avg_order_value": 4475.22,
                "growth_rate": 2.1,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 3777332,
                "percentage": 41.4,
                "avg_order_value": 2846.52,
                "growth_rate": -1.0,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2356267,
                "percentage": 14.4,
                "avg_order_value": 5111.21,
                "growth_rate": 3.9,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 541940,
                "percentage": 5.0,
                "avg_order_value": 3366.09,
                "growth_rate": -3.0,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 755563,
                "percentage": 4.0,
                "avg_order_value": 5949.32,
                "growth_rate": 4.6,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4475.22,
                "median_ticket_size": 3803.94,
                "percentile_90": 8055.4,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 2846.52,
                "median_ticket_size": 2419.54,
                "percentile_90": 5123.74,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 5111.21,
                "median_ticket_size": 4344.53,
                "percentile_90": 9200.18,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 3366.09,
                "median_ticket_size": 2861.18,
                "percentile_90": 6058.96,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5949.32,
                "median_ticket_size": 5056.92,
                "percentile_90": 10708.78,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 67,
            "total_attempts": 3378,
            "successful_transactions": 3065,
            "failed_transactions": 313,
            "processing_time_avg": 40.3,
            "retry_success_rate": 36.7,
            "peak_hour_sr": 92.9,
            "off_peak_sr": 89.4
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 92.72,
                "total_attempts": 1216,
                "successful": 1127,
                "failed": 89,
                "avg_processing_time": 47.4,
                "peak_volume_hour": "19:00-18:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 93.44,
                "total_attempts": 1390,
                "successful": 1298,
                "failed": 92,
                "avg_processing_time": 55.1,
                "peak_volume_hour": "20:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 85.15,
                "total_attempts": 521,
                "successful": 443,
                "failed": 78,
                "avg_processing_time": 41.5,
                "peak_volume_hour": "15:00-20:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 78.36,
                "total_attempts": 208,
                "successful": 162,
                "failed": 46,
                "avg_processing_time": 53.2,
                "peak_volume_hour": "21:00-22:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 87.96,
                "total_attempts": 139,
                "successful": 122,
                "failed": 17,
                "avg_processing_time": 48.1,
                "peak_volume_hour": "20:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 93,
                    "percentage": 29.7
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 81,
                    "percentage": 25.9
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 64,
                    "percentage": 20.4
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 42,
                    "percentage": 13.4
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 33,
                    "percentage": 10.5
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 12.1,
                "06-12": 8.7,
                "12-18": 7.9,
                "18-24": 9.8
            },
            "retry_attempts": {
                "single_retry": 199,
                "multiple_retries": 114,
                "success_after_retry": 115
            }
        },
        "gmv_breakdown": {
            "total_gmv": 578923.7,
            "successful_gmv": 524834.9,
            "failed_gmv": 54088.8,
            "payment_method_gmv": {
                "UPI": 203734.85,
                "CREDIT_CARD": 228456.7,
                "DEBIT_CARD": 68923.4,
                "NET_BANKING": 33720.95
            },
            "average_transaction_value": 171.36,
            "high_value_transactions": 51,
            "micro_transactions": 1356
        },
        "device_analytics": {
            "mobile_sr": 91.7,
            "desktop_sr": 90.2,
            "tablet_sr": 88.8,
            "mobile_percentage": 78.1,
            "desktop_percentage": 19.4,
            "tablet_percentage": 2.5,
            "ios_sr": 92.8,
            "android_sr": 91.1,
            "windows_sr": 89.7
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 92.3,
                    "volume": 956
                },
                {
                    "city": "Delhi",
                    "success_rate": 90.1,
                    "volume": 812
                },
                {
                    "city": "Bangalore",
                    "success_rate": 93.1,
                    "volume": 667
                },
                {
                    "city": "Chennai",
                    "success_rate": 89.7,
                    "volume": 512
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 91.8,
                    "volume": 431
                }
            ],
            "state_performance": {
                "Maharashtra": 92.3,
                "Karnataka": 93.1,
                "Delhi": 90.1,
                "Tamil Nadu": 89.7,
                "Telangana": 91.8
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "15:00-17:00",
                    "19:00-21:00"
                ],
                "low_performance_hours": [
                    "02:00-06:00"
                ],
                "weekend_pattern": "reliable_performance"
            },
            "success_rate_by_time_of_day": {
                "morning": 91.1,
                "afternoon": 91.6,
                "evening": 91.3,
                "night": 89.2
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 91.0,
            "weekend_sr": 90.1,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "CREDIT_CARD",
                "count": 26,
                "percentage": 8.1,
                "avg_retry_attempts": 1.6,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "NET_BANKING",
                "count": 25,
                "percentage": 7.8,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "NET_BANKING",
                "count": 32,
                "percentage": 9.9,
                "avg_retry_attempts": 2.0,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 37,
                "percentage": 11.5,
                "avg_retry_attempts": 1.7,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "NET_BANKING",
                "count": 37,
                "percentage": 11.5,
                "avg_retry_attempts": 2.5,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 49,
                "percentage": 15.2,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 1127,
                "percentage": 35.8,
                "peak_hour_volume": 140,
                "avg_transaction_value": 4802.38,
                "repeat_customer_rate": 73.3
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1298,
                "percentage": 41.2,
                "peak_hour_volume": 90,
                "avg_transaction_value": 4595.56,
                "repeat_customer_rate": 60.4
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 443,
                "percentage": 14.1,
                "peak_hour_volume": 114,
                "avg_transaction_value": 4952.36,
                "repeat_customer_rate": 45.1
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 162,
                "percentage": 5.1,
                "peak_hour_volume": 125,
                "avg_transaction_value": 3898.16,
                "repeat_customer_rate": 72.7
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 122,
                "percentage": 3.9,
                "peak_hour_volume": 60,
                "avg_transaction_value": 5588.12,
                "repeat_customer_rate": 66.2
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5412282,
                "percentage": 35.8,
                "avg_order_value": 4802.38,
                "growth_rate": 7.3,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 5965036,
                "percentage": 41.2,
                "avg_order_value": 4595.56,
                "growth_rate": -3.6,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2193895,
                "percentage": 14.1,
                "avg_order_value": 4952.36,
                "growth_rate": 3.4,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 631501,
                "percentage": 5.1,
                "avg_order_value": 3898.16,
                "growth_rate": 16.4,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 681750,
                "percentage": 3.9,
                "avg_order_value": 5588.12,
                "growth_rate": 15.3,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 4802.38,
                "median_ticket_size": 4082.02,
                "percentile_90": 8644.28,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 4595.56,
                "median_ticket_size": 3906.23,
                "percentile_90": 8272.01,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 4952.36,
                "median_ticket_size": 4209.51,
                "percentile_90": 8914.25,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 3898.16,
                "median_ticket_size": 3313.44,
                "percentile_90": 7016.69,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 5588.12,
                "median_ticket_size": 4749.9,
                "percentile_90": 10058.62,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    },
    {
        "overall_success_rate_data": {
            "success_rate": 70.05,
            "total_attempts": 3623,
            "successful_transactions": 3412,
            "failed_transactions": 211,
            "processing_time_avg": 35.7,
            "retry_success_rate": 43.1,
            "peak_hour_sr": 96.2,
            "off_peak_sr": 93.1
        },
        "payment_method_success_rates": [
            {
                "payment_method_type": "UPI",
                "success_rate": 91.93,
                "total_attempts": 1018,
                "successful": 935,
                "failed": 83,
                "avg_processing_time": 67.5,
                "peak_volume_hour": "19:00-19:00",
                "failure_reasons": [
                    "TRANSACTION_TIMEOUT",
                    "INSUFFICIENT_FUNDS",
                    "BANK_ERROR"
                ]
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "success_rate": 90.62,
                "total_attempts": 1164,
                "successful": 1054,
                "failed": 110,
                "avg_processing_time": 30.7,
                "peak_volume_hour": "14:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "CARD_DECLINED",
                    "EXPIRED_CARD"
                ]
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "success_rate": 85.95,
                "total_attempts": 436,
                "successful": 374,
                "failed": 62,
                "avg_processing_time": 59.9,
                "peak_volume_hour": "17:00-15:00",
                "failure_reasons": [
                    "INSUFFICIENT_FUNDS",
                    "DAILY_LIMIT_EXCEEDED",
                    "PIN_INCORRECT"
                ]
            },
            {
                "payment_method_type": "NET_BANKING",
                "success_rate": 84.5,
                "total_attempts": 174,
                "successful": 147,
                "failed": 27,
                "avg_processing_time": 33.9,
                "peak_volume_hour": "22:00-23:00",
                "failure_reasons": [
                    "BANK_TECHNICAL_ISSUE",
                    "SESSION_TIMEOUT",
                    "INVALID_CREDENTIALS"
                ]
            },
            {
                "payment_method_type": "WALLET",
                "success_rate": 86.58,
                "total_attempts": 116,
                "successful": 100,
                "failed": 16,
                "avg_processing_time": 68.2,
                "peak_volume_hour": "20:00-17:00",
                "failure_reasons": [
                    "INSUFFICIENT_WALLET_BALANCE",
                    "WALLET_BLOCKED",
                    "OTP_FAILURE"
                ]
            }
        ],
        "failure_analysis": {
            "top_failure_reasons": [
                {
                    "reason": "INSUFFICIENT_FUNDS",
                    "count": 68,
                    "percentage": 32.2
                },
                {
                    "reason": "CARD_DECLINED",
                    "count": 52,
                    "percentage": 24.6
                },
                {
                    "reason": "TRANSACTION_TIMEOUT",
                    "count": 39,
                    "percentage": 18.5
                },
                {
                    "reason": "BANK_ERROR",
                    "count": 30,
                    "percentage": 14.2
                },
                {
                    "reason": "SESSION_TIMEOUT",
                    "count": 22,
                    "percentage": 10.4
                }
            ],
            "failure_rate_by_hour": {
                "00-06": 7.8,
                "06-12": 5.3,
                "12-18": 4.9,
                "18-24": 6.4
            },
            "retry_attempts": {
                "single_retry": 134,
                "multiple_retries": 77,
                "success_after_retry": 91
            }
        },
        "gmv_breakdown": {
            "total_gmv": 673925.45,
            "successful_gmv": 634856.7,
            "failed_gmv": 39068.75,
            "payment_method_gmv": {
                "UPI": 234567.8,
                "CREDIT_CARD": 268934.65,
                "DEBIT_CARD": 82456.3,
                "NET_BANKING": 48897.95
            },
            "average_transaction_value": 186.01,
            "high_value_transactions": 69,
            "micro_transactions": 1534
        },
        "device_analytics": {
            "mobile_sr": 95.1,
            "desktop_sr": 93.8,
            "tablet_sr": 92.4,
            "mobile_percentage": 75.3,
            "desktop_percentage": 22.1,
            "tablet_percentage": 2.6,
            "ios_sr": 96.3,
            "android_sr": 94.6,
            "windows_sr": 93.2
        },
        "geographical_data": {
            "top_cities": [
                {
                    "city": "Mumbai",
                    "success_rate": 95.6,
                    "volume": 1123
                },
                {
                    "city": "Delhi",
                    "success_rate": 93.9,
                    "volume": 934
                },
                {
                    "city": "Bangalore",
                    "success_rate": 96.2,
                    "volume": 789
                },
                {
                    "city": "Chennai",
                    "success_rate": 93.1,
                    "volume": 612
                },
                {
                    "city": "Hyderabad",
                    "success_rate": 95.1,
                    "volume": 165
                }
            ],
            "state_performance": {
                "Maharashtra": 95.6,
                "Karnataka": 96.2,
                "Delhi": 93.9,
                "Tamil Nadu": 93.1,
                "Telangana": 95.1
            }
        },
        "temporal_patterns": {
            "hourly_success_rates": {
                "peak_hours": [
                    "14:00-16:00",
                    "21:00-23:00"
                ],
                "low_performance_hours": [
                    "01:00-04:00"
                ],
                "weekend_pattern": "month_end_excellence"
            },
            "success_rate_by_time_of_day": {
                "morning": 94.6,
                "afternoon": 95.1,
                "evening": 94.8,
                "night": 92.9
            },
            "payment_method_preference_by_time": {
                "morning": "UPI",
                "afternoon": "CREDIT_CARD",
                "evening": "CREDIT_CARD",
                "night": "UPI"
            }
        },
        "weekly_patterns": {
            "weekday_sr": 94.5,
            "weekend_sr": 93.6,
            "monday_peak": False,
            "friday_peak": True
        },
        "errors": [],
        "failure_details": [
            {
                "error_message": "INSUFFICIENT_FUNDS",
                "payment_method_type": "WALLET",
                "count": 42,
                "percentage": 14.1,
                "avg_retry_attempts": 1.4,
                "resolution_suggestions": [
                    "Check account balance",
                    "Try different card"
                ]
            },
            {
                "error_message": "BANK_TECHNICAL_ISSUE",
                "payment_method_type": "WALLET",
                "count": 43,
                "percentage": 14.4,
                "avg_retry_attempts": 1.1,
                "resolution_suggestions": [
                    "Try again later",
                    "Contact bank"
                ]
            },
            {
                "error_message": "TRANSACTION_TIMEOUT",
                "payment_method_type": "DEBIT_CARD",
                "count": 34,
                "percentage": 11.4,
                "avg_retry_attempts": 1.8,
                "resolution_suggestions": [
                    "Check internet connection",
                    "Retry payment"
                ]
            },
            {
                "error_message": "CARD_DECLINED",
                "payment_method_type": "UPI",
                "count": 42,
                "percentage": 14.1,
                "avg_retry_attempts": 2.1,
                "resolution_suggestions": [
                    "Contact card issuer",
                    "Try different card"
                ]
            },
            {
                "error_message": "DAILY_LIMIT_EXCEEDED",
                "payment_method_type": "NET_BANKING",
                "count": 22,
                "percentage": 7.4,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Try tomorrow",
                    "Use different payment method"
                ]
            },
            {
                "error_message": "SESSION_EXPIRED",
                "payment_method_type": "DEBIT_CARD",
                "count": 15,
                "percentage": 5.0,
                "avg_retry_attempts": 2.2,
                "resolution_suggestions": [
                    "Login again",
                    "Refresh page"
                ]
            }
        ],
        "success_volume_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "transaction_count": 935,
                "percentage": 35.8,
                "peak_hour_volume": 149,
                "avg_transaction_value": 6172.76,
                "repeat_customer_rate": 64.1
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "transaction_count": 1054,
                "percentage": 40.4,
                "peak_hour_volume": 35,
                "avg_transaction_value": 5168.63,
                "repeat_customer_rate": 54.6
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "transaction_count": 374,
                "percentage": 14.3,
                "peak_hour_volume": 85,
                "avg_transaction_value": 5743.75,
                "repeat_customer_rate": 75.0
            },
            {
                "payment_method_type": "NET_BANKING",
                "transaction_count": 147,
                "percentage": 5.6,
                "peak_hour_volume": 128,
                "avg_transaction_value": 2577.2,
                "repeat_customer_rate": 61.6
            },
            {
                "payment_method_type": "WALLET",
                "transaction_count": 100,
                "percentage": 3.8,
                "peak_hour_volume": 144,
                "avg_transaction_value": 3571.03,
                "repeat_customer_rate": 67.6
            }
        ],
        "gmv_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "gmv": 5771530,
                "percentage": 35.8,
                "avg_order_value": 6172.76,
                "growth_rate": 16.4,
                "regional_preference": {
                    "North": 38.2,
                    "South": 42.1,
                    "West": 35.7,
                    "East": 28.9
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "gmv": 5447736,
                "percentage": 40.4,
                "avg_order_value": 5168.63,
                "growth_rate": 4.1,
                "regional_preference": {
                    "North": 52.3,
                    "South": 48.9,
                    "West": 54.1,
                    "East": 46.7
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "gmv": 2148162,
                "percentage": 14.3,
                "avg_order_value": 5743.75,
                "growth_rate": 6.9,
                "regional_preference": {
                    "North": 28.5,
                    "South": 31.2,
                    "West": 26.8,
                    "East": 33.4
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "gmv": 378848,
                "percentage": 5.6,
                "avg_order_value": 2577.2,
                "growth_rate": 8.2,
                "regional_preference": {
                    "North": 15.2,
                    "South": 12.8,
                    "West": 18.9,
                    "East": 21.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "gmv": 357103,
                "percentage": 3.8,
                "avg_order_value": 3571.03,
                "growth_rate": -2.9,
                "regional_preference": {
                    "North": 22.1,
                    "South": 25.8,
                    "West": 19.4,
                    "East": 16.2
                }
            }
        ],
        "average_ticket_size_by_payment_method": [
            {
                "payment_method_type": "UPI",
                "average_ticket_size": 6172.76,
                "median_ticket_size": 5246.85,
                "percentile_90": 11110.97,
                "category_preference": {
                    "Electronics": 45.2,
                    "Fashion": 32.1,
                    "Home": 22.7
                }
            },
            {
                "payment_method_type": "CREDIT_CARD",
                "average_ticket_size": 5168.63,
                "median_ticket_size": 4393.34,
                "percentile_90": 9303.53,
                "category_preference": {
                    "Electronics": 58.7,
                    "Fashion": 28.9,
                    "Home": 12.4
                }
            },
            {
                "payment_method_type": "DEBIT_CARD",
                "average_ticket_size": 5743.75,
                "median_ticket_size": 4882.19,
                "percentile_90": 10338.75,
                "category_preference": {
                    "Electronics": 31.5,
                    "Fashion": 42.3,
                    "Home": 26.2
                }
            },
            {
                "payment_method_type": "NET_BANKING",
                "average_ticket_size": 2577.2,
                "median_ticket_size": 2190.62,
                "percentile_90": 4638.96,
                "category_preference": {
                    "Electronics": 67.8,
                    "Fashion": 18.9,
                    "Home": 13.3
                }
            },
            {
                "payment_method_type": "WALLET",
                "average_ticket_size": 3571.03,
                "median_ticket_size": 3035.38,
                "percentile_90": 6427.85,
                "category_preference": {
                    "Electronics": 28.4,
                    "Fashion": 45.6,
                    "Home": 26.0
                }
            }
        ]
    }
]