#!/usr/bin/env python3
"""
Uniswap V3 LP security check.

WHY THIS EXISTS
---------------
Confirmed live: GoPlus's `lp_holders` field is None for a V3 pool — not
stale, not partial, simply absent. GoPlus's LP model is fungible-token
shaped; a V3 position is an ERC-721 NFT held by the NonfungiblePositionManager,
so there is no fungible LP balance for GoPlus to report. Every V3 token was
therefore scoring "0.0% secured" regardless of whether its liquidity was
genuinely locked, burned, or fully exposed. This module answers the question
GoPlus structurally cannot: who currently owns the position NFT.

DESIGN CONSTRAINT — NO HARDCODED LOCKER ALLOWLIST
--------------------------------------------------
A previous version of this idea (v3_lp_lock.py, from an unverified source)
hardcoded specific "known locker" contract addresses and treated positions
held by them as SECURED. That is exactly the wrong failure mode for a
security check: a wrong or planted address in that list produces a false
SECURED — a token that can be rugged reads as safe. This module makes only
two claims, both checkable from the chain alone with zero trust in any
third-party address list:

    SECURED    position NFT owner is a burn address (0xdead / 0x0).
               This is unrecoverable by construction — no key can move it.
    UNSECURED  position NFT owner is a normal wallet (EOA).
               Pullable at will by whoever holds that key.
    UNVERIFIED position NFT owner is SOME OTHER CONTRACT (could be a
               legitimate third-party locker, could be a router, could be
               anything). We do not know, so we do not guess. Per the
               rulebook, missing data is a rejection — but it must be
               reported as UNVERIFIED, distinguishable from a confirmed
               UNSECURED, not silently folded into either.

Requires: pip install web3
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger("v3lp")

BURN_ADDRESSES = {
    "0x000000000000000000000000000000000000dEaD",
    "0x0000000000000000000000000000000000000000",
}

# NonfungiblePositionManager — verified per-chain from the block explorer for
# each chain (BscScan / BaseScan / Etherscan), NOT assumed to be a shared
# address. Uniswap deploys this contract separately on every chain; reusing
# one address across chains would silently query the wrong contract.
NFPM_ADDRESSES = {
    1:     "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",   # Ethereum
    56:    "0x7b8A01B39D58278b5DE7e48c8449c9f4F5170613",   # BSC
    8453:  "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",   # Base
    # Robinhood Chain (4663): deliberately UNSET. Not verified against
    # robinhoodchain.blockscout.com. A wrong address here queries an
    # arbitrary contract and produces meaningless results — set only after
    # confirming it yourself:
    #   4663: "0x...",
}

POOL_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint128"}]},
]

NFPM_ABI = [
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"type": "address"}]},
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [
         {"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
         {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
         {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
         {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"},
         {"name": "feeGrowthInside0LastX128", "type": "uint256"},
         {"name": "feeGrowthInside1LastX128", "type": "uint256"},
         {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"},
     ]},
    {"anonymous": False, "name": "Transfer", "type": "event",
     "inputs": [
         {"indexed": True, "name": "from", "type": "address"},
         {"indexed": True, "name": "to", "type": "address"},
         {"indexed": True, "name": "tokenId", "type": "uint256"},
     ]},
]


@dataclass
class PositionState:
    token_id: int
    owner: str
    liquidity: int
    status: str            # SECURED / UNSECURED / UNVERIFIED


@dataclass
class V3LpResult:
    status: str             # SECURED / UNSECURED / UNVERIFIED / NOT_V3
    detail: str
    secured_pct: Optional[float] = None
    positions: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "SECURED"

    def as_gate(self) -> tuple:
        """(name, verdict, detail) matching the rest of the safety layer."""
        verdict = {"SECURED": "PASS", "UNSECURED": "REJECT",
                   "UNVERIFIED": "UNKNOWN", "NOT_V3": "UNKNOWN"}[self.status]
        return ("2.3_lp_locked_v3", verdict, self.detail)


class V3LpChecker:
    def __init__(self, chain_id: int, rpc_url: Optional[str] = None):
        from web3 import Web3
        self.chain_id = chain_id
        self.nfpm_addr = NFPM_ADDRESSES.get(chain_id)
        env_key = {1: "ETH_RPC", 56: "BSC_RPC", 8453: "BASE_RPC",
                  4663: "ROBINHOOD_RPC"}.get(chain_id, "")
        self.rpc = rpc_url or os.getenv(env_key)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 20})) \
            if self.rpc else None

    def ready(self) -> tuple:
        if not self.w3:
            return False, "no RPC configured for this chain"
        if not self.nfpm_addr:
            return False, (
                f"NonfungiblePositionManager not verified for chain "
                f"{self.chain_id}. Confirm the address on that chain's "
                f"block explorer and add it to NFPM_ADDRESSES — never guess.")
        try:
            if not self.w3.is_connected():
                return False, "RPC unreachable"
        except Exception as e:  # noqa: BLE001
            return False, f"RPC error: {e}"
        return True, "ready"

    # ── single position, when you already have the tokenId ─────────────
    def check_position(self, token_id: int) -> PositionState:
        from web3 import Web3
        nfpm = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.nfpm_addr), abi=NFPM_ABI)
        owner = Web3.to_checksum_address(nfpm.functions.ownerOf(token_id).call())
        liquidity = nfpm.functions.positions(token_id).call()[7]

        if owner in {Web3.to_checksum_address(a) for a in BURN_ADDRESSES}:
            status = "SECURED"
        elif len(self.w3.eth.get_code(owner)) == 0:
            status = "UNSECURED"     # plain wallet — pullable at will
        else:
            status = "UNVERIFIED"    # some other contract — no allowlist, no guess
        return PositionState(token_id, owner, liquidity, status)

    # ── discover positions for a pool (best-effort) ─────────────────────
    def discover_positions(self, pool_address: str,
                           lookback_blocks: int = 300_000) -> list:
        """Finds position NFTs minted for this specific pool by scanning
        Transfer(from=0x0, ...) mint events on the position manager and
        matching each candidate's (token0, token1, fee) against the pool.

        Best-effort, not exhaustive: bounded by lookback_blocks and by
        whatever log range the RPC allows in one call. A position minted
        earlier than the lookback window will be missed. This under-counts
        rather than over-counts — consistent with the fail-closed rule: if
        we cannot see a position, we do not credit it as secured.
        """
        from web3 import Web3
        pool = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
        try:
            t0 = pool.functions.token0().call()
            t1 = pool.functions.token1().call()
            fee = pool.functions.fee().call()
        except Exception as e:  # noqa: BLE001
            return []

        nfpm = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.nfpm_addr), abi=NFPM_ABI)
        latest = self.w3.eth.block_number
        start = max(0, latest - lookback_blocks)

        try:
            logs = nfpm.events.Transfer().get_logs(
                fromBlock=start, toBlock=latest,
                argument_filters={"from": "0x0000000000000000000000000000000000000000"})
        except Exception as e:  # noqa: BLE001
            LOG.warning("log scan failed (RPC log-range limit?): %s", e)
            return []

        out = []
        for log in logs:
            tid = log["args"]["tokenId"]
            try:
                pos = nfpm.functions.positions(tid).call()
            except Exception:  # noqa: BLE001
                continue
            if pos[2] == t0 and pos[3] == t1 and pos[4] == fee:
                out.append(tid)
        return out

    # ── aggregate check for a pool ──────────────────────────────────────
    def check_pool(self, pool_address: str) -> V3LpResult:
        ok, msg = self.ready()
        if not ok:
            return V3LpResult("UNVERIFIED", msg)

        try:
            token_ids = self.discover_positions(pool_address)
        except Exception as e:  # noqa: BLE001
            return V3LpResult("UNVERIFIED", f"position discovery failed: {e}")

        if not token_ids:
            return V3LpResult(
                "UNVERIFIED",
                "no position NFTs found for this pool in the scanned block "
                "range — could be older than the lookback window, or this "
                "pool routes through a non-standard router")

        positions = [self.check_position(tid) for tid in token_ids]
        total_liq = sum(p.liquidity for p in positions)
        secured_liq = sum(p.liquidity for p in positions if p.status == "SECURED")
        unverified_liq = sum(p.liquidity for p in positions if p.status == "UNVERIFIED")

        if total_liq == 0:
            return V3LpResult("UNVERIFIED", "positions found but all report "
                              "zero liquidity", positions=positions)

        secured_pct = secured_liq / total_liq * 100
        unverified_pct = unverified_liq / total_liq * 100

        if secured_pct >= 90:
            status, detail = "SECURED", (
                f"{secured_pct:.1f}% of liquidity in burned positions "
                f"across {len(positions)} position(s)")
        elif unverified_pct > 0 and secured_pct + unverified_pct >= 90:
            status, detail = "UNVERIFIED", (
                f"{secured_pct:.1f}% burned + {unverified_pct:.1f}% held by "
                f"unrecognised contracts (could be a locker, unconfirmed) "
                f"across {len(positions)} position(s)")
        else:
            status, detail = "UNSECURED", (
                f"only {secured_pct:.1f}% burned across {len(positions)} "
                f"position(s) — the rest is held by wallets that can "
                f"withdraw at will")
        return V3LpResult(status, detail, secured_pct, positions)


def check(chain_id: int, pool_address: str, rpc_url: Optional[str] = None) -> V3LpResult:
    return V3LpChecker(chain_id, rpc_url).check_pool(pool_address)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("usage: python v3_positions.py <chain_id> <pool_address>")
        print("  chain_id: 1=eth 56=bsc 8453=base")
        print("  pool_address: the specific V3 pool, e.g. from GeckoTerminal's "
             "primary_pool.address")
        raise SystemExit(1)
    r = check(int(sys.argv[1]), sys.argv[2])
    print(f"\nstatus:  {r.status}")
    print(f"detail:  {r.detail}")
    if r.secured_pct is not None:
        print(f"secured: {r.secured_pct:.1f}%")
    for p in r.positions:
        print(f"  #{p.token_id}  {p.status:10}  owner={p.owner}  "
             f"liquidity={p.liquidity}")
