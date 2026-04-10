# 서버 반영 체크리스트

## 1. 목적
이번 수정이 서버에 올라간 뒤
- TP/SL 보호 주문이 유지되는지
- 심볼 레버리지가 실제 주문에 반영되는지
- private stream 재연결 후 `state_uncertain`이 과하게 남지 않는지
를 짧은 절차로 확인하기 위함이다.

## 2. 서버 반영

```bash
cd /home/user/project/auto-trader
git fetch origin
git checkout migration/web-operator-panel
git pull --ff-only origin migration/web-operator-panel
sudo systemctl restart v2-stack
sudo systemctl status v2-stack --no-pager
```

## 3. 1차 상태 확인

```bash
curl -s http://127.0.0.1:8101/status
curl -s http://127.0.0.1:8101/readyz
```

바로 볼 것:
- `state_uncertain=false`
- `recovery_required=false`
- bracket recovery 관련 이상 없음
- `user_ws_stale=false`

## 4. 레버리지 확인 순서

### operator 또는 control API 에서
- `BTCUSDT` 기준으로 테스트할 심볼 레버리지를 넣는다.
- 예: `12`

### 확인 포인트
- `/status` 또는 operator panel 에서 leverage 가 `12` 로 보이는지
- 이전 lower cap 값으로 잘리지 않는지

## 5. TP/SL 확인 순서

### supervised entry 직후
- 해당 심볼 포지션이 열렸는지 확인
- open algo orders 에 TP / SL 두 개가 동시에 남아 있는지 확인

### 체크 포인트
- 한쪽만 남았다가 곧바로 둘 다 사라지는 현상이 없는지
- 포지션이 아직 열려 있을 때 bracket state 가 성급히 `CLEANED` 되지 않는지

## 6. stale / 알림 확인 순서

### 기존 포지션 보유 중
- 포지션이 열린 상태로 몇 사이클 지나가도
  - `/status.market_data.stale=false`
  - `/readyz.market_data_stale=false`
  가 유지되는지 본다.

### ntfy 확인 포인트
- 긴 프로필명 대신 `실거래 | prod` 형태로 짧게 오는지
- `기존 포지션 보유중` 같은 정상 상태가 경고처럼 계속 울리지 않는지
- 진짜 실패/위험 알림만 강한 톤으로 오는지

## 7. 프라이빗 스트림 복구 확인 순서

### 끊김 이후 복구가 있었을 때
- `/status.user_stream.last_error`
- `/status.state_uncertain`
- `/readyz.user_ws_stale`
를 같이 본다.

### 정상 기대값
- `user_stream_disconnect` 또는 `listen_key_expired`가 잠깐 찍혀도
- 이후 `user_stream_private_ok` / `user_stream_resync`가 오면
- `user_ws_stale=false`
- `state_uncertain=false`
- `state_uncertain_reason=null`

### 비정상
- `private_ok` 또는 `resync 성공`이 보이는데도
- `state_uncertain=true`가 계속 남아 신규 판단을 막는 경우

## 8. 추천 확인 명령

```bash
journalctl -u v2-stack -n 200 --no-pager
ss -ltnp | grep 8101
```

로그에서 볼 것:
- `runtime_bracket_place_failed`
- `position_management_bracket_replace_failed`
- `bracket_on_leg_filled_failed`
- `bracket_leg_missing_without_exit_confirmation`

위 로그가 떠도 실제 포지션이 열려 있으면 repair 로 복구되는지 같이 본다.

## 9. 이상 시 1차 판단 기준

### TP/SL 쪽
- 포지션 open + recent fill 없음
- 그런데 bracket cleanup 이 바로 일어나면 비정상

### 레버리지 쪽
- operator 입력 `N`
- `/status` leverage 가 `N` 이 아님
- 실제 exchange leverage 도 `N` 이 아님
이면 다시 확인 필요

### stale 쪽
- 기존 포지션 보유 중인데
- `market_data_stale -> ready 미완료 -> 정상 복귀`
가 계속 반복되면 비정상
- 특히 `source_error` 나 probe 실패 로그가 동반되면 외부 변수까지 같이 의심해야 한다

### 프라이빗 스트림 쪽
- `user_stream_disconnect` 후 `private_ok/resync`가 왔는데도
- `state_uncertain=true`가 계속 남으면 비정상
- 반대로 `live_positions_fetch_failed` 같은 non-stream uncertainty는 `private_ok`만으로 풀리면 안 된다

## 10. 한 줄 요약
- 진입 후 TP/SL 은 둘 다 살아 있어야 정상
- 심볼 레버리지는 입력한 값 그대로 주문에 반영돼야 정상
- 기존 포지션 보유 중에도 market data freshness는 계속 살아 있어야 정상
- private stream이 이미 복구됐으면 `state_uncertain`은 같이 내려가야 정상
