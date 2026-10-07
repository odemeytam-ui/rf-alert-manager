#!/usr/bin/env bash
# Smoke test against a running deployment. Exit code != 0 means unhealthy.
#
#   deploy/smoke_test.sh http://localhost
set -euo pipefail
BASE="${1:-http://localhost}"

fail() { echo "SMOKE TEST FAILED: $*" >&2; exit 1; }

echo "==> waiting for ${BASE}/health"
for i in $(seq 1 30); do
  if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 30 ] && fail "/health not healthy after 60s"
  sleep 2
done

curl -fsS "${BASE}/health" | grep -q '"database":"ok"' || fail "ingest: database not ok"
curl -fsS "${BASE}/alerts-service/health" | grep -q '"status":"ok"' || fail "alert service unhealthy"

# Out-of-band observation from a dedicated sensor id: exercises validation + DB +
# broker without being able to trigger any rule.
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/api/v1/observations" \
  -H 'Content-Type: application/json' \
  -d "{\"sensor_id\":\"smoke-test\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"frequency_mhz\":100.0,\"bandwidth_khz\":200,\"power_dbm\":-100,\"location\":{\"lat\":0,\"lon\":0}}")
[ "$code" = 201 ] || fail "POST /api/v1/observations returned $code"

curl -fsS "${BASE}/api/v1/rules" >/dev/null || fail "GET /api/v1/rules"
curl -fsS "${BASE}/api/v1/alerts" >/dev/null || fail "GET /api/v1/alerts"
curl -fsS "${BASE}/metrics" | grep -q rfam_observations_total || fail "/metrics"

echo "==> smoke test passed"
