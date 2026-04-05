"""
Telegram Bot — User interface layer for the Portfolio Intelligence Agent.

Routes commands and scheduled triggers through the agentic loop
instead of calling APIs directly.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone, time as dt_time

import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    ALLOWED_CHAT_ID,
    PUBLIC_SECRET_KEY,
    PUBLIC_ACCOUNT_ID,
    ANTHROPIC_API_KEY,
    BREAKING_CHECK_INTERVAL,
    DIGEST_HOUR_UTC,
    PORTFOLIO_SYNC_HOUR_UTC,
    PORTFOLIO_SYNC_MINUTE_UTC,
    DATA_FILE,
)
from agent_loop import run_agent_loop
from public_api import fetch_portfolio
from portfolio_facts import (
    load_facts, save_facts, set_cost_basis,
    update_from_sync, format_facts_for_context,
)
from provider_stats import format_status_report, format_weekly_report

logger = logging.getLogger("agent.bot")

# ── Persistence ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "portfolio_symbols": [],
        "watchlist": [],
        "seen_news_hashes": [],
        "last_digest": None,
        "available_capital": 0,
    }


def save_data(data: dict):
    data["seen_news_hashes"] = data.get("seen_news_hashes", [])[-500:]
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


bot_data = load_data()
portfolio_facts = load_facts()


def is_authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == ALLOWED_CHAT_ID


def build_context() -> dict:
    """Build runtime context dict for the agentic loop."""
    symbols = [s["symbol"] for s in bot_data.get("portfolio_symbols", [])]
    watchlist = bot_data.get("watchlist", [])
    return {
        "portfolio_symbols": symbols,
        "watchlist": watchlist,
        "all_symbols": list(set(symbols + watchlist)),
        "available_capital": bot_data.get("available_capital", 0),
        "portfolio_facts": format_facts_for_context(portfolio_facts),
    }


# ── Agent-powered message handler ───────────────────────────────────────────
async def send_to_agent(
    prompt: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
    workflow_hint: str = None,
):
    """Send a prompt through the agentic loop and deliver the response via Telegram."""
    async with aiohttp.ClientSession() as session:
        response = await run_agent_loop(
            user_message=prompt,
            session=session,
            context=build_context(),
            workflow_hint=workflow_hint,
        )

    # Split long messages for Telegram's 4096 char limit
    for chunk in _split_message(response, 4000):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            # If HTML parsing fails, send as plain text
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                disable_web_page_preview=True,
            )


def _split_message(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        parts.append(current)
    return parts


# ── Command Handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = (
        "👋 <b>Portfolio Intelligence Agent</b>\n\n"
        "I'm your AI-powered portfolio assistant. I use Claude to analyze your holdings, "
        "news, insider trades, and macro data — then deliver actionable insights.\n\n"
        "<b>Commands:</b>\n"
        "/sync — Pull holdings from Public.com\n"
        "/portfolio — View tracked positions\n"
        "/news NVDA — AI-analyzed news for a ticker\n"
        "/insider NVDA — Recent insider trades\n"
        "/macro — Key economic indicators\n"
        "/watchlist — View your watchlist\n"
        "/addwatch TSLA — Add to watchlist\n"
        "/removewatch TSLA — Remove from watchlist\n"
        "/setcost NVDA 120 2.15 — Set cost basis\n"
        "/digest — Trigger daily digest now\n"
        "/ask [anything] — Ask me anything about your portfolio\n"
        "/status — Bot health check\n"
        "/stats — 7-day API reliability report\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync portfolio from Public.com."""
    if not is_authorized(update):
        return

    if not PUBLIC_SECRET_KEY or not PUBLIC_ACCOUNT_ID:
        await update.message.reply_text(
            "⚠️ Public.com API not configured. Set PUBLIC_SECRET_KEY and PUBLIC_ACCOUNT_ID in .env"
        )
        return

    await update.message.reply_text("🔄 Syncing with Public.com...")

    async with aiohttp.ClientSession() as session:
        data = await fetch_portfolio(session)

    if not data:
        await update.message.reply_text(
            "❌ Could not fetch portfolio from Public.com. Check your SECRET_KEY and ACCOUNT_ID."
        )
        return

    positions = []
    for pos in data.get("positions", []):
        inst = pos.get("instrument", {})
        positions.append({
            "symbol": inst.get("symbol", ""),
            "name": inst.get("name", ""),
            "type": inst.get("type", ""),
        })

    # Extract buying power
    bp = data.get("buyingPower", {})
    available_capital = 0
    try:
        available_capital = float(bp.get("buyingPower", 0))
    except (ValueError, TypeError):
        pass

    bot_data["portfolio_symbols"] = positions
    bot_data["available_capital"] = available_capital
    save_data(bot_data)

    # Merge share quantities into portfolio facts
    global portfolio_facts
    holdings_data = {}
    holdings_path = os.path.join("data", "holdings.json")
    if os.path.exists(holdings_path):
        try:
            with open(holdings_path, "r") as f:
                holdings_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    portfolio_facts = update_from_sync(portfolio_facts, positions, holdings_data)

    # Count how many have cost basis
    with_cost = sum(1 for s in portfolio_facts.values() if "cost_basis" in s)

    symbols = ", ".join(f"${p['symbol']}" for p in positions if p['symbol'])
    cost_note = (
        f"\n📊 {with_cost}/{len(positions)} positions have cost basis. "
        f"Use /setcost TICKER PRICE SHARES to add missing ones."
        if with_cost < len(positions) else ""
    )
    await update.message.reply_text(
        f"✅ Synced {len(positions)} positions:\n{symbols}\n\n"
        f"💰 Buying power: ${available_capital:,.2f}{cost_note}",
        parse_mode="HTML",
    )


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    symbols = bot_data.get("portfolio_symbols", [])
    if not symbols:
        await update.message.reply_text("No portfolio loaded. Use /sync first.")
        return

    # Route through agent — batch quote tool fetches all prices in one call
    symbol_list = [s["symbol"] for s in symbols]
    facts_str = format_facts_for_context(portfolio_facts)
    prompt = (
        f"The user wants to see their portfolio. "
        f"Use get_portfolio_quotes with symbols={json.dumps(symbol_list)} to fetch all prices in one call. "
        f"\n\nPosition data (shares & cost basis):\n{facts_str}"
        f"\n\nFormat as a portfolio summary. For positions with cost basis, show total P&L "
        f"(current value vs invested). For positions without cost basis, show daily change only."
    )
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="portfolio")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /news NVDA")
        return

    symbol = context.args[0].upper().replace("$", "")
    prompt = (
        f"Get the latest news for ${symbol} using the company_news filing type. "
        f"Also get the current stock price. "
        f"Analyze the news and give your assessment of the sentiment and what it means for the stock. "
        f"Be direct and concise — this is a Telegram message."
    )
    await update.message.reply_text(f"🔍 Analyzing ${symbol}...")
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="news")


