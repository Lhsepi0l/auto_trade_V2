# alpha_expansion Phase 2.5 Exit Validation

## 기준

- champion candidate는 `EXP-06 @ quality_score_v2_min=0.70`으로 고정했다.
- 이번 턴은 entry gate / threshold / regime는 유지하고 exit만 검증했다.
- `EXP-07A`는 proved-trend hold extension, `EXP-07B`는 proved-trend TP extension with protection, `EXP-07C`는 stricter selective extension-only validation이다.

## 전체 성과

### 1Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 3.002 | 2.620 | 1.935 | 2.000 | 2.440 | 2.589 | 2.631 | 2.386 | 2.409 | 2.570 | 6.33% | 5.75% | 7.02% | 7.02% | 5.75% | 154 | 133 | 133 | 133 | 133 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 60.95% | 59.40% | 66.34% | 65.61% | 61.02% | 0.0934 | 0.0920 | 0.0786 | 0.0799 | 0.0885 | 1.3005 | 1.2315 | 1.1972 | 1.2086 | 1.2027 |

### 3Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 7.956 | 8.978 | 8.483 | 8.284 | 8.936 | 2.340 | 2.531 | 2.478 | 2.464 | 2.523 | 7.70% | 6.70% | 7.02% | 7.02% | 6.67% | 546 | 469 | 469 | 469 | 469 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 68.40% | 62.05% | 63.45% | 63.73% | 62.21% | 0.0760 | 0.0850 | 0.0827 | 0.0819 | 0.0848 | 1.3583 | 1.3876 | 1.4269 | 1.4521 | 1.4165 |

## 1Y Trend / Chop 분해

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 1.684 | 0.441 | -0.210 | -0.148 | 0.273 | 3.824 | 1.879 | 0.586 | 0.709 | 1.548 | 0.47% | 0.69% | 0.91% | 0.91% | 0.69% | 34 | 29 | 29 | 29 | 29 |
| chop | 3.618 | 4.055 | 4.000 | 4.005 | 4.035 | 2.321 | 2.798 | 2.797 | 2.796 | 2.797 | 4.31% | 4.00% | 3.93% | 3.94% | 3.98% | 120 | 104 | 104 | 104 | 104 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 43.49% | 91.16% | 470.82% | 335.68% | 114.70% | 0.1370 | 0.0416 | -0.0197 | -0.0138 | 0.0257 | 1.2530 | 0.8536 | 0.4076 | 0.4932 | 0.7000 |
| chop | 67.91% | 54.73% | 54.79% | 54.80% | 54.75% | 0.0810 | 0.1061 | 0.1061 | 0.1061 | 0.1061 | 1.3187 | 1.3187 | 1.3187 | 1.3187 | 1.3187 |

## 3Y Stress Test

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | -0.163 | 1.282 | 0.890 | 0.642 | 1.251 | 1.647 | 1.756 | 1.722 | 1.700 | 1.752 | 14.81% | 13.01% | 14.76% | 14.55% | 13.47% | 546 | 469 | 469 | 469 | 469 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| overall | 101.37% | 93.07% | 95.26% | 96.64% | 93.26% | 0.0483 | 0.0546 | 0.0524 | 0.0510 | 0.0544 | 1.2979 | 1.3342 | 1.3746 | 1.3951 | 1.3642 |

## Long / Short 분해

### 1Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 2.749 | 2.455 | 2.267 | 2.250 | 2.261 | 2.833 | 3.198 | 3.052 | 3.034 | 3.033 | 1.94% | 1.79% | 1.77% | 1.78% | 1.79% | 75 | 61 | 61 | 61 | 61 |
| SHORT | 2.553 | 2.041 | 1.522 | 1.606 | 2.047 | 2.390 | 2.244 | 1.935 | 1.985 | 2.254 | 2.42% | 2.29% | 3.24% | 3.24% | 2.28% | 79 | 72 | 72 | 72 | 72 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 59.34% | 52.80% | 55.53% | 55.88% | 55.89% | 0.1001 | 0.1105 | 0.1030 | 0.1021 | 0.1021 | 1.2852 | 1.2202 | 1.1627 | 1.1560 | 1.1560 |
| SHORT | 62.68% | 66.79% | 80.19% | 77.59% | 66.42% | 0.0870 | 0.0763 | 0.0580 | 0.0611 | 0.0771 | 1.3157 | 1.2428 | 1.2367 | 1.2688 | 1.2494 |

