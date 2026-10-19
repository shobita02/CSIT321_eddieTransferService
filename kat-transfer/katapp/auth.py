"""Sign in to the gateway and manage the personal token it issues.

The password and token are never logged, shown or included in error messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ssl

import httpx

from .appconfig import GatewaySettings


@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    token_id: int
    username: str
    role: str
    expires_at: datetime | None

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expires_at is not None and self.expires_at <= now

    def expires_soon(self, days: int = 7, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.expires_at is not None and self.expires_at - now <= timedelta(days=days)


class AuthError(Exception):
    """kind: credentials, read_only, rate_limited, disabled, unreachable or invalid."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class GatewayAuth:
    def __init__(self, gateway: GatewaySettings, transport: httpx.BaseTransport | None = None):
        self.gateway = gateway
        self._transport = transport

    def _client(self) -> httpx.Client:
        # Same rules as katxfer.sinks.HttpSink: TLS verified, redirects cannot forward credentials.
        verify = ssl.create_default_context(cafile=self.gateway.ca_file) if self.gateway.ca_file else True
        return httpx.Client(base_url=self.gateway.url + "/", timeout=self.gateway.timeout_s,
                            follow_redirects=False, trust_env=False, verify=verify,
                            transport=self._transport)

    def _send(self, method: str, path: str, token: str | None = None, **kwargs) -> httpx.Response:
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            with self._client() as client:
                return client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise AuthError("unreachable", "Cannot reach the gateway. Check the network and try again.") from None

    @staticmethod
    def _refused(response: httpx.Response, sign_in: bool) -> AuthError:
        code = response.status_code
        if code == 401:
            return AuthError("credentials", "Incorrect username or password" if sign_in
                             else "Your sign-in has expired or was revoked")
        if code == 403:
            if sign_in and "HTTPS" in response.text:
                return AuthError("invalid", "The gateway only accepts sign-in over HTTPS")
            return AuthError("read_only", "Your account can view but not send data")
        if code == 429:
            wait = response.headers.get("retry-after", "")
            return AuthError("rate_limited", "Too many attempts. Try again"
                             + (f" in {wait} seconds." if wait.isdigit() else " later."))
        if code == 503:
            return AuthError("disabled", "Sign-in is not available on this gateway right now")
        return AuthError("invalid", f"The gateway returned HTTP {code}")

    @staticmethod
    def _session(data, token: str) -> Session:
        try:
            return Session(token=token, token_id=int(data["token_id"]), username=str(data["username"]),
                           role=str(data["role"]), expires_at=_parse_time(data.get("expires_at")))
        except (KeyError, TypeError, ValueError):
            raise AuthError("invalid", "The gateway returned an unexpected sign-in response") from None

    def login(self, username: str, password: str) -> Session:
        """A write token for this user; read-only users are refused."""
        response = self._send("POST", "v1/auth/login",
                              json={"username": username, "password": password, "role": "write"})
        if response.status_code != 200:
            raise self._refused(response, sign_in=True)
        try:
            data = response.json()
            token = data["token"]
        except (ValueError, KeyError, TypeError):
            raise AuthError("invalid", "The gateway returned an unexpected sign-in response") from None
        session = self._session(data, token)
        if session.role != "write":
            raise AuthError("read_only", "Your account can view but not send data")
        return session

    def me(self, token: str) -> Session:
        """Check a saved token; raises AuthError('credentials') once it stops working."""
        response = self._send("GET", "v1/auth/me", token=token)
        if response.status_code != 200:
            raise self._refused(response, sign_in=False)
        try:
            data = response.json()
        except ValueError:
            raise AuthError("invalid", "The gateway returned an unexpected response") from None
        return self._session(data, token)

    def logout(self, token: str) -> None:
        """Revoke the token on the server. Best effort: signing out locally always succeeds."""
        try:
            self._send("POST", "v1/auth/logout", token=token)
        except AuthError:
            pass
