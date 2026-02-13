# auto-trader

Binance USDT-M Futures auto-trader (Trader Engine + Discord Bot).

Detailed Korean guide: `USAGE_KO.md`
Discord-only Korean guide: `DISCORD_USAGE_KO.md`

Key components:
- `apps/trader_engine`: FastAPI control plane + scheduler + risk/execution (USDT-M futures only)
- `apps/discord_bot`: Discord slash-command remote for `/status`, `/start`, `/stop`, `/panic`, `/close`

## Safety (Read This First)

- USDT-M Futures only: spot/coin-m/withdrawal features are not implemented.
- Do not enable withdrawal permissions on the Binance API key used here. This project does not require withdrawals.
- `TRADING_DRY_RUN=true` blocks NEW entries (enter/scale/rebalance). It is enabled by default in `.env.example`.
- `/close` and `/panic` are allowed in dry-run by default for operational safety.
  - Set `DRY_RUN_STRICT=true` to block `/close` and `/panic` too (maximum safety).
- Default boot state is `STOPPED`. No orders are allowed until you call `/start`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
pip install -e ".[dev]"
copy .env.example .env
```

## Run (All-in-One, Recommended)

```powershell
.\.venv\Scripts\python.exe -m apps.run_all
```

- Starts `trader_engine` and `discord_bot` together.
- `Ctrl + C` stops both processes.

Options:

```powershell
.\.venv\Scripts\python.exe -m apps.run_all --engine-only
.\.venv\Scripts\python.exe -m apps.run_all --bot-only
```

## Run (Trader Engine only)

```powershell
.\.venv\Scripts\python.exe -m apps.trader_engine.main --api
```

### Control API quick check

```powershell
curl http://127.0.0.1:8000/status
curl -X POST http://127.0.0.1:8000/start
```

## Config (Single Source Of Truth)

Most trading/policy parameters live in SQLite as a singleton row in `risk_config` (id=1). `.env` is primarily for
infra/runtime (DB path, logging, API keys, dry-run, scheduler enable).

### Discord notifications (FINAL-3)

- `DISCORD_WEBHOOK_URL` set: `trader_engine` sends event/status notifications directly to Discord.
- `DISCORD_WEBHOOK_URL` empty: notifications are disabled and written to logs only.
- Status notification cadence uses `risk_config.notify_interval_sec` (default `1800`, i.e., every 30 minutes).

### User Stream WS (FINAL-5)

- Startup launches Binance Futures user stream service (`listenKey` + WS + keepalive).
- `/status` includes:
  - `ws_connected`
  - `last_ws_event_time`
  - `last_fill` summary

Minimal check:

1. Start API and confirm logs contain `user_stream_started` and `user_stream_connected`.
2. Hit `/status` and verify:
   - `ws_connected=true`
   - `last_ws_event_time` is not null after events arrive.
3. Place/close a small test order, then confirm logs show fill processing and `/status.last_fill` updates.

## Run (Discord Bot)

```powershell
.\.venv\Scripts\python.exe -m apps.discord_bot.bot
```

### Discord Panel (FINAL-6)

- `/panel` creates (or updates) one control-panel message in the channel.
- Controls:
  - Buttons: `Start`, `Stop`, `Panic`, `Refresh`
  - Select: `Preset` (`conservative|normal|aggressive`)
  - Select: `Exec mode` (`LIMIT|MARKET|SPLIT`)
  - Modals: `Risk Basic` + `Risk Adv` (Discord modal field limit requires split)
- Only administrators can operate panel controls.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

### Discord Intents/Permissions Checklist

1. Bot intents:
   - `discord.Intents.default()` is enough for slash commands + UI interactions.
2. Bot OAuth2 scopes:
   - `bot`
   - `applications.commands`
3. Recommended bot permissions in target channel:
   - `Send Messages`
   - `Embed Links`
   - `Read Message History`
   - `Use Application Commands`
4. Panel operation permission:
   - User must have `Administrator` permission in the guild.
