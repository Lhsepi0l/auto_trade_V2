# 운영 하드닝 진단 및 로드맵

## 기준

- 전략 연구는 종료한다.
- 현재 전략 champion candidate는 `EXP-06 @ quality_score_v2_min=0.70`으로 유지한다.
- 이번 문서는 전략 튜닝이 아니라 `24시간 자동매매 운영 안정성`만 다룬다.

## 운영 병목 진단

### 최우선 위험

1. `startup reconciliation / user stream`이 코드에 존재하지만 런타임에 실제로 연결되어 있지 않다.
   - `EngineStateStore.startup_reconcile(...)`, `apply_exchange_event(...)`, `UserStreamManager` 구현은 존재한다.
   - 하지만 `v2.run`의 `_build_runtime(...)`, `_serve_control_http(...)`, `_boot(...)` 어디에서도 `create_user_stream_manager(...)`를 호출하지 않는다.
   - 결과적으로 재부팅/프로세스 재시작 뒤 로컬 상태와 거래소 상태를 자동으로 맞추는 경로가 비어 있다.
   - user stream 단절이나 WS 유실을 감지해 신규 진입을 차단하는 경로도 없다.

2. `single-instance execution` 보장이 없다.
   - `run_stack.sh`는 `stack.pids`를 쓰지만 기존 PID 생존 여부를 검사하지 않는다.
   - systemd 한 유닛만 쓰면 대체로 괜찮지만, 수동 실행이나 중복 실행 시 duplicate runtime 가능성이 남아 있다.

3. `idempotent order submission`이 없다.
   - live submit은 `generate_client_order_id(...)`로 랜덤 `newClientOrderId`를 붙여 `/fapi/v1/order`를 호출한다.
   - 그러나 같은 거래 의도를 재시도/재기동 후 다시 보내지 않게 막는 `intent_id -> client_order_id` 영속 매핑이 없다.
   - 즉 네트워크 타임아웃, 응답 유실, 프로세스 재시작 상황에서는 “이미 제출된 주문을 모른 채 다시 제출”할 여지가 남는다.

4. `restart recovery after crash or server reboot`가 반쪽이다.
   - 현재 부팅 시 복구는 `BracketService.recover()` 정도만 있다.
   - 엔진 상태 전체에 대한 startup reconcile, uncertainty gate, auto-safe bootstrap이 없다.
   - 게다가 control API는 부팅 시 `STOPPED`에서 시작하므로 systemd 재기동만으로 자동매매가 자동 복귀하지 않는다.

5. `health / readiness / deployment gate`가 현재 live candidate 기준으로 완결되지 않았다.
   - `/readiness`는 일부 구형 프로필만 기준 spec를 갖고 있고, 현재 alpha live candidate에 대한 운영 spec가 없다.
   - `/status`는 풍부하지만 `state uncertainty`, `last_reconcile_at age`, `ws freshness` 같은 핵심 운영 메트릭이 없다.

### 중간 위험

6. `state uncertainty 시 신규 진입 차단`이 없다.
   - positions/balance fetch 실패 시 fallback/cached balance는 잘 처리하지만, 그 자체로 신규 진입을 막지는 않는다.
   - WS 없음, reconcile 없음, stale market context 여부도 진입 게이트에 반영되지 않는다.

7. `websocket reconnect / stale-data protection`은 구현 대비 운영 연결이 없다.
   - `UserStreamManager`는 reconnect/resync/reorder를 지원한다.
   - 반면 `BinanceMarketWS`는 reconnect wrapper도 없고, 런타임 메인 경로에 연결되지도 않는다.

8. `structured logging`은 부분적이다.
   - 공용 JSON logger는 존재하지만, 런타임 진입점에서 일관되게 초기화하지 않는다.
   - 따라서 order intent, reconcile, boot gate, uncertainty transition 같은 운영 핵심 이벤트를 구조화해 남기기 어렵다.

### 상대적으로 양호

