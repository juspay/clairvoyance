# Euler Offer API Schemas - Complete Integration Guide

## Overview

This document provides the exact API schemas for creating and deleting Euler offers, based on the implementation in the Clairvoyance project and the detailed delete functionality documentation provided.

## Environment Configuration

### API Endpoints
```javascript
const EULER_ENDPOINTS = {
  production: "https://portal.juspay.in",
  sandbox: "https://sandbox-portal.juspay.in" // If available
};
```

### Required Headers
```json
{
  "Content-Type": "application/json",
  "x-web-logintoken": "{eulerToken}",
  "x-tenant-id": "jt_29bd8266cbdc4e76938cfaa2d80db4d6"
}
```

## 1. CREATE EULER OFFER

### Endpoint
```
POST {EULER_DASHBOARD_ENDPOINT}/api/offers/dashboard/create?merchant_id={merchantId}
```

### Request Schema
```json
{
  "application_mode": "ORDER",
  "merchant_id": "your_merchant_id",
  "offer_code": "OFFER_CODE_UNIQUE",
  "batch_id": "",
  "offer_description": {
    "title": "Customer-facing offer title",
    "description": "Detailed offer description",
    "tnc": "",
    "sponsored_by": "BREEZE",
    "display_title": "Display title for UI"
  },
  "ui_configs": {
    "is_hidden": "false",
    "should_validate": "true",
    "auto_apply": "false",
    "offer_display_priority": 0,
    "payment_method_label": ""
  },
  "rule_dsl": {
    "order": {
      "max_quantity": null,
      "min_quantity": null,
      "max_order_amount": null,
      "min_order_amount": "100",
      "currency": "INR",
      "amount_info": []
    },
    "additional_payment_filters": null,
    "payment_instrument": [
      {
        "payment_method_type": "CARD",
        "payment_method": [],
        "app": [],
        "type": [],
        "issuer": [],
        "variant": []
      }
    ],
    "counters": [],
    "payment_channel": [],
    "benefits": [
      {
        "type": "DISCOUNT",
        "calculation_rule": "PERCENTAGE",
        "value": 10,
        "amount_info": [],
        "max_amount": 500,
        "global_max_amount": null
      }
    ],
    "filters": {
      "blacklist": [],
      "whitelist": []
    }
  },
  "status": "ACTIVE",
  "start_time": "2024-01-01T00:00:00+05:30",
  "end_time": "2024-12-31T23:59:59+05:30",
  "metadata": {
    "analytics_offer_code": "OFFER_CODE_UNIQUE",
    "customerResetPeriodType": "offerPeriod",
    "cardResetPeriodType": "offerPeriod",
    "productCustomerResetPeriodType": "offerPeriod",
    "productCardResetPeriodType": "offerPeriod",
    "upiResetPeriodType": "offerPeriod",
    "productUpiResetPeriodType": "offerPeriod",
    "start_date": "2024-01-01T00:00:00+05:30",
    "end_date": "2024-12-31T23:59:59+05:30"
  },
  "udf1": null,
  "udf2": null,
  "udf3": null,
  "udf4": null,
  "udf5": null,
  "udf6": null,
  "udf7": null,
  "udf8": null,
  "udf9": null,
  "udf10": null,
  "minOfferBreakupCheckbox": false,
  "offerBreakupBool": false,
  "benefitsAmountInfo": [],
  "has_multi_codes": false
}
```

### Success Response
```json
{
  "offer_id": "offer_123456789",
  "status": "success",
  "message": "Offer created successfully"
}
```

### Error Response
```json
{
  "error_message": "Error description",
  "status": "error"
}
```

## 2. DELETE EULER OFFER

The delete process requires two API calls:

### Step 1: Find Offer by Code

#### Endpoint
```
POST {EULER_DASHBOARD_ENDPOINT}/api/offers/dashboard/dashboard-list?merchant_id={merchantId}
```

#### Request Schema
```json
{
  "merchant_id": "your_merchant_id",
  "offer_code": ["OFFER_CODE_TO_DELETE"],
  "limit": 1,
  "start_time": "2020-01-01T00:00:00Z",
  "end_time": "2050-01-01T00:00:00Z",
  "created_at": {
    "gte": "2020-01-01T00:00:00Z",
    "lte": "2050-01-01T00:00:00Z"
  },
  "sort_offers": {
    "order": "DESCENDING",
    "field": "CREATED_AT"
  }
}
```

#### Success Response
```json
{
  "summary": {
    "total_count": 1,
    "count": 1
  },
  "list": [
    {
      "offer_id": "offer_123456789",
      "offer_code": "OFFER_CODE_TO_DELETE",
      "status": "ACTIVE",
      "offer_description": {
        "display_title": "Offer Title",
        "sponsored_by": "BREEZE",
        "title": "Offer Title",
        "description": "Offer Description"
      },
      "start_time": "2024-01-01T00:00:00Z",
      "end_time": "2024-12-31T23:59:59Z"
    }
  ]
}
```

### Step 2: Delete the Offer

#### Endpoint
```
POST {EULER_DASHBOARD_ENDPOINT}/api/offers/dashboard/{offer_id}/delete
```

#### Request Schema
```json
{
  "merchant_id": "your_merchant_id",
  "offer_id": "offer_123456789"
}
```

#### Success Response
```json
{
  "status": "success",
  "message": "Offer deleted successfully"
}
```

#### Error Response
```json
{
  "error": "Error message",
  "details": "Additional error details"
}
```

## 3. PAUSE EULER OFFER (Alternative to Delete)

