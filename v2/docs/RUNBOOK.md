# V2 Runbook

## 1. Scope and safety policy
- 운영 대상은 `v2/` 런타임입니다.
- `v2/config/config.yaml`만 행동 설정입니다.
- `.env`는 키/시크릿 보관용만 사용합니다.
- 실전(API 실거래) 전에는 테스트넷에서 충분히 점검해야 합니다.
- 실계정 키를 저장소에 넣거나, 로그/리포트에 키를 기록하지 않습니다.

## 2. 빠른 시작

### 기본 실행
```bash
# 테스트넷 Shadow (키 불필요)
python -m v2.run --profile normal --mode shadow --env testnet

# 운영 Prod Live (BINANCE_API_KEY, BINANCE_API_SECRET 필요)
python -m v2.run --profile normal --mode live --env prod

# 배포 준비(원커맨드: preflight + runtime smoke)
python -m v2.run --deploy-prep --profile normal --mode shadow --env testnet --keep-reports 30
```

### 종료
- 수동 중지는 `Ctrl+C`로 프로세스를 종료합니다.
- 현재 구현은 백그라운드 데몬 제어 API가 없고, 종료 시점에 상태가 즉시 저장됩니다.
- 재시작은 위의 시작 명령을 다시 실행합니다.

### 옵션
- `--ops-action` 사용 시에는 즉시 종료되는 단일 동작 CLI 입니다.
- `--ops-http`를 함께 사용하면 오퍼레이터 API 서버를 띄워서 HTTP로 제어합니다.
- `--control-http`를 사용하면 Discord 패널 호환 제어 API(`http://<host>:8101`)를 띄웁니다.
- `--report-dir`, `--report-path`는 리플레이 보고서 생성에만 사용합니다.

### Discord Bot (v2 패키지)
```bash
# 1) v2 제어 API 실행 (Discord 패널 호환)
python -m v2.run --mode live --env prod --env-file .env --control-http --control-http-host 127.0.0.1 --control-http-port 8101

# 2) Discord bot 실행 (같은 .env 사용)
python -m v2.discord_bot.bot
```

### 통합 실행(원커맨드)
```bash
# control API + Discord bot 동시 실행
bash v2/scripts/run_stack.sh --mode live --env prod --env-file .env --host 0.0.0.0 --port 8101
```

- 한 프로세스라도 종료되면 나머지를 정리하고 함께 종료합니다.
- 로그 파일: `v2/logs/control_api.log`, `v2/logs/discord_bot.log`
- 기본 `TRADER_API_BASE_URL`은 `http://127.0.0.1:<port>`로 자동 설정됩니다.

### systemd 서비스(자동 재시작/부팅 자동기동)
```bash
# dry-run으로 유닛 내용 확인
bash v2/scripts/install_systemd_stack.sh --dry-run --user bot --workdir /home/bot/autotrade/auto_trade_V2

# 실제 설치/기동
bash v2/scripts/install_systemd_stack.sh --user bot --workdir /home/bot/autotrade/auto_trade_V2 --mode live --env prod --env-file .env --host 0.0.0.0 --port 8101

# 상태/로그 확인
sudo systemctl status v2-stack.service --no-pager
sudo journalctl -u v2-stack.service -f
```

- 템플릿 유닛 파일은 `v2/systemd/v2-stack.service`에 포함되어 있습니다.
- 설치 스크립트는 `/etc/systemd/system/v2-stack.service`를 생성하고 `enable --now`까지 수행합니다.

- `TRADER_API_BASE_URL` 기본값은 `http://127.0.0.1:8101` 입니다.
- Discord 토큰은 `DISCORD_BOT_TOKEN`(또는 하위호환 `DISCORD_TOKEN`)을 사용합니다.

## 3. 구성 구성요소
- 실행 진입점: `v2/run.py`
- 설정 로딩: `v2/config/loader.py`
- 실행/상태 저장: `v2/engine/`
- 거래소 인터페이스: `v2/exchange/`
- TP/SL 브래킷: `v2/tpsl/`
- OPS 컨트롤러: `v2/ops/`

## 4. 키 교체(회전)
1. 새 `BINANCE_API_KEY`, `BINANCE_API_SECRET`를 발급/회수합니다.
2. 기존 `env` 값 교체 후 새 값으로 재시작합니다.
3. 새 키가 테스트넷 shadow/live 가동 중인지 확인합니다.
4. 운영 키는 절대 withdrawal 권한을 주지 않습니다.
5. 기존 키 유효성 해제(폐기) 여부를 거래소 콘솔에서 확인합니다.

### 권장 운영 순서
- 실서버 교체 전: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `DISCORD_WEBHOOK_URL` 값만 갱신.
- 테스트넷 shadow 모드에서 먼저 구동 확인.
- 즉시 `python -m v2.run --ops-action pause`로 진입 제어를 멈춰
  새 프로세스가 안전하게 시작되는지 확인.
- `status`/로그를 통해 `pause`가 반영되면, 주문 흐름이 비정상적이면 즉시 종료 후 재확인.
- 검증 후 `safe_mode`/`flatten` 동작을 통해 청산·정리 루틴이 잘 동작하는지 점검.

## 5. Ops 컨트롤(명령) 사용법

