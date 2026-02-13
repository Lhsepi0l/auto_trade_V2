# auto-trader

Binance USDT-M 선물 자동매매 시스템입니다.  
구성: **트레이더 엔진(FastAPI)** + **디스코드 봇(discord.py)**

관련 문서:
- 전체 사용 가이드: `USAGE_KO.md`
- 디스코드 전용 가이드: `DISCORD_USAGE_KO.md`
- 예산 운영 가이드: `OPS_BUDGET.md`
- 운영 런북: `RUNBOOK.md`

## 핵심 구성
- `apps/trader_engine`: 제어 API, 스케줄러, 리스크/사이징/실행, 복구 로직
- `apps/discord_bot`: 슬래시 커맨드 + 패널 UI(`/panel`)

## 안전 주의사항 (필독)
- 이 프로젝트는 **USDT-M 선물 전용**입니다.
- Binance API 키에 **출금 권한을 절대 주지 마세요**.
- 기본값은 `TRADING_DRY_RUN=true`로 신규 진입 주문을 막습니다.
- 기본적으로 `/close`, `/panic`는 dry-run에서도 허용됩니다(운영 안전 목적).
- `DRY_RUN_STRICT=true`면 `/close`, `/panic`도 차단됩니다.
- 엔진 기본 시작 상태는 `STOPPED`이며, `/start` 호출 전 주문이 나가지 않습니다.

## 설치
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
pip install -e ".[dev]"
copy .env.example .env
```

## 실행 (권장: 통합 실행)
```powershell
.\.venv\Scripts\python.exe -m apps.run_all
```

옵션:
```powershell
.\.venv\Scripts\python.exe -m apps.run_all --engine-only
.\.venv\Scripts\python.exe -m apps.run_all --bot-only
```

## 엔진만 실행
```powershell
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api
```

빠른 상태 확인:
```powershell
curl http://127.0.0.1:8000/status
curl -X POST http://127.0.0.1:8000/start
```

## 주요 API
- `GET /status`: 전체 상태/리스크/자본 스냅샷
- `POST /start`: 엔진 시작
- `POST /stop`: 엔진 중지
- `POST /panic`: 비상 정리(패닉)
- `POST /set`: 런타임 설정 변경
- `POST /trade/enter`, `/trade/close`, `/trade/close_all`

## 설정 구조 (중요)
- 거래/리스크 핵심 설정은 SQLite `risk_config(id=1)`에 저장됩니다.
- `.env`는 런타임/인프라 설정(DB 경로, 로그, API 키, dry-run 등) 중심입니다.

## 디스코드 알림
- `DISCORD_WEBHOOK_URL` 설정 시 엔진 이벤트/상태 알림 전송
- 비어 있으면 로그만 기록
- 상태 알림 주기는 `risk_config.notify_interval_sec` (기본 1800초 = 30분)

## 유저 스트림(WS)
- `listenKey` 생성/유지/재연결을 자동 관리합니다.
- `/status`에서 아래 항목을 확인할 수 있습니다.
  - `ws_connected`
  - `last_ws_event_time`
  - `safe_mode`
  - `last_fill`

## 디스코드 봇 실행
```powershell
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

## 디스코드 패널
`/panel`로 패널 메시지를 생성/갱신합니다.

주요 컨트롤:
- 버튼: `시작`, `중지`, `패닉`, `새로고침`
- 예산/증거금: `증거금설정`, 프리셋/직접 입력
- 리스크: `리스크 기본`, `리스크 고급`
- 트레일링: `트레일링설정`

권한:
- 관리자만 패널 설정 변경 가능

## 테스트
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 디스코드 권한 체크리스트
1. OAuth2 Scope
- `bot`
- `applications.commands`

2. 봇 권한(권장)
- `Send Messages`
- `Embed Links`
- `Read Message History`
- `Use Application Commands`

3. 패널 조작 권한
- 길드 관리자(Administrator) 권한 필요
