# KONA

한국투자증권 Open API로 **KOSPI200 / 미니 KOSPI200 / KOSDAQ150 선물** 잔고를 보고, 트레일링 스톱이 닿으면 **시장가 청산**하는 보조 도구입니다.

신규 진입 주문은 내지 않습니다. UI의 **Stop**은 감시만 끄고, 포지션은 그대로 둡니다.

---

## 구성

| 구성 | 역할 |
| --- | --- |
| Streamlit UI (`app/kona_futures.py`) | 로그인, 잔고·포지션, 추적폭 변경, 감시 시작/중지 |
| 트레일링 워커 (`src/futures/trailing_run.py`) | 잔고 동기화, 분봉·현재가 폴링, 스톱 도달 시 청산 |
| `cfg/config.yml` | 활성 계좌, API 키, UI 비밀번호, 텔레그램 |

워커가 살아 있어야 자동 감시·청산이 돌아갑니다. UI만 켜 두면 화면은 보이지만 청산은 나가지 않습니다.

---

## 준비

- Python 3.11+ (이 서버는 miniforge3)
- 한투 Open API 앱키 / 시크릿, 선물옵션 계좌 (`acnt_prdt_cd: "03"`)
- (선택) Telegram Bot, Tailscale

```bash
cd /home/fransis/project/kona
pip install -r requirements.txt
```

`cfg/`와 `db/`는 git에 올라가지 않습니다. 클론한 뒤에는 설정을 직접 만듭니다.

```bash
mkdir -p cfg
```

`cfg/config.yml` 예시:

```yaml
active_profile: mock_futures
default_trail_points: 5.0
ui_password: "비밀번호"

telegram:
  bot_token: "봇토큰"
  user_chat_id: "채팅ID"

mock_futures:
  acctno: "모의계좌8자리"
  acnt_prdt_cd: "03"
  appkey: "앱키"
  seckey: "시크릿"

real_futures:
  acctno: "실전계좌8자리"
  acnt_prdt_cd: "03"
  appkey: "앱키"
  seckey: "시크릿"
```

- `active_profile`만 활성 계좌입니다. UI에서 계좌를 바꾸지 않습니다.
- 기본은 **모의투자** (`mock_futures`)를 권장합니다. 실전은 `real_futures`로 전환합니다.
- UI 비밀번호는 `KONA_UI_PASSWORD` 환경변수가 `ui_password`보다 우선합니다.
- 텔레그램이 없으면 청산 알림만 생략됩니다. 봇 토큰과 `user_chat_id`(또는 `chat_id`)가 있으면 발동·체결 시 메시지가 갑니다.
- 액세스 토큰은 `cfg/.token_<프로필>.json`에 캐시됩니다.

환경변수:

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `KIS_SSL_VERIFY` | `1` | API SSL 검증 |
| `KIS_API_INTERVAL` | `0.05` | REST 호출 간격(초). 한투 한도 20 TPS |
| `KONA_UI_PASSWORD` | (없음) | UI 로그인 비밀번호 덮어쓰기 |

---

## 로컬 실행

프로젝트 루트에서 UI와 워커를 **둘 다** 띄웁니다.

```bash
cd /home/fransis/project/kona
export PYTHONPATH=src

# 터미널 1 — UI (기본 127.0.0.1:18501)
streamlit run app/kona_futures.py

# 터미널 2 — 워커
python src/futures/trailing_run.py --immediate
```

