# Trading System Final Production Audit Report

Generated at: 2026-02-14 05:47 (local)

## Executive Summary
- Overall Risk Score (0-10): **7.9**
- Production Readiness Verdict: **NOT SAFE**

판정 근거:
- 주문 중복/경합 가능성이 완전히 제거되지 않음
- 일부 보호 로직이 "실패 시 차단"이 아니라 "실패 시 진행"(fail-open)으로 동작
- WebSocket 상태 불일치(연결은 살아있지만 이벤트 정지) 상황에서 신규 진입 차단 보장이 약함
- 패닉 API 응답이 청산 실패를 호출자에게 드러내지 않아 운영자 오판 위험이 큼

---

## Detailed Section-by-Section Audit

## 1) Architecture & Dependency Review
### What I verified
- 엔진(FastAPI), 실행/리스크/사이징/복구/WS 서비스 분리 구조 확인
- Discord 패널은 제어면(control-plane), 주문면은 엔진으로 집중된 구조 확인
- SQLite 단일 DB에 상태/로그/주문기록/스냅샷 집중 저장 확인

### Potential risks
- 서비스 간 동시 호출(스케줄러, API, watchdog, WS 콜백)이 존재하지만, 실행 계층에 전역 진입 락이 없음
- DB 연결 객체 하나를 다중 스레드에서 공유하며 읽기 경로는 락 없이 직접 접근

### Status
- **NOT SAFE**

---

## 2) Strategy -> Execution Flow Integrity
### What I verified
- `TraderScheduler._tick` -> `ExecutionService.enter_position` 경로 확인
- 엔진 상태 체크, 심볼 검증, 사이징/리스크 검사, 주문 실행 순서 확인

### Potential risks
- 동시 요청 시(수동 `/trade/enter` + scheduler tick), 동일 시점에 사전검사를 모두 통과할 수 있음
- 사전 상태검사와 실제 주문 사이가 원자적(atomic)이지 않음

### Status
- **NOT SAFE**

---

## 3) Order Idempotency & Duplicate Protection
### What I verified
- `newClientOrderId` 생성 및 길이 제한(<=36)
- `order_records.client_order_id` UNIQUE 제약
- LIMIT 타임아웃 시 CID 조회 후 재전송 여부 판단 로직 확인

### Potential risks
- `apps/trader_engine/services/execution_service.py` / `enter_position`:
  - 오픈오더 확인 실패 시 `except: pass`로 진행(fail-open)
  - 결과: 기존 미체결 주문을 못 읽은 상태에서 신규 진입 주문 전송 가능
- 동시성 경합 시 서로 다른 CID로 동시에 신규 진입 가능(UNIQUE는 동일 CID만 방지)
- MARKET 직접 진입 경로는 LIMIT 경로 대비 타임아웃 재조회 방어가 약함

### Status
- **NOT SAFE**

---

## 4) Risk Management Enforcement
### What I verified
- 주문 직전 `RiskService.evaluate_pre_trade` 호출 확인
- 일손실/DD/쿨다운/스프레드/노출도/노셔널/1자산 규칙 확인

### Potential risks
- 단위 불일치:
  - `max_exposure_pct`는 모델/사이징에서 ratio(0~1)처럼 사용
  - `RiskService.enforce_constraints`에서는 `/100` 추가 적용
  - 결과: 의도보다 100배 보수적 차단 또는 운영자 설정 해석 혼선
- 다중 포지션일 때 일부 심볼 북티커 조회 실패 시 노출도 합산 누락 가능(`except: continue`)

### Status
- **NOT SAFE** (논리 일관성 결함)

---

## 5) Trailing Stop Logic (PCT & ATR)
### What I verified
- shock 우선, trailing 후순위 실행 순서 확인
- PCT arm/peak/trigger 및 ATR distance clamp 로직 확인

### Potential risks
- `apps/trader_engine/services/watchdog_service.py` / `_sync_trailing_state`:
  - `entry_ts`를 실제 진입시각이 아니라 watchdog 관측시각으로 설정
  - 재시작 후 기존 포지션도 grace가 재적용되어 보호 지연 가능
