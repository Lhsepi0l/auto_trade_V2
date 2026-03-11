# alpha_expansion EXP-06 Slice Validation

## 날카로운 진단

- EXP-06의 `penalty-based breakout structure quality` 아이디어 자체는 살아 있다. `3Y 전체`, `fee/slippage stress`, `mid/high vol`, `trend`, `long quality`에서 baseline보다 낫다.
- 하지만 현재 champion threshold인 `quality_score_v2_min=0.72`는 아직 최종 승격값이 아니다. 민감도 결과가 `1Y는 0.74`, `3Y는 0.70` 쪽으로 더 좋게 움직였고, 현재값은 안정 plateau 중심이라기보다 중간값에 가깝다.
- 1Y net 저하는 전체 구조 실패라기보다 `최근 trend 구간 언더캡처`, `short side 열화`, `low-vol bucket 약화`가 겹친 결과다. 즉 trade-quality 개선의 대가가 전부 허용 가능한 것은 아니다.
- 따라서 지금 단계의 판정은 `구조 검증 통과, 기본값 승격 보류`가 맞다.

## 판단 기준

- 승격 가능으로 보려면 `3Y 전체 우위`뿐 아니라 `1Y 최근 구간에서의 과도한 trend 캡처 손실`이 없어야 한다.
- 특정 slice의 raw net이 약간 낮아지는 것은 허용할 수 있지만, 그 대가로 `PF / expectancy / fee efficiency / MDD`가 함께 좋아져야 한다.
- threshold 민감도는 `현재값 근처에서 완만한 plateau`여야 한다. 바로 옆 값이 더 낫다면 아직 champion 고정 단계가 아니다.

## 기준선

- baseline은 `quality_score_v2_min=0.00`이고, EXP-06은 `quality_score_v2_min=0.72`이다.
- 해석 우선순위는 `raw net`보다 `PF / MDD / expectancy / fee efficiency / slice consistency`다.
- `volatility regime`는 `initial_risk_abs / quantity / entry_price`를 진입 시점 상대 변동성 프록시로 쪼갰다.
- `trend/chop`은 저장된 `regime` 필드를 사용해 `TREND_UP/TREND_DOWN`을 trend, 나머지를 chop으로 묶었다.

## 전체 성과

### 1Y

| 지표 | baseline | EXP-06 |
| --- | ---: | ---: |
| net | 3.002 | 2.696 |
| PF | 2.589 | 2.673 |
| MDD | 6.33% | 5.53% |
| trades | 154 | 131 |
| fee efficiency | 60.95% | 58.34% |
| expectancy | 0.0934 | 0.0943 |
| avg winner | 1.3005 | 1.2315 |
| avg loser | -0.0664 | -0.0639 |
| win rate | 11.69% | 12.21% |

### 3Y

| 지표 | baseline | EXP-06 |
| --- | ---: | ---: |
| net | 7.956 | 8.416 |
| PF | 2.340 | 2.499 |
| MDD | 7.70% | 6.70% |
| trades | 546 | 463 |
| fee efficiency | 68.40% | 62.82% |
| expectancy | 0.0760 | 0.0829 |
| avg winner | 1.3583 | 1.3784 |
| avg loser | -0.0618 | -0.0600 |
| win rate | 9.71% | 9.94% |

## 연도별 성과 분해 (3Y)

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Year 1<br>2023-03-11 ~ 2024-03-11 | 3.198 | 3.493 | 2.243 | 2.837 | 1.68% | 1.37% | 74.18% | 57.10% | 0.0700 | 0.0972 | 126 | 98 |
| Year 2<br>2024-03-11 ~ 2025-03-11 | 8.011 | 7.383 | 2.587 | 2.661 | 2.41% | 2.73% | 63.17% | 60.82% | 0.0821 | 0.0838 | 247 | 218 |
| Year 3<br>2025-03-11 ~ 2026-03-10 | 5.265 | 4.573 | 2.128 | 2.155 | 4.12% | 3.65% | 72.34% | 69.95% | 0.0717 | 0.0719 | 173 | 147 |

## Long / Short 분해

### 1Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 2.749 | 2.486 | 2.833 | 3.281 | 1.94% | 1.71% | 59.34% | 51.52% | 0.1001 | 0.1136 | 75 | 60 |
| SHORT | 2.553 | 2.059 | 2.390 | 2.266 | 2.42% | 2.25% | 62.68% | 65.98% | 0.0870 | 0.0780 | 79 | 71 |

### 3Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 11.571 | 11.447 | 2.704 | 3.037 | 2.45% | 2.54% | 58.69% | 51.11% | 0.0990 | 0.1148 | 300 | 254 |
| SHORT | 4.902 | 4.002 | 1.892 | 1.854 | 3.65% | 3.91% | 86.96% | 88.32% | 0.0479 | 0.0441 | 246 | 209 |

