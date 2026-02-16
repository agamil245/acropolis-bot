#!/usr/bin/env python3
"""
AcropolisBot - Advanced Polymarket Trading Bot
Main entry point for starting the bot
"""

import asyncio
import sys

from src.bot_engine import BotEngine


async def main():
    """Main entry point."""
    print("""
    ╔══════════════════════════════════════╗
    ║       AcropolisBot v1.0.0            ║
    ║  Advanced Polymarket Trading Bot      ║
    ╚══════════════════════════════════════╝
    """)
    
    # Initialize and start bot
    bot = BotEngine()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        print("\n[MAIN] Received interrupt signal...")
        await bot.stop()
    except Exception as e:
        print(f"\n[MAIN] Fatal error: {e}")
        await bot.stop()
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] Shutdown complete. Goodbye!")
