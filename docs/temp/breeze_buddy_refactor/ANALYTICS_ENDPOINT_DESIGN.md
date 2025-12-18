# 📊 Analytics Endpoint Design - Single Flexible POST Endpoint

**Date**: 2025-12-17
**Design**: Single POST endpoint for all analytics needs

---

## 🎯 Core Design Principle

**ONE endpoint to rule them all** - `POST /agent/voice/breeze-buddy/analytics`

All analytics requests go through a single endpoint with a flexible payload structure.

---

## 📐 Endpoint Structure

### Single Endpoint
```
POST /agent/voice/breeze-buddy/analytics
```

### Request Payload
```typescript
{
  "type": string,           // Type of analytics to return
  "filters": object,        // All filters (conjunctive/AND logic)
  "options": object         // Pagination, grouping, sorting
}
```

---

## 🎨 Request Payload Schema

### Full Structure
```typescript
{
  // REQUIRED: Type of analytics to return
  "type": "summary" | "call-details" | "trends" | "conversion" | "performance",

  // OPTIONAL: Filters (all applied with AND logic)
  "filters": {
    "template": string,              // Filter by template name (e.g., "order-confirmation", "appointment-reminder")
    "shop_identifier": string,       // Single shop filter
    "shop_identifiers": string[],    // Multiple shops filter
    "status": string,                // Call status (completed, failed, etc.)
    "date_from": string,             // ISO date: "2025-01-01"
    "date_to": string,               // ISO date: "2025-12-31"
    "call_duration_min": number,     // Minimum duration in seconds
    "call_duration_max": number,     // Maximum duration in seconds
    "customer_sentiment": string     // Sentiment (positive, neutral, negative)
    // Add more filters as needed - no code changes required!
  },

  // OPTIONAL: Options for formatting results
  "options": {
    "page": number,                  // Page number (1-indexed)
    "limit": number,                 // Items per page
    "group_by": string,              // Group results by field (template, shop_identifier, date, week, month)
    "time_granularity": "day" | "week" | "month",  // Time aggregation granularity (for trends)
    "sort_by": string,               // Field to sort by
    "sort_order": "asc" | "desc"     // Sort direction
  }
}
```

---

## 📊 Analytics Types

| Type | Returns | Use Case |
|------|---------|----------|
| `summary` | Aggregate statistics | Dashboard overview, KPIs |
| `call-details` | Individual call records (paginated) | Call logs, detailed history |
| `trends` | Time-series data | Charts, graphs, trend analysis |
| `conversion` | Conversion/funnel metrics | Conversion rates, funnel drop-off |
| `performance` | Performance metrics | Success rates, agent performance |

---

## 💡 Example Use Cases

### 1. Dashboard Overview (All Templates)
```bash
POST /agent/voice/breeze-buddy/analytics
Content-Type: application/json
Authorization: Bearer <jwt-token>

{
  "type": "summary",
  "filters": {},
  "options": {}
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "summary",
    "filters_applied": {
      "shop_identifiers": ["shop_123"]  // Auto-applied from JWT
    },
    "results": {
      "total_calls": 5234,
      "completed_calls": 4180,
      "failed_calls": 1054,
      "success_rate": 79.9,
      "average_duration": 142.5,
      "total_templates": 3
    }
  }
}
```