## Volatility Regime 분해

- 1Y bucket 경계: low <= 0.00874070, mid <= 0.01220278
- 3Y bucket 경계: low <= 0.00907074, mid <= 0.01304461

### 1Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 1.675 | 1.492 | 1.931 | 2.123 | 2.58% | 2.12% | 82.38% | 71.30% | 0.0857 | 0.1046 | 55 | 40 |
| mid_vol | 2.090 | 1.493 | 3.178 | 2.761 | 1.82% | 2.12% | 51.80% | 60.24% | 0.1103 | 0.0895 | 51 | 45 |
| high_vol | 1.537 | 1.559 | 3.663 | 3.890 | 0.60% | 0.57% | 44.07% | 41.56% | 0.0843 | 0.0899 | 48 | 46 |

### 3Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 5.910 | 3.256 | 1.977 | 1.698 | 3.21% | 4.98% | 84.05% | 99.12% | 0.0766 | 0.0521 | 193 | 144 |
| mid_vol | 5.575 | 7.140 | 2.538 | 3.263 | 2.09% | 1.82% | 64.47% | 49.86% | 0.0776 | 0.1150 | 179 | 158 |
| high_vol | 4.990 | 5.052 | 2.908 | 3.036 | 2.47% | 2.58% | 49.37% | 46.57% | 0.0737 | 0.0788 | 174 | 161 |

## Trend / Chop 분해

### 1Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 1.684 | 0.440 | 3.824 | 1.878 | 0.47% | 0.69% | 43.49% | 91.22% | 0.1370 | 0.0416 | 34 | 29 |
| chop | 3.618 | 4.104 | 2.321 | 2.853 | 4.31% | 3.87% | 67.91% | 53.53% | 0.0810 | 0.1093 | 120 | 102 |

### 3Y

| slice | baseline net | EXP-06 net | baseline PF | EXP-06 PF | baseline MDD | EXP-06 MDD | baseline fee | EXP-06 fee | baseline expectancy | EXP-06 expectancy | baseline trades | EXP-06 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 4.745 | 5.240 | 2.690 | 3.346 | 2.25% | 1.55% | 61.28% | 49.11% | 0.0820 | 0.1094 | 143 | 122 |
| chop | 11.729 | 10.209 | 2.237 | 2.265 | 3.98% | 3.85% | 71.10% | 68.98% | 0.0739 | 0.0734 | 403 | 341 |

## Fee / Slippage Stress Test

- stress 실행 모델: fee_bps=5.0, slippage_bps=3.0, funding_bps_8h=0.5

### 1Y

| 지표 | baseline | EXP-06 |
| --- | ---: | ---: |
| net | 1.139 | 1.195 |
| PF | 1.938 | 2.013 |
| MDD | 8.12% | 7.11% |
| trades | 154 | 131 |
| fee efficiency | 83.74% | 79.86% |
| expectancy | 0.0714 | 0.0734 |
| avg winner | 1.2547 | 1.1873 |
| avg loser | -0.0852 | -0.0816 |
| win rate | 11.69% | 12.21% |

### 3Y

| 지표 | baseline | EXP-06 |
| --- | ---: | ---: |
| net | -0.163 | 0.918 |
| PF | 1.647 | 1.726 |
| MDD | 14.81% | 12.56% |
| trades | 546 | 463 |
| fee efficiency | 101.37% | 94.94% |
| expectancy | 0.0483 | 0.0524 |
| avg winner | 1.2979 | 1.3249 |
| avg loser | -0.0804 | -0.0778 |
| win rate | 9.34% | 9.29% |

## Threshold 민감도

### 1Y

| quality_score_v2_min | net | PF | MDD | trades | fee efficiency | expectancy |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.68 | 2.386 | 2.511 | 5.75% | 138 | 62.60% | 0.0861 |
| 0.70 | 2.620 | 2.631 | 5.75% | 133 | 59.40% | 0.0920 |
| 0.72 | 2.696 | 2.673 | 5.53% | 131 | 58.34% | 0.0943 |
| 0.74 | 2.747 | 2.701 | 5.53% | 130 | 57.65% | 0.0956 |

### 3Y

| quality_score_v2_min | net | PF | MDD | trades | fee efficiency | expectancy |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.68 | 6.715 | 2.312 | 7.00% | 484 | 68.70% | 0.0735 |
| 0.70 | 8.978 | 2.531 | 6.70% | 469 | 62.05% | 0.0850 |
| 0.72 | 8.416 | 2.499 | 6.70% | 463 | 62.82% | 0.0829 |
| 0.74 | 7.123 | 2.393 | 6.55% | 458 | 65.94% | 0.0770 |

## 해석

### 연도별 성과 분해

