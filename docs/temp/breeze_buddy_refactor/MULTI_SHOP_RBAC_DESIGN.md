# 🏪 Hierarchical Merchant + Shop RBAC Design

**Date**: 2025-12-17
**Status**: Implemented
**Based On**: Existing `merchant_id` and `shop_identifier` columns in database

---

## 🎯 Overview

This document describes the hierarchical Role-Based Access Control (RBAC) design that leverages the **existing `merchant_id` and `shop_identifier` columns** in the database while adding proper authorization through JWT tokens with both `merchant_ids` and `shop_identifiers` arrays.

### Key Principles

1. **Use existing database schema** - No changes to `merchant_id` or `shop_identifier` columns
2. **Add JWT-based hierarchical authorization** - Users have arrays of accessible merchant_ids AND shop_identifiers
3. **Support hierarchical access** - merchant_ids → shop_identifiers (Admin → Reseller → Merchant → Shop)
4. **Maintain fallback pattern** - Shop-specific → Merchant-wide (NULL shop_identifier)
5. **Scalability** - Merchant with 100 shops uses merchant_id + wildcard shops instead of listing all 100

---

## 📊 Current Database Schema (No Changes Needed!)

The database **already has** `shop_identifier` support:

### Existing Tables with shop_identifier

```sql
-- lead_call_tracker table (EXISTING)
CREATE TABLE lead_call_tracker (
    id UUID PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL,
    shop_identifier VARCHAR(255),  -- ✅ Already exists!
    template VARCHAR(255) NOT NULL,
    -- ... other columns
);

-- call_execution_config table (EXISTING)
CREATE TABLE call_execution_config (
    id UUID PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL,
    shop_identifier VARCHAR(255),  -- ✅ Already exists!
    template VARCHAR(255) NOT NULL,
    -- ... other columns
    UNIQUE (merchant_id, template, shop_identifier) WHERE shop_identifier IS NOT NULL,
    UNIQUE (merchant_id, template) WHERE shop_identifier IS NULL
);

-- template table (EXISTING)
CREATE TABLE template (
    id UUID PRIMARY KEY,
    merchant_id VARCHAR(255) NOT NULL,
    shop_identifier VARCHAR,  -- ✅ Already exists!
    name VARCHAR(255) NOT NULL,
    -- ... other columns
    UNIQUE (merchant_id, shop_identifier, name) WHERE shop_identifier IS NOT NULL,
    UNIQUE (merchant_id, name) WHERE shop_identifier IS NULL
);
```

### Fallback Pattern (Already Implemented!)

- `shop_identifier = "shop_123"` → Shop-specific data
- `shop_identifier = NULL` → Merchant-wide data (fallback)

---

## 🔐 New: Users Table with Hierarchical Access Control

**NEW additions** - Add hierarchical merchant + shop access control to users:

```sql
-- Migration: 005_create_users_table.sql

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'reseller', 'merchant', 'shop')),
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,

    -- Hierarchical access control (NEW!)
    merchant_ids JSONB DEFAULT '[]'::jsonb NOT NULL,       -- ["*"] or ["merchant_123"]
    shop_identifiers JSONB DEFAULT '[]'::jsonb NOT NULL,   -- ["*"] or ["shop_123", "shop_456"]

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);

-- Indexes
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_merchant_ids ON users USING GIN(merchant_ids);
CREATE INDEX idx_users_shop_identifiers ON users USING GIN(shop_identifiers);
CREATE INDEX idx_users_is_active ON users(is_active);
```

### Migration 007: Add merchant_ids

```sql
-- Migration: 007_add_merchant_ids_to_users.sql

-- Add merchant_ids column
ALTER TABLE users
ADD COLUMN IF NOT EXISTS merchant_ids JSONB DEFAULT '[]'::jsonb NOT NULL;

-- Create index
CREATE INDEX IF NOT EXISTS idx_users_merchant_ids ON users USING GIN (merchant_ids);

-- Update admin to have wildcard merchant access
UPDATE users
SET merchant_ids = '["*"]'::jsonb
WHERE username = 'admin_breeze_buddy';
```

### Example User Data

