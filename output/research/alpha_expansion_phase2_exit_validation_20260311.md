# alpha_expansion Phase 2 Exit Validation

## 기준

- entry gate, breakout score, regime filter는 그대로 두고 exit만 바꿨다.
- baseline, `EXP-06 @ 0.70`, `EXP-07`, `EXP-08`를 동일 고정 창에서 비교했다.
- `EXP-07`은 progress-aware time stop, `EXP-08`은 quality-conditioned exit이다.

## 전체 성과

### 1Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 3.002 | 2.620 | 2.399 | 0.885 | 2.589 | 2.631 | 2.572 | 1.984 | 6.33% | 5.75% | 6.05% | 7.02% | 154 | 133 | 133 | 132 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 60.95% | 59.40% | 61.46% | 81.13% | 0.0934 | 0.0920 | 0.0876 | 0.0579 |

### 3Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 7.956 | 8.978 | 9.359 | 6.816 | 2.340 | 2.531 | 2.570 | 2.311 | 7.70% | 6.70% | 6.09% | 9.01% | 546 | 469 | 468 | 467 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 68.40% | 62.05% | 61.35% | 68.16% | 0.0760 | 0.0850 | 0.0869 | 0.0752 |

## 연도별 성과 분해 (3Y)

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Year 1<br>2023-03-12 ~ 2024-03-12 | 3.198 | 4.170 | 4.506 | 4.811 | 2.243 | 3.113 | 3.286 | 3.379 | 1.68% | 1.16% | 1.16% | 1.16% | 126 | 101 | 100 | 100 |
| Year 2<br>2024-03-12 ~ 2025-03-12 | 7.984 | 7.480 | 7.815 | 6.795 | 2.573 | 2.639 | 2.705 | 2.461 | 2.41% | 2.78% | 2.48% | 2.80% | 248 | 219 | 219 | 219 |
| Year 3<br>2025-03-12 ~ 2026-03-11 | 5.292 | 4.593 | 4.392 | 2.413 | 2.140 | 2.127 | 2.074 | 1.600 | 4.11% | 3.86% | 4.29% | 5.42% | 172 | 149 | 149 | 148 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Year 1<br>2023-03-12 ~ 2024-03-12 | 74.18% | 52.11% | 49.16% | 46.88% | 0.0700 | 0.1112 | 0.1207 | 0.1287 |
| Year 2<br>2024-03-12 ~ 2025-03-12 | 63.53% | 61.36% | 60.23% | 66.07% | 0.0815 | 0.0830 | 0.0855 | 0.0748 |
| Year 3<br>2025-03-12 ~ 2026-03-11 | 71.81% | 71.14% | 73.86% | 101.10% | 0.0725 | 0.0702 | 0.0663 | 0.0396 |

## Trend / Chop 분해

### 1Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 1.684 | 0.441 | 0.358 | -0.143 | 3.824 | 1.879 | 1.716 | 0.710 | 0.47% | 0.69% | 0.69% | 0.87% | 34 | 29 | 29 | 29 |
| chop | 3.618 | 4.055 | 3.910 | 2.820 | 2.321 | 2.798 | 2.765 | 2.267 | 4.31% | 4.00% | 3.99% | 3.94% | 120 | 104 | 104 | 103 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 43.49% | 91.16% | 101.39% | 334.78% | 0.1370 | 0.0416 | 0.0340 | -0.0138 |
| chop | 67.91% | 54.73% | 56.06% | 68.32% | 0.0810 | 0.1061 | 0.1026 | 0.0781 |

### 3Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 4.745 | 5.304 | 5.271 | 4.499 | 2.690 | 3.297 | 3.252 | 2.902 | 2.25% | 1.58% | 1.60% | 1.61% | 143 | 123 | 123 | 123 |
| chop | 11.729 | 10.939 | 11.442 | 9.519 | 2.237 | 2.317 | 2.378 | 2.143 | 3.98% | 3.97% | 3.97% | 4.13% | 403 | 346 | 345 | 344 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 61.28% | 49.83% | 50.57% | 56.19% | 0.0820 | 0.1078 | 0.1060 | 0.0916 |
| chop | 71.10% | 67.32% | 65.84% | 73.19% | 0.0739 | 0.0769 | 0.0801 | 0.0693 |

## Long / Short 분해

### 1Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 2.749 | 2.455 | 2.263 | 1.216 | 2.833 | 3.198 | 3.082 | 2.086 | 1.94% | 1.79% | 1.79% | 1.77% | 75 | 61 | 61 | 60 |
| SHORT | 2.553 | 2.041 | 2.004 | 1.461 | 2.390 | 2.244 | 2.232 | 1.913 | 2.42% | 2.29% | 2.29% | 3.20% | 79 | 72 | 72 | 72 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 59.34% | 52.80% | 55.89% | 81.63% | 0.1001 | 0.1105 | 0.1019 | 0.0578 |
| SHORT | 62.68% | 66.79% | 67.42% | 80.70% | 0.0870 | 0.0763 | 0.0755 | 0.0580 |

