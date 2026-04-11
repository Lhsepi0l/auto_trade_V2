# `v2/` 길찾기

`v2/`는 현재 운영 기준 런타임입니다.  
이 폴더 안에서 중요한 건 “실행”, “판단”, “운영”, “검증” 4축입니다.

## 처음 보는 사람용 읽기 순서
1. [run.py](./run.py)
2. [config/](./config)
3. [control/](./control)
4. [kernel/](./kernel)
5. [strategies/](./strategies)
6. [operator/](./operator) + [operator_web/](./operator_web)
7. [tests/](./tests)

## 폴더별 한 줄 설명
| 폴더 | 역할 |
|---|---|
| `config/` | 프로필/설정 로더 |
| `control/` | 제어 API, readiness, runtime orchestration |
| `kernel/` | 후보 선택, 리스크/사이징, 실행 조립 |
| `strategies/` | 실제 전략 정의 |
| `operator/` | 운영 패널용 서비스/읽기 모델 |
| `operator_web/` | `/operator` 웹 UI |
| `exchange/` | 바이낸스 REST / stream 연동 |
| `engine/` | 상태 저장, journal, reconcile |
| `tpsl/` | TP/SL bracket lifecycle |
| `storage/` | SQLite schema / runtime markers |
| `notify/` | webpush 알림 |
| `runtime/` | 부트/serve 진입 glue |
| `backtest/` | 리플레이/이벤트 테이프/연구용 백테스트 |
| `tests/` | 회귀 테스트 |
| `docs/` | 활성 문서와 archive |
| `management/` | 공용 포지션 관리 계약/상태 |
| `ops/` | pause/resume/safe/flatten 제어 |
| `scripts/` | 운영 보조 스크립트 |
| `systemd/` | 서비스 템플릿 |

## 자주 헷갈리는 폴더
- `operator/` vs `operator_web/`
  `operator/`는 서버 쪽 로직, `operator_web/`는 HTML/JS 라우트입니다.
- `control/` vs `kernel/`
  `control/`은 운영 제어면, `kernel/`은 실제 판단/사이징/실행 조립부입니다.
- `backtest/` vs `local_backtest/`
  `v2/backtest/`는 라이브러리/엔진, 루트 `local_backtest/`는 실행 스크립트 쪽입니다.
- `docs/` vs `docs/archive/`
  루트 문서는 현재 기준, `archive/`는 과거 기록입니다.

## 지금 가장 중요한 파일
- [run.py](./run.py)
- [control/api.py](./control/api.py)
- [kernel/kernel.py](./kernel/kernel.py)
- [kernel/defaults.py](./kernel/defaults.py)
- [strategies/ra_2026_alpha_v2.py](./strategies/ra_2026_alpha_v2.py)
- [strategies/ra_2026_alpha_v2_helpers.py](./strategies/ra_2026_alpha_v2_helpers.py)
- [strategies/ra_2026_alpha_v2_evaluators.py](./strategies/ra_2026_alpha_v2_evaluators.py)

## 덜 중요하거나 생성물 성격인 것
- `logs/`
- `reports/`
- `docs/archive/`

핵심 코드만 빠르게 따라가려면  
`run.py -> control/ -> kernel/ -> strategies/ -> operator_web/ -> tests/` 순서로 보면 됩니다.