브라우저: [http://127.0.0.1:18501](http://127.0.0.1:18501)

워커 옵션:

```bash
python src/futures/trailing_run.py --help

# 한 틱만 (분봉 + 현재가)
python src/futures/trailing_run.py --once

# 청산 주문 없이 감시만
python src/futures/trailing_run.py --immediate --dry-run

# 프로필을 config와 다르게 고정
python src/futures/trailing_run.py --immediate --profile mock_futures
```

`--profile`을 생략하면 매 틱 `config.yml`의 `active_profile`을 다시 읽습니다. 워커는 한 프로세스만 뜹니다 (`db/trailing_worker.lock`).

---

## UI 사용

1. `ui_password`로 로그인합니다. 세션은 URL `?sid=`로 약 12시간 유지됩니다.
2. 우측 상단 **Online / Offline**이 워커 상태입니다. Offline이면 **워커 시작** 또는 **Dry-run**으로 띄울 수 있습니다.
3. **Account** — 추정예탁자산, 당일실현손익, 미실현손익. **자세히**에 예수금·증거금 등.
4. **Positions** — 보유 종목. 트레일 중인 행만 스톱가·잔여/추적폭이 보입니다.
5. **Strategy** — KOSPI/KOSDAQ 선물만 대상입니다.
   - **Start Trail**: 추적폭(pt)을 넣고 감시 시작
   - **Update**: 추적폭만 변경
   - **Stop**: 감시 중지. 청산 주문은 나가지 않습니다.
   - 청산 진행 중(`triggered` / `closing`)에는 Start/Stop이 막힙니다.
6. **Activity** — 동기화·발동·주문 이벤트 로그

화면은 약 10초마다 갱신됩니다.

### 트레일링이 하는 일

- 대상: 종목코드가 `A01`(KOSPI200), `A05`(미니), `A06`(KOSDAQ150)로 시작하는 선물만.
- 신규·재진입 포지션은 워커가 `default_trail_points`(기본 5.0pt)로 **자동 감시**를 켭니다. Stop으로 끈 종목은 다시 Start해야 합니다.
- 롱: 고점(현재가·분봉 종가) − 추적폭이 스톱. 현재가 ≤ 스톱이면 시장가 매도.
- 숏: 저점 + 추적폭이 스톱. 현재가 ≥ 스톱이면 시장가 매수.
- 스톱 도달 시에만 청산합니다. 진입 주문은 없습니다.

모의투자는 **야간 선물 주문을 지원하지 않습니다.** 야간 청산은 실전 프로필에서만 가능합니다.

---

## 계좌 전환 (모의 ↔ 실전)

UI가 아니라 스크립트로만 바꿉니다. systemd가 깔려 있으면 데몬도 같이 재시작합니다.

```bash
./deploy/switch-account.sh status   # 현재 프로필·서비스 상태
./deploy/switch-account.sh mock      # 모의투자
./deploy/switch-account.sh real     # 실전 (yes 확인)
./deploy/switch-account.sh reset    # 계좌 유지, Streamlit·워커만 재시작
```

`--yes`로 확인을 생략할 수 있습니다. 트레일 DB는 프로필마다 다릅니다 (`db/trailing_mock_futures.db`, `db/trailing_real_futures.db`).

실전 전환 시 잔고의 KOSPI/KOSDAQ 선물은 다음 워커 틱에서 기본 추적폭으로 자동 감시됩니다.

---

## systemd로 상시 실행

유닛 파일 안의 사용자·경로·Python은 이 머신 기준으로 적혀 있습니다. 다른 서버면 `deploy/systemd/*.service`를 먼저 고칩니다.

```bash
./deploy/systemd/install.sh
```

등록되는 서비스:

- `kona-streamlit` — Streamlit (`127.0.0.1:18501`)
- `kona-trailing-worker` — 워커 (`--immediate`)

```bash
sudo systemctl status kona-streamlit kona-trailing-worker
journalctl -u kona-streamlit -f
journalctl -u kona-trailing-worker -f
```

Tailscale이 있으면 설치 스크립트가 Serve를 맞춥니다. 따로 쓰려면:

```bash
./deploy/systemd/tailscale-serve.sh
```

`.streamlit/config.toml` 포트(기본 18501)로 `http://127.0.0.1:<port>`를 Serve합니다. 공유기 8501 포트포워딩과 겹치지 않게 분리되어 있습니다.

---

## 분봉·일봉 수집 (선택)

워커가 장중에 전광판 월물을 증분 갱신합니다. 과거 구간을 미리 채우거나 DB를 다시 만들 때만 CLI를 씁니다.

```bash
export PYTHONPATH=src
python src/futures/futures_update.py --profile mock_futures
python src/futures/futures_update.py --profile mock_futures --extend -v
python src/futures/futures_update.py --interval 1m --no-past
python src/futures/futures_update.py --reset   # 기존 테이블 삭제 후 재생성
```

기본 상품: `kospi200_mini,kospi200,kosdaq150`. 저장 위치: `db/futures.db`.

세션 마감 catch-up은 워커가 합니다. 주간 평일 16:00, 야간 06:30~08:45.

---

## 워커 주기

장중(주간 평일 08:45~15:46, 야간 월~금 18:00~익일 06:01 / 일 18:00~월 06:01)에만 시세를 폴링합니다.

| 작업 | 주기 |
| --- | --- |
| 현재가·트레일 판정 | 10초 |
| 1분봉 갱신 | 매분 0초 직후 (~0.8초 지연) |
| 스톱 임박(urgent) | 2초 |
| 장외 | catch-up·하트비트만, 다음 장 시작까지 대기 |

---

## 데이터·로그

| 경로 | 내용 |
| --- | --- |
| `db/trailing_<프로필>.db` | 트레일 상태, 잔고 스냅샷, 이벤트 |
| `db/futures.db` | 월물별 1분봉·일봉 |
| `db/catchup_state.json` | 주간/야간 catch-up 완료일 |
| `db/trailing_worker.log` | UI에서 띄운 워커 로그 |
| `db/trailing_worker.pid` / `.lock` | 워커 단일 실행 |
| `db/ui_sessions/` | 로그인 세션 (12시간) |

---

## 점검 스크립트

```bash
# 실전 API — 잔고·시세. 실제 청산은 내지 않음
python scripts/verify_real_api.py

# 모의 계좌 코너케이스 (장중, 실제 주문 발생)
python scripts/mock_corner_tests.py
```

`mock_corner_tests.py`는 모의 계좌에 주문을 넣습니다. 평소 운영에는 쓰지 마세요.

---

## 주의

- 실전 프로필은 실제 돈이 움직입니다. `switch-account.sh real` 전에 계좌 번호를 확인하세요.
- Dry-run 워커는 청산 주문을 보내지 않습니다. 화면 우측 **DRY-RUN** 표시를 확인하세요.
- 한투 REST 한도를 넘기지 않도록 기본 50ms 간격을 유지하세요.
- 이 도구는 보조툴입니다. 체결·잔고의 최종 확인은 한투 HTS/MTS로 하세요.
