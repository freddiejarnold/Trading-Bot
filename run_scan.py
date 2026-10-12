"""
run_scan.py — single-execution entry point for GitHub Actions.

SCAN_TYPE env var controls behaviour:
  full          — market scan + trade if signal found
  morning_brief — positions summary + top signals, no trade
  weekly_review — Claude Haiku research note (Sundays)
  dual_mom      — monthly ISA sleeve signal (first Monday)
"""

import os, json, sys
from datetime import datetime, date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Inject API keys from environment
import config as _cfg_module
cfg = _cfg_module.CFG
cfg.alpaca_key       = os.environ.get("ALPACA_API_KEY", "")
cfg.alpaca_secret    = os.environ.get("ALPACA_SECRET_KEY", "")
cfg.alpaca_base_url  = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
cfg.finnhub_key      = os.environ.get("FINNHUB_KEY", "")
cfg.anthropic_key    = os.environ.get("ANTHROPIC_API_KEY", "")
cfg.telegram_token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
cfg.mode             = "paper"

from screener import run_full_scan, dual_momentum_signal
from executor import execute, pick_best_candidate, get_account_value, _vp
from risk     import check_circuit_breakers, load_state, save_state, record_daily_return
from analyst  import weekly_review, flag_anomaly
from logger   import log_trade, log_regime, check_and_close_positions

import asyncio, httpx

# ── Telegram ──────────────────────────────────────────────────────────────────
async def _send(msg: str):
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        print(f"[Telegram] {msg}")
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json={
                "chat_id":    cfg.telegram_chat_id,
                "text":       msg,
                "parse_mode": "HTML",
            }, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")

def send(msg: str):
    asyncio.run(_send(msg))

# ── Alpaca position fetch ─────────────────────────────────────────────────────
def get_alpaca_positions() -> list:
    """Fetch open positions from Alpaca paper account."""
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(cfg.alpaca_key, cfg.alpaca_secret, cfg.alpaca_base_url)
        positions = api.list_positions()
        account   = api.get_account()
        return positions, account
    except Exception as e:
        print(f"Alpaca fetch error: {e}")
        return [], None

# ── Determine scan type ───────────────────────────────────────────────────────
import argparse
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--scan_type", default=None)
_parser.add_argument("--dry_run", action="store_true")
_args, _ = _parser.parse_known_args()

scan_type = _args.scan_type or os.environ.get("SCAN_TYPE", "full")
dry_run   = _args.dry_run
now_utc   = datetime.utcnow()
hour_utc  = now_utc.hour
min_utc   = now_utc.minute

# Auto-detect from schedule if not explicitly set
if scan_type == "full":
    if now_utc.weekday() == 6 and hour_utc >= 21:
        scan_type = "weekly_review"
    elif now_utc.weekday() == 0 and hour_utc == 13 and now_utc.day <= 7:
        scan_type = "dual_mom"
    elif hour_utc == 13 and min_utc == 0:
        scan_type = "morning_brief"

