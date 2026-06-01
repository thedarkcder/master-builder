#!/usr/bin/env sh
set -eu

if [ -z "${AUTHORIZED_KEY_FILE:-}" ] || [ ! -f "$AUTHORIZED_KEY_FILE" ]; then
  echo "AUTHORIZED_KEY_FILE is required and must point to a mounted public key." >&2
  exit 1
fi

mkdir -p /root/.ssh
cat "$AUTHORIZED_KEY_FILE" > /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys

exec /usr/sbin/sshd -D -e
