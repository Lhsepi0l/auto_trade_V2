# 릴리즈 검증 런북

## 1) 원커맨드 검증 실행

```bash
python scripts/verify_release.py
```

- 아래 항목을 순서대로 실행합니다.
  - `python -m compileall .`
  - `pytest -m "not e2e"`
  - `pytest`
  - `python scripts/smoke_test_mode.py`
  - 로컬 임시 SQLite 백업 self-test

## 2) 성공 기준

- 마지막 줄에 `VERIFY_OK`가 출력되면 전체 검증 성공입니다.
- 중간 단계 실패 시 `VERIFY_FAIL ...`를 출력하고 종료 코드 1로 끝납니다.

## 3) 운영 DB 백업 실행

```bash
python scripts/backup_db.py --db-path ./data/auto_trader.sqlite3 --output-dir ./backups --keep 20
```

- 성공 시 `BACKUP_OK <path>` 출력
- 백업 파일은 `backups/auto_trader_backup_YYYYMMDD_HHMMSS.sqlite3` 형식으로 생성됩니다.