### 3Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 11.571 | 12.231 | 12.399 | 12.021 | 12.232 | 2.704 | 3.113 | 3.133 | 3.081 | 3.107 | 2.45% | 2.53% | 2.53% | 2.53% | 2.53% | 300 | 257 | 257 | 257 | 257 |
| SHORT | 4.902 | 4.011 | 3.369 | 3.462 | 3.983 | 1.892 | 1.832 | 1.694 | 1.721 | 1.823 | 3.65% | 3.99% | 4.03% | 3.99% | 4.01% | 246 | 212 | 212 | 212 | 212 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LONG | 58.69% | 49.92% | 49.55% | 50.25% | 50.00% | 0.0990 | 0.1199 | 0.1215 | 0.1188 | 0.1199 | 1.4116 | 1.4407 | 1.4537 | 1.4826 | 1.4411 |
| SHORT | 86.96% | 89.65% | 98.58% | 96.62% | 90.09% | 0.0479 | 0.0427 | 0.0358 | 0.0371 | 0.0423 | 1.2703 | 1.2847 | 1.3676 | 1.3867 | 1.3656 |

## Volatility 분해

### 1Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 2.834 | 2.653 | 2.456 | 2.440 | 2.457 | 2.384 | 2.708 | 2.601 | 2.588 | 2.590 | 3.05% | 2.76% | 2.73% | 2.73% | 2.76% | 67 | 51 | 51 | 51 | 51 |
| mid_vol | 1.548 | 0.926 | 0.915 | 0.916 | 0.919 | 2.850 | 2.224 | 2.227 | 2.227 | 2.221 | 1.62% | 1.70% | 1.67% | 1.67% | 1.69% | 47 | 42 | 42 | 42 | 42 |
| high_vol | 0.920 | 0.917 | 0.418 | 0.501 | 0.931 | 3.033 | 3.050 | 1.922 | 2.103 | 3.090 | 0.43% | 0.45% | 0.83% | 0.83% | 0.44% | 40 | 40 | 40 | 40 | 40 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 64.86% | 54.77% | 57.50% | 57.83% | 57.74% | 0.1160 | 0.1433 | 0.1343 | 0.1332 | 0.1332 | 1.4806 | 1.4400 | 1.3825 | 1.3758 | 1.3758 |
| mid_vol | 57.70% | 75.50% | 75.40% | 75.44% | 75.61% | 0.0879 | 0.0583 | 0.0583 | 0.0583 | 0.0583 | 1.2793 | 1.1244 | 1.1244 | 1.1244 | 1.1244 |
| high_vol | 53.80% | 53.50% | 88.32% | 79.60% | 52.74% | 0.0619 | 0.0620 | 0.0290 | 0.0346 | 0.0633 | 0.9215 | 0.9215 | 0.8002 | 0.8751 | 0.9347 |

### 3Y

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 5.720 | 3.757 | 3.547 | 2.651 | 3.536 | 1.917 | 1.758 | 1.714 | 1.536 | 1.712 | 3.37% | 5.26% | 5.30% | 5.37% | 5.28% | 200 | 152 | 152 | 152 | 152 |
| mid_vol | 5.692 | 7.300 | 7.656 | 8.094 | 7.513 | 2.623 | 3.301 | 3.407 | 3.578 | 3.363 | 2.01% | 1.69% | 1.65% | 1.63% | 1.67% | 176 | 158 | 158 | 158 | 158 |
| high_vol | 5.062 | 5.186 | 4.565 | 4.738 | 5.165 | 2.991 | 3.087 | 2.811 | 2.900 | 3.064 | 2.47% | 2.61% | 2.67% | 2.61% | 2.63% | 170 | 159 | 159 | 159 | 159 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low_vol | 87.11% | 95.18% | 98.23% | 112.46% | 98.34% | 0.0716 | 0.0580 | 0.0550 | 0.0397 | 0.0547 | 1.4910 | 1.5892 | 1.5538 | 1.4964 | 1.5497 |
| mid_vol | 62.28% | 49.29% | 47.64% | 45.20% | 48.30% | 0.0805 | 0.1156 | 0.1211 | 0.1293 | 0.1189 | 1.4309 | 1.4370 | 1.4849 | 1.5575 | 1.4662 |
| high_vol | 47.72% | 45.63% | 50.44% | 48.60% | 45.86% | 0.0765 | 0.0804 | 0.0712 | 0.0751 | 0.0798 | 1.1339 | 1.1681 | 1.2344 | 1.2786 | 1.2413 |

