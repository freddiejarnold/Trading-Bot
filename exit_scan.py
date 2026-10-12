"""
exit_scan.py — weekly signal-based exit scan.

Runs every Sunday at 11pm UTC (triggered by cron-job.org → GitHub Actions weekly_exit).
For each open position:
  1. If rank_pct < exit_rank_threshold (0.33) → close (momentum signal deteriorated)
  2. If trading days held > hold_days_max (15) → close (time-based decay cap)

Stops and take-profits remain as the catastrophic floor (broker-side, always active).
This scan catches the slower deterioration that stops/TPs don't catch.

Academic basis:
  - Novy-Marx & Velikov (2016): exit when rank drops below top-33%
  - Daniel & Moskowitz (2016): momentum crashes accelerate; early exit critical
"""

import asyncio
import datetime
import json
import os
from pathlib import Path

import httpx

from config import CFG
from screener import rank_universe
from executor import get_open_positions, close_position


# ── Telegram (same pattern as run_scan.py) ────────────────────────────────────

async def _send_async(msg: str):
    token   = CFG.telegram_token   or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = CFG.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    token_preview   = f"{token[:6]}…{token[-4:]}" if len(token) > 10 else repr(token)
    chat_id_preview = repr(chat_id) if not chat_id else chat_id
    if not token or not chat_id:
        print(f"[Telegram SKIPPED] token={token_preview} chat_id={chat_id_preview}")
        print(f"[Telegram] {msg}")
        return
    print(f"[Telegram] Sending… token={token_preview} chat_id={chat_id_preview}")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            # HTML parse mode — safe as long as dynamic text is escaped with _h()
            r = await client.post(url, json={
                "chat_id":    chat_id,
                "text":       msg,
                "parse_mode": "HTML",
            }, timeout=10)
            print(f"[Telegram] Response: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[Telegram] Error: {e}")


def _send(msg: str):
    asyncio.run(_send_async(msg))


# ── Trade log helpers ─────────────────────────────────────────────────────────

_TRADE_LOG_PATH = "trade_log.json"


def _load_trade_log() -> list:
    path = Path(_TRADE_LOG_PATH)
    if not path.exists():
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def _entry_date(ticker: str, trade_log: list) -> datetime.date | None:
    """Find the most recent open entry date for a ticker from trade log."""
    entries = [
        t for t in trade_log
        if t.get("ticker") == ticker and t.get("status") == "open"
    ]
    if not entries:
        return None
    dates = []
    for t in entries:
        ts = t.get("entry_time") or t.get("timestamp") or t.get("date")
        if ts:
            try:
                if "T" in str(ts):
                    dates.append(datetime.datetime.fromisoformat(str(ts)).date())
                else:
                    dates.append(datetime.date.fromisoformat(str(ts)[:10]))
            except Exception:
                pass
    return min(dates) if dates else None  # earliest entry for this position


def _trading_days_held(entry_date: datetime.date) -> int:
    """Count trading days (Mon-Fri) between entry_date and today."""
    today = datetime.date.today()
    if entry_date is None or entry_date >= today:
        return 0
    count = 0
    d = entry_date
    while d < today:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:   # Monday=0 … Friday=4
            count += 1
    return count


# ── Main exit scan ────────────────────────────────────────────────────────────

