#!/usr/bin/env bash
# 모의/실전 계좌 전환, 또는 Streamlit·트레일링 워커만 재시작한다 (sudo 필요).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${ROOT}/cfg/config.yml"
UNIT_SRC="${ROOT}/deploy/systemd"
UNIT_DIR="/etc/systemd/system"
SERVICES=(kona-streamlit.service kona-trailing-worker.service)

usage() {
  cat <<EOF
사용법: $(basename "$0") <mock|real|reset|status> [--yes]

  mock    모의투자 (mock_futures) 로 전환 후 데몬 재시작
  real    실전 (real_futures) 로 전환 후 데몬 재시작
  reset   계좌는 그대로 두고 Streamlit·워커만 재시작
  status  현재 프로필과 데몬 상태만 표시

  mock/real/reset 은 수동 Streamlit·워커도 종료한 뒤 systemd로 다시 올립니다.

  --yes   확인 질문 생략 (mock/real)

예: ${ROOT}/deploy/switch-account.sh mock
    ${ROOT}/deploy/switch-account.sh real
    ${ROOT}/deploy/switch-account.sh reset
EOF
}

require_cfg() {
  if [[ ! -f "${CFG}" ]]; then
    echo "없음: ${CFG}" >&2
    exit 1
  fi
}

python_cfg() {
  /home/fransis/miniforge3/bin/python - "$@" <<'PY'
import sys
from pathlib import Path

import yaml

cfg_path = Path(sys.argv[1])
cmd = sys.argv[2]
config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
if not isinstance(config, dict):
    raise SystemExit("cfg/config.yml 형식이 올바르지 않습니다.")

if cmd == "get":
    print(str(config.get("active_profile") or "").strip())
    raise SystemExit(0)

if cmd == "acct":
    profile = sys.argv[3]
    section = config.get(profile)
    if not isinstance(section, dict):
        raise SystemExit(f"cfg/config.yml에 {profile} 섹션이 없습니다.")
    print(str(section.get("acctno") or "").strip())
    raise SystemExit(0)

if cmd == "set":
    profile = sys.argv[3]
    if profile not in config or not isinstance(config[profile], dict):
        raise SystemExit(f"cfg/config.yml에 {profile} 섹션이 없습니다.")
    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    replaced = False
    out = []
    for line in lines:
        if line.lstrip().startswith("active_profile:"):
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"active_profile: {profile}{nl}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        raise SystemExit("active_profile 줄을 찾지 못했습니다.")
    cfg_path.write_text("".join(out), encoding="utf-8")
    print(profile)
    raise SystemExit(0)

raise SystemExit(f"unknown cmd: {cmd}")
PY
}

label_of() {
  case "$1" in
    mock_futures) echo "모의" ;;
    real_futures) echo "실전" ;;
    *) echo "$1" ;;
  esac
}

resolve_profile() {
  case "$1" in
    mock|mock_futures|모의) echo mock_futures ;;
    real|real_futures|실전) echo real_futures ;;
    *)
      echo "알 수 없는 대상: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
}

show_leftovers() {
  local pids
  pids="$(pgrep -af 'streamlit run app/kona_futures.py|futures/trailing_run.py' || true)"
  if [[ -n "${pids}" ]]; then
    echo "수동 프로세스:"
    echo "${pids}" | sed 's/^/  /'
  fi
}

show_status() {
  local current acct
  current="$(python_cfg "${CFG}" get)"
  acct="$(python_cfg "${CFG}" acct "${current}")"
  echo "active_profile: ${current} ($(label_of "${current}")) 계좌 ${acct}"
  local st_ui st_wk
  st_ui="$(systemctl is-active kona-streamlit.service 2>/dev/null || true)"
  st_wk="$(systemctl is-active kona-trailing-worker.service 2>/dev/null || true)"
  echo "streamlit:  ${st_ui:-unknown}"
  echo "worker:     ${st_wk:-unknown}"
  show_leftovers
}

