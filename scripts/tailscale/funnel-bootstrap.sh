#!/bin/sh
set -eu

SOCKET="${TS_SOCKET:-/tmp/tailscaled.sock}"
FUNNEL_PORT="${TS_FUNNEL_PORT:-4000}"
HEALTH_URL="${TS_FUNNEL_HEALTH_URL:-http://127.0.0.1:4000/health}"
HEALTH_TIMEOUT_SECONDS="${TS_FUNNEL_HEALTH_TIMEOUT_SECONDS:-60}"

/usr/local/bin/containerboot &
BOOT_PID=$!

until tailscale --socket="$SOCKET" status >/dev/null 2>&1; do
  if ! kill -0 "$BOOT_PID" 2>/dev/null; then
    wait "$BOOT_PID"
  fi
  sleep 1
done

echo "[tailscale] waiting for backend health at ${HEALTH_URL}"
start_ts="$(date +%s)"
while true; do
  if wget -q -O /dev/null "$HEALTH_URL" >/dev/null 2>&1; then
    break
  fi
  now_ts="$(date +%s)"
  if [ $((now_ts - start_ts)) -ge "$HEALTH_TIMEOUT_SECONDS" ]; then
    echo "[tailscale] backend health check timed out after ${HEALTH_TIMEOUT_SECONDS}s"
    exit 1
  fi
  if ! kill -0 "$BOOT_PID" 2>/dev/null; then
    wait "$BOOT_PID"
  fi
  sleep 1
done

echo "[tailscale] clearing stale funnel config"
tailscale --socket="$SOCKET" funnel reset || true
tailscale --socket="$SOCKET" serve reset || true

echo "[tailscale] enabling funnel on port ${FUNNEL_PORT}"
tailscale --socket="$SOCKET" funnel --bg "$FUNNEL_PORT" || true

echo "[tailscale] funnel status"
tailscale --socket="$SOCKET" funnel status || true

wait "$BOOT_PID"
