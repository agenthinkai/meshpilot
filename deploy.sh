#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  MeshPilot — deploy to /opt/meshpilot on the existing Hetzner box
#
#  Safe to re-run. It does NOT touch the running agenthinkmesh.com stack
#  (pitchproof-*) and never binds host ports 80/443.
#
#  Usage (from the uploaded project directory):
#      sudo bash deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TARGET=/opt/meshpilot
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_FREE_MB=4096

say()  { printf '\n▶ %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }
die()  { printf '\n✗ %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash deploy.sh"

# ── 1. Docker + Compose ──────────────────────────────────────────────────────
say "Checking Docker..."
if command -v docker >/dev/null 2>&1; then
    ok "docker present ($(docker --version | cut -d, -f1))"
else
    warn "docker not found — installing via get.docker.com"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "docker installed"
fi

if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin present ($(docker compose version --short))"
else
    warn "docker compose plugin not found — installing"
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    docker compose version >/dev/null 2>&1 || die "docker compose still unavailable"
    ok "docker compose installed"
fi

# ── 2. RAM check ─────────────────────────────────────────────────────────────
say "Checking memory..."
FREE_MB=$(free -m | awk '/^Mem:/{print $7}')      # "available", not "free"
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
echo "  total ${TOTAL_MB}MB · available ${FREE_MB}MB"
if [[ "$FREE_MB" -lt "$MIN_FREE_MB" ]]; then
    warn "Less than ${MIN_FREE_MB}MB available."
    warn "MeshPilot + llama.cpp will be tight and may be OOM-killed under load."
    warn "The existing agenthinkmesh.com stack shares this RAM."
    read -rp "  Continue anyway? [y/N] " a </dev/tty || a=n
    [[ "$a" =~ ^[Yy]$ ]] || die "Aborted by user."
else
    ok "sufficient memory available"
fi

# ── 3. Port check — never collide with the live site ─────────────────────────
say "Checking port 8100..."
if ss -tln 2>/dev/null | grep -q '127.0.0.1:8100 '; then
    warn "8100 already bound — assuming a previous MeshPilot deploy (will be replaced)."
elif ss -tln 2>/dev/null | grep -qE '(^|[^0-9])8100 '; then
    die "Port 8100 is in use by something else. Free it or change the mapping."
else
    ok "8100 free"
fi

# ── 4. Copy project files ────────────────────────────────────────────────────
say "Installing to ${TARGET}..."
mkdir -p "$TARGET"
if [[ "$SRC" != "$TARGET" ]]; then
    # --exclude .env so a server-side .env with the real password is never
    # clobbered by a re-upload.
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude '.env' --exclude '.git' "$SRC"/ "$TARGET"/
    else
        cp -r "$SRC"/. "$TARGET"/
    fi
    ok "files copied"
else
    ok "already running from ${TARGET}"
fi

# Model bind-mount target MUST exist or `docker compose up` fails outright.
mkdir -p "$TARGET/models" "$TARGET/infra/nginx/certs"
ok "model dir ready: ${TARGET}/models"

# ── 5. .env ──────────────────────────────────────────────────────────────────
say "Checking .env..."
cd "$TARGET"
if [[ -f .env ]]; then
    ok ".env already present (left untouched)"
else
    [[ -f .env.example ]] || die "no .env and no .env.example to copy from"
    cp .env.example .env
    warn "created .env from .env.example — you MUST fill in SECRET_KEY / ADMIN_PASSWORD"
fi
chmod 600 .env
ok ".env permissions set to 600"

if grep -q 'CHANGE_ME_ON_SERVER\|change-me' .env; then
    warn "Placeholder values still in .env. Set the admin password now:"
    warn "  read -rsp 'password: ' P && sed -i \"s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=\$P|\" .env && unset P"
    read -rp "  Continue with placeholders still set? [y/N] " a </dev/tty || a=n
    [[ "$a" =~ ^[Yy]$ ]] || die "Aborted — set the password, then re-run."
fi

# ── 6. Build + start ─────────────────────────────────────────────────────────
say "Building and starting containers (first build takes several minutes)..."
docker compose up -d --build

# ── 7. Wait for health ───────────────────────────────────────────────────────
say "Waiting for containers to become healthy (up to 5 minutes)..."
deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
    unhealthy=$(docker compose ps --format '{{.Name}} {{.Status}}' \
                 | grep -Ec 'starting|unhealthy|Restarting' || true)
    exited=$(docker compose ps --format '{{.Name}} {{.Status}}' | grep -Ec 'Exit|dead' || true)
    if [[ "$exited" -gt 0 ]]; then
        docker compose ps
        die "A container exited. Inspect with: docker compose logs --tail=50"
    fi
    if [[ "$unhealthy" -eq 0 ]]; then
        ok "all containers up"
        break
    fi
    sleep 5
done
(( SECONDS < deadline )) || warn "Timed out waiting for health — check: docker compose ps"

# API responds?
say "Probing the API through nginx on 8100..."
for i in $(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:8100/health >/dev/null 2>&1; then
        ok "/health responding"
        break
    fi
    [[ $i -eq 30 ]] && warn "/health not responding yet — see: docker compose logs api"
    sleep 3
done

# ── 8. Done ──────────────────────────────────────────────────────────────────
docker compose ps
cat <<EOF

═══════════════════════════════════════════════════════════════════
 ✓ MeshPilot deployed

   Internal URL : http://localhost:8100      (localhost-only, by design)
   Install dir  : ${TARGET}

   NEXT STEP — it is not reachable from the internet yet:
     1. Add a DNS A record: meshpilot.agenthinkmesh.com -> 88.198.91.81
     2. Add the site block from meshpilot-caddy.conf to
        /opt/pitchproof/infra/Caddyfile, then reload Caddy.
        ⚠ READ README-DEPLOY.md STEP 8 FIRST — reloading Caddy with the
          current on-disk file WILL take api.agenthinkmesh.com offline.

   Verify locally :  bash test.sh
   Logs           :  cd ${TARGET} && docker compose logs -f
   Stop           :  cd ${TARGET} && docker compose down
═══════════════════════════════════════════════════════════════════
EOF
