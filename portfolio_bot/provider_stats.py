"""
Provider Stats — Track API reliability per provider per day.

Logs success/failure counts and error categories so you can see which
providers are actually serving data and which are silently failing.

Stats file: data/provider_stats.json
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("agent.stats")

STATS_FILE = os.path.join("data", "provider_stats.json")


def _load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_stats(stats: dict):
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    # Keep only last 30 days of data
    dates = sorted(stats.keys())
    if len(dates) > 30:
        for old_date in dates[:-30]:
            del stats[old_date]
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def record_call(provider: str, success: bool, error_category: str = None,
                http_status: int = None, tool_name: str = None):
    """Record a single API call result."""
    if not provider:
        return

    stats = _load_stats()
    today = datetime.now().strftime("%Y-%m-%d")

    if today not in stats:
        stats[today] = {}
    if provider not in stats[today]:
        stats[today][provider] = {
            "success": 0,
            "failure": 0,
            "errors": {},
            "tools": {},
        }

    entry = stats[today][provider]

    if success:
        entry["success"] += 1
    else:
        entry["failure"] += 1
        # Track error categories
        cat = error_category or "unknown"
        entry["errors"][cat] = entry["errors"].get(cat, 0) + 1
        # Track HTTP status codes
        if http_status:
            status_key = str(http_status)
            entry["errors"][status_key] = entry["errors"].get(status_key, 0) + 1

    # Track which tools used this provider
    if tool_name:
        entry["tools"][tool_name] = entry["tools"].get(tool_name, 0) + 1

    _save_stats(stats)


def record_fallback(from_provider: str, to_provider: str, symbol: str):
    """Record when a provider fallback is triggered."""
    stats = _load_stats()
    today = datetime.now().strftime("%Y-%m-%d")

    if today not in stats:
        stats[today] = {}

    fallback_key = "_fallbacks"
    if fallback_key not in stats[today]:
        stats[today][fallback_key] = []

    stats[today][fallback_key].append({
        "from": from_provider,
        "to": to_provider,
        "symbol": symbol,
    })

    _save_stats(stats)


def get_today_summary() -> dict:
    """Get today's provider stats summary."""
    stats = _load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    return stats.get(today, {})


def format_status_report() -> str:
    """Format provider stats for the /status command."""
    summary = get_today_summary()

    if not summary:
        return "No API calls recorded today."

    lines = []
    fallbacks = summary.pop("_fallbacks", [])

    for provider, data in sorted(summary.items()):
        success = data["success"]
        failure = data["failure"]
        total = success + failure
        if total == 0:
            continue

        pct = (success / total) * 100
        icon = "✅" if pct >= 90 else "⚠️" if pct >= 50 else "❌"

        line = f"  {icon} {provider}: {success}/{total} ({pct:.0f}%)"

        # Add error breakdown if any failures
        errors = data.get("errors", {})
        if errors:
            # Show top error categories (skip HTTP status codes for brevity)
            cats = {k: v for k, v in errors.items() if not k.isdigit()}
            if cats:
                error_parts = [f"{v} {k}" for k, v in cats.items()]
                line += f" — {', '.join(error_parts)}"

        lines.append(line)

    if fallbacks:
        fallback_count = len(fallbacks)
        lines.append(f"\n  🔄 Provider fallbacks today: {fallback_count}")

    return "\n".join(lines) if lines else "No API calls recorded today."


def format_weekly_report() -> str:
    """Format last 7 days of provider stats for deeper analysis."""
    stats = _load_stats()
    dates = sorted(stats.keys())[-7:]

    if not dates:
        return "No stats available."

    # Aggregate across days
    totals: dict[str, dict] = {}
    total_fallbacks = 0

    for date in dates:
        day_data = stats[date]
        fallbacks = day_data.get("_fallbacks", [])
        total_fallbacks += len(fallbacks)

        for provider, data in day_data.items():
            if provider == "_fallbacks":
                continue
            if provider not in totals:
                totals[provider] = {"success": 0, "failure": 0}
            totals[provider]["success"] += data.get("success", 0)
            totals[provider]["failure"] += data.get("failure", 0)

    lines = [f"<b>📊 7-Day Provider Report</b> ({dates[0]} to {dates[-1]})\n"]
    for provider, data in sorted(totals.items()):
        s = data["success"]
        f = data["failure"]
        t = s + f
        pct = (s / t * 100) if t > 0 else 0
        icon = "✅" if pct >= 90 else "⚠️" if pct >= 50 else "❌"
        lines.append(f"  {icon} {provider}: {s}/{t} ({pct:.0f}%)")

    if total_fallbacks:
        lines.append(f"\n  🔄 Total fallbacks: {total_fallbacks}")

    return "\n".join(lines)
