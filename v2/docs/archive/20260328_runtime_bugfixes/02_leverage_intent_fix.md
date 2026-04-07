# 레버리지 의도 반영 수정

## 1. 왜 이 수정이 필요했나
운영자가 분명히 `N배`를 넣었는데 실제 진입은 계속 더 낮은 배수, 대표적으로 `10배`로 들어가는 혼란이 있었다.

이 문제는 화면 표시만의 문제가 아니라 실제 주문 경로까지 이어지는 문제였다.

## 2. 이전 동작

### 2.1 runtime 내부 계산
- 심볼 레버리지는 `symbol_leverage_map` 에 저장된다.
- 하지만 실제 적용 레버리지는 내부적으로
  - `min(symbol_leverage, max_leverage)`
  방식으로 결정됐다.

즉:
- 운영자가 `BTCUSDT = 12x` 를 넣어도
- runtime `max_leverage` 가 `10` 이면
- 실제 주문 경로에는 `10x` 만 들어갔다.

### 2.2 왜 더 헷갈렸나
- 설정 저장은 성공처럼 보일 수 있다.
- 하지만 내부 cap 에 의해 실제 execution leverage 가 잘려 나간다.
- Discord 패널은 아예 `max_leverage` 초과 입력을 선차단하기도 했다.

결과:
- 운영자는 "분명히 12 넣었는데 왜 10으로 들어가냐"는 상태를 겪게 된다.

## 3. 이번 수정의 핵심

### 3.1 `set_symbol_leverage` 를 operator intent 기준으로 처리
- 이제 운영자가 심볼 레버리지를 명시적으로 넣으면
- backend는 그 값을 "최종 의도"로 본다.

### 3.2 필요하면 runtime `max_leverage` 도 같이 올림
- 요청한 심볼 레버리지가 현재 `max_leverage` 보다 크면
- runtime이 `max_leverage` 를 해당 값까지 같이 올린다.

즉:
- `BTCUSDT = 12`
- 현재 `max_leverage = 10`
상태라면
- 결과적으로
  - `symbol_leverage_map["BTCUSDT"] = 12`
  - `max_leverage = 12`
로 맞춘다.

### 3.3 Discord 패널도 backend 의도를 막지 않게 정리
- 기존에는 modal 단계에서 `max_leverage` 초과를 미리 막았다.
- 지금은 backend가 최종 authoritative path 가 되도록 맞췄다.

## 4. 지금 기준 기대 동작

### 예시 1
- 현재 `max_leverage = 5`
- 운영자가 `BTCUSDT = 12` 입력

현재 결과:
- runtime `max_leverage = 12`
- 심볼 레버리지 `BTCUSDT = 12`
- 실제 주문 경로 leverage = `12`

### 예시 2
- 현재 `max_leverage = 20`
- 운영자가 `ETHUSDT = 7` 입력

현재 결과:
- runtime `max_leverage = 20` 유지
- 심볼 레버리지 `ETHUSDT = 7`
- 실제 주문 경로 leverage = `7`

## 5. 운영자가 바로 확인할 포인트
- `/status` 의 capital/leverage 표시가 입력값과 일치하는지
- operator panel 에서 레버리지 값이 잘리지 않는지
- 실제 supervised entry 후 exchange 측 leverage 가 요청값과 일치하는지

## 6. 주의할 점
- 이번 수정은 "심볼 레버리지 입력"을 operator 의도 우선으로 본다.
- 그래서 심볼 레버리지를 높게 주면 runtime `max_leverage` 도 함께 올라갈 수 있다.
- 운영 기준상 이를 원하지 않으면
  - 먼저 `max_leverage` 를 명시적으로 원하는 수준으로 관리하거나
  - 심볼 레버리지를 해제/재조정하는 방식으로 운영해야 한다.
