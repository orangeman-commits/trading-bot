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
    def lp_locked_pct(self) -> Optional[float]:
        markets = self.r.get("markets") or []
        best = None
        for m in markets:
            lp = m.get("lp") or {}
            for key in ("lpLockedPct", "lpLocked", "lpBurnPct"):
                if key in lp:
                    val = _num(lp[key], -1)
                    if val >= 0:
                        best = val if best is None else max(best, val)
                    break
        if best is None:
            for key in ("totalLPProvidersPct", "lpLockedPct"):
                if key in self.r:
                    best = _num(self.r[key], -1)
                    if best < 0:
                        best = None
        return best

    # -- gates 2.4 / 2.5 --------------------------------------------------
    def holder_concentration(self) -> tuple[Optional[float], Optional[float]]:
        holders = self.r.get("topHolders") or []
        if not holders:
            return None, None
        pcts = []
        for h in holders:
            if h.get("insider") is True and h.get("owner") in (None, ""):
                continue
            pct = _num(h.get("pct"), -1)
            if pct >= 0:
                pcts.append(pct)
        if not pcts:
            return None, None
        pcts.sort(reverse=True)
        return sum(pcts[:10]), pcts[0]

    # -- gate 2.6 ---------------------------------------------------------
    def creator_pct(self) -> Optional[float]:
        bal = self.r.get("creatorBalance")
        supply = _num((self.r.get("token") or {}).get("supply"), 0)
        if bal is None or supply <= 0:
            return None
        return _num(bal) / supply * 100

    # -- gate 2.9 ---------------------------------------------------------
    def transfer_fee_pct(self) -> float:
        fee = self.r.get("transferFee") or {}
        return _num(fee.get("pct"), 0.0)

    # -- authorities (cross-check against RPC) ----------------------------
    def authorities_clear(self) -> Optional[bool]:
        tok = self.r.get("token") or {}
        if "mintAuthority" not in tok and "freezeAuthority" not in tok:
            return None
        return not tok.get("mintAuthority") and not tok.get("freezeAuthority")

    # -- aggregate risk ---------------------------------------------------
    def danger_risks(self) -> list[str]:
        return [x.get("name", "?") for x in (self.r.get("risks") or [])
                if str(x.get("level", "")).lower() in ("danger", "high", "critical")]

    def rugged(self) -> bool:
        return bool(self.r.get("rugged"))


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
    print(f"lp_locked_pct  : {v.lp_locked_pct()}")
    print(f"top10 / largest: {top10} / {largest}")
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
