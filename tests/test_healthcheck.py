"""Healthcheck endpoint tests."""

from fastapi.testclient import TestClient

from peermarket_agent.slack_bridge.app import build_healthz_api


def test_healthz_returns_ok():
    client = TestClient(build_healthz_api())
    r = client.get("/agent/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
