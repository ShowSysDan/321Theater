#!/usr/bin/env bash
# 3·2·1→Theater gateway — VPS install / update script (cyclorama).
#
# Installs everything the gateway needs on a Debian VPS with firewalld:
# packages (caddy, python3-venv, fail2ban), the service user, /opt/321gateway
# + venv, /etc/321gateway/gateway.env (GATE_SECRET_KEY auto-generated),
# the systemd unit, the Caddyfile, firewalld rules, and the fail2ban jail.
#
# Idempotent: re-running is the UPDATE path — it re-syncs the app files from
# this directory, re-installs requirements, and restarts the service. It
# never overwrites an existing gateway.env or an existing Caddyfile that
# already serves the site.
#
# Usage:  sudo bash install.sh        (run from this gateway/ directory —
#                                      e.g. the sparse checkout in
#                                      /opt/321gateway-src/gateway)

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/321gateway"
ENV_DIR="/etc/321gateway"
ENV_FILE="${ENV_DIR}/gateway.env"
SERVICE_FILE="/etc/systemd/system/321gateway.service"
CADDYFILE="/etc/caddy/Caddyfile"
SITE="dpc.321.theater"
RUN_USER="gateway"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   3·2·1→THEATER — Public Gateway (VPS)   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

[ "$(id -u)" -eq 0 ] || error "Run as root: sudo bash install.sh"
[ -f "${SRC_DIR}/gateway_app.py" ] || error "gateway_app.py not found next to this script — run it from the gateway/ directory."

# ── Packages ──────────────────────────────────────────────────────────────────
step "Installing packages (caddy, python3-venv, fail2ban)..."
if command -v apt-get &>/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy python3-venv fail2ban curl rsync
else
    warn "apt-get not found — install caddy, python3-venv, fail2ban manually, then re-run."
fi
command -v caddy &>/dev/null || error "caddy is not installed."

# ── Service user ──────────────────────────────────────────────────────────────
step "Service user '${RUN_USER}'..."
if id "${RUN_USER}" &>/dev/null; then
    info "User exists."
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "${RUN_USER}"
    info "Created system user '${RUN_USER}'."
fi

# ── App files + venv ──────────────────────────────────────────────────────────
step "Syncing app files to ${APP_DIR}..."
mkdir -p "${APP_DIR}"
rsync -a --exclude venv --exclude install.sh --exclude '*.env*' \
    "${SRC_DIR}/" "${APP_DIR}/"
info "Files synced from ${SRC_DIR}."

step "Python virtual environment..."
if [ ! -f "${APP_DIR}/venv/bin/pip" ]; then
    python3 -m venv "${APP_DIR}/venv"
    info "Created venv."
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"
info "Dependencies installed."

# ── Environment / secrets ─────────────────────────────────────────────────────
step "Configuration (${ENV_FILE})..."
mkdir -p "${ENV_DIR}"
if [ -f "${ENV_FILE}" ]; then
    info "gateway.env already exists — leaving it untouched."
else
    GATE_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    cat > "${ENV_FILE}" << EOF
# 3·2·1→Theater gateway configuration. See gateway.env.example for docs.
GATE_SECRET_KEY=${GATE_SECRET_KEY}
# MUST equal GATEWAY_SHARED_SECRET in the app server's .env — fill this in:
GATE_SHARED_SECRET=
GATE_APP_INTERNAL_URL=http://10.201.2.101:5400
GATE_SESSION_HOURS=12
GATE_COOKIE_NAME=__Host-321gate
EOF
    info "Created gateway.env with a fresh GATE_SECRET_KEY."
fi
chown "root:${RUN_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"
if ! grep -q '^GATE_SHARED_SECRET=..*' "${ENV_FILE}"; then
    NEED_SECRET=1
    warn "GATE_SHARED_SECRET is empty — the gateway will refuse to start."
    warn "Set it to the app server's GATEWAY_SHARED_SECRET value:"
    warn "    sudo nano ${ENV_FILE} && sudo systemctl restart 321gateway"
