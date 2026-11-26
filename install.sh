#!/usr/bin/env bash
# 321Theater — install / update script
# Supports fresh installs and in-place upgrades.
# Run as root for full systemd service setup; run as a regular user for manual-start mode.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="321theater"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${APP_DIR}/.env"

# Read app_port from app_settings in PostgreSQL (falls back to 5400 if the DB
# isn't reachable yet or the setting is absent)
_read_port() {
    (cd "${APP_DIR}" && "${VENV}/bin/python" -c "
import db_adapter
try:
    db = db_adapter.connect()
    r = db.execute(\"SELECT value FROM app_settings WHERE key='app_port'\").fetchone()
    db.close()
    print(r['value'] if r and r['value'] else '5400')
except Exception:
    print('5400')
" 2>/dev/null) || echo "5400"
}
PORT=5400  # will be updated after DB is ready

# Options: --with-ollama  also install Ollama (local AI) on THIS server,
#                         bound to 127.0.0.1 only (3.55.0). Idempotent.
WITH_OLLAMA=0
for _arg in "$@"; do
    case "$_arg" in
        --with-ollama) WITH_OLLAMA=1 ;;
    esac
done

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║    3·2·1→THEATER — Production Manager    ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Python check ──────────────────────────────────────────────────────────────
step "Checking Python 3..."
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install Python 3.9+ and re-run this script."
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    error "Python 3.9+ is required (found $PY_VER)."
fi
info "Found Python $PY_VER at $(command -v python3)"

# ── Virtual environment ───────────────────────────────────────────────────────
step "Setting up virtual environment..."
VENV="${APP_DIR}/venv"
if [ ! -d "$VENV" ]; then
    info "Creating virtualenv at ${VENV}..."
    python3 -m venv "$VENV"
else
    info "Virtualenv already exists at ${VENV}"
fi

PIP="${VENV}/bin/pip"
PYTHON="${VENV}/bin/python"

info "Upgrading pip..."
"$PIP" install --upgrade pip --quiet

step "Installing Python dependencies..."
"$PIP" install -r "${APP_DIR}/requirements.txt" --quiet
info "Dependencies installed."

# Truly installed — `dpkg -s` also succeeds for a package that was REMOVED but
# left its config files behind (status "deinstall ok config-files").
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q ' ok installed$'; }

# ── LibreOffice (recommended; enables Office/OpenDocument → PDF append) ───────
# Used by the advance PDF generator to embed .doc/.docx/.odt/.rtf/.ppt/.pptx/
# .odp/.xls/.xlsx/.ods uploads. Without these packages, those files are
# listed on the trailing "omitted files" index page instead of embedded.
if pkg_installed libreoffice-writer; then
    info "LibreOffice writer already installed — skipping."
elif [ "$(id -u)" -eq 0 ] && command -v apt-get &>/dev/null; then
    step "Installing LibreOffice (headless) for Office-document → PDF conversion..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
        2>&1 | tail -5 || warn "LibreOffice install skipped (apt-get failed)."
    if command -v soffice &>/dev/null; then
        info "LibreOffice installed: $(soffice --version 2>/dev/null | head -1)"
    fi
else
    warn "LibreOffice writer/calc/impress not detected. To embed Office/OpenDocument uploads in the advance PDF,"
    warn "  install: sudo apt-get install -y libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress"
fi

# ── Bring LibreOffice up to date (it converts files users upload) ─────────────
# Debian's automatic security updates (below) keep it patched from now on;
# this catches up an install that has been sitting on an old version.
if [ "$(id -u)" -eq 0 ] && command -v apt-get &>/dev/null && pkg_installed libreoffice-core; then
    step "Checking for LibreOffice updates..."
    if apt-get update -qq >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --only-upgrade \
            libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-common \
            >/dev/null 2>&1 || warn "LibreOffice upgrade skipped (apt-get failed)."
        info "LibreOffice: $(dpkg-query -W -f='${Version}' libreoffice-core 2>/dev/null)"
    else
        warn "apt-get update failed — LibreOffice not checked for updates."
    fi
fi

