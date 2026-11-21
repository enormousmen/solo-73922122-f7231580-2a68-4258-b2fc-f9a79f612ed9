"""API tests: request validation, both capacity scenarios, /verify, /docs."""

from fastapi.testclient import TestClient

from app.main import app, DEMO_ROUTES

client = TestClient(app)


def test_root():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "fib-compressor"


def test_docs_and_openapi():
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/compress" in paths and "/verify" in paths


def test_compress_capacity_ok():
    r = client.post(
        "/compress",
        json={"routes": DEMO_ROUTES, "capacity": 4, "failed_nexthop": "NH2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["fits"] is True
    assert body["min_entries"] == 4
    assert body["verified"] is True
    assert len(body["table"]) == 4
    prefixes = [e["prefix"] for e in body["table"]]
    assert len(set(prefixes)) == len(prefixes)


def test_compress_capacity_exceeded_reports_only_count():
    r = client.post(
        "/compress",
        json={"routes": DEMO_ROUTES, "capacity": 3, "failed_nexthop": "NH2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "capacity_exceeded"
    assert body["fits"] is False
    assert body["min_entries"] == 4
    assert body["table"] is None


def test_compress_empty_table():
    r = client.post(
        "/compress", json={"routes": [], "capacity": 0, "failed_nexthop": "NH1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["min_entries"] == 0 and body["table"] == []


def test_validation_too_many_routes():
    routes = [{"prefix": f"10.{i}.0.0/16", "action": "NH1"} for i in range(129)]
    r = client.post(
        "/compress", json={"routes": routes, "capacity": 200, "failed_nexthop": "NH1"}
    )
    assert r.status_code == 422


def test_exactly_128_routes_accepted():
    routes = [{"prefix": f"10.{i}.0.0/16", "action": "NH1"} for i in range(128)]
    r = client.post(
        "/compress", json={"routes": routes, "capacity": 200, "failed_nexthop": "NH1"}
    )
    assert r.status_code == 200
    # 128 distinct /16s, all NH1: merges into a single 10.0.0.0/9 entry
    body = r.json()
    assert body["min_entries"] == 1
    assert body["table"] == [{"prefix": "10.0.0.0/9", "action": "NH1"}]


def test_validation_duplicate_prefixes():
    r = client.post(
        "/compress",
        json={
            "routes": [
                {"prefix": "10.0.0.0/8", "action": "NH1"},
                {"prefix": "10.0.0.0/8", "action": "NH2"},
            ],
            "capacity": 5,
            "failed_nexthop": "NH1",
        },
    )
    assert r.status_code == 422


def test_validation_bad_prefix():
    r = client.post(
        "/compress",
        json={
            "routes": [{"prefix": "999.0.0.0/8", "action": "NH1"}],
            "capacity": 5,
            "failed_nexthop": "NH1",
        },
    )
    assert r.status_code == 422


def test_validation_negative_capacity():
    r = client.post(
        "/compress",
        json={
            "routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
            "capacity": -1,
            "failed_nexthop": "NH1",
        },
    )
    assert r.status_code == 422


def test_prefix_normalization():
    # host bits set -> normalized to the network address
    r = client.post(
        "/compress",
        json={
            "routes": [{"prefix": "10.1.2.3/8", "action": "NH1"}],
            "capacity": 5,
            "failed_nexthop": "NH9",
        },
    )
    assert r.status_code == 200
    assert r.json()["table"] == [{"prefix": "10.0.0.0/8", "action": "NH1"}]


def test_verify_endpoint_equivalent():
    r = client.post(
        "/verify",
        json={
            "old_routes": DEMO_ROUTES,
            "new_routes": [
                {"prefix": "10.0.0.0/8", "action": "NH1"},
                {"prefix": "10.1.0.0/16", "action": "NH2"},
                {"prefix": "10.3.0.0/16", "action": "drop"},
                {"prefix": "192.168.0.0/16", "action": "NH2"},
            ],
            "failed_nexthop": "NH2",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["equivalent"] is True
    assert body["normal_equivalent"] is True
    assert body["failover_equivalent"] is True


def test_verify_endpoint_detects_mismatch():
    r = client.post(
        "/verify",
        json={
            "old_routes": [{"prefix": "10.0.0.0/8", "action": "NH1"}],
            "new_routes": [{"prefix": "10.0.0.0/8", "action": "drop"}],
            "failed_nexthop": "NH9",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["equivalent"] is False
    assert body["first_mismatch"]["old"] == "NH1"
    assert body["first_mismatch"]["new"] == "drop"


def test_verify_endpoint_detects_failover_mismatch():
    # Same normal behavior, different post-failure behavior: once NH2 is
    # withdrawn, 10.128.0.0/9 falls back to NH1 in B but to no-match in A.
    r = client.post(
        "/verify",
        json={
            "old_routes": [
                {"prefix": "10.0.0.0/8", "action": "NH2"},
                {"prefix": "10.0.0.0/9", "action": "NH1"},
            ],
            "new_routes": [
                {"prefix": "10.0.0.0/8", "action": "NH1"},
                {"prefix": "10.0.0.0/9", "action": "NH1"},
                {"prefix": "10.128.0.0/9", "action": "NH2"},
            ],
            "failed_nexthop": "NH2",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["normal_equivalent"] is True
    assert body["failover_equivalent"] is False
    assert body["equivalent"] is False
    assert body["first_mismatch"]["scenario"] == "failover"
