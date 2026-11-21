"""ECMP hash-bucket rearrangement after removing one uplink next-hop.

An ECMP group is identified by its action/group name and spreads traffic over
a small set of numbered hash buckets.  Before an uplink is taken out of
service the on-call engineer supplies:

  * the next-hop being removed;
  * up to 16 buckets with distinct numbers, each carrying its current next-hop,
    the bytes it carried in one sampling window, and its active connection
    count;
  * up to 4 retained next-hops, each with the exact number of buckets it must
    receive and a byte budget for the same sampling window.

Every bucket is assigned as a whole to exactly one retained next-hop.  Each
retained next-hop must receive exactly its requested bucket count, and the
sum of the assigned buckets' byte counts must not exceed its budget.  Buckets
that already sit on a retained next-hop may be moved as well.  Among all
feasible assignments, the total active connection count of buckets whose
next-hop changes is minimized (the number of moved buckets breaks ties).  If
no assignment satisfies the counts and budgets, the request is reported as
infeasible.

Method
------
Exact dynamic program over subsets of buckets.  ``dp[mask]`` is the least
cost of covering exactly the buckets in ``mask`` with the retained next-hops
processed so far.  Each next-hop takes one subset of exactly its bucket count
whose bytes fit its budget, so a transition enumerates only combinations of
buckets that are still free.  The state space is bounded by ``2^16`` masks;
the last next-hop is forced to take the remaining buckets, which removes the
final enumeration layer.  Next-hops are processed in the order that minimizes
the estimated transition count (at most 4! orders are compared).

Costs are encoded as ``migrated_connections * (n + 1) + moved_buckets`` so a
single integer DP keeps the primary objective and the deterministic
tie-break.  The returned assignment is re-checked (exact counts, budgets,
reported migration count) before it leaves the module.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations
from math import comb

MAX_BUCKETS = 16
MAX_RETAINED = 4

INF = 1 << 62

# One DP integer holds both objectives: connections first, moved buckets
# second. The secondary term is at most n, so it never outweighs a single
# extra migrated connection.


@dataclass(frozen=True)
class Bucket:
    """One hash bucket of the ECMP group."""

    bucket: int
    current_nexthop: str
    bytes_in_window: int
    active_connections: int


@dataclass(frozen=True)
class RetainedTarget:
    """A next-hop that stays in service and its bucket/byte targets."""

    nexthop: str
    bucket_count: int
    byte_budget: int


@dataclass(frozen=True)
class Rearrangement:
    """Outcome: either a deployable mapping or a bare infeasibility report."""

    feasible: bool
    bucket_to_nexthop: dict | None
    migrated_connections: int | None


INFEASIBLE = Rearrangement(False, None, None)


def _order_targets(counts: list[int], n: int) -> list[int]:
    """Pick a processing order that minimizes DP work.

    After ``p`` buckets have been covered the DP holds at most ``C(n, p)``
    states; a next-hop with count ``c`` then needs ``C(n - p, c)``
    transitions each.  The last next-hop is forced to take the remaining
    buckets, so its stage costs one transition per state.  Evaluate all (at
    most 4!) orders and keep the caller's order on ties, making the output
    deterministic.
    """

    def work(order):
        total = 0
        used = 0
        for pos, i in enumerate(order):
            states = comb(n, used)
            if pos == len(order) - 1:
                total += states
            else:
                total += states * comb(n - used, counts[i])
            used += counts[i]
        return total

    if len(counts) <= 1:
        return list(range(len(counts)))
    return list(min(permutations(range(len(counts))), key=work))


def _subset_sums(values: list[int]) -> list[int]:
    """Sum of ``values`` over every subset of ``range(len(values))``."""
    size = 1 << len(values)
    out = [0] * size
    for mask in range(1, size):
        lsb = mask & -mask
        out[mask] = out[mask ^ lsb] + values[lsb.bit_length() - 1]
    return out


def rearrange_buckets(
    buckets: list[Bucket],
    removed_nexthop: str,
    targets: list[RetainedTarget],
) -> Rearrangement:
    """Return a least-migration feasible assignment, or ``INFEASIBLE``.

    Structural coherence (distinct bucket numbers, distinct retained
    next-hops, the removed next-hop not retained) is expected to have been
    validated by the caller; only count/budget feasibility is decided here.
    A bucket counts as moved whenever its assigned next-hop differs from its
    current one, so buckets on the removed next-hop always count while
    buckets already on a retained next-hop may stay for free.
    """
    n = len(buckets)

    if any(t.bucket_count < 0 or t.byte_budget < 0 for t in targets):
        return INFEASIBLE
    if any(t.nexthop == removed_nexthop for t in targets):
        # A next-hop being removed cannot be one of the retained targets.
        return INFEASIBLE
    if sum(t.bucket_count for t in targets) != n:
        return INFEASIBLE
    if n == 0:
        return Rearrangement(True, {}, 0)
    if sum(b.bytes_in_window for b in buckets) > sum(t.byte_budget for t in targets):
        return INFEASIBLE

    scale = n + 1
    bytes_of = _subset_sums([b.bytes_in_window for b in buckets])
    full = (1 << n) - 1

    # cost_of[t][mask] = cost of giving the bucket subset `mask` to target t.
    cost_of = []
    for t in targets:
        per_bucket = [
            b.active_connections * scale + 1 if b.current_nexthop != t.nexthop else 0
            for b in buckets
        ]
        cost_of.append(_subset_sums(per_bucket))

    order = _order_targets([t.bucket_count for t in targets], n)

    # dp: mask -> cost of covering exactly `mask` with the next-hops so far.
    # links[stage][mask] = (previous mask, subset given to this next-hop).
    dp: dict[int, int] = {0: 0}
    links: list[dict[int, tuple[int, int]]] = []

    for stage, ti in enumerate(order):
        t = targets[ti]
        count = t.bucket_count

        if stage == len(order) - 1:
            # The last next-hop must take exactly the buckets still free.
            best = INF
            winner: tuple[int, int] | None = None
            for mask, base in dp.items():
                rest = full ^ mask
                if bytes_of[rest] <= t.byte_budget:
                    val = base + cost_of[ti][rest]
                    if val < best:
                        best = val
                        winner = (mask, rest)
            if winner is None:
                return INFEASIBLE
            dp = {full: best}
            links.append({full: winner})
            break

        ndp: dict[int, int] = {}
        link: dict[int, tuple[int, int]] = {}
        get = ndp.get
        cost = cost_of[ti]
        budget = t.byte_budget
        for mask, base in dp.items():
            free_bits = [1 << i for i in range(n) if not (mask >> i) & 1]
            if len(free_bits) < count:
                continue
            for combo in combinations(free_bits, count):
                sub = sum(combo)
                if bytes_of[sub] > budget:
                    continue
                nm = mask | sub
                val = base + cost[sub]
                old = get(nm)
                if old is None or val < old:
                    ndp[nm] = val
                    link[nm] = (mask, sub)
        links.append(link)
        dp = ndp
        if not dp:
            return INFEASIBLE

    if full not in dp:  # pragma: no cover - defensive
        return INFEASIBLE

    assignment: dict[int, str] = {}
    mask = full
    for stage in range(len(order) - 1, -1, -1):
        prev, sub = links[stage][mask]
        hop = targets[order[stage]].nexthop
        for i in range(n):
            if (sub >> i) & 1:
                assignment[buckets[i].bucket] = hop
        mask = prev

    if mask != 0 or len(assignment) != n:  # pragma: no cover - internal guard
        raise RuntimeError("ECMP rearrangement reconstruction is inconsistent")

    migrated = sum(
        b.active_connections
        for b in buckets
        if assignment[b.bucket] != b.current_nexthop
    )
    _check_assignment(buckets, targets, assignment, migrated)
    return Rearrangement(True, dict(sorted(assignment.items())), migrated)


def _check_assignment(
    buckets: list[Bucket],
    targets: list[RetainedTarget],
    assignment: dict[int, str],
    migrated: int,
) -> None:
    """Re-verify counts, budgets and the reported migration count."""
    if len(assignment) != len(buckets):
        raise RuntimeError("every bucket must be assigned exactly once")
    by_hop: dict[str, list[Bucket]] = {t.nexthop: [] for t in targets}
    for b in buckets:
        hop = assignment.get(b.bucket)
        if hop not in by_hop:  # pragma: no cover - internal guard
            raise RuntimeError(f"bucket {b.bucket} assigned to unknown next-hop {hop!r}")
        by_hop[hop].append(b)
    for t in targets:
        got = by_hop[t.nexthop]
        if len(got) != t.bucket_count:  # pragma: no cover - internal guard
            raise RuntimeError(
                f"next-hop {t.nexthop!r} got {len(got)} buckets, "
                f"expected {t.bucket_count}"
            )
        total_bytes = sum(b.bytes_in_window for b in got)
        if total_bytes > t.byte_budget:  # pragma: no cover - internal guard
            raise RuntimeError(
                f"next-hop {t.nexthop!r} uses {total_bytes} bytes "
                f"over budget {t.byte_budget}"
            )
    recomputed = sum(
        b.active_connections
        for b in buckets
        if assignment[b.bucket] != b.current_nexthop
    )
    if recomputed != migrated:  # pragma: no cover - internal guard
        raise RuntimeError(
            f"migrated connection count mismatch: {recomputed} != {migrated}"
        )
