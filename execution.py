#!/usr/bin/env python3
"""
Execution layer: Jupiter Swap API v1 + Solana transaction signing.

Endpoint note: quote-api.jup.ag/v6 and lite-api.jup.ag are both deprecated
(lite-api sunset 2026-01-31). Current base is https://api.jup.ag/swap/v1.
Free tier works without a key but is rate limited; set JUPITER_API_KEY for
production throughput.

Requires: pip install solders base58 requests
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

LOG = logging.getLogger("exec")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


# ───────────────────────────── jupiter client ──────────────────────────────

class JupiterSwap:
    BASE = "https://api.jup.ag/swap/v1"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15):
        self.s = requests.Session()
        self.timeout = timeout
        key = api_key or os.getenv("JUPITER_API_KEY")
        if key:
            self.s.headers["x-api-key"] = key

    def quote(self, input_mint: str, output_mint: str, amount: int,
              slippage_bps: int = 300) -> Optional[dict]:
        """amount is in the input token's smallest unit."""
        try:
            r = self.s.get(f"{self.BASE}/quote", timeout=self.timeout, params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount)),
                "slippageBps": int(slippage_bps),
                "restrictIntermediateTokens": "true",   # avoids fragile routes
            })
            if r.status_code == 429:
                LOG.warning("Jupiter rate limited")
                return None
            r.raise_for_status()
            q = r.json()
            return q if q.get("outAmount") else None
        except Exception as e:  # noqa: BLE001
            LOG.debug("quote failed: %s", e)
            return None

    def price_impact_pct(self, quote: dict) -> float:
        try:
            return abs(float(quote.get("priceImpactPct", 0))) * 100
        except (TypeError, ValueError):
            return 100.0

    def build_swap_tx(self, quote: dict, user_pubkey: str,
                      max_priority_lamports: int = 1_000_000) -> Optional[str]:
        """Returns a base64 unsigned VersionedTransaction."""
        payload = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(max_priority_lamports),
                    "priorityLevel": "high",
                }
            },
        }
        try:
            r = self.s.post(f"{self.BASE}/swap", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("swapTransaction")
        except Exception as e:  # noqa: BLE001
            LOG.error("swap build failed: %s", e)
            return None


# ──────────────────────────── wallet / signing ─────────────────────────────

class Wallet:
    """Loads a keypair from env. Never hardcode a key; never log one."""

    def __init__(self, secret: Optional[str] = None):
        from solders.keypair import Keypair

        raw = secret or os.getenv("SOLANA_PRIVATE_KEY")
        if not raw:
            raise RuntimeError("SOLANA_PRIVATE_KEY not set")
        raw = raw.strip()
        if raw.startswith("["):                     # JSON byte array format
            self.kp = Keypair.from_bytes(bytes(json.loads(raw)))
        else:                                       # base58 format
            import base58
            self.kp = Keypair.from_bytes(base58.b58decode(raw))
        LOG.info("wallet loaded: %s", self.pubkey)

    @property
    def pubkey(self) -> str:
        return str(self.kp.pubkey())

    def sign(self, tx_b64: str) -> str:
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [self.kp])
        return base64.b64encode(bytes(signed)).decode()


# ────────────────────────────── rpc sender ─────────────────────────────────

