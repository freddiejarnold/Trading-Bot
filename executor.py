"""
executor.py — deterministic trade execution. Zero Claude calls here.

§ supplement §4: LLM removed from order path entirely.
The order decision is made by screener.py (pure signal logic).
Claude is used ONLY in analyst.py for weekly review notes — never for orders.

§ supplement §3.2: strategy vol weight applied as size_scalar on every order.
"""

import os
from datetime import date
from config import CFG
from risk import atr_position_size, compute_portfolio_weight, load_state, save_state

# ── Virtual portfolio (in-memory, no broker required) ─────────────────────────
_vp = {
    "cash":      10_000.0,
    "positions": {},          # ticker → position dict
    "trades":    [],          # completed trade log
    "equity_history": [],     # daily equity for Barroso vol targeting
}

def get_account_value() -> float:
    if CFG.mode == "virtual":
        pos_value = sum(
            p["shares"] * p.get("current_price", p["entry"])
            for p in _vp["positions"].values()
        )
        return round(_vp["cash"] + pos_value, 2)

    api = _get_alpaca()
    return float(api.get_account().equity)


def pick_best_candidate(candidates: dict) -> dict | None:
    """
    § supplement §4: this replaces `analyze_with_claude(candidates)`.
    Pure deterministic selection — highest primary_score long or lowest short.
    Claude never touches this path.
    """
    longs  = candidates.get("long", [])
    shorts = candidates.get("short", [])
    all_c  = longs + shorts

    if not all_c:
        return None

    # Filter: must have a valid stop
    viable = [c for c in all_c if c.get("stop_long" if c["side"] == "long" else "stop_short")]
    if not viable:
        return None

    # Primary: highest |score|, filtered by side
    # Longs: highest positive score; Shorts: most negative score
    if CFG.direction in ("long", "both") and longs:
        best_long = max(longs, key=lambda x: x["primary_score"])
    else:
        best_long = None

    if CFG.direction in ("short", "both") and shorts:
        # Most negative = strongest short signal
        best_short = min(shorts, key=lambda x: x["primary_score"])
    else:
        best_short = None

    # Pick whichever has the stronger absolute score
    candidates_to_pick = [c for c in [best_long, best_short] if c is not None]
    if not candidates_to_pick:
        return None

    return max(candidates_to_pick, key=lambda x: abs(x["primary_score"]))


def execute(candidate: dict | None, size_scalar: float = 1.0) -> dict:
    """
    Execute a trade for the selected candidate.
    size_scalar comes from strategy vol targeting (supplement §3.2).
    """
    if candidate is None:
        return {"executed": False, "reason": "No qualifying candidate"}

    if CFG.mode == "virtual":
        return _execute_virtual(candidate, size_scalar)
    return _execute_alpaca(candidate, size_scalar)


def _execute_virtual(candidate: dict, size_scalar: float = 1.0) -> dict:
    equity  = get_account_value()
    state   = load_state()

    # Apply strategy vol weight (supplement §3.2)
    port_weight = compute_portfolio_weight(state)
    effective_scalar = size_scalar * port_weight

    ticker = candidate["ticker"]
    side   = candidate["side"]
    price  = candidate["price"]
    stop   = candidate["stop_long"]  if side == "long" else candidate["stop_short"]
    tp     = candidate["tp_long"]    if side == "long" else candidate["tp_short"]

    if stop is None:
        return {"executed": False, "reason": f"No stop available for {ticker}"}

    shares = atr_position_size(equity, price, stop, side, size_scalar=effective_scalar)
    if shares < 1:
        return {"executed": False, "reason": f"Position size < 1 share (size_scalar={effective_scalar:.2f})"}

    cost = shares * price
    if _vp["cash"] < cost:
        return {"executed": False, "reason": f"Insufficient virtual cash (need £{cost:.0f}, have £{_vp['cash']:.0f})"}

    _vp["cash"] -= cost
    _vp["positions"][ticker] = {
        "side":          side,
        "shares":        shares,
        "entry":         price,
        "stop":          stop,
        "take_profit":   tp,
        "current_price": price,
        "entry_date":    str(date.today()),
        "strategy":      candidate.get("primary_score", 0),
    }

    trade = {
        "executed":       True,
        "mode":           "virtual",
        "ticker":         ticker,
        "side":           side,
        "shares":         shares,
        "entry":          price,
        "stop":           stop,
        "take_profit":    tp,
        "risk_amount":    round(shares * abs(price - stop), 2),
        "port_weight":    round(port_weight, 3),
        "size_scalar":    round(effective_scalar, 3),
        "date":           str(date.today()),
    }
    _vp["trades"].append(trade)
    return trade


def _get_alpaca():
    import alpaca_trade_api as tradeapi
    return tradeapi.REST(CFG.alpaca_key, CFG.alpaca_secret, CFG.alpaca_base_url)


def _execute_alpaca(candidate: dict, size_scalar: float = 1.0) -> dict:
    api    = _get_alpaca()
    equity = float(api.get_account().equity)
    state  = load_state()

    port_weight      = compute_portfolio_weight(state)
    effective_scalar = size_scalar * port_weight

    ticker = candidate["ticker"]
    side   = candidate["side"]
    price  = candidate["price"]
    stop   = candidate["stop_long"]  if side == "long" else candidate["stop_short"]
    tp     = candidate["tp_long"]    if side == "long" else candidate["tp_short"]

    if stop is None:
        return {"executed": False, "reason": f"No stop for {ticker}"}

    shares = atr_position_size(equity, price, stop, side, size_scalar=effective_scalar)
    if shares < 1:
        return {"executed": False, "reason": "Size < 1"}

    alpaca_side = "buy" if side == "long" else "sell"
    try:
        order = api.submit_order(
            symbol=ticker, qty=shares, side=alpaca_side,
            type="market", time_in_force="day",
            order_class="bracket",
            stop_loss={"stop_price": str(round(stop, 2))},
            take_profit={"limit_price": str(round(tp, 2))},
        )
    except Exception as e:
        return {"executed": False, "reason": f"Alpaca error: {e}"}

    return {
        "executed":     True,
        "mode":         CFG.mode,
        "order_id":     order.id,
        "ticker":       ticker,
        "side":         side,
        "shares":       shares,
        "entry":        price,
        "stop":         round(stop, 2),
        "take_profit":  round(tp, 2),
        "port_weight":  round(port_weight, 3),
    }
