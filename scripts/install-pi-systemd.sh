#!/usr/bin/env bash
# Install systemd units for the Udhaari bot on a Raspberry Pi.
#
# Idempotent: safe to re-run after a git pull or .env edit. Auto-detects the
# invoking user's home + uv path so the unit files don't need manual editing
# per-machine. Reads the ngrok hostname from .env so the same script works
# whether you're on dev or prod.
#
# Run with sudo:  sudo bash scripts/install-pi-systemd.sh
#
# What it does:
#   1. Writes /etc/systemd/system/udhaari-bot.service (uvicorn)
#   2. Writes /etc/systemd/system/udhaari-tunnel.service (ngrok)
#   3. systemctl daemon-reload + verify
#   4. Enables both for auto-start on boot
#   5. Starts both immediately
#   6. Prints status

set -euo pipefail

# When invoked via sudo, $SUDO_USER is the real user. Fall back to $USER
# for non-sudo invocations (which will fail later anyway on the systemctl
# calls — but we report a clearer error).
RUN_USER="${SUDO_USER:-$USER}"
if [ "$RUN_USER" = "root" ]; then
    echo "ERROR: don't run this script directly as root. Use: sudo bash $0" >&2
    echo "       (we need \$SUDO_USER to know whose home to point at)" >&2
    exit 2
fi

RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
PROJECT_DIR="$RUN_HOME/auto-split"
UV_PATH="$RUN_HOME/.local/bin/uv"
NGROK_PATH="/usr/local/bin/ngrok"
ENV_FILE="$PROJECT_DIR/.env"

# Sanity checks
[ -d "$PROJECT_DIR" ] || { echo "ERROR: project dir not found at $PROJECT_DIR" >&2; exit 1; }
[ -x "$UV_PATH" ] || { echo "ERROR: uv not found / not executable at $UV_PATH" >&2; exit 1; }
[ -x "$NGROK_PATH" ] || { echo "ERROR: ngrok not found / not executable at $NGROK_PATH" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: .env not found at $ENV_FILE — copy from your dev machine first" >&2; exit 1; }

# Pull the ngrok domain out of PUBLIC_BASE_URL in .env so we don't hardcode
# the URL in the unit file. Strips scheme, quotes, trailing slash.
PUBLIC_URL=$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | head -1 | sed 's|^PUBLIC_BASE_URL=||')
PUBLIC_URL="${PUBLIC_URL%\"}"
PUBLIC_URL="${PUBLIC_URL#\"}"
PUBLIC_URL="${PUBLIC_URL%\'}"
PUBLIC_URL="${PUBLIC_URL#\'}"
NGROK_DOMAIN="${PUBLIC_URL#http://}"
NGROK_DOMAIN="${NGROK_DOMAIN#https://}"
NGROK_DOMAIN="${NGROK_DOMAIN%/}"

if [ -z "$NGROK_DOMAIN" ]; then
    echo "ERROR: could not parse PUBLIC_BASE_URL from $ENV_FILE" >&2
    echo "       expected something like: PUBLIC_BASE_URL=https://my.ngrok-free.dev" >&2
    exit 1
fi

cat <<INFO
Installing systemd units:
  user:        $RUN_USER
  project:     $PROJECT_DIR
  uv:          $UV_PATH
  ngrok:       $NGROK_PATH
  domain:      $NGROK_DOMAIN

INFO

# --- Bot service (uvicorn) ---------------------------------------------------
tee /etc/systemd/system/udhaari-bot.service > /dev/null <<EOF
[Unit]
Description=Udhaari Telegram bot (FastAPI / uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$UV_PATH run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10

# Pi Zero 2 W memory constraints: throttle at 350M, hard kill at 450M.
# If MemoryMax is hit repeatedly, raise these or upgrade to a Pi 4.
MemoryHigh=350M
MemoryMax=450M

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# --- Tunnel service (ngrok) --------------------------------------------------
tee /etc/systemd/system/udhaari-tunnel.service > /dev/null <<EOF
[Unit]
Description=ngrok tunnel for Udhaari bot
After=network-online.target udhaari-bot.service
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
ExecStart=$NGROK_PATH http --domain=$NGROK_DOMAIN 8000 --log=stdout --log-format=logfmt
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Reload + verify
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/udhaari-bot.service /etc/systemd/system/udhaari-tunnel.service

# Stop any tmux-managed processes that might be holding port 8000 / the
# tunnel. Best-effort; ignore errors if there's no tmux session.
if sudo -u "$RUN_USER" tmux has-session -t bot 2>/dev/null; then
    echo "Stopping existing tmux session 'bot' so it doesn't conflict..."
    sudo -u "$RUN_USER" tmux kill-session -t bot || true
fi

# Enable + start
systemctl enable --now udhaari-bot.service udhaari-tunnel.service

echo ""
echo "Status:"
echo "------"
systemctl status udhaari-bot.service udhaari-tunnel.service --no-pager --lines=5 || true

echo ""
echo "Tail live logs with:"
echo "  sudo journalctl -u udhaari-bot -u udhaari-tunnel -f"
