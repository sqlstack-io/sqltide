"""Argon2id hashing and verification through argon2-cffi."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_HASHER: PasswordHasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash one password with argon2-cffi's default Argon2id parameters."""

    return _HASHER.hash(password)


def verify_password(*, password_hash: str, password: str) -> tuple[bool, str | None]:
    """Verify a password and return a strengthened replacement hash when needed."""

    try:
        valid: bool = _HASHER.verify(password_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False, None
    if not valid:
        return False, None
    return True, _HASHER.hash(password) if _HASHER.check_needs_rehash(password_hash) else None
