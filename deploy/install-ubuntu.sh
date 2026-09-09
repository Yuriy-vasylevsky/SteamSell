#!/usr/bin/env bash
# Run from the repository root on a fresh Ubuntu 24.04 VPS:
# sudo bash deploy/install-ubuntu.sh
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with: sudo bash deploy/install-ubuntu.sh"
  exit 1
fi

if [[ ! -f compose.yaml || ! -f .env.example ]]; then
  echo "Run this script from the SteamSell project directory."
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl git docker.io docker-compose-v2 openssl python3
systemctl enable --now docker

if [[ ! -f .env ]]; then
  install -m 600 .env.example .env
  db_password="$(openssl rand -hex 24)"
  fernet_key="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  sed -i \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${db_password}|" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://steamsell:${db_password}@db:5432/steamsell|" \
    -e "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${fernet_key}|" .env
  echo ".env was created with a unique database password and encryption key."
fi

echo
echo "Edit .env now and set at least: BOT_TOKEN, ADMIN_ID, MONO_TOKEN, SUPPORT_USERNAME."
echo "For card payment also set MANUAL_CARD. It can run without a domain."
echo "For Gmail OAuth set PUBLIC_BASE_URL and Google values."
echo "Then start the bot with: docker compose up -d --build"
echo "Check it with: docker compose ps && docker compose logs --tail=100 app"
