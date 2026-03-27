# 검증 결과와 남은 리스크

## 1. 이번 수정에서 실제로 돌린 검증

```bash
python -m pytest -q v2/tests/test_tpsl_brackets.py v2/tests/test_control_api.py -k 'bracket'
python -m pytest -q v2/tests/test_tpsl_brackets.py
python -m pytest -q v2/tests/test_control_api.py -k 'position_management or bracket or trailing'
python -m pytest -q v2/tests/test_control_api.py -k 'control_api_contract or persists_risk_config_across_restart or restores_kernel_runtime_overrides_after_restart or symbol_leverage_lifts_runtime_max'
python -m pytest -q v2/tests/test_discord_panel.py -k 'symbol_leverage_modal'
python -m ruff check v2/control/api.py v2/tpsl/brackets.py v2/discord_bot/views/panel.py v2/tests/test_control_api.py v2/tests/test_tpsl_brackets.py v2/tests/test_discord_panel.py
```

## 2. 이번에 추가/보강한 회귀 포인트

### 2.1 TP/SL 관련
- single-leg missing + position open 상태에서 즉시 cleanup 하지 않고 repair 하는지
- 실제 recent fill 또는 flat position 이 있을 때만 TP/SL 종료로 확정하는지
- 2-leg 배치 중 두 번째 leg 실패 시 첫 번째 leg까지 정리되는지

### 2.2 레버리지 관련
- `set_symbol_leverage(symbol, N)` 이 현재 lower `max_leverage` 를 자동 상향하는지
- runtime 재시작 후에도 leverage override 가 유지되는지
- Discord modal 이 backend authoritative path 를 막지 않는지

## 3. 오늘 기준 남아 있는 리스크

### 3.1 TP/SL 복구 범위
- runtime이 관리하는 포지션은 방어가 강화됐다.
- 하지만 persisted management plan 이 없는 외부 포지션은 완전 자동 복구 범위가 제한될 수 있다.

### 3.2 레버리지 정책 의미
- 심볼 레버리지 입력이 이제 runtime `max_leverage` 도 같이 밀어 올릴 수 있다.
- 의도적으로 강한 입력을 넣으면 실제 주문에도 그대로 반영된다.
- 즉, 이번 수정은 "운영자 의도를 존중"하는 대신 "입력 실수도 더 직접 반영"하는 방향이다.

## 4. 오늘 변경의 실질적 의미
- TP/SL 쪽은 "과잉 cleanup"을 막고 "repair"로 바꿨다.
- 레버리지 쪽은 "조용한 cap"을 없애고 "입력값 기준 적용"으로 바꿨다.

둘 다 공통적으로:
- 운영자가 기대한 결과와
- 실제 runtime 동작의 차이를 줄이는 수정이다.
