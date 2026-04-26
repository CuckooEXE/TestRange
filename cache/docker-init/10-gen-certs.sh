#!/bin/sh
# Generate a self-signed cert on first boot if /etc/nginx/certs is empty.
# The nginx official entrypoint runs everything in /docker-entrypoint.d/
# (matching *.sh) before exec'ing nginx, so this lands certs in place
# before the main process needs them.

set -eu

# Bind-mounted ./storage/ comes up root-owned on first start; chown
# to the user nginx workers run as so PUT can write to it.
chown -R nginx:nginx /srv/cache 2>/dev/null || true

CERT_DIR=/etc/nginx/certs
CRT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"

if [ -f "$CRT" ] && [ -f "$KEY" ]; then
    echo "[gen-certs] reusing existing cert at $CRT"
    exit 0
fi

mkdir -p "$CERT_DIR"

# nginx:alpine doesn't ship openssl by default; install it on first
# cert-gen.  The early-return above means this only runs once per
# bind-mounted ./certs/ directory.
if ! command -v openssl >/dev/null 2>&1; then
    apk add --no-cache openssl
fi

openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout "$KEY" \
    -out    "$CRT" \
    -subj "/CN=testrange-cache"

echo "[gen-certs] generated self-signed cert at $CRT"
