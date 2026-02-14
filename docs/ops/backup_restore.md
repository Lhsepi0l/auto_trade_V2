# SQLite 백업/복구 운영 가이드

## 1) 백업 생성
기본 명령:

```bash
python scripts/backup_db.py
```

옵션 지정 예시:

```bash
python scripts/backup_db.py --db-path ./data/auto_trader.sqlite3 --output-dir ./backups --keep 20
```

- 백업 파일은 `backups/auto_trader_backup_YYYYMMDD_HHMMSS.sqlite3` 형태로 생성됩니다.
- 보존 개수(`--keep`)를 초과한 오래된 백업은 자동 삭제됩니다.
- 네트워크 호출 없이 로컬 SQLite `backup()` API만 사용합니다.

## 2) 복구 절차
1. 트레이딩 엔진/봇 프로세스를 먼저 중지합니다.
2. 복구 대상 백업 파일을 운영 DB 경로로 복사합니다.
3. 서비스 재기동 후 `/status`와 최근 로그로 정상 기동을 확인합니다.

PowerShell 예시:

```powershell
Copy-Item .\backups\auto_trader_backup_20260214_120000.sqlite3 .\data\auto_trader.sqlite3 -Force
```

## 3) 운영 권장치 (RPO/RTO)
- 권장 RPO: 1시간 이내 (최소 1시간 주기 백업)
- 권장 RTO: 15분 이내 (중지 -> 복사 -> 재기동 -> 상태 확인)

## 4) 점검 체크리스트
- 백업 스케줄 실행 여부
- 백업 파일 생성/용량 증가 여부
- 보존 정책(`--keep`) 적용 여부
- 월 1회 복구 리허설 수행 여부
