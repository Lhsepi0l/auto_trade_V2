# Shadow Rehearsal Runbook

## 1. 목적
- 대상 전략 후보는 `ra_2026_alpha_v2_expansion_verified_q070` 고정이다.
- 본 문서는 `mode=shadow`, `env=testnet` 기준 운영 리허설 절차만 다룬다.
- 목표는 `실거래 시작`이 아니라 `운영 상태 전이`, `복구 절차`, `canary 직전 게이트`를 검증하는 것이다.

## 2. 표준 실행 명령

### 2.1 shadow preflight
```bash
bash v2/scripts/preflight.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet
```

### 2.2 runtime preflight
```bash
python -m v2.run \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --runtime-preflight \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101
```

### 2.3 shadow control runtime
```bash
python -m v2.run \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --control-http \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101
```

### 2.4 기동 직후 점검 명령
```bash
curl -s http://127.0.0.1:8101/healthz
curl -s http://127.0.0.1:8101/readyz
curl -s http://127.0.0.1:8101/status
curl -s -X POST http://127.0.0.1:8101/start
curl -s -X POST http://127.0.0.1:8101/scheduler/tick
```

### 2.5 로그 위치
- `v2/logs/control_runtime.log`
- `v2/logs/runtime_preflight.log`
- systemd 경로 사용 시 `journalctl -u v2-stack.service`

## 3. shadow 시작 전 체크리스트
- `profile=ra_2026_alpha_v2_expansion_verified_q070`인지 다시 확인한다.
- `mode=shadow`, `env=testnet`, `control-http-host=127.0.0.1`인지 확인한다.
- `preflight`와 `runtime-preflight`가 모두 통과했는지 확인한다.
- startup banner에 `live_trading_enabled=false`가 보이는지 확인한다.
- `/readyz.ready=true`, `/status.state_uncertain=false`, `/status.recovery_required=false`인지 확인한다.
- `/status.submission_recovery.pending_review_count=0`인지 확인한다.
- shadow에서도 testnet 키를 붙일 계획이면 `/status.binance.private_error`가 비어 있는지 확인한다.

## 4. shadow smoke 절차
1. `runtime-preflight`를 먼저 돌린다.
2. shadow control runtime를 띄운다.
3. `/healthz`, `/readyz`, `/status`를 확인한다.
4. `POST /start`로 scheduler를 시작한다.
5. `POST /scheduler/tick` 한 번으로 제어 경로가 정상인지 확인한다.
6. 기대값:
   - HTTP 200
   - `/readyz.ready=true`
   - `/status.single_instance_lock_active=true`
   - `/status.last_error=null`
   - `tick` 결과는 `no_candidate` 또는 `missing_market`이어도 무방하다. 핵심은 `execution_failed`가 아니고 `last_error=null`인 것이다.

## 5. failure-injection / recovery rehearsal

### 5.1 stale market data
- 재현:
  - `python -m pytest -q v2/tests/test_control_api.py -k live_market_data_stale_blocks_new_entries_and_unblocks_when_fresh`
- 기대 반응:
  - 신규 진입이 `market_data_stale`로 차단된다.
  - fresh 상태 복구 후 unblock 된다.
- 실패 의미:
  - stale 상황에서도 blind trading이 발생할 수 있다.
- 운영자가 볼 것:
  - `/readyz.market_data_stale`
  - `/status.market_data.stale`
  - `control_runtime.log`의 `stale_transition`

### 5.2 stale user/private stream
- 재현:
  - `python -m pytest -q v2/tests/test_control_api.py -k live_user_stream_stale_blocks_new_entries_and_private_ok_unblocks`
- 기대 반응:
  - 신규 진입이 `user_ws_stale`로 차단된다.
  - private-ok 이후 unblock 된다.
- 실패 의미:
  - private freshness 상실 중에도 신규 진입이 열릴 수 있다.
- 운영자가 볼 것:
  - `/readyz.user_ws_stale`
  - `/status.user_stream.stale`
  - `control_runtime.log`의 `stale_transition`

### 5.3 dirty restart
- 재현:
```bash
python -m v2.run ... --control-http --control-http-port 8101
# 별도 셸에서 PID 확인 후 강제 종료
pkill -9 -f "control-http-port 8101"
# 다시 같은 명령으로 기동
curl -s http://127.0.0.1:8101/readyz
```
- 기대 반응:
  - 재기동 직후 `recovery_required=true`, `ready=false`
- 실패 의미:
  - dirty restart 뒤 unsafe resume 가능성이 있다.
- 운영자가 볼 것:
  - `/readyz.recovery_required`
  - `/status.last_shutdown_marker`
  - `control_runtime.log`의 `runtime_boot`

### 5.4 manual reconcile
- 재현:
```bash
curl -s -X POST http://127.0.0.1:8101/reconcile
curl -s http://127.0.0.1:8101/readyz
```
- 기대 반응:
  - reconcile 성공 시 `state_uncertain=false`, `recovery_required=false`
- 실패 의미:
  - 수동 복구 경로가 막혀 있다.
- 운영자가 볼 것:
  - `/readyz`
  - `/status.boot_recovery`
  - `control_runtime.log`의 `reconcile_start`, `reconcile_success`

### 5.5 recovery_required -> ready 복구
- 재현:
  - `python -m pytest -q v2/tests/test_control_api.py -k 'readyz_and_healthz_reflect_uncertainty_stale_and_recovery or manual_recovery_runs_bracket_recovery_path'`
- 기대 반응:
  - dirty restart 상태에서 `/readyz` fail
  - manual reconcile 뒤 `/readyz` pass