- 재시작 시 peak/armed 상태 초기화로 이미 발생한 수익 구간 보호 약화 가능

### Status
- **SAFE WITH FIXES REQUIRED**

---

## 6) Panic / Emergency Logic
### What I verified
- PANIC 진입 후 강제 취소/청산(best-effort) 경로 확인
- panic 2회 호출 멱등 테스트 존재 확인

### Potential risks
- `apps/trader_engine/api/routes.py` / `/panic`:
  - `exe.panic()`의 cleanup 결과를 API 응답에 반영하지 않음
  - 호출자는 항상 성공처럼 보이는 `EngineStateSchema`만 받음
  - 청산 실패 시 운영자가 평탄화(flat) 완료로 오판 가능
- panic close-all 내부는 예외를 삼키고 `{ok:false}` 반환하는 구조라 외부 강제 대응 유도가 약함

### Status
- **NOT SAFE** (운영 가시성 결함)

---

## 7) Restart & Reconciliation Safety
### What I verified
- startup에서 recovery lock 활성 후 reconcile 실행 확인
- open orders / positions 동기화 및 lock 해제 조건 확인

### Potential risks
- tracked symbols 기반 reconcile이라 추적범위 밖 주문은 정합성 누락 가능
- "오픈오더에 없음"을 바로 `CANCELED/EXPIRED`로 보정하여 실제 FILLED 이력과 불일치 가능
- 다만 recovery lock 자체는 존재하며 실패 시 lock 유지

### Status
- **SAFE WITH FIXES REQUIRED**

---

## 8) WebSocket Lifecycle & Safe Mode
### What I verified
- listenKey create/keepalive/reconnect/backoff 구현 확인
- safe_mode 진입 시 신규 진입 차단(`ws_down_safe_mode`) 확인

### Potential risks
- 연결 상태(`ws_connected=true`)는 유지되지만 이벤트가 정지된 반쯤-죽은 상태에서 safe_mode가 즉시 켜지지 않을 수 있음
- `keepalive` 일시 실패 시 루프 종료 후 즉시 재시도 없이 대기(정책상 보수적 보완 필요)

### Status
- **SAFE WITH FIXES REQUIRED**

---

## 9) Database Transaction Integrity
### What I verified
- 주문기록 생성->전송->상태업데이트 단계 분리 확인
- 마이그레이션/스키마 보강 경로 확인

### Potential risks
- 단일 SQLite connection 공유 + read path 무락 접근
- 복수 단계 업데이트(예: reconcile 대량 보정) 트랜잭션 묶음 부재로 중간상태 노출 가능

### Status
- **NOT SAFE** (고부하/동시성 환경 취약)

---

## 10) Concurrency & Async Race Conditions
### What I verified
- 스케줄러/API/watchdog/user-stream이 동시에 실행될 수 있는 구조 확인
- 실행 서비스 내부에 진입 직렬화 mutex 부재 확인

### Potential risks
- `apps/trader_engine/services/execution_service.py` / `enter_position`:
  - 사전검사(오픈오더/포지션)와 주문전송 사이 경쟁 상태
  - 동시 요청이 서로 존재를 못 본 채 신규 주문 발행 가능
- DB read/write 동시 접근 시 SQLite connection 경쟁 예외 가능

### Status
- **NOT SAFE**

---

## 11) Failure Scenario Simulation
### What I verified
- chaos 테스트(타임아웃+재시작+reconcile) 존재 확인
- panic 멱등, ws disconnect 테스트 존재 확인

### Potential risks
- 테스트는 주요 happy-path/일부 chaos를 커버하나, 실제 동시 진입 경쟁(수동+스케줄러 동시 enter) 시나리오 부족
- fail-open 분기(오픈오더 조회 실패)로 인한 중복 진입 리스크를 차단하는 테스트 부족

### Status
- **SAFE WITH FIXES REQUIRED**

---

## 12) DRY_RUN / TEST_MODE Isolation Safety
### What I verified
- DRY_RUN에서 신규 진입 주문 차단 및 계산/알림 유지 확인
- DRY_RUN_STRICT로 close/panic까지 차단 가능 확인