def run_exit_scan(dry_run: bool = False) -> dict:
    """
    Score the full universe, then close any position whose momentum has faded
    or whose holding period has exceeded the hard cap.

    Parameters
    ----------
    dry_run : bool
        If True, print what would be closed without sending any orders.

    Returns
    -------
    dict with keys 'exits' (list) and 'held' (list of tickers).
    """
    print("\n" + "=" * 60)
    print(f"EXIT SCAN {'(DRY RUN) ' if dry_run else ''}| {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    # ── Config ────────────────────────────────────────────────────────────────
    exit_threshold = getattr(CFG, "exit_rank_threshold", 0.33)
    hold_days_max  = getattr(CFG, "hold_days_max", 15)

    # ── Fetch open positions ──────────────────────────────────────────────────
    open_positions = get_open_positions()     # {ticker: alpaca_position_obj}
    if not open_positions:
        print("No open positions — nothing to check.")
        _send("✅ *Weekly Exit Scan* — no open positions.")
        return {"exits": [], "held": []}

    open_tickers = list(open_positions.keys())
    print(f"Open positions: {open_tickers}")

    # ── Score full universe ───────────────────────────────────────────────────
    try:
        scores = rank_universe(return_all=True)   # {ticker: {rank_pct, ...}}
    except Exception as e:
        print(f"ERROR ranking universe: {e}")
        _send(f"⚠️ Exit scan: ranking failed — {e}\nPositions NOT changed.")
        return {"exits": [], "held": open_tickers, "error": str(e)}

    trade_log = _load_trade_log()
    exits = []
    held  = []

    for ticker, pos in open_positions.items():
        score_data = scores.get(ticker)
        exit_reason = None

        # ── 1. Signal check ───────────────────────────────────────────────────
        if score_data is not None:
            rank_pct = score_data.get("rank_pct", 1.0)
            print(f"  {ticker}: rank_pct={rank_pct:.3f}  (exit threshold={exit_threshold:.2f})")
            if rank_pct < exit_threshold:
                exit_reason = f"rank_pct {rank_pct:.3f} < {exit_threshold} (signal faded)"
        else:
            print(f"  {ticker}: not in ranked universe — closing")
            exit_reason = "ticker not in ranked universe"

        # ── 2. Time-based check ───────────────────────────────────────────────
        entry_dt  = _entry_date(ticker, trade_log)
        days_held = _trading_days_held(entry_dt)
        print(f"  {ticker}: {days_held} trading days held (max={hold_days_max})")
        if days_held > hold_days_max:
            time_reason = f"held {days_held} trading days > max {hold_days_max}"
            if exit_reason:
                exit_reason = f"{exit_reason}; {time_reason}"
            else:
                exit_reason = time_reason

        # ── 3. Execute exit ───────────────────────────────────────────────────
        if exit_reason:
            # Get P&L from Alpaca position object
            try:
                unrealized_pl    = float(pos.unrealized_pl)
                unrealized_plpc  = float(pos.unrealized_plpc) * 100
            except Exception:
                unrealized_pl    = None
                unrealized_plpc  = 0.0

            pl_str = f"{unrealized_plpc:+.1f}%" if unrealized_plpc is not None else "?"
            print(f"  ▶ {'[DRY RUN] ' if dry_run else ''}CLOSING {ticker}: {exit_reason} | P&L: {pl_str}")

            if not dry_run:
                result = close_position(ticker, reason=exit_reason)
                exits.append({
                    "ticker":          ticker,
                    "reason":          exit_reason,
                    "unrealized_pl":   unrealized_pl,
                    "unrealized_plpc": unrealized_plpc,
                    "rank_pct":        score_data.get("rank_pct") if score_data else None,
                    "days_held":       days_held,
                    "result":          result,
                })
            else:
                exits.append({
                    "ticker":          ticker,
                    "reason":          exit_reason,
                    "unrealized_plpc": unrealized_plpc,
                    "dry_run":         True,
                })
        else:
            held.append(ticker)
            print(f"  ✓ HOLD {ticker}")

    # ── Telegram summary ──────────────────────────────────────────────────────
    def _h(s: str) -> str:
        """Escape a string for Telegram HTML mode."""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if exits:
        lines = [
            f"🔴 <b>Weekly Exit Scan</b>{'  [DRY RUN]' if dry_run else ''}\n"
            f"<i>Residual momentum signal review — {datetime.datetime.utcnow().strftime('%d %b %Y')}</i>\n"
        ]
        for e in exits:
            pct      = e.get("unrealized_plpc", 0) or 0
            pl_emoji = "📈" if pct >= 0 else "📉"
            rank     = e.get("rank_pct")
            days     = e.get("days_held", 0)

            # Human-readable exit reason
            reasons = []
            if rank is not None and rank < exit_threshold:
                reasons.append(f"momentum rank fell to bottom {100 - int(rank * 100)}% of universe (rank {rank:.2f})")
            if days > hold_days_max:
                reasons.append(f"held {days} trading days (max {hold_days_max})")
            if not reasons:
                reasons.append(_h(e["reason"]))
            reason_str = " + ".join(reasons)

            lines.append(
                f"\n{pl_emoji} <b>{_h(e['ticker'])}</b>  {pct:+.1f}% P&L\n"
                f"   Closed: {reason_str}\n"
                f"   Thesis: momentum signal has faded — holding further risks mean-reversion loss"
            )
        if held:
            lines.append(f"\n✅ <b>Still held:</b> {', '.join(held)}")
            lines.append("<i>These positions remain in the top third of the momentum universe.</i>")
        _send("\n".join(lines))
    else:
        held_details = []
        for t in held:
            s = scores.get(t, {})
            rank = s.get("rank_pct", None)
            rank_str = f"rank {rank:.2f}" if rank is not None else "ranked"
            held_details.append(f"{t} ({rank_str})")
        _send(
            f"✅ <b>Weekly Exit Scan</b>\n"
            f"<i>{datetime.datetime.utcnow().strftime('%d %b %Y')}</i>\n\n"
            f"All {len(held)} position(s) pass — momentum signals still strong.\n\n"
            f"<b>Held:</b> {', '.join(held_details)}\n"
            f"<i>Next exit check: Sunday.</i>"
        )

    return {"exits": exits, "held": held}


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv or "--dry" in sys.argv
    result = run_exit_scan(dry_run=dry)
    print(f"\nResult: {json.dumps(result, indent=2, default=str)}")
