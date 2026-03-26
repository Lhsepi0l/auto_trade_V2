# 2026-03-27 Live / Operator Upgrade

## 문서 목적
- 오늘 적용한 실거래/운영 웹/로그 추출/포지션 관리/Git 운영 기준 변경을 한 번에 정리한다.
- 운영자 관점에서 "무엇이 달라졌는지", 개발자 관점에서 "어디를 보면 되는지"를 빠르게 파악할 수 있게 한다.
- 이후 장애 분석이나 회귀 점검 시 기준 문서로 사용한다.

## 이 폴더에 들어있는 문서
- [01_runtime_and_operator_fixes.md](./01_runtime_and_operator_fixes.md)
  - 오늘 들어간 핵심 버그 수정과 운영 화면 변경
- [02_debug_bundle_workflow.md](./02_debug_bundle_workflow.md)
  - 빠른 추출 / 전체 추출 / zip 다운로드 / 번들 읽는 법
- [03_position_management_loop.md](./03_position_management_loop.md)
  - 보유 중 재평가 루프, partial reduce, TP 재배치, 현재 한계
- [04_git_and_release_workflow.md](./04_git_and_release_workflow.md)
  - Git / GitHub / 브랜치 / 태그 / 서버 반영 기준

## 오늘 핵심 요약
- 레버리지/증거금 반영 버그를 고쳐 실효 주문 크기가 operator 입력과 더 일치하게 정리됐다.
- 사용 가능 증거금이 약간 부족할 때는 주문을 통째로 실패시키지 않고 자동 다운사이징하도록 바뀌었다.
- 웹 operator의 `notify_interval`이 엔진 판단 주기를 같이 망가뜨리던 버그를 제거했다.
- 로그 추출은 브라우저에서 바로 zip 다운로드 가능해졌고, 빠른 추출 / 전체 추출로 모드가 분리됐다.
- 번들 생성 시 `control/*.json`이 비던 self-timeout 경로를 제거했다.
- q070 실거래 profile의 `volume_missing` 과잉 차단을 완화했다.
- 보유 중 `hold / extend / reduce / exit / partial reduce / TP 재배치 / BE 보호 / signal flip close / volatility runner lock`까지 수행하는 3차 포지션 관리 루프가 들어갔다.
- Git / GitHub 운영 기준과 서버 pull-only 원칙을 문서화하고, 서버 업데이트 스크립트를 추가했다.

## 권장 읽기 순서
1. 운영자가 지금 무엇이 달라졌는지 확인하려면 `01_runtime_and_operator_fixes.md`
2. 장애 로그를 PC에서 분석하려면 `02_debug_bundle_workflow.md`
3. 포지션 보유 중 로직이 어떻게 바뀌었는지 보려면 `03_position_management_loop.md`
4. 앞으로 버전/배포 기준을 정리하려면 `04_git_and_release_workflow.md`
