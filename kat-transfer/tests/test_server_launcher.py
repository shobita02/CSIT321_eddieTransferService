"""Private native settings and loopback-only server startup."""
import os

import pytest

from conftest import WRITE_TOKEN, READ_TOKEN
from katgateway import __main__ as launcher
from katgateway.config import DatabaseConfig, Settings


def profile():
    return Settings(DatabaseConfig(database="experimentaldata", user="csit", password="test-password"),
                    WRITE_TOKEN, READ_TOKEN)


def test_private_settings_round_trip_and_no_overwrite(tmp_path):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    stored = launcher.load_settings(path)
    assert stored.database.password == "test-password"
    assert stored.database.database == "experimentaldata"
    assert stored.read_token == READ_TOKEN
    assert stored.write_token == WRITE_TOKEN
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
    stored = launcher.load_settings(path)
    assert "5277" in out
    assert "test-password" not in out
    assert stored.write_token not in out
    assert stored.read_token not in out
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


def test_print_token_returns_only_selected_token(tmp_path, capsys):
    path = tmp_path / ".env.live.json"
    launcher.save_settings(path, profile())
    assert launcher.main(["--settings", str(path), "--print-token", "read"]) == 0
    assert capsys.readouterr().out.strip() == READ_TOKEN


def test_malformed_profile_does_not_echo_its_contents(tmp_path, capsys):
    path = tmp_path / ".env.live.json"
    path.write_text('{"PRIVATE_VALUE":', encoding="utf-8")
    path.chmod(0o600)
    assert launcher.main(["--settings", str(path), "--check"]) == 1
    assert "PRIVATE_VALUE" not in capsys.readouterr().err
