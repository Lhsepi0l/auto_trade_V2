# auto-trader 사용 가이드 (Korean)

이 문서는 `auto-trader`를 처음 실행하는 사람 기준으로, 설치부터 실사용 전 점검까지 한 번에 정리한 운영 가이드입니다.

## 1. 구성 이해

- `apps/trader_engine`: FastAPI 제어 API + 리스크/실행/스케줄러/워치독
- `apps/discord_bot`: Discord 슬래시 커맨드 + 패널 UI
- 저장소: SQLite (`risk_config`, `engine_state`, `pnl_state` 싱글톤 row 중심)

핵심 동작:
- 기본 엔진 상태는 `STOPPED`
- `POST /start` 호출 전에는 매매 실행 안 됨
- `TRADING_DRY_RUN=true`면 신규 진입 주문은 차단됨

## 2. 사전 준비

작업 경로:

```powershell
cd C:\Users\0\auto-trader
```

가상환경/의존성:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

환경파일:

```powershell
copy .env.example .env
```

## 3. .env 필수값

최소 필수:
- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `TRADER_API_BASE_URL` (봇 사용 시)
- `DISCORD_BOT_TOKEN` 또는 `DISCORD_TOKEN` (봇 사용 시)
- `DISCORD_GUILD_ID` (개발 중 커맨드 동기화용 권장)

권장 안전값:
- `TRADING_DRY_RUN=true`
- `DRY_RUN_STRICT=false` (기본)

심볼 주의:
- 골드 심볼은 `XAUUSDT`를 사용해야 합니다.
- `XAUTUSDT`는 Binance USDT-M `exchangeInfo` 기준 비활성 처리됩니다.

## 4. 실행 방법

통합 실행(권장, 엔진+봇):

```powershell
.\.venv\Scripts\python.exe -m apps.run_all
```

분리 실행:

```powershell
# 엔진만
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api

# 봇만
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

런처 옵션:

```powershell
.\.venv\Scripts\python.exe -m apps.run_all --engine-only
.\.venv\Scripts\python.exe -m apps.run_all --bot-only
```

## 5. API 빠른 점검

```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

엔진 제어:

```powershell
curl -Method Post http://127.0.0.1:8000/start
curl -Method Post http://127.0.0.1:8000/stop
curl -Method Post http://127.0.0.1:8000/panic
```

참고:
- `/start`는 멱등 처리라서 RUNNING 상태에서 다시 호출해도 200으로 유지됩니다.

## 6. Discord 명령어

슬래시 커맨드:
- `/status`
- `/risk`
- `/start`
- `/stop`
- `/panic`
- `/close symbol:BTCUSDT`
- `/closeall`
- `/set key:<risk_key> value:<value>`
- `/preset name:<conservative|normal|aggressive>`
- `/panel`

패널(`/panel`) 제공 기능:
- 버튼: `Start`, `Stop`, `Panic`, `Refresh`
- 프리셋: `conservative | normal | aggressive`
- 실행모드: `LIMIT | MARKET | SPLIT`
- 리스크 모달 2종(Basic/Adv)

## 7. 운영 절차 (추천 순서)

1. `.env`에서 `TRADING_DRY_RUN=true` 확인
2. 엔진/봇 실행
3. `/status`로 `startup_ok`, `private_ok`, `enabled_symbols` 확인
4. `/start` 실행 후 상태 `RUNNING` 확인
5. `/panel` 또는 `/set`으로 리스크 값 조정
6. 작은 notional로 `/trade/enter` 또는 실제 스케줄러 동작 검증
7. 충분히 검증된 뒤에만 dry-run 해제 검토

## 8. 자주 쓰는 설정 키 (`/set`)

- `max_leverage`
- `max_exposure_pct`
- `max_notional_pct`
- `per_trade_risk_pct`
- `daily_loss_limit_pct` (음수 비율, 예: `-0.05`)
- `dd_limit_pct` (음수 비율, 예: `-0.10`)
- `universe_symbols` (CSV, 예: `BTCUSDT,ETHUSDT,XAUUSDT`)
- `exec_mode_default` (`LIMIT|MARKET|SPLIT`)
- `spread_max_pct` (비율 단위)
- `enable_watchdog`
- `watchdog_interval_sec`
- `notify_interval_sec`

## 9. 트러블슈팅

`/panel`에서 심볼이 비활성:
- `disabled_symbols` 이유 확인
- `not_found_in_exchangeInfo`면 심볼 오타 또는 상장 미지원
- `XAUUSDT`/`XAUTUSDT` 오타 여부 확인

`private_ok=false`:
- API Key 권한/타입 확인 (USDT-M Futures)
- `BINANCE_BASE_URL` 확인 (`https://fapi.binance.com`)

Discord 명령 반응 없음:
- 봇 OAuth scope: `bot`, `applications.commands`
- 채널 권한: `Send Messages`, `Use Application Commands`, `Read Message History`, `Embed Links`
- 길드 등록/싱크 로그 확인

포트 충돌(8000):

```powershell
$p=(netstat -ano | findstr ":8000" | findstr LISTENING | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1)
if($p){ taskkill /PID $p /F } else { "no_listener" }
```

## 10. 테스트

```powershell
# 전체
.\.venv\Scripts\python.exe -m pytest -q

# e2e 제외
.\.venv\Scripts\python.exe -m pytest -m "not e2e" -q
```

## 11. 보안 체크 (중요)

- API 키에 출금 권한 부여 금지
- `.env`는 절대 외부 공유/커밋 금지
- 키/토큰이 노출되었으면 즉시 폐기 후 재발급
- 실계정 전환 전 `DRY_RUN` 단계에서 충분히 검증
