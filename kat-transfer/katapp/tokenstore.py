"""Keep the signed-in token in the operating system's credential store between runs.

Windows Credential Manager on the lab laptops. Never in config files, .env files or logs.
"""

from __future__ import annotations

import json
import logging

import keyring
from keyring.errors import KeyringError

from .auth import Session, _parse_time

SERVICE = "kat-transfer"
log = logging.getLogger("katapp.tokenstore")


def _account(url: str) -> str:
    return "session:" + url.rstrip("/")


def load(url: str) -> Session | None:
    """The saved, unexpired session for this gateway, or None."""
    try:
        raw = keyring.get_password(SERVICE, _account(url))
    except (KeyringError, RuntimeError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        session = Session(token=data["token"], token_id=int(data["token_id"]), username=data["username"],
                          role=data["role"], expires_at=_parse_time(data.get("expires_at")))
    except (ValueError, KeyError, TypeError):
        clear(url)
        return None
    if session.expired() or session.role != "write":
        clear(url)
        return None
    return session


def save(url: str, session: Session) -> bool:
    """False if this computer has no credential store; the user then signs in each time."""
    payload = json.dumps({
        "token": session.token, "token_id": session.token_id, "username": session.username,
        "role": session.role,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
    })
    try:
        keyring.set_password(SERVICE, _account(url), payload)
        return True
    except (KeyringError, RuntimeError):
        log.warning("No credential store available; the token will not be remembered")
        return False


def clear(url: str) -> None:
    try:
        keyring.delete_password(SERVICE, _account(url))
    except (KeyringError, RuntimeError):
        pass
