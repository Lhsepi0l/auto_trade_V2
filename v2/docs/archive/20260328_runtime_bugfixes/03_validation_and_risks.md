# 검증 결과와 남은 리스크

## 1. 이번 수정에서 실제로 돌린 검증

```bash
python -m pytest -q v2/tests/test_tpsl_brackets.py v2/tests/test_control_api.py -k 'bracket'
python -m pytest -q v2/tests/test_tpsl_brackets.py
python -m pytest -q v2/tests/test_control_api.py -k 'position_management or bracket or trailing'
python -m pytest -q v2/tests/test_control_api.py -k 'control_api_contract or persists_risk_config_across_restart or restores_kernel_runtime_overrides_after_restart or symbol_leverage_lifts_runtime_max'
python -m pytest -q v2/tests/test_discord_panel.py -k 'symbol_leverage_modal'
python -m pytest -q v2/tests/test_control_api.py -k 'market_data_stale or position_open_cycle_refreshes_market_data_before_stale_trip'
python -m pytest -q v2/tests/test_control_api.py -k 'ntfy or position_open_block_does_not_spam'
python -m pytest -q v2/tests/test_v2_env_and_notify.py
python -m pytest -q v2/tests/test_ra_2026_alpha_v2.py v2/tests/test_v2_config_loader.py v2/tests/test_market_intervals_config.py
python -m pytest -q v2/tests/test_v2_local_backtest.py -k 'profile_alpha_overrides_maps_expansion_profiles or historical_snapshot_provider or alpha_v2'
python -m ruff check v2/control/api.py v2/tpsl/brackets.py v2/discord_bot/views/panel.py v2/tests/test_control_api.py v2/tests/test_tpsl_brackets.py v2/tests/test_discord_panel.py
python -m ruff check v2/control/cycle.py v2/notify/runtime_events.py v2/tests/test_control_api.py v2/tests/test_v2_env_and_notify.py
python -m ruff check v2/strategies/ra_2026_alpha_v2.py v2/clean_room/kernel.py v2/backtest/policy.py v2/backtest/local_runner.py v2/control/status_payloads.py v2/tests/test_ra_2026_alpha_v2.py v2/tests/test_v2_config_loader.py v2/tests/test_market_intervals_config.py v2/tests/test_v2_local_backtest.py
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

### 2.3 stale / 알림 관련
- `position_open` 경로에서도 market data probe/freshness가 갱신되는지
- 기존 포지션 보유 중 stale 출렁임이 구조적으로 줄어드는지
- ntfy에서 `position_open` 스케줄러 알림이 스팸처럼 쏟아지지 않는지
- ntfy 본문에서 긴 프로필명 대신 짧은 mode/env 식별만 남는지

### 2.4 전략 구조 관련
- `alpha_expansion`의 trigger/quality 경로가 유닛 기준으로 깨지지 않는지
- `15m/1h/4h` 3-TF core가 유지되는지
- `30m/2h` 보강 실험을 live 기본 경로로 채택하지 않아도 관련 실험 훅이 회귀 없이 남아 있는지
- `ra_2026_alpha_v2_expansion_live_candidate`의 `expansion_quality_score_v2_min=0.62` 기본값이 설정/백테스트 경로에 일관되게 반영되는지
- local backtest에서 CLI 미지정 시 generic default가 profile override를 덮어쓰지 않고, profile 기본값 `0.62`가 실제 replay까지 전달되는지

## 3. 오늘 기준 남아 있는 리스크

### 3.1 TP/SL 복구 범위
- runtime이 관리하는 포지션은 방어가 강화됐다.
- 하지만 persisted management plan 이 없는 외부 포지션은 완전 자동 복구 범위가 제한될 수 있다.

### 3.2 레버리지 정책 의미
- 심볼 레버리지 입력이 이제 runtime `max_leverage` 도 같이 밀어 올릴 수 있다.
- 의도적으로 강한 입력을 넣으면 실제 주문에도 그대로 반영된다.
- 즉, 이번 수정은 "운영자 의도를 존중"하는 대신 "입력 실수도 더 직접 반영"하는 방향이다.

### 3.3 외부 변수 한계
- 이번 stale 수정은 코드상 heartbeat gap을 잡은 것이다.
- 하지만 실제 운영 서버에서는
  - 네트워크 지연
  - 거래소 공개 API 응답 지연
  - 호스트 리소스 압박
같은 외부 요인으로 stale 자체가 완전히 0회가 된다고 단정할 수는 없다.
- 따라서 운영에서는 여전히
  - stale 빈도
  - ready 출렁임 반복 여부
  - source error 동반 여부
를 같이 봐야 한다.

### 3.4 전략 수익성 한계
- 운영 안정성과 전략 수익성은 구분해서 봐야 한다.
- 2026-03-28 기준으로
  - TP/SL
  - leverage
  - stale
  - ntfy
는 분명히 좋아졌지만,
  전략 자체가 월 `3000 USD` 급 우상향 시스템으로 증명된 것은 아니다.
- fixed-window 1Y 비교에서 `30m/2h`를 live 기본 경로에 직접 녹인 버전은 baseline보다 `net/PF/DD`가 나빠 채택하지 않았다.
- 현재 전략 쪽에서 채택한 현실적인 개선안은
  - `15m/1h/4h` 유지
  - `expansion_quality_score_v2_min=0.62`
  - `edge_ratio >= 0.95 + high quality + 정상 spread/stop`인 `cost near-pass` 허용
로 expansion 품질을 더 보수적으로 거르면서도 과보수적인 cost 차단 일부를 풀어주는 것이다.
- 2026-03-29 재검증 기준:
  - fixed-window 1Y `2025-03-28 ~ 2026-03-28`:
    - baseline `net=6.14`, `PF=2.707`, `DD=5.62%`, `trades=318`
    - `qv2_min=0.62` `net=6.73`, `PF=3.127`, `DD=4.48%`, `trades=281`
    - `qv2_min=0.62 + cost near-pass`도 `net=6.73`, `PF=3.127`, `DD=4.48%`, `trades=281`로 동일 성과를 유지했고 `cost_missing`만 `467 -> 437`로 감소했다
  - 추가 6개월 창 `2025-03-28 ~ 2025-09-27`:
    - baseline `net=0.57`, `PF=2.140`, `DD=3.54%`, `trades=91`
    - `qv2_min=0.62` `net=0.64`, `PF=2.204`, `DD=3.34%`, `trades=89`
    - `qv2_min=0.62 + cost near-pass`도 `net=0.64`, `PF=2.204`, `DD=3.34%`, `trades=89`로 동일했고 `cost_missing`은 `308 -> 288`로 감소했다
  - 추가 6개월 창 `2025-09-28 ~ 2026-03-28`:
    - baseline `net=4.81`, `PF=3.449`, `DD=4.48%`, `trades=159`
    - `qv2_min=0.62 + cost near-pass`도 `net=4.81`, `PF=3.449`, `DD=4.48%`, `trades=159`로 동일했고 `cost_missing`은 `109 -> 101` 수준으로 감소했다.

## 4. 오늘 변경의 실질적 의미
- TP/SL 쪽은 "과잉 cleanup"을 막고 "repair"로 바꿨다.
- 레버리지 쪽은 "조용한 cap"을 없애고 "입력값 기준 적용"으로 바꿨다.
- stale 쪽은 "포지션 보유중이라 freshness가 늙어버리는 구조"를 막았다.
- ntfy 쪽은 "정상 상태가 실패처럼 보이던 표현"을 정리했다.
- 전략 쪽은 “더 많이 들어가게 만들기”보다 “애매한 확장을 더 잘 거르기” 쪽으로 방향을 재정렬했다.

둘 다 공통적으로:
- 운영자가 기대한 결과와
- 실제 runtime 동작의 차이를 줄이는 수정이다.
