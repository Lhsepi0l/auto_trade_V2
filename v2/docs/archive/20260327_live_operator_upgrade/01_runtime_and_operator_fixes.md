# Runtime / Operator 핵심 수정

## 1. 왜 이 변경이 필요했나
오늘 기준 문제는 크게 세 종류였다.

1. 실거래 사이징이 operator 입력과 다르게 동작했다.
2. operator 웹이 일부 설정을 잘못 표시하거나, 설정 변경이 엔진 주기까지 건드렸다.
3. 로그를 뽑아도 필요한 상태 정보가 빠지거나, 다운로드/분석 흐름이 불편했다.

이번 수정은 이 세 문제를 먼저 안정화하는 데 집중했다.

## 2. 레버리지 / 증거금 관련 수정

### 2.1 live sizing 정리
- 원인:
  - live runtime에서 operator가 정한 `증거금 x 레버리지` 목표 notional을 계산한 뒤에도,
    전략 payload의 `risk_per_trade_pct` / `stop_distance_frac`로 다시 더 작게 clamp 하는 숨은 2차 경로가 있었다.
- 결과:
  - operator가 `10 USDT / 10x`를 기대해도 실제 주문 notional은 `100 USDT`가 아니라 `10 USDT` 근처로 눌릴 수 있었다.
- 대응:
  - live runtime `RiskAwareSizer`에서 해당 2차 clamp를 제거했다.
  - 실효 주문 크기의 SSOT는 operator/runtime budget-leverage 로직으로 통일했다.

### 2.2 사용 가능 증거금 부족 시 자동 다운사이징
- 이전 동작:
  - `required_margin + fee buffer > available`이면 주문 전체를 실패시켰다.
- 현재 동작:
  - 가능한 최대 수량으로 먼저 자동 축소한다.
  - 거래소 최소 수량 / 최소 노셔널도 못 맞출 때만 `insufficient_available_margin`으로 실패시킨다.
- 의미:
  - "조금만 부족한데도 통째로 못 들어가는" 멍청한 실패를 줄였다.

### 2.3 q070 profile volume gate 완화
- 번들 분석 결과 최근 no-candidate의 주원인은 `volume_missing`이었다.
- `ra_2026_alpha_v2_expansion_verified_q070` profile의
  - `min_volume_ratio_15m: 0.9 -> 0.8`
  - `expansion_buffer_bps: 2.0 -> 1.5`
  로 완화했다.
- 목적:
  - 실거래에서 `volume_missing`, `trigger_missing` 때문에 entry candidate가 아예 안 나오는 과잉 차단을 줄이기 위함이다.

## 3. operator 웹 관련 수정

### 3.1 notify interval 버그 수정
- 이전 동작:
  - 웹에서 `상태 알림 주기`를 바꾸면 `scheduler_tick_sec`까지 같이 바뀌었다.
- 실제 문제:
  - 알림만 600초로 줄이려 했는데 엔진 판단도 600초마다 한 번만 돌아가는 상태가 됐다.
- 현재 동작:
  - `notify_interval_sec`는 알림 cadence만 바꾼다.
  - 엔진 판단 주기(`scheduler_tick_sec`)는 별도로 유지된다.

### 3.2 증거금 입력칸 표시 버그 수정
- 이전 동작:
  - 입력칸이 operator가 의도한 "실효 증거금"이 아니라 내부 저장용 `margin_budget_usdt` 원값을 다시 보여줬다.
  - 예: `margin_use_pct=0.1`일 때 `22`를 넣었는데 화면엔 `220`처럼 보였다.
- 현재 동작:
  - 입력칸은 실제 운영 기준 `budget_usdt`를 우선 표시한다.
  - 운영자가 `22`를 넣으면 다시 봐도 `22`로 보인다.

### 3.3 상태 오표시 수정
- 이전 동작:
  - `safe_mode`인데도 화면에 `ops_paused`처럼 뭉개서 보일 수 있었다.
- 현재 동작:
  - `safe_mode`와 `ops_paused`를 실제 ops state 기준으로 구분해 표시한다.

## 4. 오늘 기준 운영자가 체감할 변화
- `증거금/레버리지`가 예전보다 실제 주문과 더 가깝게 맞는다.
- 사용 가능 증거금이 조금 부족하면 아예 실패하는 대신 가능한 크기로 줄여서 들어간다.
- `notify_interval`을 바꿔도 엔진이 갑자기 10분마다만 생각하는 상태가 되지 않는다.
- 증거금 입력칸이 더 이상 `220`처럼 0이 붙어 보이지 않는다.
- `safe_mode`와 `일시정지`가 예전보다 덜 헷갈리게 보인다.

## 5. 바로 확인할 포인트

### 5.1 operator 화면
- `/operator`
- `/operator/logs`
- `자본 / 레버리지` 카드의 예산 입력칸
- `빠른 추출 / 전체 추출` 버튼

### 5.2 로그에서 봐야 할 것
- `cycle_result`
  - `reason=volume_missing`
  - `reason=trigger_missing`
- `position_management_update`
- `position_management_exit`
- `debug_bundle_exported`

## 6. 남아 있는 한계
- 지금도 모든 경우에 수익이 좋아진다고 단정할 수는 없다.
- q070 volume gate 완화는 trade count를 늘릴 가능성이 있지만, 품질이 낮은 진입도 일부 더 통과시킬 수 있다.
- 그래서 오늘 이후에는
  - 실제 entry 증가 여부
  - drawdown 악화 여부
  - partial reduce / BE protection이 손익 분포를 개선하는지
  를 같이 봐야 한다.
