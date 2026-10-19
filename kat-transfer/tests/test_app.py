"""Desktop app logic without a window: sign-in, saved token, background service, refused tokens."""
import logging
import sqlite3
import time

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

from conftest import remote_count
from katapp import tokenstore
from katapp.appconfig import GatewaySettings, gateway_settings
from katapp.auth import AuthError, GatewayAuth, Session
from katapp.controller import TOKEN_ENV, TransferController
from katgateway.passwords import hash_password
from katgateway.storage import ArchiveStore
from katxfer.config import LocalConfig, RemoteConfig, ServiceConfig, ZmqConfig
from katxfer.service import TransferService
from katxfer.sinks import GatewayAuthError, GatewayError

PASSWORD = "lab-laptop-password"
LOCAL = GatewaySettings("http://127.0.0.1:8080", None, 5.0)


def mock_auth(handler):
    return GatewayAuth(LOCAL, transport=httpx.MockTransport(handler))


def issued(**extra):
    return {"token": "issued-token-0123456789abcdef", "token_id": 7, "username": "alice",
            "role": "write", "expires_at": "2027-01-01T00:00:00Z", **extra}


# -- config ---------------------------------------------------------------------


def test_gateway_settings_need_no_token_and_keep_tls_rules(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[remote]\nurl = "https://archive.example/csit321/"\nca_file = "ca.pem"\n', encoding="utf-8")
    settings = gateway_settings(path)
    assert settings.url == "https://archive.example/csit321"
    assert settings.ca_file == str((tmp_path / "ca.pem").resolve())
    path.write_text('[remote]\nurl = "http://archive.example"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="HTTPS"):
        gateway_settings(path)


# -- auth -----------------------------------------------------------------------


def test_login_returns_a_session_and_hides_secrets(caplog):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=issued())

    with caplog.at_level(logging.DEBUG):
        session = mock_auth(handler).login("alice", PASSWORD)
    assert (session.username, session.role, session.token_id) == ("alice", "write", 7)
    assert session.expires_at.year == 2027 and not session.expired()
    assert b'"role":"write"' in seen[0].content.replace(b" ", b"")
    assert session.token not in repr(session)
    assert PASSWORD not in caplog.text and session.token not in caplog.text


@pytest.mark.parametrize("status, kind, text", [
    (401, "credentials", "Incorrect username or password"),
    (403, "read_only", "Your account can view but not send data"),
    (429, "rate_limited", "Try again in 30 seconds"),
    (503, "disabled", "not available"),
    (302, "invalid", "HTTP 302"),
])
def test_login_errors_are_plain_and_never_echo_the_password(status, kind, text):
    def handler(request):
        return httpx.Response(status, headers={"Retry-After": "30", "Location": "https://elsewhere/"},
                              json={"detail": "x"})

    with pytest.raises(AuthError) as caught:
        mock_auth(handler).login("alice", PASSWORD)
    assert caught.value.kind == kind
    assert text in caught.value.message
    assert PASSWORD not in str(caught.value)


def test_unreachable_gateway_and_read_only_response():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(AuthError) as caught:
        mock_auth(down).login("alice", PASSWORD)
    assert caught.value.kind == "unreachable"

    with pytest.raises(AuthError) as caught:
        mock_auth(lambda request: httpx.Response(200, json=issued(role="read"))).login("alice", PASSWORD)
    assert caught.value.kind == "read_only"


def sign_in_user(remote, username="lab-user", role="write"):
    store = ArchiveStore(remote)
    store.add_user(username, role, "test-admin")
    assert store.set_user_password(username, hash_password(PASSWORD))
    return store


def test_sign_in_me_and_logout_against_the_real_gateway(remote, api_url):
    sign_in_user(remote)
    sign_in_user(remote, "viewer", "read")
    auth = GatewayAuth(GatewaySettings(api_url, None, 5.0))

    with pytest.raises(AuthError) as caught:
        auth.login("lab-user", "wrong-password-here")
    assert caught.value.kind == "credentials"
    with pytest.raises(AuthError) as caught:
        auth.login("viewer", PASSWORD)
    assert caught.value.kind == "read_only"

    session = auth.login("lab-user", PASSWORD)
    assert auth.me(session.token).username == "lab-user"
    auth.logout(session.token)
    with pytest.raises(AuthError) as caught:
        auth.me(session.token)
    assert caught.value.kind == "credentials"


# -- token store ----------------------------------------------------------------


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.items.pop((service, username), None) is None:
            raise PasswordDeleteError("missing")


class BrokenKeyring(MemoryKeyring):
    def _fail(self, *args):
        raise KeyringError("no backend")

    get_password = set_password = delete_password = _fail


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def session_expiring(at):
    return Session(token="saved-token-0123456789abcdef", token_id=3, username="alice", role="write",
                   expires_at=at)


def test_token_store_round_trip_and_expiry(memory_keyring):
    from datetime import datetime, timedelta, timezone
    url = "https://archive.example/csit321"
    future = datetime.now(timezone.utc) + timedelta(days=30)
    assert tokenstore.save(url, session_expiring(future))
    assert list(memory_keyring.items) == [("kat-transfer", "session:" + url)]
    assert tokenstore.load(url) == session_expiring(future)
    assert tokenstore.load("https://other.example") is None

    tokenstore.clear(url)
    assert tokenstore.load(url) is None

    tokenstore.save(url, session_expiring(datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert tokenstore.load(url) is None
    assert not memory_keyring.items, "an expired token is deleted, not kept"


def test_token_store_without_a_credential_backend_does_not_raise():
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    try:
        assert not tokenstore.save("https://x.example", session_expiring(None))
        assert tokenstore.load("https://x.example") is None
        tokenstore.clear("https://x.example")
    finally:
        keyring.set_keyring(previous)


# -- service and background runner ------------------------------------------------


def offline_service(kat_db):
    cfg = ServiceConfig(LocalConfig(kat_db), RemoteConfig(token="offline-token-0123456789"),
                        ZmqConfig(enabled=False), retry_base_s=0.01, retry_max_s=0.01)
    return TransferService(cfg)


def test_refused_token_stops_retrying_but_network_errors_retry(kat_db, monkeypatch):
    svc = offline_service(kat_db)
    attempts = []

    def flaky(trigger):
        attempts.append(trigger)
        if len(attempts) < 3:
            raise GatewayError("Gateway connection failed")

    monkeypatch.setattr(svc, "drain", flaky)
    svc._drain_with_retry("sweep")
    assert len(attempts) == 3 and svc.stats.failures == 2

    def refused(trigger):
        attempts.append(trigger)
        raise GatewayAuthError("Gateway refused the API token (HTTP 401)")

    monkeypatch.setattr(svc, "drain", refused)
    with pytest.raises(GatewayAuthError):
        svc._drain_with_retry("sweep")
    assert len(attempts) == 4


def write_config(tmp_path, kat_db, api_url):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[local]\npath = "{kat_db.as_posix()}"\nbatch_size = 7\n\n'
        f'[remote]\nurl = "{api_url}"\ntoken = "${{{TOKEN_ENV}}}"\ntimeout_s = 5\n\n'
        '[zmq]\nenabled = false\n\n'
        '[service]\nsweep_interval_s = 0.3\nretry_base_s = 0.1\nretry_max_s = 0.2\n'
        'origin = "fake"\n',
        encoding="utf-8",
    )
    return path


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_signed_in_token_transfers_until_stopped(remote, api_url, kat_db, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    sign_in_user(remote)
    session = GatewayAuth(GatewaySettings(api_url, None, 5.0)).login("lab-user", PASSWORD)

    controller = TransferController(write_config(tmp_path, kat_db, api_url), session)
    controller.start()
    try:
        assert TOKEN_ENV not in __import__("os").environ, "token is not left in the environment"
        assert wait_for(lambda: remote_count(remote, "experimental_data") == 20)
        # The archive commits before the client counts the reply, so wait for the count too.
        assert wait_for(lambda: controller.snapshot()["experimental_sent"] == 20)
    finally:
        controller.stop()
    assert controller.join(5), "Quit must finish within a few seconds"
    assert ("stopped", None) in controller.get_events()
    assert session.token not in caplog.text


def test_revoked_token_ends_the_service_without_endless_retries(remote, api_url, kat_db, tmp_path):
    store = sign_in_user(remote)
    session = GatewayAuth(GatewaySettings(api_url, None, 5.0)).login("lab-user", PASSWORD)
    controller = TransferController(write_config(tmp_path, kat_db, api_url), session)
    controller.start()
    try:
        assert wait_for(lambda: remote_count(remote, "experimental_data") == 20)
        assert store.revoke_token_by_id(session.token_id)
        with sqlite3.connect(kat_db) as conn:
            conn.execute(
                'INSERT INTO ExperimentalData (systemid, run, "row", timestamp, source, data, xfer) '
                "VALUES ('K4-RIG-01', 1, 99, '2026-10-01 10:00:00.000', 'ADC-CH0', '{}', NULL)"
            )
        assert controller.join(10), "a refused token must stop the service on its own"
    finally:
        controller.stop()
        controller.join(5)
    events = controller.get_events()
    assert any(kind == "auth_failed" for kind, _ in events)
    assert controller.snapshot()["failures"] == 0
    assert remote_count(remote, "experimental_data") == 20
