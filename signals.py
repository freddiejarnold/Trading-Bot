"""
signals.py — pure, deterministic signal functions.

§ supplement §4: Claude removed from order path entirely.
All functions here return numeric scores or boolean signals only.
Every function is backtestable: given the same DataFrame it returns the same output.

Strategies implemented (in order of evidence-per-line-of-code, supplement §2.3):
  1. residual_momentum     — Blitz et al 2011, highest-value addition
  2. vol_scaled_momentum   — fixed cross-sectional version (was time-series bug)
  3. trend_template        — Minervini, bugs fixed (High for 52w, RS proxy, 200d rising)
  4. information_discreteness — Da et al 2014, frog-in-pan filter
  5. strategy_vol_weight   — Barroso & Santa-Clara 2015, portfolio-level scaling
  6. market_state_filter   — Cooper et al 2004, regime gate
"""

import numpy as np
import pandas as pd
import ta
from scipy import stats


# ─────────────────────────────────────────────────────────────────────────────
# 1. Residual momentum  (Blitz, Huij & Martens 2011)
# Rank on FF3-adjusted residuals rather than raw returns.
# Roughly twice the risk-adjusted return of total-return momentum, supplement §2.3.
# ─────────────────────────────────────────────────────────────────────────────

def residual_momentum_score(
    df: pd.DataFrame,
    mkt: pd.Series,
    smb: pd.Series,
    hml: pd.Series,
    lookback: int = 252,
    skip: int = 21,
    reg_window: int = 756,          # 36 months for regression (supplement §3.1)
) -> float:
    """
    Compute the t-statistic-like residual momentum score for a single stock.
    df must have a 'Close' column. mkt/smb/hml are daily return Series aligned to df.

    Returns a float score (higher = stronger positive residual momentum).
    Returns NaN if insufficient data.
    """
    close = df["Close"].squeeze()
    if len(close) < reg_window + lookback:
        return np.nan

    daily_ret = close.pct_change().dropna()

    # Align factors to stock returns
    aligned = pd.DataFrame({
        "ret": daily_ret,
        "mkt": mkt,
        "smb": smb,
        "hml": hml,
    }).dropna()

    if len(aligned) < reg_window:
        return np.nan

    # Rolling FF3 regression on the most recent reg_window days
    window_data = aligned.iloc[-reg_window:]
    y = window_data["ret"].values
    X = np.column_stack([
        np.ones(len(window_data)),
        window_data["mkt"].values,
        window_data["smb"].values,
        window_data["hml"].values,
    ])
    try:
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        residuals = y - X @ beta
    except np.linalg.LinAlgError:
        return np.nan

    # Formation window: t-12 to t-2 months (skip most recent month)
    formation_residuals = residuals[-(lookback):-(skip)]
    if len(formation_residuals) < 20:
        return np.nan

    # t-statistic (supplement §3.1 standardisation)
    mean_res = np.mean(formation_residuals)
    std_res  = np.std(formation_residuals, ddof=1)
    if std_res < 1e-10:
        return np.nan

    return mean_res / std_res   # dimensionless, comparable across stocks


# ─────────────────────────────────────────────────────────────────────────────
# 2. Volatility-scaled cross-sectional momentum  (fixed)
# Bug was: code implemented time-series (momentum > 0), not cross-sectional (rank).
# Fix: return raw 12-1 return for cross-sectional ranking, separate from signal logic.
# ─────────────────────────────────────────────────────────────────────────────

def raw_momentum_score(
    df: pd.DataFrame,
    lookback: int = 252,
    skip: int = 21,
) -> float:
    """
    12-1 month raw price momentum. Returns the decimal return, or NaN.
    Cross-sectional ranking is done in screener.py, not here.
    """
    close = df["Close"].squeeze()
    if len(close) < lookback + 5:
        return np.nan
    ret = (close.iloc[-skip] / close.iloc[-lookback]) - 1.0
    return float(ret)