stop_leftovers() {
  echo "기존 프로세스 종료 (systemd + 수동) ..."
  sudo systemctl stop "${SERVICES[@]}" 2>/dev/null || true
  pkill -f "streamlit run app/kona_futures.py" 2>/dev/null || true
  pkill -f "futures/trailing_run.py" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if ! pgrep -f "streamlit run app/kona_futures.py" >/dev/null \
      && ! pgrep -f "futures/trailing_run.py" >/dev/null; then
      break
    fi
    sleep 0.3
  done
  if pgrep -f "streamlit run app/kona_futures.py" >/dev/null \
    || pgrep -f "futures/trailing_run.py" >/dev/null; then
    echo "프로세스가 남아 강제 종료합니다." >&2
    pkill -9 -f "streamlit run app/kona_futures.py" 2>/dev/null || true
    pkill -9 -f "futures/trailing_run.py" 2>/dev/null || true
    sleep 0.3
  fi
  rm -f "${ROOT}/db/trailing_worker.pid"
}

wait_active() {
  local unit="$1"
  local i state
  for i in $(seq 1 15); do
    state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
    if [[ "${state}" == "active" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "${unit} 이 active가 아닙니다: $(systemctl is-active "${unit}" 2>/dev/null || true)" >&2
  journalctl -u "${unit}" -n 15 --no-pager >&2 || true
  return 1
}

restart_daemons() {
  stop_leftovers
  echo "systemd 유닛 설치 후 재시작 ..."
  sudo cp "${UNIT_SRC}/kona-streamlit.service" "${UNIT_DIR}/"
  sudo cp "${UNIT_SRC}/kona-trailing-worker.service" "${UNIT_DIR}/"
  sudo systemctl daemon-reload
  sudo systemctl reset-failed "${SERVICES[@]}" 2>/dev/null || true
  sudo systemctl start "${SERVICES[@]}"
  wait_active kona-streamlit.service
  wait_active kona-trailing-worker.service
  echo "streamlit:  $(systemctl is-active kona-streamlit.service)"
  echo "worker:     $(systemctl is-active kona-trailing-worker.service)"
}

YES=0
TARGET=""
for arg in "$@"; do
  case "${arg}" in
    -h|--help)
      usage
      exit 0
      ;;
    --yes|-y)
      YES=1
      ;;
    status)
      TARGET="status"
      ;;
    reset)
      TARGET="reset"
      ;;
    mock|mock_futures|모의|real|real_futures|실전)
      TARGET="${arg}"
      ;;
    *)
      echo "알 수 없는 인자: ${arg}" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${TARGET}" ]]; then
  usage >&2
  exit 1
fi

require_cfg

if [[ "${TARGET}" == "status" ]]; then
  show_status
  exit 0
fi

if [[ "${TARGET}" == "reset" ]]; then
  show_status
  restart_daemons
  echo "완료."
  exit 0
fi

PROFILE="$(resolve_profile "${TARGET}")"
CURRENT="$(python_cfg "${CFG}" get)"
ACCT="$(python_cfg "${CFG}" acct "${PROFILE}")"

echo "현재: $(label_of "${CURRENT}") (${CURRENT})"
echo "변경: $(label_of "${PROFILE}") (${PROFILE}) 계좌 ${ACCT}"
echo "트레일 DB는 프로필별로 다릅니다. 실전 잔고의 KOSPI/KOSDAQ 선물은 다음 1분에 5pt 자동 감시됩니다."

if [[ "${YES}" -ne 1 ]]; then
  if [[ "${PROFILE}" == "real_futures" ]]; then
    read -r -p "실전으로 전환합니다. yes 를 입력하세요: " answer
  else
    read -r -p "계속하려면 yes 를 입력하세요: " answer
  fi
  if [[ "${answer}" != "yes" ]]; then
    echo "취소했습니다."
    exit 1
  fi
fi

python_cfg "${CFG}" set "${PROFILE}" >/dev/null
echo "config.yml → active_profile: ${PROFILE}"
restart_daemons
echo "완료."
