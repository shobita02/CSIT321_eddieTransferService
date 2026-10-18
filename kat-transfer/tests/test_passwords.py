"""Sign-in password hashing."""
import pytest

from katgateway.passwords import hash_password, verify_password


def test_hash_round_trip_is_salted():
    first, second = hash_password("a-long-password"), hash_password("a-long-password")
    assert first != second
    assert first.startswith("scrypt$") and "a-long-password" not in first
    assert verify_password("a-long-password", first)
    assert not verify_password("a-long-passwore", first)


def test_missing_or_malformed_hash_never_verifies():
    assert not verify_password("a-long-password", None)
    assert not verify_password("a-long-password", "plain-text")
    assert not verify_password("a-long-password", "scrypt$x$8$1$AAAA$AAAA")


def test_short_password_is_rejected():
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("short")
