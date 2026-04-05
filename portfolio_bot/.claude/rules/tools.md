---
globs: ["tools.py"]
---

# Tool Implementation Rules

Every tool function in this file must follow these conventions:

## Structured Error Responses
- Use `_error_transient()` for timeouts, rate limits (429), server errors (5xx)
- Use `_error_validation()` for bad input (wrong symbol format, missing fields)
- Use `_error_business()` for valid empty results (no insider trades, delisted symbol) — set ok=True
- Use `_error_permission()` for missing API keys, 401/403
- Use `_classify_http_error()` to map HTTP status codes automatically
- Never raise exceptions for expected API failures — return structured errors instead

## Response Schema
Every successful response must include:
- `ok: True`
- `provider: str` — which data source served this response
- `symbol: str` — the requested symbol/identifier

Every error response must include:
- `ok: bool` — False for real errors, True for business (valid empty)
- `error_category: str` — one of: transient, validation, business, permission
- `is_retryable: bool`
- `provider: str`
- `message: str`

## Provider Chain
For stock quotes, the provider chain is: Financial Datasets → Yahoo Finance (fallback).
When falling back, call `record_fallback()` from provider_stats.py.

## Financial Datasets API
URLs MUST have a trailing slash: `.../quote/{symbol}/`
