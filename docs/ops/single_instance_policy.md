# Single-Instance Policy

## Policy
- Run exactly one engine process per exchange account.
- Running multiple processes against the same account is forbidden in production.
- Reason: process-level execution locks do not coordinate across multiple OS processes.

## Runtime Enforcement
- The engine acquires a non-blocking OS file lock at startup.
- Default lock file: `./run/engine.lock`.
- If the lock is already held, startup fails fast with:
  - `SINGLE_INSTANCE_LOCK_HELD: <path>`
- Override (dangerous, special cases only):
  - `ALLOW_MULTI_INSTANCE=1`
  - This skips the lock and logs a warning.

## Operational Guidance
- Use one service unit/container replica per account.
- Do not run duplicate `uvicorn` workers for the same account.
- Keep `ALLOW_MULTI_INSTANCE` unset in production.

## Troubleshooting `LOCK_HELD`
1. Identify the process holding the lock.
2. Stop the stale/duplicate process.
3. Restart a single engine instance.

PowerShell example:

```powershell
Get-Process | Where-Object { $_.ProcessName -match 'python|uvicorn' }
```

Linux example:

```bash
ps -ef | grep -E "python|uvicorn" | grep -v grep
```

## Deployment Caveats
- `systemd`: ensure only one service instance for the account.
- Docker/Kubernetes: replicas must stay at `1` per account.
