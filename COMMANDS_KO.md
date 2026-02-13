# auto-trader 운영/테스트 명령어 모음 (PowerShell)

기준 경로: `c:\Users\0\auto-trader`

## 0) 공통

```powershell
cd c:\Users\0\auto-trader
```

## 1) 가상환경 / 의존성

```powershell
# python 확인
.\.venv\Scripts\python.exe -V

# 패키지 설치(개발 포함)
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 테스트 전용 의존성
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 2) 서버 실행 (trader_engine FastAPI)

```powershell
# 통합 실행(권장): engine + discord bot 동시 실행
.\.venv\Scripts\python.exe -m apps.run_all

# 권장 실행
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api

# 대안(직접 uvicorn)
.\.venv\Scripts\python.exe -m uvicorn apps.trader_engine.main:app --host 127.0.0.1 --port 8000
```

## 3) 기본 점검 API

```powershell
# 헬스체크
curl http://127.0.0.1:8000/health

# 전체 상태(엔진/리스크/PnL/WS/워치독 포함)
curl http://127.0.0.1:8000/status
```

## 4) 엔진 제어 API

```powershell
# 시작/중지/패닉
curl -Method Post http://127.0.0.1:8000/start
curl -Method Post http://127.0.0.1:8000/stop
curl -Method Post http://127.0.0.1:8000/panic
```

## 5) 리스크 설정 API

```powershell
# 현재 리스크 설정
curl http://127.0.0.1:8000/risk

# 프리셋 적용
curl -Method Post http://127.0.0.1:8000/preset -ContentType application/json -Body '{ "name": "conservative" }'
curl -Method Post http://127.0.0.1:8000/preset -ContentType application/json -Body '{ "name": "normal" }'
curl -Method Post http://127.0.0.1:8000/preset -ContentType application/json -Body '{ "name": "aggressive" }'

# 단일 키 수정
curl -Method Post http://127.0.0.1:8000/set -ContentType application/json -Body '{ "key": "max_leverage", "value": "5" }'
curl -Method Post http://127.0.0.1:8000/set -ContentType application/json -Body '{ "key": "exec_mode_default", "value": "LIMIT" }'
curl -Method Post http://127.0.0.1:8000/set -ContentType application/json -Body '{ "key": "notify_interval_sec", "value": "1800" }'
```

## 6) 수동 트레이드 API (주의)

실계정 키가 설정되어 있으면 실제 Binance Futures 주문이 나갑니다.

```powershell
# 진입
curl -Method Post http://127.0.0.1:8000/trade/enter -ContentType application/json `
  -Body '{ "symbol":"BTCUSDT", "direction":"LONG", "exec_hint":"LIMIT", "notional_usdt":50 }'

# 단일 청산
curl -Method Post http://127.0.0.1:8000/trade/close -ContentType application/json `
  -Body '{ "symbol":"BTCUSDT" }'

# 전체 청산
curl -Method Post http://127.0.0.1:8000/trade/close_all
```

## 7) Discord Bot 실행

`.env` 필수:

```text
DISCORD_BOT_TOKEN=...
TRADER_API_BASE_URL=http://127.0.0.1:8000
DISCORD_GUILD_ID=...
```

실행:

```powershell
.\.venv\Scripts\python.exe -m apps.discord_bot.bot

# 통합 실행에서 bot만 띄우기
.\.venv\Scripts\python.exe -m apps.run_all --bot-only
```

## 7-1) 통합 런처 옵션

```powershell
# engine + bot 동시 실행
.\.venv\Scripts\python.exe -m apps.run_all

# engine만 실행
.\.venv\Scripts\python.exe -m apps.run_all --engine-only

