# Research Protocol `research_reset_v1`

## 목적
이 문서는 `strategy-first` 연구 루프를 중단하고 `evidence-first` 방식으로 전환하기 위한 운영 규칙이다.

핵심은 단순하다.
- 전략 코드를 먼저 만들지 않는다.
- 먼저 `opportunity scan`으로 기회 존재와 비용 적합성을 검증한다.
- 스캔이 죽으면 전략 클래스 구현 자체를 금지한다.

## 적용 범위
- 기존 실패 브랜치
  - `strict`
  - `portfolio_v1`
  - `mr_v1`
  - `fb_v1`
  - `cbr_v1`
  - `lsr_v1`
  - `sfd_v1`
  - `pfd_v1`
- 위 브랜치는 모두 동결한다. 구조 구제용 추가 튜닝은 금지한다.

## 기본 원칙
1. 실험 1개는 가설 1개만 다룬다.
2. 활성 실험은 동시에 1개만 허용한다.
3. 각 실험은 `baseline 1회 + salvage 1회`까지만 허용한다.
4. `참여도 부족` 또는 `비용 초과`가 스캔 단계에서 보이면 전략 구현을 금지한다.
5. `shadow/live/Discord/status` 확장은 `1y KEEP` 전까지 금지한다.

## 필수 산출물
모든 새 실험은 아래 세 문서를 먼저 가진다.
- `research/registry.yaml`
- `research/templates/experiment_template.md`
- `scan_<experiment_id>.json`

전략 구현 단계로 넘어간 실험은 아래를 추가로 가진다.
- `local_backtest` 6개월 baseline report
- `local_backtest` 1년 baseline report
- 필요 시 3년 confirm report

## 상태 값
- `candidate`: 아직 활성화 전인 실험
- `active`: 현재 유일한 활성 실험
- `killed_pre_implementation`: scan gate 실패로 구현 금지
- `killed_post_baseline`: 구현은 했지만 baseline에서 종료
- `promoted`: 1y 이상 검증을 통과해 다음 단계로 승격

## 표준 워크플로
1. 실험 템플릿 작성
2. `research/registry.yaml`에 활성 실험 1개 등록
3. `python -m local_backtest.opportunity_scan` 실행
4. `scan_gate=KEEP`일 때만 전략 클래스 구현
5. 구현 후 `6m baseline`
6. `6m KEEP`일 때만 `1y baseline`
7. `1y KEEP`일 때만 `3y confirm`과 bounded sweep

## Opportunity Scan Gate
scan 단계는 전략 시뮬레이터가 아니라 후보 이벤트의 존재와 비용 적합성을 판정하는 단계다.

아래 조건을 모두 통과해야 `KEEP`이다.
- `candidate_events >= 120`
- 4심볼 우주에서 `symbol당 최소 15 events`
- `top edge decile`의 `median edge_after_cost > 0`
- `single fortnight concentration <= 35%`
- `expected hold <= 16 bars`

하나라도 실패하면:
- 실험 상태를 `killed_pre_implementation`으로 기록한다.
- `kill_reason`을 남긴다.
- 전략 클래스 코드는 만들지 않는다.

## Baseline Gate
scan 통과 뒤에만 baseline을 본다.

### 6개월 baseline
- `net > 0`
- `pf >= 1.15`
- `dd <= 12%`
- `trades >= 30`

실패 시:
- 상태를 `killed_post_baseline`으로 기록한다.
- `1y`, `3y`, bounded sweep은 금지한다.

### 1년 baseline
- `net >= 10`
- `pf >= 1.30`
- `dd <= 15%`
- `trades >= 70`

실패 시:
- 상태를 `killed_post_baseline`으로 기록한다.
- `3y`, bounded sweep, shadow 설계는 금지한다.

### 3년 confirm
- `net >= 18`
- `pf >= 1.35`
- `dd <= 18%`

## Salvage 규칙
- salvage는 딱 1회만 허용한다.
- salvage는 구조를 뒤집는 재시작이 아니라, 같은 가설 내부의 제한적 수정만 의미한다.
- 두 번째 salvage는 금지한다.
- salvage 후에도 baseline 실패면 해당 가설은 종료한다.

## 기록 규칙
- `research/registry.yaml`에는 현재 활성 실험 1개만 `active`로 둔다.
- `best_report_path`는 가장 최근 또는 가장 좋은 근거 리포트 경로를 가리킨다.
- `kill_reason`은 비워 두지 않는다. 최소 한 줄로 종료 사유를 적는다.

## 기본 방향
다음 사이클에서는 전략 이름을 먼저 짓지 않는다.

우선 구현할 것은 `scan_family`이며 기본 후보는 아래다.
- `crowding_plus_liquidity`

이 스캔이 통과하면 그때 첫 전략 클래스를 설계한다.
이 스캔이 실패하면 프로젝트는 `가설 탐색 종료` 상태로 전환하고 새 전략 추가를 멈춘다.