### 2. Order Confirmation Calls in December
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "call-details",
  "filters": {
    "template": "order-confirmation",
    "date_from": "2025-12-01",
    "date_to": "2025-12-31",
    "status": "completed"
  },
  "options": {
    "page": 1,
    "limit": 50,
    "sort_by": "created_at",
    "sort_order": "desc"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "call-details",
    "filters_applied": {
      "template": "order-confirmation",
      "date_from": "2025-12-01",
      "date_to": "2025-12-31",
      "status": "completed",
      "shop_identifiers": ["shop_123"]
    },
    "results": [
      {
        "call_id": "call_xyz123",
        "template": "order-confirmation",
        "customer_phone": "+1234567890",
        "status": "completed",
        "duration": 135,
        "sentiment": "positive",
        "created_at": "2025-12-15T10:30:00Z",
        "metadata": {...}
      },
      // ... 49 more calls
    ],
    "pagination": {
      "page": 1,
      "limit": 50,
      "total": 1234,
      "total_pages": 25
    }
  }
}
```

### 3. Appointment Reminder Trends (High-Quality Calls)
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "trends",
  "filters": {
    "template": "appointment-reminder",
    "status": "completed",
    "call_duration_min": 60,
    "customer_sentiment": "positive"
  },
  "options": {
    "group_by": "date"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "trends",
    "filters_applied": {
      "template": "appointment-reminder",
      "status": "completed",
      "call_duration_min": 60,
      "customer_sentiment": "positive",
      "shop_identifiers": ["shop_123"]
    },
    "results": {
      "trends": [
        {
          "date": "2025-12-01",
          "total_calls": 45,
          "average_duration": 125.5,
          "success_rate": 88.9
        },
        {
          "date": "2025-12-02",
          "total_calls": 52,
          "average_duration": 132.0,
          "success_rate": 90.4
        }
        // ... more dates
      ],
      "summary": {
        "total_days": 31,
        "average_calls_per_day": 48.5,
        "trend": "increasing"
      }
    }
  }
}
```

### 4. Admin: Compare Templates for Specific Shop
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "performance",
  "filters": {
    "shop_identifier": "shop_456",
    "date_from": "2025-12-01",
    "date_to": "2025-12-31"
  },
  "options": {
    "group_by": "template"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "performance",
    "filters_applied": {
      "shop_identifier": "shop_456",
      "date_from": "2025-12-01",
      "date_to": "2025-12-31"
    },
    "results": {
      "by_template": [
        {
          "template": "order-confirmation",
          "total_calls": 850,
          "success_rate": 82.4,
          "average_duration": 145.2
        },
        {
          "template": "appointment-reminder",
          "total_calls": 320,
          "success_rate": 91.3,
          "average_duration": 118.5
        },
        {
          "template": "lead-followup",
          "total_calls": 150,
          "success_rate": 65.3,
          "average_duration": 98.0
        }
      ]
    }
  }
}
```

### 5. Weekly Trends Aggregation
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "trends",
  "filters": {
    "template": "order-confirmation",
    "date_from": "2025-11-01",
    "date_to": "2025-12-31"
  },
  "options": {
    "time_granularity": "week"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "trends",
    "filters_applied": {
      "template": "order-confirmation",
      "date_from": "2025-11-01",
      "date_to": "2025-12-31",
      "shop_identifiers": ["shop_123"]
    },
    "results": {
      "trends": [
        {
          "week": "2025-W44",  // ISO week format
          "week_start": "2025-11-01",
          "week_end": "2025-11-07",
          "total_calls": 315,
          "average_duration": 128.3,
          "success_rate": 82.5
        },
        {
          "week": "2025-W45",
          "week_start": "2025-11-08",
          "week_end": "2025-11-14",
          "total_calls": 342,
          "average_duration": 135.7,
          "success_rate": 84.2
        }
        // ... more weeks
      ],
      "summary": {
        "total_weeks": 9,
        "average_calls_per_week": 325.8,
        "trend": "increasing"
      }
    }
  }
}
```

### 6. Monthly Trends Aggregation
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "trends",
  "filters": {
    "date_from": "2025-01-01",
    "date_to": "2025-12-31"
  },
  "options": {
    "time_granularity": "month",
    "group_by": "template"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "trends",
    "filters_applied": {
      "date_from": "2025-01-01",
      "date_to": "2025-12-31",
      "shop_identifiers": ["shop_123", "shop_456"]
    },
    "results": {
      "trends": [
        {
          "month": "2025-01",
          "month_name": "January",
          "by_template": [
            {
              "template": "order-confirmation",
              "total_calls": 1250,
              "success_rate": 81.3
            },
            {
              "template": "appointment-reminder",
              "total_calls": 850,
              "success_rate": 89.5
            }
          ]
        },
        {
          "month": "2025-02",
          "month_name": "February",
          "by_template": [
            {
              "template": "order-confirmation",
              "total_calls": 1180,
              "success_rate": 83.1
            },
            {
              "template": "appointment-reminder",
              "total_calls": 920,
              "success_rate": 90.2
            }
          ]
        }
        // ... more months
      ]
    }
  }
}
```

### 7. Conversion Funnel Analysis
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "conversion",
  "filters": {
    "template": "order-confirmation",
    "date_from": "2025-12-01",
    "date_to": "2025-12-31"
  }
}
```

