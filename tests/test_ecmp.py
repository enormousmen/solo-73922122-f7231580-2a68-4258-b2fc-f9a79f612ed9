"""Tests for ECMP hash-bucket rearrangement around a draining next-hop."""

import random
from itertools import product

from fastapi.testclient import TestClient

from app.ecmp import (
    INFEASIBLE,
    Bucket,
    RetainedTarget,
    rearrange_buckets,
)
from app.main import app

client = TestClient(app)


def B(num, hop, nbytes, conns):
    return Bucket(num, hop, nbytes, conns)


def T(hop, count, budget):
    return RetainedTarget(hop, count, budget)


DEMO_BUCKETS = [
    B(0, "NH1", 4_200_000_000, 1200),
    B(1, "NH1", 3_800_000_000, 900),
    B(2, "NH1", 4_600_000_000, 1500),
    B(3, "NH2", 5_000_000_000, 2100),
    B(4, "NH2", 2_400_000_000, 700),
    B(5, "NH3", 3_100_000_000, 800),
    B(6, "NH3", 2_900_000_000, 650),
    B(7, "NH4", 1_800_000_000, 400),
]

DEMO_TARGETS = [
    T("NH1", 3, 15_000_000_000),
    T("NH3", 3, 14_000_000_000),
    T("NH4", 2, 5_000_000_000),
]


def check_result(buckets, removed, targets, result):
    """Assert the returned mapping honours every hard constraint."""
    assert result.feasible
    assignment = result.bucket_to_nexthop
    assert set(assignment) == {b.bucket for b in buckets}
    allowed = {t.nexthop for t in targets}
    assert set(assignment.values()) <= allowed
    by_hop = {t.nexthop: [] for t in targets}
    for b in buckets:
        by_hop[assignment[b.bucket]].append(b)
    for t in targets:
        got = by_hop[t.nexthop]
        assert len(got) == t.bucket_count
        assert sum(b.bytes_in_window for b in got) <= t.byte_budget
    moved = sum(
        b.active_connections
        for b in buckets
        if assignment[b.bucket] != b.current_nexthop
    )
    assert moved == result.migrated_connections


def brute_force_best(buckets, targets):
    """Least moved connections over all count-valid, budget-valid mappings."""
    hops = [t.nexthop for t in targets]
    best = None
    n = len(buckets)
    for combo in product(range(len(hops)), repeat=n):
        counts = [0] * len(hops)
        nbytes = [0] * len(hops)
        moved = 0
        for b, hi in zip(buckets, combo):
            counts[hi] += 1
            nbytes[hi] += b.bytes_in_window
            if hops[hi] != b.current_nexthop:
                moved += b.active_connections
        if all(counts[i] == targets[i].bucket_count for i in range(len(hops))) and all(
            nbytes[i] <= targets[i].byte_budget for i in range(len(hops))
        ):
            if best is None or moved < best:
                best = moved
    return best


# ---------------------------------------------------------------------------
# demo scenario
# ---------------------------------------------------------------------------

def test_demo_moves_only_the_draining_buckets():
    result = rearrange_buckets(DEMO_BUCKETS, "NH2", DEMO_TARGETS)
    check_result(DEMO_BUCKETS, "NH2", DEMO_TARGETS, result)
    assert result.migrated_connections == 2800
    assert result.bucket_to_nexthop[3] != "NH2"
    assert result.bucket_to_nexthop[4] != "NH2"


def test_demo_moves_nothing_extra():
    # Both NH2 buckets are the only ones that change next-hop.
    result = rearrange_buckets(DEMO_BUCKETS, "NH2", DEMO_TARGETS)
    changed = [
        b.bucket
        for b in DEMO_BUCKETS
        if result.bucket_to_nexthop[b.bucket] != b.current_nexthop
    ]
    assert changed == [3, 4]


