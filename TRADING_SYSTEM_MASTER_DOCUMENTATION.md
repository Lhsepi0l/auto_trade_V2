# 트레이딩 시스템 마스터 문서

## 0. 문서 목적
이 문서는 `auto-trader` 프로젝트를 소스코드 없이도 운영/이해할 수 있도록 만든 통합 운영 문서입니다.
대상 독자는 시스템 오너(운영자)이며, 실전 배포 전 체크리스트와 장애 대응 흐름까지 포함합니다.

---

## 1. 이 트레이딩 시스템이 무엇인가
`auto-trader`는 **Binance USDⓈ-M 선물 자동매매 시스템**입니다.
구성은 크게 두 축입니다.

1. `FastAPI` 기반 트레이딩 엔진 API
2. `discord.py` 기반 운영 패널(UI)

핵심 특징:
- 자동 전략 판단(스케줄러 tick 기반)
- 리스크/예산/포지션 제한
- 트레일링 스탑(PCT/ATR)
- 주문 멱등성(clientOrderId 기반) + 재시작 복구(reconcile)
- User Data Stream 장애 시 안전모드 진입
- SQLite 기반 상태/이벤트/체결/스냅샷 영속화

---

## 2. 전체 아키텍처

### 2.1 상위 구조
```text
[Discord Operator]
      |
      v
[Discord Bot Panel] --HTTP--> [FastAPI Trader Engine]
                                   |
                                   +--> [Strategy / Risk / Sizing / Execution]
                                   +--> [User Stream WS + ListenKey Manager]
                                   +--> [Scheduler + Watchdog]
                                   +--> [SQLite]
                                   +--> [Binance REST/WS]
```

### 2.2 주요 모듈(개념)
- API 계층: `/status`, `/start`, `/stop`, `/panic`, `/set`, `/trade/*` 등 운영 엔드포인트
- 전략 계층: 후보 심볼/방향/점수 계산, 진입 의도(intent) 생성
- 리스크 계층: 노출도/레버리지/손실한도/쿨다운/스프레드 가드
- 사이징 계층: 잔고/마크가격/거래소 필터 기반 수량 계산
- 실행 계층: 주문 전송, 멱등성, 상태 기록
- 복구 계층: 재시작 시 DB 상태와 거래소 상태 동기화
- 모니터링 계층: 운영 이벤트 로깅, PnL 스냅샷 저장, 상태 알림

---

## 3. 내부 동작 방식(엔드투엔드)

### 3.1 정상 사이클
1. 스케줄러 tick 발생
2. 전략이 진입 의사결정(심볼/방향/신뢰도) 생성
3. 리스크 가드 통과 여부 평가
4. 사이징 계산(예산 -> 증거금 -> 노셔널 -> 수량)
5. 주문 멱등 ID 부여 후 주문 전송
6. WS 체결/주문 업데이트 반영
7. 상태/이벤트/스냅샷 저장

### 3.2 재시작/복구 사이클
1. 엔진 부팅
2. `startup_reconcile()` 실행 전까지 **recovery lock 활성화**
3. 거래소 open orders/포지션과 DB order_records 정합화
4. 성공 시 lock 해제, 전략 루프 정상 허용

### 3.3 WS 장애 사이클
1. WS 끊김 감지
2. 임계시간 초과 시 safe_mode=true
3. 신규 진입 차단(엔트리 금지)
4. close/panic은 허용
5. 재연결 성공 후 reconcile, 정상 복귀

---

## 4. 설치 및 실행 방법

## 4.1 환경 준비(Windows PowerShell 기준)
```powershell
cd C:\Users\0\auto-trader
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 4.2 환경변수 파일
```powershell
copy .env.example .env
```
`.env`에 Binance 키/Discord 토큰/API URL을 설정합니다.

### 4.3 실행
```powershell
# 엔진+봇 동시 실행
.\.venv\Scripts\python.exe -m apps.run_all

# 엔진만
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api

# 디스코드 봇만
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

### 4.4 기본 확인
```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/status
```

---

## 5. 설정 옵션 전체 설명
설정은 크게 두 종류입니다.

1. 프로세스 환경설정(`.env`)
2. 런타임 리스크/운영설정(`/set` + DB 저장)

### 5.1 핵심 런타임 설정(대표)
- `max_leverage`: 최대 레버리지
- `max_exposure_pct`: 총 가용자산 대비 최대 노출 비율(선택)
- `max_notional_pct`: 노셔널 제한(퍼센트)
- `per_trade_risk_pct`: 1회 트레이드 리스크 상한
- `daily_loss_limit_pct`: 일일 손실 제한(음수)
- `dd_limit_pct`: 드로우다운 제한(음수)
- `spread_max_pct`: 스프레드 과대 시 진입 차단
- `exec_mode_default`: LIMIT / MARKET / SPLIT

