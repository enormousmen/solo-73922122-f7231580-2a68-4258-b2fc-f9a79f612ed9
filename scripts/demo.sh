#!/usr/bin/env bash
# Demo: compress a table that fits the new device, then one that does not,
# then rearrange an ECMP group around a draining uplink (feasible + infeasible).
# Usage: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

read -r -d '' ROUTES <<'JSON' || true
[
  {"prefix": "10.0.0.0/8", "action": "NH1"},
  {"prefix": "10.1.0.0/16", "action": "NH2"},
  {"prefix": "10.2.0.0/16", "action": "NH1"},
  {"prefix": "10.3.0.0/16", "action": "drop"},
  {"prefix": "192.168.0.0/16", "action": "NH2"}
]
JSON

read -r -d '' ECMP <<'JSON' || true
{
  "group": "ecmp-core",
  "removed_nexthop": "NH2",
  "buckets": [
    {"bucket": 0, "current_nexthop": "NH1", "bytes_in_window": 4200000000, "active_connections": 1200},
    {"bucket": 1, "current_nexthop": "NH1", "bytes_in_window": 3800000000, "active_connections": 900},
    {"bucket": 2, "current_nexthop": "NH1", "bytes_in_window": 4600000000, "active_connections": 1500},
    {"bucket": 3, "current_nexthop": "NH2", "bytes_in_window": 5000000000, "active_connections": 2100},
    {"bucket": 4, "current_nexthop": "NH2", "bytes_in_window": 2400000000, "active_connections": 700},
    {"bucket": 5, "current_nexthop": "NH3", "bytes_in_window": 3100000000, "active_connections": 800},
    {"bucket": 6, "current_nexthop": "NH3", "bytes_in_window": 2900000000, "active_connections": 650},
    {"bucket": 7, "current_nexthop": "NH4", "bytes_in_window": 1800000000, "active_connections": 400}
  ],
  "retained": [
    {"nexthop": "NH1", "bucket_count": 3, "byte_budget": 15000000000},
    {"nexthop": "NH3", "bucket_count": 3, "byte_budget": 14000000000},
    {"nexthop": "NH4", "bucket_count": 2, "byte_budget": 5000000000}
  ]
}
JSON

echo "=== 1) capacity sufficient (capacity=4, drill: NH2 fails) ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 4,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 2) capacity insufficient (capacity=3): only the minimum count is reported ==="
curl -sS -X POST "$BASE/compress" -H 'Content-Type: application/json' -d "{
  \"routes\": $ROUTES,
  \"capacity\": 3,
  \"failed_nexthop\": \"NH2\"
}" | python3 -m json.tool

echo
echo "=== 3) ECMP rearrange before removing NH2: mapping + migrated connections ==="
curl -sS -X POST "$BASE/ecmp/rearrange" -H 'Content-Type: application/json' -d "$ECMP" \
  | python3 -m json.tool

echo
echo "=== 4) same group, NH4 byte budget too tight: infeasible only ==="
TIGHT=$(python3 - "$ECMP" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
for target in payload["retained"]:
    if target["nexthop"] == "NH4":
        target["byte_budget"] = 1
print(json.dumps(payload))
PY
)
curl -sS -X POST "$BASE/ecmp/rearrange" -H 'Content-Type: application/json' -d "$TIGHT" \
  | python3 -m json.tool
