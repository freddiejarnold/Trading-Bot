"""
telegram_bot.py — interactive Telegram command bot.

Runs as a separate always-on process on Render.com free tier.
Listens for commands and responds with live portfolio data from Alpaca.

Commands:
  /status   — live portfolio snapshot with P&L
  /positions — open positions detail
  /regime   — current market regime check
  /explain TICKER — why that ticker scored as it did
  /help     — list commands

Deploy on Render.com:
  1. Add this file to your GitHub repo
  2. Create a new Web Service on Render pointing to the repo
  3. Set Start Command: python telegram_bot.py
  4. Add all environment variables (same as GitHub Secrets)
"""

import os, asyncio, logging
from datetime import datetime
import yfinance as yf

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def get_alpaca():
    import alpaca_trade_api as tradeapi
    return tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_URL)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full portfolio snapshot — positions, P&L, regime."""
    await update.message.reply_text("📊 Fetching live data...")

    try:
        api      = get_alpaca()
        account  = api.get_account()
        positions = api.list_positions()

        equity      = float(account.equity)
        cash        = float(account.cash)
        last_equity = float(account.last_equity)
        day_pnl     = equity - last_equity
        day_pct     = (day_pnl / last_equity * 100) if last_equity > 0 else 0
        pl_emoji    = "🟢" if day_pnl >= 0 else "🔴"

        lines = [
            f"📈 *Portfolio Status*",
            f"_{datetime.now().strftime('%d %b %Y, %H:%M')}_\n",
            f"*Account*",
            f"Equity: `${equity:,.2f}`",
            f"Cash: `${cash:,.2f}`",
            f"Day P&L: {pl_emoji} `${day_pnl:+,.2f}` ({day_pct:+.2f}%)\n",
        ]

        if positions:
            total_unrealised = sum(float(p.unrealized_pl) for p in positions)
            ur_emoji = "🟢" if total_unrealised >= 0 else "🔴"
            lines.append(f"*Open positions ({len(positions)})* — {ur_emoji} `${total_unrealised:+,.2f}` total\n")

            for p in positions:
                pl      = float(p.unrealized_pl)
                pl_pct  = float(p.unrealized_plpc) * 100
                emoji   = "📈" if pl >= 0 else "📉"
                lines.append(
                    f"{emoji} `{p.symbol}` — {p.qty} shares\n"
                    f"   Entry: `${float(p.avg_entry_price):.2f}` → Now: `${float(p.current_price):.2f}`\n"
                    f"   P&L: `${pl:+,.2f}` ({pl_pct:+.1f}%)"
                )
        else:
            lines.append("*Positions:* None open")

        # Regime check
        spy = yf.download("SPY", period="14mo", interval="1d", progress=False)
        vix = yf.download("^VIX", period="14mo", interval="1d", progress=False)
        spy_ret  = float(spy["Close"].squeeze().iloc[-1] / spy["Close"].squeeze().iloc[-252] - 1) * 100
        vix_now  = float(vix["Close"].squeeze().iloc[-1])
        vix_rank = float((vix["Close"].squeeze().iloc[-252:] <= vix_now).mean() * 100)
        ms_emoji = "✅" if spy_ret > 0 else "⚠️"

        lines.append(
            f"\n*Regime*\n"
            f"SPY 12m: {ms_emoji} `{spy_ret:+.1f}%`\n"
            f"VIX: `{vix_now:.1f}` (rank {vix_rank:.0f}th pct)"
        )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detailed positions with stops and take profits."""
    try:
        api = get_alpaca()
        pos = api.list_positions()

        if not pos:
            await update.message.reply_text("No open positions.")
            return

        lines = ["📋 *Open positions*\n"]
        for p in pos:
            pl     = float(p.unrealized_pl)
            pl_pct = float(p.unrealized_plpc) * 100
            emoji  = "🟢" if pl >= 0 else "🔴"
            lines.append(
                f"{emoji} *{p.symbol}* ({p.side})\n"
                f"Qty: {p.qty} @ `${float(p.avg_entry_price):.2f}`\n"
                f"Now: `${float(p.current_price):.2f}` · P&L: `${pl:+,.2f}` ({pl_pct:+.1f}%)\n"
                f"Market value: `${float(p.market_value):,.2f}`"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Current market regime snapshot."""
    try:
        spy = yf.download("SPY", period="14mo", interval="1d", progress=False)
        vix = yf.download("^VIX", period="14mo", interval="1d", progress=False)

        spy_ret  = float(spy["Close"].squeeze().iloc[-1] / spy["Close"].squeeze().iloc[-252] - 1) * 100
        vix_now  = float(vix["Close"].squeeze().iloc[-1])
        vix_rank = float((vix["Close"].squeeze().iloc[-252:] <= vix_now).mean() * 100)

        ms_ok    = spy_ret > 0
        ms_emoji = "✅" if ms_ok else "⚠️"
        vix_note = "Normal" if vix_rank < 60 else ("Elevated — size reduced" if vix_rank < 80 else "High — caution")

        msg = (
            f"🌐 *Market Regime*\n\n"
            f"SPY 12m return: {ms_emoji} `{spy_ret:+.1f}%`\n"
            f"Momentum gate: `{'OPEN — trading allowed' if ms_ok else 'CLOSED — no new positions'}`\n\n"
            f"VIX: `{vix_now:.1f}` (rank {vix_rank:.0f}th pct)\n"
            f"VIX status: `{vix_note}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def explain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explain why a ticker scored as it did. Usage: /explain ARM"""
    if not context.args:
        await update.message.reply_text("Usage: /explain TICKER (e.g. /explain ARM)")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"🔍 Analysing {ticker}...")

    try:
        import anthropic
        from signals import raw_momentum_score, trend_template_score, information_discreteness

        df     = yf.download(ticker, period="5y", interval="1d", progress=False)
        spy_df = yf.download("SPY",  period="14mo", interval="1d", progress=False)

        raw_mom = raw_momentum_score(df)
        tt      = trend_template_score(df, spy_df)
        id_sc   = information_discreteness(df)

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        prompt = f"""Explain in 3 plain sentences why {ticker} scored as a momentum candidate.
Raw data: 12-1m return={raw_mom:.1%}, trend template {tt['conditions_met']}/9 conditions met,
information discreteness={id_sc:.3f} (lower=more gradual=better).
Be specific. No recommendation."""

        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        explanation = response.content[0].text.strip()
        await update.message.reply_text(f"🔍 *{ticker}*\n\n{explanation}", parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error analysing {ticker}: {e}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Trading Bot Commands*\n\n"
        "/status — full portfolio snapshot with live P&L\n"
        "/positions — open positions detail\n"
        "/regime — current market regime check\n"
        "/explain TICKER — why a stock scored as it did\n"
        "/help — this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("status",   status))
    app.add_handler(CommandHandler("positions", positions))
    app.add_handler(CommandHandler("regime",   regime))
    app.add_handler(CommandHandler("explain",  explain))
    app.add_handler(CommandHandler("help",     help_cmd))
    app.add_handler(CommandHandler("start",    help_cmd))

    print("Bot polling — send /status to your Telegram bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
