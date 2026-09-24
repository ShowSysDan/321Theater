#!/usr/bin/env bash
# 321Theater service launcher.
# Reads the configured port from the database so that changing the port
# in Settings takes effect on the next service restart — no need to
# edit the systemd unit or re-run install.sh.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${APP_DIR}/venv"
# Read app_port from app_settings in PostgreSQL (db_config.ini), fall back to 5400
PORT=$(cd "${APP_DIR}" && "${VENV}/bin/python" -c "
import db_adapter
try:
    db = db_adapter.connect()
    r = db.execute(\"SELECT value FROM app_settings WHERE key='app_port'\").fetchone()
    db.close()
    print(r['value'] if r and r['value'] else '5400')
except Exception:
    print('5400')
" 2>/dev/null || echo "5400")

echo "[321theater] Starting on port ${PORT}"

exec "${VENV}/bin/gunicorn" \
    --workers 4 \
    --threads 4 \
    --bind "0.0.0.0:${PORT}" \
    --timeout 120 \
    --access-logfile - \
    --chdir "${APP_DIR}" \
    app:app
