# 📋 Endpoints Coverage Analysis

**Date**: 2025-12-17
**Purpose**: Verify all existing endpoints are covered in the new design

---

## 📊 Current Endpoints Inventory

### 1. Authentication (auth.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `GET` | `/login` | ✅ Covered | `POST /auth/login` (HTML removed, API only) |
| `POST` | `/login` | ✅ Covered | `POST /auth/login` |
| `GET` | `/logout` | ✅ Covered | `POST /auth/logout` |
| - | - | ⭐ New | `GET /auth/me` (get current user info) |

**Notes**:
- HTML login page (`GET /login`) removed - moved to separate frontend repo
- Logout changed from GET to POST (REST best practice)
- Added `/auth/me` endpoint for user info

---

### 2. Dashboard & Analytics (dashboard.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `GET` | `/dashboard` | ❌ Removed | Frontend moved to separate repo |
| `GET` | `/cron/initiate` | ⚠️ **NEEDS REVIEW** | **DO NOT CHANGE** - Review required first |
| `GET` | `/breeze/order-confirmation/analytics` | ✅ Covered | `POST /analytics` (type=summary) |
| `GET` | `/breeze/order-confirmation/call-details` | ✅ Covered | `POST /analytics` (type=call-details) |

**Notes**:
- Dashboard HTML endpoint removed
- ⚠️ **CRON endpoint** - **NEEDS REVIEW** - Do not make changes to this endpoint yet
- Analytics consolidated into single POST endpoint with flexible payload

---

### 3. Templates (template.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `GET` | `/template` | ⚠️ Keep + Deprecate | `GET /templates` (plural, with filters) |
| `POST` | `/template` | ⚠️ Keep + Deprecate | `POST /templates` |
| - | - | ⭐ New | `GET /templates/{id}` |
| - | - | ⭐ New | `PUT /templates/{id}` |
| - | - | ⭐ New | `DELETE /templates/{id}` |

**Notes**:
- **OLD endpoints must be kept** with deprecation comments for backward compatibility
- **NEW endpoints added alongside** - plural `/templates` with RESTful CRUD
- Both old and new endpoints will coexist during migration period
- Frontend/clients should migrate to new endpoints

---

### 4. Outbound Numbers (outbound_numbers.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `POST` | `/outbound-number` | ✅ Covered | `POST /numbers` |
| `GET` | `/outbound-number` | ✅ Covered | `GET /numbers` (with filters) |
| `DELETE` | `/outbound-number/{number_id}` | ✅ Covered | `DELETE /numbers/{id}` |
| `GET` | `/breeze/order-confirmation/outbound-numbers` | ✅ Covered | Consolidated into `GET /numbers?template=order-confirmation` |
| - | - | ⭐ New | `GET /numbers/{id}` |
| - | - | ⭐ New | `PUT /numbers/{id}` |

**Notes**:
- Template-specific endpoint consolidated via query parameters
- Added RESTful endpoints (GET by ID, PUT)

---

### 5. Call Execution Config (call_execution_config.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `POST` | `/call-execution-config` | ⚠️ Keep + Deprecate | `POST /configurations` |
| `PUT` | `/call-execution-config` | ⚠️ Keep + Deprecate | `PUT /configurations/{id}` |
| `GET` | `/call-execution-config/{merchant_id}` | ⚠️ Keep + Deprecate | `GET /configurations?shop_identifier={id}` |
| `GET` | `/breeze/order-confirmation/call-execution-configs` | ⚠️ Keep + Deprecate | Consolidated into `GET /configurations?template=order-confirmation` |
| - | - | ⭐ New | `GET /configurations/{id}` |
| - | - | ⭐ New | `DELETE /configurations/{id}` |

**Notes**:
- **OLD endpoints must be kept** with deprecation comments for backward compatibility
- **NEW endpoints added alongside** - `/configurations` with improved structure
- File will be renamed from `call_execution_config.py` to `configurations.py`
- Template-specific endpoint consolidated via query parameters
- Both old and new endpoints will coexist during migration period

---

### 6. Leads (leads.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `GET` | `/lead/{lead_id}` | ⚠️ Keep + Deprecate | `GET /leads/{id}` |
| `POST` | `/{merchant}/{template}` | ⚠️ **NEEDS REVIEW** | **DO NOT CHANGE** - Review required first |
| `POST` | `/push/lead/v2` | ⚠️ Keep + Deprecate | `POST /leads` |

**Notes**:
- **OLD endpoints must be kept** with deprecation comments for backward compatibility
- **NEW endpoints added alongside** - plural `/leads` with improved structure
- ⚠️ `POST /{merchant}/{template}` - **NEEDS REVIEW** - Do not make changes to this endpoint yet
- Both old and new endpoints will coexist during migration period

---

### 7. Callbacks (callbacks.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `GET` | `/{provider}/callback/details` | ✅ **Keep As-Is** | Provider-specific, keep unchanged |
| `POST` | `/{provider}/callback/details` | ✅ **Keep As-Is** | Provider-specific, keep unchanged |
| `POST` | `/{provider}/callback/status` | ✅ **Keep As-Is** | Provider-specific, keep unchanged |

**Notes**:
- These are provider-specific callbacks (Twilio, etc.)
- Should remain unchanged - external integrations depend on these
- **Keep as-is - NO changes needed**

---

