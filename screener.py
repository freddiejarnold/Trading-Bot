"""
screener.py — active market scanner. Zero paid APIs.

Data sources used (all free):
  - yfinance: historical OHLCV, VIX, SPY (backtesting + signals)
  - Alpaca free tier: IEX real-time quotes (paper/live price updates)
  - Finnhub free tier: news sentiment catalyst filter, 60 req/min

§ supplement §4: Claude NOT called here. All decisions are deterministic.
§ supplement §3.4: Buy/hold spread enforced via position_state tracking.
§ supplement §5: PDT guard — no gap-and-go; min_holding_days = 1.
§ supplement §7: Parkview COI — universe is US-only, no overlap.
"""

import os, time, json
import numpy as np
import pandas as pd
import yfinance as yf
import ta

from config import CFG
from signals import (
    residual_momentum_score,
    vol_adjusted_momentum,
    raw_momentum_score,
    trend_template_score,
    information_discreteness,
    market_state_positive,
    vix_rank,
    volume_ratio,
)

# ── Fama-French factor proxy (free via yfinance ETFs) ────────────────────────
# For residual momentum we need MKT, SMB, HML.
# True FF data needs Ken French library or WRDS (free at Warwick — check).
# Proxy: MKT = SPY returns, SMB = IWM-SPY, HML = IVE-IVW
# Close enough for signal ranking; won't match the paper's exact numbers.

def _load_ff_proxies(period: str = "5y") -> tuple[pd.Series, pd.Series, pd.Series]:
    """Load free Fama-French factor proxies from yfinance."""
    tickers = ["SPY", "IWM", "IVE", "IVW"]
    raw = yf.download(tickers, period=period, interval="1d", progress=False)["Close"]
    if raw.empty:
        return None, None, None

    spy = raw["SPY"].pct_change()
    mkt = spy                                          # market factor
    smb = raw["IWM"].pct_change() - spy                # small minus big proxy
    hml = raw["IVE"].pct_change() - raw["IVW"].pct_change()  # value minus growth proxy

    return mkt.dropna(), smb.dropna(), hml.dropna()


# ── Position state: buy/hold spread (supplement §3.4) ────────────────────────
# We track which names are currently held so we can apply a looser exit threshold.

_POSITION_STATE_FILE = "position_state.json"

def load_position_state() -> dict:
    if os.path.exists(_POSITION_STATE_FILE):
        return json.load(open(_POSITION_STATE_FILE))
    return {}

def save_position_state(state: dict):
    json.dump(state, open(_POSITION_STATE_FILE, "w"), indent=2)


# ── Core screener ─────────────────────────────────────────────────────────────

def run_full_scan() -> dict:
    """
    Full scan of long and short universes.
    Returns {'long': [...], 'short': [...], 'regime': {...}, 'portfolio_weight': float}.

    All signals are deterministic — same data → same output.
    """

    print("Loading factor proxies...")
    mkt, smb, hml = _load_ff_proxies(period="5y")

    print("Loading market regime data...")
    spy_df  = yf.download("SPY",  period="14mo", interval="1d", progress=False)
    vix_df  = yf.download("^VIX", period="14mo", interval="1d", progress=False)

    # ── Regime checks ─────────────────────────────────────────────────────────
    market_ok  = market_state_positive(spy_df)           # Cooper et al gate
    vix_r      = vix_rank(vix_df)                        # secondary only
    regime = {
        "market_state_ok": market_ok,
        "vix_rank":        round(vix_r, 1),
        "vix_secondary_block": vix_r > CFG.vix_rank_secondary_threshold,
    }

    if not market_ok:
        print(f"Market state negative — momentum profits expected negative (Cooper et al). Skipping scan.")
        return {"long": [], "short": [], "regime": regime, "portfolio_weight": 0.0}

    # ── Score all universe members ─────────────────────────────────────────────
    position_state = load_position_state()

    long_scored  = _score_universe(CFG.long_universe,  "long",  mkt, smb, hml, spy_df, position_state)
    short_scored = _score_universe(CFG.short_universe, "short", mkt, smb, hml, spy_df, position_state)

    # ── Cross-sectional ranking (this is where CS momentum is implemented) ────
    # Sort by primary score descending (long) / ascending (short)
    long_scored.sort( key=lambda x: x["primary_score"], reverse=True)
    short_scored.sort(key=lambda x: x["primary_score"])

    n_long  = len(long_scored)
    n_short = len(short_scored)

    long_candidates  = []
    short_candidates = []

    for i, c in enumerate(long_scored):
        rank_pct = (i + 1) / max(n_long, 1)
        currently_held = position_state.get(c["ticker"], {}).get("held", False)

        # Buy/hold spread: enter top-decile only; hold until dropped from top-third
        if currently_held:
            qualifies = rank_pct <= CFG.exit_rank_pct    # looser exit threshold
        else:
            qualifies = rank_pct <= CFG.entry_rank_pct   # strict entry threshold

        c["rank_pct"]  = round(rank_pct, 3)
        c["qualifies"] = qualifies
        if qualifies:
            long_candidates.append(c)

    for i, c in enumerate(short_scored):
        rank_pct = (n_short - i) / max(n_short, 1)   # inverted: worst = top rank
        currently_held = position_state.get(c["ticker"], {}).get("held", False)

        if currently_held:
            qualifies = rank_pct <= CFG.exit_rank_pct
        else:
            qualifies = rank_pct <= CFG.entry_rank_pct

        c["rank_pct"]  = round(rank_pct, 3)
        c["qualifies"] = qualifies
        if qualifies:
            short_candidates.append(c)

    # ── VIX secondary: reduce size (not skip) ────────────────────────────────
    size_scalar = 0.5 if regime["vix_secondary_block"] else 1.0

    return {
        "long":             long_candidates[:5],
        "short":            short_candidates[:3],
        "regime":           regime,
        "size_scalar":      size_scalar,
        "scan_time":        pd.Timestamp.now().isoformat(),
    }


