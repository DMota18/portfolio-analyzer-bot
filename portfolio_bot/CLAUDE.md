# Portfolio Intelligence Bot

Telegram-based AI portfolio assistant. Claude Sonnet synthesizes data from 7 financial APIs into actionable intelligence delivered via Telegram.

## Architecture

```
bot.py → agent_loop.py → tools.py → External APIs
           ↕                ↕
        hooks.py      provider_stats.py
           ↕
    portfolio_facts.py
```

- **bot.py** — Telegram command handlers, scheduled jobs, persistence (bot_data.json)
- **agent_loop.py** — Agentic loop: Claude API calls, tool_choice enforcement, circuit breaker, validation
- **tools.py** — 6 tool definitions + provider implementations (Financial Datasets, Yahoo, Finnhub, CoinGecko, FRED)
- **hooks.py** — Pre-execution safety gates (position size, irreversible actions)
- **portfolio_facts.py** — Persistent per-ticker state (cost basis, shares, digest history)
- **provider_stats.py** — API reliability tracking per provider per day
- **config.py** — Secrets, constants, system prompt, workflow config
- **public_api.py** — Public.com two-step auth flow

## Key Conventions

### Error Contract
All tools return structured error responses using one of four categories:
- `_error_transient(provider, symbol, detail)` — retryable (timeout, 429, 5xx)
- `_error_validation(provider, symbol, detail)` — bad input, do NOT retry
- `_error_business(provider, symbol, detail)` — valid empty result (ok=True), do NOT retry
- `_error_permission(provider, detail)` — auth failure, do NOT retry

Every tool response MUST include `ok`, `provider`, and either data or error fields. HTTP status codes are mapped via `_classify_http_error()`.

### Financial Datasets API
URLs require a **trailing slash**: `https://api.financialdatasets.ai/financial-data/quote/{symbol}/`
Omitting the trailing slash returns a redirect that breaks the response.

### Output Format
Telegram uses **HTML parse mode**, not markdown. Use `<b>`, `<i>`, `<a href="url">`. Never use `**bold**` or `[link](url)` in the system prompt or tool outputs.

### Tool Choice
The `TOOL_CHOICE_OVERRIDES` dict in config.py maps workflow hints to mandatory first tools. Tool choice is triggered by explicit `workflow_hint` parameter, NOT substring matching. All callers in bot.py must pass `workflow_hint=` explicitly.

### Model Routing
`WORKFLOW_CONFIG` in config.py maps workflow hints to both tool_choice and model tier. Haiku for formatting-only tasks (portfolio), Sonnet for synthesis (digest, ask, news). The agent loop resolves model from workflow_hint.

### Provider Stats
Every tool call outcome (success/failure) is recorded via `provider_stats.record_call()` in agent_loop.py. Fallbacks are recorded via `provider_stats.record_fallback()` in tools.py.

### Persistence Files
- `bot_data.json` — portfolio symbols, watchlist, capital, last digest timestamp
- `data/portfolio_facts.json` — per-ticker cost basis, shares, digest history
- `data/provider_stats.json` — daily API reliability metrics (auto-pruned to 30 days)
- `data/alerts.json` — price alert definitions (not yet wired into bot.py)
- `data/holdings.json` — raw share quantities from Public.com

## CI / Non-Interactive Mode
If running Claude Code in CI or automation, use the `-p` flag to prevent interactive prompts from hanging the process.

## Do NOT
- Commit `.env` — contains 8 API keys
- Hardcode API keys — always use `os.getenv()` via config.py
- Use markdown in system prompt or tool outputs — Telegram is HTML only
- Add tools without structured error responses — follow the 4-category contract
- Use substring matching for tool_choice — always use explicit workflow_hint
