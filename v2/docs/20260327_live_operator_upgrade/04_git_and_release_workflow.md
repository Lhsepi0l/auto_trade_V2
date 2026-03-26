# Git / GitHub / Release 기준

## 1. 결론
- `git`은 필수
- 원격 기준 저장소는 `GitHub(origin)` 하나만 사용
- `GitLab`은 현재 기준으로 병행 사용하지 않는다

## 2. 왜 이렇게 가는가
- 로컬 개발과 서버 운영 기준을 하나로 맞춰야 한다.
- 서버가 pull 받을 원본은 반드시 하나여야 한다.
- "어떤 코드가 실거래에 올라갔는지"를 빠르게 특정해야 한다.
- 장애 시 즉시 롤백 커밋/태그를 찾을 수 있어야 한다.

## 3. 브랜치 기준
- `migration/web-operator-panel`
  - 현재 개발/통합 작업 브랜치
- `main`
  - 안정 운영 최종 기준 브랜치

## 4. 서버 원칙
- 서버는 pull-only
- 서버에서 직접 코드 수정/커밋 금지
- GitHub에 push 된 커밋만 운영 반영

## 5. 태그 기준
- 오늘부터 운영용 태그를 함께 관리한다.
- 예:
  - `live-vanta-20260327-r1`
- 의미:
  - `live`: 운영 레인
  - `vanta`: 기억하기 쉬운 codename
  - `20260327`: 날짜
  - `r1`: revision

## 6. 추천 명령

### 서버 반영
```bash
bash v2/scripts/update_server_from_git.sh --branch migration/web-operator-panel --restart
```

### 안정판 전환
```bash
bash v2/scripts/update_server_from_git.sh --branch main --restart
```

### 현재 상태 확인
```bash
git branch --show-current
git rev-parse HEAD
git tag --list | tail -n 20
```

## 7. 운영자 관점 한 줄 기준
- 개발은 작업 브랜치에서
- 안정화되면 main으로
- 서버는 GitHub에서 pull만
- 실거래 반영 시 태그도 같이 남김
