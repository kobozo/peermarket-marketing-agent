#!/usr/bin/env bash
# Idempotent bootstrap for VM 129 (agent-peermarket).
# Run as root on the VM. Safe to re-run.
set -euo pipefail

APT_PKGS=(python3.12 python3.12-venv postgresql-16 postgresql-16-pgvector
          ufw age cron git curl jq)
AGENT_USER=peermarket-agent
AGENT_HOME=/opt/peermarket-agent
AGENT_DATA=/var/peermarket-agent
AGENT_DB=peermarket_agent

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PKGS[@]}"

# uv installer (fast Python deps manager)
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    install /root/.local/bin/uv /usr/local/bin/uv
fi

# Service user
id -u "$AGENT_USER" &>/dev/null || useradd --system --create-home \
    --home-dir "$AGENT_HOME" --shell /usr/sbin/nologin "$AGENT_USER"

# Data directories
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0750 \
    "$AGENT_DATA" "$AGENT_DATA/creatives" "$AGENT_DATA/recordings" \
    "$AGENT_DATA/drafts" "$AGENT_DATA/archive" "$AGENT_DATA/backups"

# Secrets dir
install -d -o root -g root -m 0700 /etc/peermarket-agent
touch /etc/peermarket-agent/secrets.env
chown root:"$AGENT_USER" /etc/peermarket-agent/secrets.env
chmod 0640 /etc/peermarket-agent/secrets.env

# Postgres: enable, create DB + role
systemctl enable --now postgresql
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$AGENT_DB'" | grep -q 1 \
    || sudo -u postgres createdb "$AGENT_DB"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$AGENT_USER'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE ROLE $AGENT_USER LOGIN"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $AGENT_DB TO $AGENT_USER"
sudo -u postgres psql -d "$AGENT_DB" -c "CREATE EXTENSION IF NOT EXISTS vector"

# ufw firewall (allow ssh only; nothing else listens publicly — tunnel handles that)
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

# systemd units
install -m 0644 "$(dirname "$0")/systemd/marketing-agent.service" \
    /etc/systemd/system/marketing-agent.service
install -m 0644 "$(dirname "$0")/systemd/slack-bridge.service" \
    /etc/systemd/system/slack-bridge.service
systemctl daemon-reload
systemctl enable marketing-agent.service slack-bridge.service

echo "Bootstrap complete. Services enabled but NOT started."
echo "Next: populate /etc/peermarket-agent/secrets.env via the deploy workflow."
