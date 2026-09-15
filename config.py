"""
config.py — all settings in one place.
v2.1: expanded universe (46 long, 20 short) for meaningful cross-sectional ranking.
Mode switched to paper for Alpaca paper trading.
"""
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class BotConfig:
    # ── Mode ─────────────────────────────────────────────────────────────────
    mode: Literal["virtual", "paper", "live"] = "paper"   # ← paper trading
    direction: Literal["long", "both", "short"] = "both"

    # ── Strategy ──────────────────────────────────────────────────────────────
    primary_strategy: str = "residual_mom"   # Blitz et al 2011
    lookback_days: int = 252
    skip_days:     int = 21
    ff_window:     int = 756                 # 36 months for FF3 regression

    # § supplement 3.4 — buy/hold spread (Novy-Marx & Velikov 2016)
    entry_rank_pct: float = 0.10   # top 10% to enter
    exit_rank_pct:  float = 0.33   # hold until dropped from top third

    # § supplement 3.2 — strategy-level vol targeting (Barroso & Santa-Clara 2015)
    strategy_vol_target:    float = 0.12
    strategy_vol_window:    int   = 126
    max_strategy_leverage:  float = 1.0

    # ── Asset universe ────────────────────────────────────────────────────────
    # 46 longs across 5 sectors: enough for top-decile ranking (4-5 names)
    # and top-third exit threshold (15 names). All US large-cap, >$50bn market
    # cap, daily volume >$500m. Zero overlap with Parkview (UK sub-£2bn).
    long_universe: list = field(default_factory=lambda: [
        # Mega-cap tech — strongest historical momentum persistence
        "NVDA", "MSFT", "AAPL", "META", "GOOGL", "AMZN",
        "AMD",  "AVGO", "CRM",  "NOW",  "ORCL",  "ADBE",
        "SNPS", "CDNS", "INTU",
        # High-momentum growth
        "TSLA", "PLTR", "ARM",  "UBER", "NFLX",
        "SPOT", "APP",  "AXON", "DECK", "FICO",
        # Financials — sector rotation momentum
        "GS",   "JPM",  "V",    "MA",   "COF",
        "AXP",  "BLK",  "SCHW",
        # Industrials / defence — strong residual momentum historically
        "LMT",  "RTX",  "NOC",  "GE",   "CAT",
        "DE",   "ETN",  "PWR",
        # Healthcare momentum
        "LLY",  "UNH",  "ISRG", "DXCM", "VEEV",
    ])

    # 20 shorts — structural downtrends, negative 12m momentum, liquid enough
    # to borrow without hard-to-borrow fees on Alpaca
    short_universe: list = field(default_factory=lambda: [
        "INTC", "BA",   "WBA",  "PFE",  "T",
        "VZ",   "MO",   "CVS",  "KHC",  "PARA",
        "DIS",  "NKE",  "MPW",  "CELH", "PLUG",
        "BYND", "RIVN", "LCID", "XPEV", "SMCI",
    ])

    sector_etfs: list = field(default_factory=lambda: [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "SPY",
    ])
    # Dual-momentum ETF sleeve — manual, monthly, ISA only (supplement §5)
    dual_mom_universe: list = field(default_factory=lambda: ["SPY", "EFA", "AGG"])

    # ── Risk controls ─────────────────────────────────────────────────────────
    risk_per_trade_pct:     float = 0.01   # 1% portfolio risk per trade
    atr_stop_multiplier:    float = 2.0
    rr_target:              float = 2.0
    max_positions:          int   = 6
    max_portfolio_risk_pct: float = 0.20

    # ── Circuit breakers ──────────────────────────────────────────────────────
    daily_loss_limit_pct:          float = 0.02
    monthly_dd_limit_pct:          float = 0.10
    vix_rank_secondary_threshold:  float = 80.0   # reduces size 50%, doesn't block
    require_positive_market_state: bool  = True    # Cooper et al primary gate

    # ── PDT rule guard ────────────────────────────────────────────────────────
    min_holding_days: int = 1   # overnight minimum; no gap-and-go

    # ── Live allocation cap ───────────────────────────────────────────────────
    live_allocation_pct: float = 0.05

    # ── API config — loaded from environment variables at runtime ─────────────
    # Never hardcode keys here. Set via GitHub Secrets or .env file.
    alpaca_key:      str = ""
    alpaca_secret:   str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    finnhub_key:     str = ""
    anthropic_key:   str = ""
    claude_model:    str = "claude-haiku-4-5"

    telegram_token:   str = ""
    telegram_chat_id: str = ""

    # ── COI guard (supplement §7) ─────────────────────────────────────────────
    # Add any UK-listed names here if you ever expand the universe
    parkview_exclusion_list: list = field(default_factory=list)


CFG = BotConfig()