- 실패 의미:
  - recovery gate가 풀리지 않거나, 반대로 너무 일찍 열린다.
- 운영자가 볼 것:
  - `/readyz.ready`
  - `/status.boot_recovery.bracket_recovery`

### 5.6 submit ambiguous / REVIEW_REQUIRED
- 재현:
  - `python -m pytest -q v2/tests/test_live_execution_service.py -k 'submit_timeout'`
  - `python -m pytest -q v2/tests/test_control_api.py -k submit_review_required_blocks_until_manual_reconcile_resolves`
- 기대 반응:
  - found면 `SUBMITTED`로 복구
  - not found면 `REVIEW_REQUIRED`
  - `REVIEW_REQUIRED` 상태에서는 신규 진입 차단
- 실패 의미:
  - submit timeout 뒤 중복 제출 또는 무증거 재시도가 발생할 수 있다.
- 운영자가 볼 것:
  - `/status.submission_recovery`
  - `/readyz.submission_recovery_ok`
  - `control_runtime.log`의 `order_intent_recovered`, `order_intent_review_required`

### 5.7 blocked 상태에서도 flatten / recovery 허용
- 재현:
  - `python -m pytest -q v2/tests/test_control_api.py -k 'live_stale_state_still_allows_flatten_and_reconcile or live_uncertainty_still_allows_flatten_reduce_only_recovery'`
- 기대 반응:
  - 신규 진입은 막히지만 `flatten`, `reconcile`은 허용된다.
- 실패 의미:
  - blocked 상태에서 risk-reduction action까지 막혀 사고 대응이 불가능해진다.
- 운영자가 볼 것:
  - `POST /trade/close_all`
  - `POST /reconcile`
  - `/status.state_uncertain`, `/status.recovery_required`

## 6. operator runbook

### 6.1 정상 기동 절차
- `preflight` 실행
- `runtime-preflight` 실행
- shadow control runtime 기동
- `/healthz` -> `/readyz` -> `/status` 순서로 확인
- `POST /start`
- `POST /scheduler/tick` 1회로 제어 경로 확인

### 6.2 `/readyz=false`일 때 확인 순서
- `recovery_required`
- `state_uncertain`
- `submission_recovery_ok`
- `user_ws_stale`
- `market_data_stale`
- `single_instance_ok`
- `private_auth_ok`

### 6.3 `state_uncertain=true`일 때
- `/status.state_uncertain_reason` 확인
- `POST /reconcile`
- 필요 시 `POST /trade/close_all`
- 복구 안 되면 clean restart

### 6.4 `user_ws_stale` 또는 `market_data_stale`일 때
- 로그에서 `stale_transition` 확인
- 먼저 `POST /reconcile`
- stale가 해소되지 않으면 restart
- stale 상태에서 신규 진입 강행 금지

### 6.5 `recovery_required=true`일 때
- dirty restart로 간주
- `POST /reconcile`
- `/readyz.ready=true`가 되기 전까지 `POST /start` 재시도 금지

### 6.6 `REVIEW_REQUIRED`일 때
- `/status.submission_recovery.pending_review` 확인
- `POST /reconcile`
- pending이 사라지지 않으면 추가 신규 진입 금지
- 관련 로그와 거래소 주문 상태를 먼저 대조

### 6.7 flatten / reconcile / restart 순서
1. `POST /trade/close_all`
2. `POST /reconcile`
3. 상태 미복구 시 restart

### 6.8 절대 하면 안 되는 것
- `0.0.0.0`로 control HTTP 노출
- `ready=false` 상태에서 신규 진입 허용 판단
- `REVIEW_REQUIRED`를 무시하고 재제출
- shadow와 live 로그를 섞어서 판단
- 원인 확인 없이 반복 restart만 수행

### 6.9 shadow/live 혼선 방지 포인트
- startup banner의 `live_trading_enabled`
- `/status.profile`, `/status.mode`, `/status.env`
- `/status.runtime_identity.surface_label`

## 7. pre-canary gate

### 7.1 최소 통과 조건
- shadow 연속 가동 `6시간 이상`
- 수동 또는 자동 scheduler cycle `20회 이상`
- clean restart rehearsal `1회` 통과
- dirty restart -> reconcile -> ready 복구 rehearsal `1회` 통과
- `runtime-preflight` 최신 실행 결과 `ok=true`

### 7.2 반드시 0건이어야 하는 것
- 미해결 `state_uncertain`
- 미해결 `recovery_required`
- 미해결 `REVIEW_REQUIRED`
- `single_instance_ok=false`
- `last_error`에 execution failure class 반복 발생

### 7.3 허용 가능한 경고
- `mode=shadow`
- `live_readiness.checks.mode=warn`
- shadow 무키 점검 시 `exchange_private=fallback`

### 7.4 canary 직전 필수 확인
- `/readyz.ready=true`
- `state_uncertain=false`
- `recovery_required=false`
- `submission_recovery.pending_review_count=0`
- `user_ws_stale=false`
- `market_data_stale=false`
- `runtime-preflight ok=true`
- startup banner와 `/status` 모두 `profile / mode / env` 일치
- testnet/prod용 private auth가 의도한 환경에서 정상
- 로그에 최근 `runtime_boot`, `ready_transition`, `stale_transition(false)` 외 치명 이벤트 없음

### 7.5 canary 진입 불가 조건
- shadow 6시간 검증 미완료
- dirty restart 복구 rehearsal 미통과
- `REVIEW_REQUIRED` 해소 실패
- stale/uncertain 상태 반복
- env/profile 혼선 흔적 존재
- private auth가 의도한 테스트 환경에서 검증되지 않음
