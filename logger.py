"""
logger.py — persists trade and regime data to trade_log.json in the repo.

The dashboard fetches this file directly from GitHub raw content URL.
No external db API needed — just a JSON file committed to the repo.

After each scan, the updated JSON is written to disk. GitHub Actions
commits it back to the repo automatically via the workflow.
"""

import os, json
from datetime import datetime, date

LOG_FILE = "trade_log.json"


def _load() -> dict:
    if os.path.exists(LOG_FILE):
        try:
            return json.load(open(LOG_FILE))
        except Exception:
            pass
    return {"trades": [], "regime": [], "last_updated": ""}


def _save(data: dict):
    data["last_updated"] = datetime.now().isoformat()
    json.dump(data, open(LOG_FILE, "w"), indent=2, default=str)


def log_trade(result: dict, candidate: dict):
    """Log a new trade entry."""
    if not result.get("executed"):
        return
    try:
        data   = _load()
        ticker = result.get("ticker", "")
        entry  = result.get("entry", 0)
        stop   = result.get("stop", 0)
        shares = result.get("shares", 0)

        trade = {
            "id":            f"trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "ticker":        ticker,
            "side":          result.get("side", "long"),
            "entry":         entry,
            "stop":          stop,
            "take_profit":   result.get("take_profit", 0),
            "shares":        shares,
            "risk_dollars":  round(abs(entry - stop) * shares, 2),
            "entry_date":    str(date.today()),
            "status":        "open",
            "mode":          result.get("mode", "paper"),
            "port_weight":   result.get("port_weight", 1.0),
            "primary_score": candidate.get("primary_score", 0) if candidate else 0,
            "tt_conditions": candidate.get("trend_template", {}).get("conditions_met", 0) if candidate else 0,
            "pnl":           None,
            "r_multiple":    None,
            "exit_price":    None,
            "exit_date":     None,
            "exit_reason":   None,
            "created_at":    datetime.now().isoformat(),
        }

        data["trades"].append(trade)
        _save(data)
        print(f"Dashboard: trade logged — {ticker} {result.get('side')} @ ${entry}")

    except Exception as e:
        print(f"Dashboard log_trade error (non-fatal): {e}")


def log_regime(scan: dict, regime: dict):
    """Log daily regime snapshot."""
    try:
        data  = _load()
        today = str(date.today())

        # Update existing entry for today or append new one
        existing = next((r for r in data["regime"] if r.get("date") == today), None)
        entry = {
            "date":                today,
            "market_state_ok":     regime.get("market_state_ok", True),
            "vix_rank":            regime.get("vix_rank", 50),
            "vix_secondary_block": regime.get("vix_secondary_block", False),
            "n_long_candidates":   len(scan.get("long", [])),
            "n_short_candidates":  len(scan.get("short", [])),
            "size_scalar":         scan.get("size_scalar", 1.0),
            "timestamp":           datetime.now().isoformat(),
        }

        if existing:
            idx = data["regime"].index(existing)
            data["regime"][idx] = entry
        else:
            data["regime"].insert(0, entry)

        # Keep last 60 days
        data["regime"] = data["regime"][:60]
        _save(data)
        print(f"Dashboard: regime logged for {today}")

    except Exception as e:
        print(f"Dashboard log_regime error (non-fatal): {e}")


def check_and_close_positions():
    """Check Alpaca for closed bracket orders and mark trades as closed."""
    try:
        import alpaca_trade_api as tradeapi
        from config import CFG
        from datetime import timedelta

        api = tradeapi.REST(
            os.environ.get("ALPACA_API_KEY", CFG.alpaca_key),
            os.environ.get("ALPACA_SECRET_KEY", CFG.alpaca_secret),
            os.environ.get("ALPACA_BASE_URL", CFG.alpaca_base_url),
        )

        since  = (datetime.now() - timedelta(hours=24)).isoformat() + "Z"
        orders = api.list_orders(status="filled", after=since, limit=50)
        data   = _load()

        for order in orders:
            if order.side == "sell":
                ticker      = order.symbol
                exit_price  = float(order.filled_avg_price or 0)
                exit_reason = "stop" if order.type == "stop" else "take_profit"

                # Find matching open trade
                for trade in data["trades"]:
                    if trade["ticker"] == ticker and trade["status"] == "open":
                        entry  = trade["entry"]
                        shares = trade["shares"]
                        stop   = trade["stop"]
                        side   = trade["side"]

                        pnl = (exit_price - entry) * shares if side == "long" else (entry - exit_price) * shares
                        risk = abs(entry - stop) * shares
                        r   = pnl / risk if risk > 0 else 0

                        trade.update({
                            "status":      "closed",
                            "exit_price":  exit_price,
                            "exit_date":   str(date.today()),
                            "exit_reason": exit_reason,
                            "pnl":         round(pnl, 2),
                            "r_multiple":  round(r, 3),
                            "closed_at":   datetime.now().isoformat(),
                        })
                        print(f"Dashboard: {ticker} closed @ ${exit_price} P&L=${pnl:.2f} R={r:.2f}")
                        break

        _save(data)

    except Exception as e:
        print(f"Dashboard check_and_close error (non-fatal): {e}")
