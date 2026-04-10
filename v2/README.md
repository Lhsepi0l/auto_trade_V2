# `v2/` 길찾기

`v2/`는 현재 운영 기준 런타임입니다. 이제는 아래 4개 구역으로 보면 됩니다.

## 1. 핵심 코드
- `run.py`: 진입점
- `config/`: 프로필/설정
- `kernel/`: 판단, 사이징, 실행 조립부
- `strategies/`: 전략 정의
- `exchange/`, `engine/`, `storage/`, `tpsl/`, `risk/`: 거래/상태/저장/브래킷/리스크 핵심부

## 2. 운영면
- `control/`: 제어 API와 상태 응답
- `operator/`: 운영용 서비스/읽기 모델
- `operator_web/`: `/operator` 웹 라우트/템플릿/정적 파일
- `discord_bot/`: fallback 운영면
- `ops/`, `runtime/`, `scripts/`, `systemd/`: 실행/배포/운영 자동화

## 3. 검증/연구면
- `tests/`: 회귀 테스트
- `backtest/`: 로컬 백테스트 엔진
- `cli/`, `common/`, `core/`, `notify/`: 공용 조립부

## 4. 생성물/기록물
- `docs/`: 운영 문서와 과거 기록
- `logs/`: 로컬 런타임 로그
- `reports/`: 현재 실행이 다시 생성할 수 있는 점검 산출물

핵심 코드만 빠르게 따라가려면 `run.py -> control/ -> kernel/ -> strategies/ -> tests/` 순서로 보면 됩니다.
