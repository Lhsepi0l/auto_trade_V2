# 프라이빗 스트림 불확실 상태 복구 수정

## 1. 증상
운영 로그/이벤트에서 아래 순서가 보일 수 있었다.

- `상태 불확실`
- `프라이빗 스트림 끊김`
- 이후 실제로는 private stream이 다시 살아났는데도
- `/status.state_uncertain=true`가 남아 `ready=false`가 이어짐

즉, “잠깐 끊겼다가 복구된 상태”를 runtime이 너무 오래 위험 상태로 들고 가는 문제가 있었다.

## 2. 원인
문제 핵심은 `disconnect`와 `private_ok`의 역할이 비대칭이었던 것이다.

- [v2/control/api.py](/home/user/project/auto-trader/v2/control/api.py)
  - `user_stream_disconnect`가 오면 즉시 `_set_state_uncertain(...)`로 `state_uncertain`을 켰다.
  - 반면 `user_stream_private_ok`는 freshness timestamp만 갱신하고, disconnect 계열 uncertainty를 직접 해제하지 않았다.

그래서
- 짧은 websocket 끊김
- listen key 재연결
- keepalive 후 회복
같은 정상적인 복구 흐름 뒤에도, 운영 상태는 계속 `상태 불확실`로 남을 수 있었다.

## 3. 심각도 판단
이건 “즉시 계좌를 망치는 주문 버그” 타입은 아니다.

하지만 운영 관점에서는 중간 이상으로 중요했다.

- `ready=false`가 계속 남을 수 있음
- 신규 판단/신규 진입이 `state_uncertain`으로 막힐 수 있음
- 운영자가 “실제로는 복구됐는데 왜 계속 막혀 있지?”를 겪게 됨

즉, 체결 보호보다는 **운영 readiness와 판단 차단**에 영향을 주는 버그였다.

## 4. 수정 내용
복구 조건을 아주 좁게 넣었다.

- `private_ok`가 들어왔을 때
- 현재 `state_uncertain`의 원인이
  - `user_stream_disconnected`
  - `listen_key_expired`
  - `user_stream_error:*`
  - `socket_*`
  같은 **일시적 user-stream 끊김 계열**이고
- 그 이유가 현재 `_user_stream_last_error`와 일치하며
- `user_ws_stale`도 더 이상 아닌 경우에만

`state_uncertain`과 `_user_stream_last_error`를 같이 해제한다.

반대로
- `live_positions_fetch_failed`
- `startup_reconcile:*`
- `resync_failed:*`
처럼 **다른 원인**으로 켜진 uncertainty는 `private_ok`만으로 풀리지 않게 유지했다.

## 5. 회귀 테스트
추가한 핵심 회귀는 두 가지다.

- disconnect 후 `private_ok`가 오면 `state_uncertain`이 자동으로 해제되는지
- non-stream uncertainty는 `private_ok`가 와도 유지되는지

대상 파일:
- [v2/tests/test_control_api.py](/home/user/project/auto-trader/v2/tests/test_control_api.py)

## 6. 운영 확인 포인트
서버 반영 후 아래를 보면 된다.

```bash
curl -s http://127.0.0.1:8101/status
curl -s http://127.0.0.1:8101/readyz
journalctl -u v2-stack -n 200 --no-pager
```

정상 기대값:
- stream이 잠깐 끊겨도 재연결/keepalive 후
- `user_ws_stale=false`
- `state_uncertain=false`
- `state_uncertain_reason=null`

비정상:
- `user_stream_private_ok` 또는 `resync 성공` 로그가 찍혔는데도
- `state_uncertain=true`가 계속 남는 경우

## 7. 한 줄 요약
이번 수정은
**“프라이빗 스트림이 이미 복구됐는데도 runtime이 계속 불확실 상태로 남는 운영 버그”를 좁고 안전하게 해제하도록 만든 것**이다.
