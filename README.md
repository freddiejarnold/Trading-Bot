# Trading Bot

An automated momentum trading system built on peer-reviewed academic research. Runs entirely on free infrastructure, requires no manual intervention, and sends trade notifications to Telegram.

## What it does

The bot scans 90 US stocks eight times per day during market hours, scores each one using institutional momentum signals, and places bracket orders on Alpaca when a qualifying candidate is found. Every decision is deterministic — no AI in the order path.

**Strategies implemented:**
- Residual momentum (Blitz, Huij & Martens 2011) — ranks on Fama-French 3-factor adjusted returns
- Volatility-scaled cross-sectional momentum — risk-adjusted 12-1 month return
- Minervini trend template — 9-condition uptrend checklist
- Information discreteness filter (Da, Gurun & Warachka 2014) — prefers gradual over jump momentum
- Strategy-level volatility targeting (Barroso & Santa-Clara 2015) — scales portfolio weight by realised vol
- Market state filter (Cooper, Gutierrez & Hameed 2004) — only trades after positive 12m market returns
- Buy/hold spread (Novy-Marx & Velikov 2016) — top-decile entry, top-third exit to reduce turnover

## Architecture

```
GitHub Actions (scheduler)
    └── run_scan.py (entry point)
         ├── screener.py  — scores all 90 stocks, applies regime filters
         ├── executor.py  — deterministic order selection, Alpaca execution
         ├── risk.py      — position sizing, circuit breakers, PDT guard
         ├── logger.py    — pushes trade + regime data to dashboard
         └── analyst.py   — Claude Haiku weekly review (research only)
```

Claude is used **only** in `analyst.py` for weekly performance notes. It never touches the order path.

## File structure

```
├── analyst.py                        # Claude Haiku research notes (not in order path)
├── backtester.py                     # Walk-forward backtest with deflated Sharpe
├── bot.py                            # Long-running scheduler (not used in GitHub Actions)
├── config.py                         # All settings — universe, risk params, API config
├── executor.py                       # Deterministic order selection + Alpaca execution
├── logger.py                         # Dashboard data push after every scan
├── requirements.txt                  # All dependencies (zero paid APIs)
├── risk.py                           # Position sizing, circuit breakers, PDT guard
├── run_scan.py                       # GitHub Actions entry point — runs once and exits
├── screener.py                       # Market scanner, signal computation, regime checks
├── signals.py                        # Pure signal functions (residual mom, trend template etc.)
├── F-F_Research_Data_Factors_daily.txt  # Ken French daily factors (update quarterly)
└── .github/workflows/trading_scan.yml  # Cron schedule — 8 scans/day + morning brief
```

## Schedule

All times London (BST/GMT). Triggered externally via cron-job.org due to GitHub Actions scheduler issues.

| Time (BST) | What runs |
|---|---|
| 9:00 AM | Morning brief — positions, P&L, regime |
| 2:45 PM | Scan 1 — market open |
| 3:30 PM | Scan 2 |
| 4:15 PM | Scan 3 |
| 5:00 PM | Scan 4 |
| 5:45 PM | Scan 5 |
| 6:30 PM | Scan 6 |
| 7:15 PM | Scan 7 |
| 7:45 PM | Scan 8 — pre-close |
| Sunday 11 PM | Weekly review (Claude Haiku) |
| First Monday 9 AM | Monthly dual-momentum ISA signal |

## Setup

### 1. Create free accounts

| Service | Purpose | URL |
|---|---|---|
| Alpaca | Paper/live trading + data | alpaca.markets |
| Finnhub | Real-time price feed | finnhub.io |
| Anthropic | Weekly review (Haiku) | console.anthropic.com |
| Telegram | Trade notifications | t.me/BotFather |
| cron-job.org | External scheduler | cron-job.org |

### 2. GitHub Secrets

Add these in repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `ALPACA_API_KEY` | Alpaca paper key ID |
| `ALPACA_SECRET_KEY` | Alpaca paper secret |
| `FINNHUB_KEY` | Finnhub API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `DASHBOARD_ARTIFACT_ID` | Claude dashboard artifact ID |

### 3. cron-job.org

Create two cronjobs pointing at:
```
https://api.github.com/repos/USERNAME/Trading-Bot/actions/workflows/trading_scan.yml/dispatches
```

Headers:
```
Authorization: Bearer YOUR_GITHUB_TOKEN
Accept: application/vnd.github+json
Content-Type: application/json
```

Scan body: `{"ref":"main"}`
Schedule: `*/45 14-20 * * 1-5`

Morning brief body: `{"ref":"main","inputs":{"scan_type":"morning_brief"}}`
Schedule: `0 9 * * 1-5`

### 4. Fama-French data

Download the daily factors file quarterly from:
`mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html`

Fama/French 3 Factors → Daily → unzip → commit `F-F_Research_Data_Factors_daily.txt` to repo root.

Next update due: **January 2027**

## Universe

**66 longs** across mega-cap tech, growth, financials, industrials, and healthcare. All US-listed, >$20bn market cap, >$200m daily volume. No UK-listed names (Parkview COI compliance).

**24 shorts** — structural downtrends: legacy tech, distressed airlines, weak pharma, EV washouts.

## Risk controls

- 1% portfolio risk per trade (ATR-based stop sizing)
- Max 8 open positions
- Strategy-level volatility targeting (12% annualised target, Barroso & Santa-Clara)
- Daily loss limit: -2% → pause
- Drawdown limit: -10% from peak → pause
- PDT guard: min 1 day holding period, tracks 3-trade-per-5-day limit
- Market state gate: no new longs when SPY 12m return < 0
- VIX secondary overlay: reduces size 50% when VIX rank > 80

## Cost

| Item | Monthly cost |
|---|---|
| GitHub Actions | Free |
| Alpaca paper trading | Free |
| Finnhub | Free |
| cron-job.org | Free |
| Anthropic Haiku | ~£0.50 |
| **Total** | **~£0.50** |

## Going live

When paper trading results are satisfactory:

1. Change `ALPACA_BASE_URL` secret to `https://api.alpaca.markets`
2. Fund Alpaca account
3. Change `mode = "paper"` to `mode = "live"` in `config.py`
4. Notify Parkview investment committee before going live (COI compliance)

## Academic references

- Jegadeesh & Titman (1993) — original 12-1 cross-sectional momentum
- Blitz, Huij & Martens (2011) — residual momentum
- Barroso & Santa-Clara (2015) — strategy-level volatility targeting
- Daniel & Moskowitz (2016) — momentum crashes
- Cooper, Gutierrez & Hameed (2004) — market state filter
- Novy-Marx (2012) — intermediate horizon momentum
- Da, Gurun & Warachka (2014) — information discreteness
- Novy-Marx & Velikov (2016) — trading costs and buy/hold spread
- George & Hwang (2004) — 52-week high
- Bailey & López de Prado — deflated Sharpe ratio
