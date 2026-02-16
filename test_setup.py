#!/usr/bin/env python3
"""
Test script to verify AcropolisBot setup
Run this to check if everything is installed correctly
"""

import sys

def test_imports():
    """Test all required imports."""
    print("Testing imports...")
    try:
        import requests
        print("✓ requests")
        
        import websockets
        print("✓ websockets")
        
        import fastapi
        print("✓ fastapi")
        
        import uvicorn
        print("✓ uvicorn")
        
        from dotenv import load_dotenv
        print("✓ python-dotenv")
        
        return True
    except ImportError as e:
        print(f"✗ Import error: {e}")
        print("\nRun: uv sync")
        return False


def test_config():
    """Test configuration loading."""
    print("\nTesting configuration...")
    try:
        from src.config import Config, MarketType
        print("✓ Config loaded")
        print(f"  - Active markets: {[m.value for m in Config.ACTIVE_MARKETS]}")
        print(f"  - Arbitrage: {Config.ENABLE_ARBITRAGE}")
        print(f"  - Streak: {Config.ENABLE_STREAK}")
        print(f"  - Paper mode: {Config.PAPER_TRADE}")
        return True
    except Exception as e:
        print(f"✗ Config error: {e}")
        return False


def test_core():
    """Test core modules."""
    print("\nTesting core modules...")
    try:
        from src.core.polymarket import PolymarketClient
        print("✓ PolymarketClient")
        
        from src.core.trader import TradingState, PaperTrader
        print("✓ TradingState, PaperTrader")
        
        return True
    except Exception as e:
        print(f"✗ Core module error: {e}")
        return False


def test_strategies():
    """Test strategy modules."""
    print("\nTesting strategies...")
    try:
        from src.strategies.arbitrage import ArbitrageStrategy
        print("✓ ArbitrageStrategy")
        
        from src.strategies.streak import evaluate as evaluate_streak
        print("✓ StreakStrategy")
        
        from src.strategies.copytrade import CopytradeMonitor
        print("✓ CopytradeMonitor")
        
        return True
    except Exception as e:
        print(f"✗ Strategy error: {e}")
        return False


def test_web():
    """Test web module."""
    print("\nTesting web server...")
    try:
        from src.web.server import app
        print("✓ FastAPI app")
        return True
    except Exception as e:
        print(f"✗ Web server error: {e}")
        return False


def test_bot_engine():
    """Test bot engine."""
    print("\nTesting bot engine...")
    try:
        from src.bot_engine import BotEngine
        print("✓ BotEngine")
        
        # Try initializing (but don't start)
        bot = BotEngine()
        print(f"✓ Bot initialized")
        print(f"  - Bankroll: ${bot.state.bankroll:.2f}")
        print(f"  - Strategies: ARB={Config.ENABLE_ARBITRAGE}, STREAK={Config.ENABLE_STREAK}")
        
        return True
    except Exception as e:
        print(f"✗ Bot engine error: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("AcropolisBot Setup Test")
    print("=" * 60)
    
    tests = [
        ("Imports", test_imports),
        ("Configuration", test_config),
        ("Core Modules", test_core),
        ("Strategies", test_strategies),
        ("Web Server", test_web),
        ("Bot Engine", test_bot_engine),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n✗ Test '{name}' failed with exception: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 60)
    print("Test Results")
    print("=" * 60)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status} - {name}")
    
    all_passed = all(result for _, result in results)
    
    print("\n" + "=" * 60)
    if all_passed:
        print("✓ ALL TESTS PASSED!")
        print("\nYou're ready to run AcropolisBot:")
        print("  - Web GUI: uv run python web.py")
        print("  - CLI:     uv run python main.py")
    else:
        print("✗ SOME TESTS FAILED")
        print("\nTroubleshooting:")
        print("  1. Run: uv sync")
        print("  2. Check .env.example exists")
        print("  3. Create .env from .env.example")
    print("=" * 60)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
