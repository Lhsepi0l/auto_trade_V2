# `alpha_expansion` Phase 1 연구 보고서

작성일: 2026-03-11  
대상 전략: `BTCUSDT` 전용 `ra_2026_alpha_v2`의 `alpha_expansion`

## 1. 현재 로직 요약

현재 `alpha_expansion`은 아래 순서로 작동한다.

- `4h`: `EMA50/EMA200 + ADX min`으로 추세 레짐을 만든다.
- `1h`: bias 정렬을 만든다.
- `15m`: 아래 hard gate를 모두 통과해야 진입 후보가 된다.
  - `volume_ratio`
  - `squeeze_percentile`
  - `width_expansion`
  - `range / ATR`
  - `Donchian breakout`
  - `break_distance / ATR`
  - `body_ratio`
  - `close_location`
  - `expected_move vs cost`
- 최종 score는 있으나, 실제로는 `bias / volume / range / width`를 얕게 더한 형태라 체결 품질을 날카롭게 재분류하지는 못한다.

요약하면, 현재 구조는 `고립된 hard threshold 집합 + 얕은 후행 score`다.  
따라서 병목 후보는 `threshold를 하나 더 올리는 것`보다 `구조 품질을 어떻게 다시 표현하느냐`에 가깝다.

## 2. 날카로운 진단

### 진짜 병목으로 의심되는 축

- `breakout structure quality`
  - 단순 breakout 유무보다, 채널 위로 얼마나 안정적으로 안착했는지가 더 중요하다.
- `entry price efficiency`
  - 같은 breakout이라도 너무 뻗은 가격에서 바로 추격하면 기대값이 급감한다.
- `exit differentiation`
  - 강한 진입과 약한 진입이 모두 `TP=2R`, `time_stop=18`을 쓰고 있어 quality-sensitive exit가 없다.

### plateau 가능성이 높은 축

- `body_ratio`, `close_location`, `range_atr`, `expected_move_cost_mult`의 hard threshold 미세조정
- `강한 followthrough 확인`
- 이번 Phase 1에서 구현한 `ADX upper window`, `ADX rising`
- 이번 Phase 1의 첫 버전 `expansion_quality_score`
  - 이유: 기존 hard gate와 feature overlap이 너무 커서 실제 체결 set을 거의 못 바꿨다.

### 아직 덜 탐색된 유망 축

- `breakout stability / breakout overhang` 재정의
- `quality-sensitive entry timing`
  - 강한 신호 즉시 진입, 중간 신호 retest 대기
- `quality-sensitive exit`
  - 강한 신호 longer hold / 더 큰 TP, 약한 신호 빠른 cut

## 3. `expansion_quality_score` 설계안

### 후보 A: additive deficit score

공식:

`score = 1 - mean(deficit(body), deficit(favored_close), deficit(width_expansion), deficit(edge_ratio))`

- 목적:
  - 강한 expansion, 안정된 close, 충분한 width expansion, 비용 대비 tradeability를 한 번에 본다.
- 기존 hard gate와의 중복:
  - 높음
- 기대 이점:
  - 구현이 가장 단순하고 on/off가 쉽다.
- 과적합 위험:
  - 낮음
- 한계:
  - hard gate를 이미 통과한 이후에는 추가 분별력이 급격히 약해질 수 있다.

### 후보 B: penalty-based breakout stability score

공식 예시:

`score = 1 - mean(penalty(low_breakout_efficiency), penalty(high_opposite_wick), penalty(low_close_above_channel), penalty(low_edge_ratio))`

- 목적:
  - `돌파했다`보다 `돌파 후 버텼다`를 본다.
- 기존 hard gate와의 중복:
  - 중간
- 기대 이점:
  - false breakout 제거 가능성이 가장 높다.
- 과적합 위험:
  - 중간
- 한계:
  - wick / overhang 정의를 잘못 잡으면 좋은 breakout도 과도하게 잘린다.

### 후보 C: interaction score

공식 예시:

`score = f(squeeze_depth * width_expansion * breakout_overhang) * g(edge_ratio)`

- 목적:
  - `깊은 squeeze 뒤 강한 expansion`이라는 구조적 상호작용을 직접 본다.