```sql
-- Admin (access to ALL merchants and ALL shops)
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'admin_breeze',
    '$2b$12$...',
    'admin',
    'admin@breezelabs.app',
    '["*"]'::jsonb,  -- All merchants
    '["*"]'::jsonb   -- All shops
);

-- Reseller (access to multiple merchants and ALL their shops)
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'reseller_acme',
    '$2b$12$...',
    'reseller',
    'reseller@acme.com',
    '["merchant_123", "merchant_456"]'::jsonb,  -- Specific merchants
    '["*"]'::jsonb   -- All shops under these merchants
);

-- Reseller (limited to specific shops across merchants)
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'reseller_regional',
    '$2b$12$...',
    'reseller',
    'reseller@regional.com',
    '["merchant_100"]'::jsonb,                  -- Single merchant
    '["shop_100", "shop_101", "shop_102"]'::jsonb  -- Specific shops
);

-- Merchant with 100 shops (using wildcard - SCALABLE!)
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'merchant_joe',
    '$2b$12$...',
    'merchant',
    'joe@joescoffee.com',
    '["merchant_123"]'::jsonb,  -- Single merchant
    '["*"]'::jsonb              -- All shops (no need to list 100 shops!)
);

-- Merchant (limited to specific shops)
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'merchant_mary',
    '$2b$12$...',
    'merchant',
    'mary@marysbakery.com',
    '["merchant_456"]'::jsonb,
    '["shop_789", "shop_790"]'::jsonb  -- 2 specific shops
);

-- Shop-level user
INSERT INTO users (username, password_hash, role, email, merchant_ids, shop_identifiers)
VALUES (
    'shop_manager_123',
    '$2b$12$...',
    'shop',
    'manager@shop123.com',
    '["merchant_123"]'::jsonb,
    '["shop_123"]'::jsonb
);
```

---

## 🎫 JWT Token Structure

```typescript
{
  "sub": "user_id",
  "username": "merchant_joe",
  "role": "merchant",
  "email": "joe@joescoffee.com",
  "merchant_ids": ["merchant_123"],              // NEW! Or ["*"] for all merchants
  "shop_identifiers": ["*"],                     // ["*"] for all shops or specific list
  "permissions": ["read:own_data", "write:own_data"],
  "iat": 1702998378,
  "exp": 1703084778
}
```

### Access Patterns

1. **Admin - All merchants, all shops**:
   ```json
   {"merchant_ids": ["*"], "shop_identifiers": ["*"]}
   ```

2. **Merchant - Single merchant, all shops (100+ shops)**:
   ```json
   {"merchant_ids": ["merchant_123"], "shop_identifiers": ["*"]}
   ```
   **Benefit**: No need to list 100 shop IDs!

3. **Merchant - Single merchant, specific shops**:
   ```json
   {"merchant_ids": ["merchant_123"], "shop_identifiers": ["shop_1", "shop_5"]}
   ```

4. **Reseller - Multiple merchants, all shops**:
   ```json
   {"merchant_ids": ["merchant_123", "merchant_456"], "shop_identifiers": ["*"]}
   ```

---

## 📊 Analytics Endpoint Design

### Single POST Endpoint

```
POST /agent/voice/breeze-buddy/analytics
```

### Request Payload

```typescript
{
  "type": "summary" | "call-details" | "trends" | "conversion" | "performance",
  "filters": {
    "template": string,
    "workflow": string,
    "shop_identifier": string,           // Single shop
    "shop_identifiers": string[],        // Multiple shops
    "status": string,
    "date_from": string,
    "date_to": string,
    // ... other filters
  },
  "options": {
    "page": number,
    "limit": number,
    "group_by": string,  // Can group by "shop_identifier"
    "sort_by": string,
    "sort_order": "asc" | "desc"
  }
}
```

---

## 💡 Use Case Examples

### 1. Admin - View All Shops

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {}
}

# User has shop_identifiers: ["*"]
# Backend: No shop filter applied (returns ALL shops)
```

### 2. Reseller (Limited) - View Their Shops

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {}
}

# User has shop_identifiers: ["shop_100", "shop_101", "shop_102"]
# Backend: Auto-apply WHERE shop_identifier IN ('shop_100', 'shop_101', 'shop_102')
```

### 3. Merchant (Multi-Shop) - View All Their Shops

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {}
}

# User has shop_identifiers: ["shop_123", "shop_456"]
# Backend: Auto-apply WHERE shop_identifier IN ('shop_123', 'shop_456')
```

### 4. Merchant - View Specific Shop

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {
    "shop_identifier": "shop_123"
  }
}

# User has shop_identifiers: ["shop_123", "shop_456"]
# Backend: Validate shop_123 is in user's array, then filter
# SQL: WHERE shop_identifier = 'shop_123'
```

### 5. Merchant - Compare Their Shops

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "performance",
  "filters": {},
  "options": {
    "group_by": "shop_identifier"
  }
}

# User has shop_identifiers: ["shop_123", "shop_456"]
# Returns breakdown by shop:
{
  "results": {
    "by_shop": [
      {"shop_identifier": "shop_123", "total_calls": 850},
      {"shop_identifier": "shop_456", "total_calls": 620}
    ]
  }
}
```

### 6. Admin - View Specific Shop

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {
    "shop_identifier": "shop_456"
  }
}

# User has shop_identifiers: ["*"]
# Backend: Admin can view any shop
# SQL: WHERE shop_identifier = 'shop_456'
```