# bot만 실행
.\.venv\Scripts\python.exe -m apps.run_all --bot-only
```

종료는 `Ctrl + C` 한 번으로 두 프로세스 모두 정리됩니다.

## 7-2) Discord 명령어 정리 (슬래시 + 패널)

### 슬래시 커맨드

- `/status`: 현재 상태 요약 조회
- `/risk`: 현재 risk_config 전체 조회
- `/start`: 엔진 시작 (`POST /start`)
- `/stop`: 엔진 중지 (`POST /stop`)
- `/panic`: 패닉 락 + 정리 청산 (`POST /panic`)
- `/close symbol:BTCUSDT`: 심볼 단일 청산 (`POST /trade/close`)
- `/closeall`: 전체 포지션 청산 (`POST /trade/close_all`)
- `/set key:<risk_key> value:<string>`: 리스크 키 단일 수정 (`POST /set`)
  - `max_exposure_pct` 입력 규칙: `0.2`(ratio) 또는 `20%`(퍼센트 표기)만 허용, `20`은 거부
- `/preset name:<conservative|normal|aggressive>`: 프리셋 적용 (`POST /preset`)
- `/panel`: 컨트롤 패널 메시지 생성/갱신

### `/set`에서 자주 쓰는 key 예시

- `max_leverage`
- `max_exposure_pct`
- `max_notional_pct`
- `per_trade_risk_pct`
- `daily_loss_limit_pct`
- `dd_limit_pct`
- `min_hold_minutes`
- `score_conf_threshold`
- `exec_mode_default`
- `exec_limit_timeout_sec`
- `exec_limit_retries`
- `notify_interval_sec`
- `spread_max_pct`
- `allow_market_when_wide_spread`
- `enable_watchdog`
- `watchdog_interval_sec`
- `shock_1m_pct`
- `shock_from_entry_pct`

### 패널(`/panel`)에서 가능한 조작

- 버튼: `Start`, `Stop`, `Panic`, `Refresh`
- 드롭다운: `Preset(conservative/normal/aggressive)`
- 드롭다운: `Exec mode(LIMIT/MARKET/SPLIT)`
- 모달:
  - `Risk Basic`: `max_leverage`, `max_exposure_pct`, `max_notional_pct`, `per_trade_risk_pct`
  - `Risk Adv`: `daily_loss_limit_pct`, `dd_limit_pct`, `min_hold_minutes`, `score_conf_threshold`

### 권한/주의

- 패널 버튼/모달 조작은 관리자(`Administrator`)만 허용
- 봇 권한 권장: `Send Messages`, `Embed Links`, `Read Message History`, `Use Application Commands`

## 8) 테스트

```powershell
# 전체 테스트
.\.venv\Scripts\python.exe -m pytest

# e2e 제외
.\.venv\Scripts\python.exe -m pytest -m "not e2e"

# 단위만
.\.venv\Scripts\python.exe -m pytest -m unit

# 통합만
.\.venv\Scripts\python.exe -m pytest -m integration
```

## 9) 코드 품질 체크

```powershell
# 문법 컴파일 체크
.\.venv\Scripts\python.exe -m compileall -q apps shared tests

# ruff
.\.venv\Scripts\python.exe -m ruff check .
```

## 10) 포트 8000 프로세스 종료

```powershell
$p=(netstat -ano | findstr ":8000" | findstr LISTENING | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1)
if($p){ taskkill /PID $p /F } else { "no_listener" }
```

## 11) DB 빠른 조회 (SQLite)

기본 DB: `./data/auto_trader.sqlite3`

```powershell
# 테이블 목록
.\.venv\Scripts\python.exe -c "import sqlite3, os; p=os.path.abspath('./data/auto_trader.sqlite3'); con=sqlite3.connect(p); print([r[0] for r in con.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall()]); con.close()"

# pnl_state 1행
.\.venv\Scripts\python.exe -c "import sqlite3, os; p=os.path.abspath('./data/auto_trader.sqlite3'); con=sqlite3.connect(p); con.row_factory=sqlite3.Row; r=con.execute('SELECT * FROM pnl_state WHERE id=1').fetchone(); print(dict(r) if r else None); con.close()"
```

## 12) (위험) pnl_state 리셋

테스트 목적 외 사용 금지.

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3, datetime, os; p=os.path.abspath('./data/auto_trader.sqlite3'); now=datetime.datetime.now(datetime.timezone.utc).isoformat(); day=datetime.datetime.now(datetime.timezone.utc).date().isoformat(); con=sqlite3.connect(p); con.execute('UPDATE pnl_state SET day=?, daily_realized_pnl=?, lose_streak=?, cooldown_until=NULL, last_block_reason=NULL, updated_at=? WHERE id=1', (day,0.0,0,now)); con.commit(); con.close(); print('pnl_state reset')"
```

## 13) 실계정 안전 체크리스트

- `TRADING_DRY_RUN=true` 상태에서 먼저 `/status`, `/start`, `/stop` 동작 확인
- Binance Futures 계정이 One-way 모드인지 확인 (Hedge mode 금지)
- 소액 심볼 1개로만 시작
- `max_leverage`, `max_exposure_pct`, `daily_loss_limit_pct`, `dd_limit_pct` 먼저 보수적으로 설정
- Discord 알림(`DISCORD_WEBHOOK_URL`) 수신 확인 후 실거래 전환
