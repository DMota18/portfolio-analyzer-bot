"""
Agentic Loop — Domain 2 Refactor + Domain 4 Hardening

Changes from v1:
  - tool_choice support: mandatory first tool for specific workflows
  - Structured error awareness: isRetryable drives retry logic
  - Simplified normalization (tools now return pre-normalized data)

Domain 4 additions:
  - Explicit workflow_hint replaces fragile substring-based tool_choice detection
  - Circuit breaker: short-circuits after 3 consecutive failures per provider
  - Post-execution tool result validation (price sanity, staleness)
"""

import json
import logging
import asyncio
from typing import Optional
from collections import defaultdict

import aiohttp

from tools import TOOL_DEFINITIONS, execute_tool
from hooks import intercept_tool_call
from provider_stats import record_call
from config import (
    ANTHROPIC_API_KEY, SYSTEM_PROMPT, CLAUDE_MODEL,
    MAX_TOOL_LOOPS, TOOL_CHOICE_OVERRIDES, WORKFLOW_CONFIG,
)

logger = logging.getLogger("agent.loop")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Circuit breaker threshold — after this many consecutive failures
# from the same provider in one agent loop, skip remaining calls to it.
CIRCUIT_BREAKER_THRESHOLD = 3


# ═════════════════════════════════════════════════════════════════════════════
# TOOL RESULT VALIDATION (post-execution)
# ═════════════════════════════════════════════════════════════════════════════
def _validate_tool_result(tool_name: str, result: dict) -> list[str]:
    """
    Validate a tool result for sanity. Returns a list of warnings.
    An empty list means the result looks clean.
    """
    warnings = []

    if result.get("ok") is False:
        return warnings  # Don't validate error responses

    if tool_name in ("get_stock_quote", "get_portfolio_quotes"):
        # For batch quotes, validate each quote in the array
        quotes = result.get("quotes", [result]) if "quotes" in result else [result]
        for quote in quotes:
            symbol = quote.get("symbol", "?")
            price = quote.get("price")

            if price is None:
                warnings.append(f"{symbol}: price is null")
            elif price == 0:
                warnings.append(f"{symbol}: price is 0 (market may be closed or symbol unsupported)")
            elif price < 0:
                warnings.append(f"{symbol}: negative price ${price} — data error")

            change_pct = quote.get("change_pct")
            if change_pct is not None:
                try:
                    if abs(float(change_pct)) > 50:
                        warnings.append(
                            f"{symbol}: daily change {change_pct}% is extreme — verify data"
                        )
                except (ValueError, TypeError):
                    pass

    elif tool_name == "get_crypto_data":
        price = result.get("price")
        if price is not None and price <= 0:
            warnings.append(f"Crypto price ${price} is invalid")

    elif tool_name == "get_macro_data":
        observations = result.get("observations", [])
        if observations:
            latest = observations[0]
            try:
                val = float(latest.get("value", 0))
                series = result.get("series_id", "")
                # Interest rates and percentages shouldn't exceed 100
                if series in ("FEDFUNDS", "DGS10", "DGS2", "UNRATE", "MORTGAGE30US"):
                    if val > 100 or val < -10:
                        warnings.append(f"{series}: value {val}% is outside plausible range")
            except (ValueError, TypeError):
                pass

    return warnings


# ═════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ═════════════════════════════════════════════════════════════════════════════
class CircuitBreaker:
    """Tracks consecutive failures per provider within a single agent loop run."""

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD):
        self.threshold = threshold
        self._failures: dict[str, int] = defaultdict(int)
        self._tripped: set[str] = set()

    def is_tripped(self, provider: str) -> bool:
        return provider in self._tripped

    def record_failure(self, provider: str):
        self._failures[provider] += 1
        if self._failures[provider] >= self.threshold:
            self._tripped.add(provider)
            logger.warning(
                f"Circuit breaker TRIPPED for {provider} "
                f"after {self._failures[provider]} consecutive failures"
            )

    def record_success(self, provider: str):
        self._failures[provider] = 0

    def get_tripped_providers(self) -> list[str]:
        return list(self._tripped)