# ── Automatic security updates (Debian unattended-upgrades) ───────────────────
# Installs Debian's security updates for the WHOLE server every day (LibreOffice,
# Python, OpenSSL, PostgreSQL client…), as root, outside the app. Debian's
# default unattended-upgrades policy takes only the security archive. An
# existing /etc/apt/apt.conf.d/20auto-upgrades is left exactly as it is — an
# admin who configured updates their own way keeps it. Settings → About
# 321Theater shows whether this is on and when it last ran.
if [ "$(id -u)" -eq 0 ] && command -v apt-get &>/dev/null; then
    step "Automatic security updates..."
    # Remember whether an admin's own config exists BEFORE the package is
    # installed: the package's own setup may write that file itself (on
    # Debian possibly with updates switched OFF) — that one is replaced.
    AUTO_CONF_PREEXISTING=0
    [ -e /etc/apt/apt.conf.d/20auto-upgrades ] && AUTO_CONF_PREEXISTING=1
    if ! pkg_installed unattended-upgrades; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades >/dev/null 2>&1 \
            && info "unattended-upgrades installed." \
            || warn "Could not install unattended-upgrades (apt-get failed)."
    fi
    if pkg_installed unattended-upgrades; then
        if [ "$AUTO_CONF_PREEXISTING" -eq 0 ]; then
            cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APTCONF'
// Written by 321Theater install.sh — Debian's daily security updates.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APTCONF
            info "Automatic security updates switched on (/etc/apt/apt.conf.d/20auto-upgrades)."
        else
            info "Kept the existing /etc/apt/apt.conf.d/20auto-upgrades."
        fi
        if command -v systemctl &>/dev/null; then
            systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
        fi
    fi
else
    warn "Not root — automatic security updates not checked. Run: sudo bash install.sh"
fi

# ── Ollama — local AI (only with --with-ollama) ───────────────────────────────
# Ollama has NO authentication, so it must only ever listen on 127.0.0.1:
# 321Theater talks to the Ollama on its own machine and hands AI work to a
# server that has one through its job queue — never across the network. The
# official installer creates ollama.service; we add ONE drop-in of our own
# that pins OLLAMA_HOST to 127.0.0.1 (an admin's own drop-ins / override.conf
# are never edited), then check the EFFECTIVE value and warn if anything else
# opens it up. Re-running is safe. Settings → AI shows each server's status.
if [ "$WITH_OLLAMA" -eq 1 ]; then
    step "Ollama (local AI, bound to 127.0.0.1)..."
    if [ "$(id -u)" -ne 0 ]; then
        warn "--with-ollama needs root. Run: sudo bash install.sh --with-ollama"
    elif ! command -v systemctl &>/dev/null; then
        warn "systemd not found — install Ollama by hand and run it with OLLAMA_HOST=127.0.0.1:11434."
    else
        if command -v ollama &>/dev/null || [ -x /usr/local/bin/ollama ] || [ -x /usr/bin/ollama ]; then
            info "Ollama already installed — not reinstalling (update it with Ollama's own installer)."
        else
            if ! command -v curl &>/dev/null && command -v apt-get &>/dev/null; then
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null 2>&1 || true
            fi
            OLLAMA_TMP="$(mktemp -d)"
            if curl -fsSL https://ollama.com/install.sh -o "${OLLAMA_TMP}/ollama-install.sh"; then
                info "Running the official Ollama installer..."
                sh "${OLLAMA_TMP}/ollama-install.sh" || warn "The Ollama installer reported an error (see above)."
            else
                warn "Could not download the Ollama installer from ollama.com (network / proxy?)."
            fi
            rm -rf "$OLLAMA_TMP"
        fi
        if systemctl cat ollama.service &>/dev/null; then
            OLLAMA_DROPIN_DIR=/etc/systemd/system/ollama.service.d
            OLLAMA_DROPIN="${OLLAMA_DROPIN_DIR}/50-321theater-localhost.conf"
            OLLAMA_WANT=$'# Written by 321Theater install.sh --with-ollama — Ollama has no password,\n# so it listens on this machine only. Delete this file to manage it yourself.\n[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"'
            mkdir -p "$OLLAMA_DROPIN_DIR"
            OLLAMA_CHANGED=0
            if [ -e "$OLLAMA_DROPIN" ] && ! grep -q '^# Written by 321Theater' "$OLLAMA_DROPIN"; then
                info "Kept the existing ${OLLAMA_DROPIN} (not written by 321Theater)."
            elif [ ! -e "$OLLAMA_DROPIN" ] || [ "$(cat "$OLLAMA_DROPIN")" != "$(printf '%b' "$OLLAMA_WANT")" ]; then
                printf '%b\n' "$OLLAMA_WANT" > "$OLLAMA_DROPIN"
                OLLAMA_CHANGED=1
                info "Ollama pinned to 127.0.0.1:11434 (${OLLAMA_DROPIN})."
            fi
            systemctl daemon-reload
            systemctl enable --now ollama >/dev/null 2>&1 || warn "Could not start ollama.service (journalctl -u ollama)."
            [ "$OLLAMA_CHANGED" -eq 1 ] && systemctl restart ollama || true
            OLLAMA_EFF="$(systemctl show ollama -p Environment --value 2>/dev/null | tr ' ' '\n' | grep '^OLLAMA_HOST=' | tail -1 | cut -d= -f2-)"
            case "${OLLAMA_EFF:-127.0.0.1:11434}" in
                127.*|localhost*|"[::1]"*|::1*)
                    info "Ollama listens on ${OLLAMA_EFF:-127.0.0.1:11434} (this machine only)." ;;
                *)
                    warn "Ollama's effective OLLAMA_HOST is '${OLLAMA_EFF}' — another drop-in opens it to the network."
                    warn "  Ollama has NO password. Remove that setting (systemctl cat ollama) so it listens on 127.0.0.1." ;;
            esac
            info "Next: pull the model from Settings → AI (Pull model), or: ollama pull <model>"
        else
            warn "ollama.service was not found after install — check the installer output above."
        fi
    fi
