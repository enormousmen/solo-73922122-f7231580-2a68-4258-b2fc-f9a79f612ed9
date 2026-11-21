"""Optimal IPv4 FIB compression with next-hop failure-drill equivalence.

Given an IPv4 forwarding table (prefix -> action, where an action is either a
next-hop name or the reserved word "drop"), a capacity limit, and a next-hop
to drill-fail, compute the *smallest* equivalent table:

  * longest-prefix-match semantics;
  * "drop" and "no-match" are distinct results and both are preserved;
  * after removing every entry whose action is the failed next-hop from BOTH
    the original and the compressed table, the two residual tables must still
    produce identical results for every destination address.

Method
------
Every one of the 2^32 addresses carries a requirement pair (normal, fallback)
derived from the original table: ``normal`` is the LPM result of the full
table, ``fallback`` the LPM result after withdrawing the failed next-hop.
Both functions are piecewise constant on at most 2*n+1 elementary intervals.
We build the binary trie of maximal constant-requirement dyadic ranges and
run an exact dynamic program over it.  The DP state at a node is the pair
(action of the longest matching entry above, action of the longest matching
non-failed entry above); only O(2k+2) such states are reachable, where k is
the number of distinct non-failed actions.  Leaves (constant regions) have
closed-form costs, including the two-layer case (normal = failed next-hop,
fallback = something else), which needs up to 3 entries when the fallback
cannot be inherited from an ancestor.

The program is exact: it returns the provably minimal number of entries.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

DROP = "drop"
INF = 1 << 30

# internal-node choice codes
NOPLACE, PLACE_F, PLACE_A = 0, 1, 2

FULL_SPACE = 1 << 32  # number of IPv4 addresses


# ---------------------------------------------------------------------------
# requirement computation
# ---------------------------------------------------------------------------

def _lpm_code(tab, point, exclude, code_of, failed, f_code):
    """Longest-prefix-match action code at ``point`` over ``tab``.

    ``tab`` entries are (start, end, prefixlen, action).  Entries whose
    action equals ``exclude`` are skipped (used for the failure scenario).
    Returns 0 for no-match.
    """
    best_len = -1
    best = 0
    for start, end, plen, act in tab:
        if act == exclude:
            continue
        if plen > best_len and start <= point <= end:
            best_len = plen
            best = f_code if act == failed else code_of[act]
    return best


def _requirement_intervals(tab, failed, code_of, f_code):
    """Elementary intervals with constant (normal, fallback) requirement.

    Returns a sorted list of (start, end_exclusive, v1, v2) covering
    [0, 2^32).  v1/v2 are action codes (0 = no-match).
    """
    bounds = {0, FULL_SPACE}
    for start, end, _plen, _act in tab:
        bounds.add(start)
        bounds.add(end + 1)
    pts = sorted(bounds)
    out = []
    for i in range(len(pts) - 1):
        lo, hi = pts[i], pts[i + 1]
        v1 = _lpm_code(tab, lo, None, code_of, failed, f_code)
        v2 = _lpm_code(tab, lo, failed, code_of, failed, f_code)
        out.append((lo, hi, v1, v2))
    return out


# ---------------------------------------------------------------------------
# constant-requirement dyadic trie
# ---------------------------------------------------------------------------

@dataclass
class _Leaf:
    lo: int
    hi: int
    v1: int
    v2: int


@dataclass
class _Internal:
    lo: int
    hi: int
    left: object
    right: object
    idx: int = -1


def _plen_of(lo_span):
    """Prefix length of a dyadic range from its size (a power of two)."""
    return 32 - (lo_span.bit_length() - 1)


def _prefix_str(lo, span):
    return f"{IPv4Address(lo)}/{_plen_of(span)}"


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

class _Solver:
    """Exact DP over the constant-requirement trie.

    Action codes: 0 = no-match (not a real entry action), 1..k = non-failed
    actions, k+1 = the failed next-hop F.

    Canonical DP states (the only reachable inherited pairs):
      s = 0            -> (none, none)
      s = 1+i, i<k     -> (a_i, a_i)        normal = fallback = non-F action
      s = 1+k+j, j<=k  -> (F, beta_j)       normal = F, fallback = beta_j
    """

    def __init__(self, intervals, n_nonf):
        self.k = n_nonf
        self.f_code = n_nonf + 1
        self.S = 2 * n_nonf + 2
        self.starts = [iv[0] for iv in intervals]
        self.intervals = intervals

        k = n_nonf
        S = self.S
        self.normal_of = [0] * S
        self.fallback_of = [0] * S
        for i in range(k):
            self.normal_of[1 + i] = i + 1
            self.fallback_of[1 + i] = i + 1
        for j in range(k + 1):
            self.normal_of[1 + k + j] = k + 1
            self.fallback_of[1 + k + j] = j
        # state index reached by placing an F-entry: (F, current fallback)
        self.fstate_of = [1 + k + self.fallback_of[s] for s in range(S)]

        self.choices = []  # per internal node: (bytearray of kind, a_star)
        self._mask1 = []   # per internal node: v1 action bitmask of subtree
        self._mask2 = []   # per internal node: v2 action bitmask of subtree

    # -- trie construction -------------------------------------------------

    def _build(self, lo, hi):
        i = bisect_right(self.starts, lo) - 1
        _s, e, v1, v2 = self.intervals[i]
        if e >= hi:  # [lo, hi) lies inside one constant-requirement interval
            return _Leaf(lo, hi, v1, v2)
        mid = (lo + hi) >> 1
        return _Internal(lo, hi, self._build(lo, mid), self._build(mid, hi))

    # -- leaf costs ----------------------------------------------------------

    def _leaf_table(self, v1, v2, span):
        """Cost of every canonical inherited state for a constant region."""
        k = self.k
        F = self.f_code
        t = [0] * self.S
        for s in range(self.S):
            i_n = self.normal_of[s]
            i_f = self.fallback_of[s]
            if v1 == 0:
                # no-match required: nothing may cover this region
                c = 0 if i_n == 0 else INF
            elif v1 != F:
                c = 0 if i_n == v1 else 1
            else:
                b = v2  # fallback requirement (never F)
                if i_n == F and i_f == b:
                    c = 0
                elif i_f == b:
                    c = 1  # one F-entry on the region; fallback inherited
                elif b == 0:
                    c = INF  # cannot un-match an inherited non-F entry
                elif span >= 2:
                    c = 3  # (region, b) + F on both child halves
                else:
                    c = INF  # a lone address must inherit its fallback
            t[s] = c
        return t

    # -- dynamic program -----------------------------------------------------

    def solve(self):
        root = self._build(0, FULL_SPACE)
        dp_root = self._solve_node(root)
        return root, dp_root[0]

    def _solve_node(self, node):
        if isinstance(node, _Leaf):
            return self._leaf_table(node.v1, node.v2, node.hi - node.lo)

        left, right = node.left, node.right
        L = self._solve_node(left)
        R = self._solve_node(right)
        k = self.k
        F = self.f_code

        # action masks of the subtree requirements
        m1 = 0
        m2 = 0
        for sub in (left, right):
            if isinstance(sub, _Leaf):
                if sub.v1:
                    m1 |= 1 << sub.v1
                if sub.v2:
                    m2 |= 1 << sub.v2
            else:
                m1 |= self._mask1[sub.idx]
                m2 |= self._mask2[sub.idx]
        mask = m1 | m2

        # best non-F placement (state independent)
        best_a = INF
        a_star = -1
        for i in range(k):
            if (mask >> (i + 1)) & 1:
                c = 1 + L[1 + i] + R[1 + i]
                if c < best_a:
                    best_a = c
                    a_star = i
        f_useful = (m1 >> F) & 1

        S = self.S
        fstate_of = self.fstate_of
        dp = [0] * S
        choice = bytearray(S)
        for s in range(S):
            best = L[s] + R[s]
            kind = NOPLACE
            if f_useful:
                fs = fstate_of[s]
                c = 1 + L[fs] + R[fs]
                if c < best:
                    best = c
                    kind = PLACE_F
            if best_a < best:
                best = best_a
                kind = PLACE_A
            dp[s] = best
            choice[s] = kind

        node.idx = len(self.choices)
        self.choices.append((choice, a_star))
        # remember masks for the parent
        self._mask1.append(m1)
        self._mask2.append(m2)
        return dp

    # -- reconstruction --------------------------------------------------------

    def reconstruct(self, root):
        out = []
        self._emit(root, 0, out)
        return out

    def _emit(self, node, s, out):
        F = self.f_code
        if isinstance(node, _Leaf):
            v1, v2 = node.v1, node.v2
            i_n = self.normal_of[s]
            i_f = self.fallback_of[s]
            if v1 == 0:
                return
            span = node.hi - node.lo
            if v1 != F:
                if i_n != v1:
                    out.append((node.lo, span, v1))
                return
            b = v2
            if i_n == F and i_f == b:
                return
            if i_f == b:
                out.append((node.lo, span, F))
                return
            # two-layer fix: fallback entry on the region, F on both halves
            out.append((node.lo, span, b))
            half = span >> 1
            out.append((node.lo, half, F))
            out.append((node.lo + half, half, F))
            return

        choice, a_star = self.choices[node.idx]
        kind = choice[s]
        span = node.hi - node.lo
        if kind == NOPLACE:
            self._emit(node.left, s, out)
            self._emit(node.right, s, out)
        elif kind == PLACE_F:
            out.append((node.lo, span, F))
            fs = self.fstate_of[s]
            self._emit(node.left, fs, out)
            self._emit(node.right, fs, out)
        else:
            out.append((node.lo, span, a_star + 1))
            st = 1 + a_star
            self._emit(node.left, st, out)
            self._emit(node.right, st, out)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def compress(routes, failed, capacity=None):
    """Compute the minimal equivalent table.

    ``routes``: list of (IPv4Network, action) with distinct prefixes.
    ``failed``: next-hop name withdrawn in the failure drill.
    ``capacity``: if given and the minimum exceeds it, the table is not
    materialized.

    Returns (min_entries, table_or_None).  ``table`` is a list of
    (prefix_str, action) sorted by prefix, or None when over capacity.
    """
    actions = sorted({a for _, a in routes})
    nonf = [a for a in actions if a != failed]
    code_of = {a: i + 1 for i, a in enumerate(nonf)}
    f_code = len(nonf) + 1
    action_of = {i + 1: a for i, a in enumerate(nonf)}
    action_of[f_code] = failed

    tab = [
        (int(net.network_address), int(net.broadcast_address), net.prefixlen, act)
        for net, act in routes
    ]
    intervals = _requirement_intervals(tab, failed, code_of, f_code)

    solver = _Solver(intervals, len(nonf))
    root, min_cost = solver.solve()

    if capacity is not None and min_cost > capacity:
        return min_cost, None

    raw = solver.reconstruct(root)
    table = [(_prefix_str(lo, span), action_of[code]) for lo, span, code in raw]
    table.sort(key=lambda pa: (int(IPv4Network(pa[0]).network_address),
                               IPv4Network(pa[0]).prefixlen))
    return min_cost, table


def lpm_action(tab, point, exclude=None):
    """LPM result (action string or None) at integer address ``point``."""
    best_len = -1
    best = None
    for start, end, plen, act in tab:
        if act == exclude:
            continue
        if plen > best_len and start <= point <= end:
            best_len = plen
            best = act
    return best


def verify(old_routes, new_routes, failed):
    """Check both tables agree on every address, before and after the drill.

    Compares LPM results on every elementary interval of the union of both
    tables' boundaries (results are constant between consecutive boundaries),
    for the full tables and for the tables with the failed next-hop withdrawn.
    Returns (ok, detail_dict).
    """
    old = [
        (int(n.network_address), int(n.broadcast_address), n.prefixlen, a)
        for n, a in old_routes
    ]
    new = [
        (int(n.network_address), int(n.broadcast_address), n.prefixlen, a)
        for n, a in new_routes
    ]
    bounds = {0, FULL_SPACE}
    for start, end, _p, _a in old + new:
        bounds.add(start)
        bounds.add(end + 1)
    pts = sorted(bounds)

    normal_ok = True
    failover_ok = True
    first_mismatch = None
    for i in range(len(pts) - 1):
        pt = pts[i]
        a_old = lpm_action(old, pt)
        a_new = lpm_action(new, pt)
        if a_old != a_new:
            normal_ok = False
            if first_mismatch is None:
                first_mismatch = {
                    "scenario": "normal",
                    "address": str(IPv4Address(pt)),
                    "old": a_old,
                    "new": a_new,
                }
        b_old = lpm_action(old, pt, exclude=failed)
        b_new = lpm_action(new, pt, exclude=failed)
        if b_old != b_new:
            failover_ok = False
            if first_mismatch is None:
                first_mismatch = {
                    "scenario": "failover",
                    "address": str(IPv4Address(pt)),
                    "old": b_old,
                    "new": b_new,
                }
    return normal_ok and failover_ok, {
        "normal_equivalent": normal_ok,
        "failover_equivalent": failover_ok,
        "checked_points": len(pts) - 1,
        "first_mismatch": first_mismatch,
    }
