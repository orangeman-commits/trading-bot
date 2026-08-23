#!/usr/bin/env python3
"""
Safety data layer. Resolves the RULES.md §2 gates that previously returned
UNKNOWN by sourcing them from RugCheck (api.rugcheck.xyz/v1).

Free tier works unauthenticated with tight rate limits. Set RUGCHECK_API_KEY
(sent as X-API-KEY) for production use.

Field names in the RugCheck report have changed before. verify_schema() prints
what a live report actually contains — run it once before trusting the gates.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

LOG = logging.getLogger("safety")

BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


class RugCheck:
    BASE = "https://api.rugcheck.xyz/v1"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15):
        self.s = requests.Session()
        self.timeout = timeout
        key = api_key or os.getenv("RUGCHECK_API_KEY")
        if key:
            self.s.headers["X-API-KEY"] = key
        self._cache: dict[str, tuple[float, dict]] = {}

    def report(self, mint: str, max_age_s: int = 300) -> Optional[dict]:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < max_age_s:
            return hit[1]
        for attempt in range(3):
            try:
                r = self.s.get(f"{self.BASE}/tokens/{mint}/report",
                               timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                data = r.json()
                self._cache[mint] = (time.time(), data)
                return data
            except Exception as e:  # noqa: BLE001
                LOG.debug("rugcheck %s attempt %d: %s", mint[:8], attempt + 1, e)
                time.sleep(1 + attempt)
        return None


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class ReportView:
    """Tolerant accessor. RugCheck's schema varies by token type and has
    changed across versions, so every field has fallbacks and a None path.
    None always means 'unknown', which callers must treat as a rejection."""

    def __init__(self, report: dict):
        self.r = report or {}

    # -- gate 2.3 ---------------------------------------------------------
    @staticmethod
    def _lp_has_real_data(lp: dict) -> bool:
        """True if RugCheck actually measured this pool's LP, not just that
        the fields exist and happen to be zero.

        Found on BULLSHIT: the single largest-liquidity pool ($320k) had
        lpLockedPct=0 with lpLocked=0, lpMaxSupply=0, lpTotalSupply=0 — every
        LP field zero. That is an unindexed pool, not a measured unlocked
        one. Treating it as "0% locked" reported a false negative on a token
        whose actual LP (in a smaller, $231k pool) was 100% locked.
        """
        return _num(lp.get("lpTotalSupply")) > 0 or _num(lp.get("lpMaxSupply")) > 0

    def primary_market(self) -> Optional[dict]:
        """Highest-liquidity pool that RugCheck actually has LP data for.

        Ranking by liquidity alone (the previous version) can select a pool
        RugCheck never indexed, which reports as a false 0%. Ranking without
        excluding empty-data pools, or taking a naive max across all markets
        (the pre-BONK-fix version), both fail in opposite directions. This
        filters to measured pools first, then takes the highest-liquidity
        one among those.
        """
        markets = self.r.get("markets") or []
        best, best_liq = None, -1.0
        for m in markets:
            lp = m.get("lp") or {}
            if not self._lp_has_real_data(lp):
                continue
            liq = _num(lp.get("baseUSD")) + _num(lp.get("quoteUSD"))
            if liq > best_liq:
                best, best_liq = m, liq
        return best

    def lp_locked_pct(self) -> Optional[float]:
        """Liquidity-weighted lock percentage across every pool RugCheck has
        real LP data for — not just the single largest one.

        A single "primary pool" is still vulnerable to the exact failure just
        found: if the biggest pool happens to be unindexed, only the next
        pool down is even visible. Weighting by liquidity across every
        MEASURED pool means one unindexed giant can no longer hide a real,
        smaller, verified lock — and a tiny locked pool still cannot outvote
        a large unlocked one, which was the original BONK problem.
        """
        markets = self.r.get("markets") or []
        total_liq, locked_liq = 0.0, 0.0
        measured_any = False
        for m in markets:
            lp = m.get("lp") or {}
            if not self._lp_has_real_data(lp):
                continue
            liq = _num(lp.get("baseUSD")) + _num(lp.get("quoteUSD"))
            if liq <= 0:
                continue
            pct = None
            for key in ("lpLockedPct", "lpBurnPct"):
                if key in lp:
                    v = _num(lp[key], -1)
                    if v >= 0:
                        pct = v
                        break
            if pct is None:
                continue
            measured_any = True
            total_liq += liq
            locked_liq += liq * pct / 100
        if not measured_any or total_liq <= 0:
            return None
        return locked_liq / total_liq * 100

    def primary_liquidity_usd(self) -> float:
        """Total liquidity across pools RugCheck has real LP data for."""
        markets = self.r.get("markets") or []
        total = 0.0
        for m in markets:
            lp = m.get("lp") or {}
            if self._lp_has_real_data(lp):
                total += _num(lp.get("baseUSD")) + _num(lp.get("quoteUSD"))
        return total

    # -- gates 2.4 / 2.5 --------------------------------------------------
    # RugCheck labels addresses it recognises. AMM vaults, LP accounts and
    # burn addresses are not whales — counting them inflates concentration and
    # rejects healthy tokens.
    NON_WHALE_LABELS = ("amm", "lp", "burn", "market", "pool", "vault",
                        "raydium", "orca", "meteora", "pump")

    def _known_accounts(self) -> dict:
        ka = self.r.get("knownAccounts")
        return ka if isinstance(ka, dict) else {}

    def _is_non_whale(self, address: str, owner: str) -> bool:
        known = self._known_accounts()
        for addr in (address, owner):
            if not addr:
                continue
            if addr in BURN_ADDRESSES:
                return True
            entry = known.get(addr)
            if isinstance(entry, dict):
                blob = f"{entry.get('name','')} {entry.get('type','')}".lower()
                if any(k in blob for k in self.NON_WHALE_LABELS):
                    return True
        return False

    def holder_concentration(self) -> tuple[Optional[float], Optional[float]]:
        """Top-10 and largest holder percentages, excluding pools and burns.

        Source is the RugCheck report already in hand. The previous
        implementation called getTokenLargestAccounts plus one getAccountInfo
        per holder — roughly 21 RPC calls per token, which the public Solana
        endpoint rate-limits into returning nothing at all.
        """
        holders = self.r.get("topHolders")
        if not isinstance(holders, list) or not holders:
            return None, None
        pcts = []
        for h in holders:
            if not isinstance(h, dict):
                continue
            if self._is_non_whale(h.get("address", ""), h.get("owner", "")):
                continue
            pct = _num(h.get("pct"), -1)
            if pct >= 0:
                pcts.append(pct)
        if not pcts:
            return None, None
        pcts.sort(reverse=True)
        return sum(pcts[:10]), pcts[0]

    def insider_pct(self) -> float:
        """Share held by wallets RugCheck flags as insiders."""
        total = 0.0
        for h in (self.r.get("topHolders") or []):
            if isinstance(h, dict) and h.get("insider") is True:
                total += _num(h.get("pct"))
        return total

    # -- gate 2.6 ---------------------------------------------------------
    def creator_pct(self) -> Optional[float]:
        bal = self.r.get("creatorBalance")
        supply = _num((self.r.get("token") or {}).get("supply"), 0)
        if bal is None or supply <= 0:
            return None
        return _num(bal) / supply * 100

    # -- gate 2.9 ---------------------------------------------------------
    def transfer_fee_pct(self) -> Optional[float]:
        """None when the field is absent.

        Previously returned 0.0 for a missing transferFee, so a schema change
        would silently report 'no tax' and PASS the gate. Absent data must
        never resolve to the safe value.
        """
        fee = self.r.get("transferFee")
        if not isinstance(fee, dict) or "pct" not in fee:
            return None
        return _num(fee.get("pct"), -1) if _num(fee.get("pct"), -1) >= 0 else None

    # -- authorities (cross-check against RPC) ----------------------------
    def authorities_clear(self) -> Optional[bool]:
        tok = self.r.get("token") or {}
        if "mintAuthority" not in tok and "freezeAuthority" not in tok:
            return None
        return not tok.get("mintAuthority") and not tok.get("freezeAuthority")

    # -- aggregate risk ---------------------------------------------------
    # Substring match against risk names that should hard-block regardless of
    # score. Everything else feeds the normalised score instead: RugCheck
    # flags "danger" liberally, and established tokens (BONK included) carry
    # danger entries. Rejecting on any danger flag rejects nearly everything.
    BLOCKING = ("honeypot", "mint authority", "freeze authority",
                "copycat", "cannot sell", "transfer fee")

    def danger_risks(self) -> list[str]:
        return [x.get("name", "?") for x in (self.r.get("risks") or [])
                if str(x.get("level", "")).lower() in ("danger", "high", "critical")]

    def blocking_risks(self) -> list[str]:
        out = []
        for x in (self.r.get("risks") or []):
            name = str(x.get("name", ""))
            if any(b in name.lower() for b in self.BLOCKING):
                out.append(name)
        return out

    def risk_score(self) -> Optional[float]:
        """RugCheck's normalised risk score, 0-100. Higher is worse."""
        v = self.r.get("score_normalised")
        return _num(v, -1) if v is not None else None

    def rugged(self) -> Optional[bool]:
        """None when absent — a missing flag is not a clean bill of health."""
        v = self.r.get("rugged")
        return None if v is None else bool(v)


