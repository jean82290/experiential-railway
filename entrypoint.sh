#!/bin/bash
# Adapts the Experiential gateway to Railway, then supervises it and Caddy.
#
#   1. bootstrap.py configures the gateway from your variables (every boot,
#      idempotent; prints the first API key once).
#   2. `exp run` serves the gateway on 127.0.0.1:EXP_INTERNAL_PORT, the only
#      address it can listen on.
#   3. Caddy listens on $PORT, the port Railway routes to, and forwards the
#      public routes (see the Caddyfile).
#
# If either process stops, the container stops and Railway restarts it.
# Railway's stop signal is passed on to both, so in-flight requests drain.
#
# Variables (all optional except one provider key):
#   OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
#   EXP_ENDPOINT_BASE_URL (+ EXP_ENDPOINT_API_KEY)  any OpenAI-compatible server
#   EXP_MODELS               provider:model list, e.g. openai:gpt-5.6-terra
#   EXP_ADMIN_PASSWORD       password for the organization-wide /usage page
#   EXP_CAPTURE_TRAFFIC      true = store request contents (default false)
#   EXP_TELEMETRY            1 = send Experiential's anonymous telemetry (default 0)
#   PORT                     public port Railway routes to (default 8080)
#   EXP_DATA_DIR             volume mount path (default /data)
set -euo pipefail

log() { echo "exp-railway: $*"; }
die() { echo "exp-railway: ERROR: $*" >&2; exit 1; }

PORT="${PORT:-8080}"
case "$PORT" in
  ''|*[!0-9]*) die "PORT must be a number, got '$PORT'" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "PORT must be between 1 and 65535, got '$PORT'"

INTERNAL_PORT="${EXP_INTERNAL_PORT:-8000}"
if [ "$INTERNAL_PORT" = "$PORT" ]; then
  INTERNAL_PORT=18000
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${EXP_DATA_DIR:-/data}"
export EXP_DATA_DIR="$DATA_DIR"
export EXP_ROOT="$DATA_DIR/.exp"
mkdir -p "$EXP_ROOT"
cd "$DATA_DIR"

# A Railway volume shows up as a mount point. Without one, every redeploy
# starts from scratch: new key, empty usage history, lost identities.
if ! awk -v d="$DATA_DIR" '$2 == d { found = 1 } END { exit !found }' /proc/mounts 2>/dev/null; then
  log "WARNING: $DATA_DIR is not a mounted volume. Keys, identities and usage will be LOST on every redeploy."
  log "WARNING: attach a Railway volume with mount path $DATA_DIR."
fi

# Admin password for the organization-wide usage page and metrics.
if [ -n "${EXP_ADMIN_PASSWORD:-}" ]; then
  EXP_ADMIN_PASSWORD_HASH=$(printf '%s\n' "$EXP_ADMIN_PASSWORD" | caddy hash-password --algorithm bcrypt --bcrypt-cost 11) \
    || die "could not hash EXP_ADMIN_PASSWORD"
else
  # No password set: lock the admin routes with a random one nobody knows.
  random_password=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  EXP_ADMIN_PASSWORD_HASH=$(printf '%s\n' "$random_password" | caddy hash-password --algorithm bcrypt --bcrypt-cost 11) \
    || die "could not hash a random admin password"
  unset random_password
  log "EXP_ADMIN_PASSWORD is empty: the organization-wide /usage page is disabled"
fi
export EXP_ADMIN_PASSWORD_HASH EXP_INTERNAL_PORT="$INTERNAL_PORT" PORT

bootstrap_status=0
python -I "$APP_DIR/bootstrap.py" || bootstrap_status=$?
if [ "$bootstrap_status" -eq 3 ]; then
  # No usable model yet (Experiential will not start without one). Stay up
  # with a page that says what to set, instead of crash-looping.
  log "serving the setup page on port $PORT until a provider key is set"
  exec caddy run --config "$APP_DIR/Caddyfile.setup" --adapter caddyfile
fi
[ "$bootstrap_status" -eq 0 ] || die "gateway configuration failed (see the messages above)"

capture_flag=(--ghost)
case "${EXP_CAPTURE_TRAFFIC:-false}" in
  1|true|TRUE|True|yes|on)
    capture_flag=()
    log "EXP_CAPTURE_TRAFFIC is on: request and response contents are stored in $EXP_ROOT/gateway"
    ;;
esac

version=$(python -I -c 'import importlib.metadata as m; print(m.version("experiential"))' 2>/dev/null || echo unknown)
log "starting Experiential $version on 127.0.0.1:$INTERNAL_PORT, public port $PORT"
exp run --root "$EXP_ROOT" --port "$INTERNAL_PORT" --non-interactive "${capture_flag[@]}" &
gateway_pid=$!
caddy run --config "$APP_DIR/Caddyfile" --adapter caddyfile &
caddy_pid=$!

stopping=0
stop() {
  stopping=1
  kill -TERM "$gateway_pid" "$caddy_pid" 2>/dev/null || true
}
trap stop TERM INT

status=0
wait -n "$gateway_pid" "$caddy_pid" || status=$?
if [ "$stopping" -eq 1 ]; then
  log "stop signal received, draining in-flight requests"
else
  log "ERROR: a process exited on its own (status $status), stopping the container"
fi
stop
wait "$gateway_pid" 2>/dev/null || true
wait "$caddy_pid" 2>/dev/null || true
if [ "$stopping" -eq 1 ] && [ "$status" -ge 128 ]; then
  exit 0
fi
exit 1
