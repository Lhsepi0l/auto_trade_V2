# `alpha_expansion` Phase 1.5 연구 보고서

작성일: 2026-03-11  
대상 전략: `BTCUSDT` 전용 `ra_2026_alpha_v2`의 `alpha_expansion`

## 1. Phase 1 결과 해석

Phase 1에서 확인된 사실은 아래 네 가지였다.

- `quality_score v1`은 plateau였다.
  - 기존 hard gate를 다시 평균낸 구조라 실제 체결 set을 거의 못 바꿨다.
- `ADX window / ADX rising`도 plateau였다.
  - 1Y/3Y 모두 체결 set이 동일했고, incremental information이 없었다.
- `breakout_efficiency`는 실제로 trade set을 바꿨다.
  - 하지만 `DD`만 줄이고 `net / PF / expectancy`를 함께 깎았다.
- 따라서 살아 있는 가설은 하나였다.
  - `breakout structure quality`는 진짜 병목이다.
  - 다만 `breakout_efficiency = breakout / candle_range` 한 줄은 너무 둔했다.

즉, 문제는 축이 아니라 표현식이었다.

## 2. 왜 breakout structure quality 축은 살아 있고, 기존 구현식만 둔했는가

기존 `breakout_efficiency`는 사실상 아래 하나만 봤다.

- `close가 channel 위로 range 대비 얼마나 올라섰는가`

이 식의 문제는 두 가지다.

1. `안착`과 `되밀림`을 구분하지 못한다.
   - close가 channel 위에 있어도 upper wick이 길면 구조는 약할 수 있다.

2. `tradeability after cost`와 상호작용하지 못한다.
   - 같은 구조 결함이라도 비용 대비 edge가 충분하면 살려둘 수 있고,
   - edge가 약하면 같은 결함이 더 치명적이다.

그래서 Phase 1.5에서는 `좋은 breakout을 더하는 점수`가 아니라, `나쁜 breakout 구조를 벌점으로 제거하는 방식`으로 바꿨다.

## 3. Phase 1.5 실험 설계

### EXP-04 `breakout_stability_score`

- 겨냥 실패 모드:
  - channel 위로는 닫혔지만 wick rejection이 크거나 실제 안착이 약한 breakout
- 왜 효과가 있을 가능성이 있는가:
  - false breakout의 핵심은 절대 크기보다 `안착 + 유지` 구조이기 때문이다.
- 어떤 부작용이 있는가:
  - 진짜 좋은 breakout 중 일부도 `wick이 큰 변동성 캔들`이면 잘릴 수 있다.
- 어떤 지표가 좋아져야 성공인가:
  - `PF`, `fee_to_trade_gross`, `MDD`, `1Y/3Y 일관성`
- plateau 판정 조건:
  - block reason만 늘고 체결 set과 요약 수치가 그대로인 경우
- 최소 구현 단위:
  - `expansion_breakout_stability_score_min`
- 구현 논리:
  - `shallow overhang penalty`
  - `rejection wick penalty`
  - 두 벌점의 평균으로 `stability score` 생성

### EXP-05 `breakout_stability_edge_score`

- 겨냥 실패 모드:
  - 구조가 조금만 흔들려도 비용 대비 edge가 약한 breakout
- 왜 효과가 있을 가능성이 있는가:
  - 같은 구조 결함도 edge가 충분하면 버틸 수 있고, edge가 약하면 기대값이 빨리 무너진다.
- 어떤 부작용이 있는가:
  - 이미 강한 structure filter 위에 edge interaction을 얹으면 trade 수를 과도하게 줄일 수 있다.
- 어떤 지표가 좋아져야 성공인가:
  - `EXP-04` 대비 `PF`, `expectancy`, `fee efficiency` 추가 개선
- plateau 판정 조건:
  - `EXP-04`와 거의 동일하거나, trade만 더 줄고 `PF`가 개선되지 않는 경우
- 최소 구현 단위:
  - `expansion_breakout_stability_edge_score_min`
- 구현 논리:
  - `EXP-04`의 structure penalty를 `edge_ratio deficit`으로 증폭

### EXP-06 `quality_score_v2`

- 겨냥 실패 모드:
  - 구조는 나쁘지 않아 보여도 `width expansion x edge`가 약해 실제론 기대값이 낮은 breakout
- 왜 효과가 있을 가능성이 있는가:
  - Phase 1의 `quality_score v1`은 기존 hard gate 중복이었다.
  - 새 품질 점수는 구조적 결함의 최악값과 `width-edge` 상호작용만 보므로 overlap이 훨씬 낮다.
- 어떤 부작용이 있는가:
  - unified gate가 너무 강하면 `EXP-05`보다 더 많은 유효 breakout을 잘라낼 수 있다.
- 어떤 지표가 좋아져야 성공인가:
  - `EXP-05` 대비 `PF`, `expectancy`, `fee efficiency` 추가 개선
- plateau 판정 조건:
  - `EXP-05`보다 숫자가 거의 같거나 오히려 net/PF가 후퇴하는 경우
- 최소 구현 단위:
  - `expansion_quality_score_v2_min`
- 구현 논리:
  - additive sum 대신 `max(structure_penalty, width_edge_penalty)` 사용
  - 즉 `좋은 조건 가산`이 아니라 `최악 결함 제거` 방식

## 4. 코드 변경 요약

수정 파일:

- `v2/strategies/ra_2026_alpha_v2.py`
- `v2/run.py`
- `v2/tests/test_ra_2026_alpha_v2.py`
- `v2/tests/test_v2_local_backtest.py`

추가 파라미터:

- `expansion_breakout_stability_score_min`
- `expansion_breakout_stability_edge_score_min`
- `expansion_quality_score_v2_min`

추가 block reason:

