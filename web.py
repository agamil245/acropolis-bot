#!/usr/bin/env python3
"""Entry point for running just the AcropolisBot web server."""

import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.web.server import run_server

if __name__ == "__main__":
    run_server()
