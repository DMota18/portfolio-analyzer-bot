"""
Portfolio Facts — Persistent per-ticker state that survives across sessions.

Stores cost basis, share counts, and digest history so Claude can say
"NVDA is up 18% since you bought it" instead of just "NVDA is up 2.3% today."

Facts file: data/portfolio_facts.json
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("agent.facts")

FACTS_FILE = os.path.join("data", "portfolio_facts.json")


def load_facts() -> dict:
    """Load portfolio facts from disk."""
    if os.path.exists(FACTS_FILE):
        try:
            with open(FACTS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load facts: {e}")
    return {}


def save_facts(facts: dict):
    """Write portfolio facts to disk."""
    os.makedirs(os.path.dirname(FACTS_FILE), exist_ok=True)
    with open(FACTS_FILE, "w") as f:
        json.dump(facts, f, indent=2)


def get_fact(facts: dict, symbol: str) -> dict:
    """Get facts for a single symbol, returning defaults if missing."""
    return facts.get(symbol.upper(), {})


def set_cost_basis(facts: dict, symbol: str, cost_basis: float, shares: float) -> dict:
    """Set or update cost basis and share count for a symbol."""
    symbol = symbol.upper()
    if symbol not in facts:
        facts[symbol] = {}
    facts[symbol]["cost_basis"] = cost_basis
    facts[symbol]["shares"] = shares
    facts[symbol]["total_invested"] = round(cost_basis * shares, 2)
    facts[symbol]["cost_basis_updated"] = datetime.now().strftime("%Y-%m-%d")
    save_facts(facts)
    return facts


def update_from_sync(facts: dict, positions: list, holdings_data: dict) -> dict:
    """
    Merge share quantities from Public.com sync and holdings.json into facts.
    Preserves manually-set cost basis — only updates shares if changed.
    """
    # Update from holdings.json (has qty data)
    for asset_type in ["stocks", "crypto"]:
        for symbol, data in holdings_data.get(asset_type, {}).items():
            symbol = symbol.upper()
            if symbol not in facts:
                facts[symbol] = {}
            qty = data.get("qty", 0)
            if qty and qty != facts[symbol].get("shares"):
                facts[symbol]["shares"] = qty
                facts[symbol]["name"] = data.get("name", symbol)
                facts[symbol]["asset_type"] = asset_type
                # Recalc total_invested if cost basis exists
                if "cost_basis" in facts[symbol]:
                    facts[symbol]["total_invested"] = round(
                        facts[symbol]["cost_basis"] * qty, 2
                    )

    # Ensure all synced portfolio symbols have an entry
    for pos in positions:
        sym = pos.get("symbol", "").upper()
        if sym and sym not in facts:
            facts[sym] = {
                "name": pos.get("name", sym),
                "asset_type": pos.get("type", "EQUITY").lower(),
            }

    save_facts(facts)
    return facts


def record_digest(facts: dict, symbol: str, price: float, change_pct: float = None) -> dict:
    """Record a price snapshot from a digest for tracking over time."""
    symbol = symbol.upper()
    if symbol not in facts:
        facts[symbol] = {}

    today = datetime.now().strftime("%Y-%m-%d")

    facts[symbol]["last_digest_price"] = price
    facts[symbol]["last_digest_date"] = today

    if change_pct is not None:
        facts[symbol]["last_digest_change_pct"] = change_pct

    # Track consecutive digest mentions
    count = facts[symbol].get("digest_mention_count", 0)
    facts[symbol]["digest_mention_count"] = count + 1

    # Save is deferred — caller should batch and save once
    return facts


def format_facts_for_context(facts: dict) -> str:
    """
    Format facts into a concise string for injection into the agent prompt.
    Only includes symbols that have meaningful data (cost basis or shares).
    """
    lines = []
    for symbol, data in sorted(facts.items()):
        parts = []

        shares = data.get("shares")
        if shares:
            parts.append(f"{shares} shares")

        cost = data.get("cost_basis")
        if cost:
            parts.append(f"avg cost ${cost:.2f}")
            total = data.get("total_invested")
            if total:
                parts.append(f"invested ${total:,.2f}")

        last_price = data.get("last_digest_price")
        last_date = data.get("last_digest_date")
        if last_price and cost and shares:
            gain = (last_price - cost) * shares
            gain_pct = ((last_price - cost) / cost) * 100
            parts.append(f"last ${last_price:.2f} on {last_date}")
            parts.append(f"P&L {'+'if gain>=0 else ''}${gain:,.2f} ({'+'if gain_pct>=0 else ''}{gain_pct:.1f}%)")

        if parts:
            lines.append(f"  {symbol}: {' | '.join(parts)}")

    if not lines:
        return "No cost basis or position data available. User can set via /setcost TICKER PRICE SHARES."

    return "\n".join(lines)