## Trend / Chop 분해 (3Y)

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 4.745 | 5.304 | 4.823 | 4.645 | 5.262 | 2.690 | 3.297 | 3.058 | 2.977 | 3.263 | 2.25% | 1.58% | 1.58% | 1.58% | 1.58% | 143 | 123 | 123 | 123 | 123 |
| chop | 11.729 | 10.939 | 10.945 | 10.838 | 10.953 | 2.237 | 2.317 | 2.315 | 2.318 | 2.316 | 3.98% | 3.97% | 3.93% | 3.92% | 3.97% | 403 | 346 | 346 | 346 | 346 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| trend | 61.28% | 49.83% | 53.62% | 54.65% | 50.21% | 0.0820 | 0.1078 | 0.0991 | 0.0958 | 0.1071 | 1.3409 | 1.3445 | 1.4848 | 1.5908 | 1.4434 |
| chop | 71.10% | 67.32% | 67.41% | 67.32% | 67.35% | 0.0739 | 0.0769 | 0.0769 | 0.0769 | 0.0769 | 1.3645 | 1.4058 | 1.4058 | 1.4058 | 1.4058 |

## 연도별 성과 분해 (3Y)

| slice | baseline net | champion net | EXP-07A net | EXP-07B net | EXP-07C net | baseline PF | champion PF | EXP-07A PF | EXP-07B PF | EXP-07C PF | baseline MDD | champion MDD | EXP-07A MDD | EXP-07B MDD | EXP-07C MDD | baseline trades | champion trades | EXP-07A trades | EXP-07B trades | EXP-07C trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Year 1<br>2023-03-12 ~ 2024-03-12 | 3.198 | 4.170 | 4.420 | 4.019 | 4.306 | 2.243 | 3.113 | 3.227 | 3.003 | 3.170 | 1.68% | 1.16% | 1.16% | 1.16% | 1.16% | 126 | 101 | 101 | 101 | 101 |
| Year 2<br>2024-03-12 ~ 2025-03-12 | 7.984 | 7.480 | 7.568 | 7.630 | 7.521 | 2.573 | 2.639 | 2.645 | 2.690 | 2.640 | 2.41% | 2.78% | 2.72% | 2.74% | 2.76% | 248 | 219 | 219 | 219 | 219 |
| Year 3<br>2025-03-12 ~ 2026-03-11 | 5.292 | 4.593 | 3.780 | 3.834 | 4.387 | 2.140 | 2.127 | 1.927 | 1.945 | 2.076 | 4.11% | 3.86% | 5.44% | 5.40% | 3.88% | 172 | 149 | 149 | 149 | 149 |

| slice | baseline fee | champion fee | EXP-07A fee | EXP-07B fee | EXP-07C fee | baseline expectancy | champion expectancy | EXP-07A expectancy | EXP-07B expectancy | EXP-07C expectancy | baseline avg winner | champion avg winner | EXP-07A avg winner | EXP-07B avg winner | EXP-07C avg winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Year 1<br>2023-03-12 ~ 2024-03-12 | 74.18% | 52.11% | 49.90% | 53.31% | 50.87% | 0.0700 | 0.1112 | 0.1176 | 0.1078 | 0.1147 | 1.4331 | 1.4949 | 1.7118 | 1.8018 | 1.6826 |
| Year 2<br>2024-03-12 ~ 2025-03-12 | 63.53% | 61.36% | 61.20% | 60.05% | 61.31% | 0.0815 | 0.0830 | 0.0833 | 0.0852 | 0.0831 | 1.3674 | 1.4534 | 1.4567 | 1.4773 | 1.4544 |
| Year 3<br>2025-03-12 ~ 2026-03-11 | 71.81% | 71.14% | 80.38% | 79.39% | 73.30% | 0.0725 | 0.0702 | 0.0582 | 0.0594 | 0.0671 | 1.3005 | 1.2315 | 1.1972 | 1.2086 | 1.2027 |