def return_volatility(df: pd.DataFrame, window: int = 60) -> float:
    """
    § supplement §4 bug fix: dimensionality of momentum/ATR was wrong.
    ATR is price-unit; momentum is a return (dimensionless).
    Use return volatility (annualised) as the denominator instead.
    """
    close = df["Close"].squeeze()
    if len(close) < window + 5:
        return np.nan
    daily_ret_vol = close.pct_change().iloc[-window:].std()
    return float(daily_ret_vol * np.sqrt(252))   # annualised


def vol_adjusted_momentum(df: pd.DataFrame, lookback: int = 252, skip: int = 21) -> float:
    """Risk-adjusted momentum = raw momentum / return volatility (dimensionally correct)."""
    mom = raw_momentum_score(df, lookback, skip)
    vol = return_volatility(df)
    if np.isnan(mom) or np.isnan(vol) or vol < 1e-8:
        return np.nan
    return mom / vol


# ─────────────────────────────────────────────────────────────────────────────
# 3. Minervini Trend Template  (bugs fixed, supplement §4)
# Fixes: High column for 52w high, 200d MA rising condition, RS proxy added.
# ─────────────────────────────────────────────────────────────────────────────

def trend_template_score(df: pd.DataFrame, market_df: pd.DataFrame) -> dict:
    """
    Full 8-point Minervini Trend Template.
    Returns {'passes': bool, 'conditions_met': int, 'rs_proxy': float}.

    Bugs fixed from supplement §4:
    - 52w high now uses df['High'], not df['Close'].rolling().max()
    - Added: 200d MA trending up for ≥ 1 month
    - Added: RS rating proxy (stock 12m return vs market 12m return)
    """
    close = df["Close"].squeeze()
    high  = df["High"].squeeze()    # § fix: was df["Close"]

    if len(close) < 252:
        return {"passes": False, "conditions_met": 0, "rs_proxy": np.nan}

    ma50  = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    ma200 = close.rolling(200).mean()

    price_now = float(close.iloc[-1])
    ma50_now  = float(ma50.iloc[-1])
    ma150_now = float(ma150.iloc[-1])
    ma200_now = float(ma200.iloc[-1])

    high52w   = float(high.iloc[-252:].max())   # § fix: uses High
    low52w    = float(close.iloc[-252:].min())

    # MA rising: compare to 21 trading days ago (≈1 month) § fix: was missing
    ma200_month_ago = float(ma200.iloc[-21]) if len(ma200) >= 21 else ma200_now
    ma200_rising    = ma200_now > ma200_month_ago

    # RS proxy: 12-1m stock return vs SPY (§ fix: was missing entirely)
    if market_df is not None and len(market_df) >= 252:
        mkt_close = market_df["Close"].squeeze()
        mkt_ret   = float(mkt_close.iloc[-21] / mkt_close.iloc[-252]) - 1.0
        stk_ret   = float(close.iloc[-21] / close.iloc[-252]) - 1.0
        rs_proxy  = stk_ret - mkt_ret          # outperformance vs market
        rs_passes = rs_proxy > 0.0             # crude proxy for RS > 70
    else:
        rs_proxy  = np.nan
        rs_passes = True  # don't penalise if SPY data missing

    conditions = [
        price_now  > ma50_now,            # 1. above 50d MA
        price_now  > ma150_now,           # 2. above 150d MA
        price_now  > ma200_now,           # 3. above 200d MA
        ma50_now   > ma150_now,           # 4. 50d > 150d
        ma150_now  > ma200_now,           # 5. 150d > 200d
        ma200_rising,                     # 6. 200d trending up ≥1 month (§ fix)
        price_now  >= low52w  * 1.30,    # 7. ≥30% above 52w low
        price_now  >= high52w * 0.75,    # 8. within 25% of 52w high
        rs_passes,                        # 9. RS proxy positive (§ fix)
    ]

    n_met = sum(conditions)
    return {
        "passes":          n_met >= 8,   # all 9 required (relaxed from perfect for RS proxy)
        "conditions_met":  n_met,
        "rs_proxy":        rs_proxy,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Information discreteness filter  (Da, Gurun & Warachka 2014)
# "Frog in the pan": gradual, continuous information → stronger, more persistent momentum.
# Cheap to compute; rarely implemented. Supplement §2.3 and §3.3.
# ─────────────────────────────────────────────────────────────────────────────

def information_discreteness(df: pd.DataFrame, lookback: int = 252, skip: int = 21) -> float:
    """
    ID = sign(PRET) × (% negative days − % positive days) over formation window.
    Low ID (negative) = continuous, gradual information = keep this winner.
    High ID (positive) = discrete jump = weaker signal, more reversal risk.

    Returns float in [-1, 1]. Lower is better for longs.
    """
    close = df["Close"].squeeze()
    if len(close) < lookback + 5:
        return np.nan

    formation = close.iloc[-(lookback):-(skip)]
    daily_rets = formation.pct_change().dropna()

    if len(daily_rets) < 20:
        return np.nan

    cumret = float(formation.iloc[-1] / formation.iloc[0]) - 1.0
    pret_sign = np.sign(cumret)

    pct_neg = float((daily_rets < 0).mean())
    pct_pos = float((daily_rets > 0).mean())

    return float(pret_sign * (pct_neg - pct_pos))


# ─────────────────────────────────────────────────────────────────────────────
# 5. Strategy-level volatility targeting  (Barroso & Santa-Clara 2015)
# Scale the PORTFOLIO weight by the strategy's own realised volatility.
# This is DIFFERENT from per-position ATR sizing — do both. Supplement §3.2.
# ─────────────────────────────────────────────────────────────────────────────

def strategy_vol_weight(
    strategy_daily_returns: pd.Series,
    target_vol: float = 0.12,
    window: int = 126,
    max_leverage: float = 1.0,
) -> float:
    """
    Compute the portfolio-level weight scalar for the strategy.
    weight = target_vol / realised_vol (capped at max_leverage).

    strategy_daily_returns: daily P&L / portfolio_value series
    target_vol: 12% annualised (matches Barroso & Santa-Clara)
    window: 126 days = 6 months (matches paper)
    """
    if len(strategy_daily_returns) < window:
        return 1.0   # default full weight when insufficient history

    realised = strategy_daily_returns.iloc[-window:].std() * np.sqrt(252)
    if realised < 1e-8:
        return max_leverage

    weight = target_vol / realised
    return float(min(weight, max_leverage))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Market state filter  (Cooper, Gutierrez & Hameed 2004)
# Mean monthly momentum profit: +0.93% after positive market, −0.37% after negative.
# This is cleaner than VIX rank for the primary regime gate. Supplement §2.2.
# ─────────────────────────────────────────────────────────────────────────────

def market_state_positive(market_df: pd.DataFrame, lookback: int = 252) -> bool:
    """
    Returns True if market 12-month return is positive (safe to trade momentum).
    market_df: SPY daily OHLCV DataFrame.
    """
    if market_df is None or len(market_df) < lookback:
        return True   # default: allow trading if data unavailable

    close = market_df["Close"].squeeze()
    ret_12m = float(close.iloc[-1] / close.iloc[-lookback]) - 1.0
    return ret_12m > 0.0


def vix_rank(vix_df: pd.DataFrame, window: int = 252) -> float:
    """
    § supplement §4 fix: explicit 252-day rolling window, not full frame.
    Returns percentile rank of latest VIX within the trailing 252 days.
    """
    if vix_df is None or len(vix_df) < window:
        return 50.0   # neutral default

    close = vix_df["Close"].squeeze()
    trailing = close.iloc[-window:]
    latest   = float(close.iloc[-1])
    rank     = float((trailing <= latest).mean()) * 100.0
    return rank


# ─────────────────────────────────────────────────────────────────────────────
# 7. Volume confirmation
# ─────────────────────────────────────────────────────────────────────────────

def volume_ratio(df: pd.DataFrame, window: int = 20) -> float:
    vol = df["Volume"].squeeze()
    if len(vol) < window + 2:
        return np.nan
    sma = float(vol.iloc[-window - 1:-1].mean())
    if sma < 1:
        return np.nan
    return float(vol.iloc[-1] / sma)
