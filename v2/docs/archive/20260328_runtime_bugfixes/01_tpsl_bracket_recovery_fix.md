# TP/SL 브래킷 복구 수정

## 1. 왜 이 수정이 필요했나
오늘 문제는 단순히 "브래킷이 한 번 실패했다" 수준이 아니었다.

실제 증상은 다음과 같았다.

1. 포지션 진입은 정상적으로 된다.
2. 진입 직후 TP/SL 이 둘 다 걸려야 하는데, 어느 순간 사라진다.
3. 포지션은 아직 살아 있는데 runtime 쪽 bracket state는 이미 정리된 것처럼 보일 수 있다.

즉, 진입 이후 보호 주문이 사라지는 위험한 상태였다.

## 2. 이번 문제의 실제 원인

### 2.1 bracket poller 오판
- 기존 로직은 open algo orders 조회에서
  - TP 만 보이거나
  - SL 만 보이면
  바로 "반대쪽이 체결됐다"고 가정했다.
- 하지만 실거래에서는
  - 조회 지연
  - 거래소 응답 누락
  - 순간적인 open-order visibility 흔들림
  때문에 실제 체결 없이 한쪽만 보이는 순간이 생길 수 있다.

결과:
- 포지션은 아직 열려 있는데
- runtime이 TP/SL 종료로 오판하고
- 남은 leg까지 cleanup 하면서 브래킷이 사라질 수 있었다.

### 2.2 브래킷 2-leg 생성의 비원자성
- live 브래킷은
  1. TP algo 주문
  2. SL algo 주문
  순서로 생성된다.
- 이전에는 첫 번째 leg가 성공하고 두 번째가 실패해도,
  첫 번째 성공분을 되돌리지 않는 경로가 있었다.

결과:
- TP 또는 SL 한쪽만 남는 partial/orphan 상태가 생길 수 있었다.
- 이후 poller가 이 비정상 상태를 또 오판하면서 정리가 꼬일 수 있었다.

## 3. 이번에 어떻게 고쳤나

### 3.1 single-leg missing을 바로 exit로 처리하지 않음
- 이제 poller는 아래 둘 중 하나가 있을 때만 실제 TP/SL 종료로 본다.
  - 포지션 flat 확인
  - 최근 fill 기록 확인

즉:
- "한쪽이 안 보인다"만으로는 청산 확정하지 않는다.

### 3.2 포지션이 살아 있으면 브래킷을 복구
- 포지션이 아직 열려 있고
- persisted position-management plan 이 남아 있으면
- 그 plan의 `take_profit_price`, `stop_price`, `quantity` 기준으로 TP/SL 을 다시 건다.

즉:
- 기존에는 오판 cleanup 으로 끝났다면
- 지금은 repair 경로로 되살린다.

### 3.3 live 배치 실패 시 partial leg도 정리
- TP/SL 2-leg 생성 중 두 번째 leg가 실패하면
- 이미 성공한 첫 번째 leg를 즉시 취소한다.
- 그리고 runtime bracket state를 `CLEANED` 로 남겨 partial state가 이어지지 않게 했다.

## 4. 운영자가 체감할 변화
- 진입 직후 TP/SL 이 더 안정적으로 유지된다.
- 거래소 조회가 순간 흔들려도 runtime이 성급하게 보호 주문을 지우지 않는다.
- 한쪽만 살아남는 비정상 브래킷 상태가 크게 줄어든다.

## 5. 바로 확인할 포인트

### 5.1 진입 직후
- `open algo orders` 에 TP / SL 두 개가 동시에 보이는지 확인
- `/status` 또는 operator 상태에서 bracket 관련 오류가 바로 뜨지 않는지 확인

### 5.2 흔들림 상황
- 한쪽 leg가 잠깐 안 보여도
  - 포지션이 열려 있으면
  - runtime이 cleanup 대신 repair 쪽으로 가는지 확인

### 5.3 실제 청산 상황
- 실제 TP/SL 체결 시에는
  - 최근 fill 근거
  - flat position 근거
  기준으로만 종료 알림/cleanup 이 나가는지 확인

## 6. 남아 있는 한계
- persisted position-management plan 이 없는 외부/수동 포지션은 자동 TP/SL 재구성 범위가 제한된다.
- 즉, 이번 수정은 "runtime이 관리하던 포지션" 기준 방어가 핵심이다.
