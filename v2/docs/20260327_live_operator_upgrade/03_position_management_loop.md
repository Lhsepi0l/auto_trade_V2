# 보유 중 재평가 루프

## 1. 왜 추가했나
기존 구조는 진입 후 관리가 대부분
- TP/SL bracket
- trailing watchdog
에 의존했다.

즉, "보유 중 계속 시장을 보며 생각하는" 성격이 약했다.

이번 단계는 그 약점을 줄이기 위해 실거래 런타임에 보유 중 재평가 루프를 넣은 것이다.

## 2. 진입 시 저장되는 정보
진입 성공 시 strategy가 이미 계산해 둔 실행 힌트를 runtime marker에 저장한다.

주요 저장 값:
- `entry_price`
- `stop_price`
- `take_profit_price`
- `risk_per_unit`
- `progress_check_bars`
- `progress_min_mfe_r`
- `progress_extend_trigger_r`
- `progress_extend_bars`
- `reward_risk_reference_r`
- `entry_quality_score_v2`
- `selective_extension_*`
- `tp_partial_ratio`
- `tp_partial_at_r`
- `move_stop_to_be_at_r`

## 3. 현재 루프가 하는 일

### 3.1 progress failed close
- 일정 bar 수까지 (`progress_check_bars`)
- 최소한의 MFE(`progress_min_mfe_r`)를 못 만들면
- 포지션을 강제 종료한다.

의미:
- "들어가긴 했는데 생각보다 너무 못 간다"면 빨리 자른다.

### 3.2 time stop close
- `time_stop_bars`를 넘기면
- 포지션을 강제 종료한다.

의미:
- 오래 끌기만 하는 포지션을 정리한다.

### 3.3 progress extension
- `progress_extend_trigger_r` 이상 가면
- `progress_extend_bars` 만큼 hold window를 늘린다.

의미:
- 잘 가는 포지션은 조금 더 오래 들고 갈 수 있게 한다.

### 3.4 selective extension
- 초반 증거가 좋고
- regime/bias/quality 기준을 만족하면
- 추가 hold window를 적용한다.

### 3.5 breakeven protection
- 일정 `R` 이상 갔다가
- 다시 본전 근처로 밀리면
- 보호 청산한다.

## 4. 이번 단계에서 새로 들어간 것

### 4.1 partial reduce
- 일정 `R` 도달 시
- `reduce-only market order`로 일부 청산한다.
- 현재 기본값은 보수적으로
  - `25%`
  - 약 `1.0 ~ 1.2R`
  기준이다.

### 4.2 partial reduce 후 브래킷 재배치
- 일부 청산 후 남은 수량만큼
- TP/SL bracket을 다시 건다.
- stop은 entry 기준으로 끌어올려 BE 보호 쪽으로 정리한다.

### 4.3 selective extension TP 재배치
- selective extension 조건 충족 시
- 더 먼 TP(`selective_extension_take_profit_r`)로 재배치한다.

### 4.4 signal weakness reduce
- 현재 score가 entry 시점보다 크게 약해졌거나,
- regime/bias strength가 entry 대비 크게 무너졌거나,
- 현재 decision이 `volume_missing`, `trigger_missing`, `quality_score_v2_missing` 같은 약화/차단 상태면
- `25%` reduce-only 청산을 한 번 더 실행한다.

### 4.5 signal weakness 2단계 reduce
- 약화가 stage 1 이후에도 더 심해지면
- 한 번 더 reduce-only 청산을 실행한다.
- 현재 구현은 `weak_reduce_stage`
  - `0 -> 1`
  - `1 -> 2`
  두 단계로 관리한다.
- 2단계 reduce 비율은 alpha / regime 상태에 따라 달라진다.

### 4.6 alpha / regime 역전 기반 강제 청산
- 현재 심볼에서 반대 방향 candidate가 뜨면 `signal_flip_close`
- 현재 `regime_missing` 또는 `bias_missing`로 구조가 무너지면 `regime_bias_lost_close`

### 4.7 runner lock
- partial reduce 이후 이익이 더 커지면
- stop을 단계적으로 끌어올린다.
- 현재 구현은 고정값이 아니라 변동성 버킷 기반이다.
  - 고변동 구간: lock을 더 느슨하게
  - 저변동 구간: lock을 더 타이트하게
  - 중간 변동 구간: 기본 lock
- 이때 남은 수량 기준으로 브래킷 stop을 다시 건다.

## 5. 현재 행동 요약

### HOLD
- 기본 TP/SL 유지
- progress extension 또는 selective extension으로 시간창 연장

### REDUCE
- 일정 `R` 도달 시 partial reduce
- signal weakness 감지 시 추가 reduce

### EXIT
- progress failed
- time stop
- breakeven protection trigger
- signal flip close
- regime / bias lost close
- 기존 TP/SL 또는 trailing

## 6. 현재 상태 평가
이제 runtime은 아래를 실제로 수행한다.

1. entry 후 progress/time stop 기반 조기 종료
2. partial reduce
3. BE protection
4. selective extension과 TP 재배치
5. signal weakness reduce
6. signal weakness 2단계 reduce
7. signal flip / regime-bias lost close
8. volatility-based runner lock 재배치

즉, 이전의 "거의 브래킷/트레일링만 있던 상태"는 넘어섰고, 실전형 `hold / reduce / extend / exit` 루프가 존재한다고 봐도 된다.

## 7. 오늘 번들 기준에서 기대되는 개선 방향
- no-candidate 과잉 차단이 완화되면 entry count가 늘어날 수 있다.
- entry 후에는
  - 초반 실패 포지션은 빨리 정리
  - 잘 가는 포지션은 조금 더 연장
  - 일정 구간에서 일부 익절
  - 본전 재이탈 방지
  - 약해지는 신호는 추가 감속
  - 반대 신호/레짐 붕괴는 강제 종료
가 가능해졌다.

## 8. 아직 남은 것
완전한 의미의 "최종형"으로 보려면 아래가 남아 있다.

1. signal weakness를 1단계가 아니라 2단계(25% -> 50%)로 더 세분화
2. alpha별 / regime별 동적 reduce 비율 차등화
3. volatility 기반 runner lock을 ATR/시장 상태 기반으로 더 세밀하게 조정
4. live 성과 로그를 보고 q070 gate를 한 번 더 튜닝

위 1, 2, 3은 이번 단계에서 1차 구현이 이미 들어갔고, 이후에는 숫자 조정과 성과 검증이 핵심이다.