### 5.2 예산/증거금 설정
- `capital_mode`: `PCT_AVAILABLE` | `FIXED_USDT` | `MARGIN_BUDGET_USDT`
- `capital_pct`: 가용자산 비율 모드에서 사용할 비율
- `capital_usdt`: 고정 USDT 모드 예산
- `margin_budget_usdt`: 증거금 예산 고정 모드 금액
- `margin_use_pct`: 예산 중 실제 증거금으로 사용할 비율
- `max_position_notional_usdt`: 포지션당 노셔널 하드캡
- `max_exposure_pct`: 가용자산 대비 추가 노출 캡
- `fee_buffer_pct`: 수수료/슬리피지 버퍼

### 5.3 트레일링 설정
- `trailing_enabled`: 트레일링 활성/비활성
- `trailing_mode`: `PCT` | `ATR`
- `trail_arm_pnl_pct`: 트레일링 활성화 시작 PnL%
- `trail_distance_pnl_pct`: PCT 모드 추적 거리
- `trail_grace_minutes`: 진입 후 트레일링 유예 시간
- `atr_trail_timeframe`: ATR 기준 타임프레임(`15m`/`1h`/`4h`)
- `atr_trail_k`: ATR% 배수
- `atr_trail_min_pct`: 최소 트레일 거리
- `atr_trail_max_pct`: 최대 트레일 거리

---

## 6. 리스크 관리 로직
리스크는 주문 직전 다층으로 검사됩니다.

1. 엔진 상태 검사: STOPPED/PANIC/recovery_lock/safe_mode
2. 계정 리스크 검사: 일손실/드로우다운/연패 쿨다운
3. 시장 리스크 검사: 스프레드 한도
4. 포지션 리스크 검사: 단일자산 규칙, 노출도/노셔널/레버리지 상한
5. 예산 리스크 검사: 사이징 block(minNotional/minQty/예산부족)

차단 시:
- 주문 미전송
- block reason 반환
- notifier 이벤트 전송 (`ENTRY BLOCKED: <reason>`)
- DB `risk_blocks`/`op_events` 기록

---

## 7. 트레일링 스탑 로직(PCT & ATR)

### 7.1 공통
- 포지션 1개 기준 상태 관리: `entry_ts`, `peak_pnl_pct`, `armed`
- 유예시간(`trail_grace_minutes`) 전에는 트레일링 미적용
- Shock close 로직이 우선순위 더 높음

### 7.2 PCT 모드
- Arm 조건: `pnl% >= trail_arm_pnl_pct`
- Peak 갱신: arm 이후 최고 pnl% 추적
- Trigger 조건: `현재 pnl% <= peak - trail_distance_pnl_pct`
- 청산 사유: `TRAILING_PCT`

### 7.3 ATR 모드
- ATR% 계산: `ATR / close_price * 100`
- 거리 계산: `dist = clamp(atr_trail_k * atr_pct, atr_trail_min_pct, atr_trail_max_pct)`
- Trigger 조건: `현재 pnl% <= peak - dist`
- 청산 사유: `TRAILING_ATR`

---

## 8. 주문 멱등성(Idempotency) 및 재시도 안정성
핵심은 **중복 진입 방지**입니다.

### 8.1 client order id 강제
- 신규 주문마다 `newClientOrderId`를 생성
- 형식 예: `BOT-{env}-{intent_id}-{attempt}` (허용 문자/길이 제한 준수)
- DB `order_records.client_order_id`에 UNIQUE 제약

### 8.2 주문 플로우
1. DB에 `CREATED` 기록
2. 거래소 주문 전송(`newClientOrderId` 포함)
3. 응답 수신 시 `ACK/SENT` + exchange_order_id 반영
4. WS 이벤트로 `PARTIAL/FILLED/CANCELED/...` 상태 진화

### 8.3 타임아웃 재시도 원칙
- 타임아웃 시 바로 재전송 금지
- 먼저 `client_order_id`로 거래소 조회
- 있으면 추적만 계속(중복 방지)
- 없다고 확인될 때만 새 attempt ID로 재주문

---

## 9. WebSocket 안전모드(safe mode)

### 9.1 listenKey 수명주기
- 생성: `POST /fapi/v1/listenKey`
- 유지: 주기적 keepalive (`PUT /fapi/v1/listenKey`)
- 무효/만료 시 재생성

### 9.2 장애 시 동작
- WS 미연결이 임계시간 초과하면 safe_mode 진입
- 신규 엔트리 차단
- 청산/패닉 허용
- 알림: `WS_DOWN_SAFE_MODE`

