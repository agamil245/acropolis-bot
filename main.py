#!/usr/bin/env python3
"""
AcropolisBot - Advanced Polymarket Trading Bot
Main entry point: starts both the trading bot AND web dashboard.
"""

import asyncio
import sys
import threading
import os

# Force unbuffered output so we see prints immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import uvicorn

from src.bot_engine import BotEngine
from src.config import Config


def run_web_server(bot_engine: BotEngine):
    """Run the FastAPI web server in a separate thread."""
    # Inject bot into the web server module
    import src.web.server as web
    web.bot = bot_engine

    uvicorn.run(
        "src.web.server:app",
        host=Config.WEB_HOST,
        port=Config.WEB_PORT,
        reload=False,
        log_level="warning",  # Quiet — bot logs are enough
    )


async def main():
    """Main entry point."""
    print("""
    ╔══════════════════════════════════════╗
    ║       AcropolisBot v1.0.0            ║
    ║  Advanced Polymarket Trading Bot      ║
    ╚══════════════════════════════════════╝
    """)

    # Initialize bot
    bot = BotEngine()

    # Start web server in background thread
    web_thread = threading.Thread(target=run_web_server, args=(bot,), daemon=True)
    web_thread.start()
    print(f"\n🌐 Dashboard → http://{Config.WEB_HOST}:{Config.WEB_PORT}\n")

    try:
        await bot.start()
    except KeyboardInterrupt:
        print("\n[MAIN] Received interrupt signal...")
        await bot.stop()
    except Exception as e:
        print(f"\n[MAIN] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Shutdown complete. Goodbye!")
