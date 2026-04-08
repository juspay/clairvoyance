# Merchant & User Account Management - Complete Guide

## Table of Contents
1. [Quick Start](#quick-start)
2. [Overview](#overview)
3. [Architecture](#architecture)
4. [Implementation Details](#implementation-details)
5. [API Reference](#api-reference)
6. [Security & RBAC](#security--rbac)
7. [Migration & Deployment](#migration--deployment)
8. [Testing](#testing)
9. [Troubleshooting](#troubleshooting)

---

## Quick Start

### What Changed?
✅ **Separated business entities from login accounts**
- `merchants` table → Business entities (companies like "RedBus")
- `users` table → Login accounts (credentials with roles)

✅ **Role update**
- `UserRole.SHOP` → `UserRole.USER`
- Existing data automatically migrated

✅ **Separate schemas per resource**
- `/merchants` endpoints use `MerchantCreate`/`MerchantResponse` schemas (business entity fields)
- `/users` endpoints use `UserCreate`/`UserResponse` schemas (login account fields)
- Each resource has its own dedicated schema classes

✅ **Reseller ownership**
- Merchant entities track which reseller owns them via `reseller_id`
- User accounts track who created them via `owner_id`

✅ **Wildcard resolution**
- `merchant_ids: ["*"]` is resolved contextually per role (see RBAC section)

### Key Endpoints

All endpoints are under the prefix `/agent/voice/breeze-buddy`.

| Resource | Endpoint | Method | Access |
|----------|----------|--------|--------|
| **Merchant Entities** | `/merchant` | POST | Admin/Reseller |
| | `/merchants` | GET | All roles (scoped) |
| | `/merchant/{merchant_id}` | GET | All roles (scoped) |
| | `/merchant/{merchant_id}` | PUT | Admin / Reseller (own only) |
| | `/merchant/{merchant_id}` | DELETE | Admin / Reseller (own only) |
| **User Accounts** | `/user` | POST | RBAC-based |
| | `/users` | GET | RBAC-filtered |
| | `/user/{user_id}` | GET/PUT/DELETE | RBAC-based |

> **⚠️ Admin accounts cannot be deleted by anyone** — including admins themselves.

---

## Overview

### Problem Statement
Previously, the system mixed two concepts:
- **Business entities** (companies/merchants like "RedBus", "Uber")
- **Login accounts** (user credentials with roles)

This caused confusion and made access control difficult.

### Solution
**Separation of Concerns** - Two distinct tables:

```
merchants table                     users table
├─ merchant_id VARCHAR(255) (PK)    ├─ id (UUID, PK)
├─ name VARCHAR(255)  [nullable]    ├─ username (unique)
├─ description TEXT                 ├─ password_hash
├─ is_active BOOLEAN                ├─ role (admin/reseller/merchant/user)
├─ reseller_id VARCHAR(255)         ├─ reseller_ids (JSONB array)
├─ created_at TIMESTAMPTZ           ├─ merchant_ids (JSONB array)
└─ updated_at TIMESTAMPTZ           ├─ owner_id VARCHAR(255)
                                    ├─ created_at TIMESTAMPTZ
                                    └─ updated_at TIMESTAMPTZ
```

**Key Concepts**:
- `merchant_id` (e.g., "redbus", "nvidia") is the **primary key** — human-readable business identifier (no separate UUID)
- `reseller_id` on merchants table tracks which reseller owns the merchant entity
- `owner_id` on users table tracks who created each user account — `VARCHAR(255)` (not a UUID foreign key) to support both UUID user IDs and legacy string IDs
- `reseller_ids` on users table — retained for backward compatibility; does not drive effective access scope
- `merchant_ids` on users table — drives effective access scope for all non-admin roles

---

## Architecture

### Architecture Principles

#### 1. **Ownership Tracking**
- **Merchant entities** track their reseller via `reseller_id`:
  - Admin creates merchant → `reseller_id` can be set to any reseller's ID (or null)
  - Reseller creates merchant → `reseller_id` auto-set to the reseller's user ID
- **User accounts** track their creator via `owner_id`:
  - When any user creates an account → `owner_id` = creator's user ID
  - Benefits: Audit trail, accountability, ownership-based access control

#### 2. **RBAC (Role-Based Access Control)**
Four roles with hierarchical permissions:

```
Admin (Full Access)
  ↓
Reseller (Scoped Access)
  ↓
Merchant (Limited Scope)
  ↓
User (Read-only)
```

#### 3. **Wildcard Resolution**
`merchant_ids: ["*"]` means different things per role:
- **Admin**: Always unrestricted (wildcard not needed)
- **Reseller with `["*"]`**: Access to only merchants they own (where `merchants.reseller_id = reseller.id`)
- **Merchant/User with `["*"]`**: Access to all merchants their owner (reseller) has access to

Resolution is handled by `resolve_merchant_ids()` in `app/core/security/scope.py`.

#### 4. **Immutability**
Certain fields cannot be changed after creation:
- `merchant_id` (business identifier)
- `username` (login name)
- `role` (user role)

Rationale: Prevents identity confusion and maintains referential integrity.

#### 5. **Delete Rules**
- **Merchant entities**: Hard delete available via `DELETE /merchant/{merchant_id}`
  - Admin: Can delete any merchant entity
  - Reseller: Can only delete merchant entities they own (`reseller_id` match)
  - Merchant/User: Cannot delete merchant entities
  - Can also use `is_active=false` to soft-deactivate (via PUT endpoint)
- **User accounts**: Hard delete available with RBAC checks
  - **Admin accounts can NEVER be deleted by anyone** (including admins themselves)
  - Cannot delete your own account (self-delete blocked)

### Database Schema

#### Migration 019: Merchants Table

##### 1. `merchants` Table
```sql
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id VARCHAR(255) PRIMARY KEY,       -- Business identifier ("redbus") — IS the PK
    name VARCHAR(255),                          -- Optional display name
    description TEXT,                           -- Optional description
    is_active BOOLEAN DEFAULT true,             -- Active status
    reseller_id VARCHAR(255),                   -- Which reseller owns this merchant
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
);
```

> **Note**: `merchant_id` is the primary key — there is no separate UUID `id` column.
> `name` is nullable (optional). `reseller_id` references users but is VARCHAR (not FK).

**Indexes**:
- `reseller_id` — Filter by owning reseller
- `is_active` — Filter active merchants
- `created_at` — Sort by date

##### 2. `users` Table (existing, with additions)
```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_users_owner_id ON users(owner_id);

-- Role constraint updated
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
UPDATE users SET role = 'user' WHERE role = 'shop';
ALTER TABLE users ADD CONSTRAINT users_role_check 
    CHECK (role IN ('admin', 'reseller', 'merchant', 'user'));
```

##### 3. Relationships

```
┌─────────────────────────────────────────┐
│          merchants table                │
│  merchant_id (PK), name, reseller_id    │
└─────────────────────────────────────────┘
              ↑ reseller_id (VARCHAR)
              │ (references a reseller user's ID)
              │
┌─────────────────────────────────────────┐
│            users table                  │
│  id (UUID PK), username, role,          │
│  merchant_ids, owner_id (VARCHAR)       │
└─────────────────────────────────────────┘
```

### Data Flow Examples

#### Scenario 1: Admin Creates Merchant Entity

```
┌──────────┐
│  Admin   │ (id: user-uuid-123)
└─────┬────┘
      │ POST /merchant { merchant_id: "redbus" }
      ↓
┌──────────────────────────────────────────┐
│  create_merchant_handler()        │
│  1. Check: admin role? ✓                 │
│  2. Validate: merchant_id unique? ✓      │
│  3. Admin can optionally set reseller_id │
└─────────────────┬────────────────────────┘
                  ↓
┌──────────────────────────────────────────┐
│  Database INSERT                         │
│  merchants (                             │
│    merchant_id: "redbus",                │
│    reseller_id: null or specified         │
│  )                                       │
└──────────────────────────────────────────┘
```

#### Scenario 2: Reseller Creates Merchant Entity

```
┌───────────┐
│ Reseller  │ (id: reseller-uuid-456)
└─────┬─────┘
      │ POST /merchant { merchant_id: "fastbus" }
      ↓
┌──────────────────────────────────────────┐
│  create_merchant_handler()        │
│  1. Check: reseller role? ✓              │
│  2. reseller_id auto-set to reseller's   │
│     user ID                              │
└─────────────────┬────────────────────────┘
                  ↓
┌──────────────────────────────────────────┐
│  Database INSERT                         │
│  merchants (                             │
│    merchant_id: "fastbus",               │
│    reseller_id: reseller-uuid-456 ←auto  │
│  )                                       │
└──────────────────────────────────────────┘
```

#### Scenario 3: Reseller Creates User Account

```
┌───────────┐
│ Reseller  │ (merchant_ids: ["redbus", "uber"])
└─────┬─────┘
      │ POST /user { username: "john", role: "user", merchant_ids: ["redbus"] }
      ↓
┌──────────────────────────────────────────┐
│  create_user_handler()                   │
│  1. Check: reseller creating user? ✓     │
│  2. resolve_merchant_ids() on reseller   │
│  3. validate_merchant_ids_subset() ✓     │
│  4. Check: username unique? ✓            │
└─────────────────┬────────────────────────┘
                  ↓
┌──────────────────────────────────────────┐
│  Database INSERT                         │
│  users (                                 │
│    username: "john",                     │
│    owner_id: reseller-uuid ←── tracked!  │
│  )                                       │
└──────────────────────────────────────────┘
```

#### Scenario 4: Merchant Lists Users (RBAC Filtering)

```
┌──────────┐
│ Merchant │ (merchant_ids: ["redbus"])
└─────┬────┘
      │ GET /users
      ↓
┌──────────────────────────────────────────┐
│  Database Query with RBAC                │
│  SELECT * FROM users                     │
│  WHERE merchant_ids::jsonb ?| ['redbus'] │
│        ↑ JSONB operator checks overlap   │
└─────────────────┬────────────────────────┘
                  ↓
         Returns only users with
         "redbus" in their merchant_ids
```

**JSONB Operator `?|`**: "Does the JSONB array contain any of these values?"
- `merchant_ids = ["redbus", "uber"]` ?| `["redbus"]` → TRUE
- `merchant_ids = ["swiggy"]` ?| `["redbus"]` → FALSE

---

## Implementation Details

### File Structure

```
app/
├── utils/
│   └── common.py                 # is_valid_uuid, is_valid_merchant_id, parse_json_field + other helpers
├── core/security/
│   └── scope.py          # RBAC helpers: resolve_merchant_ids, validate_merchant_ids_subset
├── database/
│   ├── migrations/
│   │   └── 019_create_merchants_table.sql
│   ├── accessor/breeze_buddy/
│   │   ├── __init__.py            # Re-exports decode_merchant, decode_user
│   │   ├── merchants.py   # decode_merchant
│   │   └── users.py              # decode_user
│   └── queries/breeze_buddy/
│       ├── __init__.py            # Package init
│       ├── merchants.py   # Merchant entity CRUD (DB-level filtering & pagination)
│       └── users.py              # User + merchant account CRUD
├── schemas/breeze_buddy/
│   ├── auth.py                   # UserRole, UserInfo, UserInDB
│   ├── merchants.py      # MerchantCreate/Update/Response
│   └── users.py                  # UserCreate/Update/Response (unified)
└── api/
    ├── security/breeze_buddy/
    │   └── rbac_token.py         # JWT + RBAC token management
    └── routers/breeze_buddy/
        ├── merchants/    # Business entity CRUD
        │   ├── __init__.py
        │   └── handlers.py
        └── users/        # User account CRUD (RBAC-based)
            ├── __init__.py
            └── handlers.py
```

### Code Architecture Layers

```
┌─────────────────────────────────────┐
│  API Layer (FastAPI Routers)       │ ← HTTP endpoints
│  - Validates JWT tokens             │
│  - Calls handlers                   │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│  Handler Layer (Business Logic)     │ ← RBAC enforcement
│  - Permission checks                │
│  - resolve_merchant_ids() for       │
│    wildcard resolution              │
│  - Calls database queries           │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│  Database Query Layer               │ ← Data access
│  - SQL construction                 │
│  - DB-level filtering & pagination  │
│  - JSONB operations                 │
│  - Connection management            │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│  Accessor Layer (Response Builders) │ ← Row → Pydantic
│  - decode_user()            │
│  - decode_merchant() │
│  - Imports utils from common.py     │
└─────────────────────────────────────┘
```

**Layer rules**:
- Accessors only transform DB rows into Pydantic models — no DB queries, no business logic
- Query layer imports accessors for row transformation and utils from `app.utils.common` directly
- Handlers never import accessors directly — they call query functions which return typed responses

### Key Components

#### Schemas (`app/schemas/breeze_buddy/`)

**merchants.py** — Business entities:
```python
class MerchantCreate(BaseModel):
    merchant_id: str   # min=3, max=100, Required
    name: Optional[str] = None    # max=255, Optional
    description: Optional[str] = None
    is_active: Optional[bool] = True
    reseller_id: Optional[str] = None  # Admin can set; auto-set for resellers

class MerchantUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    reseller_id: Optional[str] = None  # Only admin can change

class MerchantResponse(BaseModel):
    merchant_id: str              # PK — no separate UUID id
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    reseller_id: Optional[str] = None  # Which reseller owns this
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
```

**users.py** — Unified schemas for all user/merchant accounts:
```python
class UserCreate(BaseModel):
    id: str                     # min=1, max=255, Required
    username: str
    password: str
    email: Optional[str] = None
    role: UserRole              # admin, reseller, merchant, user
    reseller_ids: List[str] = []      # Retained for backward compatibility only
    merchant_ids: List[str] = []  # Drives effective access scope for all non-admin roles
    is_active: bool = True

class UserUpdate(BaseModel):
    password: Optional[str] = None
    email: Optional[str] = None
    reseller_ids: Optional[List[str]] = None
    merchant_ids: Optional[List[str]] = None
    is_active: Optional[bool] = None

class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str] = None
    role: UserRole
    reseller_ids: List[str] = []      # Retained for backward compatibility only
    merchant_ids: List[str] = []  # Drives effective access scope
    is_active: bool = True
    owner_id: Optional[str] = None  # Who created this account
    ...

class UserListResponse(BaseModel):
    users: List[UserResponse]
    total: int
    page: int = 1
    limit: int = 50
    total_pages: int = 1
```

#### Authorization Helpers (`app/core/security/scope.py`)

```python
async def resolve_merchant_ids(user: UserInfo) -> Optional[List[str]]:
    """Resolve wildcard ["*"] merchant_ids based on role.
    
    Returns None for unrestricted access, or a specific list of IDs.
    - Admin → None (always unrestricted)
    - Reseller → always queries owned merchants from DB
      (merchants WHERE reseller_id = reseller.id)
    - Merchant/User with ["*"] → looks up owner's merchant_ids
    - Merchant/User with specific IDs → those IDs
    """

def validate_merchant_ids_subset(
    requested_ids: List[str],
    allowed_ids: Optional[List[str]],
    error_message: str
) -> None:
    """Raises 403 if requested_ids is not a subset of allowed_ids.
    allowed_ids=None means unrestricted."""
```

#### Database Accessors

```python
# decode_merchant — merchants table
def decode_merchant(row) -> MerchantResponse:
    return MerchantResponse(
        merchant_id=row["merchant_id"],
        name=row.get("name"),         # nullable
        reseller_id=str(row["reseller_id"]) if row.get("reseller_id") else None,
        ...
    )

# decode_user — users table (used by both /users and /merchants)
def decode_user(row) -> UserResponse:
    return UserResponse(
        id=str(row["id"]),
        role=UserRole(row["role"]),
        merchant_ids=parse_json_field(row["merchant_ids"]),
        owner_id=str(row["owner_id"]) if row.get("owner_id") else None,
        ...
    )
```

---

## API Reference

### Merchant Entity Endpoints

#### POST /merchant (Create Merchant Entity)
**Access**: Admin or Reseller

```bash
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/merchant \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "redbus",
    "name": "RedBus India",
    "description": "Bus booking platform",
    "is_active": true
  }'
```

**Response** (201 Created):
```json
{
  "merchant_id": "redbus",
  "name": "RedBus India",
  "description": "Bus booking platform",
  "is_active": true,
  "reseller_id": "reseller-uuid-or-null",
  "created_at": "2026-02-04T10:00:00Z",
  "updated_at": "2026-02-04T10:00:00Z"
}
```

> **Note**: Response has no `id` field — `merchant_id` is the primary key.
> For resellers, `reseller_id` is auto-set to their user ID.
> For admins, `reseller_id` can be explicitly provided or left null.

**Validation**:
- `merchant_id`: 3-100 chars
- Must be unique across merchants table

#### GET /merchants (List Merchant Entities)
**Access**: All roles (scoped by merchant_ids)
- Admin: sees all
- Reseller: sees merchants where `reseller_id` matches or `merchant_id` is in their scope
- Merchant/User: sees only merchants matching their `merchant_ids`

**Query Parameters**:
- `page` (default: 1) - Page number
- `limit` (default: 50, max: 100) - Items per page
- `merchant_id` - Filter by merchant_id (partial match, case-insensitive)
- `name` - Filter by name (partial match, case-insensitive)
- `is_active` - Filter by active status (true/false)
- `sort_by` - Sort field (merchant_id | name | created_at | updated_at)
- `sort_order` - Sort direction (asc | desc)

#### PUT /merchant/{merchant_id} (Update Merchant Entity)
**Access**: Admin (any) / Reseller (only merchants they own — `reseller_id` match)

**Updatable fields**:
- `name`: Merchant display name
- `description`: Optional description text
- `is_active`: Active status (true/false)
- `reseller_id`: Reassign ownership (admin only)

**Restrictions**: `merchant_id` cannot be changed (immutable)

#### DELETE /merchant/{merchant_id} (Delete Merchant Entity)
**Access**: Admin (any) / Reseller (only merchants they own — `reseller_id` match)

```bash
curl -X DELETE http://localhost:8000/agent/voice/breeze-buddy/merchant/redbus \
  -H "Authorization: Bearer $TOKEN"
```

**Response** (200):
```json
{
  "success": true,
  "message": "Merchant entity 'redbus' deleted",
  "deleted_id": "redbus"
}
```

---

### User Account Endpoints

#### POST /user (Create User Account)
**Access**: RBAC-based (see matrix below)

```bash
# Create a reseller
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/user \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "new_reseller",
    "password": "secure_password",
    "role": "reseller",
    "reseller_ids": ["BB_SHOPIFY"],
    "merchant_ids": ["*"],
    "is_active": true
  }'

# Create a merchant
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/user \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "new_merchant",
    "password": "secure_password",
    "role": "merchant",
    "reseller_ids": ["BB_SHOPIFY"],
    "merchant_ids": ["shop1", "shop2"],
    "is_active": true
  }'

# Create a user
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/user \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "new_user",
    "password": "secure_password",
    "role": "user",
    "reseller_ids": ["BB_SHOPIFY"],
    "merchant_ids": ["shop1"],
    "is_active": true
  }'
```

**Validation**:
- Username must be unique
- `merchant_ids` required for merchant/user roles (drives effective access scope)
- `reseller_ids` accepted but retained for backward compatibility only
- Password is hashed before storage

#### GET /users (List User Accounts)
**Access**: RBAC-filtered

**Query Parameters**:
- `page`, `limit` - Pagination
- `username` - Filter by username (partial match)
- `role` - Filter by role (admin | reseller | merchant | user)
- `reseller_id` - Filter by reseller_id (checks if in reseller_ids array)
- `merchant_id` - Filter by merchant_id (checks if in merchant_ids array)
- `is_active` - Filter by active status
- `sort_by` - Sort field (username | role | created_at | updated_at)
- `sort_order` - Sort direction (asc | desc)

#### GET /user/{id} (Get User Account)
**Access**: Admin / Reseller/Merchant (if in scope) / Self

#### PUT /user/{id} (Update User Account)
**Access**: RBAC-based

**Restrictions**: `username` and `role` cannot be changed (immutable)

#### DELETE /user/{user_id} (Delete User Account)
**Access**: RBAC-based

**Protections**:
- **Admin accounts can NEVER be deleted by anyone** (including admins themselves) → 403
- Cannot delete your own account (self-delete blocked) → 400
- UUID validation: non-UUID IDs (e.g., `legacy_admin`) return 404 gracefully (no server crash)

---

## Security & RBAC

### Role Capabilities Matrix

| Role     | Create Merchant Entity | Delete Merchant Entity | Create User Account | View Users | Modify Users | Delete Users |
|----------|------------------------|------------------------|---------------------|------------|--------------|--------------|
| Admin    | ✅ Any                 | ✅ Any                 | ✅ Any role         | ✅ All     | ✅ All (not other admins) | ✅ Non-admin only |
| Reseller | ✅ (becomes owner)     | ✅ Own only            | ✅ merchant/user in scope | ✅ Scoped | ✅ Scoped | ✅ Scoped |
| Merchant | ❌                     | ❌                     | ✅ user in scope | ✅ Scoped | ✅ Scoped | ✅ Scoped |
| User     | ❌                     | ❌                     | ❌                  | ❌ (self only) | ❌       | ❌           |

> **Critical**: Admin accounts can **NEVER** be deleted by anyone. This is enforced at both the handler layer and the database query layer.

### Wildcard `["*"]` Resolution

| Role | `merchant_ids` | Resolved Access |
|------|----------------|-----------------|
| Admin | (any) | Unrestricted — no filtering |
| Reseller | `["*"]` | Only merchants they own (`merchants.reseller_id = reseller.id`) |
| Reseller | `["redbus", "uber"]` | Only merchants they own (DB lookup, not raw value) |
| Merchant | `["*"]` | Looks up owner's (reseller's) merchant_ids |
| User | `["*"]` | Looks up owner's merchant_ids |
| Merchant/User | `["redbus"]` | Only "redbus" |

This is implemented by `resolve_merchant_ids()` in `app/core/security/scope.py`.

### RBAC Helper Functions

#### resolve_merchant_ids() (Authorization)
Resolves effective merchant_ids for a user, handling wildcard `["*"]`.

```python
allowed = await resolve_merchant_ids(current_user)
# Returns None (unrestricted, admin only) or List[str] (specific IDs)
# Resellers always get a concrete list from DB (never None)
```

#### validate_merchant_ids_subset() (Authorization)
Validates that requested IDs are within allowed scope.

```python
validate_merchant_ids_subset(
    requested_ids=["redbus"],
    allowed_ids=allowed,  # None = unrestricted
    error_message="Cannot assign merchant_ids outside your scope"
)
# Raises HTTPException 403 if not a subset
```

#### _check_create_access() (User Accounts)
Validates if current user can create a user account with target role.

**Logic**:
1. Admin → allowed for any role/merchant_ids
2. Reseller → allowed for user/merchant roles; uses `resolve_merchant_ids` + `validate_merchant_ids_subset`
3. Merchant → allowed for user role only; uses `resolve_merchant_ids` + `validate_merchant_ids_subset`
4. User → denied

#### _check_update_access() (Merchant Entities)
Validates if current user can update a merchant entity.

**Logic**:
1. Admin → can update any merchant; only admin can change `reseller_id`
2. Reseller → can only update merchants they own (`reseller_id` match)
3. Merchant/User → denied (403)

### Security Best Practices

#### 1. SQL Injection Prevention
✅ **Always use parameterized queries**:
```python
# SAFE
query = "SELECT * FROM users WHERE username = $1"
await conn.fetchrow(query, username)

# UNSAFE - never do this!
query = f"SELECT * FROM users WHERE username = '{username}'"
```

#### 2. Password Security
✅ **Hash passwords before storage**:
```python
from app.core.security.password import hash_password
password_hash = hash_password(plain_password)
```

Never return `password_hash` in API responses.

#### 3. RBAC Enforcement
✅ **Defense in depth** (3 layers):
1. JWT token validation (API layer)
2. Role checks (handler layer)
3. Database-level filtering (query layer)

❌ **Never trust client data**:
```python
# BAD - user could fake this!
owner_id = request_body.get("owner_id")

# GOOD - from verified JWT
owner_id = current_user.id
```

#### 4. Owner Isolation
Each entity tracks its creator → enables:
- Audit logs ("who created this?")
- Accountability
- Future cascade operations

---

## Migration & Deployment

### Step 1: Run Migration

```bash
# Apply migration 019
psql -U <user> -d <database> -f app/database/migrations/019_create_merchants_table.sql
```

**What it does**:
1. Creates `merchants` table (with `reseller_id`, nullable `name`, no `merchant_ids`)
2. Adds `owner_id` column to `users` table
3. Updates role constraint (shop → user)
4. Migrates existing role='shop' to role='user'

### Step 2: Update Code

```bash
# Restart API server
# No environment variable changes needed
```

### Step 3: Test Endpoints

```bash
# 1. Login as admin
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}'

# 2. Create merchant entity
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/merchant \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"merchant_id": "test_merchant"}'

# 3. Verify creation
curl http://localhost:8000/agent/voice/breeze-buddy/merchants \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# 4. Create user account
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/user \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "test_user",
    "password": "secure_pass",
    "role": "user",
    "merchant_ids": ["test_merchant"]
  }'
```

### Breaking Changes

#### ⚠️ Role Name Change
- Old: `UserRole.SHOP = "shop"`
- New: `UserRole.USER = "user"`

**Impact**:
- Update code referencing `UserRole.SHOP` to `UserRole.USER`
- Database migration automatically converts existing data

#### Endpoint Organization

**Merchant Entities API** (Business-level merchant management):
- `POST /merchant` - Create merchant entity (Admin/Reseller)
- `GET /merchants` - List merchant entities (all roles, scoped)
- `GET /merchant/{merchant_id}` - Get merchant by merchant_id
- `PUT /merchant/{merchant_id}` - Update merchant (Admin any, Reseller own only)
- `DELETE /merchant/{merchant_id}` - Delete merchant (Admin any, Reseller own only)
- Tag: `merchants`

**User Accounts API** (Login account management):
- `POST /user` - Create user account
- `GET /users` - List user accounts
- `GET /user/{id}` - Get user account
- `PUT /user/{id}` - Update user account
- `DELETE /user/{id}` - Delete user account
- Tag: `users`

---

## Testing

### Unit Testing

```python
import pytest
from app.api.routers.breeze_buddy.merchants.handlers import (
    _check_create_merchant_access
)

def test_admin_can_create_merchant():
    user = UserInfo(role=UserRole.ADMIN)
    _check_create_merchant_access(user)  # Should not raise

def test_merchant_cannot_create_merchant():
    user = UserInfo(role=UserRole.MERCHANT)
    with pytest.raises(HTTPException) as exc:
        _check_create_merchant_access(user)
    assert exc.value.status_code == 403
```

### Integration Testing

We have **4 comprehensive test suites** under `tests/` (188 total tests, all passing):

| Test File | Tests | What It Covers |
|-----------|-------|----------------|
| `tests/test_merchant_user_api.py` | 39 | Merchant entity & user account CRUD — full/minimal/invalid payloads, pagination, sorting, filtering, legacy endpoints, integration workflow |
| `tests/test_merchant_user_flow.py` | 16 | Realistic end-to-end flow — create merchant → create user → login → profile → RBAC → cleanup |
| `tests/test_rbac_comprehensive.py` | 37 | RBAC hierarchy — admin/reseller/merchant/user role permissions, cross-role access, scope enforcement |
| `tests/test_extended_rbac.py` | 96 | Extended RBAC & validation — payload edge cases, leads/configs/templates/analytics/numbers API RBAC, auth edge cases, admin delete protection |

**Running tests:**
```bash
# All tests require server running at localhost:8000
# Uses real admin credentials from .env

# Run individual suites
python tests/test_merchant_user_api.py
python tests/test_merchant_user_flow.py
python tests/test_rbac_comprehensive.py
python tests/test_extended_rbac.py

# Run all
for f in tests/test_*.py; do python "$f"; done
```

**Test pattern**: All tests use httpx against a live server (not pytest/ASGI mocking). They load `.env` for admin credentials, create test data with unique IDs, and clean up after themselves.

**Manual RBAC testing:**
```bash
# Test RBAC - merchant trying to create admin (should fail)
curl -X POST http://localhost:8000/agent/voice/breeze-buddy/user \
  -H "Authorization: Bearer $MERCHANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin2", "password": "longpassword", "role": "admin"}'
# Expected: 403 Forbidden

# Test scoped access - reseller listing merchants
curl http://localhost:8000/agent/voice/breeze-buddy/merchants \
  -H "Authorization: Bearer $RESELLER_TOKEN"
# Expected: Only merchants in reseller's merchant_ids scope

# Test admin delete protection
curl -X DELETE http://localhost:8000/agent/voice/breeze-buddy/user/{admin_uuid} \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# Expected: 403 "Admin accounts cannot be deleted"
```

---

## Troubleshooting

### Common Issues

#### "Merchant ID already exists"
```
Cause: merchant_id must be unique

Solution: Check existing:
SELECT merchant_id FROM merchants WHERE merchant_id = 'redbus';
```

#### "Username already exists"
```
Cause: Username must be unique

Solution: Check existing:
SELECT username FROM users WHERE username = 'john';
```

#### "Cannot create accounts for merchant_ids outside your scope"
```
Cause: Reseller/merchant trying to create account for inaccessible merchant_id

Solution:
1. Check your merchant_ids: GET /users/me
2. Ensure target merchant_ids ⊆ your merchant_ids
```

#### "Cannot modify other admin accounts"
```
Cause: Admin trying to modify another admin's account

Solution: This is intentional security. Admins can only modify their own admin accounts.
```

#### "Admin accounts cannot be deleted"
```
Cause: Attempting to delete any admin account (including self-delete)

This is enforced at two layers:
1. Handler layer: Returns 403 before even looking up the user in DB
2. Database layer: Raises ValueError if target user has admin role

Solution: Admin accounts are permanently protected. Deactivate instead:
  PUT /user/{id} with { "is_active": false }
```

#### "merchant_ids are required for merchant/user roles"
```
Cause: Creating merchant/user account without merchant_ids

Solution: Always provide merchant_ids:
{
  "role": "user",
  "merchant_ids": ["redbus"]  ← Required
}
```

### Advanced Features

#### Wildcard Access
```python
# Wildcard resolution is role-aware — use resolve_merchant_ids()
allowed = await resolve_merchant_ids(user)
# Returns None (unrestricted, admin only) or a specific list
# Admin → always None
# Reseller → always queries owned merchants from DB (never None)
# Merchant/User with ["*"] → resolves from owner's merchant_ids
```

#### JSONB Operators
```sql
-- Check if merchant_id exists in array
merchant_ids::jsonb ? 'redbus'

-- Check if ANY exist (RBAC)
merchant_ids::jsonb ?| ARRAY['redbus', 'uber']

-- Check if ALL exist
merchant_ids::jsonb ?& ARRAY['redbus', 'uber']
```

#### Pagination Math
```python
offset = (page - 1) * limit
total_pages = (total + limit - 1) // limit  # Ceiling division
```

---

## Summary

### What Was Built

1. **Separation of Concerns**
   - `merchants` table = business entities (with `reseller_id`)
   - `users` table = login accounts (with `owner_id`)
   - Clear distinction prevents confusion

2. **Unified Schemas**
   - Single `UserCreate`/`UserResponse` for all user/merchant account operations
   - No duplicate `MerchantCreate`/`MerchantResponse` classes
   - `/merchants` and `/users` endpoints share the same models

3. **Reseller Ownership**
   - Merchant entities track which reseller owns them via `reseller_id`
   - Resellers auto-own entities they create
   - Only admin can reassign `reseller_id`

4. **Wildcard Resolution**
   - `["*"]` resolved contextually per role via `resolve_merchant_ids()`
   - `validate_merchant_ids_subset()` for scope enforcement
   - Handles owner chain lookups for merchant/user wildcards

5. **Comprehensive RBAC**
   - 4 roles with hierarchical permissions
   - Scope-based filtering (merchant_ids)
   - Defense in depth (API + handler + database)

6. **Production-Ready Features**
   - Pagination and filtering
   - Deactivation support for merchant entities
   - Immutable identifiers
   - JSONB for flexible arrays
   - Proper error handling

### Key Takeaways

- **merchant_id** → Business identifier ("redbus") — PK in merchants table
- **username** → Login account ("john_doe") — unique in users table
- **reseller_id** → Which reseller owns a merchant entity
- **owner_id** → Who created a user account
- **merchant_ids** → Defines user access scope
- **`["*"]`** → Wildcard, resolved by `resolve_merchant_ids()` per role
- **RBAC** → Enforced at every layer

### Implementation Checklist

✅ Migration 019 created (`merchant_id` as PK, `reseller_id`, nullable `name`)  
✅ UserRole enum updated (SHOP → USER)  
✅ RBAC token bug fixed (`"shop"` → `"user"` in permissions)  
✅ Merchant entity schemas created (with `reseller_id`, no `merchant_ids`)  
✅ Unified user schemas (`UserCreate`/`UserResponse` for all account types)  
✅ No duplicate `MerchantCreate`/`MerchantResponse` classes  
✅ Wildcard resolution via `resolve_merchant_ids()` + `validate_merchant_ids_subset()`  
✅ Merchant entity DB queries created (with `reseller_id`)  
✅ User account DB queries updated with RBAC + UUID validation  
✅ Merchant entity API router & handlers created (CRUD + reseller ownership)  
✅ User account API router & handlers created  
✅ Routers registered and schemas exported  
✅ Admin accounts can NEVER be deleted (handler + DB layer protection)  
✅ UUID validation prevents crashes from legacy string IDs  
✅ RBAC consistently enforced  
✅ Owner/reseller tracking implemented  
✅ Utility functions consolidated in `app/utils/common.py` (no separate `validators.py`)  
✅ Clean layer separation: accessors only build responses, queries import utils directly  
✅ DB-level filtering & pagination for all listing endpoints (no in-memory filtering)  

This architecture provides a scalable, secure foundation for multi-tenant merchant and user management.
