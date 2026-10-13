"""Password hashing and secret handling.

Passwords: Argon2id. Session ids, extension tokens and invite codes: 32 random bytes, handed out
once, stored only as SHA-256 (spec §7). Nothing here logs or returns a secret by accident.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi defaults to Argon2id with the RFC 9106 low-memory parameters.
_hasher = PasswordHasher()

# Verified against when the email is unknown, so login timing does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only for a matching hash. Spends the same work when stored_hash is None."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def new_secret() -> str:
    """32 random bytes, URL-safe base64 (43 characters)."""
    return secrets.token_urlsafe(32)


def sha256_digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()
