"""Unit and property tests for the compression core.

Covers: longest-prefix-match equivalence (drop vs no-match distinguished),
the next-hop failure drill, minimality on hand-computed cases, brute-force
minimality on tiny cases, and randomized equivalence checks.
"""

import random
from ipaddress import IPv4Network

import pytest

from app import compressor as C
from app.compressor import compress, verify


def R(prefix, action):
    return (IPv4Network(prefix), action)


def to_routes(table):
    return [R(p, a) for p, a in table]


def check_ok(routes, table, failed):
    ok, detail = verify(routes, to_routes(table), failed)
    assert ok, detail


# ---------------------------------------------------------------------------
# crafted cases: normal forwarding
# ---------------------------------------------------------------------------

def test_empty_table():
    m, t = compress([], "NH9")
    assert m == 0 and t == []


def test_single_route():
    m, t = compress([R("10.0.0.0/8", "NH1")], "NH9")
    assert m == 1 and t == [("10.0.0.0/8", "NH1")]


def test_merge_sibling_prefixes():
    m, t = compress([R("10.0.0.0/25", "NH1"), R("10.0.0.128/25", "NH1")], "NH9")
    assert m == 1 and t == [("10.0.0.0/24", "NH1")]


def test_no_merge_across_actions():
    routes = [R("10.0.0.0/25", "NH1"), R("10.0.0.128/25", "NH2")]
    m, t = compress(routes, "NH9")
    assert m == 2
    check_ok(routes, t, "NH9")


def test_redundant_covered_entry_removed():
    m, t = compress([R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH1")], "NH9")
    assert m == 1 and t == [("10.0.0.0/8", "NH1")]


def test_drop_is_not_no_match():
    # A drop entry must not be widened into a default route: addresses
    # outside 10/8 are no-match, which differs from drop.
    m, t = compress([R("10.0.0.0/8", "drop")], "NH9")
    assert m == 1 and t == [("10.0.0.0/8", "drop")]


def test_no_match_hole_preserved():
    # 10/8 is NH1 with a no-match hole punched by nothing; compression may
    # not fill the surrounding space.
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "drop")]
    m, t = compress(routes, "NH9")
    assert m == 2
    check_ok(routes, t, "NH9")


def test_default_route_kept():
    routes = [R("0.0.0.0/0", "NH1"), R("10.0.0.0/8", "NH2")]
    m, t = compress(routes, "NH9")
    assert m == 2
    check_ok(routes, t, "NH9")


# ---------------------------------------------------------------------------
# crafted cases: failure drill
# ---------------------------------------------------------------------------

def test_failover_falls_back_to_shorter_prefix():
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2")]
    m, t = compress(routes, "NH2")
    assert m == 2
    check_ok(routes, t, "NH2")


def test_failover_to_no_match():
    routes = [R("10.1.0.0/16", "NH2")]
    m, t = compress(routes, "NH2")
    assert m == 1 and t == [("10.1.0.0/16", "NH2")]
    check_ok(routes, t, "NH2")


def test_failover_to_drop():
    routes = [R("10.0.0.0/8", "drop"), R("10.1.0.0/16", "NH2")]
    m, t = compress(routes, "NH2")
    assert m == 2
    check_ok(routes, t, "NH2")


def test_failover_chain():
    routes = [
        R("10.0.0.0/8", "NH1"),
        R("10.1.0.0/16", "NH2"),
        R("10.1.1.0/24", "NH3"),
    ]
    m, t = compress(routes, "NH2")
    assert m == 3
    check_ok(routes, t, "NH2")


def test_failed_nexthop_not_in_table():
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "drop")]
    m, t = compress(routes, "NH9")
    assert m == 2
    check_ok(routes, t, "NH9")


def test_failed_nexthop_everywhere():
    # All entries point at the failed next-hop: after the drill both tables
    # are empty, so only the normal scenario constrains the result.
    routes = [R("10.0.0.0/8", "NH1"), R("192.168.0.0/16", "NH1")]
    m, t = compress(routes, "NH1")
    assert m == 2
    check_ok(routes, t, "NH1")


def test_failover_two_layer_single_address():
    # A lone /32 pointing at the failed next-hop, falling back to NH1 from
    # the covering /24: the fallback must be inherited, cannot be rebuilt
    # inside a single address.
    routes = [R("10.1.2.0/24", "NH1"), R("10.1.2.3/32", "NH2")]
    m, t = compress(routes, "NH2")
    assert m == 2
    check_ok(routes, t, "NH2")


def test_drill_on_drop_action():
    # The drill may withdraw any action, including drop entries.
    routes = [
        R("10.0.0.0/8", "NH1"),
        R("10.1.0.0/16", "drop"),
        R("10.2.0.0/16", "drop"),
    ]
    m, t = compress(routes, "drop")
    assert m <= 3
    check_ok(routes, t, "drop")


def test_default_route_failover():
    # Default route stays as the post-failure fallback for NH2 blocks.
    routes = [R("0.0.0.0/0", "NH1"), R("10.1.0.0/16", "NH2"), R("10.2.0.0/16", "NH2")]
    m, t = compress(routes, "NH2")
    assert m == 3
    check_ok(routes, t, "NH2")


# ---------------------------------------------------------------------------
# demo example used in /docs
# ---------------------------------------------------------------------------

DEMO_ROUTES = [
    R("10.0.0.0/8", "NH1"),
    R("10.1.0.0/16", "NH2"),
    R("10.2.0.0/16", "NH1"),
    R("10.3.0.0/16", "drop"),
    R("192.168.0.0/16", "NH2"),
]