9. `cooldown / safe mode / flatten` 계열은 이미 어느 정도 있다.
   - lose streak, daily loss, dd 기반 cooldown과 auto-safe/auto-flatten 훅이 존재한다.
   - `flatten` verification sequence도 구현돼 있다.

10. `paper/live separation`과 기본 config validation도 어느 정도 있다.
   - `shadow`는 키 없이 동작하고, `live`는 키가 없으면 시작이 막힌다.
   - Pydantic 기반 config schema validation도 있다.

## Phase A

### A1. Startup reconcile + uncertainty gate
- 막는 실패 모드:
  - 재부팅 후 거래소에는 포지션/오더가 있는데 로컬은 빈 상태로 새 진입
  - WS 미연결/초기 reconcile 실패 상태에서 blind trading
- 우선순위:
  - 가장 높음. 실거래 계정 파손 위험이 직접적이다.
- 최소 구현 단위:
  - control/runtime boot 시 `startup_reconcile()` 1회 수행
  - 실패 시 `state_uncertain=true`, `ops.safe_mode()` 또는 최소 `new entries blocked`
  - `/status`에 `state_uncertain`, `last_reconcile_at`, `startup_reconcile_ok` 추가
- 검증:
  - startup reconcile 성공/실패 유닛테스트
  - “exchange has open position, local empty” 부팅 시 새 진입 차단 테스트

### A2. User stream wiring + resync on reconnect
- 막는 실패 모드:
  - fill/order update 유실, position drift, stale local state
- 우선순위:
  - 매우 높음. A1과 함께 state SSOT를 완성하는 축이다.
- 최소 구현 단위:
  - live mode에서 `create_user_stream_manager()` 실제 연결
  - `on_resync -> startup_reconcile/apply_reconciliation`
  - `on_event -> apply_exchange_event`
  - stream disconnected / resync failed 시 `state_uncertain=true`
- 검증:
  - fake WS로 reconnect/resync 후 state 반영 테스트
  - listenKey expiry 후 resync 성공/실패에 따른 entry block 테스트

### A3. Single-instance lock
- 막는 실패 모드:
  - 중복 프로세스, duplicate order, 서로 다른 risk/runtime state 경쟁
- 우선순위:
  - 높음. 운영사고의 원인이 너무 단순하고 치명적이다.
- 최소 구현 단위:
  - `run_stack.sh` 또는 control runtime에 파일 락(`flock`) 추가
  - 이미 실행 중이면 즉시 종료 + 명확한 오류 출력
- 검증:
  - 첫 실행 성공, 두 번째 실행 실패 테스트

### A4. Submit idempotency shell
- 막는 실패 모드:
  - 요청 타임아웃/응답 유실 후 같은 의도 재제출
- 우선순위:
  - 높음. live order path의 재시도/장애 상황에서 핵심이다.
- 최소 구현 단위:
  - `intent_id` 생성 및 저장
  - `intent_id -> client_order_id -> submitted_at/status` 저장소 추가
  - 같은 intent 재실행 시 신규 submit 대신 기존 제출 상태 조회
- 검증:
  - 같은 intent 두 번 호출 시 REST submit 1회만 발생하는 테스트

## Phase B

### B1. Restart recovery policy
- 막는 실패 모드:
  - 프로세스 crash 후 사람이 모를 때 unsafe resume
- 최소 구현 단위:
  - boot reason/state 기록
  - `uncertain recovery required` 상태에서 `/start` 막기 또는 강제 safe_mode
  - 수동 승인 후 resume
- 검증:
  - dirty shutdown 후 restart 시 blocked 상태 확인

### B2. Websocket freshness / stale-data gate
- 막는 실패 모드:
  - 오래된 market/user data 기반 신규 진입
- 최소 구현 단위:
  - `last_user_ws_event_at`, `last_market_data_at` 추적
  - freshness 임계치 초과 시 신규 진입 차단
- 검증:
  - stale timestamp 주입 시 `risk_rejected` 또는 `blocked` 확인

### B3. Health / readiness 확장
- 막는 실패 모드:
  - 운영자는 ready라고 보지만 실제론 uncertainty/stale 상태