def _score_universe(
    tickers: list,
    side: str,
    mkt, smb, hml,
    spy_df,
    position_state: dict,
) -> list:
    """Score all tickers in a universe. Returns list of candidate dicts."""
    results = []

    for ticker in tickers:
        # COI guard: skip if in Parkview exclusion list (supplement §7)
        if ticker in CFG.parkview_exclusion_list:
            print(f"  Skipping {ticker} — Parkview COI exclusion")
            continue

        df = yf.download(ticker, period="5y", interval="1d", progress=False)
        if df.empty or len(df) < 252:
            continue

        # Compute all signals
        res_score = (
            residual_momentum_score(df, mkt, smb, hml)
            if mkt is not None else np.nan
        )
        vol_adj_mom  = vol_adjusted_momentum(df)
        raw_mom      = raw_momentum_score(df)
        id_score     = information_discreteness(df)
        vol_r        = volume_ratio(df)

        tt = trend_template_score(df, spy_df)

        # ATR for position sizing
        if len(df) >= 22:
            atr_val = float(
                ta.volatility.AverageTrueRange(
                    df["High"].squeeze(), df["Low"].squeeze(), df["Close"].squeeze(), 20
                ).average_true_range().iloc[-1]
            )
        else:
            atr_val = np.nan

        price = float(df["Close"].squeeze().iloc[-1])

        # Primary score: residual mom if available, else vol-adjusted
        primary = res_score if not np.isnan(res_score) else vol_adj_mom

        # ID filter: keep only continuous-momentum candidates (Da et al)
        # Low ID = gradual info = stronger signal. Exclude top-third of ID for longs.
        id_ok = (np.isnan(id_score) or id_score < 0.2) if side == "long" else True

        # Long-specific quality gates
        # Trend template relaxed to 6/9 conditions (not all 9) — avoids
        # filtering out the entire universe on any given day
        if side == "long":
            passes_quality = (
                tt["conditions_met"] >= 6 and
                id_ok and
                not np.isnan(primary)
            )
        else:  # short
            passes_quality = (
                tt["conditions_met"] <= 3 and   # clearly failing trend template
                (np.isnan(raw_mom) or raw_mom < -0.05) and
                not np.isnan(primary)
            )

        if not passes_quality:
            continue

        stop_long  = round(price - CFG.atr_stop_multiplier * atr_val, 2) if not np.isnan(atr_val) else None
        stop_short = round(price + CFG.atr_stop_multiplier * atr_val, 2) if not np.isnan(atr_val) else None
        tp_long    = round(price + CFG.atr_stop_multiplier * CFG.rr_target * atr_val, 2) if not np.isnan(atr_val) else None
        tp_short   = round(price - CFG.atr_stop_multiplier * CFG.rr_target * atr_val, 2) if not np.isnan(atr_val) else None

        results.append({
            "ticker":          ticker,
            "side":            side,
            "price":           round(price, 2),
            "primary_score":   round(float(primary), 4) if not np.isnan(primary) else 0.0,
            "residual_mom":    round(float(res_score), 4)  if not np.isnan(res_score) else None,
            "vol_adj_mom":     round(float(vol_adj_mom), 4) if not np.isnan(vol_adj_mom) else None,
            "raw_mom_12_1":    round(float(raw_mom) * 100, 1) if not np.isnan(raw_mom) else None,
            "id_score":        round(float(id_score), 3) if not np.isnan(id_score) else None,
            "trend_template":  tt,
            "volume_ratio":    round(float(vol_r), 2) if not np.isnan(vol_r) else None,
            "atr":             round(atr_val, 2) if not np.isnan(atr_val) else None,
            "stop_long":       stop_long,
            "stop_short":      stop_short,
            "tp_long":         tp_long,
            "tp_short":        tp_short,
        })

    return results


# ── Dual momentum ETF sleeve (supplement §5 ISA note) ────────────────────────
# Run separately, manually, monthly. Not in the automated loop.
# This is the tax-efficient core — run in an ISA, 2-4 trades/year.

def dual_momentum_signal(period: str = "1y") -> dict:
    """
    Antonacci dual momentum. Returns the ETF to hold this month.
    Absolute + relative: only hold equity if it beats T-bills on absolute basis.
    """
    tickers = CFG.dual_mom_universe   # ["SPY", "EFA", "AGG"]
    df = yf.download(tickers + ["^IRX"], period=period, interval="1mo", progress=False)["Close"]
    if df.empty or len(df) < 12:
        return {"hold": "AGG", "reason": "Insufficient data"}

    rets = {t: float(df[t].iloc[-1] / df[t].iloc[-12]) - 1.0
            for t in tickers if t in df.columns}

    tbill_annual = float(df["^IRX"].iloc[-1]) / 100.0 if "^IRX" in df.columns else 0.04
    tbill_12m    = tbill_annual  # approximate

    spy_ret = rets.get("SPY", -1)
    efa_ret = rets.get("EFA", -1)

    # Relative: which equity is best?
    best_equity = "SPY" if spy_ret >= efa_ret else "EFA"
    best_equity_ret = max(spy_ret, efa_ret)

    # Absolute: does it beat T-bills?
    if best_equity_ret > tbill_12m:
        return {"hold": best_equity, "ret_12m": round(best_equity_ret * 100, 1),
                "tbill": round(tbill_12m * 100, 1), "reason": "Positive absolute momentum"}
    else:
        return {"hold": "AGG", "ret_12m": round(best_equity_ret * 100, 1),
                "tbill": round(tbill_12m * 100, 1), "reason": "Negative absolute momentum → bonds"}