async def cmd_insider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /insider NVDA")
        return

    symbol = context.args[0].upper().replace("$", "")
    prompt = (
        f"Get recent insider trades for ${symbol}. "
        f"Summarize who is buying and selling, the amounts, and what it might signal. "
        f"If there's notable insider buying, highlight it. If insiders are dumping, flag it clearly."
    )
    await update.message.reply_text(f"🔍 Checking insider activity for ${symbol}...")
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="insider")


async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    prompt = (
        "Get the latest readings for the fed funds rate (FEDFUNDS), "
        "10-year treasury yield (DGS10), and CPI inflation (CPIAUCSL). "
        "Summarize the current macro environment and what it means for equity and precious metals investors."
    )
    await update.message.reply_text("📊 Pulling macro data...")
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="macro")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    wl = bot_data.get("watchlist", [])
    if not wl:
        await update.message.reply_text("Watchlist empty. Use /addwatch TSLA AAPL")
        return
    symbols = ", ".join(f"${s}" for s in wl)
    await update.message.reply_text(f"👀 <b>Watchlist:</b> {symbols}", parse_mode="HTML")


async def cmd_addwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addwatch TSLA AAPL NVDA")
        return
    added = []
    for sym in context.args:
        sym = sym.upper().replace("$", "")
        if sym not in bot_data.get("watchlist", []):
            bot_data.setdefault("watchlist", []).append(sym)
            added.append(sym)
    save_data(bot_data)
    if added:
        await update.message.reply_text(f"✅ Added: {', '.join(f'${s}' for s in added)}")
    else:
        await update.message.reply_text("Already on your watchlist.")


async def cmd_removewatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removewatch TSLA")
        return
    removed = []
    for sym in context.args:
        sym = sym.upper().replace("$", "")
        if sym in bot_data.get("watchlist", []):
            bot_data["watchlist"].remove(sym)
            removed.append(sym)
    save_data(bot_data)
    if removed:
        await update.message.reply_text(f"🗑️ Removed: {', '.join(f'${s}' for s in removed)}")


