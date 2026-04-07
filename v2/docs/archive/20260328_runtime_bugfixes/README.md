# 2026-03-28 Runtime Bugfixes

## 문서 목적
- 오늘 수정한 두 가지 실거래 이슈를 한 번에 정리한다.
- 운영자 관점에서 "무엇이 고쳐졌는지", 개발자 관점에서 "어디를 보면 되는지"를 빠르게 파악할 수 있게 한다.
- 이후 동일 증상 재발 시 기준 문서로 바로 참고할 수 있게 한다.

## 이 폴더에 들어있는 문서
- [01_tpsl_bracket_recovery_fix.md](./01_tpsl_bracket_recovery_fix.md)
  - 진입 후 TP/SL 이 사라지던 문제의 증상, 원인, 수정 내용
- [02_leverage_intent_fix.md](./02_leverage_intent_fix.md)
  - 심볼 레버리지 설정이 실제 주문 레버리지로 반영되지 않던 문제의 원인과 수정
- [03_validation_and_risks.md](./03_validation_and_risks.md)
  - 이번 수정에서 실제로 돌린 검증 명령과 남은 주의점
- [04_server_apply_checklist.md](./04_server_apply_checklist.md)
  - 서버 반영 후 운영자가 바로 확인할 체크리스트와 명령
- [05_market_data_stale_and_ntfy_cleanup.md](./05_market_data_stale_and_ntfy_cleanup.md)
  - 기존 포지션 보유 중 stale 출렁임 원인과 ntfy 알림 정리 내용
- [06_strategy_reality_check.md](./06_strategy_reality_check.md)
  - 지금 전략이 실제로 어디까지 개선됐는지, 무엇이 아직 부족한지에 대한 냉정한 정리

## 오늘 핵심 요약
- TP/SL 브래킷은 이제 한쪽 leg가 조회에서 잠깐 사라졌다고 바로 청산 처리하지 않는다.
- 포지션이 아직 살아 있으면 persisted position-management plan 기준으로 TP/SL 을 복구한다.
- live 브래킷 생성 중 한쪽만 성공하고 다른 한쪽이 실패하면, 성공한 한쪽도 바로 정리해서 orphan 상태를 남기지 않는다.
- 심볼 레버리지 설정은 이제 운영자 의도를 기준으로 처리한다.
- 심볼 레버리지가 현재 `max_leverage` 보다 크면 runtime이 `max_leverage` 도 같이 올려서 실제 주문이 요청한 값으로 들어가게 했다.
- Discord 패널도 같은 경로를 막지 않도록 맞췄다.
- 기존 포지션 보유 중에는 신규 진입은 막히더라도 market data heartbeat는 계속 갱신되도록 보정해 `market_data_stale -> ready 미완료 -> 정상 복귀` 출렁임 원인을 줄였다.
- ntfy 알림은 긴 프로필명 노출과 과한 경고성 표현을 줄여, 정상 보유 상태가 실패처럼 보이지 않도록 정리했다.
- 전략 자체는 아직 “돈 버는 기계” 수준으로 증명된 상태는 아니지만, 현재 기준 채택값은 `15m/1h/4h` 3-TF core를 유지하면서 `expansion_quality_score_v2_min=0.62`로 좋은 expansion만 더 날카롭게 거르는 방향이다.
- local backtest 기본 실행도 이제 `옵션 미지정 시 profile default를 유지`하도록 맞춰, 운영 기본값과 검증 기본값이 어긋나지 않게 정리했다.
- 이 값은 fixed-window 1Y에서 baseline `net=6.14 / PF=2.707 / DD=5.62% / trades=318` 대비 `net=6.73 / PF=3.127 / DD=4.48% / trades=281`로 개선됐다.
- 추가로 `cost_missing` 중 `edge_shortfall` near-pass만 좁게 허용하는 규칙을 적용했을 때, 1Y/6M 성과는 유지되면서 `cost_missing` 건수만 줄어드는 것을 확인했다.
- 추가 구간 검증에서도 `2025-03-28 ~ 2025-09-27` 6개월은 `net=0.57 -> 0.64`, `PF=2.140 -> 2.204`, `DD=3.54% -> 3.34%`로 소폭 개선됐고, `2025-09-28 ~ 2026-03-28` 6개월은 사실상 동일 성능으로 중립이었다.
- `30m/2h`를 live 기본 경로에 직접 녹인 버전은 fixed-window 1Y 검증에서 baseline보다 `net/PF/DD`가 나빠져 채택하지 않았다.

## 권장 읽기 순서
1. 운영 증상과 TP/SL 문제를 먼저 확인하려면 `01_tpsl_bracket_recovery_fix.md`
2. 레버리지 입력과 실제 주문 불일치 문제를 보려면 `02_leverage_intent_fix.md`
3. 오늘 검증 범위와 남은 리스크를 확인하려면 `03_validation_and_risks.md`
4. 서버에 반영하고 바로 운영 체크까지 이어가려면 `04_server_apply_checklist.md`
5. stale 출렁임 원인과 ntfy 알림 톤 정리를 보려면 `05_market_data_stale_and_ntfy_cleanup.md`
6. 지금 전략이 실제로 어디까지 개선됐는지 보려면 `06_strategy_reality_check.md`