class TxSender:
    def __init__(self, rpc_url: str, timeout: int = 20):
        self.rpc = rpc_url
        self.s = requests.Session()
        self.timeout = timeout
        self._id = 0

    def _call(self, method: str, params: list):
        self._id += 1
        r = self.s.post(self.rpc, timeout=self.timeout, json={
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        r.raise_for_status()
        out = r.json()
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out.get("result")

    def send(self, signed_b64: str) -> str:
        return self._call("sendTransaction", [signed_b64, {
            "encoding": "base64", "skipPreflight": False, "maxRetries": 3}])

    def confirm(self, sig: str, timeout_s: int = 60) -> tuple[bool, str]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                res = self._call("getSignatureStatuses", [[sig], {
                    "searchTransactionHistory": True}])
                st = (res or {}).get("value", [None])[0]
                if st:
                    if st.get("err"):
                        return False, f"tx failed on-chain: {st['err']}"
                    if st.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True, st["confirmationStatus"]
            except Exception as e:  # noqa: BLE001
                LOG.debug("confirm poll: %s", e)
            time.sleep(2)
        return False, "confirmation timeout"

    def sol_balance(self, pubkey: str) -> float:
        res = self._call("getBalance", [pubkey])
        return (res or {}).get("value", 0) / LAMPORTS_PER_SOL

    def token_balance_raw(self, owner: str, mint: str) -> int:
        res = self._call("getTokenAccountsByOwner",
                         [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        for acc in (res or {}).get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            total += int(info["tokenAmount"]["amount"])
        return total


# ────────────────────────────── live broker ────────────────────────────────

@dataclass
class SwapResult:
    ok: bool
    signature: str = ""
    in_amount: int = 0
    out_amount: int = 0
    price_impact_pct: float = 0.0
    error: str = ""


class LiveBroker:
    """Executes real swaps. Enforces slippage and impact limits at the
    transaction level, independent of the strategy's own checks."""

    def __init__(self, rpc_url: str, wallet: Wallet, jup: JupiterSwap,
                 max_slippage_pct: float = 3.0,
                 max_impact_pct: float = 4.0,
                 max_priority_fee_pct: float = 0.4):
        self.jup = jup
        self.wallet = wallet
        self.sender = TxSender(rpc_url)
        self.max_slippage_bps = int(max_slippage_pct * 100)
        self.max_impact_pct = max_impact_pct
        self.max_priority_fee_pct = max_priority_fee_pct

    def _execute(self, input_mint: str, output_mint: str,
                 amount_raw: int, value_lamports: int) -> SwapResult:
        q = self.jup.quote(input_mint, output_mint, amount_raw,
                           self.max_slippage_bps)
        if not q:
            return SwapResult(False, error="no route")

        impact = self.jup.price_impact_pct(q)
        if impact > self.max_impact_pct:
            return SwapResult(False, price_impact_pct=impact,
                              error=f"price impact {impact:.2f}% over limit")

        max_prio = max(10_000, int(value_lamports * self.max_priority_fee_pct / 100))
        tx = self.jup.build_swap_tx(q, self.wallet.pubkey, max_prio)
        if not tx:
            return SwapResult(False, error="tx build failed")

        try:
            sig = self.sender.send(self.wallet.sign(tx))
        except Exception as e:  # noqa: BLE001
            return SwapResult(False, error=f"send failed: {e}")

        ok, detail = self.sender.confirm(sig)
        return SwapResult(ok=ok, signature=sig,
                          in_amount=int(q["inAmount"]),
                          out_amount=int(q["outAmount"]),
                          price_impact_pct=impact,
                          error="" if ok else detail)

    def buy(self, mint: str, sol_amount: float) -> SwapResult:
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        bal = self.sender.sol_balance(self.wallet.pubkey)
        if bal * LAMPORTS_PER_SOL < lamports + 20_000_000:   # keep ~0.02 SOL for fees
            return SwapResult(False, error=f"insufficient SOL (have {bal:.4f})")
        return self._execute(SOL_MINT, mint, lamports, lamports)

    def sell(self, mint: str, raw_amount: int,
             est_value_lamports: int = 0) -> SwapResult:
        held = self.sender.token_balance_raw(self.wallet.pubkey, mint)
        amount = min(raw_amount, held)
        if amount <= 0:
            return SwapResult(False, error="no balance to sell")
        return self._execute(mint, SOL_MINT, amount, est_value_lamports)

    def sell_all(self, mint: str) -> SwapResult:
        """Used by emergency exits — dumps the entire balance."""
        held = self.sender.token_balance_raw(self.wallet.pubkey, mint)
        if held <= 0:
            return SwapResult(False, error="no balance")
        # Emergency exits accept worse pricing; getting out beats getting a fill price.
        saved = self.max_impact_pct
        self.max_impact_pct = 100.0
        try:
            return self._execute(mint, SOL_MINT, held, 0)
        finally:
            self.max_impact_pct = saved
