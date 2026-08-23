#!/usr/bin/env python3
"""
EVM venue adapter — covers Base AND Robinhood Chain with one implementation.

Robinhood Chain (mainnet 2026-07-01) is an Arbitrum Orbit L2, chain ID 4663,
gas in ETH, fully EVM-compatible. Base is chain ID 8453. Same bytecode, same
tooling, same swap flow — so this module serves both, and any other EVM L2 you
add to CHAINS.

Three things here are safety-critical and deliberate:

1. verify() checks eth_chainId against the expected value before any trade.
   A documented ecosystem of phishing RPCs and lookalike explorers grew up
   around Robinhood Chain after launch. A malicious RPC can feed you fake
   balances and fake quotes. This check is cheap and catches that class of
   attack, so it runs on startup and is not optional.

2. Approvals are EXACT-AMOUNT, never unlimited. An unlimited approval is a
   standing permission for a router contract to move that token out of your
   wallet forever. This is the single most common way EVM traders get drained
   long after the trade is done.

3. router addresses are NOT hardcoded for chains I could not verify. A wrong
   router address means sending funds to an arbitrary contract. Set it from
   the chain's official docs and confirm it on the block explorer.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger("evm")

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    rpc_env: str
    default_rpc: str
    wrapped_native: str = ""
    router: str = ""            # Uniswap V3 SwapRouter02, or aggregator target
    explorer: str = ""
    verified: bool = False      # False => user must supply and confirm router


CHAINS: dict[str, ChainConfig] = {
    "base": ChainConfig(
        name="base",
        chain_id=8453,
        rpc_env="BASE_RPC",
        default_rpc="https://mainnet.base.org",
        wrapped_native="0x4200000000000000000000000000000000000006",   # WETH
        router="0x2626664c2603336E57B271c5C0b26F421741e481",           # SwapRouter02
        explorer="https://basescan.org",
        verified=True,
    ),
    "robinhood": ChainConfig(
        name="robinhood",
        chain_id=4663,
        rpc_env="ROBINHOOD_RPC",
        default_rpc="https://rpc.mainnet.chain.robinhood.com",
        wrapped_native=os.getenv("RH_WETH", ""),
        router=os.getenv("RH_ROUTER", ""),
        explorer="https://robinhoodchain.blockscout.com",
        verified=False,   # Uniswap is deployed here; addresses NOT verified by me
    ),
}


@dataclass
class EvmResult:
    ok: bool
    tx_hash: str = ""
    amount_out: int = 0
    error: str = ""


class EvmVenue:
    """One adapter, many chains. Instantiate per chain."""

    def __init__(self, chain: str, private_key: Optional[str] = None,
                 max_slippage_pct: float = 3.0):
        from web3 import Web3
        from eth_account import Account

        if chain not in CHAINS:
            raise ValueError(f"unknown chain {chain}; known: {list(CHAINS)}")
        self.cfg = CHAINS[chain]
        self.name = self.cfg.name
        self.slippage = max_slippage_pct

        rpc = os.getenv(self.cfg.rpc_env) or self.cfg.default_rpc
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))

        key = private_key or os.getenv("EVM_PRIVATE_KEY")
        if not key:
            raise RuntimeError("EVM_PRIVATE_KEY not set")
        self.acct = Account.from_key(key)
        LOG.info("%s venue wallet %s", self.name, self.acct.address)

    # ── anti-phishing / config sanity ──────────────────────────────────
    def verify(self) -> tuple[bool, str]:
        """Run before trading. Catches phishing RPCs and unset routers."""
        try:
            actual = self.w3.eth.chain_id
        except Exception as e:  # noqa: BLE001
            return False, f"RPC unreachable: {e}"
        if actual != self.cfg.chain_id:
            return False, (f"CHAIN ID MISMATCH — RPC reports {actual}, expected "
                           f"{self.cfg.chain_id}. Possible phishing RPC. Stop.")
        if not self.cfg.router:
            return False, (f"no router configured for {self.name}. Set "
                           f"{self.name.upper()}_ROUTER from official docs and "
                           f"confirm it on {self.cfg.explorer} before trading.")
        if not self.cfg.verified:
            LOG.warning("%s router %s is UNVERIFIED by this codebase — confirm "
                        "on %s that it is the real Uniswap router",
                        self.name, self.cfg.router, self.cfg.explorer)
        if not self.w3.is_address(self.cfg.router):
            return False, f"router {self.cfg.router} is not a valid address"
        code = self.w3.eth.get_code(self.w3.to_checksum_address(self.cfg.router))
        if not code or len(code) < 2:
            return False, "router address has no contract code — wrong address"
        return True, f"{self.name} ok (chain {actual})"

    # ── erc20 helpers ──────────────────────────────────────────────────
    def _erc20(self, token: str):
        return self.w3.eth.contract(
            address=self.w3.to_checksum_address(token), abi=ERC20_ABI)

    def decimals(self, token: str) -> int:
        try:
            return self._erc20(token).functions.decimals().call()
        except Exception:  # noqa: BLE001
            return 18

    def balance_raw(self, token: str) -> int:
        return self._erc20(token).functions.balanceOf(self.acct.address).call()

    def supports(self, asset: str) -> bool:
        return asset.startswith("0x") and len(asset) == 42

    # ── approvals: exact amount only ───────────────────────────────────
    def ensure_approval(self, token: str, amount: int) -> EvmResult:
        """Approves exactly `amount`. Never MAX_UINT.

        Costs an extra transaction per trade versus an unlimited approval.
        That is the intended trade-off: a stale unlimited approval to a
        malicious or later-compromised router is a standing drain permission.
        """
        c = self._erc20(token)
        router = self.w3.to_checksum_address(self.cfg.router)
        current = c.functions.allowance(self.acct.address, router).call()
        if current >= amount:
            return EvmResult(True)

        # Some tokens (USDT-style) revert on approve() unless allowance is 0.
        if current > 0:
            r = self._send(c.functions.approve(router, 0))
            if not r.ok:
                return r

        return self._send(c.functions.approve(router, int(amount)))

    def revoke_approval(self, token: str) -> EvmResult:
        """Housekeeping — call after exiting a position."""
        c = self._erc20(token)
        return self._send(c.functions.approve(
            self.w3.to_checksum_address(self.cfg.router), 0))

    # ── tx plumbing ────────────────────────────────────────────────────
    def _send(self, fn, value: int = 0) -> EvmResult:
        try:
            tx = fn.build_transaction({
                "from": self.acct.address,
                "nonce": self.w3.eth.get_transaction_count(self.acct.address),
                "chainId": self.cfg.chain_id,
                "value": value,
            })
            try:
                tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.25)
            except Exception as e:  # noqa: BLE001
                # A failing estimate usually means the swap itself would revert
                # (honeypot, no liquidity, sell disabled). Do not force-send.
                return EvmResult(False, error=f"gas estimate failed — likely "
                                              f"revert: {str(e)[:120]}")
            signed = self.acct.sign_transaction(tx)
            h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
            if rcpt.status != 1:
                return EvmResult(False, h.hex(), error="tx reverted on-chain")
            return EvmResult(True, h.hex())
        except Exception as e:  # noqa: BLE001
            return EvmResult(False, error=str(e)[:200])

    # ── swaps ──────────────────────────────────────────────────────────
    def swap(self, token_in: str, token_out: str, amount_in: int,
             min_out: int, fee_tier: int = 3000) -> EvmResult:
        """Uniswap V3 exactInputSingle.

        min_out must be computed by the caller from a real quote. Passing 0
        means accepting any output and is an open invitation to sandwich bots;
        this method rejects it.
        """
        if min_out <= 0:
            return EvmResult(False, error="min_out must be > 0 (MEV protection)")

        ok, msg = self.verify()
        if not ok:
            return EvmResult(False, error=msg)

        appr = self.ensure_approval(token_in, amount_in)
        if not appr.ok:
            return EvmResult(False, error=f"approval failed: {appr.error}")

        router_abi = [{
            "name": "exactInputSingle", "type": "function",
            "stateMutability": "payable",
            "inputs": [{"name": "params", "type": "tuple", "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
            "outputs": [{"name": "amountOut", "type": "uint256"}]}]

        router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.cfg.router), abi=router_abi)
        params = (
            self.w3.to_checksum_address(token_in),
            self.w3.to_checksum_address(token_out),
            fee_tier,
            self.acct.address,
            int(amount_in),
            int(min_out),
            0,
        )
        return self._send(router.functions.exactInputSingle(params))

    def balances(self) -> dict[str, float]:
        native = self.w3.eth.get_balance(self.acct.address) / 1e18
        return {"ETH": round(native, 6)}


# ─────────────────────── EVM token safety screen ───────────────────────────

class GoPlusSecurity:
    """EVM equivalent of RugCheck. RugCheck is Solana-only, so the §2 safety
    gates need a different source on Base and Robinhood Chain.

    Free, no key required, rate limited. Returns honeypot status, buy/sell
    tax, LP lock, holder concentration, and owner privileges.
    """

    BASE = "https://api.gopluslabs.io/api/v1/token_security"

    def __init__(self, timeout: int = 15):
        import requests
        self.s = requests.Session()
        self.timeout = timeout

    def check(self, chain_id: int, token: str) -> Optional[dict]:
        try:
            r = self.s.get(f"{self.BASE}/{chain_id}",
                           params={"contract_addresses": token},
                           timeout=self.timeout)
            r.raise_for_status()
            result = (r.json() or {}).get("result") or {}
            return result.get(token.lower())
        except Exception as e:  # noqa: BLE001
            LOG.debug("goplus failed: %s", e)
            return None

    def gates(self, chain_id: int, token: str,
              max_tax_pct: float = 5.0) -> list[tuple[str, bool, str]]:
        """Returns (gate_name, passed, detail). Missing data => failed."""
        d = self.check(chain_id, token)
        if not d:
            return [("evm_safety", False, "no security report — treat as reject")]

        def num(k, default=0.0):
            try:
                return float(d.get(k) or default)
            except (TypeError, ValueError):
                return default

        buy_tax, sell_tax = num("buy_tax") * 100, num("sell_tax") * 100
        return [
            ("2.8_honeypot", d.get("is_honeypot") == "0",
             f"honeypot={d.get('is_honeypot')}"),
            ("2.9_round_trip_tax", (buy_tax + sell_tax) < max_tax_pct,
             f"{buy_tax:.1f}%+{sell_tax:.1f}%"),
            ("2.1_mintable", d.get("is_mintable") == "0",
             f"mintable={d.get('is_mintable')}"),
            ("2.2_pausable_transfer", d.get("transfer_pausable") == "0",
             f"pausable={d.get('transfer_pausable')}"),
            ("2.3_lp_locked", num("lp_holder_count") > 0
             and any(float(h.get("percent", 0)) > 0.9
                     for h in (d.get("lp_holders") or [])
                     if h.get("is_locked") == 1),
             f"lp_holders={d.get('lp_holder_count')}"),
            ("2.5_owner_can_take_back", d.get("owner_change_balance") == "0",
             f"owner_change_balance={d.get('owner_change_balance')}"),
            ("2.11_blacklist_fn", d.get("is_blacklisted") == "0",
             f"blacklist={d.get('is_blacklisted')}"),
        ]


def preflight() -> None:
    """Prints what is and isn't configured. Run before trading either chain."""
    print("EVM venue preflight\n" + "─" * 60)
    for name, cfg in CHAINS.items():
        rpc = os.getenv(cfg.rpc_env) or cfg.default_rpc
        print(f"\n{name}  (chain id {cfg.chain_id})")
        print(f"  rpc      {rpc}")
        print(f"  router   {cfg.router or '** NOT SET **'}"
              f"{'' if cfg.verified else '   [UNVERIFIED — confirm on explorer]'}")
        print(f"  wnative  {cfg.wrapped_native or '** NOT SET **'}")
        print(f"  explorer {cfg.explorer}")
        if not os.getenv("EVM_PRIVATE_KEY"):
            print("  wallet   ** EVM_PRIVATE_KEY not set **")
            continue
        try:
            v = EvmVenue(name)
            ok, msg = v.verify()
            print(f"  status   {'OK' if ok else 'BLOCKED'} — {msg}")
        except Exception as e:  # noqa: BLE001
            print(f"  status   ERROR — {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    preflight()