def test_demo_example_minimum_is_four():
    m, t = compress(DEMO_ROUTES, "NH2")
    assert m == 4
    check_ok(DEMO_ROUTES, t, "NH2")


def test_capacity_threshold():
    m_ok, t_ok = compress(DEMO_ROUTES, "NH2", capacity=4)
    assert m_ok == 4 and t_ok is not None and len(t_ok) == 4
    m_over, t_over = compress(DEMO_ROUTES, "NH2", capacity=3)
    assert m_over == 4 and t_over is None


# ---------------------------------------------------------------------------
# brute-force minimality on tiny cases
# ---------------------------------------------------------------------------

def _candidate_prefixes(routes, failed):
    """Universe in which an optimal table's prefixes provably live: nodes of
    the constant-requirement trie, plus children of leaves (two-layer fix)."""
    actions = sorted({a for _, a in routes})
    nonf = [a for a in actions if a != failed]
    code_of = {a: i + 1 for i, a in enumerate(nonf)}
    f_code = len(nonf) + 1
    tab = [
        (int(n.network_address), int(n.broadcast_address), n.prefixlen, a)
        for n, a in routes
    ]
    intervals = C._requirement_intervals(tab, failed, code_of, f_code)
    solver = C._Solver(intervals, len(nonf))
    root = solver._build(0, C.FULL_SPACE)
    prefixes = []
    stack = [root]
    while stack:
        node = stack.pop()
        span = node.hi - node.lo
        prefixes.append((node.lo, span))
        if isinstance(node, C._Internal):
            stack.append(node.left)
            stack.append(node.right)
        elif span >= 2:
            half = span >> 1
            prefixes.append((node.lo, half))
            prefixes.append((node.lo + half, half))
    return prefixes


def _brute_force_minimal(routes, failed, limit):
    """Smallest equivalent table over the candidate universe, or None."""
    from itertools import combinations

    prefixes = _candidate_prefixes(routes, failed)
    actions = sorted({a for _, a in routes})
    entries = [(lo, span, act) for lo, span in prefixes for act in actions]
    for size in range(0, limit):
        for combo in combinations(entries, size):
            seen = set()
            dup = False
            for lo, span, _act in combo:
                if (lo, span) in seen:
                    dup = True
                    break
                seen.add((lo, span))
            if dup:
                continue
            table = [
                (IPv4Network((lo, C._plen_of(span))), act)
                for lo, span, act in combo
            ]
            ok, _ = verify(routes, table, failed)
            if ok:
                return size
    return None


TINY_CASES = [
    ([R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2")], "NH9"),
    ([R("10.0.0.0/25", "NH1"), R("10.0.0.128/25", "NH1")], "NH9"),
    ([R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2")], "NH2"),
    ([R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2"), R("10.2.0.0/16", "drop")], "NH9"),
    ([R("10.0.0.0/8", "drop"), R("10.1.0.0/16", "NH2")], "NH2"),
    ([R("10.1.2.0/24", "NH1"), R("10.1.2.3/32", "NH2")], "NH2"),
]


@pytest.mark.parametrize("routes,failed", TINY_CASES)
def test_brute_force_confirms_minimality(routes, failed):
    m, t = compress(routes, failed)
    check_ok(routes, t, failed)
    bf = _brute_force_minimal(routes, failed, limit=m)
    assert bf is None, f"brute force found a smaller table of size {bf} < {m}"


# ---------------------------------------------------------------------------
# randomized equivalence
# ---------------------------------------------------------------------------

def _random_routes(rng, n, actions):
    seen = set()
    out = []
    while len(out) < n:
        plen = rng.choice([4, 8, 10, 12, 16, 18, 24, 26, 32])
        net = IPv4Network((rng.getrandbits(32), plen), strict=False)
        if str(net) in seen:
            continue
        seen.add(str(net))
        out.append((net, rng.choice(actions)))
    return out


def test_random_equivalence_and_shrinkage():
    rng = random.Random(20261120)
    for _ in range(300):
        n = rng.randint(0, 12)
        routes = _random_routes(rng, n, ["NH1", "NH2", "NH3", "drop"])
        failed = rng.choice(["NH1", "NH2", "NH3", "NH9"])
        m, t = compress(routes, failed)
        assert m <= n  # the original table is always a feasible solution
        assert len({p for p, _ in t}) == len(t)  # distinct prefixes
        assert {a for _, a in t} <= {a for _, a in routes}  # actions reused
        check_ok(routes, t, failed)


def test_random_capacity_boundary():
    rng = random.Random(99)
    for _ in range(60):
        n = rng.randint(1, 10)
        routes = _random_routes(rng, n, ["NH1", "NH2", "drop"])
        failed = rng.choice(["NH1", "NH2"])
        m, t = compress(routes, failed)
        m2, t2 = compress(routes, failed, capacity=m)
        assert m2 == m and t2 is not None
        if m > 0:
            m3, t3 = compress(routes, failed, capacity=m - 1)
            assert m3 == m and t3 is None


def test_perf_128_routes():
    import time

    rng = random.Random(7)
    routes = _random_routes(
        rng, 128, ["NH1", "NH2", "NH3", "NH4", "NH5", "NH6", "drop"]
    )
    start = time.monotonic()
    m, t = compress(routes, "NH3")
    elapsed = time.monotonic() - start
    assert m <= 128
    check_ok(routes, t, "NH3")
    assert elapsed < 10.0, f"compression too slow: {elapsed:.2f}s"
