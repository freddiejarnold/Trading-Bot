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


def get_open_positions() -> dict:
    """
    Fetch currently open positions from Alpaca paper account.
    Returns {ticker: position_dict}. Used to prevent duplicate entries.
    """
    if CFG.mode == "virtual":
        return _vp.get("positions", {})
    try:
        api = _get_alpaca()
        positions = api.list_positions()
        return {p.symbol: p for p in positions}
    except Exception as e:
        print(f"Warning: could not fetch positions: {e}")
        return {}


def _get_ticker_risk_pct(ticker: str, equity: float, open_positions: dict) -> float:
    """
    Calculate total current risk percentage for a ticker across all open positions.
    Risk = sum of (entry - stop) * shares for each position in this ticker.
    """
    if CFG.mode == "virtual":
        pos = _vp["positions"].get(ticker)
        if not pos:
            return 0.0
        risk = abs(pos["entry"] - pos["stop"]) * pos["shares"]
        return risk / equity if equity > 0 else 0.0

    pos = open_positions.get(ticker)
    if not pos:
        return 0.0
    try:
        # Alpaca position object — estimate risk from current stop orders
        notional = float(pos.market_value)
        return notional / equity * 0.10  # approximate if no stop data
    except Exception:
        return 0.0


def _get_ticker_notional_pct(ticker: str, equity: float, open_positions: dict) -> float:
    """Total notional allocation to a ticker as a % of equity."""
    pos = open_positions.get(ticker)
    if not pos:
        return 0.0
    try:
        return abs(float(pos.market_value)) / equity
    except Exception:
        return 0.0


def _get_tranche_count(ticker: str, open_positions: dict) -> int:
    """How many tranches (separate entries) exist for this ticker."""
    # For paper/live: Alpaca shows one aggregated position per ticker
    # We track tranches via state file
    import json, os
    state_file = "tranche_state.json"
    if os.path.exists(state_file):
        state = json.load(open(state_file))
        return state.get(ticker, 0)
    return 1 if ticker in open_positions else 0


def _record_tranche(ticker: str):
    """Record a new tranche entry for this ticker."""
    import json, os
    state_file = "tranche_state.json"
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}
    state[ticker] = state.get(ticker, 0) + 1
    json.dump(state, open(state_file, "w"))


def _clear_tranche(ticker: str):
    """Clear tranche count when a position is fully closed."""
    import json, os
    state_file = "tranche_state.json"
    if os.path.exists(state_file):
        state = json.load(open(state_file))
        state.pop(ticker, None)
        json.dump(state, open(state_file, "w"))