### 7. Merchant - Try to Access Unauthorized Shop (DENIED!)

```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {
    "shop_identifier": "shop_999"
  }
}

# User has shop_identifiers: ["shop_123", "shop_456"]
# Backend: shop_999 NOT in user's array
# Response: 403 Forbidden - "Access denied to shop shop_999"
```

---

## 🔒 Backend Authorization Logic

```python
def get_accessible_shops(shop_identifiers: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible shops, or None if access to ALL shops

    Args:
        shop_identifiers: shop_identifiers array from JWT token

    Returns:
        None if user has access to ALL shops (["*"])
        List[str] of specific shop_identifiers otherwise
    """
    if "*" in shop_identifiers:
        return None  # None means "all shops"
    else:
        return shop_identifiers


@router.post("/analytics")
async def get_analytics(
    request: AnalyticsRequest,
    current_user: TokenData = Depends(get_current_user)
):
    filters = request.filters.dict(exclude_none=True)

    # Get user's accessible shop_identifiers from JWT
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    if accessible_shops is None:
        # User has access to ALL shops (admin/reseller with ["*"])

        if "shop_identifier" in filters:
            # Admin wants specific shop - allow
            pass

        elif "shop_identifiers" in filters:
            # Admin wants multiple specific shops - allow
            pass

        # else: No shop filter = return data from ALL shops

    else:
        # User has access to SPECIFIC shops only

        if "shop_identifier" in filters:
            requested_shop = filters["shop_identifier"]

            # Validate: Does user have access to this shop?
            if requested_shop not in accessible_shops:
                raise HTTPException(
                    status_code=403,
                    detail=f"Access denied to shop {requested_shop}"
                )
            # Keep the shop_identifier filter

        elif "shop_identifiers" in filters:
            requested_shops = filters["shop_identifiers"]

            # Validate: Does user have access to ALL requested shops?
            if not all(shop in accessible_shops for shop in requested_shops):
                raise HTTPException(
                    status_code=403,
                    detail="Access denied to one or more requested shops"
                )
            # Keep the shop_identifiers filter

        else:
            # No shop filter provided - apply user's accessible shops
            filters["shop_identifiers"] = accessible_shops

    # Execute analytics with validated shop filtering
    return await execute_analytics(request.type, filters, request.options)
```

---

## 🗄️ Database Query Implementation

### Query Builder (Works with Existing Schema!)

```python
def build_query_with_filters(base_query, filters: dict):
    """
    Build query with filters - works with existing shop_identifier column
    """
    query = base_query

    # Shop filtering (existing column!)
    if "shop_identifier" in filters:
        # Single shop
        query = query.filter(
            LeadCallTracker.shop_identifier == filters["shop_identifier"]
        )
    elif "shop_identifiers" in filters:
        # Multiple shops (IN clause)
        query = query.filter(
            LeadCallTracker.shop_identifier.in_(filters["shop_identifiers"])
        )

    # Template filtering
    if "template" in filters:
        query = query.filter(LeadCallTracker.template == filters["template"])

    # Status filtering
    if "status" in filters:
        query = query.filter(LeadCallTracker.status == filters["status"])

    # Date range filtering
    if "date_from" in filters:
        query = query.filter(LeadCallTracker.created_at >= filters["date_from"])

    if "date_to" in filters:
        query = query.filter(LeadCallTracker.created_at <= filters["date_to"])

    # ... other filters

    return query
```

### Example SQL Generated

```sql
-- User with shop_identifiers: ["shop_123", "shop_456"]
SELECT * FROM lead_call_tracker
WHERE shop_identifier IN ('shop_123', 'shop_456')
  AND template = 'order-confirmation'
  AND status = 'completed';

-- Admin with shop_identifiers: ["*"]
SELECT * FROM lead_call_tracker
WHERE template = 'order-confirmation'
  AND status = 'completed';
-- (no shop_identifier filter)
```

---

## 📋 Access Control Matrix

| User Type | `merchant_ids` | `shop_identifiers` | View All Shops | View Specific Shop | Create Data | Group by Shop |
|-----------|---------------|-------------------|----------------|-------------------|-------------|---------------|
| **Admin** | `["*"]` | `["*"]` | ✅ Yes (all) | ✅ Any shop | ✅ Any shop | ✅ Yes |
| **Reseller (All)** | `["m1", "m2"]` | `["*"]` | ✅ Yes (all under merchants) | ✅ Any shop | ✅ Any shop | ✅ Yes |
| **Reseller (Limited)** | `["m1"]` | `["shop_1", "shop_2"]` | ✅ Their shops | ✅ If in list | ✅ If in list | ✅ Yes |
| **Merchant (100 shops!)** | `["merchant_123"]` | `["*"]` | ✅ All their shops | ✅ If they own | ✅ If they own | ✅ Yes |
| **Merchant (Specific)** | `["merchant_123"]` | `["shop_a", "shop_b"]` | ✅ Their shops | ✅ If in list | ✅ If in list | ✅ Yes |
| **Shop Manager** | `["merchant_123"]` | `["shop_y"]` | ✅ Their shop | ✅ Only shop_y | ✅ Only shop_y | ❌ No (1 shop) |