### 9.3 상태 지표(`/status`)
- `ws_connected`
- `listenKey_last_keepalive_ts`
- `last_ws_event_ts`
- `safe_mode`

---

## 10. Discord 패널 사용법
패널은 운영자가 API를 쉽게 제어하는 UI입니다.

### 10.1 주요 버튼
- `시작` / `중지` / `패닉` / `새로고침`
- `리스크 기본` / `리스크 고급`
- `증거금설정`
- `트레일링설정`
- `프리셋 적용` / `직접 입력...`

### 10.2 권한
- 관리자 전용 동작은 admin gate 적용
- 비관리자는 ephemeral deny 메시지 처리

### 10.3 패널 동작
- 버튼/모달 입력 -> FastAPI `/set` 호출
- 성공 후 `/status` 재조회
- 기존 메시지 embed 갱신

---

## 11. 예산/증거금 제어 로직

### 11.1 계산 흐름
```text
available -> fee buffer 적용 -> available_net
  -> capital_mode 기준 budget 결정
  -> (optional) exposure cap
  -> used_margin = budget * margin_use_pct
  -> notional = used_margin * leverage
  -> (optional) max_position_notional cap
  -> 거래소 filter(minQty/minNotional/stepSize)로 qty 산출
```

### 11.2 block 조건
- `budget <= 0`
- `minNotional` 미달
- `minQty` 미달
- 잔고/마크가격 조회 불가

결과는 `capital_snapshot`과 block reason으로 운영자에게 노출됩니다.

---

## 12. 재시작 시 reconcile 동작

### 12.1 startup_reconcile 핵심
- 거래소 open orders 조회
- 거래소 포지션 조회
- DB `order_records`와 매핑(clientOrderId)
- DB pending인데 거래소에 있으면 ACK/OPEN 계열로 보정
- DB open인데 거래소에 없으면 CANCELED/EXPIRED 보정

### 12.2 recovery lock
- reconcile 성공 전 신규 진입 차단
- 허용 API: 상태조회/중지/패닉/청산
- 실패 시 lock 유지(보수적 안전 정책)

---

## 13. 데이터베이스 구조 개요(SQLite)

주요 테이블:
- `risk_config`: 런타임 리스크/예산/트레일링 설정
- `engine_state`: 엔진 상태(STOPPED/RUNNING/PANIC 등)
- `pnl_state`: 누적 손익/연패 등 리스크 지표
- `status_snapshot`: 상태 캐시
- `op_events`: 범용 운영 이벤트(JSON)
- `decisions`: 전략 의사결정 로그
- `executions`: 주문/체결 실행 로그
- `risk_blocks`: 차단 이벤트 로그
- `pnl_snapshots`: 시점별 포지션/PnL 스냅샷
- `order_records`: 멱등 주문 상태 추적(UNIQUE client_order_id)
- `schema_migrations`: 스키마 버전 관리

---

## 14. 운영 워크플로우(정상 흐름)

```text
[부팅]
  -> DB 마이그레이션
  -> 서비스 초기화
  -> startup reconcile
  -> (성공) recovery lock 해제
  -> 스케줄러 tick
  -> 전략 결정
  -> 리스크/사이징 통과
  -> 주문 전송/추적
  -> PnL 스냅샷/알림/로그
```

운영자가 주로 보는 것:
- Discord panel 상태
- `/status`의 `capital_snapshot`, `safe_mode`, `last_error`
- 로그(`engine.log`, `bot.log`)와 DB 이벤트 테이블

---

## 15. 비상 워크플로우(패닉 / WS 다운 / safe mode)

### 15.1 패닉
- `/panic` 호출 시 신규 진입 중단
- 가능한 포지션 정리 시도
- 멱등 처리(2번 호출해도 안정 상태 유지)

### 15.2 WS 다운
- safe mode 진입 후 신규 진입 차단
- `close`/`close_all`/`panic` 허용
- 재연결 후 reconcile 완료 시 정상 복귀

### 15.3 강제 점검 순서
1. `/status`에서 safe_mode/last_ws_event_ts 확인
2. 필요 시 `/stop` 후 재기동(재기동 시 reconcile 자동)
3. 포지션 리스크 있으면 `/panic`

---

## 16. 프로덕션 안전 배포 방법

### 16.1 권장 단계
1. DRY_RUN으로 충분한 기간 검증
2. 소액 예산/저레버리지로 라이브 전환
3. 초반 1~2일은 수동 모니터링 강화
4. 이상 징후 시 즉시 stop/panic

### 16.2 보안
- Binance 키는 선물 권한 최소화(출금 권한 금지)
- IP 화이트리스트 권장
- `.env`/로그에 시크릿 출력 금지