### Endpoint
```
POST {EULER_DASHBOARD_ENDPOINT}/api/offers/dashboard/{offer_id}/pause
```

### Request Schema
Same as delete:
```json
{
  "merchant_id": "your_merchant_id",
  "offer_id": "offer_123456789"
}
```

## 4. PAYMENT INSTRUMENT MAPPING

When creating offers, use these payment instrument configurations:

```json
{
  "CARD": {
    "payment_method_type": "CARD",
    "payment_method": [],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "NB": {
    "payment_method_type": "NB",
    "payment_method": [],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "WALLET": {
    "payment_method_type": "WALLET",
    "payment_method": [],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "CONSUMER_FINANCE": {
    "payment_method_type": "CONSUMER_FINANCE",
    "payment_method": [],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "REWARD": {
    "payment_method_type": "REWARD",
    "payment_method": [],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "CASH": {
    "payment_method_type": "CASH",
    "payment_method": ["CASH"],
    "app": [],
    "type": [],
    "issuer": [],
    "variant": []
  },
  "UPI": {
    "payment_method_type": "UPI",
    "payment_method": [],
    "app": [],
    "type": ["UPI_COLLECT", "UPI_PAY", "UPI_QR", "UPI_INAPP"],
    "issuer": [],
    "variant": []
  }
}
```

## 5. OFFER TYPES AND CALCULATION RULES

### Supported Offer Types
- `DISCOUNT`: Reduces order amount
- `CASHBACK`: Gives money back to customer

### Calculation Rules
- `PERCENTAGE`: Percentage-based calculation
- `ABSOLUTE`: Fixed amount calculation

## 6. COMPLETE INTEGRATION EXAMPLE

```javascript
class EulerOfferAPI {
  constructor(baseUrl, eulerToken, merchantId) {
    this.baseUrl = baseUrl;
    this.eulerToken = eulerToken;
    this.merchantId = merchantId;
    this.headers = {
      'Content-Type': 'application/json',
      'x-web-logintoken': eulerToken,
      'x-tenant-id': 'jt_29bd8266cbdc4e76938cfaa2d80db4d6'
    };
  }

  async createOffer(offerData) {
    const endpoint = `${this.baseUrl}/api/offers/dashboard/create?merchant_id=${this.merchantId}`;
    
    const payload = {
      application_mode: "ORDER",
      merchant_id: this.merchantId,
      offer_code: offerData.offerCode,
      batch_id: "",
      offer_description: {
        title: offerData.title,
        description: offerData.description,
        tnc: "",
        sponsored_by: "BREEZE",
        display_title: offerData.title
      },
      // ... rest of the payload structure
    };

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify(payload)
    });

    return await response.json();
  }

  async findOfferByCode(offerCode) {
    const endpoint = `${this.baseUrl}/api/offers/dashboard/dashboard-list?merchant_id=${this.merchantId}`;
    
    const payload = {
      merchant_id: this.merchantId,
      offer_code: [offerCode],
      limit: 1,
      start_time: "2020-01-01T00:00:00Z",
      end_time: "2050-01-01T00:00:00Z",
      created_at: {
        gte: "2020-01-01T00:00:00Z",
        lte: "2050-01-01T00:00:00Z"
      },
      sort_offers: {
        order: "DESCENDING",
        field: "CREATED_AT"
      }
    };

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify(payload)
    });

    const result = await response.json();
    return result.list && result.list.length > 0 ? result.list[0].offer_id : null;
  }

  async deleteOffer(offerCode) {
    // Step 1: Find offer ID
    const offerId = await this.findOfferByCode(offerCode);
    if (!offerId) {
      throw new Error(`Offer with code "${offerCode}" not found`);
    }

    // Step 2: Delete the offer
    const endpoint = `${this.baseUrl}/api/offers/dashboard/${offerId}/delete`;
    const payload = {
      merchant_id: this.merchantId,
      offer_id: offerId
    };

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify(payload)
    });

    return await response.json();
  }

  async pauseOffer(offerCode) {
    // Step 1: Find offer ID
    const offerId = await this.findOfferByCode(offerCode);
    if (!offerId) {
      throw new Error(`Offer with code "${offerCode}" not found`);
    }

    // Step 2: Pause the offer
    const endpoint = `${this.baseUrl}/api/offers/dashboard/${offerId}/pause`;
    const payload = {
      merchant_id: this.merchantId,
      offer_id: offerId
    };

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: this.headers,
      body: JSON.stringify(payload)
    });

    return await response.json();
  }
}

// Usage Example
const eulerAPI = new EulerOfferAPI(
  'https://portal.juspay.in',
  'your_euler_token',
  'your_merchant_id'
);

// Create offer
const newOffer = await eulerAPI.createOffer({
  offerCode: 'SUMMER2024',
  title: 'Summer Sale 20% Off',
  description: 'Get 20% discount on all items'
});

// Delete offer
const deleteResult = await eulerAPI.deleteOffer('SUMMER2024');
```

## 7. REQUIRED PARAMETERS SUMMARY

### For Creating Offers
- `merchantId`: Your merchant identifier
- `eulerToken`: Authentication token
- `offerCode`: Unique offer code
- `offerType`: "DISCOUNT" or "CASHBACK"
- `offerTitle`: Customer-facing title
- `discountValue`: Discount amount or percentage
- `startDate`: Start date in IST format
- `endDate`: End date in IST format
- `offerDescription`: Detailed description

### For Deleting Offers
- `merchantId`: Your merchant identifier
- `eulerToken`: Authentication token
- `offerCode`: Code of the offer to delete

This schema provides everything needed to implement Euler offer creation and deletion in any repository.
