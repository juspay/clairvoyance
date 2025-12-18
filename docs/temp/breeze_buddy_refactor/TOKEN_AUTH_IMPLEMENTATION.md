# 🔐 SECURE TOKEN-BASED AUTHENTICATION IMPLEMENTATION GUIDE

## Executive Summary

This document provides a complete implementation guide for migrating from cookie-based authentication to secure token-based authentication using **JWT (JSON Web Tokens)** with **Role-Based Access Control (RBAC)**.

**Key Features:**
- 🔐 Simple JWT-based authentication with Bearer tokens
- 👥 Role-Based Access Control (RBAC) - Admin and Merchant roles
- 🌐 Cross-domain support (works across different domains)
- 📊 Scoped data access (merchants see only their data)
- 🔒 Granular permissions system
- 🚀 Extensible architecture (easy to add new roles)
- ⚡ Simple logout flow - token expires, redirect to login

---

## 📋 Table of Contents

1. [Security Architecture](#security-architecture)
2. [Token Strategy](#token-strategy)
3. [Role-Based Access Control (RBAC)](#role-based-access-control-rbac)
4. [Backend Implementation](#backend-implementation)
5. [Frontend Implementation](#frontend-implementation)
6. [Security Measures](#security-measures)
7. [Migration Plan](#migration-plan)
8. [Testing Checklist](#testing-checklist)

---

## 🛡️ Security Architecture

### Token Storage Strategy (Simple Approach)

We use a **single JWT token** stored in localStorage or cookies for simplicity and cross-domain compatibility:

| Token Type | Storage Location | Purpose | Lifetime | Notes |
|------------|-----------------|---------|----------|-------|
| **JWT Access Token** | localStorage or Cookie | API authentication | Configurable (e.g., 24 hours) | Sent via Authorization header |

### Why This Approach

1. **Simplicity**: Single token, easy to manage and debug
2. **Cross-Domain**: Works seamlessly across different domains
3. **Standard**: Uses industry-standard Bearer token pattern
4. **Flexible Storage**: Can use localStorage or cookies without SameSite restrictions
5. **Clear Expiry**: When token expires, user is logged out and redirected to login
6. **No Refresh Complexity**: No refresh token management or rotation logic

### Trade-offs

**Pros:**
- Simple implementation
- Easy to debug
- Works across domains
- Standard Bearer token approach

**Cons:**
- Token accessible via JavaScript (XSS risk if app is compromised)
- User must re-login when token expires (no automatic refresh)

**Security Note:** This approach prioritizes simplicity and functionality. Ensure your application has proper XSS protection measures (Content Security Policy, input sanitization, etc.)

---

## 🎯 Token Strategy

### Token Structure

#### JWT Access Token (Single Token)

**Admin Example (Access to ALL shops):**
```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "user_id",
    "username": "admin_breeze_buddy",
    "role": "admin",
    "email": "admin@breezelabs.app",
    "merchant_ids": ["*"],
    "shop_identifiers": ["*"],
    "permissions": ["read:all", "write:all", "delete:all"],
    "iat": 1702998378,
    "exp": 1703084778
  },
  "signature": "HMACSHA256(...)"
}
```

**Multi-Shop Merchant Example:**
```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "merchant_user_id",
    "username": "merchant_joe",
    "role": "merchant",
    "email": "joe@joescoffee.com",
    "merchant_ids": ["merchant_123"],
    "shop_identifiers": ["*"],  // All shops under merchant_123
    "permissions": ["read:own_data", "write:own_data"],
    "iat": 1702998378,
    "exp": 1703084778
  },
  "signature": "HMACSHA256(...)"
}
```

**Single Shop Example:**
```json
{
  "header": {
    "alg": "HS256",
    "typ": "JWT"
  },
  "payload": {
    "sub": "shop_user_id",
    "username": "shop_manager_123",
    "role": "shop",
    "email": "manager@shop123.com",
    "merchant_ids": ["merchant_123"],
    "shop_identifiers": ["shop_123"],
    "permissions": ["read:own_data", "write:own_data"],
    "iat": 1702998378,
    "exp": 1703084778
  },
  "signature": "HMACSHA256(...)"
}
```

**Token Lifetime:**
- Recommended: 24 hours (86400 seconds)
- Configurable based on security requirements
- No refresh token - users must re-login after expiry

### Token Lifecycle

```
1. LOGIN
   User → Frontend: Login credentials
   Frontend → Backend: POST /login {username, password}
   Backend → Backend: Validate credentials
   Backend → Backend: Generate JWT access token
   Backend → Frontend: Return {access_token, token_type: "Bearer", expires_in}
   Frontend → Frontend: Store access_token in localStorage
   Frontend → User: Redirect to dashboard

2. AUTHENTICATED API CALLS
   Frontend → Backend: GET /analytics
                      Headers: { Authorization: "Bearer {access_token}" }
   Backend → Backend: Validate JWT token signature and expiry
   Backend → Backend: Extract user role, merchant_ids, and shop_identifiers from token
   Backend → Backend: Apply hierarchical filtering based on token data
   Backend → Frontend: Return filtered data

3. TOKEN EXPIRY
   Frontend → Backend: GET /analytics (with expired token)
   Backend → Frontend: Return 401 Unauthorized
   Frontend → Frontend: Clear localStorage
   Frontend → User: Redirect to /login

4. LOGOUT
   Frontend → Frontend: Clear token from localStorage
   Frontend → User: Redirect to /login
   Note: No backend call needed - token naturally expires
```

---

## 🔐 Role-Based Access Control (RBAC)

### Overview

The system supports hierarchical role-based access with multi-shop support:

| Role | Access Level | Shop Access | Permissions |
|------|-------------|-------------|-------------|
| **ADMIN** | Full access | All shops (`["*"]`) | All resources, all operations |
| **RESELLER** | Multi-shop | Specific shops or all | Can manage assigned shops |
| **MERCHANT** | Multi-shop | Specific shops | Own shops only (read/write) |
| **SHOP** | Single shop | One shop | Single shop access |

### Role Definitions

#### 1. ADMIN Role

**Access:**
- View all call records (all shops)
- View all analytics (aggregated and individual)
- Manage all call configurations
- Manage all outbound numbers
- View all shop data
- Create/update/delete any resource

**Token Payload:**
```json
{
  "role": "admin",
  "merchant_ids": ["*"],
  "shop_identifiers": ["*"],
  "permissions": [
    "read:all",
    "write:all",
    "delete:all",
    "analytics:all",
    "configurations:all",
    "shops:all"
  ]
}
```

**Use Cases:**
- Breeze Buddy platform administrators
- Support staff with full access
- DevOps/Engineering teams

#### 2. RESELLER Role

**Access:**
- View call records for assigned shops
- View analytics for assigned shops
- Manage configurations for assigned shops
- Can have wildcard access (`["*"]`) or specific shops

**Token Payload:**
```json
{
  "role": "reseller",
  "merchant_ids": ["merchant_123", "merchant_456"],  // or ["*"] for all merchants
  "shop_identifiers": ["*"],  // or ["shop_1", "shop_2", ...]
  "permissions": [
    "read:assigned_shops",
    "write:assigned_shops",
    "analytics:assigned_shops"
  ]
}
```

**Use Cases:**
- White-label resellers managing multiple clients
- Agency managing multiple merchant shops

#### 3. MERCHANT Role

**Access:**
- View own call records only (across their shops)
- View own analytics only
- View own call configurations
- View own outbound numbers
- Cannot access other merchants' data
- Can manage multiple shops

**Token Payload:**
```json
{
  "role": "merchant",
  "merchant_ids": ["merchant_123"],
  "shop_identifiers": ["shop_123", "shop_456"],  // or ["*"] for all shops under merchant_123
  "permissions": [
    "read:own_data",
    "write:own_data",
    "analytics:own",
    "configurations:read"
  ]
}
```

**Use Cases:**
- Multi-location business owners
- Franchise operators managing multiple shops
- Merchant staff with access to multiple locations

#### 4. SHOP Role

**Access:**
- View call records for single shop only
- View analytics for single shop
- View configurations for single shop
- Cannot access other shops

**Token Payload:**
```json
{
  "role": "shop",
  "merchant_ids": ["merchant_123"],
  "shop_identifiers": ["shop_123"],
  "permissions": [
    "read:own_data",
    "analytics:own"
  ]
}
```

**Use Cases:**
- Individual shop managers
- Store-level staff
- Location-specific customer support

### Permission Structure

Permissions follow the format: `action:resource` or `action:scope`

#### Available Actions
- `read` - View/retrieve data
- `write` - Create/update data
- `delete` - Remove data
- `analytics` - Access analytics endpoints
- `configurations` - Manage configurations

#### Available Scopes
- `all` - All resources
- `own` - User's own resources only
- `own_data` - All data scoped to user's merchant
- Specific resources: `call_records`, `analytics`, `configurations`, `numbers`

### Backend Permission Checking

#### Pseudocode for Permission Middleware

```python
def check_permission(required_permission, required_scope=None):
    """
    Decorator to check if user has required permission
    """
    def decorator(func):
        def wrapper(request, *args, **kwargs):
            user = request.user  # Extracted from JWT token

            # Check if user has the required permission
            if not has_permission(user, required_permission):
                raise Forbidden('Insufficient permissions')

            # Check scope if required
            if required_scope and not check_scope(user, required_scope, kwargs):
                raise Forbidden('Access denied to this resource')

            return func(request, *args, **kwargs)
        return wrapper
    return decorator

def has_permission(user, permission):
    """Check if user has specific permission"""
    if user.role == 'admin':
        return True  # Admins have all permissions

    return permission in user.permissions

def check_scope(user, scope_type, kwargs):
    """Check if user has access to specific resource scope"""
    if user.role == 'admin':
        return True  # Admins can access everything

    if user.role == 'merchant':
        # Merchant can only access their own data
        if scope_type == 'merchant_id':
            requested_merchant_id = kwargs.get('merchant_id')
            return requested_merchant_id == user.merchant_id

        if scope_type == 'call_record':
            # Check if call record belongs to merchant
            call_record = get_call_record(kwargs.get('record_id'))
            return call_record.merchant_id == user.merchant_id

    return False
```

#### Example Protected Endpoints

```python
# Analytics endpoint - Admin sees all, Merchant sees own only
@app.route('/agent/voice/breeze-buddy/analytics')
@check_permission('analytics:read')
def get_analytics(request):
    user = request.user

    if user.role == 'admin':
        # Return all analytics
        return get_all_analytics(request.params)
    elif user.role == 'merchant':
        # Return only this merchant's analytics
        return get_merchant_analytics(user.merchant_id, request.params)

# Call records endpoint - scoped to merchant
@app.route('/agent/voice/breeze-buddy/call-details/<record_id>')
@check_permission('read:call_records')
@check_scope('call_record')
def get_call_record(request, record_id):
    user = request.user
    record = get_record_by_id(record_id)

    # Scope check already done by decorator
    return record

# Configuration endpoint - Admin full access, Merchant read-only
@app.route('/agent/voice/breeze-buddy/call-execution-configs/<config_id>', methods=['PUT'])
@check_permission('write:configurations')
def update_configuration(request, config_id):
    # Only admins can reach here (merchants don't have write:configurations)
    return update_config(config_id, request.body)
```

### Frontend Permission Handling

#### 1. Create Permission Store

**New file:** `src/lib/stores/auth.ts`

```typescript
import { writable, derived } from 'svelte/store';
import type { UserRole, Permission } from '$lib/types/auth';

interface AuthState {
  isAuthenticated: boolean;
  user: {
    id: string;
    username: string;
    role: UserRole;
    permissions: Permission[];
    merchantIds: string[];
    shopIdentifiers: string[];
  } | null;
}

function createAuthStore() {
  const { subscribe, set, update } = writable<AuthState>({
    isAuthenticated: false,
    user: null,
  });

  return {
    subscribe,
    setUser: (user: AuthState['user']) => {
      update(state => ({
        isAuthenticated: true,
        user,
      }));
    },
    clearUser: () => {
      set({
        isAuthenticated: false,
        user: null,
      });
    },
  };
}

export const authStore = createAuthStore();

// Derived stores for easy access
export const userRole = derived(authStore, $auth => $auth.user?.role);
export const isAdmin = derived(authStore, $auth => $auth.user?.role === 'admin');
export const isMerchant = derived(authStore, $auth => $auth.user?.role === 'merchant');
export const merchantIds = derived(authStore, $auth => $auth.user?.merchantIds || []);
export const shopIdentifiers = derived(authStore, $auth => $auth.user?.shopIdentifiers || []);
```

#### 2. Permission Checking Utilities

**New file:** `src/lib/utils/permissions.ts`

```typescript
import type { UserRole, Permission } from '$lib/types/auth';

/**
 * Check if user has specific permission
 */
export function hasPermission(
  userRole: UserRole,
  userPermissions: Permission[],
  requiredPermission: Permission
): boolean {
  // Admins have all permissions
  if (userRole === 'admin') {
    return true;
  }

  // Check if user has the specific permission
  return userPermissions.includes(requiredPermission);
}

/**
 * Check if user can access merchant
 */
export function canAccessMerchant(
  userRole: UserRole,
  userMerchantIds: string[],
  resourceMerchantId: string
): boolean {
  // Admins can access everything
  if (userRole === 'admin') {
    return true;
  }

  // Check for wildcard access
  if (userMerchantIds.includes('*')) {
    return true;
  }

  // Check if user has access to this specific merchant
  return userMerchantIds.includes(resourceMerchantId);
}

/**
 * Check if user can access shop
 */
export function canAccessShop(
  userRole: UserRole,
  userShopIdentifiers: string[],
  resourceShopId: string
): boolean {
  // Admins can access everything
  if (userRole === 'admin') {
    return true;
  }

  // Check for wildcard access
  if (userShopIdentifiers.includes('*')) {
    return true;
  }

  // Check if user has access to this specific shop
  return userShopIdentifiers.includes(resourceShopId);
}

/**
 * Filter data based on user's merchant and shop access
 */
export function filterByAccess<T extends { merchant_id?: string; shop_identifier?: string }>(
  data: T[],
  userRole: UserRole,
  userMerchantIds: string[],
  userShopIdentifiers: string[]
): T[] {
  // Admins see all data
  if (userRole === 'admin') {
    return data;
  }

  // Filter by merchant access
  let filtered = data;
  if (!userMerchantIds.includes('*')) {
    filtered = filtered.filter(item =>
      item.merchant_id && userMerchantIds.includes(item.merchant_id)
    );
  }

  // Filter by shop access
  if (!userShopIdentifiers.includes('*')) {
    filtered = filtered.filter(item =>
      item.shop_identifier && userShopIdentifiers.includes(item.shop_identifier)
    );
  }

  return filtered;
}
```

#### 3. UI Components with Permission Guards

**Example: Conditional rendering based on role**

```svelte
<script lang="ts">
  import { isAdmin, isMerchant } from '$lib/stores/auth';
</script>

{#if $isAdmin}
  <!-- Admin-only features -->
  <button>Delete All Records</button>
  <button>Manage Merchants</button>
{/if}

{#if $isMerchant}
  <!-- Merchant-only features -->
  <p>Viewing data for your shop only</p>
{/if}

<!-- Available to both -->
<div>
  <h2>Call Records</h2>
  <!-- Data will be filtered on backend based on role -->
</div>
```

#### 4. Route Protection

**Update file:** `src/routes/(app)/+layout.ts`

```typescript
import { redirect } from '@sveltejs/kit';
import type { LayoutLoad } from './$types';
import { checkAuth } from '$lib/api/auth';
import { authStore } from '$lib/stores/auth';

export const load: LayoutLoad = async ({ url }) => {
  // Check if user is authenticated
  const isAuthenticated = await checkAuth();

  if (!isAuthenticated) {
    // Not logged in → redirect to login
    throw redirect(303, '/login');
  }

  // Optional: Check role-based route access
  const restrictedAdminRoutes = ['/admin', '/merchants'];

  if (restrictedAdminRoutes.some(route => url.pathname.startsWith(route))) {
    const user = get(authStore).user;
    if (user?.role !== 'admin') {
      throw redirect(303, '/dashboard');
    }
  }

  // User is authenticated → allow access
  return {};
};
```

### Permission Matrix

| Feature | Admin | Merchant |
|---------|-------|----------|
| View All Analytics | ✅ | ❌ |
| View Own Analytics | ✅ | ✅ |
| View All Call Records | ✅ | ❌ |
| View Own Call Records | ✅ | ✅ |
| Export Data (All) | ✅ | ❌ |
| Export Data (Own) | ✅ | ✅ |
| Create Configurations | ✅ | ❌ |
| View Configurations | ✅ | ✅ (own only) |
| Update Configurations | ✅ | ❌ |
| Delete Configurations | ✅ | ❌ |
| Manage Outbound Numbers | ✅ | ✅ (own only) |
| View All Merchants | ✅ | ❌ |
| Manage Users | ✅ | ❌ (future: own staff) |

### Data Filtering Examples

#### Backend - Automatic Scope Filtering

```python
def get_call_records(request):
    """Get call records with automatic scope filtering"""
    user = request.user
    query = CallRecord.query()

    # Apply scope filter based on role
    if user.role == 'merchant':
        query = query.filter(CallRecord.merchant_id == user.merchant_id)
    # Admins see all records (no filter)

    # Apply additional filters from request
    if request.params.get('status'):
        query = query.filter(CallRecord.status == request.params['status'])

    if request.params.get('date_from'):
        query = query.filter(CallRecord.created_at >= request.params['date_from'])

    return query.all()
```

#### Frontend - Display Scoped Data

```typescript
// API automatically returns scoped data
const records = await apiClient.get('/agent/voice/breeze-buddy/call-details');

// Data is already filtered on backend based on user role
// Admin: sees all records
// Merchant: sees only their records
```

### Future Expansion: Sub-Roles

The architecture supports adding more granular roles:

```typescript
type UserRole =
  | 'admin'           // Full access
  | 'admin_readonly'  // View-only admin
  | 'merchant'        // Merchant owner
  | 'merchant_staff'  // Merchant employee (limited access)
  | 'support'         // Customer support (read-only, all merchants)
  | 'analyst';        // Data analyst (analytics only)

// Permission granularity
type Permission =
  | 'read:all' | 'read:own_data' | 'read:analytics'
  | 'write:all' | 'write:own_data'
  | 'delete:all' | 'delete:own_data'
  | 'manage:users' | 'manage:merchants'
  | 'export:all' | 'export:own_data';
```

---

## 🔧 Backend Implementation

### Database Schema Changes

#### 1. Users Table

**Purpose:** Store user credentials, roles, and merchant associations

```sql
-- Users table - stores authentication and role information
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,  -- bcrypt/argon2 hashed password
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'reseller', 'merchant', 'shop')),
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,

    -- Multi-shop access control
    shop_identifiers JSONB DEFAULT '[]'::jsonb,  -- ["*"] for all shops, or ["shop_123", "shop_456"]

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_shop_identifiers ON users USING GIN(shop_identifiers);
CREATE INDEX idx_users_is_active ON users(is_active);
```

**Example Data:**
```sql
-- Admin user (access to ALL shops)
INSERT INTO users (username, password_hash, role, shop_identifiers, email)
VALUES (
    'admin_breeze_buddy',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5Tr8n7tBcjDwS',
    'admin',
    '["*"]'::jsonb,  -- Wildcard = all shops
    'admin@breezelabs.app'
);

-- Reseller user (access to multiple shops)
INSERT INTO users (username, password_hash, role, shop_identifiers, email)
VALUES (
    'reseller_acme',
    '$2b$12$xyz...hashed...',
    'reseller',
    '["*"]'::jsonb,  -- Can also have wildcard access
    'reseller@acme.com'
);

-- Multi-shop merchant user
INSERT INTO users (username, password_hash, role, shop_identifiers, email)
VALUES (
    'merchant_joe',
    '$2b$12$abc...hashed...',
    'merchant',
    '["shop_123", "shop_456"]'::jsonb,  -- Access to multiple shops
    'joe@joescoffee.com'
);

-- Single shop user
INSERT INTO users (username, password_hash, role, shop_identifiers, email)
VALUES (
    'shop_manager_123',
    '$2b$12$def...hashed...',
    'shop',
    '["shop_123"]'::jsonb,  -- Access to single shop only
    'manager@shop123.com'
);
```

#### 2. Optional: Merchants Table

**Purpose:** Store merchant metadata (if not already exists)

```sql
-- Merchants table - stores merchant/shop information
CREATE TABLE merchants (
    merchant_id VARCHAR(255) PRIMARY KEY,
    shop_name VARCHAR(255) NOT NULL,
    contact_email VARCHAR(255),
    phone VARCHAR(50),
    address TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_merchants_is_active ON merchants(is_active);
CREATE INDEX idx_merchants_shop_name ON merchants(shop_name);
```

**Example Data:**
```sql
INSERT INTO merchants (merchant_id, shop_name, contact_email, phone)
VALUES
    ('shop_123', 'Joe''s Coffee Shop', 'joe@coffeeshop.com', '+1-555-0123'),
    ('shop_456', 'Tech Store Plus', 'info@techstore.com', '+1-555-0456');
```

#### 3. Migration Scripts

**PostgreSQL Migration - Create Tables:**
```sql
-- Migration: 001_create_users_and_merchants.sql

BEGIN;

-- Create merchants table first (if it doesn't exist)
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id VARCHAR(255) PRIMARY KEY,
    shop_name VARCHAR(255) NOT NULL,
    contact_email VARCHAR(255),
    phone VARCHAR(50),
    address TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Create users table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'merchant')),
    merchant_id VARCHAR(255) NULL,
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT fk_users_merchant FOREIGN KEY (merchant_id)
        REFERENCES merchants(merchant_id) ON DELETE SET NULL
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_merchant_id ON users(merchant_id);
CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_merchants_is_active ON merchants(is_active);
CREATE INDEX IF NOT EXISTS idx_merchants_shop_name ON merchants(shop_name);

-- Create admin user (update password hash with actual hashed password)
INSERT INTO users (username, password_hash, role, merchant_id, email)
VALUES (
    'admin_breeze_buddy',
    '$2b$12$REPLACE_WITH_ACTUAL_HASH',
    'admin',
    NULL,
    'admin@breezelabs.app'
) ON CONFLICT (username) DO NOTHING;

COMMIT;
```

**Rollback Migration:**
```sql
-- Migration: 001_rollback_users_and_merchants.sql

BEGIN;

DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS merchants CASCADE;

COMMIT;
```

#### 4. Password Hashing

**IMPORTANT:** Always hash passwords before storing. Never store plain text passwords.

**Python Example (using bcrypt):**
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

# Example usage
hashed_pw = hash_password('secure_password_123')
print(hashed_pw)  # $2b$12$LQv3c1yqBWVHxkd0LHAkCO...

# Verification
is_valid = verify_password('secure_password_123', hashed_pw)
print(is_valid)  # True
```

**Node.js Example (using bcrypt):**
```javascript
const bcrypt = require('bcrypt');

async function hashPassword(password) {
  const saltRounds = 12;
  const hash = await bcrypt.hash(password, saltRounds);
  return hash;
}

async function verifyPassword(password, hash) {
  return await bcrypt.compare(password, hash);
}

// Example usage
const hashed = await hashPassword('secure_password_123');
console.log(hashed);

const isValid = await verifyPassword('secure_password_123', hashed);
console.log(isValid); // true
```

#### 5. Database Queries for Authentication

**Login Query:**
```python
def authenticate_user(username: str, password: str):
    """Authenticate user and return user data"""
    # Get user from database
    user = db.execute("""
        SELECT id, username, password_hash, role, merchant_id, email, is_active
        FROM users
        WHERE username = %s AND is_active = true
    """, (username,)).fetchone()

    if not user:
        return None

    # Verify password
    if not verify_password(password, user['password_hash']):
        return None

    # Return user data (without password hash)
    return {
        'user_id': user['id'],
        'username': user['username'],
        'role': user['role'],
        'merchant_id': user['merchant_id'],
        'email': user['email']
    }
```

**Get User by ID (for token refresh):**
```python
def get_user_by_id(user_id: str):
    """Get user data by ID"""
    user = db.execute("""
        SELECT id, username, role, merchant_id, email
        FROM users
        WHERE id = %s AND is_active = true
    """, (user_id,)).fetchone()

    return user
```

**Update User Role:**
```python
def update_user_role(user_id: str, new_role: str, merchant_id: str = None):
    """Update user role (e.g., promote to admin)"""
    db.execute("""
        UPDATE users
        SET role = %s, merchant_id = %s, updated_at = NOW()
        WHERE id = %s
    """, (new_role, merchant_id, user_id))
    db.commit()
```

#### 6. Data Relationships

```
┌─────────────────┐
│    merchants    │
│─────────────────│
│ merchant_id PK  │
│ shop_name       │
│ contact_email   │
│ is_active       │
└────────┬────────┘
         │
         │ 1:N (one merchant, many users)
         │
         ▼
┌─────────────────┐
│     users       │
│─────────────────│
│ id PK           │
│ username UNIQUE │
│ password_hash   │
│ role            │
│ merchant_id FK  │◀── Points to merchants.merchant_id
│ email           │
│ is_active       │
└─────────────────┘
```

**Notes:**
- One merchant can have multiple users (future: merchant + staff)
- Admins have `merchant_id = NULL`
- Merchants have `merchant_id` pointing to their shop
- All call records, analytics, etc. have `merchant_id` for filtering

#### 7. Database Security Best Practices

✅ **Password Hashing:**
- Use bcrypt (12+ rounds) or Argon2
- Never use MD5, SHA1, or plain text
- Add salt automatically (bcrypt does this)

✅ **SQL Injection Prevention:**
```python
# ✅ GOOD - Parameterized query
db.execute("SELECT * FROM users WHERE username = %s", (username,))

# ❌ BAD - String concatenation
db.execute(f"SELECT * FROM users WHERE username = '{username}'")
```

✅ **Access Control:**
- Use separate DB users with minimal privileges
- Application user should NOT have DROP/ALTER permissions
- Use read-only user for analytics queries

✅ **Indexes:**
- Add indexes on frequently queried columns (`username`, `merchant_id`)
- Monitor query performance with `EXPLAIN`

✅ **Constraints:**
- Use CHECK constraints for role validation
- Use UNIQUE constraints for usernames
- Use FOREIGN KEY constraints for data integrity

---

### Required Changes

#### 1. API Endpoint Updates

**Endpoints:**
- `POST /agent/voice/breeze-buddy/login` - Updated to return JWT token
- No logout endpoint needed - handled client-side

#### 2. Login Endpoint Response

**Current (Cookie-based):**
```json
{
  "success": true
}
```
Sets: `Set-Cookie: session=...; HttpOnly`

**New (JWT Token-based):**
```json
{
  "success": true,
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 86400
}
```

**No cookies set** - Frontend stores token in localStorage or cookies

#### 3. Token Validation Middleware

**Pseudocode:**
```python
def validate_access_token(request):
    # Extract token from Authorization header
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise Unauthorized('Missing or invalid authorization header')

    token = auth_header.replace('Bearer ', '')

    try:
        # Decode and verify JWT (automatically checks signature and expiration)
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])

        # JWT library already verified:
        # - Token signature is valid
        # - Token has not expired (exp claim)
        # - Token was issued (iat claim)

        # Create user object from token payload
        request.user = {
            'id': payload['sub'],
            'username': payload['username'],
            'role': payload['role'],
            'permissions': payload['permissions'],
            'scope': payload['scope'],
            'merchant_id': payload.get('merchant_id')
        }

        return True

    except jwt.ExpiredSignatureError:
        raise Unauthorized('Token has expired')
    except jwt.InvalidTokenError:
        raise Unauthorized('Invalid token')
```

**Key Points:**
- Extract token from `Authorization: Bearer <token>` header
- Validate signature using SECRET_KEY
- JWT library automatically checks expiration
- Return 401 if invalid or expired
- Attach user info from token to request object

#### 4. Token Generation

**Pseudocode:**
```python
import jwt
from datetime import datetime, timedelta

def generate_access_token(user_id, username, role, merchant_ids=None, shop_identifiers=None):
    # Get permissions based on role
    permissions = get_permissions_for_role(role)

    # JWT payload
    payload = {
        'sub': user_id,
        'username': username,
        'role': role,
        'permissions': permissions,
        'merchant_ids': merchant_ids or [],
        'shop_identifiers': shop_identifiers or [],
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=24)  # 24 hours
    }

    # Encode JWT with secret key
    access_token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')

    return {
        'access_token': access_token,
        'token_type': 'Bearer',
        'expires_in': 86400  # 24 hours in seconds
    }

def get_permissions_for_role(role):
    """Get permissions list based on user role"""
    if role == 'admin':
        return [
            'read:all',
            'write:all',
            'delete:all',
            'analytics:all',
            'configurations:all',
            'merchants:all'
        ]
    elif role == 'reseller':
        return [
            'read:assigned_shops',
            'write:assigned_shops',
            'analytics:assigned_shops',
            'configurations:assigned_shops'
        ]
    elif role == 'merchant':
        return [
            'read:own_data',
            'write:own_data',
            'analytics:own',
            'configurations:read'
        ]
    elif role == 'shop':
        return [
            'read:own_data',
            'analytics:own'
        ]
    else:
        return []
```

**Token Expiration:**
- Default: 24 hours (86400 seconds)
- Configurable via environment variable
- No automatic refresh - users must re-login after expiry

#### 5. Hierarchical Merchant + Shop Access Enforcement

**CRITICAL**: Backend must ALWAYS use `merchant_ids` and `shop_identifiers` from JWT token, never from request parameters.

**Pseudocode:**
```python
@app.route('/agent/voice/breeze-buddy/analytics')
@validate_access_token  # Middleware extracts user from token
def get_analytics(request):
    user = request.user  # From JWT token

    # ALWAYS use merchant_ids and shop_identifiers from token, NOT from request params
    query = build_base_query()

    # Apply merchant filter
    if user['role'] == 'admin' or '*' in user['merchant_ids']:
        # Admin or wildcard merchant access - no merchant filter
        pass
    else:
        # Filter to specific merchants
        query = query.filter(analytics.merchant_id.in_(user['merchant_ids']))

    # Apply shop filter
    if '*' in user['shop_identifiers']:
        # Wildcard shop access - no shop filter
        pass
    else:
        # Filter to specific shops
        query = query.filter(analytics.shop_identifier.in_(user['shop_identifiers']))

    return query.all()
```

**Security Note:** Never trust `merchant_ids` or `shop_identifiers` from query params, request body, or URL path. Always use the arrays from the JWT token.

### Backend Security Checklist

- [ ] Use strong secret keys (minimum 256-bit random string)
- [ ] Store secrets in environment variables (never in code)
- [ ] Implement rate limiting on login endpoint
- [ ] Use HTTPS only in production
- [ ] Set appropriate CORS headers
- [ ] Log authentication failures
- [ ] Validate token expiry strictly (JWT library does this automatically)
- [ ] Implement account lockout after N failed login attempts
- [ ] Always use merchant_ids and shop_identifiers from JWT token, never from request params
- [ ] Hash passwords with bcrypt (cost factor 12+)
- [ ] Return same error message for invalid username and invalid password

---

## 💻 Frontend Implementation

### File Changes Required

#### 1. Create Token Storage Manager

**New file:** `src/lib/utils/token-storage.ts`

```typescript
/**
 * Token Storage Manager
 * Simple JWT token storage using localStorage
 */

const TOKEN_KEY = 'auth_token';
const TOKEN_EXPIRY_KEY = 'auth_token_expiry';

class TokenStorage {
  /**
   * Store access token in localStorage
   */
  setToken(accessToken: string, expiresIn: number): void {
    const expiresAt = Date.now() + expiresIn * 1000;

    localStorage.setItem(TOKEN_KEY, accessToken);
    localStorage.setItem(TOKEN_EXPIRY_KEY, expiresAt.toString());
  }

  /**
   * Get access token from localStorage
   * Returns null if token doesn't exist or is expired
   */
  getToken(): string | null {
    const token = localStorage.getItem(TOKEN_KEY);
    const expiryStr = localStorage.getItem(TOKEN_EXPIRY_KEY);

    if (!token || !expiryStr) {
      return null;
    }

    // Check if token is expired
    const expiresAt = parseInt(expiryStr, 10);
    if (Date.now() >= expiresAt) {
      this.clearToken();
      return null;
    }

    return token;
  }

  /**
   * Check if we have a valid token
   */
  hasValidToken(): boolean {
    return this.getToken() !== null;
  }

  /**
   * Clear token from localStorage
   */
  clearToken(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_EXPIRY_KEY);
  }

  /**
   * Get time until token expires (in seconds)
   */
  getTimeUntilExpiry(): number {
    const expiryStr = localStorage.getItem(TOKEN_EXPIRY_KEY);
    if (!expiryStr) return 0;

    const expiresAt = parseInt(expiryStr, 10);
    return Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
  }
}

// Export singleton instance
export const tokenStorage = new TokenStorage();
```

**Alternative: Cookie-based storage**

If you prefer cookies over localStorage:

```typescript
/**
 * Token Storage Manager - Cookie-based
 */
class TokenStorage {
  private readonly TOKEN_COOKIE = 'auth_token';

  setToken(accessToken: string, expiresIn: number): void {
    const expires = new Date(Date.now() + expiresIn * 1000);
    document.cookie = `${this.TOKEN_COOKIE}=${accessToken}; expires=${expires.toUTCString()}; path=/; SameSite=Lax`;
  }

  getToken(): string | null {
    const cookies = document.cookie.split('; ');
    const tokenCookie = cookies.find(c => c.startsWith(`${this.TOKEN_COOKIE}=`));
    return tokenCookie ? tokenCookie.split('=')[1] : null;
  }

  clearToken(): void {
    document.cookie = `${this.TOKEN_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
  }

  hasValidToken(): boolean {
    return this.getToken() !== null;
  }
}

export const tokenStorage = new TokenStorage();
```

#### 2. Create JWT Decoder Utility

**New file:** `src/lib/utils/jwt-decoder.ts`

```typescript
import type { UserInfo } from '$lib/types/auth';

/**
 * Decode JWT token payload (client-side only, no signature verification)
 * Note: This is for reading token data, NOT for security validation
 */
export function decodeJWT(token: string): UserInfo | null {
  try {
    // JWT format: header.payload.signature
    const parts = token.split('.');
    if (parts.length !== 3) {
      return null;
    }

    // Decode base64 payload
    const payload = JSON.parse(atob(parts[1]));

    return {
      id: payload.sub,
      username: payload.username,
      role: payload.role,
      permissions: payload.permissions || [],
      merchantIds: payload.merchant_ids || [],
      shopIdentifiers: payload.shop_identifiers || [],
    };
  } catch (error) {
    console.error('Failed to decode JWT:', error);
    return null;
  }
}
```

#### 3. Update Auth API - Login

**Update file:** `src/lib/api/auth/mutations.ts`

```typescript
/**
 * Auth API Mutations
 * Authentication-related operations
 */

import { apiClient } from '../client';
import { authEndpoints } from '../endpoints';
import type { LoginCredentials, LoginResponse, TokenResponse } from '$lib/types/auth';
import { tokenStorage } from '$lib/utils/token-storage';
import { decodeJWT } from '$lib/utils/jwt-decoder';
import { authStore } from '$lib/stores/auth';

/**
 * Login user with JWT token-based authentication
 */
export async function login(credentials: LoginCredentials): Promise<LoginResponse> {
  try {
    const response = await apiClient.post<TokenResponse>(authEndpoints.login, credentials);

    if (response.success && response.data) {
      // Store access token in localStorage
      tokenStorage.setToken(response.data.access_token, response.data.expires_in);

      // Decode token to get user info
      const userInfo = decodeJWT(response.data.access_token);
      if (userInfo) {
        authStore.setUser(userInfo);
      }

      return { success: true };
    }

    return {
      success: false,
      detail: response.error?.message || 'Invalid username or password',
    };
  } catch (error) {
    console.error('Login error:', error);
    return {
      success: false,
      detail: 'An error occurred. Please try again.',
    };
  }
}

/**
 * Logout user
 */
export async function logout(): Promise<void> {
  // Clear token from storage
  tokenStorage.clearToken();
  authStore.clearUser();

  // Note: No backend call needed - token naturally expires
}
```

#### 4. Update Auth API - Check Auth

**Update file:** `src/lib/api/auth/queries.ts`

```typescript
/**
 * Auth API Queries
 * Read-only authentication checks
 */

import { tokenStorage } from '$lib/utils/token-storage';
import { decodeJWT } from '$lib/utils/jwt-decoder';
import { authStore } from '$lib/stores/auth';

/**
 * Check if user is authenticated
 * Returns true if we have a valid token in localStorage
 */
export async function checkAuth(): Promise<boolean> {
  const token = tokenStorage.getToken();

  if (!token) {
    return false;
  }

  // Restore user info from token
  const userInfo = decodeJWT(token);
  if (userInfo) {
    authStore.setUser(userInfo);
    return true;
  }

  return false;
}

/**
 * Initialize auth on app load
 * Restores session from localStorage if available
 */
export async function initializeAuth(): Promise<void> {
  await checkAuth();
}
```

#### 5. Update API Client to Use Tokens

**Update file:** `src/lib/api/client.ts`

Add token injection to all requests:

```typescript
import { tokenStorage } from '$lib/utils/token-storage';
import { goto } from '$app/navigation';

/**
 * Handle 401 Unauthorized - redirect to login
 */
function handle401Redirect(): void {
  // Clear token and redirect to login
  tokenStorage.clearToken();
  goto('/login', { invalidateAll: true });
}

/**
 * Make an HTTP request with error handling
 */
async function request<T>(
  endpoint: string,
  options: RequestOptions = {}
): Promise<ApiResponse<T>> {
  const {
    timeout = apiConfig.timeout,
    retry = true,
    headers = {},
    ...fetchOptions
  } = options;

  const url = `${apiConfig.baseUrl}${endpoint}`;

  // Get access token from localStorage
  const accessToken = tokenStorage.getToken();

  // Merge default headers with custom headers and auth token
  const requestHeaders: HeadersInit = {
    ...apiConfig.defaultHeaders,
    ...headers,
  };

  // Add Authorization header if we have a token
  if (accessToken) {
    requestHeaders['Authorization'] = `Bearer ${accessToken}`;
  }

  // Create abort controller for timeout
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...fetchOptions,
      headers: requestHeaders,
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    // Handle non-OK responses
    if (!response.ok) {
      const errorData = await response.json().catch(() => ({
        code: 'UNKNOWN_ERROR',
        message: 'An unknown error occurred',
      }));

      // Handle 401 Unauthorized - redirect to login
      if (response.status === 401) {
        handle401Redirect();
      }

      throw new ApiClientError(
        response.status,
        errorData.code || 'API_ERROR',
        errorData.message || errorData.detail || `HTTP Error ${response.status}`,
        errorData.details
      );
    }

    // Parse response
    const data = await response.json();

    // Handle API response format
    if (typeof data === 'object' && data !== null && 'success' in data) {
      return data as ApiResponse<T>;
    }

    // Wrap non-standard responses
    return {
      success: true,
      data: data as T,
    };
  } catch (error) {
    // ... rest of error handling remains the same
  }
}
```

#### 5. Update Auth Types

**Update file:** `src/lib/types/auth.ts`

```typescript
export interface LoginCredentials {
  username: string;
  password: string;
}

export interface LoginResponse {
  success: boolean;
  detail?: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number; // seconds
}

export interface AuthError {
  message: string;
  detail?: string;
}

/**
 * User roles
 */
export type UserRole = 'admin' | 'reseller' | 'merchant' | 'shop';

/**
 * Permissions
 */
export type Permission =
  | 'read:all'
  | 'read:own_data'
  | 'read:analytics'
  | 'write:all'
  | 'write:own_data'
  | 'delete:all'
  | 'delete:own_data'
  | 'analytics:all'
  | 'analytics:own'
  | 'configurations:all'
  | 'configurations:read'
  | 'merchants:all';

/**
 * User info extracted from JWT token
 */
export interface UserInfo {
  id: string;
  username: string;
  role: UserRole;
  permissions: Permission[];
  merchantIds: string[];
  shopIdentifiers: string[];
}
```

#### 6. Update API Endpoints

**Update file:** `src/lib/api/endpoints.ts`

```typescript
/**
 * Auth endpoints
 */
export const authEndpoints = {
  login: `${agent}/login`,
} as const;
```

#### 7. Initialize Auth on App Load

**Update file:** `src/routes/+layout.svelte`

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  import { initializeAuth } from '$lib/api/auth';

  // Initialize authentication on app load
  onMount(async () => {
    await initializeAuth();
  });
</script>

<slot />
```

#### 8. Update Logout Handler

**Update file:** `src/lib/components/shared/navigation/NavUser.svelte`

```typescript
import { logout } from '$lib/api/auth';
import { goto } from '$app/navigation';

async function handleLogout() {
  // Logout clears token from localStorage and auth store
  await logout();

  // Redirect to login
  goto('/login', { invalidateAll: true });
}
```

#### 9. Remove Old Cookie Utils

**Delete or deprecate:**
- `src/lib/utils/session.ts` (cookie-based session utils)
- Cookie-related functions in `src/lib/utils/auth.ts`

**Key Changes Summary:**
- Token stored in localStorage (or cookies if preferred)
- No refresh token - users re-login when token expires
- 401 responses automatically redirect to login
- Simple logout - just clear localStorage and redirect

---

## 🔒 Security Measures

### 1. HTTPS Only

| Measure | Implementation |
|---------|----------------|
| **HTTPS Required** | All token transmission must use HTTPS in production |
| **Secure Context** | Tokens sent via Authorization header over secure connection |

### 2. XSS Protection

| Measure | Implementation |
|---------|----------------|
| **Content Security Policy** | Add CSP headers to prevent inline script injection |
| **Input sanitization** | Sanitize all user inputs on backend |
| **Output encoding** | Encode data before rendering in UI |

**Note**: Tokens in localStorage are vulnerable to XSS. Ensure proper CSP and input sanitization.

**Add to `static` folder or hosting config:**

`static/_headers` (for static hosting):
```
/*
  Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self' https://clairvoyance.breezesdk.store
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  X-XSS-Protection: 1; mode=block
  Referrer-Policy: strict-origin-when-cross-origin
```

### 3. CORS Configuration

| Measure | Implementation |
|---------|----------------|
| **CORS headers** | Whitelist only trusted origins |
| **Credentials handling** | Proper CORS configuration for cross-domain requests |

**Backend CORS configuration:**
```python
ALLOWED_ORIGINS = [
    'https://yourapp.com',
    'http://localhost:5173',  # For development
]

@app.middleware
def cors_middleware(request, response):
    origin = request.headers.get('Origin')
    if origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
```

### 4. Token Security

| Measure | Implementation |
|---------|----------------|
| **JWT Signature Verification** | Backend validates signature on every request |
| **Expiration Enforcement** | Tokens expire after configured time (e.g., 24 hours) |
| **Strong Secret Key** | Use minimum 256-bit random secret key |
| **Automatic Logout on Expiry** | 401 responses redirect to login |

### 5. Brute Force Protection

**Backend implementation:**
```python
from datetime import datetime, timedelta
import redis

redis_client = redis.Redis()

def rate_limit_login(username, ip_address):
    """Rate limit login attempts"""
    key = f'login_attempts:{username}:{ip_address}'
    attempts = redis_client.incr(key)

    if attempts == 1:
        redis_client.expire(key, 300)  # 5 minutes

    if attempts > 5:
        raise TooManyRequests('Too many login attempts. Try again in 5 minutes.')
```

---

## 📝 Migration Plan

### Phase 1: Backend Preparation (3-4 days)

**Day 1: Setup**
- [ ] Install JWT library (PyJWT, jsonwebtoken, etc.)
- [ ] Generate and secure JWT secret key (256-bit minimum)
- [ ] Create database migration for users table
- [ ] Add indexes for performance

**Day 2: Implementation**
- [ ] Implement token generation function
- [ ] Update login endpoint to return JWT token
- [ ] Implement token validation middleware
- [ ] Add merchant_id enforcement logic

**Day 3: Testing**
- [ ] Unit tests for token generation and validation
- [ ] Integration tests for login flow
- [ ] Test role-based access control
- [ ] Security testing (invalid tokens, expired tokens, etc.)

### Phase 2: Frontend Implementation (2-3 days)

**Day 1: Core Implementation**
- [ ] Create token storage manager (localStorage or cookie-based)
- [ ] Create JWT decoder utility
- [ ] Update auth API functions (login, logout, checkAuth)
- [ ] Update auth types
- [ ] Integrate token injection in API client

**Day 2: Integration & Testing**
- [ ] Initialize auth on app load
- [ ] Update login flow
- [ ] Update logout flow
- [ ] Handle 401 redirects to login
- [ ] Test login flow
- [ ] Test logout flow
- [ ] Test page refresh (should maintain session from localStorage)
- [ ] Test token expiry handling (redirect to login)

**Day 3: Security & Cleanup**
- [ ] Add CSP headers
- [ ] Configure CORS properly
- [ ] Remove old cookie-based auth utils
- [ ] Security audit

### Phase 3: Deployment (2-3 days)

**Day 1: Staging Deployment**
- [ ] Deploy backend to staging
- [ ] Deploy frontend to staging
- [ ] Integration testing on staging
- [ ] Test cross-domain requests if applicable

**Day 2: Production Deployment**
- [ ] Deploy backend to production
- [ ] Monitor for errors
- [ ] Deploy frontend to production
- [ ] Monitor authentication metrics
- [ ] Monitor login success rates

**Day 3: Monitoring & Cleanup**
- [ ] Monitor error rates
- [ ] Fix any issues
- [ ] Remove old cookie-based code
- [ ] Update documentation

---

## ✅ Testing Checklist

### Functional Testing

- [ ] **Login Flow**
  - [ ] Valid credentials return JWT access token
  - [ ] Invalid credentials return error
  - [ ] Token is stored in localStorage correctly
  - [ ] User info is decoded from token
  - [ ] Auth store is updated with user info

- [ ] **Authenticated Requests**
  - [ ] API calls include `Authorization: Bearer <token>` header
  - [ ] Valid token returns data
  - [ ] Invalid token returns 401
  - [ ] Expired token returns 401 and redirects to login
  - [ ] Token is correctly extracted from localStorage

- [ ] **Logout**
  - [ ] Token is removed from localStorage
  - [ ] Auth store is cleared
  - [ ] User is redirected to login page
  - [ ] Subsequent API calls fail (no token)

- [ ] **Page Refresh**
  - [ ] Session is restored from localStorage
  - [ ] User stays logged in
  - [ ] Auth store is rehydrated from token
  - [ ] Expired token causes logout and redirect

### Security Testing

- [ ] **HTTPS**
  - [ ] All production traffic uses HTTPS
  - [ ] Tokens never sent over HTTP in production

- [ ] **XSS Awareness**
  - [ ] CSP headers are configured
  - [ ] Input sanitization is working
  - [ ] Token in localStorage is understood as XSS risk

- [ ] **CORS Configuration**
  - [ ] Only whitelisted origins can access API
  - [ ] Preflight requests work correctly
  - [ ] Authorization header allowed in CORS

- [ ] **Token Security**
  - [ ] JWT signature is validated on backend
  - [ ] Expired tokens are rejected
  - [ ] Invalid signatures are rejected
  - [ ] Token expiration is enforced (e.g., 24 hours)

- [ ] **Brute Force**
  - [ ] Rate limiting works on login endpoint
  - [ ] Account lockout after N failed attempts
  - [ ] IP-based rate limiting

### Performance Testing

- [ ] Token generation is fast (<50ms)
- [ ] Token validation is fast (<10ms)
- [ ] localStorage access doesn't impact UX
- [ ] Login flow completes in <1 second

### RBAC Testing

- [ ] **Admin Role (shop_identifiers: ["*"])**
  - [ ] Can view all call records (all shops)
  - [ ] Can view all analytics across all shops
  - [ ] Can create/update/delete configurations for any shop
  - [ ] Can manage all outbound numbers
  - [ ] Can access all endpoints
  - [ ] Wildcard access is properly validated

- [ ] **Reseller Role**
  - [ ] Can view call records for assigned shops only
  - [ ] Can view analytics for assigned shops
  - [ ] Cannot view other shops' data
  - [ ] Wildcard or specific shop array works correctly

- [ ] **Merchant Role (Multi-Shop)**
  - [ ] Can view call records across all owned shops
  - [ ] Can view analytics for all owned shops
  - [ ] Cannot view other merchants' shops
  - [ ] Shop filtering works with multiple shop_identifiers
  - [ ] Can view own configurations (read-only)
  - [ ] Cannot access shops not in their shop_identifiers array

- [ ] **Shop Role (Single Shop)**
  - [ ] Can only view call records for single shop
  - [ ] Can only view analytics for single shop
  - [ ] Cannot access other shops
  - [ ] Single shop_identifier array works correctly

- [ ] **Permission Checks**
  - [ ] Permissions are correctly included in JWT
  - [ ] Backend validates permissions on each request
  - [ ] Missing permissions return 403 Forbidden
  - [ ] Frontend hides/shows UI based on permissions
  - [ ] shop_identifiers are correctly validated

- [ ] **Shop Access Filtering**
  - [ ] Data is correctly filtered by shop_identifiers array
  - [ ] Admin with ["*"] sees all shops
  - [ ] Attempting to access unauthorized shop returns 403
  - [ ] Query parameters cannot bypass shop filtering
  - [ ] Multi-shop merchants see only their shops
  - [ ] Empty shop_identifiers array denies all access

---

## 📊 Implementation Summary

### Backend Changes

| File/Component | Change Type | Effort |
|----------------|-------------|--------|
| **Database** | | |
| Users table | Create | Low |
| Merchants table (optional) | Create | Low |
| Migration scripts | New | Low |
| Database indexes | Create | Low |
| **API Endpoints** | | |
| Login endpoint | Modify | Medium |
| **Auth Logic** | | |
| Auth middleware | New | Medium |
| Token generation | New | Low |
| Token validation | New | Low |
| Password hashing | New | Low |
| User queries | New | Low |
| Merchant ID enforcement | New | Low |
| **Security** | | |
| Rate limiting | New | Low |
| Permission checking | New | Medium |
| CORS configuration | Configure | Low |
| **Total** | | **~12-16 hours** |

### Frontend Changes

| File | Change Type | Effort |
|------|-------------|--------|
| `token-storage.ts` | New | Low |
| `jwt-decoder.ts` | New | Low |
| `stores/auth.ts` | Existing | N/A |
| `utils/permissions.ts` | Existing | N/A |
| `auth/mutations.ts` | Modify | Low |
| `auth/queries.ts` | Modify | Low |
| `api/client.ts` | Modify | Low |
| `types/auth.ts` | Modify | Low |
| `endpoints.ts` | Modify | Low |
| `+layout.svelte` | Modify | Low |
| `(app)/+layout.ts` | Existing | N/A |
| `NavUser.svelte` | Modify | Low |
| Remove `session.ts` | Delete | Low |
| **Total** | | **~8-10 hours** |

### Total Effort Estimate

- **Backend**: 12-16 hours (simplified JWT auth + RBAC)
- **Frontend**: 8-10 hours (simple token storage + logout)
- **Testing**: 6-8 hours (functional + security + RBAC)
- **Deployment & Monitoring**: 2-4 hours
- **Total**: **28-38 hours** (~4-5 days for 1 developer)

---

## 🎯 Success Criteria

After implementation, the following must be true:

✅ **Functionality**
- Users can login and receive JWT access tokens
- Users can access protected resources with Bearer token
- Tokens are stored in localStorage (or cookies)
- Users can logout successfully
- Sessions persist across page refreshes (from localStorage)
- Expired tokens redirect to login page

✅ **Security**
- JWT signature is validated on every request
- Expired tokens are rejected automatically
- HTTPS is enforced in production
- CORS is properly configured
- Rate limiting prevents brute force attacks
- merchant_ids and shop_identifiers from JWT are enforced (never from request params)

✅ **Performance**
- Login completes in <1 second
- Token validation is fast (<10ms)
- No performance degradation from localStorage access

✅ **User Experience**
- Simple authentication flow
- Clear error messages
- Automatic logout on token expiry
- Smooth navigation
- User must re-login after token expires (expected behavior)

---

## 🔄 Rollback Plan

If issues arise in production:

1. **Immediate Actions** (5 minutes)
   - Revert frontend deployment
   - Keep backend changes (backwards compatible)
   - Users fall back to cookie-based auth

2. **Investigation** (1-2 hours)
   - Review error logs
   - Identify root cause
   - Test fix in staging

3. **Hotfix** (2-4 hours)
   - Apply fix
   - Test thoroughly
   - Redeploy

4. **Communication**
   - Notify users of temporary issues
   - Provide status updates
   - Confirm resolution

---

## 📞 Support & Maintenance

### Monitoring Dashboards

Monitor these metrics:

- **Login success rate** (target: >99%)
- **Authentication errors** (target: <1%)
- **Token generation time** (target: <50ms)
- **Token validation time** (target: <10ms)
- **401 error rate** (indicates token expiry frequency)

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Users logged out unexpectedly | Token expired | Expected behavior - increase token lifetime if too short |
| 401 errors on API calls | Access token expired | User must re-login - verify token lifetime is appropriate |
| Token not persisting on refresh | localStorage cleared or disabled | Check browser settings, verify storage implementation |
| High error rate on login | Rate limiting too aggressive | Adjust rate limits |
| CORS errors | Missing or incorrect CORS headers | Update backend CORS configuration |

---

## 📚 Additional Resources

### Security Best Practices
- [OWASP JWT Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html)
- [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [RFC 7519 - JWT Standard](https://datatracker.ietf.org/doc/html/rfc7519)

### Libraries
- **Python**: PyJWT, python-jose
- **Node.js**: jsonwebtoken, jose
- **Go**: golang-jwt/jwt
- **Java**: jjwt, nimbus-jose-jwt

### Tools for Testing
- **JWT Debugger**: https://jwt.io/
- **Security Testing**: OWASP ZAP, Burp Suite
- **Load Testing**: k6, Artillery, JMeter

---

---

## 🎯 Key Features Summary

### Authentication
✅ **Simple JWT Token-Based Auth** - Single access token with Bearer authentication
✅ **localStorage Storage** - Token persists across page refreshes
✅ **Cross-Domain Support** - Works with different frontend/backend domains via CORS
✅ **Automatic Logout on Expiry** - 401 responses redirect to login
✅ **No Refresh Complexity** - Users re-login when token expires

### Authorization (RBAC)
✅ **Role-Based Access Control** - Admin, Reseller, Merchant, and Shop roles
✅ **Permission System** - Granular permissions (read, write, delete, analytics, etc.)
✅ **Hierarchical Filtering** - Automatic data filtering by merchant_ids and shop_identifiers from JWT
✅ **Frontend Guards** - UI components adapt to user permissions
✅ **Backend Enforcement** - All endpoints protected with permission checks
✅ **Extensible** - Easy to add new roles (merchant_staff, support, analyst, etc.)

### Security
✅ **JWT Signature Validation** - Backend validates every token
✅ **HTTPS Required** - Secure token transmission
✅ **Rate Limiting** - Brute force protection on login
✅ **CORS Configuration** - Whitelisted origins only
✅ **Hierarchical Access Enforcement** - Always use merchant_ids and shop_identifiers from JWT, never from request params
✅ **Configurable Token Lifetime** - Default 24 hours, adjustable based on security needs

---

**Document Version**: 2.0
**Last Updated**: 2025-12-17
**Author**: Claude Code
**Status**: Ready for Implementation
**Approach**: Simple JWT Token Auth (localStorage) + Role-Based Access Control (RBAC)