### Potential risks
- DRY_RUN에서도 one-way 체크/시장조회 실패로 운영자가 혼동할 수 있으나, 실주문 측면 안전성은 높음

### Status
- **SAFE**

---

## 13) Logging & Observability Completeness
### What I verified
- `op_events`, `decisions`, `executions`, `risk_blocks`, `pnl_snapshots` 저장 확인
- 구조화 로그 필드(run_id/cycle_id/intent_id/client_order_id 등) 확인

### Potential risks
- `/panic` 응답에서 cleanup 실패 미노출(관측 공백)
- 일부 핵심 분기의 `except: pass`로 원인 소실 가능
- 평문 콘솔 포맷 사용 시 redaction 일관성이 약함

### Status
- **SAFE WITH FIXES REQUIRED**

---

## 14) Security & Secret Handling
### What I verified
- JSON 로그 formatter의 key 기반 redaction 구현 확인
- 소스 내 명시적 시크릿 출력은 제한적

### Potential risks
- `apps/trader_engine/logging_setup.py`:
  - `exc_info`는 redaction 없이 그대로 기록
  - 외부 라이브러리 예외 메시지에 URL/토큰 포함 시 누출 가능
- `log_json=false` 콘솔 formatter는 redaction 비적용

### Status
- **NOT SAFE**

---

## 15) Production Deployment Risks
### What I verified
- runbook/상태 API/테스트 스위트 존재
- 복구락/세이프모드/트레일링/패닉 등 보호장치 존재

### Potential risks
- 동시성/중복주문/관측공백/비정상 분기 fail-open이 남아 있어 실자본 대규모 투입은 위험

### Status
- **NOT SAFE**

---

## Critical Issues (Must Fix Before Live)
1. **동시 진입 경합으로 중복 주문 가능**
- File: `apps/trader_engine/services/execution_service.py`
- Area: `enter_position` (사전검사 -> 주문 전송 사이)
- Nature: 원자성 부재, 글로벌 진입 락 부재
- Why dangerous: 수동/API/스케줄러 동시 진입 시 동일 심볼 신규 주문 중복 가능
- Break scenario: 사용자가 패널 수동 진입을 누르는 시점과 scheduler tick 진입이 겹침

2. **오픈오더 조회 실패 시 fail-open 진행**
- File: `apps/trader_engine/services/execution_service.py`
- Area: `enter_position` open order guard (`except Exception: pass`)
- Nature: 보호로직 실패 시 차단이 아니라 진행
- Why dangerous: 이미 열린 주문을 못 읽고 신규 진입 추가 발행 가능
- Break scenario: Binance 일시 오류/네트워크 지연으로 openOrders 조회 실패

3. **DB 동시 접근 안전성 미흡(단일 connection 공유 + 무락 read)**
- File: `apps/trader_engine/storage/db.py`, `apps/trader_engine/storage/repositories.py`
- Area: `db.conn.execute(...)` 직접 read 경로
- Nature: 멀티스레드 동시 접근 경쟁
- Why dangerous: 런타임 예외/상태 읽기 불일치/운영 중 비결정적 실패 가능
- Break scenario: scheduler/watchdog/API/threadpool에서 동시 read-write 집중

4. **패닉 API의 청산 실패 은닉**
- File: `apps/trader_engine/api/routes.py`
- Area: `/panic` endpoint
- Nature: cleanup 결과 미반영(항상 상태만 반환)
- Why dangerous: 운영자가 평탄화 완료로 오판하여 추가 손실 가능
- Break scenario: 청산 주문 실패했는데도 API는 성공처럼 응답

5. **노출도 단위 불일치(max_exposure_pct)**
- File: `apps/trader_engine/services/risk_service.py`, `apps/trader_engine/services/sizing_service.py`, `apps/trader_engine/domain/models.py`
- Area: exposure 계산식
- Nature: ratio/percent 혼용
- Why dangerous: 의도치 않은 과차단 또는 설정 오해로 운영 실수
- Break scenario: 운영자가 0.2를 20%로 기대했는데 리스크 가드는 0.2%로 처리

---