**Returns**:
```json
{
  "success": true,
  "data": {
    "type": "conversion",
    "filters_applied": {
      "template": "order-confirmation",
      "date_from": "2025-12-01",
      "date_to": "2025-12-31",
      "shop_identifiers": ["shop_123"]
    },
    "results": {
      "funnel": [
        {
          "stage": "initiated",
          "count": 1200,
          "percentage": 100
        },
        {
          "stage": "connected",
          "count": 980,
          "percentage": 81.7
        },
        {
          "stage": "completed",
          "count": 850,
          "percentage": 70.8
        },
        {
          "stage": "confirmed",
          "count": 720,
          "percentage": 60.0
        }
      ],
      "conversion_rate": 60.0,
      "drop_off_points": [
        {
          "stage": "initiated_to_connected",
          "drop_off": 220,
          "drop_off_rate": 18.3
        }
      ]
    }
  }
}
```

---

## 🔗 Conjunctive Filtering (AND Logic)

All filters in the `filters` object are applied together with AND logic:

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "call-details",
  "filters": {
    "template": "order-confirmation",
    "status": "completed",
    "call_duration_min": 120,
    "customer_sentiment": "positive",
    "date_from": "2025-12-01"
  }
}
```

This returns calls that match **ALL** of these conditions:
- ✅ Template is "order-confirmation" **AND**
- ✅ Status is "completed" **AND**
- ✅ Duration is at least 120 seconds **AND**
- ✅ Sentiment is "positive" **AND**
- ✅ Date is on or after 2025-12-01

---

## 🔐 Automatic Shop Filtering

For non-admin users, `shop_identifiers` are **automatically** applied from the JWT token:

```typescript
// Merchant makes request
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {
    "template": "order-confirmation"
  }
}

// Backend automatically adds shop_identifiers from JWT
filters_applied = {
  "template": "order-confirmation",
  "shop_identifiers": ["shop_123", "shop_456"]  // From JWT token
}

// Merchant can ONLY see their accessible shops' data
```

Admins (with `shop_identifiers: ["*"]`) can optionally specify `shop_identifier` or `shop_identifiers` in filters to view specific shop data.

---

## ✅ Advantages of This Design

### 1. Single Endpoint - Simple & Predictable
- Only one URL to remember
- Consistent request/response format
- Easy to document and test

### 2. Unlimited Filter Combinations
- Add any number of filters
- No URL length limits (payload-based)
- Easy to extend with new filters

### 3. Conjunctive Filtering Power
- Combine multiple filters with AND logic
- Drill down to specific data subsets
- Complex queries made simple

### 4. Type-Based Analytics
- Different analytics from same endpoint
- Summary, details, trends, conversion, performance
- Easy to add new types

### 5. Template-Agnostic
- Works with any template
- No code changes for new templates
- Consistent across all workflows

### 6. Pagination & Sorting Built-In
- Consistent pagination across all types
- Flexible sorting options
- Grouping support for aggregations

### 7. Automatic RBAC Enforcement
- Shop filtering from JWT token
- Admins can query all shops
- Multi-shop merchants can access their shops
- Security built into the design

---

## 🛠️ Backend Implementation Notes

### Request Validation (Pydantic Models)
```python
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import date

class AnalyticsFilters(BaseModel):
    template: Optional[str] = None
    shop_identifier: Optional[str] = None
    shop_identifiers: Optional[List[str]] = None
    status: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    call_duration_min: Optional[int] = None
    call_duration_max: Optional[int] = None
    customer_sentiment: Optional[str] = None

class AnalyticsOptions(BaseModel):
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=1000)
    group_by: Optional[str] = None
    time_granularity: Optional[Literal["day", "week", "month"]] = "day"  # For trends aggregation
    sort_by: Optional[str] = "created_at"
    sort_order: Literal["asc", "desc"] = "desc"

