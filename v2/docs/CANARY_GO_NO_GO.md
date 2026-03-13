# Canary Go / No-Go

## 1. 판단 입력 증거
- 최신 shadow kickoff / 30분 / 1시간 / 6시간 snapshot
- clean restart snapshot
- dirty restart -> reconcile 복구 snapshot
- `preflight.sh` 결과
- 필요 시 `run_stack.sh` 또는 `journalctl -u v2-stack.service` tail

## 2. Go 조건
- `/readyz.ready == true`
- `/readyz.single_instance_ok == true`
- `/readyz.state_uncertain == false`
- `/readyz.recovery_required == false`
- `/readyz.submission_recovery_ok == true`
- `/readyz.bracket_recovery_ok == true`
- `/readyz.user_ws_stale == false`
- `/readyz.market_data_stale == false`
- `/readyz.private_auth_ok == true` 또는 shadow 무키 점검이면 `/status.binance.usdt_balance.source == "fallback"`가 의도된 결과로 증거에 남아 있을 것
- `/status.profile == "ra_2026_alpha_v2_expansion_verified_q070"`
- `/status.mode == "shadow"`
- `/status.env == "testnet"`
- `/status.single_instance_lock_active == true`
- `/status.state_uncertain == false`
- `/status.recovery_required == false`
- `/status.submission_recovery.pending_review_count == 0`
- `/status.scheduler.last_error == null`
- `/status.health.ready == true`
- clean restart 1회 통과
- dirty restart -> `POST /reconcile` -> `/readyz.ready=true` 복구 1회 통과
- 6시간 shadow soak 증거가 남아 있을 것

## 3. No-Go 조건
- `/readyz.ready == false`
- `/readyz.state_uncertain == true`
- `/readyz.recovery_required == true`
- `/readyz.submission_recovery_ok == false`
- `/readyz.bracket_recovery_ok == false`
- `/readyz.user_ws_stale == true` 또는 `/readyz.market_data_stale == true`가 반복된다.
- `/readyz.private_auth_ok == false`가 의도하지 않은 shadow 키 검증에서도 반복된다.
- `/status.binance.private_error`가 `balance_auth_failed`, `balance_rate_limited`, `balance_fetch_timeout`, `balance_fetch_failed`, `balance_payload_invalid`, `usdt_asset_missing`로 반복된다.
- `/status.binance.usdt_balance.source`가 기대와 다르게 계속 `fallback` 또는 `exchange_cached`에 머문다.
- `/status.submission_recovery.pending_review_count > 0`
- `/status.last_error` 또는 `/status.scheduler.last_error`에 execution failure class가 반복된다.
- `/status.last_shutdown_marker`와 현재 `profile/mode/env`가 맞지 않는다.
- clean restart 또는 dirty restart recovery rehearsal 중 하나라도 실패한다.
- 로그에 동일 원인의 `stale_transition`, `runtime_boot`, `flatten_requested`, `reconcile_success` 비정상 반복이 있다.

## 4. canary 직전 마지막 판정 질문
1. 가장 최근 `readyz.json`이 `ready=true`인가
2. 가장 최근 `status.json`이 `state_uncertain=false`, `recovery_required=false`, `submission_recovery.pending_review_count=0`인가
3. restart recovery 증거가 둘 다 있는가
4. fallback/private error 상황이 운영자가 의도한 shadow 구성과 일치하는가
5. 6시간 shadow soak bundle이 남아 있는가

위 5개 중 하나라도 `아니오`면 `No-Go`다.