## High Risk Issues
1. **WS 연결은 살아있으나 이벤트 정지 상태 탐지 약함**
- File: `apps/trader_engine/services/user_stream_service.py`
- Area: `_health_guard_loop`, safe_mode 전환 기준
- Risk: desync 상태에서 신규 진입이 계속 허용될 수 있음

2. **트레일링 상태 재시작 시 보호 약화**
- File: `apps/trader_engine/services/watchdog_service.py`
- Area: `_sync_trailing_state`
- Risk: 기존 포지션도 grace/peak 초기화로 트레일 보호 공백

3. **예외 스택(redaction 미적용)으로 시크릿 노출 가능성**
- File: `apps/trader_engine/logging_setup.py`
- Area: `JsonFormatter.format`의 `exc_info`
- Risk: webhook/token 포함 예외 메시지 로그 유출 가능

---

## Medium Risk Issues
1. **reconcile 보정 정확도 한계(openOrders만 기준)**
- File: `apps/trader_engine/services/reconcile_service.py`
- Area: `_reconcile_open_orders`
- Risk: 실제 FILLED를 CANCELED로 표기하는 기록 불일치

2. **close/cleanup 경로의 광범위 예외 무시**
- File: `apps/trader_engine/services/execution_service.py`
- Area: cancel/cleanup 다수 `except: pass`
- Risk: 실패 원인 추적 난이도 상승

3. **PnL 실현 계산의 근사치 의존**
- File: `apps/trader_engine/services/execution_service.py`, `apps/trader_engine/services/pnl_service.py`
- Area: wallet delta + 짧은 sleep 기반
- Risk: 리스크 지표(연패/일손실) 왜곡 가능

---

## Low Risk / Improvements
1. 운영 로그/테이블 인덱스 부재로 장기 운영 시 조회 성능 저하 가능
2. keepalive 실패 재시도 정책 세분화(일시 실패 vs 키 무효) 여지
3. 일부 알림 문자열 인코딩/가독성 문제(운영 UX)

---

## Race Condition Findings
1. `ExecutionService.enter_position` 동시 호출 경쟁
- file/area: `apps/trader_engine/services/execution_service.py`
- 증상: 오픈오더/포지션 검사 통과 후 서로 신규 주문 발행 가능

2. SQLite connection 다중 스레드 read/write 경쟁
- file/area: `apps/trader_engine/storage/db.py` + repositories 전반
- 증상: read 무락 접근으로 비결정적 예외 가능

3. watchdog close vs manual close 동시 호출
- file/area: `apps/trader_engine/services/watchdog_service.py` + `execution_service.py`
- 증상: 같은 심볼에 중복 close 시도/불필요 실패 로그 증가

---

## Duplicate Order Risk Analysis
- 강점:
  - CID 강제 + UNIQUE(client_order_id)
  - LIMIT timeout 시 CID 조회 후 재판단
- 취약점:
  - fail-open 분기(openOrders 조회 실패 시 진행)
  - 동시 진입 직렬화 부재(서로 다른 CID로 중복 신규 주문 가능)
- 결론:
  - **중복 주문 위험이 실전에서 여전히 유의미함**

---

## Capital Safety Assessment
- 긍정:
  - budget sizing/캡/필터(minQty/minNotional) 기반 차단 존재
  - ws safe mode, recovery lock 존재
- 부정:
  - 동시성/실패 처리 취약점이 "예산 보호"를 우회할 수 있는 경로를 남김
  - 노출도 단위 혼선으로 운영 설정 오작동 가능
- 종합:
  - **소액 제한 테스트는 가능하나, 현재 상태로 실자본 본배포는 비권장**

---

## Final Recommendation
1. 현재 코드는 **운영 기능은 풍부하지만, 실자본 풀배포 기준 안전성은 미달**입니다.
2. 최소한 아래가 정리되기 전까지는 대규모 라이브 금지:
   - 진입 경로 직렬화(원자성 보장)
   - fail-open 제거(조회 실패 시 차단)
   - panic 결과 가시화(청산 성공/실패 명시)
   - DB 동시성 안전성 강화
   - exposure 단위 일관성 정리
3. 현 시점 배포 권고:
   - **소액 제한 + 강한 수동 모니터링 환경에서만 제한 운영 가능**

