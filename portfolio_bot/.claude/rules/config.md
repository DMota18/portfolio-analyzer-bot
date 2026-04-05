---
globs: ["config.py"]
---

# Config Rules

## System Prompt
- Uses HTML for Telegram, never markdown
- Few-shot examples must use `<b>`, `<i>`, `<a href="">` tags
- Include NULL HANDLING rules for any new data fields
- Include SOURCE ATTRIBUTION requirements for any new data sources

## WORKFLOW_CONFIG
- Maps workflow_hint strings to tool_choice AND model tier
- Haiku for formatting-heavy, low-synthesis tasks (portfolio table)
- Sonnet for synthesis tasks (digest, news analysis, freeform questions)
- Every entry needs both `tool` and `model` keys

## Secrets
- All API keys loaded via `os.getenv()` with empty string defaults
- Never hardcode keys or add defaults that look like real credentials
