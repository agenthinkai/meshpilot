#!/bin/sh
# Makes sure nginx has a TLS certificate. A company's own certificate, placed in
# infra/nginx/certs as fullchain.pem and privkey.pem, is never touched. Otherwise
# this creates a self-signed one, and replaces it when it nears expiry or when
# MESHPILOT_HOSTNAMES changes.
set -eu

CERT_DIR=/etc/nginx/certs
CERT="$CERT_DIR/fullchain.pem"
KEY="$CERT_DIR/privkey.pem"
MARKER="$CERT_DIR/.self-signed"
NAMES="${MESHPILOT_HOSTNAMES:-localhost}"
THIRTY_DAYS=2592000

if [ -s "$CERT" ] && [ -s "$KEY" ]; then
    [ -f "$MARKER" ] || exit 0
    if [ "$(cat "$MARKER")" = "$NAMES" ] && openssl x509 -checkend "$THIRTY_DAYS" -noout -in "$CERT" >/dev/null; then
        exit 0
    fi
fi

san="DNS:localhost,IP:127.0.0.1"
first=""
for name in $(echo "$NAMES" | tr ',' ' '); do
    [ -n "$first" ] || first="$name"
    [ "$name" = "localhost" ] && continue
    case "$name" in
        *[!0-9.]*) san="$san,DNS:$name" ;;
        *)         san="$san,IP:$name" ;;
    esac
done

mkdir -p "$CERT_DIR"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
    -subj "/CN=${first:-localhost}/O=MeshPilot" -addext "subjectAltName=$san" \
    -keyout "$KEY" -out "$CERT"
chmod 600 "$KEY"
printf '%s' "$NAMES" > "$MARKER"
echo "MeshPilot: created a self-signed certificate for $san"