async def cmd_setcost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set cost basis and share count for a position."""
    if not is_authorized(update):
        return

    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /setcost TICKER AVG_PRICE SHARES\n"
            "Example: /setcost NVDA 120.50 2.15"
        )
        return

    symbol = context.args[0].upper().replace("$", "")
    try:
        avg_price = float(context.args[1])
        shares = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Price and shares must be numbers. Example: /setcost NVDA 120.50 2.15")
        return

    global portfolio_facts
    portfolio_facts = set_cost_basis(portfolio_facts, symbol, avg_price, shares)

    total = round(avg_price * shares, 2)
    await update.message.reply_text(
        f"✅ <b>${symbol}</b> cost basis set\n"
        f"  Shares: {shares}\n"
        f"  Avg price: ${avg_price:,.2f}\n"
        f"  Total invested: ${total:,.2f}",
        parse_mode="HTML",
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger daily digest manually via the agent."""
    if not is_authorized(update):
        return

    symbols = [s["symbol"] for s in bot_data.get("portfolio_symbols", [])]
    watchlist = bot_data.get("watchlist", [])

    if not symbols and not watchlist:
        await update.message.reply_text("No holdings or watchlist. Use /sync or /addwatch first.")
        return

    await update.message.reply_text("📊 Generating your daily digest...")

    all_symbols = symbols + [w for w in watchlist if w not in symbols]
    prompt = (
        f"Generate a comprehensive daily portfolio digest. "
        f"Portfolio holdings: {', '.join(symbols)}. "
        f"Watchlist: {', '.join(watchlist) if watchlist else 'none'}. "
        f"\n\nStep 1: Use get_portfolio_quotes with symbols={json.dumps(all_symbols)} to fetch all prices in one call. "
        f"Step 2: Get news for the top 3-5 movers (biggest % change). "
        f"Step 3: Provide your overall market sentiment assessment. "
        f"End with 'This is not financial advice.'"
    )
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="portfolio")

    bot_data["last_digest"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_data(bot_data)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Freeform question to the agent."""
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /ask What's happening with NVDA earnings?")
        return

    question = " ".join(context.args)
    symbols = [s["symbol"] for s in bot_data.get("portfolio_symbols", [])]

    prompt = (
        f"The user asks: {question}\n\n"
        f"Context — their portfolio holds: {', '.join(symbols) if symbols else 'not synced yet'}. "
        f"Watchlist: {', '.join(bot_data.get('watchlist', [])) or 'empty'}. "
        f"Use your tools to look up any data needed to answer thoroughly."
    )
    await send_to_agent(prompt, update.effective_chat.id, context, workflow_hint="ask")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    pc = len(bot_data.get("portfolio_symbols", []))
    wc = len(bot_data.get("watchlist", []))
    ld = bot_data.get("last_digest", "Never")
    cap = bot_data.get("available_capital", 0)

    from config import FINNHUB_KEY, FRED_API_KEY

    # Cost basis coverage
    with_cost = sum(1 for s in portfolio_facts.values() if "cost_basis" in s)

    stats_report = format_status_report()

    msg = (
        "🤖 <b>Agent Status</b>\n\n"
        f"💼 Portfolio: {pc} positions ({with_cost} with cost basis)\n"
        f"👀 Watchlist: {wc} symbols\n"
        f"💰 Buying power: ${cap:,.2f}\n"
        f"📅 Last digest: {ld}\n\n"
        f"<b>Connections:</b>\n"
        f"  {'✅' if ANTHROPIC_API_KEY else '❌'} Claude API (brain)\n"
        f"  {'✅' if PUBLIC_SECRET_KEY else '❌'} Public.com\n"
        f"  {'✅' if FINNHUB_KEY else '❌'} Finnhub\n"
        f"  {'✅' if FRED_API_KEY else '❌'} FRED\n"
        f"  ✅ Yahoo Finance\n"
        f"  ✅ CoinGecko\n"
        f"  ✅ SEC EDGAR\n\n"
        f"<b>Today's API Reliability:</b>\n{stats_report}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show 7-day provider reliability report."""
    if not is_authorized(update):
        return
    report = format_weekly_report()
    await update.message.reply_text(report, parse_mode="HTML")


# Handle any non-command text as a freeform question
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text
    if not text:
        return

    symbols = [s["symbol"] for s in bot_data.get("portfolio_symbols", [])]
    prompt = (
        f"The user says: {text}\n\n"
        f"Their portfolio: {', '.join(symbols) if symbols else 'not synced'}. "
        f"Watchlist: {', '.join(bot_data.get('watchlist', [])) or 'empty'}."
    )
    await send_to_agent(prompt, update.effective_chat.id, context)


# ── Scheduled Jobs ───────────────────────────────────────────────────────────
async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily digest via the agent."""
    if not ALLOWED_CHAT_ID:
        return

    symbols = [s["symbol"] for s in bot_data.get("portfolio_symbols", [])]
    watchlist = bot_data.get("watchlist", [])
    if not symbols and not watchlist:
        return

    all_symbols = symbols + [w for w in watchlist if w not in symbols]
    prompt = (
        f"Generate the daily portfolio digest. "
        f"Portfolio: {', '.join(symbols)}. Watchlist: {', '.join(watchlist) if watchlist else 'none'}. "
        f"\n\nStep 1: Use get_portfolio_quotes with symbols={json.dumps(all_symbols)} to fetch all prices in one call. "
        f"Step 2: Get news for the top 3-5 movers (biggest % change). "
        f"Step 3: Synthesize into a clean daily briefing. "
        f"End with 'This is not financial advice.'"
    )

    async with aiohttp.ClientSession() as session:
        response = await run_agent_loop(
            user_message=prompt, session=session, context=build_context(),
            workflow_hint="portfolio",
        )

    for chunk in _split_message(response, 4000):
        try:
            await context.bot.send_message(
                chat_id=int(ALLOWED_CHAT_ID), text=chunk,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception:
            await context.bot.send_message(
                chat_id=int(ALLOWED_CHAT_ID), text=chunk,
                disable_web_page_preview=True,
            )

    bot_data["last_digest"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_data(bot_data)
    logger.info("Scheduled digest sent")


async def scheduled_sync(context: ContextTypes.DEFAULT_TYPE):
    """Auto-sync portfolio from Public.com."""
    if not PUBLIC_SECRET_KEY or not PUBLIC_ACCOUNT_ID:
        return

    async with aiohttp.ClientSession() as session:
        data = await fetch_portfolio(session)

    if not data:
        return

    positions = []
    for pos in data.get("positions", []):
        inst = pos.get("instrument", {})
        positions.append({
            "symbol": inst.get("symbol", ""),
            "name": inst.get("name", ""),
            "type": inst.get("type", ""),
        })

    bp = data.get("buyingPower", {})
    try:
        bot_data["available_capital"] = float(bp.get("buyingPower", 0))
    except (ValueError, TypeError):
        pass

    bot_data["portfolio_symbols"] = positions
    save_data(bot_data)
    logger.info(f"Auto-synced {len(positions)} positions")


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is required!")
        return

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY not set — agent brain will not function.")
        print("   The bot will start but /news, /digest, /ask won't work.")

    print("🚀 Starting Portfolio Intelligence Agent...")
    print(f"   Claude:     {'✅' if ANTHROPIC_API_KEY else '❌'}")
    print(f"   Public.com: {'✅' if PUBLIC_SECRET_KEY else '❌'}")
    print(f"   Finnhub:    {'✅' if os.getenv('FINNHUB_KEY') else '❌'}")
    print(f"   FRED:       {'✅' if os.getenv('FRED_API_KEY') else '❌'}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("insider", cmd_insider))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("addwatch", cmd_addwatch))
    app.add_handler(CommandHandler("removewatch", cmd_removewatch))
    app.add_handler(CommandHandler("setcost", cmd_setcost))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Freeform messages go through the agent
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduled jobs
    jq = app.job_queue

    # Daily digest at 5 PM ET (22:00 UTC)
    jq.run_daily(
        scheduled_digest,
        time=dt_time(hour=DIGEST_HOUR_UTC, minute=0, second=0, tzinfo=timezone.utc),
        name="daily_digest",
    )

    # Auto-sync at 9:35 AM ET
    jq.run_daily(
        scheduled_sync,
        time=dt_time(
            hour=PORTFOLIO_SYNC_HOUR_UTC,
            minute=PORTFOLIO_SYNC_MINUTE_UTC,
            second=0,
            tzinfo=timezone.utc,
        ),
        name="auto_sync",
    )

    print("✅ Agent is live. Send /start in Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