print(f"\n{'='*60}")
print(f"Run: {scan_type.upper()} | {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"Mode: {cfg.mode} | Strategy: {cfg.primary_strategy}")
print(f"Universe: {len(cfg.long_universe)} longs, {len(cfg.short_universe)} shorts")
print(f"{'='*60}\n")

# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY EXIT SCAN
# ─────────────────────────────────────────────────────────────────────────────
if scan_type == "weekly_exit":
    from exit_scan import run_exit_scan
    result = run_exit_scan(dry_run=dry_run)
    print(f"\nResult: {json.dumps(result, indent=2, default=str)}")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# MORNING BRIEF
# ─────────────────────────────────────────────────────────────────────────────
if scan_type == "morning_brief":
    positions, account = get_alpaca_positions()

    lines = ["☀️ *Morning Brief*\n"]

    # Account summary
    if account:
        equity     = float(account.equity)
        cash       = float(account.cash)
        day_pl     = float(account.equity) - float(account.last_equity)
        day_pl_pct = (day_pl / float(account.last_equity)) * 100 if float(account.last_equity) > 0 else 0
        pl_emoji   = "🟢" if day_pl >= 0 else "🔴"
        lines.append(
            f"*Account*\n"
            f"Equity: `${equity:,.2f}` | Cash: `${cash:,.2f}`\n"
            f"Day P&L: {pl_emoji} `${day_pl:+,.2f}` ({day_pl_pct:+.2f}%)\n"
        )

    # Open positions
    if positions:
        lines.append(f"*Open positions ({len(positions)})*")
        for p in positions:
            side_emoji = "🟢" if p.side == "long" else "🔴"
            pl         = float(p.unrealized_pl)
            pl_pct     = float(p.unrealized_plpc) * 100
            pl_emoji   = "📈" if pl >= 0 else "📉"
            lines.append(
                f"{side_emoji} `{p.symbol}` {p.qty} shares @ ${float(p.avg_entry_price):.2f}\n"
                f"  Now: `${float(p.current_price):.2f}` {pl_emoji} `${pl:+,.2f}` ({pl_pct:+.1f}%)"
            )
    else:
        lines.append("*Positions:* None open")

    # Quick regime check
    import yfinance as yf
    spy = yf.download("SPY", period="14mo", interval="1d", progress=False)
    vix = yf.download("^VIX", period="14mo", interval="1d", progress=False)
    spy_ret = float(spy["Close"].squeeze().iloc[-1] / spy["Close"].squeeze().iloc[-252] - 1) * 100
    vix_now = float(vix["Close"].squeeze().iloc[-1])
    vix_rank_val = float((vix["Close"].squeeze().iloc[-252:] <= vix_now).mean() * 100)
    market_emoji = "✅" if spy_ret > 0 else "⚠️"

    lines.append(
        f"\n*Regime*\n"
        f"SPY 12m: {market_emoji} `{spy_ret:+.1f}%` | "
        f"VIX: `{vix_now:.1f}` (rank {vix_rank_val:.0f}th pct)\n"
        f"Market state: {'✅ Positive — momentum favoured' if spy_ret > 0 else '⚠️ Negative — reduced exposure'}"
    )

    lines.append("\n_Next scan: 9:45 AM ET_")
    send("\n".join(lines))
    print("Morning brief sent.")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY REVIEW
# ─────────────────────────────────────────────────────────────────────────────
if scan_type == "weekly_review":
    state  = load_state()
    trades = state.get("trade_log", [])[-50:]
    review = weekly_review(trades, [])
    positions, account = get_alpaca_positions()
    pos_summary = ""
    if positions:
        pos_summary = "\n\n*Current positions:*\n" + "\n".join(
            f"• `{p.symbol}`: ${float(p.unrealized_pl):+.2f} ({float(p.unrealized_plpc)*100:+.1f}%)"
            for p in positions
        )
    send(f"📋 *Weekly strategy review*\n\n{review}{pos_summary}")
    print("Weekly review sent.")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY DUAL MOMENTUM
# ─────────────────────────────────────────────────────────────────────────────
if scan_type == "dual_mom":
    sig = dual_momentum_signal()
    send(
        f"📅 *Monthly dual momentum signal*\n"
        f"Hold: `{sig['hold']}`\n"
        f"Reason: {sig['reason']}\n"
        f"12m return: {sig.get('ret_12m', 'n/a')}% vs T-bills {sig.get('tbill', 'n/a')}%\n\n"
        f"_Execute manually in your ISA — not auto-traded._"
    )
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
# FULL SCAN
# ─────────────────────────────────────────────────────────────────────────────
if now_utc.weekday() >= 5:
    send("⏸ Weekend — no scan")
    sys.exit(0)

state  = load_state()
equity = get_account_value()

ok, reason = check_circuit_breakers(equity, state)
if not ok:
    send(f"⛔ Scan blocked: {reason}")
    sys.exit(0)

print("Running full market scan...")
scan = run_full_scan()

regime = scan["regime"]
print(f"Regime: market_state={'✓' if regime['market_state_ok'] else '✗'} | VIX rank={regime['vix_rank']:.0f}")
print(f"Candidates: {len(scan['long'])} long, {len(scan['short'])} short")

flag = flag_anomaly(scan)
if flag:
    send(flag)

if not regime["market_state_ok"]:
    send(f"⏸ Market state negative — no new positions today")
    sys.exit(0)

# Check if any bracket orders closed since last scan
check_and_close_positions()

# Deterministic order selection
best   = pick_best_candidate(scan)
result = execute(best, size_scalar=scan.get("size_scalar", 1.0))

# Log to dashboard
log_regime(scan, regime)
if result.get("executed") and best:
    log_trade(result, best)

# Fetch current positions to include in notification
positions, account = get_alpaca_positions()
pos_count = len(positions) if positions else 0

if result.get("executed"):
    side_emoji = "🟢" if result["side"] == "long" else "🔴"
    msg = (
        f"{side_emoji} *{result['side'].upper()} — {result['ticker']}*\n"
        f"Entry: `${result['entry']}`\n"
        f"Stop: `${result['stop']}` | TP: `${result['take_profit']}`\n"
        f"Shares: {result['shares']} | Port weight: {result.get('port_weight', 1.0):.0%}\n"
        f"Mode: `{result['mode']}` | Open positions: {pos_count + 1}"
    )
else:
    reason = result.get("reason", "No qualifying candidate")
    # Show top candidates even when no trade taken
    top = (scan["long"] + scan["short"])[:3]
    top_str = ""
    if top:
        top_str = "\n\n*Top signals (no trade taken):*\n" + "\n".join(
            f"• `{c['ticker']}` {c['side']} score={c['primary_score']:.3f} tt={c['trend_template']['conditions_met']}/9"
            for c in top
        )
    msg = f"🔍 No trade: _{reason}_{top_str}\n\nOpen positions: {pos_count}"

send(msg)
print(f"\nResult: {json.dumps(result, indent=2, default=str)}")

record_daily_return(state, state.get("daily_start_equity", equity), equity)
