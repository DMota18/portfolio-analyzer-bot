# Domain 2 MCP Refactor — Architecture Analysis

## Overview

This document walks through each of the 5 refactor areas, showing what was wrong,
why it matters, and what was changed.

---

## 1. Tool Descriptions — Before vs After

### PROBLEM
The original descriptions were one-liners:
```
"Get current price, daily change, volume, and key metrics for a stock or ETF.
 Use for any real-time equity price lookup."
```
This causes three failure modes:
- **Misrouting**: Claude uses get_stock_quote for Bitcoin because "price lookup" sounds right
- **Wrong inputs**: Claude passes "BTC" to CoinGecko instead of "bitcoin"
- **Unnecessary retries**: Claude retries a tool that returned valid empty results

### FIX
Every tool now includes 6 sections in its description:
1. **PRIMARY PURPOSE** — what the tool does in one sentence
2. **INPUT** — exact format, with explicit examples of correct and incorrect inputs
3. **EXAMPLE QUERIES** — natural language → parameter mapping
4. **EDGE CASES** — what "normal weird" looks like (no news ≠ error)
5. **LIMITATIONS** — what the tool explicitly cannot do
6. **DO NOT USE FOR** — hard boundaries preventing misrouting

The "DO NOT USE FOR" section is critical. It tells Claude:
- `get_stock_quote` → "Do NOT use for cryptocurrency" (prevents BTC misrouting)
- `get_crypto_data` → "Do NOT use for stock prices" (prevents GLD misrouting)
- `get_company_news` → "Do NOT use for insider trades" (prevents filing confusion)

---

## 2. Structured Error Responses

### PROBLEM
The original tools returned flat error dicts:
```python
return {"error": f"Finnhub returned {resp.status}", "provider": "finnhub", "symbol": symbol}
```
This creates two critical issues:
1. **Claude can't distinguish retryable from permanent errors**: A 429 rate limit
   and a 404 not-found both look like `{"error": "..."}` to Claude
2. **Valid empty results treated as failures**: If NVDA has no insider trades in
   the last month, that's information — not an error. But the old code returned
   the same error shape, causing Claude to apologize or retry pointlessly.

### FIX
Four error categories, each with clear semantics:

| Category | ok | is_retryable | Example |
|----------|-----|-------------|---------|
| **transient** | False | True | Rate limited, timeout, 5xx server error |
| **validation** | False | False | Invalid symbol, bad input format |
| **business** | **True** | False | No insider trades exist (valid empty result) |
| **permission** | False | False | Missing API key, expired token |

The `business` category is the key insight. When `ok=True` and `error_category="business"`,
Claude knows: "The request worked, the data is just empty. Report this as information."

In the agent loop, `is_error` on the tool_result is set based on `ok`:
```python
is_tool_error = result.get("ok") is False
```
So business results (ok=True, empty data) do NOT set is_error, and Claude doesn't
treat them as failures.

### HTTP status mapping:
```
401, 403 → permission (don't retry, check API key)
404      → business (symbol not found — inform user, don't retry)
422, 400 → validation (bad input — fix the request)
429      → transient (rate limited — retry after delay)
500+     → transient (server issue — retry)
```

---

## 3. Tool Distribution

### PROBLEM
The original `get_company_filings` tool was a 3-in-1 multiplexer:
```python
"filing_type": {"enum": ["insider_trades", "recent_filings", "company_news"]}
```
This violates single-responsibility: Claude has to choose the right `filing_type`
sub-parameter, and the tool internally routes to completely different APIs
(Finnhub insider endpoint vs Finnhub news endpoint vs SEC EDGAR).

Misrouting was common — Claude would call `get_company_filings` with
`filing_type="company_news"` when the user asked about insider trades,
because the tool name contains "filings."

### FIX
Split into 3 purpose-specific tools, then consolidated to 5 total:

