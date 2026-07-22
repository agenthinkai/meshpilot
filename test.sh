#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  MeshPilot — smoke test
#
#  Run ON THE SERVER after deploy.sh:
#      bash test.sh
#
#  1. Waits for /health
#  2. Sends a real inference request
#  3. PASS if the response is valid JSON containing a "text" field
#
#  Override the target if needed:  BASE=http://127.0.0.1:8100 bash test.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8100}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
INFER_TIMEOUT="${INFER_TIMEOUT:-180}"

pass() { printf '\n\033[32mPASS\033[0m — %s\n' "$*"; exit 0; }
fail() { printf '\n\033[31mFAIL\033[0m — %s\n' "$*"; exit 1; }

command -v curl >/dev/null || fail "curl is not installed"

# ── 1. Wait for /health ──────────────────────────────────────────────────────
printf '▶ Waiting for %s/health (up to %ss)' "$BASE" "$HEALTH_TIMEOUT"
deadline=$((SECONDS + HEALTH_TIMEOUT))
healthy=0
while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 "$BASE/health" >/dev/null 2>&1; then
        healthy=1; break
    fi
    printf '.'; sleep 3
done
printf '\n'
if [[ "$healthy" -ne 1 ]]; then
    fail "API never became healthy at $BASE/health after ${HEALTH_TIMEOUT}s.
       Check:  cd /opt/meshpilot && docker compose ps
               docker compose logs --tail=50 api nginx"
fi
echo "  ✓ /health OK: $(curl -s --max-time 5 "$BASE/health")"

# ── 2. Inference request ─────────────────────────────────────────────────────
# Try the documented endpoints in order — the first that is not 404 is used, so
# this keeps working if the route is mounted under a different prefix.
PROMPT='Say hello in exactly three words.'
PAYLOAD=$(printf '{"prompt":%s,"max_tokens":32}' "$(printf '%s' "$PROMPT" | sed 's/"/\\"/g; s/^/"/; s/$/"/')")

BODY=""; ENDPOINT=""; CODE=""
for ep in /api/v1/inference /api/inference /api/v1/generate /api/generate; do
    printf '▶ POST %s%s\n' "$BASE" "$ep"
    resp=$(curl -s -w '\n%{http_code}' --max-time "$INFER_TIMEOUT" \
             -X POST "$BASE$ep" \
             -H 'Content-Type: application/json' \
             -d "$PAYLOAD" 2>&1)
    code=$(printf '%s' "$resp" | tail -1)
    body=$(printf '%s' "$resp" | sed '$d')
    echo "  HTTP $code"
    if [[ "$code" != "404" && -n "$code" ]]; then
        ENDPOINT="$ep"; CODE="$code"; BODY="$body"; break
    fi
done

[[ -n "$ENDPOINT" ]] || fail "No inference endpoint responded (all returned 404).
       Check the mounted routes:  curl -s $BASE/openapi.json | head -c 2000"

# ── 3. Validate ──────────────────────────────────────────────────────────────
if [[ "$CODE" == "401" || "$CODE" == "403" ]]; then
    fail "Inference endpoint requires authentication (HTTP $CODE).
       Create an API key first, then re-run with the key attached.
       Response: $(printf '%s' "$BODY" | head -c 300)"
fi

if [[ "$CODE" -lt 200 || "$CODE" -ge 300 ]]; then
    fail "Inference returned HTTP $CODE from $ENDPOINT
       Response: $(printf '%s' "$BODY" | head -c 500)"
fi

# Valid JSON?
if ! printf '%s' "$BODY" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
    fail "Response from $ENDPOINT is not valid JSON.
       Raw: $(printf '%s' "$BODY" | head -c 500)"
fi

# Contains a "text" field (top level or nested one level down)?
TEXT=$(printf '%s' "$BODY" | python3 -c '
import json,sys
d=json.load(sys.stdin)
def find(o):
    if isinstance(o,dict):
        if isinstance(o.get("text"),str): return o["text"]
        for v in o.values():
            r=find(v)
            if r is not None: return r
    if isinstance(o,list):
        for v in o:
            r=find(v)
            if r is not None: return r
    return None
t=find(d)
print(t if t is not None else "__NO_TEXT_FIELD__")
' 2>/dev/null)

if [[ "$TEXT" == "__NO_TEXT_FIELD__" || -z "$TEXT" ]]; then
    fail "Response is valid JSON but has no \"text\" field.
       Endpoint: $ENDPOINT
       Response: $(printf '%s' "$BODY" | head -c 500)"
fi

echo "  ✓ endpoint : $ENDPOINT"
echo "  ✓ generated: $(printf '%s' "$TEXT" | head -c 200)"
pass "inference returned valid JSON with a \"text\" field"
