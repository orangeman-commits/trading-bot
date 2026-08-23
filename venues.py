#!/usr/bin/env python3
"""
Venue abstraction. One interface, three very different backends.

  SolanaVenue     — permissionless AMM. Any token. This is where the strategy lives.
  BaseVenue       — permissionless AMM (EVM). Any token. Needs web3 + an aggregator.
  RobinhoodVenue  — brokerage. Majors only. NOT a chain, and cannot trade microcaps.

The important asymmetry: Robinhood lists roughly two dozen large caps. A newly
deployed token will never be there. Robinhood is for holding majors and stables,
not for the scanner strategy. Routing a /buy of a random mint to Robinhood will
correctly fail with "unsupported symbol" — that is not a bug.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

import requests

LOG = logging.getLogger("venues")


@dataclass
class OrderResult:
    ok: bool
    ref: str = ""              # tx signature or order id
    filled_qty: float = 0.0
    avg_price: float = 0.0
    error: str = ""


class Venue(Protocol):
    name: str
    def supports(self, asset: str) -> bool: ...
    def buy(self, asset: str, quote_amount: float) -> OrderResult: ...
    def sell(self, asset: str, qty: float, fraction: float = 1.0) -> OrderResult: ...
    def balances(self) -> dict[str, float]: ...


# ───────────────────────────── solana ──────────────────────────────────────

class SolanaVenue:
    """Wraps execution.LiveBroker. Trades any SPL mint via Jupiter."""

    name = "solana"

    def __init__(self, rpc_url: str, max_slippage_pct: float = 3.0):
        from execution import JupiterSwap, LiveBroker, Wallet
        self.broker = LiveBroker(rpc_url, Wallet(), JupiterSwap(),
                                 max_slippage_pct=max_slippage_pct)

    def supports(self, asset: str) -> bool:
        return 32 <= len(asset) <= 44 and asset.isalnum()   # base58 mint

    def buy(self, asset: str, quote_amount: float) -> OrderResult:
        r = self.broker.buy(asset, quote_amount)            # quote_amount in SOL
        return OrderResult(r.ok, r.signature, r.out_amount, 0.0, r.error)

    def sell(self, asset: str, qty: float, fraction: float = 1.0) -> OrderResult:
        r = self.broker.sell_all(asset) if fraction >= 1.0 \
            else self.broker.sell(asset, int(qty * fraction))
        return OrderResult(r.ok, r.signature, r.out_amount, 0.0, r.error)

    def balances(self) -> dict[str, float]:
        return {"SOL": self.broker.sender.sol_balance(self.broker.wallet.pubkey)}


# ────────────────────────────── base ───────────────────────────────────────

class EvmVenueAdapter:
    """Wraps evm_venue.EvmVenue for Base and Robinhood Chain.

    Robinhood Chain is an Arbitrum Orbit L2 (id 4663) — same EVM surface as
    Base, so one adapter covers both. Instantiate per chain.
    """

    def __init__(self, chain: str, max_slippage_pct: float = 3.0):
        from evm_venue import EvmVenue
        self.v = EvmVenue(chain, max_slippage_pct=max_slippage_pct)
        self.name = chain
        ok, msg = self.v.verify()
        self.ready = ok
        if not ok:
            LOG.warning("%s venue not ready: %s", chain, msg)

    def supports(self, asset: str) -> bool:
        return self.ready and self.v.supports(asset)

    def buy(self, asset: str, quote_amount: float) -> OrderResult:
        wnative = self.v.cfg.wrapped_native
        if not wnative:
            return OrderResult(False, error=f"wrapped native token not set for {self.name}")
        amt = int(quote_amount * 1e18)
        r = self.v.swap(wnative, asset, amt, min_out=1)   # caller must set real min_out
        return OrderResult(r.ok, r.tx_hash, r.amount_out, 0.0, r.error)

    def sell(self, asset: str, qty: float, fraction: float = 1.0) -> OrderResult:
        wnative = self.v.cfg.wrapped_native
        if not wnative:
            return OrderResult(False, error=f"wrapped native token not set for {self.name}")
        held = self.v.balance_raw(asset)
        amt = int(held * fraction)
        if amt <= 0:
            return OrderResult(False, error="no balance")
        r = self.v.swap(asset, wnative, amt, min_out=1)
        if r.ok and fraction >= 1.0:
            self.v.revoke_approval(asset)       # no lingering permissions
        return OrderResult(r.ok, r.tx_hash, r.amount_out, 0.0, r.error)

    def balances(self) -> dict[str, float]:
        return self.v.balances()


# ─────────────────────────── robinhood crypto ──────────────────────────────

class RobinhoodVenue:
    """Official Robinhood Crypto Trading API.

    Auth: API key + Ed25519 signature over
          {api_key}{timestamp}{path}{method}{body}
    Headers: x-api-key, x-timestamp, x-signature

    Requires a US Robinhood Crypto account with API access enabled in Crypto
    Account Settings. Keys are generated there; the private key never leaves
    your machine. Set RH_API_KEY and RH_PRIVATE_KEY (base64 seed).
    """

    name = "robinhood"
    BASE = "https://trading.robinhood.com"

    def __init__(self, api_key: Optional[str] = None,
                 private_key_b64: Optional[str] = None):
        import nacl.signing

        self.api_key = api_key or os.getenv("RH_API_KEY")
        seed = private_key_b64 or os.getenv("RH_PRIVATE_KEY")
        if not self.api_key or not seed:
            raise RuntimeError("RH_API_KEY / RH_PRIVATE_KEY not set")
        self.signer = nacl.signing.SigningKey(base64.b64decode(seed))
        self.s = requests.Session()
        self._pairs_cache: tuple[float, set[str]] = (0.0, set())

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time()))
        message = f"{self.api_key}{ts}{path}{method}{body}"
        sig = self.signer.sign(message.encode()).signature
        return {
            "x-api-key": self.api_key,
            "x-timestamp": ts,
            "x-signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: str = "") -> Optional[dict]:
        try:
            r = self.s.request(method, f"{self.BASE}{path}",
                               headers=self._headers(method, path, body),
                               data=body or None, timeout=15)
            if r.status_code >= 400:
                LOG.error("RH %s %s -> %s %s", method, path, r.status_code, r.text[:200])
                return None
            return r.json()
        except Exception as e:  # noqa: BLE001
            LOG.error("RH request failed: %s", e)
            return None

    def supported_pairs(self, max_age_s: int = 3600) -> set[str]:
        ts, cached = self._pairs_cache
        if cached and time.time() - ts < max_age_s:
            return cached
        data = self._request("GET", "/api/v1/crypto/trading/trading_pairs/")
        pairs = {p.get("symbol", "") for p in (data or {}).get("results", [])}
        if pairs:
            self._pairs_cache = (time.time(), pairs)
        return pairs

    def supports(self, asset: str) -> bool:
        sym = asset.upper()
        sym = sym if "-" in sym else f"{sym}-USD"
        return sym in self.supported_pairs()

    def _order(self, symbol: str, side: str, asset_qty: float) -> OrderResult:
        import json as _json
        symbol = symbol.upper()
        symbol = symbol if "-" in symbol else f"{symbol}-USD"
        body = _json.dumps({
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "symbol": symbol,
            "type": "market",
            "market_order_config": {"asset_quantity": str(asset_qty)},
        }, separators=(",", ":"))
        res = self._request("POST", "/api/v1/crypto/trading/orders/", body)
        if not res:
            return OrderResult(False, error=f"{side} rejected")
        return OrderResult(True, res.get("id", ""),
                           float(res.get("filled_asset_quantity") or 0), 0.0)

    def buy(self, asset: str, quote_amount: float) -> OrderResult:
        """quote_amount is USD; converted to asset qty via best bid/ask."""
        sym = asset.upper() if "-" in asset else f"{asset.upper()}-USD"
        md = self._request(
            "GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={sym}")
        try:
            price = float((md or {})["results"][0]["ask_inclusive_of_buy_spread"])
        except (KeyError, IndexError, TypeError, ValueError):
            return OrderResult(False, error="no market data")
        if price <= 0:
            return OrderResult(False, error="bad price")
        return self._order(sym, "buy", round(quote_amount / price, 8))

    def sell(self, asset: str, qty: float, fraction: float = 1.0) -> OrderResult:
        return self._order(asset, "sell", round(qty * fraction, 8))

    def balances(self) -> dict[str, float]:
        data = self._request("GET", "/api/v1/crypto/trading/holdings/")
        return {h["asset_code"]: float(h.get("total_quantity") or 0)
                for h in (data or {}).get("results", [])}


# ───────────────────────────── router ──────────────────────────────────────

class VenueRouter:
    """Picks the venue that can actually handle an asset."""

    def __init__(self, venues: list):
        self.venues = {v.name: v for v in venues}

    def resolve(self, asset: str, prefer: Optional[str] = None):
        if prefer:
            v = self.venues.get(prefer)
            if v and v.supports(asset):
                return v
            if v:
                raise ValueError(f"{prefer} does not support {asset}")
            raise ValueError(f"unknown venue {prefer}")
        for v in self.venues.values():
            if v.supports(asset):
                return v
        raise ValueError(
            f"no venue supports {asset}. Microcap mints trade on solana/base; "
            f"robinhood only lists major pairs.")

    def all_balances(self) -> dict[str, dict]:
        out = {}
        for name, v in self.venues.items():
            try:
                out[name] = v.balances()
            except Exception as e:  # noqa: BLE001
                out[name] = {"error": str(e)}
        return out
