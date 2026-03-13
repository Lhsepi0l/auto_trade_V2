# Shadow Soak Checklist

## 1. 범위
- 기준 프로필은 `ra_2026_alpha_v2_expansion_verified_q070`이다.
- 기준 실행 조합은 `mode=shadow`, `env=testnet`, `control-http-host=127.0.0.1`, `control-http-port=8101`이다.
- 이번 문서는 구조정리 1차 종료본에 대한 운영 검증 준비용이다. 전략/리스크/주문 의미 변경 없이 shadow soak 증거를 남기는 절차만 다룬다.

## 2. 사전 점검

### 2.1 코드/경로 점검
```bash
python -m v2.run --help
python -c "from v2.control import create_control_http_app, build_runtime_controller; print('control_import_ok')"
```

### 2.2 live/prod direct boot guard 확인
아래 두 명령은 `성공`이 아니라 `non-zero 차단`이 정상이다.

```bash
python -m v2.run --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod
python -m v2.run --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --ops-http
```

### 2.3 shadow preflight
```bash
bash v2/scripts/preflight.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet
```

필요하면 full gate도 남긴다.

```bash
bash v2/scripts/preflight.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --test-scope full
```

### 2.4 runtime-preflight
```bash
python -m v2.run \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --runtime-preflight \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101
```

### 2.5 시작 전 상태 확인
아래 세 endpoint는 soak 시작 전에 반드시 기록한다.

```bash
curl -sS http://127.0.0.1:8101/healthz | python -m json.tool
curl -sS http://127.0.0.1:8101/readyz | python -m json.tool
curl -sS http://127.0.0.1:8101/status | python -m json.tool
```

시작 전 필수 확인 field:
- `/status.profile == "ra_2026_alpha_v2_expansion_verified_q070"`
- `/status.mode == "shadow"`
- `/status.env == "testnet"`
- `/status.single_instance_lock_active == true`
- `/status.state_uncertain == false`
- `/status.recovery_required == false`
- `/status.submission_recovery.pending_review_count == 0`
- `/readyz.ready == true`
- `/readyz.private_auth_ok == true` 또는 키 미설정 shadow 점검이면 `/status.binance.usdt_balance.source == "fallback"` 인지 의도적으로 확인
- `/healthz.ready == true`

## 3. shadow soak 시작 절차

### 3.1 control-http 단독 기동
```bash
python -m v2.run \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --control-http \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101
```

### 3.2 full stack 기동
Discord bot까지 같이 보려면 아래를 사용한다.

```bash
bash v2/scripts/run_stack.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --host 127.0.0.1 \
  --port 8101
```

### 3.3 kickoff 증거 저장
```bash
bash v2/scripts/shadow_soak_snapshot.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --label kickoff
```

## 4. 30분 / 1시간 / 6시간 체크 포인트

### 4.1 30분 체크
```bash
bash v2/scripts/shadow_soak_snapshot.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --label t30m
```

확인 항목:
- `/status.engine_state.state`
- `/status.scheduler.running`
- `/status.scheduler.last_action`
- `/status.scheduler.last_decision_reason`
- `/status.scheduler.last_error`
- `/status.health.ready`
- `/status.binance.usdt_balance.source`
- `/status.binance.private_error`
- `/readyz.ready`
- `/readyz.user_ws_stale`
- `/readyz.market_data_stale`
- `/readyz.submission_recovery_ok`

### 4.2 1시간 체크
```bash
bash v2/scripts/shadow_soak_snapshot.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --label t1h
```

추가 확인 항목:
- `/status.state_uncertain == false`
- `/status.recovery_required == false`
- `/status.last_error == null`
- `/status.user_stream.stale == false`
- `/status.market_data.stale == false`
- `/status.live_readiness.ready`와 `/readyz.ready`가 같은 의미로 유지되는지

### 4.3 6시간 체크
```bash
bash v2/scripts/shadow_soak_snapshot.sh \
  --profile ra_2026_alpha_v2_expansion_verified_q070 \
  --mode shadow \
  --env testnet \
  --label t6h
```

추가 확인 항목:
- 6시간 동안 `state_uncertain`, `recovery_required`, `submission_recovery.pending_review_count`가 미해결 상태로 남지 않았는지
- `logs/shadow_soak/.../logs/` 및 `journal_v2-stack.service.log`에서 동일 원인의 stale flap 또는 private error 반복이 없는지
- `/status.runtime_identity.profile/mode/env`가 시작 시점과 같은지
- `/status.binance.usdt_balance.source`가 의도한 운영 형태와 일치하는지

## 5. restart recovery 확인 절차

### 5.1 clean restart 확인
1. 시작 전 snapshot 저장
```bash
bash v2/scripts/shadow_soak_snapshot.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet --label pre_clean_restart
```
2. `POST /stop`
```bash
curl -sS -X POST http://127.0.0.1:8101/stop | python -m json.tool
```
3. 프로세스를 정상 종료한다.
4. 같은 명령으로 재기동한다.
5. 재기동 직후 아래를 확인한다.
- `/status.last_shutdown_marker.clean_shutdown == true`
- `/status.last_shutdown_marker.profile == "ra_2026_alpha_v2_expansion_verified_q070"`
- `/readyz.ready == true`

