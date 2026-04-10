# auto-trader

Binance USDT-M 선물 자동매매 저장소입니다. 현재 활성 런타임 표면은 `v2/`입니다.

## 저장소 지도
- `v2/`: 현재 운영 기준 코드
- `v2/README.md`: `v2` 내부 길찾기
- `local_backtest/`: 로컬 연구/백테스트 진입점
- `output/`: Git에 남겨둔 연구/보안 참고 산출물
- `archive/`: 과거 산출물/로컬 아카이브 모음

생성물/로컬 작업물:
- `data/`, `logs/`, `v2/logs/`: 런타임 산출물
- `local_backtest/cache/`, `local_backtest/reports/`: 로컬 백테스트 생성물
- `출력/`: 로컬 디버그/내보내기 산출물
- `.omx/`, `.venv/`, `.vscode/`: 로컬 작업 환경

코드만 빠르게 보려면 루트에서는 `v2/`와 `local_backtest/`만 보면 됩니다. 나머지는 거의 전부 생성물/기록물입니다.

## 현재 런타임
- 엔트리포인트: `python -m v2.run`
- 제어 API: `v2/control/*`
- 웹 운영 콘솔: `/operator`
- 디스코드 봇: optional fallback / emergency-use-only
- 테스트: `v2/tests/`

## 주요 경로
- `v2/run.py`: 런타임/제어 HTTP/로컬 백테스트 CLI
- `v2/config/config.yaml`: 프로필과 기본 동작 설정
- `v2/control/api.py`: 런타임 제어와 risk override
- `v2/kernel/*`: 핵심 판단/사이징/실행 조립부
- `v2/operator/*`: 운영용 서비스/읽기 모델
- `v2/operator_web/*`: `/operator` 웹 라우트/정적 리소스
- `v2/docs/web_operator_panel.md`: web-first 운영 안내
- `v2/docs/RUNBOOK.md`: 운영 런북
- `v2/docs/SHADOW_SOAK_CHECKLIST.md`: shadow 점검 절차
- `v2/docs/GIT_WORKFLOW_KO.md`: Git / GitHub 운영 기준
- `v2/docs/archive/20260327_live_operator_upgrade/`: 과거 업그레이드 기록

## 설치
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Windows PowerShell:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 실행 예시
Shadow:
```bash
python -m v2.run --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --control-http --control-http-host 127.0.0.1 --control-http-port 8101 --operator-web
```

Live:
```bash
python -m v2.run --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --env-file .env --control-http --control-http-host 127.0.0.1 --control-http-port 8101 --operator-web
```

권장 web-first 통합 실행:
```bash
bash v2/scripts/run_stack.sh --mode live --env prod
```

## 검증
```bash
python -m ruff check v2 v2/tests
python -m pytest -q
python -m v2.run --deploy-prep --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --keep-reports 30
```

## Git / GitHub 기준
- 버전 관리는 `git`
- 원격 기준 저장소는 `GitHub(origin)` 하나
- 서버는 직접 수정하지 말고 `git pull`만 사용
- 자세한 기준은 [v2/docs/GIT_WORKFLOW_KO.md](./v2/docs/GIT_WORKFLOW_KO.md)

서버 업데이트 예시:
```bash
bash v2/scripts/update_server_from_git.sh --branch migration/web-operator-panel --restart
```

## 제어 API
- 상태 확인: `GET http://127.0.0.1:8101/status`
- 리스크 확인: `GET http://127.0.0.1:8101/risk`
- 시작: `POST http://127.0.0.1:8101/start`
- 즉시 tick: `POST http://127.0.0.1:8101/scheduler/tick`
- 런타임 설정 변경: `POST http://127.0.0.1:8101/set`
- 웹 운영 콘솔: `GET http://127.0.0.1:8101/operator`

## 운영 안전 주의
- 이 시스템은 USDT-M 선물 전용입니다.
- `.env`에는 시크릿만 두고, 동작 설정은 `v2/config/config.yaml` 또는 runtime risk state로 관리합니다.
- 제어 HTTP의 mutating endpoint는 현재 별도 인증이 없습니다. `127.0.0.1`에만 bind하고 SSH 터널/VPN/역방향 프록시 인증 없이 외부에 노출하지 마세요.
- 기본 운영면은 웹 패널입니다. Discord는 선택형 fallback / emergency-use-only 로 취급합니다.
- 실거래 전에는 `v2/docs/RUNBOOK.md`와 `v2/docs/SHADOW_SOAK_CHECKLIST.md` 절차를 먼저 따르세요.