def test_infeasible_budget_reports_only_failure():
    tight = [T("NH1", 3, 15_000_000_000), T("NH3", 3, 14_000_000_000),
             T("NH4", 2, 1_000_000_000)]
    result = rearrange_buckets(DEMO_BUCKETS, "NH2", tight)
    assert result == INFEASIBLE
    assert result.bucket_to_nexthop is None
    assert result.migrated_connections is None


def test_infeasible_when_counts_do_not_sum():
    targets = [T("NH1", 3, 10**18), T("NH3", 3, 10**18)]  # only 6 of 8
    assert rearrange_buckets(DEMO_BUCKETS, "NH2", targets) == INFEASIBLE


def test_bucket_on_removed_hop_must_move_even_with_loose_budget():
    buckets = [B(0, "NH2", 1, 10), B(1, "NH1", 1, 5)]
    result = rearrange_buckets(buckets, "NH2", [T("NH1", 2, 100)])
    check_result(buckets, "NH2", [T("NH1", 2, 100)], result)
    assert result.migrated_connections == 10


def test_moving_a_retained_bucket_can_be_cheaper():
    # NH3 needs one bucket but its 1-byte budget excludes the heavy bucket 2,
    # so bucket 2 (currently on the removed NH2, 0 connections) must go to
    # NH1.  NH1 then has three buckets for a count of two, so one of its own
    # buckets has to move to NH3: the cheap bucket 1 (1 connection) rather
    # than the expensive bucket 0 (100).  Minimum migration is 1 connection.
    buckets = [
        B(0, "NH1", 1, 100),
        B(1, "NH1", 1, 1),
        B(2, "NH2", 100, 0),
    ]
    targets = [T("NH1", 2, 1000), T("NH3", 1, 1)]
    result = rearrange_buckets(buckets, "NH2", targets)
    check_result(buckets, "NH2", targets, result)
    assert result.migrated_connections == 1
    assert result.bucket_to_nexthop[1] == "NH3"
    assert result.bucket_to_nexthop[2] == "NH1"


def test_empty_group_is_trivially_feasible():
    result = rearrange_buckets([], "NH2", [])
    assert result.feasible and result.bucket_to_nexthop == {}
    assert result.migrated_connections == 0


def test_empty_group_with_positive_counts_is_infeasible():
    assert rearrange_buckets([], "NH2", [T("NH1", 1, 100)]) == INFEASIBLE


def test_zero_count_retained_target_receives_nothing():
    buckets = [B(0, "NH1", 5, 10), B(1, "NH2", 5, 20)]
    targets = [T("NH1", 2, 100), T("NH3", 0, 0)]
    result = rearrange_buckets(buckets, "NH2", targets)
    check_result(buckets, "NH2", targets, result)
    assert result.migrated_connections == 20


# ---------------------------------------------------------------------------
# randomized optimality against brute force
# ---------------------------------------------------------------------------

def test_random_optimality_matches_brute_force():
    rng = random.Random(20261121)
    for _ in range(200):
        n = rng.randint(0, 6)
        hops = ["NH1", "NH2", "NH3", "NH4"][: rng.randint(1, 4)]
        buckets = [
            B(i, rng.choice(hops), rng.randint(0, 10), rng.randint(0, 20))
            for i in range(n)
        ]
        removed = rng.choice(hops)
        retained_hops = [h for h in hops if h != removed]
        rng.shuffle(retained_hops)
        retained_hops = retained_hops[: rng.randint(1, 2)]
        if not retained_hops:
            continue
        # Random exact counts summing to n.
        counts = [0] * len(retained_hops)
        for _ in range(n):
            counts[rng.randrange(len(counts))] += 1
        targets = [T(h, c, rng.randint(0, 40)) for h, c in zip(retained_hops, counts)]
        result = rearrange_buckets(buckets, removed, targets)
        best = brute_force_best(buckets, targets)
        if best is None:
            assert result == INFEASIBLE
        else:
            check_result(buckets, removed, targets, result)
            assert result.migrated_connections == best


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _demo_payload(**overrides):
    payload = {
        "group": "ecmp-core",
        "removed_nexthop": "NH2",
        "buckets": [
            {
                "bucket": b.bucket,
                "current_nexthop": b.current_nexthop,
                "bytes_in_window": b.bytes_in_window,
                "active_connections": b.active_connections,
            }
            for b in DEMO_BUCKETS
        ],
        "retained": [
            {
                "nexthop": t.nexthop,
                "bucket_count": t.bucket_count,
                "byte_budget": t.byte_budget,
            }
            for t in DEMO_TARGETS
        ],
    }
    payload.update(overrides)
    return payload