else
    NEED_SECRET=0
fi

# ── systemd service ───────────────────────────────────────────────────────────
step "systemd service..."
cp "${SRC_DIR}/321gateway.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable 321gateway >/dev/null 2>&1
if [ "${NEED_SECRET}" -eq 0 ]; then
    systemctl restart 321gateway
    sleep 1
    if curl -sf http://127.0.0.1:8100/__gate/healthz >/dev/null; then
        info "321gateway is running (healthz OK)."
    else
        warn "321gateway did not answer healthz — check: journalctl -u 321gateway -n 30"
    fi
else
    info "Service installed + enabled; start it after setting GATE_SHARED_SECRET."
fi

# ── Caddy ─────────────────────────────────────────────────────────────────────
step "Caddy..."
if [ -f "${CADDYFILE}" ] && grep -q "${SITE}" "${CADDYFILE}"; then
    info "Caddyfile already serves ${SITE} — leaving it untouched."
else
    if [ -f "${CADDYFILE}" ]; then
        cp "${CADDYFILE}" "${CADDYFILE}.bak.$(date +%Y%m%d%H%M%S)"
        warn "Existing Caddyfile backed up alongside it."
    fi
    cp "${SRC_DIR}/Caddyfile.example" "${CADDYFILE}"
    info "Installed Caddyfile for ${SITE}."
fi
if caddy validate --config "${CADDYFILE}" >/dev/null 2>&1; then
    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl reload caddy 2>/dev/null || systemctl restart caddy
    info "Caddy reloaded. (Certificate issuance needs DNS for ${SITE} → this VPS and port 80/443 open.)"
else
    warn "Caddyfile failed validation — fix it, then: systemctl reload caddy"
fi

# ── firewalld ─────────────────────────────────────────────────────────────────
step "Firewall (firewalld)..."
if command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null; then
    firewall-cmd --permanent --add-service=http  >/dev/null
    firewall-cmd --permanent --add-service=https >/dev/null
    firewall-cmd --reload >/dev/null
    info "http + https allowed (no WireGuard rule needed — this VPS dials out)."
else
    warn "firewalld not running — open ports 80 and 443 with your firewall of choice."
fi

# ── fail2ban ──────────────────────────────────────────────────────────────────
step "fail2ban jail for OTP guessing..."
if command -v fail2ban-client &>/dev/null; then
    cat > /etc/fail2ban/filter.d/321gateway.conf << 'EOF'
[Definition]
failregex = GATE_OTP_FAIL ip=<HOST>
journalmatch = _SYSTEMD_UNIT=321gateway.service
EOF
    cat > /etc/fail2ban/jail.d/321gateway.conf << 'EOF'
[321gateway]
enabled  = true
backend  = systemd
filter   = 321gateway
banaction = firewallcmd-rich-rules
maxretry = 10
findtime = 600
bantime  = 3600
EOF
    systemctl restart fail2ban
    info "Jail active: 10 failed codes / 10 min → 1 h ban (via firewalld rich rules)."
else
    warn "fail2ban not installed — skipping jail."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}══════════════════════ Done ══════════════════════${NC}"
if [ "${NEED_SECRET}" -eq 1 ]; then
    echo -e "${YELLOW}NEXT STEP:${NC} set GATE_SHARED_SECRET in ${ENV_FILE}"
    echo "  (must equal GATEWAY_SHARED_SECRET on the app server), then:"
    echo "      sudo systemctl restart 321gateway"
fi
echo "Checks:"
echo "  systemctl status 321gateway caddy"
echo "  curl -s http://127.0.0.1:8100/__gate/healthz     # {\"ok\": true}"
echo "  curl -s https://${SITE}/robots.txt               # once DNS is live"
echo "Update later: git pull in the checkout, then re-run this script."