class AnalyticsRequest(BaseModel):
    type: Literal["summary", "call-details", "trends", "conversion", "performance"]
    filters: AnalyticsFilters = Field(default_factory=AnalyticsFilters)
    options: AnalyticsOptions = Field(default_factory=AnalyticsOptions)
```

### Endpoint Handler
```python
@router.post("/analytics")
async def get_analytics(
    request: AnalyticsRequest,
    current_user: TokenData = Depends(get_current_user)
):
    filters = request.filters.dict(exclude_none=True)

    # Get user's accessible shops from JWT
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    if accessible_shops is None:
        # Admin with ["*"] - can access all shops
        # Keep filters as-is (may include shop_identifier if admin specified)
        pass
    else:
        # Non-admin user - enforce shop access
        if "shop_identifier" in filters:
            # Validate user has access to requested shop
            if filters["shop_identifier"] not in accessible_shops:
                raise HTTPException(status_code=403, detail="Access denied to shop")
        elif "shop_identifiers" in filters:
            # Validate user has access to all requested shops
            if not all(shop in accessible_shops for shop in filters["shop_identifiers"]):
                raise HTTPException(status_code=403, detail="Access denied to one or more shops")
        else:
            # No shop filter - apply user's accessible shops
            filters["shop_identifiers"] = accessible_shops

    # Route to appropriate analytics handler based on type
    if request.type == "summary":
        return await get_summary_analytics(filters, request.options)
    elif request.type == "call-details":
        return await get_call_details(filters, request.options)
    elif request.type == "trends":
        return await get_trends_analytics(filters, request.options)
    elif request.type == "conversion":
        return await get_conversion_analytics(filters, request.options)
    elif request.type == "performance":
        return await get_performance_analytics(filters, request.options)
```

### Database Query Building
```python
def build_query_with_filters(base_query, filters: dict):
    """Apply all filters with AND logic"""
    query = base_query

    if "template" in filters:
        query = query.filter(Call.template == filters["template"])

    # Shop filtering (single or multiple)
    if "shop_identifier" in filters:
        query = query.filter(Call.shop_identifier == filters["shop_identifier"])
    elif "shop_identifiers" in filters:
        query = query.filter(Call.shop_identifier.in_(filters["shop_identifiers"]))

    if "status" in filters:
        query = query.filter(Call.status == filters["status"])

    if "date_from" in filters:
        query = query.filter(Call.created_at >= filters["date_from"])

    if "date_to" in filters:
        query = query.filter(Call.created_at <= filters["date_to"])

    if "call_duration_min" in filters:
        query = query.filter(Call.duration >= filters["call_duration_min"])

    if "call_duration_max" in filters:
        query = query.filter(Call.duration <= filters["call_duration_max"])

    if "customer_sentiment" in filters:
        query = query.filter(Call.sentiment == filters["customer_sentiment"])

    return query
```

---

## 🚀 Future Extensions (No Breaking Changes)

### Adding New Filters
Just add to the database query logic - **no endpoint changes needed**:

```python
# Add new filter: "agent_id"
if "agent_id" in filters:
    query = query.filter(Call.agent_id == filters["agent_id"])

# Frontend can immediately use it:
{
  "filters": {
    "agent_id": "agent_xyz",
    "template": "order-confirmation"
  }
}
```

### Adding New Analytics Types
Just add a new handler - **no endpoint changes needed**:

```python
elif request.type == "sentiment-analysis":
    return await get_sentiment_analysis(filters, request.options)

# Frontend can immediately use it:
{
  "type": "sentiment-analysis",
  "filters": {...}
}
```

---

## 📚 API Documentation Example

```yaml
paths:
  /agent/voice/breeze-buddy/analytics:
    post:
      summary: Get Analytics
      description: Flexible analytics endpoint supporting multiple types and filters
      security:
        - BearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/AnalyticsRequest'
            examples:
              summary:
                value:
                  type: summary
                  filters: {}
              call-details:
                value:
                  type: call-details
                  filters:
                    template: order-confirmation
                    status: completed
                  options:
                    page: 1
                    limit: 50
      responses:
        200:
          description: Analytics data
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/AnalyticsResponse'
        401:
          description: Unauthorized
        403:
          description: Forbidden
```

---

**This design provides maximum flexibility with minimum complexity!** 🎉