def test_api_rearrange_demo():
    r = client.post("/ecmp/rearrange", json=_demo_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is True
    assert body["group"] == "ecmp-core"
    assert body["removed_nexthop"] == "NH2"
    assert body["migrated_connections"] == 2800
    assert len(body["assignment"]) == 8
    assert set(body["assignment"].values()) <= {"NH1", "NH3", "NH4"}


def test_api_rearrange_infeasible_shape():
    payload = _demo_payload()
    payload["retained"][2]["byte_budget"] = 1
    r = client.post("/ecmp/rearrange", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["feasible"] is False
    assert body["assignment"] is None
    assert body["migrated_connections"] is None


def test_api_rejects_duplicate_bucket_numbers():
    payload = _demo_payload()
    payload["buckets"][1]["bucket"] = payload["buckets"][0]["bucket"]
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_rejects_too_many_buckets():
    payload = _demo_payload()
    payload["buckets"] = [
        {"bucket": i, "current_nexthop": "NH1", "bytes_in_window": 1,
         "active_connections": 1}
        for i in range(17)
    ]
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_rejects_too_many_retained():
    payload = _demo_payload()
    payload["retained"] = [
        {"nexthop": f"NH{i}", "bucket_count": 2, "byte_budget": 10**12}
        for i in range(5)
    ]
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_rejects_duplicate_retained():
    payload = _demo_payload()
    payload["retained"][1]["nexthop"] = payload["retained"][0]["nexthop"]
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_rejects_removed_also_retained():
    payload = _demo_payload()
    payload["retained"].append(
        {"nexthop": "NH2", "bucket_count": 0, "byte_budget": 0}
    )
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_rejects_negative_values():
    payload = _demo_payload()
    payload["buckets"][0]["bytes_in_window"] = -1
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422
    payload = _demo_payload()
    payload["retained"][0]["bucket_count"] = -1
    assert client.post("/ecmp/rearrange", json=payload).status_code == 422


def test_api_docs_contains_rearrange_example():
    spec = client.get("/openapi.json").json()
    assert "/ecmp/rearrange" in spec["paths"]
    examples = spec["paths"]["/ecmp/rearrange"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert "remove_uplink" in examples
    value = examples["remove_uplink"]["value"]
    assert value["group"] == "ecmp-core"
    assert value["removed_nexthop"] == "NH2"
    # The documented request must actually be feasible and return 2800.
    r = client.post("/ecmp/rearrange", json=value)
    assert r.status_code == 200
    assert r.json()["migrated_connections"] == 2800
    assert client.get("/docs").status_code == 200


def test_api_16_buckets_4_targets_is_fast():
    import time

    rng = random.Random(5)
    hops = ["NH1", "NH2", "NH3", "NH4", "NH5"]
    buckets = [
        {"bucket": i, "current_nexthop": rng.choice(hops),
         "bytes_in_window": rng.randint(0, 1000),
         "active_connections": rng.randint(0, 1000)}
        for i in range(16)
    ]
    counts = [4, 4, 4, 4]
    targets = [
        {"nexthop": h, "bucket_count": c, "byte_budget": 10**6}
        for h, c in zip(["NH1", "NH3", "NH4", "NH5"], counts)
    ]
    payload = {
        "group": "ecmp-wide",
        "removed_nexthop": "NH2",
        "buckets": buckets,
        "retained": targets,
    }
    start = time.monotonic()
    r = client.post("/ecmp/rearrange", json=payload)
    elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert r.json()["feasible"] is True
    assert elapsed < 10.0
