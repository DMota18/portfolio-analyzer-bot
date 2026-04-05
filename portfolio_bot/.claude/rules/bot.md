---
globs: ["bot.py"]
---

# Bot Command Rules

## Adding New Commands
1. Create `async def cmd_name(update, context)` with `is_authorized()` check
2. Register with `app.add_handler(CommandHandler("name", cmd_name))`
3. Add to `/start` help text
4. If the command needs live data, pass `workflow_hint=` to `send_to_agent()`

## Agent Calls
- Always pass `workflow_hint=` explicitly when a tool_choice override applies
- Never rely on prompt text to trigger tool_choice — use the hint parameter
- For digest/portfolio commands, use workflow_hint="portfolio" to force batch quotes

## Persistence
- Call `save_data(bot_data)` after any mutation to bot_data
- Call `save_facts(portfolio_facts)` after any mutation to portfolio_facts
- Use `global portfolio_facts` when reassigning the facts dict
