"""Private native settings, loopback-only startup and token administration."""
import os

import pytest

from katgateway import __main__ as launcher
from katgateway.config import DatabaseConfig, Settings


def profile():
    return Settings(DatabaseConfig(database="experimentaldata", user="csit", password="test-password"))


def test_private_settings_round_trip_and_no_overwrite(tmp_path):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    stored = launcher.load_settings(path)
    assert stored.database.password == "test-password"
    assert stored.database.database == "experimentaldata"
    assert "test-password" not in repr(stored)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        launcher.save_settings(path, profile())
    assert path.read_bytes() == before
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_setup_checks_archive_and_hides_secrets(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env.live.json"
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt: "test-password")
    monkeypatch.setattr(launcher.ArchiveStore, "counts", lambda self: {
        "experiment": 24, "experimental_data": 5277, "environment": 1})
    assert launcher.main(["--configure", "--settings", str(path)]) == 0
    out = capsys.readouterr().out
    assert "5277" in out
    assert "test-password" not in out
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt: pytest.fail("Existing settings must not prompt"))
    assert launcher.main(["--configure", "--settings", str(path)]) == 1


def test_failed_archive_check_does_not_save_profile(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env.live.json"
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt: "test-password")

    def fail(store):
        raise launcher.psycopg2.OperationalError("private connection details")

    monkeypatch.setattr(launcher.ArchiveStore, "counts", fail)
    assert launcher.main(["--configure", "--settings", str(path)]) == 1
    assert not path.exists()
    out = capsys.readouterr()
    assert "private connection details" not in out.err
    assert "test-password" not in out.err


def test_launcher_binds_loopback_and_accepts_proxy_prefix(tmp_path, monkeypatch):
    import uvicorn
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert launcher.main(["--settings", str(path)]) == 0
    assert calls[-1][1]["host"] == "127.0.0.1"
    assert calls[-1][1]["root_path"] == ""
    assert launcher.main(["--settings", str(path), "--root-path", "/csit321"]) == 0
    assert calls[-1][1]["root_path"] == "/csit321"


@pytest.mark.skipif(os.name != "posix", reason="Linux settings permissions")
def test_world_readable_settings_are_rejected(tmp_path):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        launcher.load_settings(path)


def test_admin_token_create_prints_once_and_uses_separate_login(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt: "issuer-password")

    class Store:
        def __init__(self, cfg):
            assert cfg.user == "issuer"
            assert cfg.password == "issuer-password"

        def create_token(self, username, role, expires_days, note):
            assert (username, role, expires_days, note) == ("alice", "write", 90, "laptop")
            return 7, "only-shown-once"

    monkeypatch.setattr(launcher, "ArchiveStore", Store)
    assert launcher.main([
        "--settings", str(path), "--admin-user", "issuer", "token", "create",
        "--user", "alice", "--role", "write", "--note", "laptop",
    ]) == 0
    assert capsys.readouterr().out.strip() == "Token 7 (shown once): only-shown-once"


def test_admin_list_revoke_and_user_commands(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt: "issuer-password")
    calls = []

    class Store:
        def __init__(self, cfg):
            pass

        def list_tokens(self):
            return [{"id": 4, "token_prefix": "abc12345", "username": "alice",
                     "role": "read", "revoked_at": None}]

        def revoke_token(self, selector):
            calls.append(("revoke", selector))
            return True

        def add_user(self, *args):
            calls.append(("add", *args))

        def set_user_role(self, *args):
            calls.append(("set-role", *args))
            return True

        def disable_user(self, *args):
            calls.append(("disable", *args))
            return True

    monkeypatch.setattr(launcher, "ArchiveStore", Store)
    common = ["--settings", str(path), "--admin-user", "issuer"]
    assert launcher.main([*common, "token", "list"]) == 0
    assert '"token_prefix": "abc12345"' in capsys.readouterr().out
    assert launcher.main([*common, "token", "revoke", "abc12345"]) == 0
    assert launcher.main([*common, "user", "add", "alice", "--role", "read", "--granted-by", "admin"]) == 0
    assert launcher.main([*common, "user", "set-role", "alice", "write", "--granted-by", "admin"]) == 0
    assert launcher.main([*common, "user", "disable", "alice"]) == 0
    assert calls == [
        ("revoke", "abc12345"),
        ("add", "alice", "read", "admin", None),
        ("set-role", "alice", "write", "admin"),
        ("disable", "alice"),
    ]


def test_admin_expiry_is_capped_at_90_days():
    assert launcher._expiry_days("90d") == 90
    for invalid in ("0d", "91d", "90", "1w"):
        with pytest.raises(ValueError):
            launcher._expiry_days(invalid)


def test_malformed_profile_does_not_echo_its_contents(tmp_path, capsys):
    path = tmp_path / ".env.live.json"
    path.write_text('{"PRIVATE_VALUE":', encoding="utf-8")
    path.chmod(0o600)
    assert launcher.main(["--settings", str(path), "--check"]) == 1
    assert "PRIVATE_VALUE" not in capsys.readouterr().err
