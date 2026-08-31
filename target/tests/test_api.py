"""Integration tests for the generated URL shortener API.

Uses FastAPI's TestClient, which drives the real ASGI app in-process --
these exercise actual route/storage/codec wiring, not mocks.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTENER_DB_PATH", str(tmp_path / f"{uuid.uuid4().hex}.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_then_redirect(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/very/long/path"})
    assert r.status_code == 201
    body = r.json()
    assert body["long_url"] == "https://example.com/very/long/path"

    code = body["code"]
    got = client.get(f"/{code}", follow_redirects=False)
    assert got.status_code == 302
    assert got.headers["location"] == "https://example.com/very/long/path"


def test_unknown_code_is_404(client):
    assert client.get("/doesnotexist12345", follow_redirects=False).status_code == 404


def test_invalid_long_url_is_rejected(client):
    r = client.post("/api/urls", json={"long_url": "not-a-url"})
    assert r.status_code == 422


def test_delete_then_gone(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/to-delete"})
    code = r.json()["code"]
    assert client.delete(f"/api/urls/{code}").status_code == 204
    assert client.get(f"/{code}", follow_redirects=False).status_code == 404


def test_custom_alias_is_used_verbatim_and_rejects_a_duplicate(client):
    payload1 = {"long_url": "https://example.com/a", "custom_alias": "mine"}
    r1 = client.post("/api/urls", json=payload1)
    assert r1.status_code == 201
    assert r1.json()["code"] == "mine"

    payload2 = {"long_url": "https://example.com/b", "custom_alias": "mine"}
    r2 = client.post("/api/urls", json=payload2)
    assert r2.status_code == 409


def test_expired_link_returns_410(client):
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    r = client.post(
        "/api/urls", json={"long_url": "https://example.com/expired", "expires_at": past}
    )
    code = r.json()["code"]
    got = client.get(f"/{code}", follow_redirects=False)
    assert got.status_code == 410


def test_stats_reflect_click_count(client):
    r = client.post("/api/urls", json={"long_url": "https://example.com/tracked"})
    code = r.json()["code"]

    before = client.get(f"/api/urls/{code}/stats").json()
    assert before["click_count"] == 0

    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)

    after = client.get(f"/api/urls/{code}/stats").json()
    assert after["click_count"] == 2
