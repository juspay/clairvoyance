---
paths:
  - "app/api/**/*.py"
---

# API Endpoint Rules

## Structure
- Endpoints are thin: validate auth via `Depends()`, delegate to handler functions for business logic
- Use `response_model=` for automatic serialization. Define schemas in `app/schemas/`
- Status codes: 201 for POST creation, 204 for DELETE, 403 for auth failures, 500 for server errors

## Authentication
- Automatic agent: JWT validation via `validate_automatic_request` dependency
- Breeze Buddy: JWT + RBAC via `get_current_user_with_rbac` dependency
- Admin-only endpoints: Call `require_admin_access(current_user, "action description")`

## Error Responses
- Use `HTTPException(status_code=..., detail="...")` for client errors
- Use `JSONResponse(status_code=500, content={"error": ...})` for server errors
- Include meaningful detail messages for debugging