- `Year 1`은 EXP-06이 명확한 개선이다. net, PF, MDD, fee efficiency, expectancy가 모두 좋아졌다.
- `Year 2`는 net이 줄었지만 PF와 expectancy는 소폭 개선됐다. 다만 MDD가 약간 나빠져 완전한 승리는 아니다.
- `Year 3`는 최근 1Y와 같은 방향이다. net은 감소했지만 PF, MDD, fee efficiency는 개선됐다.
- 따라서 `3Y 개선이 특정 한 해 착시`는 아니다. 다만 모든 해에서 net까지 동시에 개선된 것은 아니고, 최근 구간은 `질 개선 vs 순이익 감소`의 트레이드오프가 분명하다.

### Long / Short 분해

- `LONG`은 1Y와 3Y 모두 trade-quality 개선이 분명하다. 특히 3Y LONG은 net을 거의 유지하면서 PF, expectancy, fee efficiency가 눈에 띄게 좋아졌다.
- `SHORT`는 1Y와 3Y 모두 약하다. net, PF, expectancy, fee efficiency가 baseline보다 나빠졌다.
- 즉 EXP-06의 주된 개선은 `LONG side purification`이고, 현재 약점은 `SHORT side degradation`이다.

### Volatility Regime 분해

- `mid/high vol`에서는 EXP-06이 강하다. 3Y 기준 mid/high vol은 net, PF, expectancy, fee efficiency가 모두 좋아졌다.
- 반대로 `low_vol`에서는 뚜렷하게 나쁘다. 3Y low_vol은 net, PF, expectancy, MDD, fee efficiency가 전부 악화됐다.
- 이 말은 현재 penalty gate가 `좋지 않은 breakout`을 잘 제거하는 대신, `조용한 변동성 구간의 정상 breakout`까지 지나치게 잘라낼 가능성이 있다는 뜻이다.

### Trend / Chop 분해

- `3Y trend`는 분명한 개선이다. net, PF, expectancy, MDD, fee efficiency가 모두 좋아졌다.
- `3Y chop`은 net이 줄었지만 PF와 MDD는 소폭 개선됐다. 구조적으로 나쁜 그림은 아니다.
- 문제는 `1Y trend`다. net이 `1.684 -> 0.440`, PF가 `3.824 -> 1.878`, expectancy가 `0.1370 -> 0.0416`으로 크게 훼손됐다.
- 반면 `1Y chop`은 크게 개선됐다. 따라서 최근 1Y net 저하는 `최근 trend 캡처 실패`가 핵심 원인이다.

### Fee / Slippage Stress Test

- 비용 스트레스에서는 EXP-06이 baseline보다 명확히 강하다.
- 1Y stress에서 net, PF, MDD, fee efficiency, expectancy가 모두 우세하다.
- 3Y stress에서는 baseline이 음수로 무너지는 반면, EXP-06은 여전히 양수 net과 더 낮은 MDD를 유지한다.
- 따라서 EXP-06은 `비용 내성` 측면에서 robust candidate로 볼 근거가 충분하다.

### Threshold 민감도

- 1Y는 `0.68 -> 0.70 -> 0.72 -> 0.74`로 갈수록 거의 단조롭게 개선된다.
- 3Y는 `0.70`이 최고이고, `0.72`는 그보다 한 단계 낮다. `0.74`로 가면 오히려 확실히 나빠진다.
- 즉 현재 `0.72`는 나쁜 값이 아니지만, `안정 중심값`으로 확정할 정도의 plateau는 아니다.
- 현재 데이터가 말하는 더 정확한 해석은 `구조는 맞고, threshold 중심점은 아직 덜 맞았다`이다.

## 최종 Verdict

`candidate 유지, 추가 보완 후 재검증`

이유는 세 가지다.

- EXP-06 구조 자체는 robust하다. 3Y 전체, 비용 스트레스, trend/mid-high-vol/long quality 측면에서 baseline 대비 개선이 분명하다.
- 그러나 현재 `quality_score_v2_min=0.72`는 최종 champion 값이 아니다. 민감도에서 `0.70`이 3Y 전체 우위이고, `0.74`가 1Y 우위라 현재값 고정 근거가 부족하다.
- 최근 1Y의 `trend`와 `short`, 그리고 3Y `low_vol` 열화가 아직 남아 있다. 즉 기본값 승격 전 마지막 보정 없이 들어가면 slice asymmetry를 안고 가게 된다.

권고 순서는 이렇다.

1. 다음 검증은 로직 추가가 아니라 `quality_score_v2_min` 재중심화다. 우선 `0.70`을 새 challenger로 올리고 동일한 slice validation을 한 번 더 반복한다.
2. 그 뒤에도 `1Y trend / short / low_vol` 약점이 남으면, 다음 실험은 exit 변경이 아니라 `short-side 또는 low-vol 예외 처리`처럼 더 좁은 구조 보정으로 가야 한다.
3. 그 전까지는 EXP-06을 `최상위 candidate`로만 유지하고, `default/profile 승격`은 보류한다.
