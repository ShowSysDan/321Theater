#!/usr/bin/env bash
# 3·2·1→Theater AI node — install / update script (the GPU box).
#
# Installs: the service user (321ai-node), /opt/321ai-node/ai_node.py,
# /etc/321ai-node/node.env (NODE_SECRET generated), a self-signed TLS
# certificate, the systemd unit, and (with --with-ollama) Ollama bound to
# 127.0.0.1. This box gets NO database access and no 321Theater app code.
#
# Idempotent: re-running is the UPDATE path. It never overwrites node.env or
# an existing certificate (a new certificate would need a new
# AI_NODE_CERT_SHA256 on every app server — delete node.crt/node.key and
# re-run to rotate on purpose).
#
# Usage:  sudo bash install.sh [--with-ollama]     (from this ai_node/ dir)

set -euo pipefail

WITH_OLLAMA=0
for arg in "$@"; do
    case "$arg" in
        --with-ollama) WITH_OLLAMA=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/321ai-node"
ENV_DIR="/etc/321ai-node"
ENV_FILE="${ENV_DIR}/node.env"
CERT="${ENV_DIR}/node.crt"
KEY="${ENV_DIR}/node.key"
SERVICE_FILE="/etc/systemd/system/321ai-node.service"
RUN_USER="321ai-node"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

[ "$(id -u)" -eq 0 ] || error "Run as root: sudo bash install.sh"
[ -f "${SRC_DIR}/ai_node.py" ] || error "ai_node.py not found next to this script — run it from the ai_node/ directory."
command -v python3 >/dev/null || error "python3 is required."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || error "Python 3.8 or newer is required."
command -v openssl >/dev/null || { command -v apt-get >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl >/dev/null; } || error "openssl is required."

step "Service user and files..."
id "$RUN_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
install -d -m 755 "$APP_DIR"
install -m 644 "${SRC_DIR}/ai_node.py" "${APP_DIR}/ai_node.py"
install -d -m 750 -o root -g "$RUN_USER" "$ENV_DIR"

if [ ! -f "$ENV_FILE" ]; then
    SECRET="$(openssl rand -hex 32)"
    sed "s/^NODE_SECRET=.*/NODE_SECRET=${SECRET}/" "${SRC_DIR}/node.env.example" > "$ENV_FILE"
    info "Wrote ${ENV_FILE} with a new NODE_SECRET."
else
    info "Kept the existing ${ENV_FILE}."
fi
chown root:"$RUN_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"

if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    step "Self-signed TLS certificate (pinned by the app servers)..."
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
        -subj "/CN=$(hostname)-321ai-node" -keyout "$KEY" -out "$CERT" 2>/dev/null
    info "Made ${CERT} (10 years)."
fi
chown root:"$RUN_USER" "$CERT" "$KEY"; chmod 644 "$CERT"; chmod 640 "$KEY"

if [ "$WITH_OLLAMA" -eq 1 ]; then
    step "Ollama (bound to 127.0.0.1)..."
    if command -v ollama &>/dev/null || [ -x /usr/local/bin/ollama ] || [ -x /usr/bin/ollama ]; then
        info "Ollama already installed — not reinstalling (update it with Ollama's own installer)."
    else
        TMP="$(mktemp -d)"
        if curl -fsSL https://ollama.com/install.sh -o "${TMP}/ollama-install.sh"; then
            sh "${TMP}/ollama-install.sh" || warn "The Ollama installer reported an error (see above)."
        else
            warn "Could not download the Ollama installer from ollama.com."
        fi
        rm -rf "$TMP"
    fi
    if systemctl cat ollama.service &>/dev/null; then
        D=/etc/systemd/system/ollama.service.d; F="${D}/50-321theater-localhost.conf"
        mkdir -p "$D"
        if [ -e "$F" ] && ! grep -q '^# Written by 321Theater' "$F"; then
            info "Kept the existing ${F} (not written by 321Theater)."
        else
            printf '%s\n' '# Written by 321Theater ai_node/install.sh — Ollama has no password, so it' \
                '# listens on this machine only; the AI node is its only way in.' '[Service]' \
                'Environment="OLLAMA_HOST=127.0.0.1:11434"' > "$F"
        fi
        systemctl daemon-reload
        systemctl enable --now ollama >/dev/null 2>&1 || warn "Could not start ollama.service."
        systemctl restart ollama || true
    fi
fi
if command -v ss >/dev/null 2>&1; then
    OPEN="$( { ss -Hltnp 2>/dev/null | grep '"ollama"' || true; } | awk '{print $4}' | grep -v -E '^(127\.|\[::1\]:|\[::ffff:127\.)' || true)"
    [ -n "$OPEN" ] && warn "Ollama listens on ${OPEN} — reachable from the network with NO password. Make OLLAMA_HOST 127.0.0.1:11434."
fi

step "systemd service..."
install -m 644 "${SRC_DIR}/321ai-node.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable 321ai-node >/dev/null 2>&1
systemctl restart 321ai-node
sleep 1
systemctl is-active --quiet 321ai-node && info "321ai-node is running." || warn "321ai-node did not start — journalctl -u 321ai-node"

LISTEN="$(grep -E '^NODE_LISTEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"')"
PORT="${LISTEN##*:}"; PORT="${PORT:-11500}"
FP="$(openssl x509 -in "$CERT" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')"
NODE_SECRET_VAL="$(grep -E '^NODE_SECRET=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"')"
grep -qE '^NODE_ALLOW_IPS=.+' "$ENV_FILE" || warn "NODE_ALLOW_IPS is empty — list the app servers' IPs in ${ENV_FILE}."
grep -qE '^NODE_MODELS=.+' "$ENV_FILE" || warn "NODE_MODELS is empty — 'Pull model' from Settings → AI is refused until it lists the model(s)."

echo ""
echo -e "${CYAN}Add these three lines to the .env of EVERY app server that should use this node${NC}"
echo -e "${CYAN}(replace the address with this box's address the app servers reach), then restart 321theater:${NC}"
echo ""
echo "AI_NODE_URL=https://<this-box-ip>:${PORT}"
echo "AI_NODE_SECRET=${NODE_SECRET_VAL}"
echo "AI_NODE_CERT_SHA256=${FP}"
echo ""
warn "The secret above is a password: paste it only into the app servers' .env files."
