## Assumption Check-in

- Live control HTTP is intended to stay on `127.0.0.1:8101`, based on the current defaults in `v2/scripts/run_stack.sh`, `v2/scripts/install_systemd_stack.sh`, and `v2/systemd/v2-stack.service`.
- Runtime operators can issue privileged state-changing calls such as `POST /set` through the FastAPI control surface in `v2/control/api.py`.
- Binance API credentials are loaded from `.env` through `load_effective_config(...)` and become integrity-critical runtime assets.
- Discord and local control HTTP together form the main operator trust boundary; there is no application-layer auth visible on the control HTTP path itself.
- This note covers runtime/control exposure only. Strategy quality, exchange alpha, and market risk are out of scope.

## Evidence Anchors

- Control HTTP listener defaults: `v2/run.py`, `v2/scripts/run_stack.sh`, `v2/scripts/install_systemd_stack.sh`, `v2/systemd/v2-stack.service`
- Control API privileged mutation path: `v2/control/api.py` (`/set`)
- Secret loading path: `v2/config/loader.py`

## Top Risk Themes

1. If localhost binding is ever relaxed, the control surface becomes high-risk immediately because privileged state mutation is exposed over HTTP without visible request authentication in app code.
2. Binance credentials in `.env` remain a high-value asset; host compromise or accidental control-surface exposure becomes exchange-account compromise in practice.
3. Discord/operator flows are integrity-critical because they can influence live trading behavior even when the market-side strategy is correct.

## Open Questions

1. Will control HTTP ever be exposed beyond localhost via reverse proxy, VPN, or direct WAN routing?
2. Is there any authn/authz layer outside app code for `/set`, `/panic`, or similar privileged operations?
3. Is the target deployment single-operator only, or do multiple humans/bots share the same control path?
