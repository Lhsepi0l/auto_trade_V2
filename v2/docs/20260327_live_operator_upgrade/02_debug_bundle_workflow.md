# Debug Bundle 사용법

## 1. 목적
라즈베리파이 서버에서 발생한 상태/로그/DB 정보를 한 번에 묶어서 PC에서 읽기 쉽게 분석하기 위함이다.

## 2. 현재 지원 방식

### 웹 operator
- `/operator/logs`
- 버튼:
  - `빠른 추출`
  - `전체 추출`
- 결과:
  - 서버에서 bundle 디렉터리 생성
  - zip 아카이브 생성
  - 브라우저에서 바로 다운로드

### CLI
```bash
python v2/scripts/export_runtime_debug_bundle.py --label manual_check
python v2/scripts/export_runtime_debug_bundle.py --label full_dump --all
```

## 3. 빠른 추출 vs 전체 추출

### 빠른 추출
- 최근 operator events
- 최근 journal events
- 최근 submission intents
- 각 로그 파일 tail
- 최근 systemd journal tail
- 용도:
  - 1차 원인 파악
  - 빠른 공유
  - 파일 크기 최소화

### 전체 추출
- operator events 전체
- journal events 전체
- submission intents 전체
- 로컬 로그 파일 전체
- systemd journal 전체
- 용도:
  - 깊은 장애 분석
  - 장기 누적 문제 추적
  - PC에서 후속 분석

## 4. 번들 안에 들어가는 것
- `control/`
  - `healthz`, `readyz`, `readiness`, `status`
- `sqlite/`
  - `operator_events`
  - `runtime_risk_config`
  - `runtime_markers`
  - `ops_state`
  - `submission_intents`
  - `bracket_states`
- `system/`
  - `systemctl status`
  - `journalctl`
  - `git status`
  - `git branch`
  - `git head`
  - `ss -ltnp`
- `logs/`
  - `v2/logs/control_api.log`
  - `v2/logs/control_runtime.log`
  - 기타 로컬 로그 파일

## 5. 오늘 수정된 점

### 5.1 self-timeout 제거
- 이전 문제:
  - 웹에서 bundle을 만들 때 같은 control server의 `/readyz`, `/status`, `/readiness`를 다시 HTTP로 호출했다.
  - 그 과정에서 timeout이 나면 정작 제일 중요한 `control/*.json`이 비어 버렸다.
- 현재:
  - 웹 추출 경로는 export 완료 후 controller 직접 스냅샷으로 `control/*.json`을 채운다.
  - 즉, self-timeout에 덜 취약하다.

### 5.2 브라우저 다운로드 개선
- 버튼 클릭 후 zip 다운로드가 바로 되도록 바뀌었다.
- 빠른 추출 / 전체 추출 모드를 분리했다.

## 6. 파일 읽는 순서
1. `SUMMARY.md`
2. `control/status.json`
3. `control/readyz.json`
4. `sqlite/operator_events.json`
5. `logs/v2_logs_control_runtime.log.tail.log`
6. `logs/v2_logs_control_api.log.tail.log`

## 7. 이번 번들에서 실제로 유용했던 해석 예시
- 최근 실거래 문제는 `execution_failed`가 아니라 `no_candidate` 연속이었다.
- 이유는 대부분 `volume_missing`, 일부 `trigger_missing`.
- `notify_interval=600`이 `scheduler_tick_sec=600`까지 같이 바꾼 버그도 번들에서 발견됐다.
- `state_uncertain` / `user_stream_disconnected` 이후 수동 `reconcile`로 회복된 흐름도 operator events만 보고 추적 가능했다.

## 8. 운영 팁
- 1차 점검은 `빠른 추출`
- 진짜 깊게 파야 할 때만 `전체 추출`
- 다운로드한 zip은 PC에서 풀고 `SUMMARY.md`부터 읽는 것이 가장 빠르다