fi

# ── Backup directories ────────────────────────────────────────────────────────
step "Creating backup directories..."
mkdir -p "${APP_DIR}/backups/hourly"
mkdir -p "${APP_DIR}/backups/daily"
# Owner-only: dumps hold password hashes, live session ids and stored secrets.
chmod -R u=rwX,go= "${APP_DIR}/backups"
info "Backup dirs ready: ${APP_DIR}/backups/{hourly,daily}"

# ── Database ──────────────────────────────────────────────────────────────────
step "Initializing PostgreSQL database..."
# Connection settings: PG_* in .env (3.4.0+), each missing key falling back to
# the legacy db_config.ini — so an older install with only db_config.ini
# still passes this check.
if ! (cd "${APP_DIR}" && "$PYTHON" -c "import sys, db_adapter; sys.exit(0 if db_adapter.is_configured() else 1)"); then
    error "PostgreSQL is not configured. Copy .env.example to ${ENV_FILE}, fill in PG_HOST / PG_DBNAME / PG_USER / PG_PASSWORD, and re-run."
fi
if [ -f "${APP_DIR}/db_config.ini" ]; then
    warn "db_config.ini is deprecated — move its values into .env (python3 app_config.py --export prints the lines). It keeps working until then."
fi
# Idempotent: creates schemas/tables if missing, applies column migrations,
# and seeds defaults only into empty tables (fresh installs).
"$PYTHON" "${APP_DIR}/init_db.py" || error "Database initialization failed (see output above)."
info "Database ready."

# Read the configured port from the database now that it exists
PORT=$(_read_port)
info "App port: ${PORT}"

# ── Systemd service setup (root only) ─────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    step "Configuring systemd service (running as root)..."

    # Determine service user (prefer the SUDO_USER who invoked sudo)
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        RUN_USER="$SUDO_USER"
    else
        RUN_USER="$(logname 2>/dev/null || echo root)"
    fi
    info "Service will run as user: ${RUN_USER}"

    # Ensure all app files are owned by the service user
    chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}/backups" 2>/dev/null || true
    if [ -f "${APP_DIR}/db_config.ini" ]; then
        chown "${RUN_USER}:${RUN_USER}" "${APP_DIR}/db_config.ini" 2>/dev/null || true
        chmod 600 "${APP_DIR}/db_config.ini" 2>/dev/null || true
    fi
    chown "${RUN_USER}:${RUN_USER}" "${APP_DIR}" 2>/dev/null || true

    # Generate SECRET_KEY if not already present (a blank "SECRET_KEY=" line,
    # as in .env.example, is filled in place)
    if [ ! -f "$ENV_FILE" ] || ! grep -q '^SECRET_KEY=.' "$ENV_FILE" 2>/dev/null; then
        SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
        if [ -f "$ENV_FILE" ] && grep -q '^SECRET_KEY=[[:space:]]*$' "$ENV_FILE"; then
            sed -i "s/^SECRET_KEY=[[:space:]]*\$/SECRET_KEY=${SECRET_KEY}/" "$ENV_FILE"
        else
            echo "SECRET_KEY=${SECRET_KEY}" >> "$ENV_FILE"
        fi
        info "Generated new SECRET_KEY → ${ENV_FILE}"
    else
        info "SECRET_KEY already present in ${ENV_FILE}"
    fi
    # .env holds the DB password (3.4.0+): owner-only, always.
    chmod 600 "$ENV_FILE"

    # Stub the VPS-gateway settings so admins can find them later. All three
    # stay commented out by default — the gateway endpoints and trusted-proxy
    # handling are fully disabled until these are set (see GATEWAY_DESIGN.md
    # and gateway/README.md).
    if ! grep -q 'GATEWAY_SHARED_SECRET' "$ENV_FILE" 2>/dev/null; then
        cat >> "$ENV_FILE" << 'GWEOF'
