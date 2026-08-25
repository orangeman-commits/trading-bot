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
              max_tax_pct: float = 5.0,
              max_top10_pct: float = 25.0,
              observed_volume_24h: float = 0.0,
              observed_pool_count: int = 0) -> list[tuple[str, str, str]]:
        """Returns (gate_name, verdict, detail) where verdict is
        PASS / REJECT / UNKNOWN.

        Three states, not two. GoPlus coverage varies sharply by chain: the
        full Ethereum response carries is_honeypot, is_mintable, lp_holders
        and friends, while Base returns only 11 fields and omits all of them.
        A missing field is NOT a failed check. Both still block a trade, but
        conflating them tells you a token is dangerous when the truth is that
        nobody looked.
        """
        d = self.check(chain_id, token)
        if not d:
            return [("evm_safety", "UNKNOWN", "no report from provider")]

        out: list[tuple[str, str, str]] = []

        def three(name: str, key: str, good_value: str, label: str):
            raw = d.get(key)
            if raw is None:
                out.append((name, "UNKNOWN", f"{label} not covered on this chain"))
            else:
                out.append((name, "PASS" if str(raw) == good_value else "REJECT",
                            f"{label}={raw}"))

        # Fields present on every chain GoPlus supports
        try:
            buy_t = float(d.get("buy_tax") or 0) * 100
            sell_t = float(d.get("sell_tax") or 0) * 100
            total = buy_t + sell_t
            out.append(("2.9_round_trip_tax",
                        "PASS" if total < max_tax_pct else "REJECT",
                        f"buy {buy_t:.1f}% + sell {sell_t:.1f}%"))
        except (TypeError, ValueError):
            out.append(("2.9_round_trip_tax", "UNKNOWN", "tax fields unparseable"))

        # Proxy contracts: the implementation can be swapped after you buy,
        # so today's audited bytecode is not a guarantee about tomorrow's.
        # This field was previously ignored entirely on every EVM chain.
        proxy = d.get("is_proxy")
        out.append(("2.12_proxy_contract",
                    "UNKNOWN" if proxy is None else
                    ("PASS" if str(proxy) == "0" else "REJECT"),
                    "not covered" if proxy is None else
                    ("no proxy" if str(proxy) == "0" else
                     "UPGRADEABLE — implementation can change after purchase")))

        src = d.get("is_open_source")
        out.append(("2.11_open_source",
                    "UNKNOWN" if src is None else
                    ("PASS" if str(src) == "1" else "REJECT"),
                    f"open_source={src}"))

        in_dex = d.get("is_in_dex")
        # GoPlus's is_in_dex has shown stale/false negatives on newer chains
        # (seen on Robinhood Chain: is_in_dex=0 on a token with $1.7M/day
        # volume and 6,500+ transactions in the SAME report). Real observed
        # market activity is direct, harder-to-fake evidence than a single
        # provider flag, so it overrides a REJECT here — but not a PASS
        # claim; we still note the disagreement rather than hiding it.
        has_real_activity = observed_volume_24h > 1000 or observed_pool_count > 0
        if in_dex is None:
            out.append(("2.8_tradeable", "UNKNOWN", "in_dex not covered"))
        elif str(in_dex) != "1" and has_real_activity:
            out.append(("2.8_tradeable", "PASS",
                       f"in_dex=0 but observed ${observed_volume_24h:,.0f} "
                       f"24h volume across {observed_pool_count} pool(s) — "
                       f"GoPlus flag appears stale, overridden by market data"))
        else:
            out.append(("2.8_tradeable",
                        "PASS" if str(in_dex) == "1" else "REJECT",
                        f"in_dex={in_dex}"))

        # Holder concentration from the holders array when present
        holders = d.get("holders")
        if isinstance(holders, list) and holders:
            pcts = []
            for h in holders:
                if str(h.get("is_locked")) == "1":
                    continue                      # locked LP is not a whale
                try:
                    pcts.append(float(h.get("percent") or 0) * 100)
                except (TypeError, ValueError):
                    pass
            if pcts:
                pcts.sort(reverse=True)
                top10 = sum(pcts[:10])
                out.append(("2.4_top10_concentration",
                            "PASS" if top10 < max_top10_pct else "REJECT",
                            f"{top10:.1f}%"))
            else:
                out.append(("2.4_top10_concentration", "UNKNOWN",
                            "holder percentages unparseable"))
        else:
            out.append(("2.4_top10_concentration", "UNKNOWN",
                        "holders not covered on this chain"))

        # Ownership renounced is a genuine positive signal
        owner = d.get("owner_address")
        if owner is None:
            out.append(("2.6_ownership", "UNKNOWN", "owner not covered"))
        elif owner in ("", "0x0000000000000000000000000000000000000000"):
            out.append(("2.6_ownership", "PASS", "renounced"))
        else:
            out.append(("2.6_ownership", "REJECT", f"owner active {owner[:12]}…"))

        # Chain-dependent fields: absent on Base, present on Ethereum/BSC
        three("2.8_honeypot", "is_honeypot", "0", "honeypot")
        three("2.1_mintable", "is_mintable", "0", "mintable")
        three("2.2_pausable_transfer", "transfer_pausable", "0", "pausable")
        three("2.5_owner_can_take_back", "owner_change_balance", "0",
              "owner_change_balance")
        three("2.11_blacklist_fn", "is_blacklisted", "0", "blacklist")

        lp = d.get("lp_holders")
        if lp is None:
            out.append(("2.3_lp_locked", "UNKNOWN", "lp_holders not covered"))
        elif not lp:
            out.append(("2.3_lp_locked", "UNKNOWN", "no lp holder data"))
        else:
            # SUM the locked share. The previous version required a single
            # holder above 90%, so a pool split across ten fully-locked
            # holders failed the gate — a false rejection, not a real risk.
            locked_pct, burned_pct = 0.0, 0.0
            for h in lp:
                try:
                    pct = float(h.get("percent") or 0) * 100
                except (TypeError, ValueError):
                    continue
                addr = str(h.get("address", "")).lower()
                if addr in ("0x0000000000000000000000000000000000000000",
                            "0x000000000000000000000000000000000000dead"):
                    burned_pct += pct
                elif str(h.get("is_locked")) == "1":
                    locked_pct += pct
            secured = locked_pct + burned_pct
            out.append(("2.3_lp_locked",
                        "PASS" if secured >= 90.0 else "REJECT",
                        f"{secured:.1f}% secured "
                        f"({locked_pct:.1f}% locked + {burned_pct:.1f}% burned) "
                        f"across {len(lp)} holders"))

        return out


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
