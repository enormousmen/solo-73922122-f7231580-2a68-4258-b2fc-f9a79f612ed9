#!/usr/bin/env bash
# Demo: compress a table that fits the new device, then one that does not.
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
