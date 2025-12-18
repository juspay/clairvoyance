# 🚀 CLAIRVOYANCE - IMPLEMENTATION PLAN

**Document Version**: 1.0
**Created**: 2025-12-17
**Project**: Clairvoyance (Breeze Voice Agents Platform)
**Status**: Ready for Implementation

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Phase 1: Token Authentication Implementation](#phase-1-token-authentication-implementation)
3. [Phase 2: API Reorganization](#phase-2-api-reorganization)
4. [Phase 3: Static Pages Cleanup](#phase-3-static-pages-cleanup)
5. [Phase 4: Testing & Deployment](#phase-4-testing--deployment)
6. [Timeline & Effort Estimates](#timeline--effort-estimates)

---

## 🎯 Overview

This document outlines the complete implementation plan for three major improvements to the Clairvoyance platform:

1. **Token-Based Authentication Migration** - Move from cookie-based session auth to JWT token-based authentication with RBAC
2. **API Reorganization** - Move Breeze Buddy APIs (analytics, auth) into proper folders with better endpoints
   - **Template-Agnostic Design**: All endpoints will be generic and work with any template/workflow (not hardcoded to "order-confirmation")
3. **Static Pages Cleanup** - Remove existing static pages as they are moved to a new repository

### Current State

- **Authentication**: Mixed authentication patterns
  - JWT Bearer tokens for API endpoints
  - Session cookies for dashboard endpoints
  - Lighthouse JWT validation for Automatic agent
- **API Structure**: Breeze Buddy APIs spread across different routers
  - Auth in `auth.py`
  - Analytics in `dashboard.py`
  - Templates, leads, configs in separate files
- **Static Pages**: Dashboard HTML served from backend
  - `/static/home.html` - Root page
  - `/app/ai/voice/agents/breeze_buddy/dashboard/index.html` - Dashboard
  - `/app/ai/voice/agents/breeze_buddy/dashboard/login.html` - Login page

### Target State

- **Authentication**: Unified JWT token-based authentication
  - Single authentication pattern across all endpoints
  - Role-Based Access Control (Admin, Merchant)
  - Proper permission management
  - All tokens follow Bearer standard
- **API Structure**: Clean, organized, template-agnostic API structure
  - `/agent/voice/breeze-buddy/auth/*` - All auth endpoints
  - `/agent/voice/breeze-buddy/analytics` - **SINGLE POST endpoint** for all analytics (payload-based filtering)
  - `/agent/voice/breeze-buddy/configurations/*` - Configuration management (works for any template)
  - `/agent/voice/breeze-buddy/numbers/*` - Outbound numbers (works for any template)
  - Payload-based filtering for analytics (flexible, conjunctive)
  - Clear separation of concerns
  - RESTful endpoint naming
- **Static Pages**: Removed from backend
  - Dashboard moved to separate repository
  - Backend serves only API endpoints
  - Clean separation of frontend/backend

### Key Design Principles

**🎨 Template-Agnostic Architecture**

All endpoints are designed to be generic and template-agnostic:

| Aspect | Old Approach | New Approach |
|--------|-------------|--------------|
| **Endpoint Paths** | `/breeze/order-confirmation/analytics` | `POST /analytics` (payload-based) |
| **Hard-coded Templates** | Endpoints tied to "order-confirmation" | Single endpoint, template in payload |
| **Scalability** | New template = new endpoints | New template = same endpoint, different payload |
| **Flexibility** | Template name in URL path | All filters in request payload |
| **Filtering** | Limited query parameters | Unlimited conjunctive filters in payload |

**Benefits**:
- ✅ Works with any template (order-confirmation, appointment-reminder, etc.)
- ✅ No code changes needed for new templates/workflows
- ✅ Single analytics endpoint - simple and predictable
- ✅ Unlimited filter combinations via payload
- ✅ Conjunctive filtering (AND logic for all filters)
- ✅ Different analytics types from one endpoint (summary, call-details, trends, etc.)
- ✅ Easy to extend with new filter types

---

## 📦 Phase 1: Token Authentication Implementation

Based on `TOKEN_AUTH_IMPLEMENTATION.md` - implementing JWT token-based authentication with RBAC.

### 1.1 Database Changes

#### 1.1.1 Create Users Table

**File**: New migration script
**Location**: `/app/database/migrations/`

```sql
-- Migration: 001_create_users_table.sql

BEGIN;

-- Create users table for authentication and authorization
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'reseller', 'merchant', 'shop')),
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,

    -- Multi-shop access control (leverages existing shop_identifier in other tables)
    shop_identifiers JSONB DEFAULT '[]'::jsonb,  -- ["*"] for all shops, or ["shop_123", "shop_456"]

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_shop_identifiers ON users USING GIN(shop_identifiers);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);

-- Create default admin user (access to ALL shops)
-- TODO: Replace password hash with actual hashed password
INSERT INTO users (username, password_hash, role, shop_identifiers, email)
VALUES (
    'admin_breeze_buddy',
    '$2b$12$REPLACE_WITH_ACTUAL_HASH',
    'admin',
    '["*"]'::jsonb,  -- Wildcard = access to all shops
    'admin@breezelabs.app'
) ON CONFLICT (username) DO NOTHING;

COMMIT;
```

**Rollback script**:
```sql
-- Migration: 001_rollback_users_table.sql
BEGIN;
DROP TABLE IF EXISTS users CASCADE;
COMMIT;
```

**Tasks**:
- [ ] Create migration script
- [ ] Generate secure password hash for admin user
- [ ] Run migration on development database
- [ ] Verify indexes created
- [ ] Test rollback script

**Effort**: 2 hours

#### 1.1.2 Create Database Accessor for Users

**File**: `/app/database/accessor/breeze_buddy/users.py` (NEW)

**Content**:
- `get_user_by_username(username: str) -> Optional[UserInDB]`
- `get_user_by_id(user_id: str) -> Optional[UserInDB]`
- `create_user(user_data: UserCreate, password_hash: str) -> Optional[User]`
- `update_user_merchant_ids(user_id: str, merchant_ids: List[str]) -> bool`
- `update_user_shop_identifiers(user_id: str, shop_identifiers: List[str]) -> bool`
- `update_user_role(user_id: str, role: UserRole) -> bool`
- `has_merchant_access(user_id: str, merchant_id: str) -> bool`
- `has_shop_access(user_id: str, shop_identifier: str) -> bool`

**Tasks**:
- [ ] Create users accessor file
- [ ] Implement all database functions
- [ ] Add proper error handling
- [ ] Write unit tests

**Effort**: 3 hours

### 1.2 Backend Authentication Changes

#### 1.2.1 Create Breeze Buddy RBAC Token Manager

**File**: `/app/ai/voice/agents/breeze_buddy/security/rbac_token.py` (NEW)

**Changes**:
1. Create Breeze Buddy-specific RBAC token manager
2. Add role and permissions to token payload
3. Add merchant_ids and shop_identifiers arrays to token payload
4. Implement permission helper functions
5. Use generic JWT manager for core token operations

**New functions to add**:
- `get_permissions_for_role(role: str) -> List[str]`
- `create_access_token_with_rbac(user_id, username, role, merchant_ids, shop_identifiers, email) -> str`
- `verify_rbac_token(token: str) -> UserInfo`

**Tasks**:
- [ ] Add RBAC fields to token payload (role, permissions, merchant_ids, shop_identifiers)
- [ ] Implement permission helper functions
- [ ] Update UserInfo model to include role, permissions, merchant_ids, shop_identifiers
- [ ] Test token generation with RBAC fields
- [ ] Verify token validation still works

**Effort**: 4 hours

#### 1.2.2 Create Breeze Buddy Authorization Utilities

**File**: `/app/ai/voice/agents/breeze_buddy/security/authorization.py` (NEW)

**Changes**:
1. Create Breeze Buddy-specific authorization functions
2. Add merchant access validation functions
3. Add shop access validation functions
4. Add hierarchical merchant + shop filtering logic

**New functions to add**:
- `get_accessible_merchants(merchant_ids: List[str]) -> Optional[List[str]]`
- `get_accessible_shops(shop_identifiers: List[str]) -> Optional[List[str]]`
- `validate_merchant_access(current_user: UserInfo, merchant_id: str) -> None`
- `validate_shop_access(current_user: UserInfo, shop_identifier: str) -> None`
- `apply_merchant_shop_filter(current_user: UserInfo, ...) -> tuple[Optional[List[str]], Optional[List[str]]]`

**Example**:
```python
def get_accessible_merchants(merchant_ids: List[str]) -> Optional[List[str]]:
    """Returns list of accessible merchants, or None if wildcard access"""
    if "*" in merchant_ids:
        return None  # None means "all merchants"
    return merchant_ids

def apply_merchant_shop_filter(
    current_user: UserInfo,
    requested_merchant_id: Optional[str] = None,
    requested_shop_identifier: Optional[str] = None,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """Apply hierarchical merchant and shop filter"""
    accessible_merchants = get_accessible_merchants(current_user.merchant_ids)
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    # Validate and return (merchant_filter, shop_filter)
    # None in either position means no filter needed (wildcard access)
    ...
```

**Tasks**:
- [ ] Implement merchant access validation functions
- [ ] Implement shop access validation functions
- [ ] Implement hierarchical filtering logic
- [ ] Add tests for authorization checks
- [ ] Document usage examples

**Effort**: 3 hours

#### 1.2.3 Update Login Endpoint

**File**: `/app/api/routers/breeze_buddy/auth.py` (MODIFY)

**Current endpoint**: `POST /agent/voice/breeze-buddy/login`

**Changes**:
1. Remove session cookie response
2. Return JWT token in response body
3. Verify password using new users accessor
4. Generate RBAC token with role, permissions, merchant_ids, and shop_identifiers
5. Remove HTML login page endpoint (moved to new repo)

**New response format**:
```json
{
    "success": true,
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "Bearer",
    "expires_in": 86400,
    "user": {
        "username": "admin_breeze_buddy",
        "role": "admin",
        "merchant_ids": ["*"],
        "shop_identifiers": ["*"]
    }
}
```

**Tasks**:
- [ ] Update login endpoint to use users table
- [ ] Add password verification with bcrypt
- [ ] Return JWT token in response body
- [ ] Add rate limiting (5 attempts per 5 minutes)
- [ ] Add audit logging for login attempts
- [ ] Remove session cookie logic
- [ ] Update error responses
- [ ] Test login flow with valid/invalid credentials

**Effort**: 4 hours

#### 1.2.4 Remove Session-Based Authentication

**Files to modify**:
- `/app/core/security/jwt.py` - Remove `get_breeze_buddy_session`
- `/app/api/routers/breeze_buddy/dashboard.py` - Update all endpoints
- `/app/api/routers/breeze_buddy/call_execution_config.py` - Update dashboard endpoints

**Changes**:
1. Replace `Depends(get_breeze_buddy_session)` with `Depends(get_current_user)`
2. Add proper permission checks
3. Add merchant filtering where needed
4. Update all dashboard endpoints to use Bearer tokens

**Tasks**:
- [ ] Find all uses of `get_breeze_buddy_session`
- [ ] Replace with `get_current_user` + permission checks
- [ ] Add merchant filtering to data queries
- [ ] Test each updated endpoint
- [ ] Remove session-related code

**Effort**: 5 hours

### 1.3 Password Hashing Implementation

#### 1.3.1 Add bcrypt Dependency

**File**: `/requirements.txt` (MODIFY)

Add: `bcrypt==4.1.2`

**Tasks**:
- [ ] Add bcrypt to requirements.txt
- [ ] Update requirements in local environment
- [ ] Update Docker image

**Effort**: 0.5 hours

#### 1.3.2 Create Password Utilities

**File**: `/app/core/security/password.py` (NEW)

```python
import bcrypt

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')

def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a hash"""
    return bcrypt.checkpw(
        password.encode('utf-8'),
        password_hash.encode('utf-8')
    )
```

**Tasks**:
- [ ] Create password utilities file
- [ ] Add password hashing function
- [ ] Add password verification function
- [ ] Write unit tests
- [ ] Document usage

**Effort**: 2 hours

### 1.4 Endpoint Permission Protection

#### 1.4.1 Update Analytics Endpoints

**File**: `/app/api/routers/breeze_buddy/dashboard.py` (MODIFY)

**Endpoints to update**:
- `GET /breeze/order-confirmation/analytics`
- `GET /breeze/order-confirmation/call-details`

**Changes**:
1. Add permission check using Breeze Buddy RBAC
2. Add hierarchical merchant + shop filtering
3. Update database queries to filter by merchant_ids and shop_identifiers

**Example**:
```python
from app.ai.voice.agents.breeze_buddy.security import (
    get_current_user_with_rbac,
    apply_merchant_shop_filter,
)

@router.post("/agent/voice/breeze-buddy/analytics")
async def get_analytics(
    request: AnalyticsRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    # Check permission
    if "analytics:read" not in current_user.permissions:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Apply hierarchical merchant + shop filter
    merchant_filter, shop_filter = apply_merchant_shop_filter(
        current_user,
        requested_merchant_id=request.filters.get("merchant_id"),
        requested_shop_identifier=request.filters.get("shop_identifier"),
    )

    # Query with filters (None means no filter - wildcard access)
    analytics_data = await get_analytics_data(
        merchant_ids=merchant_filter,  # None for wildcard, list for specific
        shop_identifiers=shop_filter,  # None for wildcard, list for specific
        **request.filters
    )

    return analytics_data
```

**Tasks**:
- [ ] Add permission checks to analytics endpoint
- [ ] Add hierarchical merchant + shop filtering to queries
- [ ] Add permission checks to call details endpoint
- [ ] Add hierarchical filtering to call details queries
- [ ] Test admin access (sees all merchants and shops)
- [ ] Test merchant access (sees only their shops)
- [ ] Test shop access (sees only their shop)

**Effort**: 4 hours

#### 1.4.2 Update Configuration Endpoints

**File**: `/app/api/routers/breeze_buddy/call_execution_config.py` (MODIFY)

**Changes**:
1. Add role-based access:
   - Admins: Full CRUD access
   - Merchants: Read-only access to own configs
2. Add merchant filtering
3. Update permission checks

**Tasks**:
- [ ] Add permission checks (admin: write, merchant: read-only)
- [ ] Add merchant filtering to GET endpoints
- [ ] Prevent merchants from modifying configs
- [ ] Test admin CRUD operations
- [ ] Test merchant read-only access

**Effort**: 3 hours

#### 1.4.3 Update Outbound Numbers Endpoints

**File**: `/app/api/routers/breeze_buddy/outbound_numbers.py` (MODIFY)

**Changes**:
1. Add merchant filtering
2. Merchants can only see/manage own numbers
3. Admins can see/manage all numbers

**Tasks**:
- [ ] Add merchant filtering to GET endpoint
- [ ] Add merchant check to POST endpoint
- [ ] Add merchant check to DELETE endpoint
- [ ] Test admin access to all numbers
- [ ] Test merchant access to own numbers

**Effort**: 2 hours

### 1.5 Testing Checklist

**Functional Tests**:
- [ ] Login with valid credentials returns JWT token
- [ ] Login with invalid credentials returns error
- [ ] Token includes role and permissions
- [ ] Token includes merchant_ids and shop_identifiers arrays
- [ ] API calls with valid token succeed
- [ ] API calls with expired token return 401
- [ ] API calls with invalid token return 401

**RBAC Tests**:
- [ ] Admin can view all merchants and shops
- [ ] Admin can view all analytics and call details
- [ ] Admin can create/update/delete configurations
- [ ] Reseller can view only assigned merchants/shops
- [ ] Merchant can view only own shops' analytics
- [ ] Merchant can view only own shops' call details
- [ ] Merchant with 100 shops uses wildcard access efficiently
- [ ] Shop user can view only single shop data
- [ ] Merchant cannot view other merchants' data
- [ ] Shop cannot view other shops' data
- [ ] Merchant cannot modify configurations
- [ ] Merchant can view own configurations

**Security Tests**:
- [ ] Passwords are hashed with bcrypt
- [ ] JWT signature is validated
- [ ] Expired tokens are rejected
- [ ] Rate limiting prevents brute force
- [ ] merchant_ids and shop_identifiers from token are enforced (not from params)
- [ ] Wildcard access (*) works correctly for admins
- [ ] Hierarchical filtering (merchant → shop) works correctly

**Effort**: 8 hours

---

## 🗂️ Phase 2: API Reorganization

Reorganize Breeze Buddy APIs into proper folders with better endpoints.

### 🔄 Deprecation Strategy

**IMPORTANT**: For backward compatibility, we will maintain both old and new endpoints during migration:

**Affected Router Files** (Keep OLD + Add NEW):
1. **Templates** (`template.py`)
   - Keep: `/template` (GET, POST) with deprecation warnings
   - Add: `/templates` (GET, POST, GET/{id}, PUT/{id}, DELETE/{id})

2. **Call Execution Config** (`call_execution_config.py` → `configurations.py`)
   - Keep: `/call-execution-config` (GET, POST, PUT) with deprecation warnings
   - Add: `/configurations` (GET, POST, GET/{id}, PUT/{id}, DELETE/{id})

3. **Leads** (`leads.py`)
   - Keep: `/lead/{id}` (GET), `/push/lead/v2` (POST) with deprecation warnings
   - Add: `/leads` (GET/{id}, POST, POST/{id}/trigger)
   - ⚠️ **DO NOT CHANGE**: `POST /{merchant}/{template}` - Requires review first

**Keep As-Is** (NO changes):
- **Callbacks** (`callbacks.py`) - Provider-specific, external integrations depend on these
- **WebSockets** (`websocket.py`) - Template-specific by design

**Needs Review** (DO NOT change without investigation):
- `GET /cron/initiate` - Unknown purpose, investigate first
- `POST /{merchant}/{template}` - Lead trigger endpoint, clarify first

### 2.1 Create New API Structure

#### 2.1.1 Authentication Endpoints

**Current**:
- `POST /agent/voice/breeze-buddy/login`
- `GET /agent/voice/breeze-buddy/logout`

**New**:
- `POST /agent/voice/breeze-buddy/auth/login`
- `POST /agent/voice/breeze-buddy/auth/logout`
- `GET /agent/voice/breeze-buddy/auth/me` (Get current user info)

**File**: `/app/api/routers/breeze_buddy/auth.py` (MODIFY)

**Tasks**:
- [ ] Update login endpoint path
- [ ] Add logout endpoint (clears token client-side, but provide endpoint for consistency)
- [ ] Add `/auth/me` endpoint to get current user info
- [ ] Update router prefix in main app
- [ ] Add API documentation

**Effort**: 2 hours

#### 2.1.2 Analytics Endpoint (Single Flexible POST Endpoint)

**Current** (template-specific):
- `GET /agent/voice/breeze-buddy/breeze/order-confirmation/analytics`
- `GET /agent/voice/breeze-buddy/breeze/order-confirmation/call-details`

**New** (single flexible POST endpoint):
- `POST /agent/voice/breeze-buddy/analytics` - **ONE endpoint for ALL analytics**

**Design Philosophy**:
- 🎯 One endpoint to rule them all
- 📦 POST request with flexible payload
- 🔄 Returns different analytics based on requested `type`
- 🎛️ All filters in payload (template, date range, status, etc.)
- 🔗 Conjunctive filtering (all filters applied together)

**Request Payload Structure**:
```typescript
{
  "type": "summary" | "call-details" | "trends" | "conversion" | "performance",
  "filters": {
    "template": "order-confirmation",      // Optional: filter by template
    "shop_identifier": "shop_123",         // Optional: single shop
    "shop_identifiers": ["shop_123", "shop_456"],  // Optional: multiple shops
    "status": "completed",                 // Optional: call status
    "date_from": "2025-01-01",            // Optional: start date
    "date_to": "2025-12-31",              // Optional: end date
    "call_duration_min": 30,              // Optional: minimum call duration (seconds)
    "call_duration_max": 300,             // Optional: maximum call duration
    "customer_sentiment": "positive"       // Optional: sentiment filter
  },
  "options": {
    "page": 1,                             // Pagination
    "limit": 50,                           // Items per page
    "group_by": "template",                // Group results by field
    "sort_by": "created_at",               // Sort field
    "sort_order": "desc"                   // Sort direction
  }
}
```

**Example Requests**:

1. **Summary analytics for all templates**:
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {},
  "options": {}
}

# Returns: Total calls, success rate, avg duration across ALL templates
```

2. **Call details for order-confirmation template**:
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "call-details",
  "filters": {
    "template": "order-confirmation",
    "status": "completed",
    "date_from": "2025-12-01",
    "date_to": "2025-12-31"
  },
  "options": {
    "page": 1,
    "limit": 50
  }
}

# Returns: Paginated list of completed calls for order-confirmation in December
```

3. **Trends with multiple filters (conjunctive)**:
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

# Returns: Daily trends for appointment-reminder calls that:
#   - Were completed AND
#   - Lasted at least 60 seconds AND
#   - Had positive sentiment
```

4. **Admin: Analytics for specific shop and template**:
```bash
POST /agent/voice/breeze-buddy/analytics
{
  "type": "summary",
  "filters": {
    "shop_identifier": "shop_456",
    "template": "order-confirmation"
  }
}

# Returns: Summary for shop_456's order-confirmation calls only
```

5. **Merchant: Compare their multiple shops**:
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
# Returns: Performance breakdown by shop
{
  "results": {
    "by_shop": [
      {"shop_identifier": "shop_123", "total_calls": 850},
      {"shop_identifier": "shop_456", "total_calls": 620}
    ]
  }
}
```

**Response Structure**:
```typescript
{
  "success": true,
  "data": {
    "type": "summary",
    "filters_applied": {
      "template": "order-confirmation",
      "date_from": "2025-12-01",
      "date_to": "2025-12-31",
      "shop_identifiers": ["shop_123"]  // Auto-applied from JWT for non-admin users
    },
    "results": {
      // Structure varies by type
      "total_calls": 1234,
      "completed_calls": 980,
      "failed_calls": 254,
      "average_duration": 125.5,
      // ... more metrics based on type
    },
    "pagination": {  // Only for paginated types
      "page": 1,
      "limit": 50,
      "total": 1234,
      "total_pages": 25
    }
  }
}
```

**Supported Analytics Types**:

| Type | Description | Returns |
|------|-------------|---------|
| `summary` | Aggregate statistics | Total calls, success rate, avg duration, conversion rates |
| `call-details` | Individual call records | Paginated list of calls with full details |
| `trends` | Time-series data | Daily/weekly/monthly trends |
| `conversion` | Conversion metrics | Funnel analysis, conversion rates by stage |
| `performance` | Performance metrics | Success rates by template, agent performance |

**File**: `/app/api/routers/breeze_buddy/analytics.py` (NEW)

**Tasks**:
- [ ] Create new analytics router file
- [ ] Create single POST endpoint `/analytics`
- [ ] Create Pydantic models for request payload (AnalyticsRequest, Filters, Options)
- [ ] Implement payload validation (type, filters, options)
- [ ] Implement type-based analytics logic:
  - [ ] `summary` - Aggregate statistics
  - [ ] `call-details` - Paginated call records
  - [ ] `trends` - Time-series data
  - [ ] `conversion` - Conversion metrics
  - [ ] `performance` - Performance analytics
- [ ] Implement conjunctive filtering (AND logic for all filters)
- [ ] Add automatic shop filtering from JWT token (users see only their accessible shops)
- [ ] Add shop access validation (prevent unauthorized shop access)
- [ ] Add permission checks (require "analytics:read")
- [ ] Handle pagination for call-details type
- [ ] Handle grouping and sorting based on options
- [ ] Return consistent response structure
- [ ] Add comprehensive error handling (invalid type, invalid filters)
- [ ] Update API documentation with examples for each type
- [ ] Register new router in main app

**Effort**: 6 hours (increased for flexible payload handling and multiple analytics types)

#### 2.1.3 Call Execution Config Endpoints

**Current** (template-specific):
- `POST /agent/voice/breeze-buddy/call-execution-config`
- `PUT /agent/voice/breeze-buddy/call-execution-config`
- `GET /agent/voice/breeze-buddy/call-execution-config/{merchant_id}`
- `GET /agent/voice/breeze-buddy/breeze/order-confirmation/call-execution-configs` (dashboard, template-specific)

**New** (template-agnostic):
- `POST /agent/voice/breeze-buddy/configurations` - Create config for any template
- `PUT /agent/voice/breeze-buddy/configurations/{config_id}` - Update config
- `GET /agent/voice/breeze-buddy/configurations` - List all configs (with filters)
- `GET /agent/voice/breeze-buddy/configurations/{config_id}` - Get single config
- `DELETE /agent/voice/breeze-buddy/configurations/{config_id}` - Delete config

**Query Parameters** (for filtering):
- `?template={template_name}` - Filter configs by template
- `?merchant_id={merchant_id}` - Filter by merchant (admin only)
- `?active={true|false}` - Filter by active status

**Example requests**:
```bash
# Get all configurations
GET /agent/voice/breeze-buddy/configurations

# Get configurations for order-confirmation template
GET /agent/voice/breeze-buddy/configurations?template=order-confirmation

# Get configurations for appointment-reminder template
GET /agent/voice/breeze-buddy/configurations?template=appointment-reminder

# Create configuration for any template
POST /agent/voice/breeze-buddy/configurations
{
  "template": "order-confirmation",
  "merchant_id": "shop_123",
  "config": {...}
}
```

**File**: `/app/api/routers/breeze_buddy/call_execution_config.py` (MODIFY)

**Tasks**:
- [ ] Rename router file to `configurations.py`
- [ ] **KEEP OLD ENDPOINTS** with deprecation warnings (backward compatibility)
- [ ] **ADD NEW ENDPOINTS** alongside old ones
- [ ] Update endpoint paths (remove template-specific parts)
- [ ] Consolidate dashboard endpoint into main GET endpoint
- [ ] Add query parameter filtering for template/merchant
- [ ] Add DELETE endpoint
- [ ] Ensure configs work for any template type
- [ ] Update router prefix
- [ ] Add proper RESTful design
- [ ] Update API documentation with template flexibility

**Deprecation Strategy**: Both old (`/call-execution-config`) and new (`/configurations`) endpoints will coexist

**Effort**: 4 hours (increased due to generic filtering)

#### 2.1.4 Outbound Numbers Endpoints

**Current** (template-specific):
- `POST /agent/voice/breeze-buddy/outbound-number`
- `GET /agent/voice/breeze-buddy/outbound-number`
- `DELETE /agent/voice/breeze-buddy/outbound-number/{number_id}`
- `GET /agent/voice/breeze-buddy/breeze/order-confirmation/outbound-numbers` (dashboard, template-specific)

**New** (template-agnostic):
- `POST /agent/voice/breeze-buddy/numbers` - Add number for any template
- `GET /agent/voice/breeze-buddy/numbers` - List all numbers (with filters)
- `GET /agent/voice/breeze-buddy/numbers/{number_id}` - Get single number
- `PUT /agent/voice/breeze-buddy/numbers/{number_id}` - Update number details
- `DELETE /agent/voice/breeze-buddy/numbers/{number_id}` - Delete/disable number

**Query Parameters** (for filtering):
- `?template={template_name}` - Filter numbers by template
- `?merchant_id={merchant_id}` - Filter by merchant (admin only)
- `?active={true|false}` - Filter by active status

**Example requests**:
```bash
# Get all outbound numbers
GET /agent/voice/breeze-buddy/numbers

# Get numbers for order-confirmation template
GET /agent/voice/breeze-buddy/numbers?template=order-confirmation

# Add number for any template
POST /agent/voice/breeze-buddy/numbers
{
  "phone_number": "+1234567890",
  "template": "appointment-reminder",
  "merchant_id": "shop_123"
}
```

**File**: `/app/api/routers/breeze_buddy/outbound_numbers.py` (MODIFY)

**Tasks**:
- [ ] Update endpoint paths (remove template-specific parts)
- [ ] Consolidate dashboard endpoint into main GET endpoint
- [ ] Add query parameter filtering for template/merchant
- [ ] Add GET by ID endpoint
- [ ] Add PUT endpoint for updates
- [ ] Ensure numbers can be used across all templates
- [ ] Update router prefix
- [ ] Update API documentation

**Effort**: 3 hours (increased due to filtering and PUT endpoint)

#### 2.1.5 Template Endpoints

**Current**:
- `GET /agent/voice/breeze-buddy/template`
- `POST /agent/voice/breeze-buddy/template`

**New**:
- `GET /agent/voice/breeze-buddy/templates`
- `POST /agent/voice/breeze-buddy/templates`
- `GET /agent/voice/breeze-buddy/templates/{template_id}`
- `PUT /agent/voice/breeze-buddy/templates/{template_id}`
- `DELETE /agent/voice/breeze-buddy/templates/{template_id}`

**File**: `/app/api/routers/breeze_buddy/template.py` (MODIFY)

**Tasks**:
- [ ] **KEEP OLD ENDPOINTS** `/template` (GET, POST) with deprecation warnings
- [ ] **ADD NEW ENDPOINTS** `/templates` (plural) alongside old ones
- [ ] Add GET by ID endpoint (`/templates/{id}`)
- [ ] Add PUT endpoint for updates (`/templates/{id}`)
- [ ] Add DELETE endpoint (`/templates/{id}`)
- [ ] Add merchant filtering
- [ ] Update API documentation

**Deprecation Strategy**: Both old (`/template`) and new (`/templates`) endpoints will coexist

**Effort**: 3 hours

#### 2.1.6 Leads Endpoints

**Current**:
- `GET /agent/voice/breeze-buddy/lead/{lead_id}`
- `POST /agent/voice/breeze-buddy/{merchant}/{template}` (trigger)
- `POST /agent/voice/breeze-buddy/push/lead/v2`

**New**:
- `GET /agent/voice/breeze-buddy/leads/{lead_id}`
- `POST /agent/voice/breeze-buddy/leads`
- `POST /agent/voice/breeze-buddy/leads/{lead_id}/trigger`

**File**: `/app/api/routers/breeze_buddy/leads.py` (MODIFY)

**Tasks**:
- [ ] **KEEP OLD ENDPOINTS** `/lead/{id}` (GET), `/push/lead/v2` (POST) with deprecation warnings
- [ ] **ADD NEW ENDPOINTS** `/leads` (plural) alongside old ones
- [ ] Update GET endpoint path to `/leads/{id}`
- [ ] Consolidate POST endpoints to `/leads`
- [ ] Add trigger as sub-resource (`/leads/{id}/trigger`)
- [ ] Add merchant filtering
- [ ] ⚠️ **DO NOT CHANGE** `POST /{merchant}/{template}` - Needs review first
- [ ] Update API documentation

**Deprecation Strategy**: Both old and new endpoints will coexist
**Important**: `POST /{merchant}/{template}` trigger endpoint requires review before making any changes

**Effort**: 3 hours

### 2.2 Update Router Registration

**File**: `/app/api/routers/breeze_buddy/__init__.py` (MODIFY)

**Changes**:
1. Import new `analytics` router
2. Rename `call_execution_config` to `configurations`
3. Update router prefixes
4. Remove dashboard router (HTML endpoints moved to new repo)

**New structure**:
```python
from fastapi import APIRouter

from . import (
    auth,           # /auth
    analytics,      # /analytics (NEW)
    configurations, # /configurations (RENAMED)
    outbound_numbers,  # /numbers
    template,       # /templates
    leads,          # /leads
    callbacks,      # /callbacks (keep as-is)
    websocket,      # /websocket (keep as-is)
)

router = APIRouter()

router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
router.include_router(analytics.router, prefix="/analytics", tags=["Analytics"])
router.include_router(configurations.router, prefix="/configurations", tags=["Configurations"])
router.include_router(outbound_numbers.router, prefix="/numbers", tags=["Outbound Numbers"])
router.include_router(template.router, prefix="/templates", tags=["Templates"])
router.include_router(leads.router, prefix="/leads", tags=["Leads"])
router.include_router(callbacks.router, prefix="/callbacks", tags=["Callbacks"])
router.include_router(websocket.router, tags=["WebSocket"])
```

**Tasks**:
- [ ] Update router imports
- [ ] Update router prefixes
- [ ] Update tags for better API docs
- [ ] Verify all routes are registered
- [ ] Test API documentation generation

**Effort**: 2 hours

### 2.3 Backward Compatibility (Optional)

If existing clients depend on old endpoints, create redirect endpoints.

**File**: `/app/api/routers/breeze_buddy/legacy.py` (NEW)

**Tasks**:
- [ ] Create legacy router
- [ ] Add redirects for old endpoints
- [ ] Add deprecation warnings
- [ ] Document migration timeline
- [ ] Add to main router

**Effort**: 2 hours (if needed)

### 2.4 API Documentation Update

**Tasks**:
- [ ] Update OpenAPI/Swagger documentation
- [ ] Add endpoint descriptions
- [ ] Add request/response examples
- [ ] Add authentication documentation
- [ ] Add RBAC documentation
- [ ] Create API migration guide

**Effort**: 4 hours

---

## 🧹 Phase 3: Static Pages Cleanup

Remove existing static HTML pages as frontend moves to separate repository.

### 3.1 Identify Static Pages

**Current static pages**:
1. `/static/home.html` - Root page ("App is UP")
2. `/app/ai/voice/agents/breeze_buddy/dashboard/index.html` - Dashboard
3. `/app/ai/voice/agents/breeze_buddy/dashboard/login.html` - Login page

**Endpoints serving these pages**:
1. `GET /` - Serves home.html
2. `GET /agent/voice/breeze-buddy/login` - Serves login.html
3. `GET /agent/voice/breeze-buddy/dashboard` - Serves dashboard index.html

### 3.2 Remove Dashboard HTML Endpoints

**File**: `/app/api/routers/breeze_buddy/dashboard.py` (MODIFY or DELETE)

**Current endpoints to remove**:
- `GET /agent/voice/breeze-buddy/login` (HTML page)
- `GET /agent/voice/breeze-buddy/dashboard` (HTML page)

**Tasks**:
- [ ] Remove login HTML endpoint (keep POST /auth/login)
- [ ] Remove dashboard HTML endpoint
- [ ] Move analytics endpoints to new analytics.py router
- [ ] Delete dashboard.py if empty, or rename to analytics.py

**Effort**: 1 hour

### 3.3 Update Root Endpoint

**File**: `/app/main.py` (MODIFY)

**Current**: `GET /` serves `/static/home.html`

**Options**:
1. **Keep as health check**: Return JSON with app status
2. **Redirect to docs**: Redirect to `/docs` (Swagger UI)
3. **Remove entirely**: Return 404

**Recommended**: Keep as JSON health check

```python
@app.get("/")
async def root():
    """Root endpoint - API health check"""
    return {
        "service": "clairvoyance",
        "status": "running",
        "version": __version__,
        "docs": "/docs",
        "health": "/health"
    }
```

**Tasks**:
- [ ] Update root endpoint to return JSON
- [ ] Remove FileResponse import if not used elsewhere
- [ ] Update API documentation
- [ ] Test root endpoint

**Effort**: 0.5 hours

### 3.4 Remove Static HTML Files

**Files to delete**:
- `/static/home.html`
- `/app/ai/voice/agents/breeze_buddy/dashboard/index.html`
- `/app/ai/voice/agents/breeze_buddy/dashboard/login.html`

**Tasks**:
- [ ] Create backup of HTML files (in case needed for reference)
- [ ] Delete home.html
- [ ] Delete login.html
- [ ] Delete dashboard index.html
- [ ] Check if `/static/` directory has other files
- [ ] Remove `/static/` directory if empty
- [ ] Update `.gitignore` if needed

**Effort**: 0.5 hours

### 3.5 Remove Static File Mounting

**File**: `/app/main.py` (MODIFY)

**Current**: App mounts `/static` directory

```python
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
```

**Tasks**:
- [ ] Remove StaticFiles import from starlette
- [ ] Remove static directory mounting
- [ ] Remove STATIC_DIR constant
- [ ] Verify no other code references static files
- [ ] Test application startup

**Effort**: 0.5 hours

### 3.6 Update Documentation

**Tasks**:
- [ ] Update README to remove references to static pages
- [ ] Document that frontend is in separate repository
- [ ] Add link to frontend repository (once created)
- [ ] Update deployment documentation

**Effort**: 1 hour

---

## 🧪 Phase 4: Testing & Deployment

### 4.1 Unit Testing

**Test files to create/update**:

1. **Authentication Tests**
   - File: `/tests/test_auth.py`
   - Tests:
     - [ ] Password hashing and verification
     - [ ] Token generation with RBAC fields
     - [ ] Token validation
     - [ ] Login endpoint with valid credentials
     - [ ] Login endpoint with invalid credentials
     - [ ] Rate limiting on login

2. **Authorization Tests**
   - File: `/tests/test_rbac.py`
   - Tests:
     - [ ] Permission checking
     - [ ] Role checking
     - [ ] Merchant filtering
     - [ ] Admin access to all resources
     - [ ] Merchant access to own resources only

3. **API Tests**
   - File: `/tests/test_api_endpoints.py`
   - Tests:
     - [ ] All new endpoint paths
     - [ ] Request/response formats
     - [ ] Error handling
     - [ ] Permission enforcement

**Effort**: 12 hours

### 4.2 Integration Testing

**Test scenarios**:
- [ ] End-to-end login flow
- [ ] End-to-end analytics access (admin)
- [ ] End-to-end analytics access (merchant)
- [ ] Configuration management (admin)
- [ ] Configuration viewing (merchant)
- [ ] Token expiry and renewal
- [ ] Cross-domain API calls (if applicable)

**Effort**: 8 hours

### 4.3 Security Testing

**Security checks**:
- [ ] JWT signature validation
- [ ] Token expiration enforcement
- [ ] HTTPS enforcement (production)
- [ ] CORS configuration
- [ ] Rate limiting effectiveness
- [ ] SQL injection prevention
- [ ] XSS prevention
- [ ] merchant_id enforcement (cannot be bypassed via params)

**Tools**:
- OWASP ZAP
- Manual penetration testing
- Security audit checklist

**Effort**: 6 hours

### 4.4 Staging Deployment

**Tasks**:
- [ ] Deploy database migrations to staging
- [ ] Deploy backend to staging
- [ ] Run smoke tests
- [ ] Test with staging frontend (if available)
- [ ] Monitor error logs
- [ ] Performance testing
- [ ] Load testing

**Effort**: 4 hours

### 4.5 Production Deployment

**Pre-deployment checklist**:
- [ ] All tests passing
- [ ] Security audit complete
- [ ] Documentation updated
- [ ] Migration scripts tested
- [ ] Rollback plan prepared
- [ ] Monitoring configured
- [ ] Alerts configured

**Deployment steps**:
1. [ ] Database migration (users table)
2. [ ] Deploy backend with new auth system
3. [ ] Monitor authentication metrics
4. [ ] Verify API endpoints working
5. [ ] Check error rates
6. [ ] Monitor performance

**Post-deployment**:
- [ ] Monitor for 24 hours
- [ ] Check authentication success rate
- [ ] Verify no errors in logs
- [ ] Test all critical flows
- [ ] Update status page

**Effort**: 6 hours

### 4.6 Documentation

**Documentation to create/update**:
- [ ] API Documentation (OpenAPI/Swagger)
- [ ] Authentication Guide
- [ ] RBAC Guide
- [ ] Migration Guide (for existing clients)
- [ ] Deployment Guide
- [ ] Troubleshooting Guide

**Effort**: 6 hours

---

## ⏱️ Timeline & Effort Estimates

### Summary Table

| Phase | Tasks | Effort (hours) | Days (8h/day) |
|-------|-------|----------------|---------------|
| **Phase 1: Token Authentication** | | | |
| 1.1 Database Changes | 2 | 5 | 0.6 |
| 1.2 Backend Auth Changes | 5 | 16 | 2.0 |
| 1.3 Password Hashing | 2 | 2.5 | 0.3 |
| 1.4 Endpoint Protection | 3 | 9 | 1.1 |
| 1.5 Testing | - | 8 | 1.0 |
| **Phase 1 Subtotal** | **12** | **40.5** | **5.0** |
| | | | |
| **Phase 2: API Reorganization** | | | |
| 2.1 New API Structure | 7 | 23 | 2.9 |
| 2.2 Router Registration | 1 | 2 | 0.3 |
| 2.3 Backward Compatibility | 1 | 2 | 0.3 |
| 2.4 API Documentation | 1 | 4 | 0.5 |
| **Phase 2 Subtotal** | **10** | **31** | **3.9** |
| | | | |
| **Phase 3: Static Pages Cleanup** | | | |
| 3.1-3.5 Remove Static Pages | 5 | 3.5 | 0.4 |
| 3.6 Documentation | 1 | 1 | 0.1 |
| **Phase 3 Subtotal** | **6** | **4.5** | **0.6** |
| | | | |
| **Phase 4: Testing & Deployment** | | | |
| 4.1 Unit Testing | 3 | 12 | 1.5 |
| 4.2 Integration Testing | 1 | 8 | 1.0 |
| 4.3 Security Testing | 1 | 6 | 0.8 |
| 4.4 Staging Deployment | 1 | 4 | 0.5 |
| 4.5 Production Deployment | 1 | 6 | 0.8 |
| 4.6 Documentation | 1 | 6 | 0.8 |
| **Phase 4 Subtotal** | **8** | **42** | **5.3** |
| | | | |
| **TOTAL** | **36** | **118** | **14.8** |

### Recommended Timeline

**Conservative Estimate**: 3-4 weeks (1 developer)

**Week 1**: Phase 1 (Token Authentication)
- Days 1-2: Database changes + backend auth
- Days 3-4: Password hashing + endpoint protection
- Day 5: Testing

**Week 2**: Phase 2 (API Reorganization)
- Days 1-3: New API structure + router updates
- Day 4: Backward compatibility + documentation
- Day 5: Testing

**Week 3**: Phase 3 + Testing
- Day 1: Static pages cleanup
- Days 2-3: Unit + integration testing
- Days 4-5: Security testing

**Week 4**: Deployment
- Days 1-2: Staging deployment + testing
- Days 3-4: Production deployment + monitoring
- Day 5: Documentation + cleanup

### Parallel Work Opportunities

If multiple developers are available:

**Developer 1** (Backend):
- Phase 1: Token authentication (Week 1)
- Phase 2: API reorganization (Week 2)

**Developer 2** (Testing/DevOps):
- Week 1-2: Write tests while dev 1 implements
- Week 3: Security testing
- Week 4: Deployment

**This reduces timeline to ~2-3 weeks with 2 developers**

---

## 🎯 Success Criteria

After implementation, verify:

✅ **Authentication**
- Users can login with username/password
- JWT tokens are returned with RBAC fields
- Tokens work across all protected endpoints
- Expired tokens are rejected

✅ **Authorization**
- Admins can access all resources
- Merchants can only access own resources
- Permission checks prevent unauthorized access
- merchant_id filtering works correctly

✅ **API Organization**
- All endpoints follow new structure
- API documentation is clear and accurate
- Backward compatibility works (if implemented)
- No broken endpoints

✅ **Static Pages**
- No HTML files served from backend
- Root endpoint returns JSON
- Frontend repository is separate
- Documentation is updated

✅ **Security**
- Passwords are hashed with bcrypt
- JWT signatures are validated
- HTTPS is enforced in production
- Rate limiting prevents brute force
- No SQL injection vulnerabilities
- merchant_id cannot be spoofed

✅ **Performance**
- Login completes in <1 second
- Token validation is <10ms
- API response times unchanged
- No performance degradation

---

## 🔄 Rollback Plan

If critical issues occur:

### Immediate Rollback (5 minutes)
1. Revert backend deployment to previous version
2. Keep database changes (backward compatible)
3. Notify team and users

### Partial Rollback (per phase)

**Phase 1 rollback**:
- Revert to session-based auth
- Keep database migrations (no impact)

**Phase 2 rollback**:
- Revert router changes
- Old endpoints still work

**Phase 3 rollback**:
- Re-add static file mounting
- Restore HTML files from backup

### Investigation & Hotfix
1. Review error logs
2. Identify root cause
3. Test fix in staging
4. Deploy hotfix
5. Monitor for 24 hours

---

## 📞 Support & Maintenance

### Monitoring

**Metrics to track**:
- Login success rate (target: >99%)
- Authentication errors (target: <1%)
- API response times
- 401/403 error rates
- Token generation time
- Database query performance

### Alerts

**Set up alerts for**:
- Login success rate drops below 95%
- Authentication error rate exceeds 5%
- API response time exceeds 500ms
- Database connection failures
- High rate of 401/403 errors

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Users logged out unexpectedly | Token expired | Increase token lifetime or implement refresh |
| 401 errors on API | Invalid/expired token | User must re-login |
| 403 errors | Insufficient permissions | Check user role and permissions |
| Login failures | Wrong password or rate limit | Check credentials, wait if rate limited |
| Merchant sees no data | merchant_ids or shop_identifiers mismatch | Verify token merchant_ids and shop_identifiers match data |

---

## 📚 Additional Notes

### Dependencies to Add

```txt
# Authentication
bcrypt==4.1.2
```

### Environment Variables to Add

```bash
# JWT Configuration
JWT_SECRET_KEY=<existing>
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_HOURS=24

# RBAC Configuration
ENABLE_RBAC=true

# Rate Limiting
LOGIN_RATE_LIMIT_ATTEMPTS=5
LOGIN_RATE_LIMIT_WINDOW_SECONDS=300
```

### Database Migrations

Store all migrations in: `/app/database/migrations/`

Naming convention: `{number}_{description}.sql`

Example:
- `001_create_users_table.sql`
- `001_rollback_users_table.sql`
- `002_add_user_indexes.sql`

---

**End of Implementation Plan**

This document should be updated as implementation progresses. Track completed tasks and update effort estimates based on actual time spent.