### 3Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 11.571 | 12.231 | 12.419 | 10.572 | 2.704 | 3.113 | 3.155 | 2.798 | 2.45% | 2.53% | 2.55% | 3.07% | 300 | 257 | 256 | 255 |
| SHORT | 4.902 | 4.011 | 4.294 | 3.446 | 1.892 | 1.832 | 1.880 | 1.716 | 3.65% | 3.99% | 4.03% | 4.05% | 246 | 212 | 212 | 212 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 58.69% | 49.92% | 49.77% | 55.30% | 0.0990 | 0.1199 | 0.1211 | 0.1061 |
| SHORT | 86.96% | 89.65% | 86.91% | 96.77% | 0.0479 | 0.0427 | 0.0455 | 0.0380 |

## Fee / Slippage Stress Test

- stress 실행 모델: fee_bps=5.0, slippage_bps=3.0, funding_bps_8h=0.5

### 1Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 1.139 | 1.102 | 0.868 | -0.517 | 1.938 | 1.980 | 1.924 | 1.485 | 8.12% | 7.38% | 7.68% | 9.47% | 154 | 133 | 133 | 132 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 83.74% | 81.42% | 84.93% | 115.48% | 0.0714 | 0.0711 | 0.0663 | 0.0376 |

### 3Y

| slice | baseline net | champion net | EXP-07 net | EXP-08 net | baseline PF | champion PF | EXP-07 PF | EXP-08 PF | baseline MDD | champion MDD | EXP-07 MDD | EXP-08 MDD | baseline trades | champion trades | EXP-07 trades | EXP-08 trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | -0.163 | 1.282 | 0.885 | -0.508 | 1.647 | 1.756 | 1.729 | 1.599 | 14.81% | 13.01% | 13.85% | 19.23% | 546 | 469 | 469 | 468 |

| slice | baseline fee | champion fee | EXP-07 fee | EXP-08 fee | baseline expectancy | champion expectancy | EXP-07 expectancy | EXP-08 expectancy |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 101.37% | 93.07% | 95.26% | 103.93% | 0.0483 | 0.0546 | 0.0523 | 0.0443 |

## 실험 진단

| window | champion progress cut | EXP-07 progress cut | champion quality exit | EXP-08 quality exit | champion take-profit rate | EXP-07 take-profit rate | EXP-08 take-profit rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1Y | 0.00% | 1.50% | 0.00% | 89.39% | 4.51% | 4.51% | 3.03% |
| 3Y | 0.00% | 1.50% | 0.00% | 89.72% | 5.33% | 5.77% | 4.07% |

## 해석

- `EXP-07`은 겨냥한 실패 모드 자체는 틀리지 않았다. `progress-aware time stop`이 들어가자 3Y 전체는 `net 8.978 -> 9.359`, `PF 2.531 -> 2.570`, `MDD 6.70% -> 6.09%`로 개선됐다.
- 하지만 최근 1Y trend slice는 회복하지 못했다. `trend net 0.441 -> 0.358`, `PF 1.879 -> 1.716`으로 오히려 더 약해졌다.
- 비용 스트레스에서도 champion보다 낫지 않았다. 3Y stress는 `net 1.282 -> 0.885`, `PF 1.756 -> 1.729`, `MDD 13.01% -> 13.85%`로 후퇴했다.
- 진단 테이블상 `EXP-07`의 `progress_time_stop` 발동 비중은 1Y/3Y 모두 `1.50%`에 그쳤다. 즉 방향은 완전히 틀리지 않았지만, 현재 파라미터는 최근 약점을 치료하기엔 영향력이 너무 약하다.
- `EXP-08`은 명확히 잘못된 방향이었다. `quality_exit_applied`가 1Y/3Y 모두 약 `89%`에 달해 사실상 거의 전 구간 exit 재작성으로 작동했고, 1Y/3Y/stress 전부를 동시에 악화시켰다.
- 특히 `EXP-08`은 1Y 전체 `net 2.620 -> 0.885`, 3Y 전체 `8.978 -> 6.816`, 3Y stress `1.282 -> -0.508`, `MDD 13.01% -> 19.23%`로 무너졌다. 보수적 2-tier라기보다 지나치게 넓은 high-quality 판정으로 해석하는 게 맞다.

## Verdict

`추가 보완 필요`

- 이유 1: exit 축은 완전 plateau가 아니다. `EXP-07`이 3Y 전체와 3Y trend/chop 일부에서 개선을 만들었기 때문이다.
- 이유 2: 하지만 이번 구현은 핵심 목표였던 `최근 1Y trend slice 회복`을 달성하지 못했다.
- 이유 3: champion 대비 stress robustness를 보존하지 못했다. 실전 후보 승격 관점에서는 이 점이 치명적이다.
- 이유 4: `EXP-08`은 구조적으로 실패했다. 다음 턴에서 그대로 확장할 가치가 없다.

다음 단계 권고:

- `EXP-07` 계열만 남기고 재설계한다.
- 방향은 `더 자주 자르기`보다 `trend slice에서만 더 오래 보유할 trade를 더 정확히 분리`하는 쪽이 맞다.
- 이번 턴에서 효과가 약했던 이유는 exit 로직이 실제로 건드린 trade 비중이 너무 낮았기 때문이다. 다음 실험은 `progress trigger` 자체보다 `extension activation 조건`과 `trend 전용 적용 범위`를 더 날카롭게 설계해야 한다.
- `EXP-08`의 현재 형태는 폐기한다. 같은 아이디어를 다시 쓰려면 `quality_exit_score_threshold`를 훨씬 높이거나, 반대로 `low-quality cut` 위주로 완전히 재설계해야 한다.
