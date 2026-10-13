"""Pure unit tests for hashing and secrets. No database needed."""

import hashlib

from app.security import (
    hash_password,
    new_secret,
    password_needs_rehash,
    sha256_digest,
    verify_password,
)


def test_password_hash_is_argon2id_and_salted() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first.startswith("$argon2id$")
    assert first != second  # random salt
    assert "correct horse" not in first


def test_verify_password() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password(stored, "correct horse battery staple") is True
    assert verify_password(stored, "wrong") is False
    assert verify_password(stored, "") is False


def test_verify_password_without_a_stored_hash_is_always_false() -> None:
    assert verify_password(None, "anything") is False
    # Even the internal timing-equaliser password must not log anyone in.
    assert verify_password(None, "timing-equaliser-not-a-real-password") is False


def test_verify_password_rejects_garbage_hash() -> None:
    assert verify_password("not-a-hash", "x") is False


def test_fresh_hash_does_not_need_rehash() -> None:
    assert password_needs_rehash(hash_password("x" * 12)) is False


def test_new_secret_is_32_random_bytes() -> None:
    secret = new_secret()
    assert len(secret) == 43  # 32 bytes, unpadded URL-safe base64
    assert new_secret() != secret
    assert len({new_secret() for _ in range(200)}) == 200


def test_sha256_digest_is_plain_sha256_of_the_text() -> None:
    assert sha256_digest("abc") == hashlib.sha256(b"abc").digest()
    assert len(sha256_digest("abc")) == 32
