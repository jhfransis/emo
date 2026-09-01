#!/usr/bin/env bash
# Tailscale Serve → Streamlit (config.toml port)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="$(grep -E '^port\s*=' "${ROOT}/.streamlit/config.toml" | head -1 | sed 's/.*=\s*//;s/[^0-9]//g')"
PORT="${PORT:-18501}"

echo "Tailscale Serve → http://127.0.0.1:${PORT}"
sudo tailscale serve reset
sudo tailscale serve --bg "http://127.0.0.1:${PORT}"
tailscale serve status
