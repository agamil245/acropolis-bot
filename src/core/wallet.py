"""Wallet utilities — read on-chain USDC balance."""
import httpx

# USDC on Polygon (both native and bridged)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC
USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e

POLYGON_RPC = "https://polygon-rpc.com"

# ERC-20 balanceOf(address) function selector
BALANCE_OF = "0x70a08231"


def get_usdc_balance(address: str, rpc_url: str = POLYGON_RPC) -> float | None:
    """Get total USDC balance (native + bridged) for an address on Polygon."""
    total = 0.0

    for token, decimals in [(USDC_NATIVE, 6), (USDC_BRIDGED, 6)]:
        try:
            # Encode balanceOf call
            padded_address = address.lower().replace("0x", "").zfill(64)
            data = BALANCE_OF + padded_address

            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": token, "data": data}, "latest"],
                "id": 1,
            }

            resp = httpx.post(rpc_url, json=payload, timeout=10)
            result = resp.json().get("result", "0x0")
            balance = int(result, 16) / (10 ** decimals)
            total += balance
        except Exception:
            continue

    return total if total > 0 else None
