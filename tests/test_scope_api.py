"""Tests for the public scope-checker HTTP vertical.

The service layer is already covered by ``test_scope.py``; here we assert the
thin HTTP wrapper: the page renders, the CN code drives ``in_scope`` over the
API, and empty input fails loud as 400.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_scope_checker_page_renders(client: TestClient) -> None:
    response = client.get("/scope-checker")
    assert response.status_code == 200
    assert "EUDR scope check" in response.text


def test_check_scope_in_scope_cn(client: TestClient) -> None:
    response = client.post("/api/check-scope", data={"cn_code": "0901"})
    assert response.status_code == 200
    body = response.json()
    assert body["in_scope"] is True
    assert body["commodity"] == "coffee"
    assert body["matched_cn"] == "0901"
    assert body["required_documentation"]


def test_check_scope_out_of_scope_cn(client: TestClient) -> None:
    response = client.post("/api/check-scope", data={"cn_code": "8471"})
    assert response.status_code == 200
    body = response.json()
    assert body["in_scope"] is False
    assert body["commodity"] is None
    assert body["matched_cn"] is None


def test_check_scope_neither_field_is_400(client: TestClient) -> None:
    response = client.post("/api/check-scope", data={})
    assert response.status_code == 400
    assert "error" in response.json()


def test_check_scope_htmx_returns_fragment(client: TestClient) -> None:
    response = client.post(
        "/api/check-scope",
        data={"cn_code": "0901"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "IN SCOPE" in response.text
