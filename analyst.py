"""
analyst.py — Claude used ONLY for research notes and weekly review.

§ supplement §4: "Use an LLM for research notes and anomaly monitoring,
never for the order decision."

Claude Haiku is called here for:
  1. Weekly trade review (Sunday, ~500 tokens output)
  2. Anomaly flags (e.g. signal divergence, unusual scan results)
  3. On-demand explanations via Telegram /explain command

Claude is NEVER called from executor.py, screener.py, or the order path.
Estimated cost: ~£0.50/month at 2 weekly reviews + occasional Telegram queries.
"""

import os, json
import anthropic
from config import CFG

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", CFG.anthropic_key))


def weekly_review(trades: list, regime_log: list) -> str:
    """
    Sunday evening: feed the week's trade log to Claude Haiku for a review note.
    Pure research — the output is text for your reading, never fed back into orders.
    Uses Haiku (~£0.002 per call at 1,500 tokens).
    """
    if not trades:
        return "No trades this week to review."

    wins  = [t for t in trades if t.get("pnl", 0) > 0]
    loses = [t for t in trades if t.get("pnl", 0) <= 0]

    prompt = f"""You are reviewing an automated momentum trading bot's weekly performance.
The strategies used are: residual momentum (Blitz et al 2011), volatility-scaled cross-sectional
momentum, and Minervini trend template. All trade decisions were deterministic — you were not
involved. Your job is to write a concise research note, not to suggest trades.

Trade log this week ({len(trades)} trades):
{json.dumps(trades[-20:], indent=2, default=str)}

Regime conditions logged:
{json.dumps(regime_log[-5:], indent=2, default=str)}

Write a 3-paragraph research note:
1. Performance summary: win rate, average R:R, any pattern in winners vs losers.
2. Signal quality: did residual momentum scores correlate with outcomes? Any anomalies?
3. Market regime observation: was the Cooper et al market-state filter working correctly?
   Any concern about crowding (supplement reference: Ehsani & Linnainmaa 2022)?

Be concise, honest, and flag anything worth investigating. This is a research note, not advice."""

    response = client.messages.create(
        model=CFG.claude_model,   # Haiku
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def explain_signal(candidate: dict) -> str:
    """
    On-demand explanation of why a specific candidate scored as it did.
    Called via Telegram /explain NVDA — for your understanding, not for order logic.
    """
    prompt = f"""Explain in plain English why this stock scored as a momentum candidate.
Focus on what the academic signals mean, not whether to trade it.

Candidate data:
{json.dumps(candidate, indent=2, default=str)}

Signal definitions:
- residual_mom: t-statistic of FF3-adjusted returns (Blitz et al 2011). Higher = stronger.
- vol_adj_mom: raw 12-1m return divided by return volatility. Risk-adjusted.
- id_score: information discreteness (Da et al 2014). Lower = more gradual info = better.
- trend_template conditions_met: out of 9 Minervini criteria.

2 short paragraphs. Plain language. No recommendation."""

    response = client.messages.create(
        model=CFG.claude_model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def flag_anomaly(scan_result: dict) -> str | None:
    """
    Quick anomaly check after each scan. Returns a flag string or None.
    Only called if the scan result looks unusual (e.g. all signals same direction,
    very high vol-adj scores). Cost: ~£0.001 per call.
    """
    longs  = scan_result.get("long", [])
    shorts = scan_result.get("short", [])

    # Heuristic triggers — call Claude only if something looks odd
    all_scores = [c["primary_score"] for c in longs + shorts if c.get("primary_score")]
    if not all_scores:
        return None

    max_score = max(abs(s) for s in all_scores)

    # Unusually high scores can indicate crowding (Ehsani & Linnainmaa 2022)
    if max_score > 3.0:
        prompt = f"""A momentum scan returned an unusually high signal score of {max_score:.2f}
(residual momentum t-statistic). This may indicate data issues, a mismatch in the FF3 factor
proxies, or genuine crowding (per Ehsani & Linnainmaa 2022 on factor momentum crowding).

Top candidates: {json.dumps((longs + shorts)[:3], indent=2, default=str)}

In one sentence: is this score level plausible, or does it suggest a data or model issue?"""

        response = client.messages.create(
            model=CFG.claude_model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        return f"⚠️ Anomaly flag: {response.content[0].text.strip()}"

    return None
