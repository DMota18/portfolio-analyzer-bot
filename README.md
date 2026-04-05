# Portfolio Analyzer Bot

Telegram-based AI portfolio assistant. Claude Sonnet synthesizes data from multiple financial APIs into actionable intelligence delivered via Telegram.

## Screenshots

<p align="center">
  <img src="screenshots/commands.png" alt="Bot Commands" width="300"/>
  <img src="screenshots/daily-digest.png" alt="Daily Digest" width="300"/>
  <img src="screenshots/fear-greed.png" alt="Fear & Greed Dashboard" width="300"/>
</p>

## Features

- **Portfolio tracking** — Live prices, daily P&L, and total returns for all positions
- **Daily digest** — Automated portfolio summary with news and macro context
- **Fear & Greed dashboard** — Stock and crypto sentiment indicators plus VIX
- **Company news** — Headlines and AI-generated sentiment analysis per ticker
- **Insider trades** — SEC Form 4 data with buy/sell signal interpretation
- **Macro snapshot** — Fed Funds, 10Y Treasury, CPI from FRED
- **Price alerts** — Set above/below price triggers for any ticker

## Architecture

```
bot.py → agent_loop.py → tools.py → External APIs
           ↕                ↕
        hooks.py      provider_stats.py
           ↕
    portfolio_facts.py
```

- **bot.py** — Telegram command handlers, scheduled jobs, persistence
- **agent_loop.py** — Agentic loop: Claude API calls, tool_choice enforcement, circuit breaker
- **tools.py** — Tool definitions + provider implementations (Financial Datasets, Yahoo, Finnhub, CoinGecko, FRED)
- **hooks.py** — Pre-execution safety gates (position size, irreversible actions)
- **portfolio_facts.py** — Persistent per-ticker state (cost basis, shares, digest history)
- **provider_stats.py** — API reliability tracking per provider per day
- **config.py** — Secrets, constants, system prompt, workflow config
- **public_api.py** — Public.com two-step auth flow

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your API keys
3. Install dependencies:
   ```bash
   pip install -r portfolio_bot/requirements.txt
   ```
4. Run the bot:
   ```bash
   python portfolio_bot/run.py
   ```

## Data Sources

| Provider | Data |
|----------|------|
| Financial Datasets | Stock quotes (primary) |
| Yahoo Finance | Stock quotes (fallback) |
| Finnhub | Company news, insider trades |
| CoinGecko | Crypto prices |
| FRED | Macroeconomic indicators |
| Public.com | Portfolio holdings sync |
