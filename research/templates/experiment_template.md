# 실험 작성 템플릿

## 1. 기본 정보
- `experiment_id`:
- `작성일`:
- `작성자`:
- `상태`:
  - `candidate`
  - `active`
  - `killed_pre_implementation`
  - `killed_post_baseline`
  - `promoted`

## 2. 가설
- 한 줄 가설:
- 왜 이 가설이 기존 실패 브랜치와 다른가:
- 예상 엣지의 원천:
- 실패 조건:

## 3. 데이터 축
- `data_axes`:
- 각 축이 필요한 이유:
- 재현 가능한 축만 남겼는가:
- 런타임 전용 축이 있다면 왜 이번 실험에서 제외하는가:

## 4. 대상 우주와 고정 창
- `universe`:
- `window.start_utc`:
- `window.end_utc`:
- 선택 이유:

## 5. Scan Family 정의
- `scan_family`:
- 이벤트 정의:
- 기대 hold time:
- 비용 차감 전 예상 edge:
- 비용 차감 후 기대 edge:
- 주요 차단 조건:

## 6. Opportunity Scan 실행 계획
- 실행 명령:
```bash
python -m local_backtest.opportunity_scan \
  --experiment-id <experiment_id> \
  --registry-path research/registry.yaml \
  --report-dir local_backtest/reports \
  --cache-root local_backtest/reports/_cache
```
- 확인할 필드:
  - `candidate_events`
  - `events_per_symbol`
  - `events_per_week`
  - `edge_after_cost_distribution`
  - `hold_time_distribution`
  - `block_top`
  - `scan_gate`

## 7. Scan Gate 판정
- `candidate_events >= 120`:
- `symbol당 최소 15 events`:
- `top edge decile median edge_after_cost > 0`:
- `single fortnight concentration <= 35%`:
- `expected hold <= 16 bars`:
- 최종 판정:
  - `KEEP`
  - `KILL`

## 8. 전략 구현 허용 여부
- `scan_gate=KEEP`인가:
- 구현 허용 여부:
- 구현 금지 사유:

## 9. Baseline 결과
- `6m baseline`:
  - `net > 0`
  - `pf >= 1.15`
  - `dd <= 12%`
  - `trades >= 30`
  - 판정:
- `1y baseline`:
  - `net >= 10`
  - `pf >= 1.30`
  - `dd <= 15%`
  - `trades >= 70`
  - 판정:
- `3y confirm`:
  - `net >= 18`
  - `pf >= 1.35`
  - `dd <= 18%`
  - 판정:

## 10. Salvage
- salvage 필요 여부:
- 허용된 1회 수정 내용:
- 수정 후 다시 볼 지표:
- 두 번째 수정이 금지되는 이유:

## 11. 최종 결론
- `best_report_path`:
- `kill_reason`:
- 남길 자산:
- 다음 단계:
