# auto-trader

Binance USDT-M 선물 자동매매 저장소입니다.  
현재 실제 런타임은 `v2/` 하나로 보면 됩니다.

## 1분 요약
- **실행 코드는 거의 전부 `v2/` 안에 있다**
- **웹 운영면은 `/operator` 하나다**
- **루트의 `output/`, `archive/`, `logs/`, `data/`, `.venv/` 는 코드보다 산출물/환경에 가깝다**
- **처음 읽는 순서**:
  1. `README.md`
  2. `v2/README.md`
  3. `v2/AGENTS.md`
  4. `v2/run.py`
  5. `v2/control/`, `v2/kernel/`, `v2/strategies/`

## 루트 폴더 설명
| 경로 | 의미 | 평소 중요도 |
|---|---|---|
| `v2/` | 현재 운영 기준 코드 | 매우 높음 |
| `local_backtest/` | 로컬 백테스트 실행용 진입점 | 중간 |
| `output/` | 연구/보안/실험 산출물 아카이브 | 낮음 |
| `archive/` | 과거 자료 / 로컬 아카이브 | 낮음 |
| `data/` | 런타임 SQLite 등 상태 파일 | 낮음 |
| `logs/` | 루트 레벨 로그 산출물 | 낮음 |
| `.venv/` | 로컬 Python 가상환경 | 낮음 |
| `.omx/` | Codex/OMX 작업 상태 | 낮음 |
| `.vscode/` | 에디터 설정 | 낮음 |

루트에서 코드를 보려면 사실상 `v2/`와 `local_backtest/`만 보면 됩니다.

## 지금 어디를 열어야 하나
### 운영 흐름을 보고 싶을 때
- [v2/run.py](./v2/run.py)
- [v2/control/api.py](./v2/control/api.py)
- [v2/operator_web/router.py](./v2/operator_web/router.py)

### 진입/청산 판단을 보고 싶을 때
- [v2/kernel/kernel.py](./v2/kernel/kernel.py)
- [v2/kernel/defaults.py](./v2/kernel/defaults.py)
- [v2/strategies/ra_2026_alpha_v2.py](./v2/strategies/ra_2026_alpha_v2.py)
- [v2/strategies/ra_2026_alpha_v2_helpers.py](./v2/strategies/ra_2026_alpha_v2_helpers.py)
- [v2/strategies/ra_2026_alpha_v2_evaluators.py](./v2/strategies/ra_2026_alpha_v2_evaluators.py)

### 운영 패널을 보고 싶을 때
- [v2/operator/](./v2/operator)
- [v2/operator_web/](./v2/operator_web)

### 백테스트/연구를 보고 싶을 때
- [local_backtest/](./local_backtest)
- [v2/backtest/](./v2/backtest)
- [output/research/](./output/research)

## 빠른 실행
Shadow:
```bash
python -m v2.run --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --control-http --control-http-host 127.0.0.1 --control-http-port 8101 --operator-web
```

Live:
```bash
python -m v2.run --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --env-file .env --control-http --control-http-host 127.0.0.1 --control-http-port 8101 --operator-web
```

권장 통합 실행:
```bash
bash v2/scripts/run_stack.sh --mode live --env prod
```

## 빠른 검증
```bash
python -m ruff check v2 v2/tests
python -m pytest -q
python -m v2.run --deploy-prep --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --keep-reports 30
```

## Git / 서버 운영 기준
- 버전 관리는 `git`
- 원격 기준 저장소는 `GitHub(origin)` 하나
- 운영 서버는 직접 수정하지 않고 `git pull --ff-only`만 사용

예시:
```bash
bash v2/scripts/update_server_from_git.sh --branch migration/web-operator-panel --restart
```

## 운영 안전 주의
- 이 시스템은 USDT-M 선물 전용입니다.
- `.env`는 시크릿/인프라만 담고, 동작 설정은 `v2/config/config.yaml` 또는 runtime state에서 관리합니다.
- 제어 HTTP는 `127.0.0.1` bind 기준으로 쓰는 게 기본입니다.
- 실거래 전에는 [v2/docs/RUNBOOK.md](./v2/docs/RUNBOOK.md) 와 [v2/docs/SHADOW_SOAK_CHECKLIST.md](./v2/docs/SHADOW_SOAK_CHECKLIST.md) 를 먼저 확인하세요.
