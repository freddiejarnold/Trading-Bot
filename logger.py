"""
logger.py — pushes trade and regime data to the persistent dashboard.

Called from run_scan.py after every scan and trade.
Uses the Anthropic API to write directly to the dashboard's artifact db.
No extra dependencies — uses the anthropic client already in requirements.txt.

Setup: add DASHBOARD_ARTIFACT_ID to GitHub Secrets.
Value: the artifact ID from the dashboard URL, e.g. 9hdrEWv4W2j1nTPa3Eym8k
"""

import os, json
from datetime import datetime, date
import anthropic

ARTIFACT_ID   = os.environ.get("DASHBOARD_ARTIFACT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _client():
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def _doc_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def log_trade(result: dict, candidate: dict):
    """
    Push a new trade entry to the dashboard db.
    Called immediately after execute() returns a successful trade.
    """
    if not ARTIFACT_ID or not result.get("executed"):
        return

    try:
        client = _client()

        ticker = result.get("ticker", "")
        side   = result.get("side", "long")
        entry  = result.get("entry", 0)
        stop   = result.get("stop", 0)
        tp     = result.get("take_profit", 0)
        shares = result.get("shares", 0)

        # Calculate risk amount
        risk_per_share = abs(entry - stop)
        risk_dollars   = round(risk_per_share * shares, 2)

        doc_id = _doc_id("trade")

        client.beta.artifacts.database.documents.set(
            artifact_id  = ARTIFACT_ID,
            collection   = "trades",
            document_id  = doc_id,
            document     = {
                "ticker":        ticker,
                "side":          side,
                "entry":         entry,
                "stop":          stop,
                "take_profit":   tp,
                "shares":        shares,
                "risk_dollars":  risk_dollars,
                "entry_date":    str(date.today()),
                "status":        "open",
                "mode":          result.get("mode", "paper"),
                "port_weight":   result.get("port_weight", 1.0),
                "primary_score": candidate.get("primary_score", 0),
                "strategy":      candidate.get("strategy", "residual_mom"),
                "tt_conditions": candidate.get("trend_template", {}).get("conditions_met", 0),
                "notes":         f"Auto-logged by bot. Score={candidate.get('primary_score', 0):.3f}",
                "created_at":    datetime.now().isoformat(),
            }
        )
        print(f"Dashboard: trade logged — {ticker} {side} @ ${entry}")

    except Exception as e:
        print(f"Dashboard log_trade error (non-fatal): {e}")


def log_position_closed(ticker: str, exit_price: float, exit_reason: str):
    """
    Mark an open trade as closed when Alpaca reports the bracket order filled.
    Finds the most recent open trade for this ticker and closes it.
    """
    if not ARTIFACT_ID:
        return

    try:
        client = _client()

        # Find the open trade for this ticker
        docs = client.beta.artifacts.database.documents.list(
            artifact_id = ARTIFACT_ID,
            collection  = "trades",
        )

        open_trade = None
        for doc in docs.documents:
            data = doc.document
            if data.get("ticker") == ticker and data.get("status") == "open":
                open_trade = doc
                break

        if not open_trade:
            print(f"Dashboard: no open trade found for {ticker}")
            return

        data = open_trade.document
        entry  = data.get("entry", 0)
        shares = data.get("shares", 0)
        stop   = data.get("stop", 0)
        side   = data.get("side", "long")

        # Calculate P&L and R multiple
        if side == "long":
            pnl = (exit_price - entry) * shares
        else:
            pnl = (entry - exit_price) * shares

        risk_per_share = abs(entry - stop)
        r_multiple = pnl / (risk_per_share * shares) if risk_per_share > 0 else 0

        client.beta.artifacts.database.documents.update(
            artifact_id  = ARTIFACT_ID,
            collection   = "trades",
            document_id  = open_trade.id,
            document     = {
                "status":      "closed",
                "exit_price":  exit_price,
                "exit_date":   str(date.today()),
                "exit_reason": exit_reason,
                "pnl":         round(pnl, 2),
                "r_multiple":  round(r_multiple, 3),
                "closed_at":   datetime.now().isoformat(),
            }
        )
        print(f"Dashboard: {ticker} closed @ ${exit_price} | P&L=${pnl:.2f} | R={r_multiple:.2f}")

    except Exception as e:
        print(f"Dashboard log_position_closed error (non-fatal): {e}")


def log_regime(scan: dict, regime: dict):
    """
    Push daily regime snapshot to the dashboard.
    Called once per scan from run_scan.py.
    """
    if not ARTIFACT_ID:
        return

    try:
        client = _client()
        today  = str(date.today())

        client.beta.artifacts.database.documents.set(
            artifact_id  = ARTIFACT_ID,
            collection   = "regime",
            document_id  = f"regime_{today.replace('-', '')}",
            document     = {
                "date":               today,
                "market_state_ok":    regime.get("market_state_ok", True),
                "vix_rank":           regime.get("vix_rank", 50),
                "vix_secondary_block": regime.get("vix_secondary_block", False),
                "n_long_candidates":  len(scan.get("long", [])),
                "n_short_candidates": len(scan.get("short", [])),
                "size_scalar":        scan.get("size_scalar", 1.0),
                "timestamp":          datetime.now().isoformat(),
            }
        )
        print(f"Dashboard: regime logged for {today}")

    except Exception as e:
        print(f"Dashboard log_regime error (non-fatal): {e}")


def check_and_close_positions():
    """
    Check Alpaca for any bracket orders that filled (stop or TP hit)
    and automatically close the matching dashboard trade.
    Called at the start of each scan.
    """
    if not ARTIFACT_ID:
        return

    try:
        import alpaca_trade_api as tradeapi
        from config import CFG

        api = tradeapi.REST(
            os.environ.get("ALPACA_API_KEY", CFG.alpaca_key),
            os.environ.get("ALPACA_SECRET_KEY", CFG.alpaca_secret),
            os.environ.get("ALPACA_BASE_URL", CFG.alpaca_base_url),
        )

        # Get recent filled orders (last 24h)
        from datetime import timedelta
        since = (datetime.now() - timedelta(hours=24)).isoformat() + "Z"
        orders = api.list_orders(status="filled", after=since, limit=50)

        # Look for filled stop-loss or take-profit orders (sell side of a bracket)
        for order in orders:
            if order.side == "sell" and order.order_class == "bracket":
                ticker      = order.symbol
                exit_price  = float(order.filled_avg_price or 0)
                exit_reason = "stop" if order.type == "stop" else "take_profit"
                log_position_closed(ticker, exit_price, exit_reason)

    except Exception as e:
        print(f"Dashboard check_and_close error (non-fatal): {e}")
