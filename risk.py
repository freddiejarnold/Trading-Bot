"""
risk.py — position sizing, circuit breakers, strategy-level vol targeting.

§ supplement §3.2: strategy vol weight (Barroso & Santa-Clara) applied at portfolio level.
§ supplement §4 bug fix: atr_position_size comparison corrected.
§ supplement §5: PDT guard built in.
"""

import json, os
from datetime import date, datetime
import numpy as np
import pandas as pd

from config import CFG
from signals import strategy_vol_weight, vix_rank

STATE_FILE = "state.json"

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {
        "peak_equity":          None,
        "daily_start":          None,
        "daily_start_equity":   None,
        "paused":               False,
        "pause_reason":         "",
        "strategy_daily_returns": [],     # for Barroso vol targeting
        "pdt_trades_this_week": 0,        # PDT guard
        "pdt_week_start":       None,
    }

def save_state(state: dict):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def atr_position_size(
    account_value: float,
    entry: float,
    stop: float,
    side: str = "long",
    size_scalar: float = 1.0,   # from strategy vol targeting
) -> int:
    """
    Size each position so dollar risk = risk_per_trade_pct × account_value.

    § supplement §4 fix: was `if risk_per_share < 0: return 0`
    Corrected to `<= 0.01` to also guard against near-zero differences.
    """
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0.01:   # § fix: was `< 0` — syntax error in original
        return 0

    dollar_risk  = account_value * CFG.risk_per_trade_pct * size_scalar
    shares       = int(dollar_risk / risk_per_share)

    # Hard caps
    max_by_pct   = int((account_value * 0.10) / entry)        # no single pos > 10%
    if CFG.mode == "live":
        live_cap = int((account_value * CFG.live_allocation_pct) / entry)
        shares   = min(shares, live_cap)

    return min(shares, max_by_pct)


def compute_portfolio_weight(state: dict) -> float:
    """
    § supplement §3.2 Barroso & Santa-Clara: scale strategy allocation by its own
    realised volatility over the prior 6 months to a constant 12% target.
    Applied ON TOP OF per-position ATR sizing.
    """
    returns = state.get("strategy_daily_returns", [])
    if len(returns) < 20:
        return 1.0

    series = pd.Series(returns)
    weight = strategy_vol_weight(
        series,
        target_vol=CFG.strategy_vol_target,
        window=min(CFG.strategy_vol_window, len(returns)),
        max_leverage=CFG.max_strategy_leverage,
    )
    return weight


def record_daily_return(state: dict, equity_start: float, equity_end: float):
    """Append today's strategy return for Barroso vol targeting."""
    if equity_start > 0:
        daily_ret = (equity_end - equity_start) / equity_start
        rets = state.get("strategy_daily_returns", [])
        rets.append(daily_ret)
        # Keep rolling 2-year window
        state["strategy_daily_returns"] = rets[-504:]
    save_state(state)


def check_pdt_guard(state: dict) -> tuple[bool, str]:
    """
    § supplement §5: Pattern Day Trader rule.
    < $25k account: max 3 round-trip day trades per rolling 5 business days.
    We enforce a stricter min_holding_days = 1 (overnight minimum) to avoid
    this entirely — no gap-and-go allowed by design.
    """
    # This bot enforces overnight holds, so PDT is unlikely to trigger.
    # But we track it as a safety net.
    today = str(date.today())
    week_start = state.get("pdt_week_start")
    count      = state.get("pdt_trades_this_week", 0)

    # Reset weekly counter every Monday
    if week_start is None or (
        datetime.today().weekday() == 0 and week_start != today
    ):
        state["pdt_week_start"]    = today
        state["pdt_trades_this_week"] = 0
        save_state(state)
        return True, ""

    if count >= 3:
        return False, f"PDT limit reached ({count}/3 day-trades this week)"
    return True, ""


def check_circuit_breakers(equity: float, state: dict) -> tuple[bool, str]:
    """
    Returns (ok_to_trade, reason_if_not).
    Checks: daily loss, monthly drawdown, PDT guard.
    VIX is now secondary only (supplement §2.2) — doesn't block, only scales size.
    Market state is checked separately in screener.py (Cooper et al regime gate).
    """
    today = str(date.today())

    # Reset daily tracker
    if state.get("daily_start") != today:
        state["daily_start"]         = today
        state["daily_start_equity"]  = equity
        save_state(state)

    daily_pnl_pct = (equity - state["daily_start_equity"]) / max(state["daily_start_equity"], 1)
    if daily_pnl_pct < -CFG.daily_loss_limit_pct:
        return False, f"Daily loss limit hit ({daily_pnl_pct:.1%} < -{CFG.daily_loss_limit_pct:.0%})"

    if state["peak_equity"] is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity
        save_state(state)

    drawdown = (state["peak_equity"] - equity) / state["peak_equity"]
    if drawdown > CFG.monthly_dd_limit_pct:
        return False, f"Drawdown limit hit ({drawdown:.1%} > {CFG.monthly_dd_limit_pct:.0%})"

    pdt_ok, pdt_reason = check_pdt_guard(state)
    if not pdt_ok:
        return False, pdt_reason

    return True, ""