- 기존 hard gate와의 중복:
  - 낮음
- 기대 이점:
  - 구조적 quality를 가장 잘 잡을 가능성이 있다.
- 과적합 위험:
  - 높음
- 한계:
  - 첫 구현으로 넣기에는 자유도가 너무 많다.

### Phase 1에서 선택한 보수 버전

- 선택: 후보 A
- 이유:
  - 가장 작은 구현 단위이고, 독립 toggle이 쉽고, 실패했을 때 해석이 명확하다.
- 결과:
  - 실제로는 예상대로 `중복도가 너무 높아서` incremental signal이 거의 없었다.

## 4. 실험별 설계 질문과 판정

### EXP-01 `expansion_quality_score`

- 겨냥한 실패 모드:
  - 개별 hard gate는 통과하지만 종합적으로는 약한 expansion candle
- 왜 더 중요한가:
  - `body/close/range` 미세조정보다 `복합 결손`이 더 본질적일 가능성이 높기 때문
- 성공 시 좋아져야 할 지표:
  - `PF`, `fee_to_trade_gross`, `MDD`
- plateau 판정 조건:
  - block reason은 늘어나도 실제 체결 set과 성능 수치가 거의 안 바뀌는 경우
- 최소 구현 단위:
  - `expansion_quality_score_min` 단일 threshold

### EXP-02 `breakout_efficiency`

- 겨냥한 실패 모드:
  - 채널 위로 살짝 걸친 뒤 바로 꺾이는 false breakout
- 왜 더 중요한가:
  - breakout magnitude보다 breakout stability가 진짜 병목일 가능성이 높기 때문
- 성공 시 좋아져야 할 지표:
  - `PF`, `fee_to_trade_gross`, `MDD`
- plateau 판정 조건:
  - trade만 줄고 `PF`가 개선되지 않는 경우
- 최소 구현 단위:
  - `expansion_breakout_efficiency_min` 단일 threshold

### EXP-03A `ADX window`

- 겨냥한 실패 모드:
  - 너무 늦은 과열 추세에서의 진입
- 왜 더 중요한가:
  - regime suitability는 구조적 병목일 가능성이 있지만, threshold 미세조정보다 레짐 적합도 자체가 더 중요하기 때문
- 성공 시 좋아져야 할 지표:
  - `MDD`, `PF`
- plateau 판정 조건:
  - 1Y/3Y 모두 체결 set이 동일한 경우
- 최소 구현 단위:
  - `trend_adx_max_4h`

### EXP-03B `ADX rising`

- 겨냥한 실패 모드:
  - 방향은 맞아도 이미 힘이 식은 추세에서의 진입
- 왜 더 중요한가:
  - 단순 ADX 수준보다 추세 에너지 지속 여부를 보기 때문
- 성공 시 좋아져야 할 지표:
  - `PF`, `fee_to_trade_gross`, worst slice 품질
- plateau 판정 조건:
  - 1Y/3Y 모두 체결 set이 동일한 경우
- 최소 구현 단위:
  - `trend_adx_rising_lookback_4h`
  - `trend_adx_rising_min_delta_4h`

## 5. 실험 결과

비교 기준 baseline:

- 1Y: `rolling window`, `2025-03-10T20:48:29Z ~ 2026-03-10T20:48:29Z`
- 3Y: `rolling window`, `2023-03-11T20:48:29Z ~ 2026-03-10T20:48:29Z`
- 주의:
  - 이전 고정 리포트와 숫자가 약간 다른 이유는 `2026-03-11 현재 시각 기준 rolling window`를 다시 돌렸기 때문이다.

### 요약 표

