"""Smoke tests against a running orders API.

    API_URL=http://localhost:18000 python3 -m pytest services/orders-api/tests -q

Point API_URL at the sandbox when validating a fix, never at production.
"""
import os
import uuid

import httpx
import pytest

API = os.getenv("API_URL", "http://localhost:18000")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=API, timeout=10) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"] == {"postgres": "ok", "redis": "ok"}


def test_products(client):
    products = client.get("/products").json()
    assert len(products) == 14
    assert {"id", "name", "price_cents"} <= products[0].keys()


def test_product_detail_and_404(client):
    assert client.get("/products/1").json()["id"] == 1
    assert client.get("/products/9999").status_code == 404


def test_bestsellers(client):
    r = client.get("/bestsellers")
    assert r.status_code == 200
    assert 1 <= len(r.json()) <= 5


def test_cart_and_session(client):
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    client.post("/cart", json={"email": email, "product_id": 3, "qty": 2}).raise_for_status()
    assert client.get("/session", params={"email": email}).json()["cart_items"] == 2


def test_checkout_roundtrip(client):
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/orders", json={"email": email, "items": [{"product_id": 2, "qty": 1},
                                                              {"product_id": 6, "qty": 2}]})
    assert r.status_code == 201, r.text
    order = r.json()
    fetched = client.get(f"/orders/{order['order_id']}").json()
    assert fetched["total_cents"] == order["total_cents"]
    assert len(fetched["items"]) == 2


def test_checkout_rejects_unknown_product(client):
    r = client.post("/orders", json={"email": "x@example.com", "items": [{"product_id": 999, "qty": 1}]})
    assert r.status_code == 400
