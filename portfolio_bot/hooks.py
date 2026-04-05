"""
Hooks — Tool Call Interception (pre-execution safety gate)

Domain 2 Refactor: PostToolUse normalization removed.
Tools now return pre-normalized data with structured error responses.
The normalizer was creating a second transformation layer that obscured
the original field names tools were returning — making debugging harder
and adding complexity without benefit now that tools output consistent schemas.
"""

import logging
from config import MAX_POSITION_SIZE_PCT, IRREVERSIBLE_TOOLS

logger = logging.getLogger("agent.hooks")


def intercept_tool_call(tool_name: str, tool_input: dict, context: dict) -> dict:
    """
    Inspect a tool call before execution. Returns:
      {"blocked": False} → safe to proceed
      {"blocked": True, "reason": "...", "action": "..."} → blocked
    """

    # ── Rule A: Position size check ──────────────────────────────────
    if tool_name == "execute_trade":
        available_capital = context.get("available_capital", 0)
        trade_amount = tool_input.get("amount", 0) or tool_input.get("notional_value", 0)

        if available_capital > 0 and trade_amount > 0:
            max_allowed = available_capital * MAX_POSITION_SIZE_PCT
            if trade_amount > max_allowed:
                return {
                    "blocked": True,
                    "reason": (
                        f"Position size ${trade_amount:,.2f} exceeds {MAX_POSITION_SIZE_PCT*100:.0f}% limit. "
                        f"Available capital: ${available_capital:,.2f}. "
                        f"Max allowed: ${max_allowed:,.2f}."
                    ),
                    "action": "escalated_to_user",
                }

    # ── Rule B: Irreversible action check ────────────────────────────
    if tool_name in IRREVERSIBLE_TOOLS:
        rules = IRREVERSIBLE_TOOLS[tool_name]
        trade_amount = tool_input.get("amount", 0) or tool_input.get("notional_value", 0)
        confirm_threshold = rules.get("confirm_above_usd", 0)

        if trade_amount > confirm_threshold:
            return {
                "blocked": True,
                "reason": (
                    f"Irreversible action '{tool_name}' (${trade_amount:,.2f}) "
                    f"requires user confirmation (threshold: ${confirm_threshold:,.2f})."
                ),
                "action": "awaiting_user_confirmation",
            }

    return {"blocked": False}