### 8. WebSocket (websocket.py)
| Method | Current Path | Status | New Design |
|--------|-------------|--------|------------|
| `WS` | `/{service_provider}/callback/{template}` | ✅ **Keep As-Is** | Template-specific by design |
| `WS` | `/{service_provider}/callback/{template}/v2` | ✅ **Keep As-Is** | Template-specific by design |

**Notes**:
- WebSocket endpoints are inherently template-specific (needed for routing)
- Should remain unchanged
- **Keep as-is - NO changes needed**

---

## ⚠️ Endpoints Requiring Review (DO NOT CHANGE)

### 1. **CRON Endpoint** - NEEDS REVIEW
```
GET /cron/initiate
```
**Current Usage**: Unknown - needs investigation
**Action Required**:
- ⚠️ **DO NOT CHANGE THIS ENDPOINT** until reviewed
- Investigate what this endpoint does
- Determine if still needed before making any changes
- Add to design only after investigation complete

### 2. **Lead Trigger Endpoint** - NEEDS REVIEW
```
POST /{merchant}/{template}
```
**Current Usage**: Triggers a call for a lead
**Action Required**:
- ⚠️ **DO NOT CHANGE THIS ENDPOINT** until reviewed
- Clarify if this should be `POST /leads/{id}/trigger`
- Or keep existing path for backward compatibility
- Document in migration guide after decision

---

## ✅ Summary

### Coverage Statistics
- **Total Current Endpoints**: 23
- **Covered in New Design**: 20 (87%)
- **Needs Review (DO NOT CHANGE)**: 2 (9%)
- **Intentionally Removed**: 1 (4%) - Dashboard HTML

### Endpoints by Status

#### ✅ Covered with Deprecation Strategy (17)
- All authentication endpoints
- All analytics endpoints (consolidated)
- All template CRUD (old endpoints deprecated, new endpoints added)
- All outbound numbers
- All configurations (old endpoints deprecated, new endpoints added)
- Lead endpoints (old endpoints deprecated, new endpoints added - except trigger)

#### ✅ Keep As-Is - NO Changes (4)
- All callbacks (provider-specific, external integrations depend on these)
- All websockets (template-specific by design)

#### ⚠️ Needs Review - DO NOT CHANGE (2)
1. `GET /cron/initiate` - Unknown purpose, requires investigation first
2. `POST /{merchant}/{template}` - Lead trigger endpoint, requires clarification first

#### ❌ Removed (1)
1. `GET /dashboard` - Moved to separate frontend repo

---

## 🔄 Deprecation Strategy

For the following router files, **BOTH old and new endpoints must exist**:

### 1. Templates (`template.py`)
- **Keep OLD endpoints**: `/template` (GET, POST) - Add deprecation warnings
- **Add NEW endpoints**: `/templates` (GET, POST, GET/{id}, PUT/{id}, DELETE/{id})

### 2. Call Execution Config (`call_execution_config.py` → `configurations.py`)
- **Keep OLD endpoints**: `/call-execution-config` (GET, POST, PUT) - Add deprecation warnings
- **Add NEW endpoints**: `/configurations` (GET, POST, GET/{id}, PUT/{id}, DELETE/{id})

### 3. Leads (`leads.py`)
- **Keep OLD endpoints**: `/lead/{id}` (GET), `/push/lead/v2` (POST) - Add deprecation warnings
- **Add NEW endpoints**: `/leads` (GET/{id}, POST, POST/{id}/trigger)
- **DO NOT CHANGE**: `POST /{merchant}/{template}` - Requires review first

**Migration Period**: Both old and new endpoints will coexist to allow clients to migrate gradually

---

## 📝 Action Items

### High Priority
- [ ] **Investigate `/cron/initiate` endpoint**
  - What does it do?
  - Is it still needed?
  - Where should it live in new design?

- [ ] **Clarify lead trigger endpoint**
  - Should it be `POST /leads/{id}/trigger`?
  - Or keep `POST /{merchant}/{template}` for backward compatibility?
  - Document in migration guide

### Medium Priority
- [ ] **Add CRON endpoint to design** (if needed)
- [ ] **Update IMPLEMENTATION_PLAN.md** with any missing endpoints
- [ ] **Create migration guide** for breaking changes

### Low Priority
- [ ] **Document all query parameters** for filtering
- [ ] **Add OpenAPI/Swagger specs** for all new endpoints

---

## 🎯 Recommendation

### Option 1: Add Missing Endpoints to Design (Recommended)
Update the design documents to include:
1. CRON endpoint (after investigation)
2. Lead trigger endpoint with clear documentation

### Option 2: Deprecate Missing Endpoints
If endpoints are no longer needed:
1. Document deprecation timeline
2. Add deprecation warnings
3. Remove in future version

### Option 3: Keep as Legacy Endpoints
For backward compatibility:
1. Keep old endpoints alongside new ones
2. Add deprecation warnings
3. Document migration path

---

## 📚 Related Documents
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) - Implementation tasks and phases
- [ANALYTICS_ENDPOINT_DESIGN.md](ANALYTICS_ENDPOINT_DESIGN.md) - Single analytics endpoint design
- [MULTI_SHOP_RBAC_DESIGN.md](MULTI_SHOP_RBAC_DESIGN.md) - Multi-shop access control
- [SUMMARY.md](SUMMARY.md) - High-level overview of all changes

---

**Next Steps**: Review missing endpoints and update design documents accordingly.