def verify_schema(mint: str) -> None:
    """Run this once against a known token before trusting any gate."""
    rc = RugCheck()
    rep = rc.report(mint)
    if not rep:
        print("no report returned — check network, key, or mint address")
        return
    v = ReportView(rep)
    top10, largest = v.holder_concentration()
    print(f"top-level keys : {sorted(rep.keys())}")
    print(f"markets        : {len(rep.get('markets') or [])}")
    print(f"primary liq    : ${v.primary_liquidity_usd():,.0f}")
    print(f"lp_locked_pct  : {v.lp_locked_pct()}  (primary pool only)")
    print(f"risk_score     : {v.risk_score()}  (0-100, higher is worse)")
    print(f"blocking risks : {v.blocking_risks()}")
    print(f"top10 / largest: {top10} / {largest}")
    print(f"insider pct    : {v.insider_pct():.2f}%")
    print(f"creator_pct    : {v.creator_pct()}")
    print(f"transfer_fee   : {v.transfer_fee_pct()}")
    print(f"authorities_ok : {v.authorities_clear()}")
    print(f"danger risks   : {v.danger_risks()}")
    print(f"rugged flag    : {v.rugged()}")
    print("\nAny None above means that gate will reject every token until the "
          "field mapping in ReportView is corrected.")


if __name__ == "__main__":
    import sys
    verify_schema(sys.argv[1] if len(sys.argv) > 1
                  else "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