# ── VPS gateway pre-auth (optional — see gateway/README.md) ──────────────
# Shared secret for the /internal/gateway/otp/* endpoints (same value as
# GATE_SHARED_SECRET on the VPS). Unset = endpoints answer 404.
#GATEWAY_SHARED_SECRET=
# Restrict those endpoints to these raw socket peer IPs (comma-separated).
#GATEWAY_PEER_IPS=10.201.4.9
# Honor X-Forwarded-For/-Proto only from these proxy IPs (comma-separated).
#TRUSTED_PROXY_IPS=10.201.4.9
GWEOF
    fi

    # Stub the test-server flag so admins can find it later (off by default).
    if ! grep -q 'TEST_MODE' "$ENV_FILE" 2>/dev/null; then
        cat >> "$ENV_FILE" << 'TMEOF'
# ── Test server (optional — see .env.example) ────────────────────────────
# 1 = TEST instance: never clusters/leads, leader-only jobs off, email only
# to admins with a [TEST INSTANCE] subject, syslog lines tagged the same.
#TEST_MODE=1
TMEOF
    fi

    # Ensure .env is readable by service user
    chown "${RUN_USER}" "$ENV_FILE" 2>/dev/null || true

    # Make start.sh executable
    chmod +x "${APP_DIR}/start.sh"

    # Write the systemd unit — ExecStart uses start.sh so the port is
    # read from the database on every service restart (no need to edit
    # this file when the port changes via the Settings UI).
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=321Theater — Production Management
Documentation=https://github.com/ShowSysDan/ShowAdvance
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/start.sh
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}
# Files the app creates are not world-readable; /tmp is private to the service.
UMask=0027
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

    info "Systemd unit written to ${SERVICE_FILE}"

    # Allow the service user to restart 321theater without a password.
    # This enables the "Change Port" UI button to trigger a live restart.
    SUDOERS_FILE="/etc/sudoers.d/${APP_NAME}"
    echo "${RUN_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${APP_NAME}, /usr/bin/systemctl restart ${APP_NAME}" \
        > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    info "Sudoers entry written to ${SUDOERS_FILE} (allows service restart from UI)"

    systemctl daemon-reload
    systemctl enable "$APP_NAME" --quiet
    systemctl restart "$APP_NAME"

    # Brief wait to confirm startup
    sleep 2
    if systemctl is-active --quiet "$APP_NAME"; then
        info "Service is running."
    else
        warn "Service may have failed to start. Check: journalctl -u ${APP_NAME} -n 50"
    fi

    # Determine LAN IP for the success message
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║          Installation Complete!          ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Open 321Theater at: ${CYAN}http://${LAN_IP}:${PORT}${NC}"
    echo ""
    echo -e "  ${YELLOW}Default login:${NC}  admin / admin   ← change this immediately!"
    echo ""
    echo "  Useful commands:"
    echo "    systemctl status ${APP_NAME}"
    echo "    journalctl -u ${APP_NAME} -f"
    echo "    systemctl restart ${APP_NAME}"
    echo ""

else
    # ── Non-root: manual-start instructions ──────────────────────────────────
    step "Non-root install complete — systemd service not configured."
    echo ""
    echo "  To start 321Theater manually:"
    echo ""
    echo "    ${VENV}/bin/python ${APP_DIR}/app.py"
    echo ""
    echo "  Or, to run with gunicorn (production-style):"
    echo ""
    echo "    ${VENV}/bin/gunicorn --workers 2 --bind 0.0.0.0:${PORT} --chdir ${APP_DIR} app:app"
    echo ""
    echo "  Then open: http://localhost:${PORT}"
    echo ""
    echo -e "  ${YELLOW}Default login:${NC}  admin / admin   ← change this immediately!"
    echo ""
    warn "To install the systemd service, re-run as root:  sudo ./install.sh"
    echo ""
fi