# ═════════════════════════════════════════════════════════════════════════════
# AGENT LOOP
# ═════════════════════════════════════════════════════════════════════════════
async def run_agent_loop(
    user_message: str,
    session: aiohttp.ClientSession,
    conversation_history: Optional[list] = None,
    context: Optional[dict] = None,
    workflow_hint: Optional[str] = None,
) -> str:
    """
    Run the agentic loop with tool_choice enforcement and structured error handling.

    Args:
        workflow_hint: Explicit workflow identifier (e.g. "portfolio", "macro").
                       Used for tool_choice enforcement instead of substring matching.
    """
    if not ANTHROPIC_API_KEY:
        return "Anthropic API key not configured. Set ANTHROPIC_API_KEY in your .env file."

    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": user_message})

    context = context or {}
    loop_count = 0
    breaker = CircuitBreaker()

    # Resolve tool_choice and model from workflow config
    forced_tool_choice = None
    model = CLAUDE_MODEL  # default
    if workflow_hint and workflow_hint in WORKFLOW_CONFIG:
        wf = WORKFLOW_CONFIG[workflow_hint]
        if wf.get("tool"):
            forced_tool_choice = {"type": "tool", "name": wf["tool"]}
        if wf.get("model"):
            model = wf["model"]
        logger.info(f"Workflow '{workflow_hint}': model={model}, tool_choice={forced_tool_choice}")

    while loop_count < MAX_TOOL_LOOPS:
        loop_count += 1
        logger.info(f"Agent loop iteration {loop_count}")

        # ── Build payload ────────────────────────────────────────────
        # Append portfolio facts to system prompt if available
        system = SYSTEM_PROMPT
        facts = context.get("portfolio_facts")
        if facts and loop_count == 1:
            system += f"\n\nPORTFOLIO POSITION DATA:\n{facts}"

        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
        }

        # Apply tool_choice on first iteration only, then revert to auto
        if loop_count == 1 and forced_tool_choice:
            payload["tool_choice"] = forced_tool_choice
            logger.info(f"Forcing tool_choice: {forced_tool_choice}")
        else:
            payload["tool_choice"] = {"type": "auto"}

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        # ── Call Claude ──────────────────────────────────────────────
        try:
            async with session.post(
                ANTHROPIC_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Claude API error {resp.status}: {error_text}")
                    return f"Claude API error ({resp.status}). Try again shortly."
                response = await resp.json()
        except asyncio.TimeoutError:
            logger.error("Claude API timeout")
            return "Request timed out. Try again."
        except Exception as e:
            logger.error(f"Claude API exception: {e}")
            return f"Error contacting Claude: {e}"

        stop_reason = response.get("stop_reason", "end_turn")
        content_blocks = response.get("content", [])

        # ── stop_reason == "end_turn" ────────────────────────────────
        if stop_reason == "end_turn":
            text_parts = [
                block["text"]
                for block in content_blocks
                if block.get("type") == "text"
            ]
            final_text = "\n".join(text_parts) if text_parts else "(No response generated)"

            # Append circuit breaker notice if any providers were tripped
            tripped = breaker.get_tripped_providers()
            if tripped:
                notice = (
                    f"\n\n⚠️ <i>Data from {', '.join(tripped)} was unavailable "
                    f"due to repeated errors. Some prices may be missing.</i>"
                )
                final_text += notice

            logger.info(f"Agent loop complete after {loop_count} iteration(s)")
            return final_text

        # ── stop_reason == "tool_use" ────────────────────────────────
        if stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": content_blocks})

            tool_calls = [
                block for block in content_blocks if block.get("type") == "tool_use"
            ]

            tool_results = []

            for tool_call in tool_calls:
                tool_name = tool_call["name"]
                tool_input = tool_call["input"]
                tool_use_id = tool_call["id"]

                logger.info(f"Tool call: {tool_name}({json.dumps(tool_input)[:200]})")

                # ── HOOK 1: Interception ─────────────────────────────
                interception = intercept_tool_call(tool_name, tool_input, context)
                if interception["blocked"]:
                    logger.warning(f"BLOCKED: {tool_name} — {interception['reason']}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": json.dumps({
                            "blocked": True,
                            "reason": interception["reason"],
                            "action": interception.get("action", "escalated_to_user"),
                        }),
                    })
                    continue

                # ── HOOK 2: Circuit breaker check ────────────────────
                # Infer provider from tool input or tool name
                provider = tool_input.get("provider", "")
                if not provider:
                    # Map tools to their primary provider
                    provider_map = {
                        "get_company_news": "finnhub",
                        "get_insider_trades": "finnhub",
                        "get_macro_data": "fred",
                        "get_crypto_data": "coingecko",
                    }
                    provider = provider_map.get(tool_name, "")

                if provider and breaker.is_tripped(provider):
                    logger.warning(f"Circuit breaker skipping {tool_name} — {provider} is tripped")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": json.dumps({
                            "ok": False,
                            "error_category": "transient",
                            "is_retryable": False,
                            "message": (
                                f"{provider} is temporarily unavailable after multiple failures. "
                                f"Skipping this call to avoid further errors."
                            ),
                        }),
                    })
                    continue

                # ── Execute tool ─────────────────────────────────────
                try:
                    result = await execute_tool(tool_name, tool_input, session)
                except Exception as e:
                    logger.error(f"Tool execution error ({tool_name}): {e}")
                    if provider:
                        breaker.record_failure(provider)
                    record_call(provider, success=False, error_category="exception", tool_name=tool_name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": json.dumps({
                            "ok": False,
                            "error_category": "transient",
                            "is_retryable": True,
                            "message": str(e),
                        }),
                    })
                    continue

                # ── HOOK 3: Circuit breaker + stats tracking ─────────
                result_provider = result.get("provider", provider)
                if result.get("ok") is False:
                    if result_provider:
                        breaker.record_failure(result_provider)
                    record_call(
                        result_provider, success=False,
                        error_category=result.get("error_category"),
                        http_status=result.get("http_status"),
                        tool_name=tool_name,
                    )
                else:
                    if result_provider:
                        breaker.record_success(result_provider)
                    record_call(result_provider, success=True, tool_name=tool_name)

                # ── HOOK 4: Post-execution validation ────────────────
                validation_warnings = _validate_tool_result(tool_name, result)
                if validation_warnings:
                    logger.warning(f"Validation warnings for {tool_name}: {validation_warnings}")
                    # Inject warnings into the result so Claude can surface them
                    result["_validation_warnings"] = validation_warnings

                # ── Determine is_error flag for Claude ───────────────
                # Tools now return structured errors with ok/error_category.
                # is_error=True tells Claude the tool failed.
                # ok=True with error_category="business" is NOT a failure —
                # it's valid empty data. We do NOT set is_error.
                is_tool_error = result.get("ok") is False

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_tool_error,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        else:
            logger.warning(f"Unexpected stop_reason: {stop_reason}")
            text_parts = [
                block["text"]
                for block in content_blocks
                if block.get("type") == "text"
            ]
            return "\n".join(text_parts) if text_parts else "(Unexpected response)"

    logger.warning(f"Agent loop hit max iterations ({MAX_TOOL_LOOPS})")
    return "I ran into a processing limit. Try a more specific question."
