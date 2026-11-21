#!/usr/bin/env python3
"""Independent re-check of a /ecmp/rearrange response.

Not part of the service: a small operator-side validator that takes the
request and response JSON and proves the mapping honours every hard
constraint.  Used by scripts/demo.sh and handy during a change window.

Usage: python3 scripts/check_ecmp_response.py request.json response.json
"""

import json
import sys


def main(request_path, response_path):
    with open(request_path, encoding="utf-8") as f:
        req = json.load(f)
    with open(response_path, encoding="utf-8") as f:
        resp = json.load(f)

    buckets = req["buckets"]
    retained = req["retained"]
    removed = req["removed_nexthop"]

    if not resp["feasible"]:
        print("response reports infeasible; nothing to check")
        return 0

    assignment = {int(k): v for k, v in resp["assignment"].items()}
    assert set(assignment) == {b["bucket"] for b in buckets}, "bucket set mismatch"
    assert removed not in set(assignment.values()), "assigned to removed next-hop"

    by_hop = {t["nexthop"]: [] for t in retained}
    for b in buckets:
        by_hop[assignment[b["bucket"]]].append(b)
    for t in retained:
        got = by_hop[t["nexthop"]]
        assert len(got) == t["bucket_count"], f"{t['nexthop']}: bucket count"
        assert (
            sum(x["bytes_in_window"] for x in got) <= t["byte_budget"]
        ), f"{t['nexthop']}: over budget"

    migrated = sum(
        b["active_connections"]
        for b in buckets
        if assignment[b["bucket"]] != b["current_nexthop"]
    )
    assert migrated == resp["migrated_connections"], "migration count mismatch"
    print(f"OK: {len(buckets)} buckets, migrated {migrated} connections")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