- `breakout_stability_missing`
- `breakout_stability_edge_missing`
- `quality_score_v2_missing`

핵심 구현 변화:

- breakout level을 `buffered channel` 기준으로 정렬
- `overhang`과 `rejection wick`을 별도 구조 feature로 계산
- `edge_ratio`와 `width expansion`을 additive가 아니라 penalty interaction으로 연결

## 5. 백테스트 결과

### baseline

| 기간 | net | PF | MDD | trades | fee_to_trade_gross | win_rate | avg_winner | avg_loser | expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1Y | 3.0018 | 2.5893 | 6.3317% | 154 | 60.9512% | 11.6883% | 0.4799 | -0.0245 | 0.0344 |
| 3Y | 7.9560 | 2.3404 | 7.7014% | 546 | 68.4038% | 9.7070% | 0.5427 | -0.0249 | 0.0302 |

### 실험 결과

| 실험 | 기간 | net | PF | MDD | trades | fee_to_trade_gross | win_rate | avg_winner | avg_loser | expectancy |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| EXP-04 breakout_stability | 1Y | 2.6140 | 2.6290 | 5.5271% | 132 | 59.4380% | 12.1212% | 0.4528 | -0.0238 | 0.0340 |
| EXP-04 breakout_stability | 3Y | 8.2153 | 2.4736 | 6.7785% | 466 | 63.5534% | 9.8712% | 0.5583 | -0.0247 | 0.0328 |
| EXP-05 stability_edge | 1Y | 2.6644 | 2.6558 | 5.5271% | 131 | 58.7645% | 12.2137% | 0.4535 | -0.0238 | 0.0345 |
| EXP-05 stability_edge | 3Y | 8.3286 | 2.4883 | 6.6464% | 464 | 63.1275% | 9.9138% | 0.5591 | -0.0247 | 0.0331 |
| EXP-06 quality_score_v2 | 1Y | 2.6956 | 2.6729 | 5.5271% | 131 | 58.3411% | 12.2137% | 0.4538 | -0.0236 | 0.0347 |
| EXP-06 quality_score_v2 | 3Y | 8.4157 | 2.4991 | 6.6982% | 463 | 62.8188% | 9.9352% | 0.5599 | -0.0247 | 0.0334 |

## 6. 결과 해석

### EXP-04

- 판정:
  - `유효`
- 해석:
  - 1Y에서는 net이 baseline보다 줄었지만 `PF`, `MDD`, `fee efficiency`가 개선됐다.
  - 3Y에서는 `net/PF/MDD/fee efficiency`가 동시에 좋아졌다.
- 결론:
  - `penalty-based breakout stability`는 확실히 살아 있는 축이다.

### EXP-05

- 판정:
  - `EXP-04 대비 개선`
- 해석:
  - 1Y/3Y 모두 `EXP-04`보다 `PF`, `expectancy`, `fee efficiency`가 더 좋아졌다.
  - 즉 구조 결함을 cost edge와 상호작용으로 본 것이 실제로 도움이 됐다.
- 결론:
  - `structure + edge interaction`은 의미 있는 개선 축이다.

### EXP-06

- 판정:
  - `현재 최상위`
- 해석:
  - 1Y 기준 `PF`, `expectancy`, `fee efficiency`가 가장 좋다.
  - 3Y 기준 `net`, `PF`, `fee efficiency`, `win_rate`, `expectancy`가 모두 가장 좋다.
  - `MDD`는 `EXP-05`보다 소폭 높지만 baseline보다 훨씬 낮다.
- 결론:
  - 현재 기준 winner는 `quality_score_v2_min=0.72`다.
  - 이번 개선은 `raw net만 올린 것`이 아니라 `PF / expectancy / fee efficiency / 1Y-3Y 균형`을 같이 개선했다는 점이 중요하다.

## 7. 최종 진단

### 살아 있는 축

- `breakout structure quality`
- `overhang above breakout/channel`
- `rejection wick penalty`
- `cost edge interaction`
- `penalty-based quality gate`

### plateau 또는 후순위 축

- `body_ratio / close_location / range_atr` 미세조정
- `ADX upper / ADX rising`
- `강한 followthrough confirmation`
- 단순 `breakout_efficiency` threshold

## 8. 권고안

### 지금 당장 할 것

- `EXP-06`을 현시점 최상위 후보로 승격 검토
  - 권장 값: `expansion_quality_score_v2_min=0.72`

### 바로 하지 말 것

- body/close/range 하드 임계값 재미세조정
- ADX 파생 조건 추가
- 더 강한 followthrough 확인

### 다음 연구 순서

1. `Phase 1.5 winner`를 slice별로 다시 확인
2. 이상 없으면 그다음에야 `Entry split` 또는 `Exit conditioned on quality`로 이동

## 9. 산출물

- baseline 1Y: `local_backtest/reports/alpha_expansion_phase15_baseline_1y_20260311.json`
- baseline 3Y: `local_backtest/reports/alpha_expansion_phase15_baseline_3y_20260311.json`
- EXP-04 1Y: `local_backtest/reports/alpha_expansion_phase15_exp04_breakout_stability_1y_20260311.json`
- EXP-04 3Y: `local_backtest/reports/alpha_expansion_phase15_exp04_breakout_stability_3y_20260311.json`
- EXP-05 1Y: `local_backtest/reports/alpha_expansion_phase15_exp05_stability_edge_1y_20260311.json`
- EXP-05 3Y: `local_backtest/reports/alpha_expansion_phase15_exp05_stability_edge_3y_20260311.json`
- EXP-06 1Y: `local_backtest/reports/alpha_expansion_phase15_exp06_quality_v2_1y_20260311.json`
- EXP-06 3Y: `local_backtest/reports/alpha_expansion_phase15_exp06_quality_v2_3y_20260311.json`
