"""
config.py — all settings in one place.
v2.2: expanded universe (60 long, 25 short), more scan slots, morning brief added.
"""
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class BotConfig:
    # ── Mode ─────────────────────────────────────────────────────────────────
    mode: Literal["virtual", "paper", "live"] = "paper"
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
    strategy_vol_target:   float = 0.12
    strategy_vol_window:   int   = 126
    max_strategy_leverage: float = 1.0

    # ── Asset universe ─────────────────────────────────────────────────────────
    # 60 longs: top-decile = 6 names, top-third = 20 names. Better ranking depth.
    # All US-listed, >$20bn market cap, daily volume >$200m.
    # No overlap with Parkview (UK sub-£2bn universe).
    long_universe: list = field(default_factory=lambda: [
        # Mega-cap tech
        "NVDA", "MSFT", "AAPL", "META", "GOOGL", "AMZN",
        "AMD",  "AVGO", "CRM",  "NOW",  "ORCL",  "ADBE",
        "SNPS", "CDNS", "INTU", "PANW", "CRWD",  "FTNT",
        # High-momentum growth
        "TSLA", "PLTR", "ARM",  "UBER", "NFLX",  "SPOT",
        "APP",  "AXON", "DECK", "FICO", "DUOL",  "CELH",
        "TTD",  "DDOG", "MDB",  "SNOW",
        # Financials
        "GS",   "JPM",  "V",    "MA",   "COF",
        "AXP",  "BLK",  "SCHW", "HOOD", "IBKR",
        # Industrials / defence
        "LMT",  "RTX",  "NOC",  "GE",   "CAT",
        "DE",   "ETN",  "PWR",  "CACI", "LDOS",
        # Healthcare
        "LLY",  "UNH",  "ISRG", "DXCM", "VEEV",
        "PODD", "TMDX", "RXRX",
        # Energy / commodities momentum
        "XOM",  "CVX",  "OXY",  "FANG",
    ])

    # 25 shorts — verified downtrends, negative 12m momentum.
    # Removed: WBA (delisted), MPW (restructured), RIDE (delisted).
    short_universe: list = field(default_factory=lambda: [
        # Legacy tech in secular decline
        "INTC", "IBM",  "HPE",  "DELL", "STX",
        # Aerospace / industrials distress
        "BA",   "AAL",  "UAL",  "DAL",
        # Pharma / healthcare weakness
        "PFE",  "BMY",  "VRTX", "CVS",
        # Telecoms (low growth, high debt)
        "T",    "VZ",   "LUMN",
        # Consumer staples decay
        "MO",   "KHC",  "CPB",
        # EV / speculative washouts
        "RIVN", "LCID", "XPEV", "BYND", "PLUG",
    ])

    sector_etfs: list = field(default_factory=lambda: [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "SPY",
    ])
    dual_mom_universe: list = field(default_factory=lambda: ["SPY", "EFA", "AGG"])

    # ── Risk controls ──────────────────────────────────────────────────────────
    risk_per_trade_pct:     float = 0.01   # 1% portfolio risk per trade
    atr_stop_multiplier:    float = 2.0
    rr_target:              float = 2.0
    max_positions:          int   = 8      # slightly higher with bigger universe
    max_portfolio_risk_pct: float = 0.20

    # ── Circuit breakers ───────────────────────────────────────────────────────
    daily_loss_limit_pct:          float = 0.02
    monthly_dd_limit_pct:          float = 0.10
    vix_rank_secondary_threshold:  float = 80.0
    require_positive_market_state: bool  = True

    # ── PDT rule guard ─────────────────────────────────────────────────────────
    min_holding_days: int = 1

    # ── Live allocation cap ────────────────────────────────────────────────────
    live_allocation_pct: float = 0.05

    # ── API config ─────────────────────────────────────────────────────────────
    alpaca_key:      str = ""
    alpaca_secret:   str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    finnhub_key:     str = ""
    anthropic_key:   str = ""
    claude_model:    str = "claude-haiku-4-5"

    telegram_token:   str = ""
    telegram_chat_id: str = ""

    # ── COI guard (supplement §7) ──────────────────────────────────────────────
    parkview_exclusion_list: list = field(default_factory=list)


CFG = BotConfig()