- 최소 구현 단위:
  - `/healthz`, `/readyz`, `/status` 분리
  - current alpha live candidate용 readiness spec 추가
  - 핵심 체크: single-instance, reconcile age, ws freshness, private auth, paused/safe_mode
- 검증:
  - 각 failure case별 endpoint 상태 코드/JSON 테스트

### B4. Structured runtime logging
- 막는 실패 모드:
  - 사고 후 root cause 불명확
- 최소 구현 단위:
  - runtime entrypoint에서 `setup_logging(...)` 호출
  - boot/reconcile/order_intent/uncertainty/flatten/trip 이벤트를 JSON 로그로 통일
- 검증:
  - 주요 경로 로그 필드 존재 테스트

## Phase C

### C1. Graceful shutdown with persistence
- 막는 실패 모드:
  - SIGTERM 중간 종료 후 상태 일부 유실
- 최소 구현 단위:
  - stop hook에서 running 플래그 정리, 마지막 reconcile timestamp 및 runtime marker 저장
  - 가능하면 user stream orderly stop
- 검증:
  - shutdown signal 후 상태 저장/다음 부팅 recovery 경로 테스트

### C2. Deployment gate checklist automation
- 막는 실패 모드:
  - 실계정 기동 전에 필수 안전 체크 누락
- 최소 구현 단위:
  - `deploy-prep`에 live-candidate readiness spec, instance lock, env separation, localhost bind, uncertainty clean boot 확인 추가
- 검증:
  - good/bad config fixture 기반 preflight 결과 테스트

### C3. Paper/live separation 강화
- 막는 실패 모드:
  - testnet/prod 혼선, shadow/live 혼선
- 최소 구현 단위:
  - startup banner에 `profile/mode/env/live-trading-enabled` 강제 출력
  - Discord/status에도 동일 표시
- 검증:
  - startup smoke + status snapshot 테스트

## Phase A 우선 구현안

1. `boot_reconcile_guard`
   - boot 직후 거래소 snapshot을 가져와 `startup_reconcile()`
   - 실패 시 `safe_mode + state_uncertain`
   - 성공 시 `last_reconcile_at`, `startup_reconcile_ok=true`

2. `live_user_stream_runtime`
   - live 모드에서 user stream 시작
   - `on_event`/`on_resync`를 state store에 연결
   - disconnect/resync failure는 uncertainty로 승격

3. `instance_lock`
   - `run_stack.sh`에 `flock` 기반 단일 실행 락 추가
   - control API 단독 실행 경로에도 동일한 보호 고려

4. `order_intent_registry`
   - 가장 작은 범위로는 신규 table 하나와 live submit wrapper 하나면 충분하다
   - 현재는 strategy logic을 건드리지 않고 execution service 앞단에서 intent registry를 붙인다

## 검증 계획

1. 유닛 테스트
   - startup reconcile 성공/실패
   - uncertainty 시 신규 진입 차단
   - duplicate runtime 실행 차단
   - 같은 intent 재호출 시 submit 1회

2. 통합 테스트
   - 재시작 후 exchange snapshot 반영
   - user stream reconnect + resync
   - stale/uncertain 상태에서 `/readiness` blocked

3. 운영 리허설
   - shadow 24h 연속 실행
   - 강제 kill 후 재기동
   - network drop 시뮬레이션 후 user stream 복구
   - `/status`, `/risk`, `/readiness`가 uncertainty/freshness를 정확히 반영하는지 확인

## 최종 verdict

- `hardening 진행 필요`
- 이유:
  - 현재 코드는 `flatten`, `cooldown`, `risk persistence`, `status`는 꽤 갖췄다.
  - 하지만 `startup reconciliation`, `user stream wiring`, `state uncertainty gate`, `single-instance lock`, `submit idempotency`가 24시간 실거래 기준으로 아직 비어 있다.
  - 따라서 지금은 전략 추가가 아니라 `Phase A` 운영 하드닝이 최우선이다.
