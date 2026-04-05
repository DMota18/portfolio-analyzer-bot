"""
Launcher — Loads .env file and starts the bot.
Usage: python run.py
"""

import os
import sys
from pathlib import Path


def load_env(env_path=".env"):
    """Load environment variables from .env file."""
    env_file = Path(env_path)
    if not env_file.exists():
        print(f"❌ No .env file found at {env_file.resolve()}")
        print(f"   Copy .env.template to .env and fill in your API keys.")
        sys.exit(1)

    loaded = 0
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if value and key:
                    os.environ[key] = value
                    loaded += 1

    print(f"✅ Loaded {loaded} environment variables from .env")


if __name__ == "__main__":
    load_env()

    # Python 3.14+ removed automatic event loop creation — ensure one exists
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # Now import and run the bot (imports read env vars at module load)
    from bot import main
    main()