### 16.3 운영 분리
- 테스트/실전 계정 분리 권장
- 테스트 모드 스모크 스크립트 정기 실행

---

## 17. 라이브 런칭 체크리스트(권장)

1. `TRADING_DRY_RUN=false` 최종 확인
2. API 키 권한(USD-M Futures, Trade, Read) 확인
3. `capital_mode`/예산/캡/레버리지 수동 지정
4. `/status` 확인:
   - `engine_state=STOPPED` (런칭 직전)
   - `safe_mode=false`
   - `ws_connected=true`
   - `capital_snapshot.blocked=false`
5. `RUNBOOK.md` 비상 절차 숙지
6. 시작 후 첫 사이클은 실시간 모니터링

---

## 18. 자주 하는 실수와 트러블슈팅

### 18.1 신규 진입이 안 되는 경우
- `safe_mode=true`인지 확인
- `recovery_lock_active` 여부 확인
- `capital_snapshot.block_reason` 확인
- 리스크 제한(일손실/DD/쿨다운/스프레드) 확인

### 18.2 주문 중복 우려
- `order_records`에서 동일 `client_order_id` 중복 여부 확인
- 타임아웃 직후 재시도 로직이 조회 우선인지 확인

### 18.3 패널 모달 오류
- Discord 모달은 컴포넌트 5개 제한
- 필드 라벨 길이 제한(1~45)
- 관리자 권한/ephemeral 응답 처리 확인

### 18.4 상태가 이상할 때
- `/status` + `op_events` + `executions`를 함께 확인
- WS 이벤트 시각(`last_ws_event_ts`)가 오래됐는지 확인

---

## 19. 로그와 pnl_snapshots 해석법

### 19.1 로그
운영 로그는 구조화(JSON) 필드 중심으로 해석합니다.
대표 필드:
- `ts`, `level`, `component`, `event`
- `symbol`, `side`, `action`, `reason`
- `run_id`, `cycle_id`, `intent_id`, `client_order_id`

해석 팁:
- `intent_id` 기준으로 결정->실행->체결 연쇄 추적
- `client_order_id` 기준으로 멱등성 검증
- `reason`으로 block/close 원인 분석

### 19.2 pnl_snapshots
핵심 컬럼:
- `qty`, `entry_price`, `mark_price`
- `unrealized_pnl_usdt`, `unrealized_pnl_pct`
- `realized_pnl_usdt`
- `equity_usdt`, `available_usdt`

활용:
- 전략 성과 회고(시간대별 PnL 곡선)
- 트레일링 발동 전후 비교
- 비정상 급변(쇼크) 탐지 근거

---

## 20. 판매/라이선스 관점(비즈니스 요약)
이 시스템을 제품화할 때는 기술+운영+법적 고지를 함께 설계해야 합니다.

### 20.1 상품화 포인트
- Discord 기반 운영 UX(비개발자 접근성)
- 안정성 기능(멱등성, reconcile, safe mode)
- 감사 가능한 DB 로그/스냅샷

### 20.2 라이선스/패키징 아이디어
- 형태: 소스 라이선스 / 호스팅형 SaaS / 매니지드 운영형
- 과금: 월 구독 + 운영 지원 플랜
- 차별화: 전략 템플릿 + 리스크 프리셋 + 모니터링 대시보드

### 20.3 필수 고지
- 투자 손실 책임/면책 조항
- 거래소 장애/통신 장애 리스크
- 성과 보장 없음(백테스트/과거 성과는 미래 보장 아님)

---

## 부록 A. 자주 쓰는 운영 명령
```powershell
# 상태 조회
curl http://127.0.0.1:8000/status

# 엔진 시작/중지/패닉
curl -Method Post http://127.0.0.1:8000/start
curl -Method Post http://127.0.0.1:8000/stop
curl -Method Post http://127.0.0.1:8000/panic

# 전체 테스트
.\.venv\Scripts\python.exe -m pytest -q

# 스모크(테스트 모드)
.\.venv\Scripts\python.exe scripts\smoke_test_mode.py
```

## 부록 B. 실전 권장 초기값 예시(보수적)
- `capital_mode=PCT_AVAILABLE`
- `capital_pct=0.05`
- `margin_use_pct=0.5`
- `max_position_notional_usdt=100`
- `max_exposure_pct=0.2`
- `fee_buffer_pct=0.002`
- `max_leverage=5`
- `trailing_enabled=true`
- `trailing_mode=PCT`
- `trail_arm_pnl_pct=1.2`
- `trail_distance_pnl_pct=0.8`

위 값은 출발점일 뿐이며, 실계좌 특성과 전략 변동성에 맞춰 점진 조정이 필요합니다.
