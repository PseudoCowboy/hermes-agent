#!/usr/bin/env bash
# Quick health snapshot for the hermes-agent Azure VM deploy.
#
# Usage (from mac):
#   ssh -i ~/.ssh/duck_key.pem azureuser@172.169.248.86 'bash -s' < scripts/vm-status.sh
# Or on the VM directly:
#   bash ~/hermes-agent/scripts/vm-status.sh

set -u

hr() { printf -- '--- %s ---\n' "$1"; }

hr "systemd services"
systemctl is-active copilot-api.service hermes-gateway.service || true
systemctl status copilot-api.service --no-pager --lines=0 | head -5 || true
systemctl status hermes-gateway.service --no-pager --lines=0 | head -5 || true

hr "disk"
df -h / | awk 'NR==1 || /\//'

hr "memory"
free -h | awk 'NR==1 || /Mem:/'

hr "load"
uptime

hr "copilot-api endpoints"
# Single-port deploy: Anthropic-compat on :4141 only. (OpenAI-compat :4142 not
# started in this setup — copilot-api's Anthropic endpoint serves both CLIs.)
for port in 4141; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${port}/v1/models")
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'port %s  → err (curl rc=%d)\n' "$port" "$rc"
  else
    printf 'port %s  → %s\n' "$port" "$code"
  fi
done

hr "last 20 gateway log lines"
sudo journalctl -u hermes-gateway.service -n 20 --no-pager 2>/dev/null || \
  journalctl -u hermes-gateway.service -n 20 --no-pager 2>/dev/null || \
  echo '(journalctl not accessible without sudo)'

hr "last 20 copilot-api log lines"
sudo journalctl -u copilot-api.service -n 20 --no-pager 2>/dev/null || \
  journalctl -u copilot-api.service -n 20 --no-pager 2>/dev/null || \
  echo '(journalctl not accessible without sudo)'