| 실험 | 기간 | net | PF | MDD | trades | fee_to_trade_gross | win_rate | avg_winner | avg_loser | expectancy |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 1Y | 3.0018 | 2.5893 | 6.3317% | 154 | 60.9512% | 11.6883% | 0.4799 | -0.0245 | 0.0344 |
| EXP-01 quality_score | 1Y | 3.0018 | 2.5893 | 6.3317% | 154 | 60.9512% | 11.6883% | 0.4799 | -0.0245 | 0.0344 |
| EXP-02 breakout_efficiency | 1Y | 2.3548 | 2.4932 | 5.7463% | 138 | 63.1137% | 11.5942% | 0.4511 | -0.0237 | 0.0313 |
| EXP-03A adx_window | 1Y | 3.0018 | 2.5893 | 6.3317% | 154 | 60.9512% | 11.6883% | 0.4799 | -0.0245 | 0.0344 |
| EXP-03B adx_rising | 1Y | 3.0018 | 2.5893 | 6.3317% | 154 | 60.9512% | 11.6883% | 0.4799 | -0.0245 | 0.0344 |
| baseline | 3Y | 7.9560 | 2.3404 | 7.7014% | 546 | 68.4038% | 9.7070% | 0.5427 | -0.0249 | 0.0302 |
| EXP-01 quality_score | 3Y | 7.9560 | 2.3404 | 7.7014% | 546 | 68.4038% | 9.7070% | 0.5427 | -0.0249 | 0.0302 |
| EXP-02 breakout_efficiency | 3Y | 6.5234 | 2.2839 | 7.1427% | 495 | 69.8510% | 9.4949% | 0.5292 | -0.0243 | 0.0282 |
| EXP-03A adx_window | 3Y | 7.9560 | 2.3404 | 7.7014% | 546 | 68.4038% | 9.7070% | 0.5427 | -0.0249 | 0.0302 |
| EXP-03B adx_rising | 3Y | 7.9560 | 2.3404 | 7.7014% | 546 | 68.4038% | 9.7070% | 0.5427 | -0.0249 | 0.0302 |

### 세부 해석

#### EXP-01 `expansion_quality_score`

- 결과:
  - 1Y/3Y 모두 `완전 동일`
- 해석:
  - `quality_score_missing` block은 새로 생겼다.
  - 하지만 실제 체결 set과 성능 숫자는 전혀 안 바뀌었다.
  - 즉 score는 후보 일부를 막았지만, 체결까지 이어지는 최종 candidate에는 추가 정보량을 못 줬다.
- 결론:
  - 현재 score formulation은 `plateau`에 가깝다.
  - 이유는 기존 hard gate와의 overlap이 너무 크기 때문이다.

#### EXP-02 `breakout_efficiency`

- 결과:
  - 1Y: `net 3.0018 -> 2.3548`, `PF 2.5893 -> 2.4932`, `MDD 6.3317 -> 5.7463`
  - 3Y: `net 7.9560 -> 6.5234`, `PF 2.3404 -> 2.2839`, `MDD 7.7014 -> 7.1427`
- 해석:
  - 분명히 false breakout 일부는 잘랐다.
  - 하지만 좋은 breakout도 같이 잘라서 `PF`와 `expectancy`까지 내려갔다.
  - `breakout stability`가 병목 축이라는 thesis는 유지된다.
  - 다만 현재 구현식은 너무 거칠다.
- 결론:
  - 축은 유망하지만, 현재 one-line filter는 채택 불가

#### EXP-03A `ADX window`

- 결과:
  - 1Y/3Y 모두 `완전 동일`
- 해석:
  - 현재 baseline 체결은 `ADX > 32` 구간에 거의 의존하지 않는다.
- 결론:
  - `ADX upper bound`는 plateau

#### EXP-03B `ADX rising`

- 결과:
  - 1Y/3Y 모두 `완전 동일`
- 해석:
  - 현재 baseline 체결은 이미 `ADX rising` 조건을 자연스럽게 만족하거나, 해당 제약이 후보 집합을 못 건드린다.
- 결론:
  - `ADX slope` 추가는 plateau

### EXP-03C `ADX window + rising`을 생략한 이유

- `window`와 `rising`이 각각 1Y/3Y 체결 set을 전혀 바꾸지 못했다.
- 따라서 둘의 conjunction도 incremental information을 주지 못한다.
- 이는 `실험 회피`가 아니라 strict ablation 관점의 `subsumed case 제거`다.

## 6. 최종 판정

### 병목 축 판정

