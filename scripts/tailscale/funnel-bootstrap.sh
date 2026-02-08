#!/bin/sh
set -eu

SOCKET="${TS_SOCKET:-/tmp/tailscaled.sock}"
FUNNEL_PORT="${TS_FUNNEL_PORT:-4000}"

/usr/local/bin/containerboot &
BOOT_PID=$!

until tailscale --socket="$SOCKET" status >/dev/null 2>&1; do
  if ! kill -0 "$BOOT_PID" 2>/dev/null; then
    wait "$BOOT_PID"
  fi
  sleep 1
done

echo "[tailscale] enabling funnel on port ${FUNNEL_PORT}"
tailscale --socket="$SOCKET" funnel --bg "$FUNNEL_PORT" || true

echo "[tailscale] funnel status"
tailscale --socket="$SOCKET" funnel status || true

wait "$BOOT_PID"
