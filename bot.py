"""
bot.py — main orchestrator.

§ supplement §4: order path is fully deterministic.
  screener → pick_best_candidate → execute
  Claude is invoked only in analyst.py (weekly review, /explain, anomaly flag).

Zero paid APIs. Only cost: Anthropic Haiku (~£0.50/mo).
"""

import os, time
from datetime import datetime, date
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

from config   import CFG
from screener import run_full_scan, dual_momentum_signal
from executor import execute, pick_best_candidate, get_account_value, _vp
from risk     import check_circuit_breakers, load_state, save_state, record_daily_return
from analyst  import weekly_review, flag_anomaly
from notifier import send_sync, notify_trade, notify_regime

# ── Regime log (for weekly review) ───────────────────────────────────────────
_regime_log: list = []


def _run_scan_cycle(label: str = ""):
    state  = load_state()
    equity = get_account_value()

    if state.get("paused"):
        print(f"[{label}] Paused — skipping")
        return

    ok, reason = check_circuit_breakers(equity, state)
    if not ok:
        notify_regime(f"⛔ {label}: {reason}")
        return

    print(f"[{label}] Scanning at {datetime.now().strftime('%H:%M')}")
    scan = run_full_scan()

    _regime_log.append({
        "time":   datetime.now().isoformat(),
        "label":  label,
        "regime": scan["regime"],
        "n_long": len(scan["long"]),
        "n_short": len(scan["short"]),
    })

    # Anomaly check (Claude — research only, not order-path)
    flag = flag_anomaly(scan)
    if flag:
        send_sync(flag)

    if not scan["regime"]["market_state_ok"]:
        notify_regime(f"⏸ {label}: Market state negative — no new positions (Cooper et al)")
        return

    # ── DETERMINISTIC order selection — no LLM ───────────────────────────────
    best = pick_best_candidate(scan)
    result = execute(best, size_scalar=scan.get("size_scalar", 1.0))

    notify_trade(result, best, scan["regime"], label=label)


def _morning_scan():
    _run_scan_cycle("Morning 9:45")

def _midday_scan():
    _run_scan_cycle("Midday 12:30")

def _afternoon_scan():
    _run_scan_cycle("Afternoon 2:45")


def _weekly_review_job():
    """Sunday: Claude Haiku reviews the week's trades. Research only."""
    trades = _vp.get("trades", [])[-50:]
    review = weekly_review(trades, _regime_log[-20:])
    send_sync(f"📋 *Weekly review*\n\n{review}")


def _record_eod():
    """Record end-of-day equity for Barroso vol targeting."""
    state  = load_state()
    equity = get_account_value()
    start  = state.get("daily_start_equity", equity)
    record_daily_return(state, start, equity)


# ── Dual momentum monthly signal (ISA sleeve — text-only, no auto-trade) ─────
def _monthly_dual_mom():
    sig = dual_momentum_signal()
    send_sync(
        f"📅 *Monthly dual momentum signal*\n"
        f"Hold: `{sig['hold']}`\n"
        f"Reason: {sig['reason']}\n"
        f"12m return: {sig.get('ret_12m', 'n/a')}% vs T-bills {sig.get('tbill', 'n/a')}%\n\n"
        f"_Run this in your ISA manually — not auto-executed._"
    )


def main():
    print(f"Bot starting in {CFG.mode.upper()} mode, direction={CFG.direction}")
    print(f"Primary strategy: {CFG.primary_strategy}")
    print(f"Zero paid APIs. Claude: research only. Orders: deterministic.")

    scheduler = BackgroundScheduler(timezone="America/New_York")
    scheduler.add_job(_morning_scan,    "cron", day_of_week="mon-fri", hour=9,  minute=45)
    scheduler.add_job(_midday_scan,     "cron", day_of_week="mon-fri", hour=12, minute=30)
    scheduler.add_job(_afternoon_scan,  "cron", day_of_week="mon-fri", hour=14, minute=45)
    scheduler.add_job(_record_eod,      "cron", day_of_week="mon-fri", hour=16, minute=5)
    scheduler.add_job(_weekly_review_job, "cron", day_of_week="sun",   hour=18, minute=0)
    scheduler.add_job(_monthly_dual_mom,  "cron", day_of_week="mon",   hour=9,  minute=0,
                      month="*", day="1-7")   # first Monday of the month
    scheduler.start()

    from telegram_runner import start_telegram_bot
    start_telegram_bot()   # blocks


if __name__ == "__main__":
    main()
