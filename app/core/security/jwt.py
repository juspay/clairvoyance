from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import jwt
from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.core.config import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
)
from app.core.logger import logger
from app.schemas import TokenData


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
        credentials_exception = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])

            # Check if token has expired
            exp = payload.get("exp")
            if exp is None:
                logger.warning("Token missing expiration claim")
                raise credentials_exception

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
            raise credentials_exception
        except Exception as e:
            logger.error(f"Unexpected error verifying token: {e}")
            raise credentials_exception


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