---

## 🔧 PostgreSQL JSONB Operations

### Check Access

```sql
-- Check if user has access to shop_123
SELECT * FROM users
WHERE
    (shop_identifiers @> '["*"]'::jsonb)  -- Has wildcard
    OR (shop_identifiers @> '["shop_123"]'::jsonb);  -- Has explicit access
```

### Add Shop to User

```sql
UPDATE users
SET shop_identifiers = shop_identifiers || '["shop_new"]'::jsonb
WHERE id = 'user_uuid';
```

### Remove Shop from User

```sql
UPDATE users
SET shop_identifiers = shop_identifiers - 'shop_old'
WHERE id = 'user_uuid';
```

### Grant All Shop Access

```sql
UPDATE users
SET shop_identifiers = '["*"]'::jsonb
WHERE id = 'user_uuid';
```

---

## 🚀 Migration Strategy

### Phase 1: Add Users Table (NEW)

```sql
-- Add users table with shop_identifiers
CREATE TABLE users (...);
```

### Phase 2: Populate Users

```sql
-- Create admin
INSERT INTO users (username, ..., shop_identifiers) VALUES (..., '["*"]');

-- Create merchants based on existing data
INSERT INTO users (username, ..., shop_identifiers)
SELECT DISTINCT
    merchant_id,
    ...,
    jsonb_agg(DISTINCT shop_identifier)
FROM lead_call_tracker
WHERE shop_identifier IS NOT NULL
GROUP BY merchant_id;
```

### Phase 3: Update JWT Generation

- Add `shop_identifiers` to JWT payload
- Extract from users table during login

### Phase 4: Add Authorization Middleware

- Validate shop access on all protected endpoints
- Auto-apply shop filters based on user's shop_identifiers

### Phase 5: Existing Data Works As-Is!

- No changes to `lead_call_tracker`, `template`, `call_execution_config`
- Existing `shop_identifier` column continues to work
- Fallback pattern (NULL for merchant-wide) maintained

---

## ✅ Benefits of This Design

### 1. No Database Migration Pain
- ✅ Uses existing `shop_identifier` column
- ✅ Only adds `users` table
- ✅ No data migration needed

### 2. Security Added
- ✅ JWT-based shop access control
- ✅ Users can't access unauthorized shops
- ✅ Proper RBAC enforcement

### 3. Flexibility
- ✅ `["*"]` for global access
- ✅ Specific shop list for limited access
- ✅ Works for any hierarchy

### 4. Performance
- ✅ GIN index on JSONB for fast lookups
- ✅ Efficient IN queries for multiple shops
- ✅ No complex joins needed

### 5. Backward Compatibility
- ✅ Existing fallback pattern preserved
- ✅ NULL shop_identifier still works
- ✅ Existing queries unchanged

---

## 📊 Summary

| Aspect | Implementation |
|--------|----------------|
| **Database Changes** | Add `users` table with `merchant_ids` and `shop_identifiers` JSONB columns |
| **Existing Tables** | No changes to `lead_call_tracker`, `template`, `call_execution_config` |
| **JWT Token** | Add `merchant_ids` and `shop_identifiers` arrays to payload |
| **Authorization** | Hierarchical validation: merchant → shop access in middleware |
| **Query Pattern** | Use IN clause for multiple merchants/shops, = for single |
| **Fallback** | Shop-specific → Merchant-wide (NULL) pattern preserved |
| **Access Control** | `["*"]` = wildcard (all), specific arrays for limited access |
| **Scalability** | Merchant with 100 shops: `merchant_ids: ["m123"], shop_identifiers: ["*"]` |

---

## 🚀 Key Benefit: Scalability

**Before** (merchant with 100 shops):
```json
{
  "shop_identifiers": ["shop_1", "shop_2", ..., "shop_100"]  // ❌ Impractical!
}
```

**After** (hierarchical approach):
```json
{
  "merchant_ids": ["merchant_123"],
  "shop_identifiers": ["*"]  // ✅ All shops under merchant_123
}
```

---

**This design leverages your existing database schema while adding hierarchical merchant + shop RBAC security through JWT tokens!** 🎯
