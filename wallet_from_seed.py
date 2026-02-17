#!/usr/bin/env python3
"""Derive wallet from seed phrase (mnemonic)."""
import sys
from eth_account import Account

Account.enable_unaudited_hdwallet_features()

if len(sys.argv) < 2:
    print("Usage: python wallet_from_seed.py 'word1 word2 word3 ... word12'")
    sys.exit(1)

mnemonic = " ".join(sys.argv[1:])

# Derive first account (same as MetaMask account #1)
wallet = Account.from_mnemonic(mnemonic)

print("=" * 50)
print("  🏛️ Wallet Derived from Seed Phrase")
print("=" * 50)
print()
print(f"  Address:     {wallet.address}")
print(f"  Private Key: {wallet.key.hex()}")
print()
print("  Add to your .env:")
print(f"  PRIVATE_KEY={wallet.key.hex()}")
print("=" * 50)
