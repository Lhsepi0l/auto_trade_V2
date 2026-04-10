# Long Delayed Continuation Family

## 목적
- `alpha_expansion` rescue 튜닝 루프를 끊고, 실제 데이터에서 분리된 신규 family를 정의한다.
- 기존 `same-candle breakout` 가설이 아니라, **강한 impulse setup 뒤 8~16 bar 드리프트**를 먹는 구조를 연구 대상으로 삼는다.
- 이 문서는 바로 다음 구현/백테스트의 계약서 역할을 한다.

## 왜 이 family인가
1Y fixed-window `2025-03-28 ~ 2026-03-28` event tape / cluster / setup-queue 분석 결과:

- profitable `trigger_missing` cluster 중 가장 유의미한 LONG setup은 아래였다.
  - `reason=trigger_missing`
  - `side=LONG`
  - `range_atr>=1.2`
  - `body_ratio>=0.5`
  - `favored_close_long<0.6`
  - `width_expansion_frac>=0.10`
  - `edge_ratio>=1.10`
  - `count=101`
  - `avg 8/12/16 bar return = 11.27 / 18.36 / 16.12 bps`
  - `16-bar positive rate = 57.43%`

- 하지만 이 setup들이 실제 `alpha_expansion` confirm으로 이어지는 비율은 매우 낮았다.
  - 4 bar 안 full confirm: `5 / 98`
  - 8 bar 안 full confirm: `8 / 98`
  - 16 bar 안 full confirm: `12 / 98`

즉 결론:
- 이 패턴은 `breakout family` 가 아니다.
- `same-candle rescue`도 아니다.
- `1~4 bar delayed breakout`도 아니다.
- **강한 impulse setup 이후 느리게 이어지는 continuation family**로 보는 게 더 맞다.

## Family 정의
이 family는 아래 상황을 노린다.

- `4h`: 완전한 역추세는 아님
- `1h`: bias는 LONG 쪽 정렬
- `15m`: 강한 range/body bar가 나왔지만, close는 high에 붙지 않음
- `width_expansion`은 충분히 커서 impulse는 맞음
- `edge_ratio`도 충분해 비용 우위는 존재
- 그러나 donchian / buffered breakout을 같은 봉에서 만족하지 않음

즉:
- “돌파 완성형”이 아니라
- **“강한 시작 이후 비정형 continuation”**

## v1 계약
### 엔트리 setup
- side: `LONG`
- `range_atr >= 1.2`
- `body_ratio >= 0.5`
- `favored_close_long < 0.6`
- `width_expansion_frac >= 0.10`
- `edge_ratio >= 1.10`
- `bias_side == LONG`

### 엔트리 방식
- setup 바에서 즉시 진입하지 않는다.
- setup 발생 후 `1~8 bar` 관찰창을 둔다.
- confirm은 breakout이 아니라 아래 2개 중 하나를 사용한다.
  - `close > setup_close`
  - `close > ema_15m`

### 진입 취소
- 관찰창 안에 confirm이 안 나오면 폐기
- setup 저점 이탈 시 폐기

### 기본 exit
- `take_profit_r`: `1.8` 이상
- `time_stop_bars`: `16`
- 이후에는 trailing / progressive exit 비교 필요

## 구현 원칙
- live 전략에 바로 연결하지 않는다.
- first implementation은 `research-only scaffold` 로 둔다.
- runtime branch 기본값은 계속 `q62 + cost near-pass` 유지

## 관찰 메모
- short family는 분리 후에도 1Y `net=-1.43`, `PF=1.229`로 폐기
- long drift family도 현재 직접 진입형은 1Y `net=0.42`, `PF=1.425`로 채택 불가
- 따라서 다음 구현은 **entry-only family**가 아니라 **setup-window + confirm + exit pair** 단위로 봐야 한다

## 다음 실험
1. research scaffold에서 setup-window 상태 머신 구현
2. confirm 2안 비교
   - `close > setup_close`
   - `close > ema_15m`
3. exit 2안 비교
   - fixed `1.8R + 16 bars`
   - progressive/trailing

## 2026-04-02 진행 결과
- `same-candle rescue`: 폐기
- `short family`: 폐기
- `long direct drift`: 폐기
- `long delayed continuation`: 상태형 queue 구조는 방향이 맞았지만, 전체 1Y 전략으로는 아직 약했다.

### best-known long delayed continuation
- family: `LONG only`
- setup window expiry: `8 bars`
- confirm: `close > max(setup_close, ema_15m)`
- exit winner: `take_profit_r=1.8`, `time_stop_bars=16`

### best-known metrics
- 1Y fixed-window `2025-03-28 ~ 2026-03-28`
  - `net=0.56`
  - `PF=2.519`
  - `DD=2.40%`
  - `trades=14`

### matrix verdict
- `1.8R / 16` 가 가장 낫고
- `1.8R / 24`, `2.2R / 16`, `2.2R / 24` 는 모두 더 나빴다
- 즉, 이 family는 **exit 최적점은 찾았지만 메인 전략으로 승격할 수준은 아니다**

### 최종 판단
- 이 family branch는 여기서 종료한다
- 운영 baseline은 계속 `q62 + cost near-pass`
- 다음 새 연구는 이 branch를 더 미세조정하지 말고, **다른 family**로 넘어가야 한다

## 한 줄 요약
- `alpha_expansion` 구제는 끝
- `short` family는 폐기
- 다음은 **LONG delayed continuation** 을 별도 family로 연구한다
