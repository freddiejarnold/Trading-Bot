"""
run_scan.py — single-execution entry point for GitHub Actions.

Unlike bot.py (which runs forever with a scheduler), this runs once and exits.
GitHub Actions handles the timing via cron in trading_scan.yml.

State (positions, equity history, PDT counter) is persisted between runs
via GitHub Actions cache (state.json + position_state.json).
"""

import os, json, sys
from datetime import datetime, date

# Load env vars (GitHub Actions injects these; locally use .env)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Inject API keys from environment into config before importing anything else
import config as _cfg_module
cfg = _cfg_module.CFG
cfg.alpaca_key       = os.environ.get("ALPACA_API_KEY", "")
cfg.alpaca_secret    = os.environ.get("ALPACA_SECRET_KEY", "")
cfg.alpaca_base_url  = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
cfg.finnhub_key      = os.environ.get("FINNHUB_KEY", "")
cfg.anthropic_key    = os.environ.get("ANTHROPIC_API_KEY", "")
cfg.telegram_token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
cfg.mode             = "paper"   # always paper until you manually change this

from screener import run_full_scan, dual_momentum_signal
from executor import execute, pick_best_candidate, get_account_value, _vp
from risk     import check_circuit_breakers, load_state, save_state, record_daily_return
from analyst  import weekly_review, flag_anomaly

import asyncio
import httpx

# ── Telegram helper (no bot framework needed for one-way notifications) ───────
async def _send(msg: str):
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        print(f"[Telegram disabled] {msg}")
        return
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id":    cfg.telegram_chat_id,
            "text":       msg,
            "parse_mode": "Markdown",
        })

def send(msg: str):
    asyncio.run(_send(msg))

# ── Determine what type of run this is ───────────────────────────────────────
scan_type = os.environ.get("SCAN_TYPE", "full")
now_utc   = datetime.utcnow()
hour_utc  = now_utc.hour

# Auto-detect from UTC hour if not set
if scan_type == "full":
    if now_utc.weekday() == 6 and hour_utc >= 21:  # Sunday evening
        scan_type = "weekly_review"
    elif now_utc.weekday() == 0 and hour_utc == 13 and now_utc.day <= 7:
        scan_type = "dual_mom"

print(f"\n{'='*60}")
print(f"Run type: {scan_type.upper()} | {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"Mode: {cfg.mode} | Strategy: {cfg.primary_strategy}")
print(f"Universe: {len(cfg.long_universe)} longs, {len(cfg.short_universe)} shorts")
print(f"{'='*60}\n")

# ── Weekly review (Claude Haiku — research only) ──────────────────────────────
if scan_type == "weekly_review":
    state  = load_state()
    trades = state.get("trade_log", [])[-50:]
    review = weekly_review(trades, [])
    send(f"📋 *Weekly strategy review*\n\n{review}")
    print("Weekly review sent.")
    sys.exit(0)

# ── Monthly dual-momentum signal (ISA sleeve, text-only) ─────────────────────
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

# ── Full scan ─────────────────────────────────────────────────────────────────
state  = load_state()
equity = get_account_value()

# Check it's actually a trading day/hour before doing anything expensive
if now_utc.weekday() >= 5:
    send("⏸ Weekend — no scan")
    sys.exit(0)

# Circuit breakers
ok, reason = check_circuit_breakers(equity, state)
if not ok:
    send(f"⛔ Scan blocked: {reason}")
    sys.exit(0)

# Run scan
print("Running full market scan...")
scan = run_full_scan()

regime = scan["regime"]
print(f"Regime: market_state={'✓' if regime['market_state_ok'] else '✗'} | VIX rank={regime['vix_rank']:.0f}")
print(f"Candidates: {len(scan['long'])} long, {len(scan['short'])} short")

# Anomaly flag (Claude — research only, not order path)
flag = flag_anomaly(scan)
if flag:
    send(flag)

if not regime["market_state_ok"]:
    send(f"⏸ Market state negative (12m SPY return < 0) — no new positions today")
    sys.exit(0)

# Deterministic order selection — zero LLM involvement
best   = pick_best_candidate(scan)
result = execute(best, size_scalar=scan.get("size_scalar", 1.0))

# Build notification
if result.get("executed"):
    side_emoji = "🟢" if result["side"] == "long" else "🔴"
    msg = (
        f"{side_emoji} *{result['side'].upper()} — {result['ticker']}*\n"
        f"Entry: `${result['entry']}`\n"
        f"Stop: `${result['stop']}` | TP: `${result['take_profit']}`\n"
        f"Shares: {result['shares']} | Risk: ${result.get('risk_amount', '?')}\n"
        f"Port weight: {result.get('port_weight', 1.0):.0%} (Barroso vol target)\n"
        f"Mode: `{result['mode']}`"
    )
else:
    reason = result.get("reason", "No qualifying candidate")
    msg = f"🔍 Scan complete — no trade: _{reason}_"

send(msg)
print(f"\nResult: {json.dumps(result, indent=2, default=str)}")

# Record daily return for vol targeting
record_daily_return(state, state.get("daily_start_equity", equity), equity)
