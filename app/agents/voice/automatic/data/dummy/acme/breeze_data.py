"""
ACME Store Breeze Analytics Data - 31 Days
Complete time-based data for merchant_id="acme-store-demo"
Each entry represents one day (index 0-30) with comprehensive e-commerce metrics
ALL 31 DAYS EXPLICITLY DEFINED - SYNCHRONIZED WITH TYPESCRIPT SOURCE
"""

# 31 days of comprehensive Breeze analytics data (index 0-30)
ACME_BREEZE_DATA = [
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4785.41, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3920.04, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,820',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4907.95,
                        'Home & Kitchen': 4845.03,
                        'Fashion': 4820.29
                    }
                },
                'title': 'Average order value',
                'value': 4820.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.68%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.68
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 91, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 26, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 81.25, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 112',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 39,
                        'Fashion': 28
                    }
                },
                'title': 'Total orders',
                'value': 112
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 435472.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 101921.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 80.66, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹539,875',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 215950,
                        'facebook': 134968,
                        'direct': 107975,
                        'email': 53987,
                        'others': 26993
                    }
                },
                'title': 'Total sales',
                'value': 539875.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4758.88, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5556.89, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,895',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4895.38,
                        'Home & Kitchen': 4895.37,
                        'Fashion': 4895.37
                    }
                },
                'title': 'Average order value',
                'value': 4895.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.75%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.75
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 112, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 27, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 80.00, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 140',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 56,
                        'Home & Kitchen': 49,
                        'Fashion': 35
                    }
                },
                'title': 'Total orders',
                'value': 140
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 532995.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 150036.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 77.77, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹685,353',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 274141,
                        'facebook': 171338,
                        'direct': 137070,
                        'email': 68535,
                        'others': 34267
                    }
                },
                'title': 'Total sales',
                'value': 685353.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5153.70, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3604.27, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,970',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4970.77,
                        'Home & Kitchen': 4970.76,
                        'Fashion': 4970.77
                    }
                },
                'title': 'Average order value',
                'value': 4970.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.82%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.82
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 105, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 33, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 75.00, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 140',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 56,
                        'Home & Kitchen': 49,
                        'Fashion': 35
                    }
                },
                'title': 'Total orders',
                'value': 140
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 541138.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 118941.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 77.76, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹695,908',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 278363,
                        'facebook': 173977,
                        'direct': 139181,
                        'email': 69590,
                        'others': 34795
                    }
                },
                'title': 'Total sales',
                'value': 695908.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4434.13, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6405.48, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,721',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4739.25,
                        'Home & Kitchen': 4777.87,
                        'Fashion': 4757.18
                    }
                },
                'title': 'Average order value',
                'value': 4721.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.89%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.89
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 107, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 80.45, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 133',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 53,
                        'Home & Kitchen': 46,
                        'Fashion': 33
                    }
                },
                'title': 'Total orders',
                'value': 133
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 474452.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 134515.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 75.56, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹627,950',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 251180,
                        'facebook': 156987,
                        'direct': 125590,
                        'email': 62795,
                        'others': 31397
                    }
                },
                'title': 'Total sales',
                'value': 627950.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5240.96, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5551.33, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,150',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5197.68,
                        'Home & Kitchen': 5266.08,
                        'Fashion': 5293.93
                    }
                },
                'title': 'Average order value',
                'value': 5150.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.96%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.96
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 91, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 24, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 81.98, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 111',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 38,
                        'Fashion': 27
                    }
                },
                'title': 'Total orders',
                'value': 111
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 476927.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 133232.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.42, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹571,746',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 228698,
                        'facebook': 142936,
                        'direct': 114349,
                        'email': 57174,
                        'others': 28587
                    }
                },
                'title': 'Total sales',
                'value': 571746.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4402.39, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 4500.54, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,258',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4273.53,
                        'Home & Kitchen': 4284.65,
                        'Fashion': 4320.65
                    }
                },
                'title': 'Average order value',
                'value': 4258.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.03%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.03
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 112, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 24, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 81.16, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 138',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 55,
                        'Home & Kitchen': 48,
                        'Fashion': 34
                    }
                },
                'title': 'Total orders',
                'value': 138
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 493068.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 108013.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.91, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹587,611',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 235044,
                        'facebook': 146902,
                        'direct': 117522,
                        'email': 58761,
                        'others': 29380
                    }
                },
                'title': 'Total sales',
                'value': 587611.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5192.50, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5208.41, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,928',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4962.52,
                        'Home & Kitchen': 4938.20,
                        'Fashion': 4996.97
                    }
                },
                'title': 'Average order value',
                'value': 4928.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.10%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.1
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 111, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 27, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 76.03, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 146',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 58,
                        'Home & Kitchen': 51,
                        'Fashion': 36
                    }
                },
                'title': 'Total orders',
                'value': 146
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 576367.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 140627.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 80.10, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹719,566',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 287826,
                        'facebook': 179891,
                        'direct': 143913,
                        'email': 71956,
                        'others': 35978
                    }
                },
                'title': 'Total sales',
                'value': 719566.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6541.72, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 7420.38, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹6,357',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 6442.62,
                        'Home & Kitchen': 6504.56,
                        'Fashion': 6471.36
                    }
                },
                'title': 'Average order value',
                'value': 6357.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.68%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.68
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 87, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 76.32, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 114',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 45,
                        'Home & Kitchen': 39,
                        'Fashion': 28
                    }
                },
                'title': 'Total orders',
                'value': 114
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 569130.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 155828.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 78.52, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹724,795',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 289918,
                        'facebook': 181198,
                        'direct': 144959,
                        'email': 72479,
                        'others': 36239
                    }
                },
                'title': 'Total sales',
                'value': 724795.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6064.45, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 4118.04, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹6,076',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 6076.14,
                        'Home & Kitchen': 6156.08,
                        'Fashion': 6188.63
                    }
                },
                'title': 'Average order value',
                'value': 6076.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.75%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.75
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 85, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 26, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 77.27, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 110',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 38,
                        'Fashion': 27
                    }
                },
                'title': 'Total orders',
                'value': 110
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 515478.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 107069.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 77.12, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹668,375',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 267350,
                        'facebook': 167093,
                        'direct': 133675,
                        'email': 66837,
                        'others': 33418
                    }
                },
                'title': 'Total sales',
                'value': 668375.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5193.94, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3645.76, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,966',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5053.22,
                        'Home & Kitchen': 5084.80,
                        'Fashion': 5009.66
                    }
                },
                'title': 'Average order value',
                'value': 4966.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.82%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.82
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 93, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 25, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 79.49, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 117',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 46,
                        'Home & Kitchen': 40,
                        'Fashion': 29
                    }
                },
                'title': 'Total orders',
                'value': 117
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 483036.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 91144.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.12, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹581,121',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 232448,
                        'facebook': 145280,
                        'direct': 116224,
                        'email': 58112,
                        'others': 29056
                    }
                },
                'title': 'Total sales',
                'value': 581121.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5105.84, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5490.59, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,714',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4714.76,
                        'Home & Kitchen': 4785.48,
                        'Fashion': 4747.50
                    }
                },
                'title': 'Average order value',
                'value': 4714.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.89%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.89
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 112, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 27, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 77.24, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 145',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 58,
                        'Home & Kitchen': 50,
                        'Fashion': 36
                    }
                },
                'title': 'Total orders',
                'value': 145
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 571854.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 148246.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.65, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹683,641',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 273456,
                        'facebook': 170910,
                        'direct': 136728,
                        'email': 68364,
                        'others': 34182
                    }
                },
                'title': 'Total sales',
                'value': 683641.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5617.63, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 7404.82, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,969',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5021.72,
                        'Home & Kitchen': 5009.16,
                        'Fashion': 4969.42
                    }
                },
                'title': 'Average order value',
                'value': 4969.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.96%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.96
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 108, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 22, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 75.00, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 144',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 57,
                        'Home & Kitchen': 50,
                        'Fashion': 36
                    }
                },
                'title': 'Total orders',
                'value': 144
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 606704.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 162906.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 84.78, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹715,596',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 286238,
                        'facebook': 178899,
                        'direct': 143119,
                        'email': 71559,
                        'others': 35779
                    }
                },
                'title': 'Total sales',
                'value': 715596.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4425.47, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6325.10, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,600',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4619.67,
                        'Home & Kitchen': 4606.23,
                        'Fashion': 4715.90
                    }
                },
                'title': 'Average order value',
                'value': 4600.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.03%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.03
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 102, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 20, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 82.93, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 123',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 49,
                        'Home & Kitchen': 43,
                        'Fashion': 30
                    }
                },
                'title': 'Total orders',
                'value': 123
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 451398.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 126502.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 79.76, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹565,911',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 226364,
                        'facebook': 141477,
                        'direct': 113182,
                        'email': 56591,
                        'others': 28295
                    }
                },
                'title': 'Total sales',
                'value': 565911.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5229.18, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3932.57, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,478',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5499.61,
                        'Home & Kitchen': 5577.73,
                        'Fashion': 5478.12
                    }
                },
                'title': 'Average order value',
                'value': 5478.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.10%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.1
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 107, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 30, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 83.59, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 128',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 51,
                        'Home & Kitchen': 44,
                        'Fashion': 32
                    }
                },
                'title': 'Total orders',
                'value': 128
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 559522.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 117977.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 79.79, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹701,202',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 280480,
                        'facebook': 175300,
                        'direct': 140240,
                        'email': 70120,
                        'others': 35060
                    }
                },
                'title': 'Total sales',
                'value': 701202.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5380.47, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6996.05, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,522',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5628.02,
                        'Home & Kitchen': 5590.00,
                        'Fashion': 5682.15
                    }
                },
                'title': 'Average order value',
                'value': 5522.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.68%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.68
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 85, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 79.44, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 107',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 42,
                        'Home & Kitchen': 37,
                        'Fashion': 26
                    }
                },
                'title': 'Total orders',
                'value': 107
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 457340.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 146917.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 77.39, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹590,944',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 236377,
                        'facebook': 147736,
                        'direct': 118188,
                        'email': 59094,
                        'others': 29547
                    }
                },
                'title': 'Total sales',
                'value': 590944.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4568.58, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6494.72, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,058',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5118.39,
                        'Home & Kitchen': 5075.73,
                        'Fashion': 5098.38
                    }
                },
                'title': 'Average order value',
                'value': 5058.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.75%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.75
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 109, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 25, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 84.50, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 129',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 51,
                        'Home & Kitchen': 45,
                        'Fashion': 32
                    }
                },
                'title': 'Total orders',
                'value': 129
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 497975.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 162368.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 76.31, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹652,595',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 261038,
                        'facebook': 163148,
                        'direct': 130519,
                        'email': 65259,
                        'others': 32629
                    }
                },
                'title': 'Total sales',
                'value': 652595.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5271.51, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6101.29, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,705',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4705.89,
                        'Home & Kitchen': 4705.90,
                        'Fashion': 4705.89
                    }
                },
                'title': 'Average order value',
                'value': 4705.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.82%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.82
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 106, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 24, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 75.71, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 140',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 56,
                        'Home & Kitchen': 49,
                        'Fashion': 35
                    }
                },
                'title': 'Total orders',
                'value': 140
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 558780.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 146431.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 84.81, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹658,826',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 263530,
                        'facebook': 164706,
                        'direct': 131765,
                        'email': 65882,
                        'others': 32941
                    }
                },
                'title': 'Total sales',
                'value': 658826.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5017.58, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3903.74, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,965',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5051.48,
                        'Home & Kitchen': 5083.05,
                        'Fashion': 5007.93
                    }
                },
                'title': 'Average order value',
                'value': 4965.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.89%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.89
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 97, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 23, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 82.91, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 117',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 46,
                        'Home & Kitchen': 40,
                        'Fashion': 29
                    }
                },
                'title': 'Total orders',
                'value': 117
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 486705.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 89786.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.78, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹580,922',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 232368,
                        'facebook': 145230,
                        'direct': 116184,
                        'email': 58092,
                        'others': 29046
                    }
                },
                'title': 'Total sales',
                'value': 580922.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5060.10, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5289.79, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,535',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5640.95,
                        'Home & Kitchen': 5602.84,
                        'Fashion': 5695.19
                    }
                },
                'title': 'Average order value',
                'value': 5535.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.96%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.96
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 88, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 19, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 82.24, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 107',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 42,
                        'Home & Kitchen': 37,
                        'Fashion': 26
                    }
                },
                'title': 'Total orders',
                'value': 107
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 445289.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 100506.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 75.18, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹592,302',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 236920,
                        'facebook': 148075,
                        'direct': 118460,
                        'email': 59230,
                        'others': 29615
                    }
                },
                'title': 'Total sales',
                'value': 592302.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5300.21, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6606.14, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,053',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5090.54,
                        'Home & Kitchen': 5117.62,
                        'Fashion': 5053.12
                    }
                },
                'title': 'Average order value',
                'value': 5053.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.03%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.03
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 109, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 80.15, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 136',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 54,
                        'Home & Kitchen': 47,
                        'Fashion': 34
                    }
                },
                'title': 'Total orders',
                'value': 136
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 577723.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 138729.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 84.07, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹687,224',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 274889,
                        'facebook': 171806,
                        'direct': 137444,
                        'email': 68722,
                        'others': 34361
                    }
                },
                'title': 'Total sales',
                'value': 687224.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6116.11, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 9063.78, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,840',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5891.24,
                        'Home & Kitchen': 5928.05,
                        'Fashion': 5840.45
                    }
                },
                'title': 'Average order value',
                'value': 5840.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.10%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.1
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 87, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 18, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 75.00, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 116',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 46,
                        'Home & Kitchen': 40,
                        'Fashion': 29
                    }
                },
                'title': 'Total orders',
                'value': 116
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 532102.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 163148.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 78.54, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹677,494',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 270997,
                        'facebook': 169373,
                        'direct': 135498,
                        'email': 67749,
                        'others': 33874
                    }
                },
                'title': 'Total sales',
                'value': 677494.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5070.32, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 4166.85, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,000',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5044.11,
                        'Home & Kitchen': 5075.62,
                        'Fashion': 5000.62
                    }
                },
                'title': 'Average order value',
                'value': 5000.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.68%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.68
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 88, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 27, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 75.86, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 116',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 46,
                        'Home & Kitchen': 40,
                        'Fashion': 29
                    }
                },
                'title': 'Total orders',
                'value': 116
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 446188.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 112505.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 76.92, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹580,074',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 232029,
                        'facebook': 145018,
                        'direct': 116014,
                        'email': 58007,
                        'others': 29003
                    }
                },
                'title': 'Total sales',
                'value': 580074.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5155.36, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5605.67, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,029',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5106.96,
                        'Home & Kitchen': 5051.43,
                        'Fashion': 5029.58
                    }
                },
                'title': 'Average order value',
                'value': 5029.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.75%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.75
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 105, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 79.55, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 132',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 52,
                        'Home & Kitchen': 46,
                        'Fashion': 33
                    }
                },
                'title': 'Total orders',
                'value': 132
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 541313.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 117719.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 81.53, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹663,905',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 265562,
                        'facebook': 165976,
                        'direct': 132781,
                        'email': 66390,
                        'others': 33195
                    }
                },
                'title': 'Total sales',
                'value': 663905.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6077.03, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 4402.32, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹6,082',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 6140.29,
                        'Home & Kitchen': 6098.81,
                        'Fashion': 6199.35
                    }
                },
                'title': 'Average order value',
                'value': 6082.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.82%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.82
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 90, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 22, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 84.91, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 106',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 42,
                        'Home & Kitchen': 37,
                        'Fashion': 26
                    }
                },
                'title': 'Total orders',
                'value': 106
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 546933.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 96851.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 84.83, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹644,732',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 257892,
                        'facebook': 161183,
                        'direct': 128946,
                        'email': 64473,
                        'others': 32236
                    }
                },
                'title': 'Total sales',
                'value': 644732.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6733.73, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 9777.53, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹6,430',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 6488.68,
                        'Home & Kitchen': 6574.05,
                        'Fashion': 6608.85
                    }
                },
                'title': 'Average order value',
                'value': 6430.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.89%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.89
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 85, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 17, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 76.58, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 111',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 38,
                        'Fashion': 27
                    }
                },
                'title': 'Total orders',
                'value': 111
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 572367.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 166218.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 80.19, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹713,757',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 285502,
                        'facebook': 178439,
                        'direct': 142751,
                        'email': 71375,
                        'others': 35687
                    }
                },
                'title': 'Total sales',
                'value': 713757.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 5275.64, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6974.29, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,285',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5381.55,
                        'Home & Kitchen': 5312.56,
                        'Fashion': 5285.46
                    }
                },
                'title': 'Average order value',
                'value': 5285.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.96%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.96
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 94, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 83.93, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 112',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 39,
                        'Fashion': 28
                    }
                },
                'title': 'Total orders',
                'value': 112
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 495910.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 146460.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.77, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹591,972',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 236788,
                        'facebook': 147993,
                        'direct': 118394,
                        'email': 59197,
                        'others': 29598
                    }
                },
                'title': 'Total sales',
                'value': 591972.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4429.32, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 3660.36, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,200',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4242.93,
                        'Home & Kitchen': 4212.33,
                        'Fashion': 4228.59
                    }
                },
                'title': 'Average order value',
                'value': 4200.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.03%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.03
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 115, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 28, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 77.18, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 149',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 59,
                        'Home & Kitchen': 52,
                        'Fashion': 37
                    }
                },
                'title': 'Total orders',
                'value': 149
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 509372.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 102490.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 81.39, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹625,834',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 250333,
                        'facebook': 156458,
                        'direct': 125166,
                        'email': 62583,
                        'others': 31291
                    }
                },
                'title': 'Total sales',
                'value': 625834.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.5, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.55%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.72,
                        'Credit Card': 91.75,
                        'Debit Card': 87.63,
                        'Net Banking': 80.07,
                        'Wallet': 86.21
                    }
                },
                'title': 'Payment success rate',
                'value': 89.55
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 6417.76, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6567.62, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹5,822',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 5875.45,
                        'Home & Kitchen': 5952.76,
                        'Fashion': 5984.26
                    }
                },
                'title': 'Average order value',
                'value': 5822.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 4.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 5.10%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 5.1
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 83, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 21, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 74.77, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 111',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 44,
                        'Home & Kitchen': 38,
                        'Fashion': 27
                    }
                },
                'title': 'Total orders',
                'value': 111
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 532674.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 137920.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 82.42, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹646,301',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 258520,
                        'facebook': 161575,
                        'direct': 129260,
                        'email': 64630,
                        'others': 32315
                    }
                },
                'title': 'Total sales',
                'value': 646301.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.4, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.85%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.02,
                        'Credit Card': 92.05,
                        'Debit Card': 87.93,
                        'Net Banking': 80.37,
                        'Wallet': 86.51
                    }
                },
                'title': 'Payment success rate',
                'value': 89.85
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4809.57, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 5013.60, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,994',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4994.08,
                        'Home & Kitchen': 5049.56,
                        'Fashion': 5072.09
                    }
                },
                'title': 'Average order value',
                'value': 4994.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.8, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.1, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.68%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.68
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 109, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 20, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 83.85, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 130',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 52,
                        'Home & Kitchen': 45,
                        'Fashion': 32
                    }
                },
                'title': 'Total orders',
                'value': 130
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 524243.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 100272.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 80.75, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹649,231',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 259692,
                        'facebook': 162307,
                        'direct': 129846,
                        'email': 64923,
                        'others': 32461
                    }
                },
                'title': 'Total sales',
                'value': 649231.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.0, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.6, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 92.7, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.15%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.32,
                        'Credit Card': 92.35,
                        'Debit Card': 88.23,
                        'Net Banking': 80.67,
                        'Wallet': 86.81
                    }
                },
                'title': 'Payment success rate',
                'value': 90.15
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4934.30, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 4937.50, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,776',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4828.36,
                        'Home & Kitchen': 4840.94,
                        'Fashion': 4881.62
                    }
                },
                'title': 'Average order value',
                'value': 4776.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.75%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.75
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 106, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 30, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 76.26, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 139',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 55,
                        'Home & Kitchen': 48,
                        'Fashion': 34
                    }
                },
                'title': 'Total orders',
                'value': 139
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 523036.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 148125.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 78.78, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': -2.3, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹663,902',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 265560,
                        'facebook': 165975,
                        'direct': 132780,
                        'email': 66390,
                        'others': 33195
                    }
                },
                'title': 'Total sales',
                'value': 663902.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 93.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 89.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 86.4, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 93.0, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 90.45%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 91.62,
                        'Credit Card': 92.65,
                        'Debit Card': 88.53,
                        'Net Banking': 80.97,
                        'Wallet': 87.11
                    }
                },
                'title': 'Payment success rate',
                'value': 90.45
            }
        }
    },
    {
        'averageOrderValue': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID_AOV', 'rate': 4696.69, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD_AOV', 'rate': 6298.65, 'subUnit': 'AMOUNT'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Overall AOV: ₹4,761',
                    'title': 'Category AOV',
                    'toolTipText': 'AOV by product category',
                    'unit': 'AMOUNT',
                    'value': {
                        'Electronics': 4794.19,
                        'Home & Kitchen': 4770.69,
                        'Fashion': 4827.47
                    }
                },
                'title': 'Average order value',
                'value': 4761.0
            }
        },
        'businessConversionBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'DESKTOP', 'rate': 6.3, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'MOBILE', 'rate': 3.9, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'TABLET', 'rate': 4.2, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'LINE_CHART',
                    'subTitle': 'Overall CR: 4.82%',
                    'title': 'Checkout Funnel',
                    'toolTipText': 'Conversion funnel rates',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'Cart View': 76.06,
                        'Checkout Init': 79.29,
                        'Address Entry': 85.21,
                        'Payment': 85.73,
                        'Confirmation': 100.0
                    }
                },
                'title': 'Conversion rate',
                'value': 4.82
            }
        },
        'businessTotalOrdersBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'NUMBER',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 123, 'subUnit': 'NUMBER'},
                    {'metric': 'COD', 'rate': 23, 'subUnit': 'NUMBER'},
                    {'metric': 'PREPAID(%)', 'rate': 84.25, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'BAR_CHART',
                    'subTitle': 'Total Orders: 146',
                    'title': 'Category Performance',
                    'toolTipText': 'Orders by product category',
                    'unit': 'NUMBER',
                    'value': {
                        'Electronics': 58,
                        'Home & Kitchen': 51,
                        'Fashion': 36
                    }
                },
                'title': 'Total orders',
                'value': 146
            }
        },
        'businessTotalSalesBreakdown': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'AMOUNT',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'PREPAID', 'rate': 577693.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'COD', 'rate': 144869.0, 'subUnit': 'AMOUNT'},
                    {'metric': 'PREPAID(%)', 'rate': 83.10, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'Growth', 'rate': 12.5, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'DONUT_CHART',
                    'subTitle': 'Total Sales: ₹695,159',
                    'title': 'Channel Performance',
                    'toolTipText': 'Sales by traffic source',
                    'unit': 'AMOUNT',
                    'value': {
                        'google': 278063,
                        'facebook': 173789,
                        'direct': 139031,
                        'email': 69515,
                        'others': 34757
                    }
                },
                'title': 'Total sales',
                'value': 695159.0
            }
        },
        'paymentSuccessRate': {
            'componentType': 'STATISTICS_CARD_WITH_SLOT',
            'unit': 'PERCENTAGE',
            'value': {
                'bottomContainerItems': [
                    {'metric': 'UPI', 'rate': 92.1, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'CARDS', 'rate': 88.7, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'NET_BANKING', 'rate': 85.2, 'subUnit': 'PERCENTAGE'},
                    {'metric': 'WALLETS', 'rate': 91.8, 'subUnit': 'PERCENTAGE'}
                ],
                'slotProperties': {
                    'componentType': 'PROGRESS_CHART',
                    'subTitle': 'Overall PSR: 89.25%',
                    'title': 'Payment Method Performance',
                    'toolTipText': 'Success rate by payment method',
                    'unit': 'PERCENTAGE',
                    'value': {
                        'UPI': 90.42,
                        'Credit Card': 91.45,
                        'Debit Card': 87.33,
                        'Net Banking': 79.77,
                        'Wallet': 85.91
                    }
                },
                'title': 'Payment success rate',
                'value': 89.25
            }
        }
    }
]