### CLI 액션
```bash
# pause: 새 진입 막기
python -m v2.run --mode shadow --env testnet --ops-action pause

# resume: 진입 재허용
python -m v2.run --mode shadow --env testnet --ops-action resume

# safe_mode: pause + safe_mode true
python -m v2.run --mode shadow --env testnet --ops-action safe_mode

# flatten: 심볼 기준으로 오더/포지션 정리 + 상태 고정
python -m v2.run --mode live --env testnet --ops-action flatten --ops-symbol BTCUSDT
```

### HTTP Ops API
```bash
python -m v2.run --ops-http --ops-http-host 127.0.0.1 --ops-http-port 8102

curl -s -X POST http://127.0.0.1:8102/ops/pause
curl -s -X POST http://127.0.0.1:8102/ops/resume
curl -s -X POST http://127.0.0.1:8102/ops/safe_mode
curl -s -X POST http://127.0.0.1:8102/ops/flatten -H 'content-type: application/json' -d '{"symbol":"BTCUSDT"}'
```

### 동작 요약
- `pause`: `apply_ops_mode(paused=True)`.
- `resume`: `apply_ops_mode(paused=False, safe_mode=False)`.
- `safe_mode`: `apply_ops_mode(paused=True, safe_mode=True)`.
- `flatten`: 취소 주문 + algo 주문 취소 + 포지션 정리 후 정합성 검증. 성공 시 `paused=true`, `safe_mode=true`, `open_regular_orders=0`, `open_algo_orders=0`, `position_amt=0.0`.

## 6. 테스트넷/실서버 시작 체크리스트
### 실행 전
- `v2/config/config.yaml`만 변경했는지 확인
- `.env`에서 다음 값이 모두 현재 사용 환경에 맞는지 확인: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `DISCORD_WEBHOOK_URL`
- `python -m ruff check v2 v2/tests`
- `python -m pytest -q v2/tests`
- `v2/scripts/preflight.sh --mode shadow --env testnet --profile normal` 실행(또는 `--mode live --env prod`)

### 실행 중 모니터링
- 출력에 `v2 boot completed`가 보이고 프로세스가 지속 실행되는지 확인
- `python -m v2.run --ops-action pause/resume/safe_mode` 응답에 맞는 상태 JSON이 반환되는지 확인
- 실시간 오더/체결 데이터와 상태값이 비정상 증분 없이 이어지는지 확인

### 운영 종료
- Ctrl+C 종료 후 재기동을 통해 상태가 `runtime`에 보존되는지 확인
- 필요한 경우 즉시 `safe_mode` + `flatten`로 수동 정리 후 종료

## 7. 공통 Binance 에러 대응

### -4120 `Order would trigger instantly` 계열
- 원인: 조건부 주문(TP/SL)을 레거시 주문 엔드포인트로 넣는 패턴.
- 대응: 조건부 진입/청산 정책은 `v2/tpsl` 경유 + `algoOrder` 사용 경로를 확인.
- 점검: TP/SL 생성 지점에서 `v2/exchange/rest_client.py`의 `place_algo_order` 사용 여부 확인.

### -4015 client order id length
- 원인: `clientOrderId` 길이/형식 위반.
- 대응: 기본 접두어/길이 정책(`v2/exchange/binance_adapter.py::next_client_order_id`) 적용 상태 확인.
- 운영 규칙: 길이 제한을 넘지 않도록 생성 규칙과 템플릿 점검.

### -1021 time drift
- 원인: 로컬 시각과 서버 시각 불일치.
- 대응: `v2/exchange/rest_client.py`는 signed 요청에서 시간 오프셋 동기화 후 재시도.
- 점검: preflight의 시간 동기 step이 통과하는지 확인.

### 429, 418
- 원인: 요청 한도 초과/IP 제한.
- 대응: `v2/config/config.yaml`의 `exchange.request_rate_limit_per_sec`를 낮추고 재시도 정책(`backoff_base_seconds`, `backoff_cap_seconds`) 점검.
- 확인: 동일 요청이 과도한 재시도로 실패하지 않는지 로그 및 재시도 횟수 확인.

## 8. 배포 준비 리포트
`v2/scripts/preflight.sh`를 실행하면 `v2/reports/deployment_readiness_YYYYMMDD_HHMMSS.md` 형태로
자동 점검 리포트를 생성합니다.

```bash
bash v2/scripts/preflight.sh --profile normal --mode shadow --env testnet
bash v2/scripts/preflight.sh --profile normal --mode live --env prod --report-file reports/preflight_prod_$(date -u +%Y%m%d_%H%M%S).md --keep-reports 30
```

리포트에는 다음 항목이 포함됩니다.
- ruff 검사
- tests/v2 실행 결과
- config validation
- ping/time sync 확인
- 점검 요약과 권고 조치

### 리포트 보존 정책
- 장기 보존이 필요한 경우 `--keep-reports <n>`을 사용해 최근 `n`개 리포트만 남길 수 있습니다.
- `--keep-reports 0` 또는 미지정 시에는 삭제를 수행하지 않습니다.
- 권장 기본값: 배포 브랜치에서는 `--keep-reports 30`, 일회성 점검은 생략
