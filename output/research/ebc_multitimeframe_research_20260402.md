# EBC Multi-Timeframe Research

## 목적
- 기존 `alpha_expansion` / `alpha_drift` 계열 미세조정 루프를 중단하고, 더 단순한 지표 조합으로 새 family를 연구한다.
- 사용 지표를 **3개로 제한**한다.
  - `EMA`
  - `Bollinger Bands`
  - `CCI`
- 사용 시간봉을 **4개로 제한**한다.
  - `5m`
  - `30m`
  - `2h`
  - `12h`

## 왜 이 주제로 가나
- 지금까지의 실패는 대부분 “지표가 너무 많아서”라기보다, **검문소가 너무 많고 알파 정의가 너무 복잡해진 것**에서 나왔다.
- `alpha_expansion` rescue, same-candle rescue, setup/confirm, delayed continuation, drift family까지 가봤지만
  - 승자가 없거나
  - 특정 구간 편향이 너무 심하거나
  - entry는 맞아도 전체 전략으로 승격할 수준이 아니었다.
- 따라서 다음 단계는
  - 새 family를 또 복잡하게 만들기보다
  - **단순 지표 + 역할이 분명한 시간봉**으로 다시 시작하는 게 맞다.

## 연구 이름
- working name: `ebc`
- 의미:
  - `E`MA
  - `B`ollinger
  - `C`CI

## 시간봉 역할 분담
### 12h
- 최상위 환경 필터
- 큰 방향 / 과열 환경 / mean-revert 금지 구간 판단

### 2h
- 중기 bias
- trend continuation / pullback continuation 가능 여부 판단

### 30m
- setup 품질
- squeeze, re-expansion, 중심선 복귀 후 재출발 등 setup 구조 판단

### 5m
- 미세 confirm
- entry timing, wick noise 회피, chase 방지

## 지표 역할 분담
### EMA
- 방향성과 구조
- `12h`, `2h`에서 bias/regime
- `30m`, `5m`에서는 reclaim/continuation 확인

### Bollinger Bands
- squeeze / expansion / band-walk 여부
- 변동성 수축 후 확장인지, 이미 과열 추격인지 판단

### CCI
- timing / impulse / mean-revert 반응
- 과열에서 바로 추격할지, pullback 뒤 재개시인지 구분

## 첫 연구 가설
### 가설 A: continuation
- `12h`: EMA 방향 정렬
- `2h`: EMA 방향 + CCI가 과도하게 꺾이지 않음
- `30m`: 볼린저 squeeze 또는 중단 후 재확장
- `5m`: EMA reclaim + CCI 재상승(또는 재하락)

### 가설 B: drift after squeeze
- `12h`: 명확한 반대 레짐 아니어야 함
- `2h`: bias side 유지
- `30m`: BB 수축 -> 중심선 위/아래 유지
- `5m`: CCI가 0선 부근 재돌파하면서 band 상단/하단 재접근

## 구현 원칙
- 처음부터 다기능 family로 만들지 않는다.
- 1차 구현은 **하나의 alpha / 하나의 entry family**만 만든다.
- hard gate를 최소화한다.
- entry/exit를 한 번에 너무 많이 얹지 않는다.

## 첫 구현 우선순위
1. `ebc_v1_continuation` 하나만 만든다.
2. side는 처음엔 `LONG/SHORT` 둘 다 지원하되, backtest에서 분리해서 검증한다.
3. exit는 2안만 비교한다.
   - `fixed R + time stop`
   - `fixed R + trailing`

## 검증 기준
- 1Y fixed window
- 6M 두 창
- 기준 baseline은 계속 `q62 + cost near-pass`
- 다음을 동시에 본다.
  - `net`
  - `PF`
  - `DD`
  - `trades`
  - `fee_to_trade_gross`

## 즉시 금지
- 기존 `alpha_expansion` rescue를 다시 붙이지 않음
- 기존 drift family를 또 미세조정하지 않음
- 3개 초과 지표 추가 금지
- 4개 초과 시간봉 추가 금지

## 다음 액션
1. `ebc` 연구 계약을 `AGENTS.md`에 남긴다
2. `ebc_v1_continuation` 최소 스캐폴드 생성
3. backtest infra가 현재 `15m` base라서, `5m` 실행축을 쓰려면 replay/provider를 `5m` base로 확장하는 작업이 먼저 필요하다
4. 그 다음 `LONG only` / `SHORT only` / `BOTH`를 각각 분리 검증

## 현재 상태
- `ebc_v1_continuation` research scaffold 는 생성됐다:
  - [output/research/ebc_v1_continuation_scaffold_20260402.py](/home/user/project/auto-trader/output/research/ebc_v1_continuation_scaffold_20260402.py)
- 아직 runtime/kernel/profile에 연결하지 않았다.
- 이유는 현재 local backtest infra가 `15m` base timeline에 묶여 있어서, `5m` 실행축을 쓰는 EBC 연구는 base-timeframe refactor를 먼저 하는 게 맞기 때문이다.

## 한 줄 요약
- 다음 연구는 **EBC + 4TF**
- 더 단순하게
- 더 명확하게
- 다시 처음부터 family를 정의한다
