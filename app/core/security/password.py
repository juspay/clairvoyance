"""
Password hashing and verification utilities using bcrypt.
Provides secure password storage and validation for user authentication.
"""

import asyncio
from typing import Tuple

import bcrypt

from app.core.logger import logger


class PasswordHasher:
    """Password hashing and verification using bcrypt"""

    # bcrypt work factor (cost parameter)
    # 12 rounds provides strong security while maintaining reasonable performance
    # Each increment doubles the computation time
    BCRYPT_ROUNDS = 12

    @classmethod
    def hash_password(cls, password: str) -> str:
        """
        Hash a password using bcrypt with salt.

        Args:
            password: Plain text password to hash

        Returns:
            Hashed password string (includes salt)

        Raises:
            ValueError: If password is empty or invalid
        """
        if not password or not isinstance(password, str):
            raise ValueError("Password must be a non-empty string")

        if len(password) > 72:
            # bcrypt has a 72-byte password limit
            logger.warning(
                "Password exceeds 72 characters, will be truncated by bcrypt"
            )

        try:
            # Generate salt and hash password
            salt = bcrypt.gensalt(rounds=cls.BCRYPT_ROUNDS)
            password_bytes = password.encode("utf-8")
            hashed = bcrypt.hashpw(password_bytes, salt)

            # Return as string
            return hashed.decode("utf-8")

        except Exception as e:
            logger.error(f"Error hashing password: {e}")
            raise ValueError(f"Failed to hash password: {e}")

    @classmethod
    def verify_password(cls, plain_password: str, hashed_password: str) -> bool:
        """
        Verify a password against a hash.

        Args:
            plain_password: Plain text password to verify
            hashed_password: Hashed password to compare against

        Returns:
            True if password matches, False otherwise

        Raises:
            ValueError: If inputs are invalid
        """
        if not plain_password or not isinstance(plain_password, str):
            raise ValueError("Password must be a non-empty string")

        if not hashed_password or not isinstance(hashed_password, str):
            raise ValueError("Hash must be a non-empty string")

        try:
            password_bytes = plain_password.encode("utf-8")
            hash_bytes = hashed_password.encode("utf-8")

            # bcrypt.checkpw handles timing attack resistance
            return bcrypt.checkpw(password_bytes, hash_bytes)

        except Exception as e:
            logger.error(f"Error verifying password: {e}")
            # Return False instead of raising to avoid information leakage
            return False

    @classmethod
    def hash_and_salt(cls, password: str) -> Tuple[str, str]:
        """
        Hash password and return both hash and salt (for legacy systems).
        Note: bcrypt includes salt in the hash, so separate salt is not needed.
        This method is provided for compatibility only.

        Args:
            password: Plain text password

        Returns:
            Tuple of (hashed_password, salt) - both are the same for bcrypt
        """
        hashed = cls.hash_password(password)
        return hashed, hashed  # bcrypt includes salt in hash


# Convenience functions
def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password string

    Example:
        >>> hashed = hash_password("my_secure_password")
        >>> print(hashed)
        $2b$12$LQv3c1yqBWVHxkd0LHAkCO...
    """
    return PasswordHasher.hash_password(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash.

    Args:
        plain_password: Plain text password to check
        hashed_password: Hashed password from database

    Returns:
        True if password matches, False otherwise

    Example:
        >>> hashed = hash_password("my_secure_password")
        >>> verify_password("my_secure_password", hashed)
        True
        >>> verify_password("wrong_password", hashed)
        False
    """
    return PasswordHasher.verify_password(plain_password, hashed_password)


def generate_password_hash(password: str) -> str:
    """
    Alias for hash_password() for compatibility.

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    return hash_password(password)


def check_password_hash(hashed_password: str, password: str) -> bool:
    """
    Alias for verify_password() with reversed parameters for compatibility.

    Args:
        hashed_password: Hashed password from database
        password: Plain text password to check

    Returns:
        True if password matches, False otherwise
    """
    return verify_password(password, hashed_password)


# Async wrappers -- use these from any ``async def`` call site.
#
# ``bcrypt.hashpw``/``bcrypt.checkpw`` at BCRYPT_ROUNDS=12 take ~239-240ms on
# typical hardware -- longer than the 166ms Plivo call that caused the
# orphan-alert P1 (see app/core/concurrency.py). Each API pod runs a single
# event loop with a single worker, so calling either synchronously from an
# ``async def`` freezes every other in-flight call for that duration. bcrypt
# releases the GIL, so offloading to a thread via ``asyncio.to_thread``
# genuinely unblocks the loop even on a 1-core pod.
async def hash_password_async(password: str) -> str:
    """Hash a password using bcrypt, off the event loop. See module note above."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash, off the event loop. See module note above."""
    return await asyncio.to_thread(verify_password, plain_password, hashed_password)
