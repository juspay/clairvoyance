import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import (
    AUTOMATIC_CONNECT_BLOCKED_ORIGINS,
    ENABLE_LIGHTHOUSE_AUTH,
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    LIGHTHOUSE_JWT_SECRET,
)
from app.core.logger import logger
from app.schemas import TokenData

# Common credential exception
CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


class JWTManager:
    """JWT token management class"""

    def __init__(self):
        self.secret_key = JWT_SECRET_KEY
        self.algorithm = JWT_ALGORITHM
        self.access_token_expire_minutes = JWT_ACCESS_TOKEN_EXPIRE_MINUTES

    def create_access_token(
        self, data: Dict[str, Any], expires_delta: Optional[timedelta] = None
    ) -> str:
        """
        Create a JWT access token

        Args:
            data: Dictionary containing the payload data
            expires_delta: Optional custom expiration time

        Returns:
            Encoded JWT token string
        """
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(
                minutes=self.access_token_expire_minutes
            )

        to_encode.update({"exp": expire, "iat": datetime.utcnow()})

        try:
            encoded_jwt = jwt.encode(
                to_encode, self.secret_key, algorithm=self.algorithm
            )
            logger.info(
                f"JWT token created successfully for user: {data.get('user_id', 'unknown')}"
            )
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error creating JWT token: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not create access token",
            )

    def verify_token(self, token: str) -> TokenData:
        """
        Verify and decode a JWT token

        Args:
            token: JWT token string

        Returns:
            TokenData object containing the decoded payload

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])

            # Check if token has expired
            exp = payload.get("exp")
            if exp is None:
                logger.warning("Token missing expiration claim")
                raise CREDENTIALS_EXCEPTION

            if datetime.utcnow() > datetime.fromtimestamp(exp):
                logger.warning("Token has expired")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Extract token data
            token_data = TokenData(
                user_id=payload.get("user_id"),
                username=payload.get("username"),
                email=payload.get("email"),
                scopes=payload.get("scopes", []),
            )

            logger.info(f"Token verified successfully for user: {token_data.user_id}")
            return token_data

        except jwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise CREDENTIALS_EXCEPTION
        except Exception as e:
            logger.error(f"Unexpected error verifying token: {e}")
            raise CREDENTIALS_EXCEPTION

    def verify_breeze_token(self, breeze_token: str) -> Dict[str, Any]:
        """
        Verify the breezeToken JWT from lighthouse

        Args:
            breeze_token: JWT token string from lighthouse

        Returns:
            Dictionary containing user context from the token

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            # Use lighthouse's JWT secret (same as ACCESS_JWT_SECRET from lighthouse)
            # Decode the JWT token using lighthouse's secret
            if not LIGHTHOUSE_JWT_SECRET:
                logger.error("LIGHTHOUSE_JWT_SECRET is not configured")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Server misconfiguration",
                )
            payload = jwt.decode(
                breeze_token,
                LIGHTHOUSE_JWT_SECRET,
                algorithms=["HS256"],  # Same algorithm lighthouse uses
            )

            # Extract user information from the decoded payload
            user_context = {
                "name": payload.get("name"),
                "email": payload.get("email"),
                "scopes": payload.get("scopes", []),
                "merchantName": payload.get("merchantName"),
                "merchantId": payload.get("merchantId"),
                "shopURL": payload.get("shopURL"),
                "context": payload.get("context"),
                "platformToken": payload.get("platformToken"),
                "breezeTokenData": payload.get("breezeTokenData"),
                "resellerId": payload.get("resellerId"),
            }

            logger.info(user_context)
            return user_context

        except jwt.ExpiredSignatureError:
            logger.warning("Breeze token has expired")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Breeze token has expired",
            )
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid breeze token: {e}")
            raise CREDENTIALS_EXCEPTION
        except Exception as e:
            logger.error(f"Unexpected error verifying breeze token: {e}")
            raise CREDENTIALS_EXCEPTION


# Initialize JWT manager
jwt_manager = JWTManager()

# HTTP Bearer security scheme
security = HTTPBearer()


# Dependency functions for FastAPI
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> TokenData:
    """
    FastAPI dependency to get current authenticated user from JWT token

    Args:
        credentials: HTTP Bearer credentials from request header

    Returns:
        TokenData object containing user information

    Raises:
        HTTPException: If token is invalid or missing
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return jwt_manager.verify_token(credentials.credentials)


# Breeze Authentication Functions


async def validate_automatic_request(raw_request: Request) -> Optional[Dict[str, Any]]:
    """
    FastAPI dependency to get user context from breezeToken in request body

    Args:
        raw_request: FastAPI Request object to read raw body

    Returns:
        Dictionary containing user context from verified breezeToken if ENABLE_LIGHTHOUSE_AUTH is True,
        None if ENABLE_LIGHTHOUSE_AUTH is False

    Raises:
        HTTPException: If ENABLE_LIGHTHOUSE_AUTH is True and token is missing, invalid, or expired
    """

    origin = raw_request.headers.get("origin")
    referer = raw_request.headers.get("referer")
    if origin:
        if any(blocked in origin for blocked in AUTOMATIC_CONNECT_BLOCKED_ORIGINS):
            raise HTTPException(
                status_code=403,
                detail=f"Access from origin '{origin}' is forbidden.",
            )
    elif referer:
        if any(blocked in referer for blocked in AUTOMATIC_CONNECT_BLOCKED_ORIGINS):
            raise HTTPException(
                status_code=403,
                detail=f"Access from referer '{referer}' is forbidden.",
            )

    # If authentication is disabled, return None
    if not ENABLE_LIGHTHOUSE_AUTH:
        return None

    try:
        body = await raw_request.body()
        data = json.loads(body)
        breeze_token = data.get("breezeToken")
        mode = data.get("mode")
        if mode == "TEST":  # Skip auth for test mode
            logger.info("Test mode detected, skipping auth validation")
            return None
        if not breeze_token:
            logger.error("Missing breezeToken in request body")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="breezeToken is required in request body",
            )

        user_context = jwt_manager.verify_breeze_token(breeze_token)
        logger.info(
            f"Body-based breeze token verified for user: {user_context['email']}"
        )
        return user_context

    except json.JSONDecodeError:
        logger.error("Invalid JSON in request body")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON in request body",
        )
    except Exception as e:
        logger.error(f"Unexpected error reading request body: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error processing request body",
        )
