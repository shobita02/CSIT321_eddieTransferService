"""Salted scrypt password hashes for gateway sign-in accounts (standard library only)."""
import base64
import hashlib
import hmac
import secrets

N, R, P, LENGTH = 2 ** 14, 8, 1, 32
MIN_LENGTH = 12


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=LENGTH, maxmem=64 * 1024 * 1024)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Password must be at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_derive(password, salt, N, R, P))}"


# Unknown users and users without a password are checked against this, so a
# failed sign-in takes the same time whether or not the username exists.
_DUMMY = f"scrypt${N}${R}${P}${_b64(b'0' * 16)}${_b64(b'0' * LENGTH)}"


def verify_password(password: str, stored: str | None) -> bool:
    known = stored is not None
    try:
        scheme, n, r, p, salt, expected = (stored if known else _DUMMY).split("$")
        if scheme != "scrypt":
            raise ValueError
        actual = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(actual, base64.b64decode(expected)) and known
    except ValueError:
        _derive(password, b"0" * 16, N, R, P)
        return False
