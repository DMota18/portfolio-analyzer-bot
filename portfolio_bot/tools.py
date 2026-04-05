"""
MCP Tool Definitions & Execution Router — Domain 2 Refactor

Changes from v1:
  1. TOOL DESCRIPTIONS: Production-grade with purpose, inputs, examples, 
     edge cases, limitations, and explicit "Do NOT use for" boundaries
  2. STRUCTURED ERRORS: Four categories (transient, validation, business, 
     permission) with isError, isRetryable, and descriptive messages.
     Critically distinguishes access failures from valid empty results.
  3. TOOL DISTRIBUTION: 5 tools scoped to single responsibility. 
     Split get_company_filings into get_insider_trades + get_company_news.
     Removed filing_type multiplexer that caused misrouting.
  4. Financial Datasets API support with trailing slash requirement.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from config import FINNHUB_KEY, FRED_API_KEY, SEC_EDGAR_USER_AGENT, FIN_DATASETS_KEY
from provider_stats import record_fallback

logger = logging.getLogger("agent.tools")


# ═════════════════════════════════════════════════════════════════════════════
# STRUCTURED ERROR RESPONSES
# ═════════════════════════════════════════════════════════════════════════════
def _error_transient(provider: str, symbol: str, detail: str, status: int = 0) -> dict:
    """Network timeout, rate limit, 5xx — Claude should retry or inform user to wait."""
    return {
        "ok": False,
        "error_category": "transient",
        "is_retryable": True,
        "provider": provider,
        "symbol": symbol,
        "message": detail,
        "http_status": status,
    }


def _error_validation(provider: str, symbol: str, detail: str) -> dict:
    """Bad input — wrong symbol format, missing required field. Do NOT retry."""
    return {
        "ok": False,
        "error_category": "validation",
        "is_retryable": False,
        "provider": provider,
        "symbol": symbol,
        "message": detail,
    }


def _error_business(provider: str, symbol: str, detail: str) -> dict:
    """Valid request, valid empty result — ticker delisted, no insider trades exist, etc.
    This is NOT a failure. Do NOT retry. Report the absence as information."""
    return {
        "ok": True,  # NOTE: ok=True because the request succeeded, data is just empty
        "error_category": "business",
        "is_retryable": False,
        "provider": provider,
        "symbol": symbol,
        "message": detail,
        "data": [],
    }


def _error_permission(provider: str, detail: str) -> dict:
    """Missing API key, expired token, 401/403. Do NOT retry."""
    return {
        "ok": False,
        "error_category": "permission",
        "is_retryable": False,
        "provider": provider,
        "symbol": "",
        "message": detail,
    }


def _classify_http_error(status: int, provider: str, symbol: str, body: str = "") -> dict:
    """Map HTTP status codes to the correct error category."""
    if status == 401 or status == 403:
        return _error_permission(provider, f"Authentication failed ({status}). Check your {provider} API key.")
    elif status == 404:
        return _error_business(provider, symbol, f"Symbol '{symbol}' not found on {provider}. It may be delisted, misspelled, or not covered.")
    elif status == 422 or status == 400:
        return _error_validation(provider, symbol, f"Invalid request ({status}): {body[:200]}")
    elif status == 429:
        return _error_transient(provider, symbol, f"Rate limited by {provider}. Wait 60 seconds before retrying.", status)
    elif status >= 500:
        return _error_transient(provider, symbol, f"{provider} server error ({status}). This is on their end, try again shortly.", status)
    else:
        return _error_transient(provider, symbol, f"Unexpected response from {provider}: HTTP {status}", status)


# ═════════════════════════════════════════════════════════════════════════════
# TOOL DEFINITIONS — Production-grade descriptions
# ═════════════════════════════════════════════════════════════════════════════
TOOL_DEFINITIONS = [
    # ── TOOL 1: Stock Quotes ─────────────────────────────────────────────
    {
        "name": "get_stock_quote",
        "description": (
            "PRIMARY PURPOSE: Fetch current price, daily change, volume, and key metrics "
            "for a US-listed stock, ETF, or precious metals ETF.\n\n"
            "INPUT: Standard US ticker symbols in uppercase (e.g. NVDA, AAPL, GLD, SPY, QQQ). "
            "Optionally specify provider: 'financial_datasets' for large-cap equities with "
            "fundamentals, 'finnhub' for real-time intraday quotes, or 'yahoo' as general fallback.\n\n"
            "EXAMPLE QUERIES: 'What's NVDA trading at?' → symbol='NVDA'. "
            "'Get me the price of gold' → symbol='GLD' (the gold ETF).\n\n"
            "EDGE CASES: OTC stocks may not be available on all providers. "
            "If Financial Datasets returns empty, the tool automatically falls back to Yahoo Finance. "
            "Pre/post market data is only available from Finnhub.\n\n"
            "LIMITATIONS: Does not return historical price series — only current/latest quote. "
            "Does not support non-US exchanges.\n\n"
            "DO NOT USE FOR: Cryptocurrency prices (use get_crypto_data instead). "
            "Macroeconomic indicators like interest rates or CPI (use get_macro_data). "
            "Company news or insider trades (use get_company_news or get_insider_trades)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "US ticker symbol in uppercase. Examples: NVDA, AAPL, GLD, SPY, MSFT",
                },
                "provider": {
                    "type": "string",
                    "enum": ["financial_datasets", "yahoo", "finnhub"],
                    "description": (
                        "Data provider. 'financial_datasets' for large-cap with fundamentals (default), "
                        "'finnhub' for real-time intraday, 'yahoo' as fallback for broad coverage."
                    ),
                },
            },
            "required": ["symbol"],
        },
    },
    # ── TOOL 1b: Batch Stock Quotes ──────────────────────────────────────
    {
        "name": "get_portfolio_quotes",
        "description": (
            "PRIMARY PURPOSE: Fetch current prices for MULTIPLE stocks in a single call. "
            "Use this instead of calling get_stock_quote repeatedly when you need prices "
            "for a portfolio or list of symbols.\n\n"
            "INPUT: A JSON array of US ticker symbols in uppercase "
            "(e.g. [\"NVDA\", \"AMZN\", \"PLTR\", \"GOOGL\"]). Maximum 25 symbols per call.\n\n"
            "WHEN TO USE: Always use this for /portfolio commands, daily digests, or any "
            "request involving 3+ stock prices. It fetches all quotes concurrently and returns "
            "them in a single response — far more efficient than individual get_stock_quote calls.\n\n"
            "OUTPUT: Returns a 'quotes' array with each symbol's price data, plus a 'failed' "
            "array listing any symbols that could not be fetched. Partial results are normal — "
            "present what succeeded and note what failed.\n\n"
            "DO NOT USE FOR: A single stock lookup (use get_stock_quote). "
            "Cryptocurrency prices (use get_crypto_data). "
            "Macro indicators (use get_macro_data)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of US ticker symbols in uppercase. Example: [\"NVDA\", \"AMZN\", \"PLTR\"]",
                },
            },
            "required": ["symbols"],
        },
    },
    # ── TOOL 2: Crypto Data ──────────────────────────────────────────────
    {
        "name": "get_crypto_data",
        "description": (
            "PRIMARY PURPOSE: Fetch current price, market cap, 24h change, volume, and "
            "all-time high for a cryptocurrency.\n\n"
            "INPUT: CoinGecko coin IDs — these are lowercase slugs, NOT ticker symbols. "
            "Examples: 'bitcoin' (not BTC), 'ethereum' (not ETH), 'solana' (not SOL), "
            "'cardano' (not ADA), 'dogecoin' (not DOGE). Optionally specify vs_currency "
            "(default: 'usd').\n\n"
            "EXAMPLE QUERIES: 'How's Bitcoin doing?' → coin_id='bitcoin'. "
            "'What's the price of ETH in euros?' → coin_id='ethereum', vs_currency='eur'.\n\n"
            "EDGE CASES: New or very small-cap tokens may not be listed on CoinGecko. "
            "If coin_id is wrong, the API returns empty — check spelling. "
            "CoinGecko free tier allows ~30 calls/minute; rate limiting returns a specific message.\n\n"
            "LIMITATIONS: Data updates every 1-2 minutes, not real-time tick data. "
            "Does not support DEX-only tokens or LP tokens. No historical chart data.\n\n"
            "DO NOT USE FOR: Stock or ETF prices (use get_stock_quote even for crypto ETFs like BITO). "
            "Macro data like interest rates (use get_macro_data). "
            "Fear & Greed index (this tool returns price data only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "coin_id": {
                    "type": "string",
                    "description": (
                        "CoinGecko coin ID (lowercase slug). "
                        "Common: bitcoin, ethereum, solana, cardano, dogecoin, ripple, polkadot"
                    ),
                },
                "vs_currency": {
                    "type": "string",
                    "description": "Quote currency. Default: usd. Options: usd, eur, gbp, btc, eth",
                },
            },
            "required": ["coin_id"],
        },
    },
    # ── TOOL 3: Company News ─────────────────────────────────────────────
    {
        "name": "get_company_news",
        "description": (
            "PRIMARY PURPOSE: Fetch recent news articles and headlines for a specific "
            "publicly traded company. Returns headline, source, URL, summary, and timestamp.\n\n"
            "INPUT: Standard US ticker symbol in uppercase. Optionally set limit (default 5, max 10) "
            "and lookback_days (default 7, max 30).\n\n"
            "EXAMPLE QUERIES: 'What's in the news for NVDA?' → symbol='NVDA'. "
            "'Any Tesla news this month?' → symbol='TSLA', lookback_days=30.\n\n"
            "EDGE CASES: Very small companies may have zero news coverage — this is a valid "
            "empty result, not an error. Weekend/holiday gaps are normal.\n\n"
            "LIMITATIONS: English-language sources only. Does not include social media sentiment, "
            "Reddit, or Twitter discussions. News articles may be 15-60 minutes delayed from "
            "wire services.\n\n"
            "DO NOT USE FOR: Insider trading activity (use get_insider_trades). "
            "Current stock price (use get_stock_quote). "
            "Cryptocurrency news (not supported — use web search instead). "
            "Macroeconomic news (use get_macro_data for data, web search for commentary)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "US ticker symbol in uppercase (e.g. NVDA, TSLA, AAPL)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of articles to return. Default 5, max 10.",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "How many days back to search. Default 7, max 30.",
                },
            },
            "required": ["symbol"],
        },
    },
    # ── TOOL 4: Insider Trades ───────────────────────────────────────────
    {
        "name": "get_insider_trades",
        "description": (
            "PRIMARY PURPOSE: Fetch recent SEC Form 4 insider transactions — purchases, sales, "
            "option exercises, and grants by company officers, directors, and 10%+ owners.\n\n"
            "INPUT: Standard US ticker symbol in uppercase. Optionally set limit (default 10).\n\n"
            "EXAMPLE QUERIES: 'Are NVDA insiders buying or selling?' → symbol='NVDA'. "
            "'Show me insider trades for Tesla' → symbol='TSLA'.\n\n"
            "EDGE CASES: Many companies show mostly 'Sale' and 'Tax withholding' transactions — "
            "this is normal (executives selling vested shares). What's signal is unusual PURCHASES "
            "by multiple insiders. Some companies have very few insiders and may show no recent activity — "
            "this is a valid empty result, not an error.\n\n"
            "LIMITATIONS: Data is sourced from SEC EDGAR filings via Finnhub. Filings can be "
            "delayed up to 48 hours from the actual transaction date. Does not include 13F "
            "institutional holdings (hedge fund positions).\n\n"
            "DO NOT USE FOR: Company news or headlines (use get_company_news). "
            "Current stock price (use get_stock_quote). "
            "Institutional/hedge fund holdings (not currently supported). "
            "Crypto wallet tracking (not supported)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "US ticker symbol in uppercase (e.g. NVDA, TSLA, AAPL)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of transactions to return. Default 10.",
                },
            },
            "required": ["symbol"],
        },
    },
    # ── TOOL 5: Macro Data ───────────────────────────────────────────────
    {
        "name": "get_macro_data",
        "description": (
            "PRIMARY PURPOSE: Fetch macroeconomic time series data from the Federal Reserve "
            "Economic Database (FRED). Returns the most recent observations for a given indicator.\n\n"
            "INPUT: FRED series ID in uppercase. Common series:\n"
            "  - FEDFUNDS: Federal funds effective rate\n"
            "  - DGS10: 10-year Treasury yield\n"
            "  - DGS2: 2-year Treasury yield\n"
            "  - CPIAUCSL: Consumer Price Index (inflation)\n"
            "  - UNRATE: Unemployment rate\n"
            "  - GDP: Gross Domestic Product\n"
            "  - MORTGAGE30US: 30-year fixed mortgage rate\n"
            "  - VIXCLS: VIX volatility index (daily close)\n\n"
            "EXAMPLE QUERIES: 'What's the current fed funds rate?' → series_id='FEDFUNDS'. "
            "'Show me the yield curve' → call twice with DGS2 and DGS10.\n\n"
            "EDGE CASES: Some series update weekly (MORTGAGE30US), monthly (CPI, UNRATE), "
            "or quarterly (GDP). If the latest observation seems stale, that's the release "
            "schedule — not an error. FRED returns '.' for missing values.\n\n"
            "LIMITATIONS: FRED data is US-focused. Does not cover individual stock fundamentals, "
            "earnings, or company-level data. Release dates lag real-time — CPI is ~2 weeks "
            "after the reference month.\n\n"
            "DO NOT USE FOR: Stock prices (use get_stock_quote). "
            "Crypto prices (use get_crypto_data). "
            "Company news or insider trades (use respective tools). "
            "Non-US economic data (not supported)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "series_id": {
                    "type": "string",
                    "description": (
                        "FRED series ID in uppercase. Examples: FEDFUNDS, DGS10, DGS2, "
                        "CPIAUCSL, UNRATE, GDP, MORTGAGE30US, VIXCLS"
                    ),
                },
                "observation_start": {
                    "type": "string",
                    "description": "Start date YYYY-MM-DD. Default: 1 year ago.",
                },
                "observation_end": {
                    "type": "string",
                    "description": "End date YYYY-MM-DD. Default: today.",
                },
            },
            "required": ["series_id"],
        },
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# TOOL EXECUTION ROUTER
# ═════════════════════════════════════════════════════════════════════════════
async def execute_tool(
    tool_name: str, tool_input: dict, session: aiohttp.ClientSession
) -> dict:
    """Route a tool call to its provider implementation."""
    router = {
        "get_stock_quote": _exec_stock_quote,
        "get_portfolio_quotes": _exec_portfolio_quotes,
        "get_crypto_data": _exec_crypto_data,
        "get_company_news": _exec_company_news,
        "get_insider_trades": _exec_insider_trades,
        "get_macro_data": _exec_macro_data,
    }

    handler = router.get(tool_name)
    if not handler:
        return _error_validation("system", "", f"Unknown tool: {tool_name}")

    return await handler(tool_input, session)


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 1: Stock Quotes (Financial Datasets → Yahoo fallback)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_stock_quote(params: dict, session: aiohttp.ClientSession) -> dict:
    symbol = params.get("symbol", "").upper().strip()
    if not symbol or not symbol.isalpha():
        return _error_validation("system", symbol, f"Invalid ticker symbol: '{symbol}'. Must be uppercase letters only (e.g. NVDA, AAPL).")

    provider = params.get("provider", "financial_datasets")

    # Try Financial Datasets first (if key available)
    if provider == "financial_datasets" and FIN_DATASETS_KEY:
        result = await _financial_datasets_quote(symbol, session)
        if result.get("ok") is not False or result.get("error_category") == "business":
            return result
        # Fall through to Yahoo on transient errors only
        if result.get("is_retryable"):
            logger.info(f"Financial Datasets transient error for {symbol}, falling back to Yahoo")
            record_fallback("financial_datasets", "yahoo", symbol)

    # Finnhub for real-time
    if provider == "finnhub" and FINNHUB_KEY:
        return await _finnhub_quote(symbol, session)

    # Yahoo fallback
    return await _yahoo_quote(symbol, session)


async def _financial_datasets_quote(symbol: str, session: aiohttp.ClientSession) -> dict:
    """Fetch from Financial Datasets API. NOTE: requires trailing slash on URLs."""
    url = f"https://api.financialdatasets.ai/financial-data/quote/{symbol}/"  # TRAILING SLASH REQUIRED
    headers = {"Authorization": f"Bearer {FIN_DATASETS_KEY}"}

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "financial_datasets", symbol, body)
            data = await resp.json()

        quote = data.get("quote") or data
        if not quote or (isinstance(quote, dict) and not quote.get("price") and not quote.get("close")):
            return _error_business("financial_datasets", symbol,
                f"No quote data available for {symbol} on Financial Datasets. May not be covered.")

        return {
            "ok": True,
            "provider": "financial_datasets",
            "symbol": symbol,
            "price": quote.get("price") or quote.get("close"),
            "previous_close": quote.get("previous_close"),
            "open": quote.get("open"),
            "high": quote.get("high") or quote.get("day_high"),
            "low": quote.get("low") or quote.get("day_low"),
            "volume": quote.get("volume"),
            "market_cap": quote.get("market_cap"),
            "change": quote.get("change"),
            "change_pct": quote.get("change_percent") or quote.get("change_pct"),
            "name": quote.get("name", symbol),
        }
    except aiohttp.ClientError as e:
        return _error_transient("financial_datasets", symbol, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("financial_datasets", symbol, f"Unexpected error: {e}")


async def _yahoo_quote(symbol: str, session: aiohttp.ClientSession) -> dict:
    """Fetch from Yahoo Finance v8 API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "5d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "yahoo", symbol, body)
            data = await resp.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return _error_business("yahoo", symbol, f"No data returned for {symbol}. May be delisted or not traded on US exchanges.")

        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")

        return {
            "ok": True,
            "provider": "yahoo",
            "symbol": symbol,
            "price": price,
            "previous_close": prev,
            "volume": meta.get("regularMarketVolume"),
            "market_cap": meta.get("marketCap"),
            "week_52_high": meta.get("fiftyTwoWeekHigh"),
            "week_52_low": meta.get("fiftyTwoWeekLow"),
            "currency": meta.get("currency", "USD"),
            "exchange": meta.get("exchangeName"),
            "change": round(price - prev, 4) if price and prev else None,
            "change_pct": round(((price - prev) / prev) * 100, 4) if price and prev and prev > 0 else None,
        }
    except aiohttp.ClientError as e:
        return _error_transient("yahoo", symbol, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("yahoo", symbol, f"Unexpected error: {e}")


async def _finnhub_quote(symbol: str, session: aiohttp.ClientSession) -> dict:
    """Fetch real-time quote from Finnhub."""
    if not FINNHUB_KEY:
        return _error_permission("finnhub", "FINNHUB_KEY not set.")

    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": FINNHUB_KEY}

    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "finnhub", symbol, body)
            data = await resp.json()

        price = data.get("c", 0)
        if price == 0:
            return _error_business("finnhub", symbol, f"No quote data for {symbol} on Finnhub. May be unsupported or markets are closed with no data.")

        prev = data.get("pc", 0)
        return {
            "ok": True,
            "provider": "finnhub",
            "symbol": symbol,
            "price": price,
            "previous_close": prev,
            "day_high": data.get("h"),
            "day_low": data.get("l"),
            "day_open": data.get("o"),
            "change": data.get("d"),
            "change_pct": data.get("dp"),
            "timestamp": data.get("t"),
        }
    except aiohttp.ClientError as e:
        return _error_transient("finnhub", symbol, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("finnhub", symbol, f"Unexpected error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 1b: Batch Portfolio Quotes (concurrent fetch)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_portfolio_quotes(params: dict, session: aiohttp.ClientSession) -> dict:
    """Fetch quotes for multiple symbols concurrently using the existing provider chain."""
    import asyncio

    symbols = params.get("symbols", [])
    if not symbols:
        return _error_validation("system", "", "symbols array is required and must not be empty.")
    if len(symbols) > 25:
        return _error_validation("system", "", f"Too many symbols ({len(symbols)}). Maximum is 25 per call.")

    # Deduplicate and normalize
    seen = set()
    clean_symbols = []
    for s in symbols:
        s = s.upper().strip()
        if s and s not in seen:
            seen.add(s)
            clean_symbols.append(s)

    # Fetch all quotes concurrently via the existing single-quote handler
    tasks = [
        _exec_stock_quote({"symbol": s}, session)
        for s in clean_symbols
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    quotes = []
    failed = []

    for symbol, result in zip(clean_symbols, results):
        if isinstance(result, Exception):
            failed.append({"symbol": symbol, "error": str(result)})
        elif result.get("ok") is False:
            failed.append({"symbol": symbol, "error": result.get("message", "Unknown error")})
        else:
            quotes.append(result)

    return {
        "ok": True,
        "provider": "multi",
        "quotes": quotes,
        "failed": failed,
        "total_requested": len(clean_symbols),
        "total_succeeded": len(quotes),
        "total_failed": len(failed),
    }


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 2: Crypto Data (CoinGecko)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_crypto_data(params: dict, session: aiohttp.ClientSession) -> dict:
    coin_id = params.get("coin_id", "").lower().strip()
    if not coin_id:
        return _error_validation("coingecko", "", "coin_id is required. Use CoinGecko slugs like 'bitcoin', 'ethereum'.")

    vs_currency = params.get("vs_currency", "usd").lower()

    url = "https://api.coingecko.com/api/v3/coins/markets"
    query = {
        "vs_currency": vs_currency,
        "ids": coin_id,
        "order": "market_cap_desc",
        "sparkline": "false",
    }

    try:
        async with session.get(url, params=query, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "coingecko", coin_id, body)
            data = await resp.json()

        if not data or len(data) == 0:
            return _error_business("coingecko", coin_id,
                f"No data for coin_id '{coin_id}'. Check spelling — use CoinGecko slugs "
                f"(e.g. 'bitcoin' not 'BTC', 'ethereum' not 'ETH').")

        coin = data[0]
        return {
            "ok": True,
            "provider": "coingecko",
            "symbol": coin.get("symbol", coin_id),
            "name": coin.get("name"),
            "price": coin.get("current_price"),
            "market_cap": coin.get("market_cap"),
            "volume": coin.get("total_volume"),
            "change_pct": coin.get("price_change_percentage_24h"),
            "day_high": coin.get("high_24h"),
            "day_low": coin.get("low_24h"),
            "ath": coin.get("ath"),
            "ath_date": coin.get("ath_date"),
            "timestamp": coin.get("last_updated"),
            "vs_currency": vs_currency,
        }
    except aiohttp.ClientError as e:
        return _error_transient("coingecko", coin_id, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("coingecko", coin_id, f"Unexpected error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 3: Company News (Finnhub)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_company_news(params: dict, session: aiohttp.ClientSession) -> dict:
    symbol = params.get("symbol", "").upper().strip()
    if not symbol:
        return _error_validation("finnhub", "", "symbol is required.")

    if not FINNHUB_KEY:
        return _error_permission("finnhub", "FINNHUB_KEY not set. Cannot fetch company news.")

    limit = min(params.get("limit", 5), 10)
    lookback = min(params.get("lookback_days", 7), 30)

    today = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=lookback)).strftime("%Y-%m-%d")

    url = "https://finnhub.io/api/v1/company-news"
    query = {"symbol": symbol, "from": from_date, "to": today, "token": FINNHUB_KEY}

    try:
        async with session.get(url, params=query, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "finnhub", symbol, body)
            data = await resp.json()

        articles = data[:limit] if isinstance(data, list) else []

        # CRITICAL: Empty articles is a valid business result, NOT an error
        if not articles:
            return _error_business("finnhub", symbol,
                f"No news articles found for {symbol} in the last {lookback} days. "
                f"This may be a low-coverage stock or a quiet news period.")

        return {
            "ok": True,
            "provider": "finnhub",
            "symbol": symbol,
            "articles": [
                {
                    "headline": a.get("headline", ""),
                    "url": a.get("url", ""),
                    "source": a.get("source", ""),
                    "summary": a.get("summary", "")[:300],  # Truncate long summaries
                    "datetime": a.get("datetime", 0),
                    "category": a.get("category", ""),
                }
                for a in articles
            ],
            "count": len(articles),
        }
    except aiohttp.ClientError as e:
        return _error_transient("finnhub", symbol, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("finnhub", symbol, f"Unexpected error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 4: Insider Trades (Finnhub → SEC EDGAR)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_insider_trades(params: dict, session: aiohttp.ClientSession) -> dict:
    symbol = params.get("symbol", "").upper().strip()
    if not symbol:
        return _error_validation("finnhub", "", "symbol is required.")

    if not FINNHUB_KEY:
        return _error_permission("finnhub", "FINNHUB_KEY not set. Cannot fetch insider trades.")

    limit = min(params.get("limit", 10), 25)

    url = "https://finnhub.io/api/v1/stock/insider-transactions"
    query = {"symbol": symbol, "token": FINNHUB_KEY}

    try:
        async with session.get(url, params=query, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                return _classify_http_error(resp.status, "finnhub", symbol, body)
            data = await resp.json()

        transactions = data.get("data", [])[:limit]

        # CRITICAL: No insider trades is valid — some companies have very few insiders
        if not transactions:
            return _error_business("finnhub", symbol,
                f"No recent insider transactions found for {symbol}. "
                f"This company may have few reporting insiders or no recent Form 4 filings.")

        # Map transaction codes to readable names
        tx_codes = {
            "P": "Purchase", "S": "Sale", "A": "Grant/Award",
            "D": "Disposition", "F": "Tax withholding", "M": "Option exercise",
            "G": "Gift", "C": "Conversion", "X": "Option expiration",
        }

        return {
            "ok": True,
            "provider": "finnhub",
            "symbol": symbol,
            "transactions": [
                {
                    "name": t.get("name", ""),
                    "shares_held": t.get("share", 0),
                    "shares_changed": t.get("change", 0),
                    "transaction_date": t.get("transactionDate", ""),
                    "transaction_type": tx_codes.get(t.get("transactionCode", ""), t.get("transactionCode", "")),
                    "transaction_code": t.get("transactionCode", ""),
                    "price_per_share": t.get("transactionPrice", 0),
                    "filing_date": t.get("filingDate", ""),
                }
                for t in transactions
            ],
            "count": len(transactions),
        }
    except aiohttp.ClientError as e:
        return _error_transient("finnhub", symbol, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("finnhub", symbol, f"Unexpected error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# TOOL 5: Macro Data (FRED)
# ═════════════════════════════════════════════════════════════════════════════
async def _exec_macro_data(params: dict, session: aiohttp.ClientSession) -> dict:
    series_id = params.get("series_id", "").upper().strip()
    if not series_id:
        return _error_validation("fred", "", "series_id is required. Example: FEDFUNDS, DGS10, CPIAUCSL")

    if not FRED_API_KEY:
        return _error_permission("fred", "FRED_API_KEY not set. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html")

    today = datetime.now().strftime("%Y-%m-%d")
    year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    obs_start = params.get("observation_start", year_ago)
    obs_end = params.get("observation_end", today)

    url = "https://api.stlouisfed.org/fred/series/observations"
    query = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": obs_start,
        "observation_end": obs_end,
        "sort_order": "desc",
        "limit": 12,
    }

    try:
        async with session.get(url, params=query, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                if "Bad Request" in body and series_id:
                    return _error_validation("fred", series_id,
                        f"Series '{series_id}' not found in FRED. Check the ID at https://fred.stlouisfed.org/")
                return _classify_http_error(resp.status, "fred", series_id, body)
            data = await resp.json()

        observations = data.get("observations", [])

        # Filter out missing values (FRED uses "." for missing)
        valid_obs = [o for o in observations if o.get("value", ".") != "."]

        if not valid_obs:
            return _error_business("fred", series_id,
                f"No observations for {series_id} in the requested date range. "
                f"This series may update infrequently (monthly/quarterly).")

        return {
            "ok": True,
            "provider": "fred",
            "series_id": series_id,
            "observations": [
                {"date": o["date"], "value": o["value"]}
                for o in valid_obs
            ],
            "count": len(valid_obs),
            "latest_date": valid_obs[0]["date"] if valid_obs else None,
            "latest_value": valid_obs[0]["value"] if valid_obs else None,
        }
    except aiohttp.ClientError as e:
        return _error_transient("fred", series_id, f"Connection error: {e}")
    except Exception as e:
        return _error_transient("fred", series_id, f"Unexpected error: {e}")