### 5.2 dirty restart -> reconcile 복구 확인
1. snapshot 저장
```bash
bash v2/scripts/shadow_soak_snapshot.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet --label pre_dirty_restart
```
2. 별도 셸에서 control-http 프로세스를 강제 종료한다.
3. 같은 명령으로 다시 기동한다.
4. 재기동 직후 아래를 확인한다.
- `/readyz.ready == false`
- `/readyz.recovery_required == true`
5. 수동 복구를 실행한다.
```bash
curl -sS -X POST http://127.0.0.1:8101/reconcile | python -m json.tool
curl -sS http://127.0.0.1:8101/readyz | python -m json.tool
```
6. 복구 후 아래를 확인한다.
- `/readyz.ready == true`
- `/readyz.recovery_required == false`
- `/status.state_uncertain == false`

## 6. control 명령 확인 순서
아래 순서는 shadow soak 중 control surface가 깨지지 않았는지 보는 최소 순서다.

```bash
curl -sS -X POST http://127.0.0.1:8101/start | python -m json.tool
curl -sS -X POST http://127.0.0.1:8101/scheduler/tick | python -m json.tool
curl -sS -X POST http://127.0.0.1:8101/cooldown/clear | python -m json.tool
curl -sS -X POST http://127.0.0.1:8101/report | python -m json.tool
curl -sS -X POST http://127.0.0.1:8101/stop | python -m json.tool
```

`panic`은 soak 종료 직전, 별도 증거 snapshot을 남긴 뒤 한 번만 검증한다.

```bash
curl -sS -X POST http://127.0.0.1:8101/panic | python -m json.tool
```

확인 포인트:
- `POST /start` 응답의 `state`
- `POST /scheduler/tick` 응답의 `ok`, `snapshot.last_action`, `snapshot.last_error`
- `POST /cooldown/clear` 이후 `lose_streak == 0`
- `POST /stop` 이후 `/status.engine_state.state` 반영
- `POST /panic` 이후 `engine_state.state == "KILLED"`

## 7. 실패 조건 / 즉시 중단 조건
- `/status.state_uncertain == true`가 수동 `POST /reconcile` 후에도 반복된다.
- `/status.recovery_required == true`가 dirty restart 복구 절차 후에도 남는다.
- `/readyz.user_ws_stale == true` 또는 `/readyz.market_data_stale == true`가 2회 연속 snapshot에서 유지된다.
- `/readyz.private_auth_ok == false`가 의도하지 않은 shadow key 점검 경로에서 반복된다.
- `/status.binance.private_error`가 `balance_auth_failed`, `balance_rate_limited`, `balance_fetch_timeout`, `balance_fetch_failed`, `balance_payload_invalid`, `usdt_asset_missing`로 반복된다.
- `/status.submission_recovery.pending_review_count > 0`가 해소되지 않는다.
- `/status.engine_state.state`와 control 응답 state가 일치하지 않는다.
- 로그에 동일 원인의 `stale_transition`, `runtime_boot`, `flatten_requested`, `reconcile_success`가 비정상 반복으로 남는다.

## 8. canary 전환 조건
- shadow soak `6시간 이상`
- clean restart 1회 통과
- dirty restart -> reconcile -> ready 복구 1회 통과
- `/readyz.ready == true`
- `/status.state_uncertain == false`
- `/status.recovery_required == false`
- `/status.submission_recovery.pending_review_count == 0`
- `/readyz.user_ws_stale == false`
- `/readyz.market_data_stale == false`
- `/status.health.private_auth_ok == true` 또는 무키 shadow 점검이면 fallback 의도와 증거가 문서화되어 있을 것
- 최신 snapshot bundle, preflight 결과, soak 중간 snapshot이 모두 보존되어 있을 것

## 9. 남겨야 하는 증거 목록
- `logs/shadow_soak/<timestamp>_<label>/control/status.json`
- `logs/shadow_soak/<timestamp>_<label>/control/healthz.json`
- `logs/shadow_soak/<timestamp>_<label>/control/readyz.json`
- `logs/shadow_soak/<timestamp>_<label>/meta/runtime_context.txt`
- `logs/shadow_soak/<timestamp>_<label>/git/head.txt`
- `logs/shadow_soak/<timestamp>_<label>/git/status.txt`
- `logs/shadow_soak/<timestamp>_<label>/system/processes.txt`
- `logs/shadow_soak/<timestamp>_<label>/system/sockets.txt`
- `logs/shadow_soak/<timestamp>_<label>/logs/*.tail.log`
- `logs/shadow_soak/<timestamp>_<label>/system/journal_v2-stack.service.log`

### 9.1 종료 후 번들 묶기
```bash
bash v2/scripts/shadow_soak_bundle.sh
```

또는 특정 snapshot 디렉토리를 직접 지정한다.

```bash
bash v2/scripts/shadow_soak_bundle.sh --source-dir logs/shadow_soak/<timestamp>_<label>
```