| # | Tool | Provider | Responsibility |
|---|------|----------|---------------|
| 1 | `get_stock_quote` | Financial Datasets → Yahoo → Finnhub | Equity/ETF prices |
| 2 | `get_crypto_data` | CoinGecko | Crypto prices |
| 3 | `get_company_news` | Finnhub | News articles/headlines |
| 4 | `get_insider_trades` | Finnhub (SEC Form 4) | Insider buying/selling |
| 5 | `get_macro_data` | FRED | Economic indicators |

5 tools is within the 4-5 per agent role guideline. Each tool has one job,
one provider chain, and one return shape. No internal multiplexing.

The old `recent_filings` sub-type was removed because it was the least-used
and overlapped with what `get_company_news` covers (8-K filings = news events).
Can be re-added as a 6th tool if needed.

---

## 4. tool_choice Configuration

### PROBLEM
Two workflows have mandatory first steps that Claude sometimes skips:

1. `/portfolio` — Claude MUST call `get_stock_quote` for each holding to show live prices.
   Without enforcement, Claude sometimes tries to answer from memory: "NVDA is trading
   around $188" (stale data from training).

2. `/macro` — Claude MUST call `get_macro_data` for FRED data. It cannot guess the
   current fed funds rate.

The system prompt says "ALWAYS call get_stock_quote" but prompt instructions are
advisory, not enforceable.

### FIX
Added `TOOL_CHOICE_OVERRIDES` in config:
```python
TOOL_CHOICE_OVERRIDES = {
    "portfolio": "get_stock_quote",
    "macro": "get_macro_data",
}
```

The agent loop detects these triggers and sets `tool_choice={"type":"tool","name":...}`
on the **first iteration only**, then reverts to `tool_choice={"type":"auto"}` for
subsequent iterations. This means:

- First call: Claude is FORCED to use the specified tool
- Second call onwards: Claude has full autonomy to call any tool or respond

This pattern ensures the mandatory first step happens while preserving Claude's
ability to chain additional tool calls as needed.

---

## 5. MCP Configuration

### .mcp.json
The config file uses environment variable expansion for all credentials:
```json
"env": {
    "FINNHUB_KEY": "${FINNHUB_KEY}",
    "FIN_DATASETS_KEY": "${FIN_DATASETS_KEY}",
    ...
}
```

### Community MCP Servers Assessment

| Provider | Community Server? | Recommendation | Reason |
|----------|-------------------|---------------|--------|
| FRED | Yes (mcp-server-fred) | Keep custom | Structured errors needed |
| CoinGecko | Yes | Keep custom | Rate limit handling + error categories |
| Financial Datasets | **No** | Must be custom | **Trailing slash requirement** breaks standard OpenAPI MCP servers |
| Finnhub | Yes | Keep custom | Free tier rate limits need custom handling |
| Yahoo Finance | No official | Keep custom | Unofficial API, needs User-Agent management |

**Key insight**: Financial Datasets API requires trailing slashes on all endpoint URLs
(`/quote/NVDA/` not `/quote/NVDA`). This is non-standard and would break any
community MCP server that auto-generates URLs from OpenAPI specs. This alone
necessitates a custom implementation.

The structured error response system we built also doesn't exist in any community
MCP server — they all return raw provider errors. Since our agent loop depends on
`ok`, `error_category`, and `is_retryable` fields, we'd need wrapper logic around
any community server anyway, negating the benefit.

**Recommendation**: Keep all 5 tools as custom implementations. Revisit when the
MCP ecosystem standardizes error handling.

---

## File Changes Summary

| File | What Changed |
|------|-------------|
| `tools.py` | Complete rewrite — production descriptions, structured errors, tool split, Financial Datasets support |
| `config.py` | Added FIN_DATASETS_KEY, TOOL_CHOICE_OVERRIDES, updated system prompt with tool usage rules |
| `agent_loop.py` | Added tool_choice enforcement, structured error awareness for is_error flag |
| `hooks.py` | Removed PostToolUse normalizer (tools now self-normalize), kept interception |
| `.mcp.json` | New — MCP config with env var expansion and community server assessment |
