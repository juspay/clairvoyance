"""
Breeze Buddy specific RBAC token management.
Handles JWT token creation and verification with Breeze Buddy's RBAC model.
"""

from datetime import timedelta
from typing import List, Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.logger import logger
from app.core.security.jwt import jwt_manager
from app.core.security.token_revocation import is_token_revoked
from app.schemas import UserInfo, UserRole

# HTTP Bearer security scheme
security = HTTPBearer()


class BreezeBuddyRBACTokenManager:
    """Breeze Buddy specific RBAC token manager."""

    def __init__(self):
        self.jwt_manager = jwt_manager

    def create_access_token_with_rbac(
        self,
        user_id: str,
        username: str,
        role: UserRole,
        reseller_ids: List[str],
        merchant_ids: List[str],
        email: Optional[str] = None,
        owner_id: Optional[str] = None,
        expires_delta: Optional[timedelta] = None,
        src: Optional[str] = None,
    ) -> str:
        """
        Create a JWT access token with Breeze Buddy RBAC information.

        Args:
            user_id: User's unique identifier
            username: Username
            role: User's role (admin, reseller, merchant, user)
            reseller_ids: List of accessible reseller IDs (["*"] for all resellers)
            merchant_ids: List of accessible merchant identifiers (["*"] for all merchants under the reseller(s))
            email: User's email (optional)
            expires_delta: Optional custom expiration time
            src: Optional provenance marker recorded on the token (e.g.
                "nautilus" for Shopify-admin launch tokens)

        Returns:
            Encoded JWT token string with RBAC data
        """
        # Get permissions based on role
        permissions = self._get_permissions_for_role(role)

        # Build JWT payload with Breeze Buddy RBAC data
        payload = {
            "sub": user_id,
            "username": username,
            "role": role.value if isinstance(role, UserRole) else role,
            "email": email,
            "reseller_ids": reseller_ids,
            "merchant_ids": merchant_ids,
            "permissions": permissions,
            "owner_id": owner_id,
        }

        if src:
            payload["src"] = src

        # Use the generic JWT manager to create the token
        return self.jwt_manager.create_access_token(payload, expires_delta)

    async def verify_rbac_token(self, token: str) -> UserInfo:
        """
        Verify and decode a JWT token with Breeze Buddy RBAC information.

        Also enforces server-side revocation (denylist) and a per-request user
        liveness recheck, so a logged-out/forged token can be killed and a
        disabled/deleted account's tokens stop working (PT-22).

        Args:
            token: JWT token string

        Returns:
            UserInfo object containing user and RBAC information

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            payload = pyjwt.decode(
                token,
                self.jwt_manager.secret_key,
                algorithms=[self.jwt_manager.algorithm],
            )

            # Reject non-RBAC token types up front. Widget tokens (typ="widget",
            # see ``widget_token.py``) and demo tokens (``demo: true``, see
            # ``demo_token.py``) are signed by the same key but must never be
            # accepted on RBAC routes — they carry no reseller/merchant scopes
            # and would otherwise authenticate as a UserInfo with empty
            # arrays. All existing RBAC/s2s tokens are minted without these
            # claims, so the check is backward-compatible.
            if payload.get("typ") == "widget":
                logger.warning("RBAC verifier: rejected widget token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if payload.get("demo") is True:
                logger.warning("RBAC verifier: rejected demo token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Check if token has expired (redundant with jwt.decode but explicit)
            exp = payload.get("exp")
            if exp is None:
                logger.warning("Token missing expiration claim")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Validate required identity claims
            user_id = payload.get("sub")
            username = payload.get("username")
            if not user_id or not username:
                logger.warning("Token missing required identity claims (sub/username)")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if user_id == "legacy_admin":
                logger.warning("RBAC verifier: rejected legacy admin token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Server-side revocation: a token that was logged out or explicitly
            # revoked is rejected regardless of its (still-valid) signature.
            if await is_token_revoked(token):
                logger.warning(f"RBAC verifier: rejected revoked token for {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Liveness recheck: a disabled/deleted account's outstanding tokens
            # (including long-lived S2S tokens, whose ``sub`` is the minting
            # user) must stop working immediately.
            #
            # Exception: launch tokens (POST /auth/launch-token) authenticate a
            # *virtual* merchant subject ("merchant:<id>") that has no backing
            # ``users`` row by design, so a users-table liveness probe would
            # always fail closed for them. They are bounded instead by a fixed
            # 60-minute TTL, the merchant liveness check performed at mint time,
            # the signed ``src`` provenance claim, and the revocation denylist
            # checked just above (which still applies to them). Keep this
            # prefix in lockstep with the ``sub`` minted in launch_token_handler.
            # Imported lazily to avoid a module-load import cycle with the DB
            # accessor layer.
            if not str(user_id).startswith("merchant:"):
                from app.database.accessor.breeze_buddy.users import is_user_active

                if not await is_user_active(user_id):
                    logger.warning(
                        f"RBAC verifier: rejected token for inactive/deleted user {user_id}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Could not validate credentials",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

            # Extract Breeze Buddy RBAC data
            # Normalize legacy "shop" role to "user" for old JWTs still in circulation
            role_str = payload.get("role")
            if role_str == "shop":
                role_str = "user"

            user_info = UserInfo(
                id=user_id,
                username=username,
                role=UserRole(role_str),
                email=payload.get("email"),
                reseller_ids=payload.get("reseller_ids", []),
                merchant_ids=payload.get("merchant_ids", []),
                permissions=payload.get("permissions", []),
                owner_id=payload.get("owner_id"),
            )

            logger.info(
                f"RBAC token verified for user: {user_info.username} "
                f"(role: {user_info.role}, resellers: {user_info.reseller_ids}, merchants: {user_info.merchant_ids})"
            )
            return user_info

        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except pyjwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except ValueError as e:
            logger.warning(f"Invalid role in token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Unexpected error verifying RBAC token: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def _get_permissions_for_role(self, role: UserRole) -> List[str]:
        """
        Get Breeze Buddy permissions list based on user role.

        Args:
            role: User's role

        Returns:
            List of permission strings for Breeze Buddy
        """
        role_str = role.value if isinstance(role, UserRole) else role

        if role_str == "admin":
            return [
                "read:all",
                "write:all",
                "delete:all",
                "analytics:all",
                "configurations:all",
                "merchants:all",
            ]
        elif role_str == "reseller":
            return [
                "read:assigned_merchants",
                "write:assigned_merchants",
                "analytics:assigned_merchants",
                "configurations:assigned_merchants",
            ]
        elif role_str == "merchant":
            return [
                "read:own_data",
                "write:own_data",
                "analytics:own",
                "configurations:read",
            ]
        elif role_str == "user":
            return [
                "read:own_data",
                "analytics:own",
            ]
        else:
            return []


# Initialize Breeze Buddy RBAC token manager
rbac_token_manager = BreezeBuddyRBACTokenManager()


# FastAPI dependency for Breeze Buddy RBAC authentication
async def get_current_user_with_rbac(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserInfo:
    """
    FastAPI dependency to get current authenticated user with Breeze Buddy RBAC information.

    Args:
        credentials: HTTP Bearer credentials from request header

    Returns:
        UserInfo object containing user and RBAC information

    Raises:
        HTTPException: If token is invalid or missing
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await rbac_token_manager.verify_rbac_token(credentials.credentials)


async def get_user_from_websocket(websocket: WebSocket) -> UserInfo:
    """Authenticate a WebSocket connection with the standard RBAC bearer token.

    The FastAPI ``Depends(HTTPBearer())`` guards are HTTP-only, so WebSocket
    routes call this directly after ``accept()``. Token sources, in order:
    the ``Authorization: Bearer`` header, then a ``token`` query parameter
    (browser WebSocket clients cannot set headers).

    Raises:
        HTTPException: 401 when the token is missing or invalid — callers
        translate this into a WebSocket close.
    """
    auth_header = websocket.headers.get("authorization") or ""
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[len("bearer ") :].strip()
    if not token:
        token = (websocket.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
        )
    return await rbac_token_manager.verify_rbac_token(token)


async def get_active_user(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UserInfo:
    """
    FastAPI dependency to get current active user.

    Args:
        current_user: Current user from token

    Returns:
        UserInfo if user is active

    Raises:
        HTTPException: If user is inactive
    """
    # Note: is_active check would need database lookup
    # For now, we assume token = active user
    return current_user
