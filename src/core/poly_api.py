"""Direct Polymarket CLOB API client using API keys.

No py_clob_client dependency for authenticated calls.
Uses API key + HMAC-SHA256 signing directly.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

import httpx


class PolymarketDirectClient:
    """Direct HTTP client for Polymarket CLOB + Gamma APIs.
    
    Uses API key/secret/passphrase from Polymarket dashboard.
    No private key needed for reads. Order signing handled separately.
    """

    CLOB_HOST = "https://clob.polymarket.com"
    GAMMA_HOST = "https://gamma-api.polymarket.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        proxy_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.proxy_url = proxy_url

        # Proxied client for Polymarket APIs (geoblocked)
        proxy_kwargs = {"proxy": proxy_url} if proxy_url else {}
        self._client = httpx.Client(
            timeout=15.0,
            headers={
                "User-Agent": "AcropolisBot/2.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            **proxy_kwargs,
        )

        # Direct client for non-geoblocked APIs (Bybit, etc.)
        self._direct = httpx.Client(timeout=10.0)

    def _build_hmac_headers(self, method: str, path: str, body: str = "") -> dict:
        """Build HMAC-SHA256 auth headers for CLOB API."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        hmac_key = base64.b64decode(self.api_secret)
        sig = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256)
        sig_b64 = base64.b64encode(sig.digest()).decode("utf-8")

        return {
            "POLY-API-KEY": self.api_key,
            "POLY-SIGNATURE": sig_b64,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self.passphrase,
        }

    # ─── Balance ──────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Get USDC balance from Polymarket CLOB."""
        path = "/balance-allowance?asset_type=COLLATERAL&signature_type=0"
        headers = self._build_hmac_headers("GET", path)
        try:
            resp = self._client.get(f"{self.CLOB_HOST}{path}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            raw = float(data.get("balance", 0))
            return raw / 1e6 if raw > 1000 else raw
        except Exception as e:
            # Try signature_type=1 (proxy wallet)
            try:
                path2 = "/balance-allowance?asset_type=COLLATERAL&signature_type=1"
                headers2 = self._build_hmac_headers("GET", path2)
                resp2 = self._client.get(f"{self.CLOB_HOST}{path2}", headers=headers2)
                resp2.raise_for_status()
                data2 = resp2.json()
                raw2 = float(data2.get("balance", 0))
                return raw2 / 1e6 if raw2 > 1000 else raw2
            except Exception as e2:
                print(f"[poly_api] Balance failed (type=0: {e}, type=1: {e2})")
                return 0.0

    # ─── Market Data (Gamma API — no auth needed) ─────────────────────────

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch market data from Gamma API by slug."""
        try:
            resp = self._client.get(
                f"{self.GAMMA_HOST}/events",
                params={"slug": slug},
            )
            resp.raise_for_status()
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
            return None
        except Exception as e:
            print(f"[poly_api] Market fetch failed for {slug}: {e}")
            return None

    def search_markets(self, query: str, limit: int = 10) -> list[dict]:
        """Search for markets on Gamma API."""
        try:
            resp = self._client.get(
                f"{self.GAMMA_HOST}/markets",
                params={"_q": query, "_limit": limit, "active": True},
            )
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            print(f"[poly_api] Market search failed: {e}")
            return []

    # ─── Orders ───────────────────────────────────────────────────────────

    def get_open_orders(self, market_id: Optional[str] = None) -> list[dict]:
        """Get open orders."""
        path = "/orders?state=OPEN"
        if market_id:
            path += f"&market={market_id}"
        headers = self._build_hmac_headers("GET", path)
        try:
            resp = self._client.get(f"{self.CLOB_HOST}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            print(f"[poly_api] Get orders failed: {e}")
            return []

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        path = "/order"
        body = json.dumps({"orderID": order_id})
        headers = self._build_hmac_headers("DELETE", path, body)
        try:
            resp = self._client.request(
                "DELETE", f"{self.CLOB_HOST}{path}",
                headers=headers, content=body,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[poly_api] Cancel order failed: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        path = "/cancel-all"
        headers = self._build_hmac_headers("DELETE", path)
        try:
            resp = self._client.request(
                "DELETE", f"{self.CLOB_HOST}{path}", headers=headers,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[poly_api] Cancel all failed: {e}")
            return False

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> Optional[dict]:
        """Place a limit order (GTC) via CLOB API.
        
        Note: This uses the API key flow. For accounts created via
        polymarket.com dashboard, the server handles order signing.
        """
        path = "/order"
        order_payload = {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side.upper(),
            "type": "GTC",
            "feeRateBps": "0",  # maker = 0 fees
        }
        body = json.dumps(order_payload)
        headers = self._build_hmac_headers("POST", path, body)
        try:
            resp = self._client.post(
                f"{self.CLOB_HOST}{path}",
                headers=headers,
                content=body,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            print(f"[poly_api] Order failed [{e.response.status_code}]: {e.response.text}")
            return None
        except Exception as e:
            print(f"[poly_api] Order failed: {e}")
            return None

    def get_order(self, order_id: str) -> Optional[dict]:
        """Get order status by ID."""
        path = f"/order/{order_id}"
        headers = self._build_hmac_headers("GET", path)
        try:
            resp = self._client.get(f"{self.CLOB_HOST}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[poly_api] Get order failed: {e}")
            return None

    # ─── Trades History ───────────────────────────────────────────────────

    def get_trades(self, market_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Get trade history."""
        path = f"/trades?limit={limit}"
        if market_id:
            path += f"&market={market_id}"
        headers = self._build_hmac_headers("GET", path)
        try:
            resp = self._client.get(f"{self.CLOB_HOST}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            print(f"[poly_api] Get trades failed: {e}")
            return []

    # ─── Orderbook (public, no auth) ──────────────────────────────────────

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Get orderbook for a token (public endpoint)."""
        try:
            resp = self._client.get(
                f"{self.CLOB_HOST}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[poly_api] Orderbook failed: {e}")
            return None

    # ─── Health Check ─────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Test connectivity to all APIs. Returns status dict."""
        results = {}

        # Test Gamma API (market data)
        try:
            resp = self._client.get(f"{self.GAMMA_HOST}/markets?_limit=1&active=true")
            results["gamma"] = f"✅ OK ({resp.status_code})"
        except Exception as e:
            results["gamma"] = f"❌ {e}"

        # Test CLOB API (public)
        try:
            resp = self._client.get(f"{self.CLOB_HOST}/time")
            results["clob_public"] = f"✅ OK ({resp.status_code})"
        except Exception as e:
            results["clob_public"] = f"❌ {e}"

        # Test CLOB API (authenticated)
        try:
            path = "/balance-allowance?asset_type=COLLATERAL&signature_type=0"
            headers = self._build_hmac_headers("GET", path)
            resp = self._client.get(f"{self.CLOB_HOST}{path}", headers=headers)
            results["clob_auth"] = f"{'✅' if resp.status_code == 200 else '❌'} ({resp.status_code}: {resp.text[:100]})"
        except Exception as e:
            results["clob_auth"] = f"❌ {e}"

        return results

    def close(self):
        """Close HTTP clients."""
        self._client.close()
        self._direct.close()
