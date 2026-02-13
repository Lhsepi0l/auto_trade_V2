# Discord 사용 가이드 (auto-trader)

이 문서는 `apps/discord_bot` 기준으로, 디스코드에서 실제 운영할 때 필요한 내용만 정리합니다.

## 1. 먼저 알아둘 점

- 디스코드는 **원격 조작 UI**입니다.
- 실제 매매 로직/실행은 `trader_engine`이 담당합니다.
- 따라서 봇만 켜서는 동작하지 않고, **엔진(API)도 같이 실행**되어야 합니다.

중요:
- 아래 값들은 디스코드에서 바꿀 수 없습니다. `.env`에서 설정 후 재시작해야 적용됩니다.
  - `TRADING_DRY_RUN`
  - `SCHEDULER_ENABLED`
  - `SCHEDULER_TICK_SEC`

## 2. .env 필수 설정

최소 필수:

```env
DISCORD_BOT_TOKEN=...
TRADER_API_BASE_URL=http://127.0.0.1:8000
DISCORD_GUILD_ID=123456789012345678
```

자동매매를 원하면 추가로:

```env
TRADING_DRY_RUN=false
SCHEDULER_ENABLED=true
SCHEDULER_TICK_SEC=60
```

설명:
- `TRADING_DRY_RUN=false`: 신규 진입 주문 허용
- `SCHEDULER_ENABLED=true`: 자동매매 스케줄러 활성화
- `SCHEDULER_TICK_SEC=60`: 60초마다 판단 (테스트용 권장; 운영 시 더 길게 가능)

참고:
- `DISCORD_TOKEN`도 호환되지만, 권장 키는 `DISCORD_BOT_TOKEN`입니다.

## 3. 실행 순서

1. 엔진 실행

```powershell
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api
```

2. 디스코드 봇 실행

```powershell
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

또는 통합 실행:

```powershell
.\.venv\Scripts\python.exe -m apps.run_all
```

## 4. 디스코드 권한

OAuth2 scope:
- `bot`
- `applications.commands`

권장 채널 권한:
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Use Application Commands`

권한 제한:
- `/panel` 생성/갱신, 패널 버튼/모달 조작은 `Administrator` 권한 사용자만 가능

## 5. 슬래시 명령어

- `/status`: 엔진/거래소/리스크 상태 요약
- `/risk`: 현재 리스크 설정 조회
- `/start`: 엔진 시작
- `/stop`: 엔진 정지
- `/panic`: 패닉 상태 전환 + 정리 동작
- `/close symbol:BTCUSDT`: 단일 포지션 청산
- `/closeall`: 전체 포지션 청산
- `/set key:<risk_key> value:<값>`: 리스크 키 1개 수정
- `/preset name:<conservative|normal|aggressive>`: 프리셋 적용
- `/panel`: 컨트롤 패널 메시지 생성/갱신

## 6. /panel 사용법

`/panel`을 실행하면 채널에 패널 임베드가 생성됩니다.

구성:
- 버튼: `Start`, `Stop`, `Panic`, `Refresh`
- 버튼: `Risk Basic`, `Risk Adv`
- 셀렉트: `Preset` (`conservative`, `normal`, `aggressive`)
- 셀렉트: `Exec mode` (`LIMIT`, `MARKET`, `SPLIT`)

동작:
- 같은 채널에서 `/panel`을 다시 실행하면 기존 패널을 갱신합니다.
- 버튼/모달 실행 후 상태 임베드가 갱신됩니다.

## 7. 자동매매 시작 절차 (실전용)

1. `.env` 확인
- `TRADING_DRY_RUN=false`
- `SCHEDULER_ENABLED=true`
- `SCHEDULER_TICK_SEC` 원하는 값

2. 프로세스 재시작
- 엔진/봇 둘 다 재시작

3. 디스코드에서 `/status` 확인
- `dry_run=false`
- `engine_state`가 정상
- `binance.private_ok=true`
- `enabled_symbols`가 기대값인지 확인

4. 리스크 보수적으로 세팅
- `/preset conservative`
- 필요시 `/set`으로 `max_leverage`, `max_exposure_pct` 등 조정

5. 엔진 시작
- `/start`

6. 모니터링
- `/panel` 고정
- `/status`로 `last_error`, `last_action`, `ws_connected` 확인

7. 이상 시 즉시 대응
- `/stop` (정지)
- `/panic` (긴급 정리)

## 8. 디스코드에서 가능한 것 / 불가능한 것

가능:
- 엔진 시작/중지/패닉
- 리스크 설정 변경
- 수동 청산
- 상태 조회 및 패널 모니터링

불가능:
- `.env` 값 변경 (`TRADING_DRY_RUN`, `SCHEDULER_ENABLED` 등)
- 프로세스 재시작
- API 키/토큰 갱신

## 9. 자주 발생하는 오류

`API 오류: 409`
- 상태 전이 충돌일 때 발생
- 현재 빌드 기준 `/start`는 RUNNING에서 멱등 처리
- `engine_in_panic`이면 패닉 상태 해소 절차 먼저 필요

`unknown interaction`
- 디스코드 상호작용 응답 시간 초과(보통 3초 제한)
- 잠시 후 재시도, 엔진 지연 여부 확인

`/panel`에 비활성 심볼 표시
- `/status`의 `disabled_symbols` 이유 확인
- `not_found_in_exchangeInfo`면 심볼 오타/미지원
- 골드 심볼은 `XAUUSDT` 사용

`/panel` 명령이 안 보임
- 봇 scope 확인 (`applications.commands`)
- `DISCORD_GUILD_ID` 확인
- 봇 재시작 후 커맨드 동기화 완료까지 대기

## 10. 보안 경고

- `.env`에 있는 Binance 키/Discord 토큰/웹훅은 노출되면 즉시 악용될 수 있습니다.
- 유출 흔적이 있으면 즉시 폐기하고 재발급하세요.
- 출금 권한이 있는 키는 사용하지 마세요.
