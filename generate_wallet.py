#!/usr/bin/env python3
"""Generate a fresh wallet for AcropolisBot trading."""
from eth_account import Account

wallet = Account.create()
print("=" * 50)
print("  🏛️ AcropolisBot — New Trading Wallet")
print("=" * 50)
print()
print(f"  Address:     {wallet.address}")
print(f"  Private Key: {wallet.key.hex()}")
print()
print("  NEXT STEPS:")
print(f"  1. Send $20 USDC to {wallet.address}")
print("     on POLYGON network (not Ethereum!)")
print("  2. Add to your .env file:")
print(f"     PRIVATE_KEY={wallet.key.hex()}")
print("  3. Set PAPER_TRADE=false")
print("  4. Restart the bot")
print()
print("  ⚠️  SAVE THE PRIVATE KEY — if you lose it,")
print("      you lose access to the wallet forever!")
print("=" * 50)
