"""Password handling: bcrypt primitives and the shared strength policy.

Two concerns, deliberately kept in separate modules but exported from one
package so callers import from a single place:

- ``password``        -> bcrypt hashing/verification (``hash_password``,
                         ``verify_password``, ``DUMMY_PASSWORD_HASH``)
- ``password_policy`` -> the strength rules enforced wherever a password is
                         set (``validate_password_strength``)

Import from the package (``from app.core.security.password import ...``)
rather than reaching into the submodules, so the internal split can change
without touching call sites.
"""

from app.core.security.password.password import (
    DUMMY_PASSWORD_HASH,
    PasswordHasher,
    check_password_hash,
    generate_password_hash,
    hash_password,
    verify_password,
)
from app.core.security.password.password_policy import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    validate_password_strength,
)

__all__ = [
    "DUMMY_PASSWORD_HASH",
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "PasswordHasher",
    "check_password_hash",
    "generate_password_hash",
    "hash_password",
    "validate_password_strength",
    "verify_password",
]
