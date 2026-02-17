#!/usr/bin/env python3
"""Approve USDC spending for Polymarket's CTF Exchange on Polygon."""
import os
import json
import httpx
from eth_account import Account

POLYGON_RPC = "https://polygon-rpc.com"

# USDC on Polygon
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (bridged)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"    # Native USDC

# Polymarket CTF Exchange contract (spender)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Max approval
MAX_UINT256 = "0x" + "f" * 64

# ERC-20 approve(address,uint256) selector
APPROVE_SELECTOR = "0x095ea7b3"

def approve(private_key: str, token: str, spender: str):
    account = Account.from_key(private_key)
    address = account.address
    
    # Encode approve(spender, max_uint256)
    padded_spender = spender.lower().replace("0x", "").zfill(64)
    padded_amount = "f" * 64
    data = APPROVE_SELECTOR + padded_spender + padded_amount
    
    # Get nonce
    resp = httpx.post(POLYGON_RPC, json={
        "jsonrpc": "2.0", "method": "eth_getTransactionCount",
        "params": [address, "latest"], "id": 1
    })
    nonce = int(resp.json()["result"], 16)
    
    # Get gas price
    resp = httpx.post(POLYGON_RPC, json={
        "jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1
    })
    gas_price = int(resp.json()["result"], 16)
    
    # Build transaction
    tx = {
        "nonce": nonce,
        "to": token,
        "value": 0,
        "gas": 60000,
        "gasPrice": int(gas_price * 1.2),
        "data": bytes.fromhex(data[2:]),
        "chainId": 137,
    }
    
    signed = account.sign_transaction(tx)
    raw_tx = "0x" + signed.raw_transaction.hex()
    
    # Send
    resp = httpx.post(POLYGON_RPC, json={
        "jsonrpc": "2.0", "method": "eth_sendRawTransaction",
        "params": [raw_tx], "id": 1
    })
    result = resp.json()
    if "error" in result:
        print(f"  ❌ Error: {result['error']}")
    else:
        print(f"  ✅ TX: {result['result']}")
    return result

def main():
    key = os.environ.get("PRIVATE_KEY", "")
    if not key:
        print("Set PRIVATE_KEY env var")
        return
    
    account = Account.from_key(key)
    print(f"Wallet: {account.address}")
    print()
    
    spenders = [CTF_EXCHANGE, NEG_RISK_CTF, NEG_RISK_ADAPTER]
    tokens = [USDC_NATIVE, USDC_CONTRACT]
    
    for token in tokens:
        for spender in spenders:
            print(f"Approving {token[:10]}... for {spender[:10]}...")
            approve(key, token, spender)
    
    print("\n✅ All approvals done! Restart the bot.")

if __name__ == "__main__":
    main()