def pick_best_candidate(candidates: dict) -> dict | None:
    """
    Deterministic order selection with controlled pyramiding.

    Rules:
    - Max CFG.max_positions total open positions
    - New ticker: allowed if position count < max and not already held
    - Existing ticker (pyramid): allowed if ALL of:
        1. Position is profitable (current price > entry for longs)
        2. Total risk on this ticker < CFG.max_ticker_risk_pct (default 3%)
        3. Total notional on this ticker < CFG.max_ticker_notional_pct (default 15%)
        4. Fewer than CFG.max_tranches (default 3) already open for this ticker
    - Pyramid size is halved each tranche (1% → 0.5% → 0.25% risk)
    """
    open_positions = get_open_positions()
    n_open = len(open_positions)
    equity = get_account_value()

    print(f"  Open positions: {n_open}/{CFG.max_positions} — {list(open_positions.keys())}")

    if n_open >= CFG.max_positions:
        print(f"  Max positions reached ({CFG.max_positions}) — no new entry")
        return None

    longs  = candidates.get("long", [])
    shorts = candidates.get("short", [])
    all_c  = longs + shorts

    if not all_c:
        return None

    # Filter: must have a valid stop
    viable = [c for c in all_c if c.get("stop_long" if c["side"] == "long" else "stop_short")]
    if not viable:
        return None

    # Classify each candidate as new entry or pyramid
    new_entries = []
    pyramid_entries = []

    for c in viable:
        ticker = c["ticker"]
        side   = c["side"]

        if ticker not in open_positions:
            # New position — standard entry
            new_entries.append(c)
        else:
            # Existing position — check pyramid conditions
            pos = open_positions[ticker]
            try:
                avg_entry    = float(pos.avg_entry_price)
                current_price = float(pos.current_price)
            except Exception:
                continue

            # Condition 1: position must be profitable
            is_profitable = (
                current_price > avg_entry if side == "long"
                else current_price < avg_entry
            )
            if not is_profitable:
                print(f"  {ticker}: pyramid skipped — position not yet profitable")
                continue

            # Condition 2: total risk under per-ticker cap
            ticker_risk = _get_ticker_risk_pct(ticker, equity, open_positions)
            if ticker_risk >= CFG.max_ticker_risk_pct:
                print(f"  {ticker}: pyramid skipped — ticker risk {ticker_risk:.1%} >= {CFG.max_ticker_risk_pct:.0%} cap")
                continue

            # Condition 3: total notional under per-ticker cap
            ticker_notional = _get_ticker_notional_pct(ticker, equity, open_positions)
            if ticker_notional >= CFG.max_ticker_notional_pct:
                print(f"  {ticker}: pyramid skipped — notional {ticker_notional:.1%} >= {CFG.max_ticker_notional_pct:.0%} cap")
                continue

            # Condition 4: tranche limit
            tranches = _get_tranche_count(ticker, open_positions)
            if tranches >= CFG.max_tranches:
                print(f"  {ticker}: pyramid skipped — max tranches ({CFG.max_tranches}) reached")
                continue

            print(f"  {ticker}: pyramid ALLOWED — profitable, risk={ticker_risk:.1%}, notional={ticker_notional:.1%}, tranches={tranches}")
            c["is_pyramid"] = True
            c["tranche_num"] = tranches + 1
            # Halve risk per tranche: tranche 2 = 0.5%, tranche 3 = 0.25%
            c["size_fraction"] = 1.0 / (2 ** (tranches))
            pyramid_entries.append(c)

    # Prefer new entries over pyramids to maintain diversification
    # But if no new entries, allow best pyramid
    pool = new_entries if new_entries else pyramid_entries
    if not pool:
        print(f"  No viable candidates — all held and no pyramid conditions met")
        return None

    longs_pool  = [c for c in pool if c["side"] == "long"]
    shorts_pool = [c for c in pool if c["side"] == "short"]

    best_long  = max(longs_pool,  key=lambda x: x["primary_score"]) if longs_pool  and CFG.direction in ("long", "both")  else None
    best_short = min(shorts_pool, key=lambda x: x["primary_score"]) if shorts_pool and CFG.direction in ("short", "both") else None

    picks = [c for c in [best_long, best_short] if c is not None]
    if not picks:
        return None

    chosen = max(picks, key=lambda x: abs(x["primary_score"]))
    return chosen


def execute(candidate: dict | None, size_scalar: float = 1.0) -> dict:
    """
    Execute a trade for the selected candidate.
    size_scalar comes from strategy vol targeting (supplement §3.2).
    For pyramid entries, size_fraction further reduces the position size.
    """
    if candidate is None:
        return {"executed": False, "reason": "No qualifying candidate"}

    # Apply pyramid size reduction if this is an add-on entry
    pyramid_fraction = candidate.get("size_fraction", 1.0)
    effective_scalar = size_scalar * pyramid_fraction

    if candidate.get("is_pyramid"):
        tranche = candidate.get("tranche_num", 2)
        print(f"  Pyramid tranche #{tranche} — size reduced to {pyramid_fraction:.0%} of normal")
        _record_tranche(candidate["ticker"])

    if CFG.mode == "virtual":
        return _execute_virtual(candidate, effective_scalar)
    return _execute_alpaca(candidate, effective_scalar)


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
