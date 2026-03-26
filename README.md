# auto-trader

Binance USDT-M 선물 자동매매 저장소입니다. 현재 활성 런타임 표면은 `v2/`입니다.

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
- `v2/clean_room/*`: kernel, sizing, execution
- `v2/discord_bot/*`: optional fallback operator surface
- `v2/docs/web_operator_panel.md`: web-first 운영 안내
- `v2/docs/RUNBOOK.md`: 운영 런북
- `v2/docs/SHADOW_SOAK_CHECKLIST.md`: shadow 점검 절차
- `v2/docs/GIT_WORKFLOW_KO.md`: Git / GitHub 운영 기준
- `v2/docs/20260327_live_operator_upgrade/`: 2026-03-27 실거래/운영 웹/포지션 관리 정리 문서 묶음

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

선택형 Discord fallback:
```bash
bash v2/scripts/run_stack.sh --mode live --env prod --with-discord-bot
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
