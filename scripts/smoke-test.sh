#!/bin/sh
# Smoke test for a deployed Experiential gateway: health, key checks, the
# models list, the protected usage page and, optionally, one real model call.
#
# Usage:
#   sh scripts/smoke-test.sh <url> <gateway-key> [admin-password] [model-alias]
#
# The model call is skipped unless you pass a model alias; it spends a few
# tokens on your provider account.
set -eu

URL="${1:?usage: smoke-test.sh <url> <gateway-key> [admin-password] [model-alias]}"
KEY="${2:?usage: smoke-test.sh <url> <gateway-key> [admin-password] [model-alias]}"
ADMIN="${3:-}"
MODEL="${4:-}"
URL="${URL%/}"
fails=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (expected $2, got $3)"; fails=$((fails + 1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "Testing $URL"
check "health check answers without a key"     200 "$(code "$URL/health/ready")"
check "API refuses a request with no key"      401 "$(code "$URL/v1/models")"
check "API refuses a wrong key"                401 "$(code -H 'Authorization: Bearer wrong' "$URL/v1/models")"
check "API accepts the gateway key"            200 "$(code -H "Authorization: Bearer $KEY" "$URL/v1/models")"
models=$(curl -s -H "Authorization: Bearer $KEY" "$URL/v1/models" | grep -o '"id":"[^"]*"' | cut -d'"' -f4 | tr '\n' ' ')
echo "       models: ${models:-none}"
check "organization usage needs the password"  401 "$(code "$URL/usage.json")"
check "metrics need the password"              401 "$(code "$URL/metrics")"
check "a key sees its own usage"               200 "$(code -H "Authorization: Bearer $KEY" "$URL/usage.json")"
if [ -n "$ADMIN" ]; then
  check "the admin password opens usage"       200 "$(code -u "admin:$ADMIN" "$URL/usage.json")"
fi
check "unknown paths are not exposed"          404 "$(code "$URL/not-a-route")"

if [ -n "$MODEL" ]; then
  reply=$(curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    "$URL/v1/chat/completions" \
    -d "{\"model\":\"$MODEL\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word ok.\"}]}")
  if printf '%s' "$reply" | grep -q '"choices"'; then
    echo "  ok   $MODEL answered through the gateway"
  else
    echo "  FAIL $MODEL did not answer: $reply"
    fails=$((fails + 1))
  fi
fi

if [ "$fails" -eq 0 ]; then echo "All checks passed."; else echo "$fails check(s) failed."; exit 1; fi
