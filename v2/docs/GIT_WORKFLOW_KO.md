# Git / GitHub 운영 기준

## 1. 원칙
- 버전 관리는 `git`으로 한다.
- 원격 기준 저장소는 `GitHub(origin)` 하나로 고정한다.
- 서버는 `git pull`만 하고, 서버에서 직접 코드 수정은 하지 않는다.
- 실거래 반영 기준은 "GitHub에 push 된 커밋"이다.

## 2. 기준 브랜치
- `main`
  - 가장 안정적인 기준 브랜치
  - 실거래 운영 최종 기준은 이 브랜치로 수렴시킨다
- 작업 브랜치
  - 기능/버그 단위로 사용
  - 현재 통합 작업 브랜치는 `migration/web-operator-panel`
- `hotfix/...`
  - 운영 중 급한 수정
- `experiment/...`
  - 백테스트/연구 전용

## 3. 현재 추천 운영 방식
- 당분간 개발은 `migration/web-operator-panel`에서 계속한다.
- 충분히 안정화되면 `main`으로 병합한다.
- 실거래 서버는 한 번에 하나의 브랜치만 pull 한다.
- "지금 서버가 어느 브랜치를 기준으로 운영 중인지"를 항상 명확히 유지한다.

## 4. 커밋 규칙
- 작은 단위로 자주 커밋한다.
- 커밋 메시지는 Conventional Commit 스타일을 유지한다.
  - `feat: ...`
  - `fix: ...`
  - `docs: ...`
  - `chore: ...`
- 서버 반영 전에는 반드시 로컬 검증을 끝낸다.

## 5. 서버 반영 규칙
- 서버는 원칙적으로 아래 순서만 사용한다.
  1. `git fetch origin`
  2. `git checkout <branch>`
  3. `git pull --ff-only origin <branch>`
  4. 서비스 재시작
- `git pull`이 실패하면 서버에 로컬 변경이 있다는 뜻이므로 먼저 원인을 확인한다.
- 서버에서 테스트용 임시 수정, 수동 편집, 임시 stash 운용은 금지한다.

## 6. 태그 기준
- 실거래에 올린 시점은 태그로 남기는 것이 좋다.
- 예시:
  - `prod-20260327-a`
  - `prod-20260327-b`
- 의미:
  - 언제 어떤 코드가 운영에 올라갔는지 즉시 알 수 있다.
  - 롤백 기준점을 명확하게 잡을 수 있다.

## 7. 롤백 기준
- 서버 문제 발생 시 임의 수정하지 말고 이전 안정 커밋/태그로 돌아간다.
- 예:
  - `git checkout main`
  - `git pull --ff-only origin main`
  - 또는 특정 태그/커밋 checkout 후 재기동

## 8. 하지 말아야 할 것
- 서버에서 직접 코드 수정
- GitHub에 없는 커밋을 운영 기준으로 사용
- dirty worktree 상태에서 pull 강행
- 실거래 서버를 실험 브랜치와 안정 브랜치 사이에서 자주 흔들기

## 9. 추천 명령
### 현재 상태 확인
```bash
git branch --show-current
git rev-parse HEAD
git status --short
```

### 서버 반영
```bash
bash v2/scripts/update_server_from_git.sh --branch migration/web-operator-panel --restart
```

### 실거래 기준 전환
```bash
bash v2/scripts/update_server_from_git.sh --branch main --restart
```
