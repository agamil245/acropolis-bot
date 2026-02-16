#!/usr/bin/env python3
"""
AcropolisBot Web Interface
Start the web dashboard
"""

from src.web.server import run_server


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════╗
    ║       AcropolisBot Web GUI           ║
    ║  Advanced Polymarket Trading Bot      ║
    ╚══════════════════════════════════════╝
    """)
    
    run_server()