## Selective Extension 진단

| window | champion activation | EXP-07A activation | EXP-07B activation | EXP-07C activation | champion tp-extension | EXP-07A tp-extension | EXP-07B tp-extension | EXP-07C tp-extension | champion protection | EXP-07A protection | EXP-07B protection | EXP-07C protection | champion TP rate | EXP-07A TP rate | EXP-07B TP rate | EXP-07C TP rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1Y | 0.00% | 2.26% | 2.26% | 1.50% | 0.00% | 0.00% | 2.26% | 0.00% | 0.00% | 0.00% | 2.26% | 0.00% | 4.51% | 4.51% | 4.51% | 4.51% |
| 3Y | 0.00% | 4.05% | 4.05% | 2.77% | 0.00% | 0.00% | 4.05% | 0.00% | 0.00% | 0.00% | 4.05% | 0.00% | 5.33% | 5.33% | 4.90% | 5.33% |

## 해석

- `EXP-07A`가 겨냥한 실패 모드는 `초기 proof를 만든 trend trade가 고정된 time_stop=18 때문에 너무 일찍 잘리는 문제`였다. 하지만 결과는 반대였다. 1Y trend slice가 `net 0.441 -> -0.210`, `PF 1.879 -> 0.586`으로 더 악화됐고, 3Y 전체도 `8.978 -> 8.483`, 3Y stress도 `1.282 -> 0.890`으로 후퇴했다. hold extension 자체가 trend 약점을 고치지 못했다.
- `EXP-07B`는 같은 proof 조건에 `TP 소폭 확장 + BE protection`을 추가해 stress 훼손 없이 runner를 더 살리는 가설이었다. 하지만 1Y trend slice는 여전히 `net -0.148`, `PF 0.709`로 무너졌고, 3Y 전체 `8.284`, 3Y stress `0.642`로 `EXP-07A`보다도 더 약했다. protection을 붙여도 현재 selective extension coverage로는 질적 개선이 나오지 않았다.
- `EXP-07C`는 `generic extension이 너무 넓거나 너무 둔한 것 아닌가`를 확인하기 위한 stricter extension-only validation이었다. 이 변형이 셋 중 가장 덜 나빴지만 champion을 넘지는 못했다. 1Y 전체 `2.620 -> 2.440`, 1Y trend `0.441 -> 0.273`, 3Y 전체 `8.978 -> 8.936`, 3Y stress `1.282 -> 1.251`로 모두 소폭 열세였다.
- 공통 진단은 명확하다. selective extension activation 비중이 1Y `1.50%~2.26%`, 3Y `2.77%~4.05%`에 그쳤다. 즉 `trend proof가 있는 소수 trade만 연장`한다는 방향 자체는 규율 면에서 맞지만, 현재 proof/context 정의로는 최근 1Y trend 약점을 치유할 만큼 충분한 영향을 주지 못한다.
- 1Y slice를 보면 문제는 더 분명하다. champion은 이미 `chop`에서 `baseline`보다 좋아졌고, 약점은 `trend`에 집중돼 있다. 그런데 `07A/B/C`는 이 trend slice를 회복하지 못했고, 대신 `SHORT`, `high_vol`, stress robustness까지 흔들었다. 이건 `exit extension`이 현재 병목의 중심축이 아니라는 신호에 가깝다.
- 3Y 관점에서도 champion이 여전히 가장 균형적이다. `EXP-07C`가 3Y 전체와 stress에서 가장 근접했지만, `PF`, `MDD`, fee efficiency, expectancy를 같이 보면 champion을 이길 정도의 추가 설득력은 없었다.

## Verdict

- `EXP-06 @ 0.70` 유지가 더 적절
- 이유 1: 핵심 목표였던 `최근 1Y trend slice 회복`을 어떤 selective extension도 달성하지 못했다.
- 이유 2: 3Y 전체, 3Y stress, fee efficiency, expectancy를 함께 보면 champion이 여전히 가장 균형 잡힌 후보다.
- 이유 3: 이번 결과는 `exit 축이 완전 불가능`하다는 뜻은 아니지만, 현재의 selective extension 계열은 champion replacement가 아니다.
