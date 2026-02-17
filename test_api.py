"""Quick test: verify Polymarket API keys work."""
import sys
sys.path.insert(0, ".")

from src.core.poly_api import PolymarketDirectClient

client = PolymarketDirectClient(
    api_key="019c6d91-2a64-7bf8-8604-67597879a114",
    api_secret="PfFWWfAI8xsfhSjxpMMH4uuCYmCANOodb6Uv1EItwyo=",
    passphrase="270c12a4e29cdbfe88c054c0d9bff29c8a46740f7f44c48c8558a273f84e62ea",
    # No proxy — test direct first
)

print("=== Testing Polymarket API Connection ===\n")
results = client.test_connection()
for endpoint, status in results.items():
    print(f"  {endpoint}: {status}")

print(f"\n=== Balance ===")
balance = client.get_balance()
print(f"  USDC: ${balance:.2f}")

print(f"\n=== Market Test ===")
market = client.get_market_by_slug("btc-updown-5m-1771365000")
if market:
    print(f"  Found: {market.get('title', 'N/A')}")
else:
    print("  No market found (may be expired)")

# Try a current timestamp
import time
ts = int(time.time())
# Round to nearest 5 min
ts_rounded = (ts // 300) * 300
slug = f"btc-updown-5m-{ts_rounded}"
print(f"\n=== Current Market ({slug}) ===")
market2 = client.get_market_by_slug(slug)
if market2:
    print(f"  Found: {market2.get('title', 'N/A')}")
    markets = market2.get("markets", [])
    for m in markets[:2]:
        print(f"    {m.get('groupItemTitle', '?')}: token={m.get('clobTokenIds', ['?'])[0][:20]}...")
else:
    print("  Not found")

client.close()
print("\n✅ Done")
