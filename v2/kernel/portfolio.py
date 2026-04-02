from __future__ import annotations

from dataclasses import dataclass, field

from v2.kernel.contracts import Candidate


def portfolio_bucket_for_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if normalized in {"BTCUSDT", "ETHUSDT"}:
        return "majors"
    if normalized in {"SOLUSDT", "BNBUSDT"}:
        return "alts"
    return normalized or "unknown"


@dataclass(frozen=True)
class PortfolioRoutingConfig:
    max_open_positions: int = 1
    max_new_entries_per_tick: int = 1


@dataclass(frozen=True)
class PortfolioRoutingResult:
    selected: list[Candidate] = field(default_factory=list)
    blocked: list[tuple[Candidate, str]] = field(default_factory=list)
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    open_position_count: int = 0
    max_open_positions: int = 1


def route_ranked_candidates(
    *,
    candidates: list[Candidate],
    open_symbols: set[str],
    allow_reentry: bool,
    config: PortfolioRoutingConfig,
) -> PortfolioRoutingResult:
    normalized_open_symbols = {
        str(symbol).strip().upper() for symbol in open_symbols if str(symbol).strip()
    }
    occupied_buckets = {portfolio_bucket_for_symbol(symbol) for symbol in normalized_open_symbols}
    max_open_positions = max(int(config.max_open_positions), 1)
    free_slots = max(max_open_positions - len(normalized_open_symbols), 0)
    per_tick_limit = max(int(config.max_new_entries_per_tick), 1)
    remaining_entries = min(free_slots, per_tick_limit)

    blocked_reasons: dict[str, int] = {}
    blocked: list[tuple[Candidate, str]] = []
    selected: list[Candidate] = []
    selected_symbols: set[str] = set()

    for candidate in candidates:
        candidate_symbol = str(candidate.symbol).strip().upper()
        candidate_bucket = str(
            candidate.portfolio_bucket or portfolio_bucket_for_symbol(candidate_symbol)
        ).strip()

        if (not allow_reentry) and (
            candidate_symbol in normalized_open_symbols or candidate_symbol in selected_symbols
        ):
            reason = "portfolio_symbol_open"
        elif len(normalized_open_symbols) + len(selected_symbols) >= max_open_positions:
            reason = "portfolio_cap_reached"
        elif candidate_bucket and candidate_bucket in occupied_buckets:
            reason = "portfolio_bucket_cap"
        elif remaining_entries <= 0:
            reason = "portfolio_cap_reached"
        else:
            selected.append(candidate)
            selected_symbols.add(candidate_symbol)
            if candidate_bucket:
                occupied_buckets.add(candidate_bucket)
            remaining_entries -= 1
            continue

        blocked.append((candidate, reason))
        blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1

    return PortfolioRoutingResult(
        selected=selected,
        blocked=blocked,
        blocked_reasons=blocked_reasons,
        open_position_count=len(normalized_open_symbols),
        max_open_positions=max_open_positions,
    )