- `breakout structure quality`: 맞다. 아직 유효한 병목 축이다.
- `expansion quality discrimination`: 맞다. 다만 현재 score 설계가 너무 중복적이다.
- `regime suitability`: 부분적으로 맞다. 하지만 `ADX window/rising`은 아니다.
- `entry price efficiency`: 아직 Phase 1에서 직접 못 건드렸다.
- `exit differentiation`: 아직 손대지 않았다.

### plateau 판정

- 거의 확정 plateau:
  - `body_ratio / close_location / range_atr` hard threshold 미세조정
  - `EXP-01`의 첫 score 설계
  - `ADX upper window`
  - `ADX rising`
- 아직 유망하지만 현재 구현이 실패:
  - `breakout_efficiency`

## 7. 무엇을 바로 할 것 / 나중에 할 것 / 하지 말아야 할 것

### 바로 할 것

- `Phase 1.5`: `breakout_efficiency`를 `breakout_stability_score`로 재설계
  - 예시 구성:
    - `breakout_overhang_atr`
    - `close_above_channel_frac`
    - `opposite_wick_penalty`
    - `edge_ratio`
- `quality_score` 재설계
  - 현재처럼 `body/close/width/cost`를 다시 더하지 말고
  - `width_expansion x overhang x cost_edge`처럼 구조적 상호작용을 보도록 바꿔야 한다.

### 나중에 할 것

- `Entry split`
  - strongest signal만 즉시 진입
  - 중간 quality는 retest 대기
- `Exit conditioned on entry quality`
  - 강한 진입은 `TP/time_stop` 완화
  - 약한 진입은 더 짧게 cut

### 하지 말아야 할 것

- `body_ratio`를 0.25, 0.27, 0.29처럼 다시 미세조정하기
- `close_location`을 0.45, 0.47, 0.50처럼 다시 미세조정하기
- `range_atr`를 다시 잘게 쪼개기
- `ADX window / rising`을 더 세분화해서 다시 파기
- `followthrough`를 더 세게 걸기

## 8. Phase 2 진행 권고

권고: `지금 바로 Phase 2로 넘어가지 말 것`

이유:

- Phase 1의 목적은 `진짜 병목 축이 무엇인지`를 확인하는 것이었다.
- 결과적으로 `breakout structure`는 유효한 축으로 남았지만, `quality score v1`과 `ADX refinement`는 거의 무효였다.
- 이런 상태에서 `entry split`이나 `quality-conditioned exit`로 넘어가면, 구조 품질이 불명확한 상태에서 downstream complexity만 증가한다.

따라서 다음 단계는 `Phase 2`가 아니라 아래 순서가 맞다.

1. `Phase 1.5`
   - `breakout_stability_score` 재설계
   - `quality_score`를 구조 상호작용 기반으로 재설계
2. 그 결과가 실제로 `PF / expectancy / fee efficiency`를 개선하면
3. 그때 `Entry split`과 `Exit conditioned on quality`로 넘어간다

## 9. 산출물

- baseline 1Y: `local_backtest/reports/alpha_expansion_phase1_baseline_1y_20260311.json`
- baseline 3Y: `local_backtest/reports/alpha_expansion_phase1_baseline_3y_20260311.json`
- EXP-01 1Y: `local_backtest/reports/alpha_expansion_phase1_exp01_quality_score_1y_20260311.json`
- EXP-01 3Y: `local_backtest/reports/alpha_expansion_phase1_exp01_quality_score_3y_20260311.json`
- EXP-02 1Y: `local_backtest/reports/alpha_expansion_phase1_exp02_breakout_efficiency_1y_20260311.json`
- EXP-02 3Y: `local_backtest/reports/alpha_expansion_phase1_exp02_breakout_efficiency_3y_20260311.json`
- EXP-03A 1Y: `local_backtest/reports/alpha_expansion_phase1_exp03a_adx_window_1y_20260311.json`
- EXP-03A 3Y: `local_backtest/reports/alpha_expansion_phase1_exp03a_adx_window_3y_20260311.json`
- EXP-03B 1Y: `local_backtest/reports/alpha_expansion_phase1_exp03b_adx_rising_1y_20260311.json`
- EXP-03B 3Y: `local_backtest/reports/alpha_expansion_phase1_exp03b_adx_rising_3y_20260311.json`
