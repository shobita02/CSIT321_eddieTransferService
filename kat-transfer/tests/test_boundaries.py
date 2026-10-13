"""Batch and reverse-proxy boundaries used by the deployment templates."""
import sqlite3

import httpx
from fastapi.testclient import TestClient

from conftest import WRITE_TOKEN, READ_TOKEN
from katgateway.app import create_app
from katgateway.config import DatabaseConfig, Settings
from katxfer.config import RemoteConfig
from katxfer.localdb import LocalKatDb
from katxfer.sinks import HttpSink


def test_large_parent_lookup_keeps_all_runs(kat_db):
    keys = {("BOUNDARY", run) for run in range(1000)}
    with sqlite3.connect(kat_db) as conn:
        conn.executemany("INSERT INTO Experiment (systemid, run, description) VALUES (?, ?, 'boundary')", sorted(keys))
    parents = LocalKatDb(kat_db).experiments_for(keys)
    assert {(row.systemid, row.run) for row in parents} == keys


def test_gateway_url_keeps_reverse_proxy_prefix():
    seen = []
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"counts": {"experiment": 0, "experimental_data": 0, "environment": 0}})
    sink = HttpSink(RemoteConfig(url="https://example.test/csit321", token=WRITE_TOKEN), "fake", "host")
    sink._client = httpx.Client(base_url=sink.cfg.url + "/", transport=httpx.MockTransport(respond))
    try:
        sink.counts()
        assert seen == ["/csit321/v1/status"]
    finally:
        sink.close()


def test_body_limit_and_documentation_work_with_live_prefix():
    class Store:
        pass
    app = create_app(Settings(DatabaseConfig(), WRITE_TOKEN, READ_TOKEN), Store())
    with TestClient(app, root_path="/csit321") as client:
        response = client.get("/csit321/docs")
        assert response.status_code == 200
        assert "/csit321/openapi.json" in response.text
        response = client.post("/csit321/v1/ingest/experiments",
                               headers={"Authorization": "Bearer " + WRITE_TOKEN},
                               content=b" " * (8 * 1024 * 1024 + 1))
        assert response.status_code == 413
