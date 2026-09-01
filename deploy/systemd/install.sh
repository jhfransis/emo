#!/usr/bin/env bash
# KONA systemd 서비스 등록 (sudo 필요)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_DIR="/etc/systemd/system"

echo "Installing units from ${ROOT}/deploy/systemd/ ..."
sudo cp "${ROOT}/deploy/systemd/kona-streamlit.service" "${UNIT_DIR}/"
sudo cp "${ROOT}/deploy/systemd/kona-trailing-worker.service" "${UNIT_DIR}/"
sudo systemctl daemon-reload
sudo systemctl enable kona-streamlit.service kona-trailing-worker.service

echo ""
echo "기존 수동 프로세스가 있으면 종료합니다 ..."
pkill -f "streamlit run app/kona_futures.py" 2>/dev/null || true
pkill -f "futures/trailing_run.py" 2>/dev/null || true
rm -f "${ROOT}/db/trailing_worker.pid"

sudo systemctl restart kona-streamlit.service kona-trailing-worker.service

if command -v tailscale >/dev/null 2>&1; then
  echo ""
  echo "Tailscale Serve 포트 맞춤 ..."
  "${ROOT}/deploy/systemd/tailscale-serve.sh" || true
fi

echo ""
sudo systemctl status kona-streamlit.service --no-pager -l || true
echo ""
sudo systemctl status kona-trailing-worker.service --no-pager -l || true
echo ""
echo "완료. 로그: journalctl -u kona-streamlit -f"
echo "       journalctl -u kona-trailing-worker -f"
