#!/usr/bin/env python3
"""
Rule-based low-cap token scanner and position manager.
Implements RULES.md v1.0. Solana adapter complete; Base requires an EVM adapter.

Defaults to PAPER mode. Live trading is intentionally not implemented — see
LiveBroker. Read RULES.md before changing any threshold in Config.

    python sniper_bot.py --once      # single scan cycle, prints decisions
    python sniper_bot.py             # continuous loop
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

LAMPORTS_PER_SOL = 1_000_000_000

import requests

LOG = logging.getLogger("bot")


def app_data_dir() -> Path:
    """Writable per-user directory for state.

    A packaged .app or .exe launches with its working directory set somewhere
    read-only, so a relative db path fails with "unable to open database file".
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "TradingBot"
    elif sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home())) / "TradingBot"
    else:
        base = Path(os.getenv("XDG_DATA_HOME",
                              Path.home() / ".local" / "share")) / "TradingBot"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        import tempfile
        return Path(tempfile.gettempdir())

BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

# Populate from a maintained list. Incomplete coverage inflates apparent
# holder concentration and causes false rejections (safe direction).
KNOWN_CEX_ADDRESSES: set[str] = set()


# ─────────────────────────────── config ────────────────────────────────────

@dataclass
class Config:
    # -- mode -------------------------------------------------------------
    mode: str = "PAPER"                      # PAPER | LIVE  (rule 8.1)
    capital_usd: float = 10_000.0

    # -- endpoints --------------------------------------------------------
    solana_rpc: str = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
    dexscreener: str = "https://api.dexscreener.com"
    jupiter_quote: str = "https://api.jup.ag/swap/v1/quote"  # v6/lite-api deprecated

    # -- §1 discovery -----------------------------------------------------
    # Age is no longer a strategy filter. The floor stays small and non-zero
    # because a pair minutes old has an unreadable holder graph and no sell
    # history — set to 0 to disable entirely.
    min_pair_age_min: int = 15
    max_pair_age_hr: int = 0        # 0 = no upper limit
    min_liquidity_usd: float = 40_000
    min_fdv_usd: float = 150_000
    max_fdv_usd: float = 0          # 0 = no upper limit
    min_volume_24h_usd: float = 150_000
    min_vol_liq_ratio: float = 1.5
    max_vol_liq_ratio: float = 25.0
    min_unique_traders_24h: int = 250
    min_buy_sell_ratio: float = 0.8
    max_buy_sell_ratio: float = 3.0
    allowed_quotes: tuple = ("SOL", "WETH", "ETH", "USDC", "USDT", "WSOL")

    # -- §2 safety --------------------------------------------------------
    min_lp_burned_pct: float = 90.0
    max_top10_pct: float = 25.0
    max_single_holder_pct: float = 8.0
    max_insider_pct: float = 15.0
    max_deployer_pct: float = 5.0
    max_round_trip_tax_pct: float = 5.0
    max_exit_price_impact_pct: float = 4.0
    max_risk_score: float = 60.0   # RugCheck normalised, higher is worse
    strict_lp_check: bool = True             # UNKNOWN LP status => reject

    # -- §3 scoring -------------------------------------------------------
    min_score: int = 62
    min_signal_coverage: float = 50.0   # must match analyze.py
    min_cohort_wallets: int = 3
    cohort_enabled: bool = False       # True once a verified wallet list exists
    sentiment_spike_multiple: float = 8.0

    # -- §4 sizing --------------------------------------------------------
    base_position_pct: float = 2.0
    max_position_pct: float = 3.0
    max_pct_of_liquidity: float = 0.5
    max_concurrent_positions: int = 5
    max_deployed_pct: float = 20.0

    # -- §5 entry ---------------------------------------------------------
    max_slippage_pct: float = 3.0
    max_chase_pct: float = 8.0

    # -- §6 exits ---------------------------------------------------------
    tp1_gain_pct: float = 100.0
    tp1_sell_pct: float = 50.0
    tp2_gain_pct: float = 300.0
    tp2_sell_pct: float = 25.0
    trailing_stop_pct: float = 35.0
    hard_stop_pct: float = -35.0
    time_stop_hours: int = 24
    time_stop_min_gain_pct: float = 20.0
    volume_death_pct: float = 15.0
    liq_drop_exit_pct: float = 25.0
    insider_dump_drop_pct: float = 5.0
    emergency_impact_pct: float = 10.0

    # -- §7 breakers ------------------------------------------------------
    daily_loss_halt_pct: float = -10.0
    consecutive_loss_halt: int = 4
    weekly_drawdown_halt_pct: float = -20.0
    total_drawdown_stop_pct: float = -35.0
    max_feed_staleness_sec: int = 90

    # -- loop -------------------------------------------------------------
    scan_interval_sec: int = 120
    exit_check_interval_sec: int = 30
    db_path: str = field(default_factory=lambda: str(app_data_dir() / "bot_state.db"))
    halt_file: str = field(default_factory=lambda: str(app_data_dir() / "HALT"))


# ──────────────────────────────── models ───────────────────────────────────

class Verdict(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass
class GateResult:
    name: str
    verdict: Verdict
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.PASS


@dataclass
class Candidate:
    chain: str
    mint: str
    symbol: str
    pair_address: str
    price_usd: float
    liquidity_usd: float
    fdv_usd: float
    volume_24h: float
    txns_buys_24h: int
    txns_sells_24h: int
    pair_created_ms: int
    quote_symbol: str
    raw: dict = field(default_factory=dict)

    @property
    def age_minutes(self) -> float:
        return (time.time() * 1000 - self.pair_created_ms) / 60_000

    @property
    def vol_liq_ratio(self) -> float:
        return self.volume_24h / self.liquidity_usd if self.liquidity_usd else 0.0

    @property
    def buy_sell_ratio(self) -> float:
        return self.txns_buys_24h / max(self.txns_sells_24h, 1)


@dataclass
class Position:
    mint: str
    symbol: str
    chain: str
    pair_address: str
    decimals: int
    entry_price: float
    entry_time: float
    qty: float
    cost_usd: float
    entry_liquidity: float
    entry_hourly_volume: float
    high_water_price: float
    tp1_done: bool = False
    tp2_done: bool = False
    realised_usd: float = 0.0
    failed_exits: int = 0
    entry_top10: float = 0.0

    def gain_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100

    def hours_held(self) -> float:
        return (time.time() - self.entry_time) / 3600


# ──────────────────────────── data clients ─────────────────────────────────

class HttpClient:
    def __init__(self, timeout: int = 12):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "rule-bot/1.0"
        self.timeout = timeout

    def get_json(self, url: str, **kw) -> Optional[dict]:
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001
                LOG.debug("GET %s failed (%s/3): %s", url, attempt + 1, e)
                time.sleep(1 + attempt)
        return None

    def post_json(self, url: str, payload: dict) -> Optional[dict]:
        try:
            r = self.s.post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            LOG.debug("POST %s failed: %s", url, e)
            return None


class DexScreener:
    """Pair discovery and live pricing.

    Endpoints drift. Verify against docs.dexscreener.com before live use.
    """

    def __init__(self, http: HttpClient, cfg: Config):
        self.http, self.cfg = http, cfg

    def search(self, query: str) -> list[Candidate]:
        data = self.http.get_json(f"{self.cfg.dexscreener}/latest/dex/search",
                                  params={"q": query})
        if not data:
            return []
        return [c for c in (self._parse(p) for p in data.get("pairs") or []) if c]

    def pair(self, chain: str, pair_address: str) -> Optional[Candidate]:
        data = self.http.get_json(
            f"{self.cfg.dexscreener}/latest/dex/pairs/{chain}/{pair_address}")
        pairs = (data or {}).get("pairs") or []
        return self._parse(pairs[0]) if pairs else None

    @staticmethod
    def _parse(p: dict) -> Optional[Candidate]:
        try:
            txns = p.get("txns", {}).get("h24", {}) or {}
            return Candidate(
                chain=p["chainId"],
                mint=p["baseToken"]["address"],
                symbol=p["baseToken"].get("symbol", "?"),
                pair_address=p["pairAddress"],
                price_usd=float(p.get("priceUsd") or 0),
                liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
                fdv_usd=float(p.get("fdv") or 0),
                volume_24h=float((p.get("volume") or {}).get("h24") or 0),
                txns_buys_24h=int(txns.get("buys") or 0),
                txns_sells_24h=int(txns.get("sells") or 0),
                pair_created_ms=int(p.get("pairCreatedAt") or 0),
                quote_symbol=(p.get("quoteToken") or {}).get("symbol", "?"),
                raw=p,
            )
        except (KeyError, TypeError, ValueError):
            return None


class SolanaRPC:
    def __init__(self, http: HttpClient, cfg: Config):
        self.http, self.cfg = http, cfg
        self._id = 0

    def _call(self, method: str, params: list) -> Optional[Any]:
        self._id += 1
        res = self.http.post_json(self.cfg.solana_rpc, {
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if res is None or "error" in res:
            LOG.debug("RPC %s error: %s", method, (res or {}).get("error"))
            return None
        return res.get("result")

    def mint_info(self, mint: str) -> Optional[dict]:
        """Returns mintAuthority, freezeAuthority, supply, decimals."""
        res = self._call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        try:
            return res["value"]["data"]["parsed"]["info"]
        except (TypeError, KeyError):
            return None

    def largest_holders(self, mint: str) -> Optional[list[dict]]:
        res = self._call("getTokenLargestAccounts", [mint])
        return (res or {}).get("value") if res else None

    def owner_of(self, token_account: str) -> Optional[str]:
        res = self._call("getAccountInfo", [token_account, {"encoding": "jsonParsed"}])
        try:
            return res["value"]["data"]["parsed"]["info"]["owner"]
        except (TypeError, KeyError):
            return None


class Jupiter:
    """Quotes — used for pricing, sell simulation, and price-impact checks."""

    SOL = "So11111111111111111111111111111111111111112"

    def __init__(self, http: HttpClient, cfg: Config):
        self.http, self.cfg = http, cfg

    def quote(self, input_mint: str, output_mint: str, amount: int,
              slippage_bps: int = 300) -> Optional[dict]:
        return self.http.get_json(self.cfg.jupiter_quote, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
        })

    def can_sell(self, mint: str, raw_amount: int) -> tuple[bool, float, str]:
        """Rule 2.8 / 2.10 — simulate selling the FULL position, not dust."""
        q = self.quote(mint, self.SOL, raw_amount)
        if not q or not q.get("outAmount"):
            return False, 100.0, "no sell route for full size"
        try:
            impact = abs(float(q.get("priceImpactPct", 0))) * 100
        except (TypeError, ValueError):
            return False, 100.0, "unparseable price impact"
        return True, impact, f"impact {impact:.2f}%"


# ─────────────────────────── safety screen §2 ──────────────────────────────

class SafetyScreen:
    def __init__(self, rpc: SolanaRPC, jup: Jupiter, cfg: Config, rugcheck=None):
        self.rpc, self.jup, self.cfg = rpc, jup, cfg
        self.rugcheck = rugcheck

    def run(self, cand: Candidate, intended_raw_amount: int) -> list[GateResult]:
        if cand.chain != "solana":
            return [GateResult("chain_adapter", Verdict.UNKNOWN,
                               f"no safety adapter for {cand.chain}")]

        results: list[GateResult] = []
        info = self.rpc.mint_info(cand.mint)
        if not info:
            return [GateResult("mint_info", Verdict.UNKNOWN, "mint account unreadable")]

        # 2.1 / 2.2 — authorities must be revoked
        results.append(GateResult(
            "2.1_mint_authority",
            Verdict.PASS if info.get("mintAuthority") is None else Verdict.REJECT,
            str(info.get("mintAuthority"))))
        results.append(GateResult(
            "2.2_freeze_authority",
            Verdict.PASS if info.get("freezeAuthority") is None else Verdict.REJECT,
            str(info.get("freezeAuthority"))))

        # 2.4 / 2.5 — holder concentration (from the RugCheck report)
        results.extend(self._concentration_gates(cand, info))

        # 2.8 / 2.10 — full-size sell simulation
        ok, impact, detail = self.jup.can_sell(cand.mint, intended_raw_amount)
        results.append(GateResult("2.8_sell_simulation",
                                  Verdict.PASS if ok else Verdict.REJECT, detail))
        results.append(GateResult(
            "2.10_exit_price_impact",
            Verdict.PASS if ok and impact < self.cfg.max_exit_price_impact_pct
            else Verdict.REJECT, f"{impact:.2f}%"))

        results.extend(self._rugcheck_gates(cand))
        return results

    def _rugcheck_gates(self, cand: Candidate) -> list[GateResult]:
        """Gates 2.3, 2.6, 2.9 and the aggregate risk check, from RugCheck."""
        from safety_data import ReportView

        rep = self.rugcheck.report(cand.mint) if self.rugcheck else None
        if not rep:
            return [GateResult("2.x_rugcheck", Verdict.UNKNOWN, "no report")]
        v = ReportView(rep)
        out: list[GateResult] = []

        rugged = v.rugged()
        out.append(GateResult(
            "2.0_rugged_flag",
            Verdict.UNKNOWN if rugged is None else
            (Verdict.REJECT if rugged else Verdict.PASS),
            "unknown" if rugged is None else str(rugged)))

        blocking = v.blocking_risks()
        out.append(GateResult("2.0_blocking_risks",
                              Verdict.REJECT if blocking else Verdict.PASS,
                              ", ".join(blocking) or "none"))

        rs = v.risk_score()
        out.append(GateResult(
            "2.0_risk_score",
            Verdict.UNKNOWN if rs is None else
            (Verdict.PASS if rs <= self.cfg.max_risk_score else Verdict.REJECT),
            "unknown" if rs is None else f"{rs:.0f}/100"))

        lp = v.lp_locked_pct()
        if lp is None and not self.cfg.strict_lp_check:
            LOG.warning("%s: LP lock unknown, strict_lp_check disabled — "
                        "treating as advisory", cand.symbol)
        out.append(GateResult(
            "2.3_lp_burned",
            (Verdict.UNKNOWN if self.cfg.strict_lp_check else Verdict.PASS)
            if lp is None else
            (Verdict.PASS if lp >= self.cfg.min_lp_burned_pct else Verdict.REJECT),
            "unknown" if lp is None else f"{lp:.1f}%"))

        creator = v.creator_pct()
        out.append(GateResult(
            "2.6_deployer_balance",
            Verdict.UNKNOWN if creator is None else
            (Verdict.PASS if creator < self.cfg.max_deployer_pct else Verdict.REJECT),
            "unknown" if creator is None else f"{creator:.2f}%"))

        fee = v.transfer_fee_pct()
        out.append(GateResult(
            "2.9_transfer_tax",
            Verdict.UNKNOWN if fee is None else
            (Verdict.PASS if fee < self.cfg.max_round_trip_tax_pct else Verdict.REJECT),
            "unknown" if fee is None else f"{fee:.2f}%"))

        # 2.7 deployer history and 2.11 impersonation still need their own
        # datasets; RugCheck's danger risks partially cover 2.11.
        return out

    def _concentration_gates(self, cand: Candidate, info: dict) -> list[GateResult]:
        # Prefer RugCheck: the report is already fetched, carries owner and
        # insider labels, and costs zero extra RPC calls. The RPC path below
        # needs ~21 requests per token and the public endpoint throttles it
        # into failure, which is why this gate read UNKNOWN on every token.
        if self.rugcheck:
            from safety_data import ReportView
            rep = self.rugcheck.report(cand.mint)
            if rep:
                v = ReportView(rep)
                top10, largest = v.holder_concentration()
                if top10 is not None:
                    out = [
                        GateResult("2.4_top10_concentration",
                                   Verdict.PASS if top10 < self.cfg.max_top10_pct
                                   else Verdict.REJECT, f"{top10:.1f}%"),
                        GateResult("2.5_largest_holder",
                                   Verdict.PASS if largest < self.cfg.max_single_holder_pct
                                   else Verdict.REJECT, f"{largest:.1f}%"),
                    ]
                    ins = v.insider_pct()
                    if ins > 0:
                        out.append(GateResult(
                            "2.7_insider_holdings",
                            Verdict.PASS if ins < self.cfg.max_insider_pct
                            else Verdict.REJECT, f"{ins:.1f}%"))
                    return out

        holders = self.rpc.largest_holders(cand.mint)
        if not holders:
            return [GateResult("2.4_top10_concentration", Verdict.UNKNOWN,
                               "holder data unavailable")]
        try:
            supply = float(info["supply"])
        except (KeyError, TypeError, ValueError):
            return [GateResult("2.4_top10_concentration", Verdict.UNKNOWN, "no supply")]
        if supply <= 0:
            return [GateResult("2.4_top10_concentration", Verdict.UNKNOWN, "zero supply")]

        filtered: list[float] = []
        for h in holders:
            owner = self.rpc.owner_of(h["address"]) or h["address"]
            if owner in BURN_ADDRESSES or owner in KNOWN_CEX_ADDRESSES:
                continue
            filtered.append(float(h.get("amount", 0)) / supply * 100)

        top10 = sum(sorted(filtered, reverse=True)[:10])
        largest = max(filtered, default=0.0)
        return [
            GateResult("2.4_top10_concentration",
                       Verdict.PASS if top10 < self.cfg.max_top10_pct else Verdict.REJECT,
                       f"{top10:.1f}%"),
            GateResult("2.5_largest_holder",
                       Verdict.PASS if largest < self.cfg.max_single_holder_pct
                       else Verdict.REJECT, f"{largest:.1f}%"),
        ]


# ───────────────────────────── discovery §1 ────────────────────────────────

def discovery_filters(c: Candidate, cfg: Config) -> list[GateResult]:
    def gate(name: str, ok: bool, detail: str) -> GateResult:
        return GateResult(name, Verdict.PASS if ok else Verdict.REJECT, detail)

    age = c.age_minutes
    age_ok = age >= cfg.min_pair_age_min and (
        cfg.max_pair_age_hr <= 0 or age <= cfg.max_pair_age_hr * 60)
    return [
        gate("1.1_pair_age", age_ok,
             f"{age/24/60:.1f}d" if age > 1440 else f"{age:.0f}m"),
        gate("1.2_liquidity", c.liquidity_usd >= cfg.min_liquidity_usd,
             f"${c.liquidity_usd:,.0f}"),
        gate("1.3_fdv",
             c.fdv_usd >= cfg.min_fdv_usd and
             (cfg.max_fdv_usd <= 0 or c.fdv_usd <= cfg.max_fdv_usd),
             f"${c.fdv_usd:,.0f}"),
        gate("1.4_volume", c.volume_24h >= cfg.min_volume_24h_usd,
             f"${c.volume_24h:,.0f}"),
        gate("1.5_vol_liq_ratio",
             cfg.min_vol_liq_ratio <= c.vol_liq_ratio <= cfg.max_vol_liq_ratio,
             f"{c.vol_liq_ratio:.1f}x"),
        gate("1.6_unique_traders",
             (c.txns_buys_24h + c.txns_sells_24h) >= cfg.min_unique_traders_24h,
             f"{c.txns_buys_24h + c.txns_sells_24h:,} txns (proxy)"),
        gate("1.7_buy_sell_ratio",
             cfg.min_buy_sell_ratio <= c.buy_sell_ratio <= cfg.max_buy_sell_ratio,
             f"{c.buy_sell_ratio:.2f}"),
        gate("1.8_quote_asset", c.quote_symbol in cfg.allowed_quotes, c.quote_symbol),
    ]


# ────────────────────────────── scoring §3 ─────────────────────────────────

class Scorer:
    """Signals not backed by a real data source return 0, never a guess.
    A token cannot reach min_score on liquidity and volume alone — by design."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def score(self, c: Candidate,
              cohort_buys: int = 0,
              holder_growth_per_hr: Optional[float] = None,
              sentiment: Optional[dict] = None) -> tuple[int, dict]:
        """Returns (score_0_100, parts).

        The score is normalised against the signals actually MEASURABLE right
        now, not against a theoretical 100. Without a cohort list and without
        holder-growth history, 50 of the 100 raw points are unreachable, so an
        absolute score can never clear a 62 threshold no matter how good the
        token is. Normalising fixes that while keeping the parts honest:
        `available` tells you how much of the full picture you are seeing.
        """
        parts: dict[str, float] = {}
        available = 0.0

        # Always measurable
        # Liquidity: log scale that keeps resolving past the floor instead of
        # saturating at 20 almost immediately (the old curve gave $200k and
        # $2M the same score, which made ranking meaningless).
        liq = max(c.liquidity_usd, 1.0)
        parts["liquidity"] = max(0.0, min(
            20.0, 20 * (math.log10(liq) - math.log10(self.cfg.min_liquidity_usd))
            / (math.log10(5_000_000) - math.log10(self.cfg.min_liquidity_usd))))
        available += 20.0

        # Volume quality: continuous, peaking around 4x liquidity. Below ~1x
        # is dead; above ~15x the churn is bots, not accumulation. The old
        # three-bucket version scored a 0.3x token higher than a 13x one.
        r = c.vol_liq_ratio
        if r <= 0:
            vq = 0.0
        else:
            vq = 15.0 * math.exp(-((math.log(r / 4.0)) ** 2) / 1.1)
        parts["volume_quality"] = max(0.0, min(15.0, vq))
        available += 15.0

        # Measurable only with observation history.
        # None = never measured (excluded from the denominator).
        # 0 or negative = MEASURED and bad — a token bleeding holders must
        # score worse than one we simply haven't observed yet.
        if holder_growth_per_hr is None:
            parts["holder_growth"] = 0.0
        else:
            available += 20.0
            if holder_growth_per_hr >= 0:
                parts["holder_growth"] = min(20.0, holder_growth_per_hr / 5.0)
            else:
                # Losing holders: negative contribution, floored at -10.
                parts["holder_growth"] = max(-10.0, holder_growth_per_hr / 10.0)

        # Measurable only with a verified cohort list
        if self.cfg.cohort_enabled:
            parts["smart_money"] = (
                30.0 if cohort_buys >= self.cfg.min_cohort_wallets else 0.0)
            available += 30.0
        else:
            parts["smart_money"] = 0.0

        # Measurable only with attention data
        if sentiment:
            parts["sentiment"] = self._sentiment(sentiment)
            available += 15.0
        else:
            parts["sentiment"] = 0.0

        raw = sum(parts.values())
        total = int(round(raw / available * 100)) if available > 0 else 0
        parts["_available_weight"] = available
        return max(0, min(100, total)), parts

    @staticmethod
    def confidence(parts: dict) -> float:
        """Fraction of the full signal set that was actually measurable."""
        return (parts.get("_available_weight", 0.0)) / 100.0

    def _sentiment(self, s: Optional[dict]) -> float:
        if not s:
            return 0.0
        baseline = s.get("baseline_hourly_mentions", 0)
        current = s.get("current_hourly_mentions", 0)
        if baseline > 0 and current >= baseline * self.cfg.sentiment_spike_multiple:
            return -20.0          # distribution event, not a buy signal
        weighted = s.get("authenticity_weighted_mentions", 0)
        return min(15.0, weighted / 20.0)


# ────────────────────────────── execution ──────────────────────────────────

class PaperBroker:
    """Executes against real quoted prices. Moves no funds."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cash = cfg.capital_usd

    def buy(self, c: Candidate, usd: float, decimals: int = 9) -> Optional[Position]:
        if usd > self.cash:
            LOG.warning("insufficient paper cash")
            return None
        self.cash -= usd
        LOG.info("BUY  %-10s $%s @ %.10f", c.symbol, f"{usd:,.0f}", c.price_usd)
        return Position(
            mint=c.mint, symbol=c.symbol, chain=c.chain,
            pair_address=c.pair_address, decimals=decimals,
            entry_price=c.price_usd, entry_time=time.time(),
            qty=usd / c.price_usd, cost_usd=usd,
            entry_liquidity=c.liquidity_usd,
            entry_hourly_volume=c.volume_24h / 24,
            high_water_price=c.price_usd)

    def sell(self, pos: Position, price: float, fraction: float,
             reason: str) -> SellResult:
        if price <= 0:
            return SellResult(False, error="no price")
        qty = pos.qty * fraction
        proceeds = qty * price
        pos.qty -= qty
        pos.realised_usd += proceeds
        self.cash += proceeds
        LOG.info("SELL %-10s %.0f%% @ %.10f (%+.1f%%) — %s",
                 pos.symbol, fraction * 100, price, pos.gain_pct(price), reason)
        return SellResult(True, proceeds)


class LiveBrokerAdapter:
    """Wraps execution.LiveBroker in the PaperBroker interface so the strategy
    code is identical in both modes. Positions are sized in USD but swaps are
    denominated in SOL, so a SOL/USD price is required."""

    def __init__(self, live, sol_usd_price: float = 0.0):
        self.live = live
        self.sol_usd = sol_usd_price
        self.cash = 0.0

    SOL_MINT = "So11111111111111111111111111111111111111112"

    def _sol_price(self) -> float:
        if self.sol_usd > 0:
            return self.sol_usd
        raise RuntimeError("SOL/USD price unavailable")

    def set_sol_price(self, usd: float) -> None:
        if usd > 0:
            self.sol_usd = usd

    def refresh_state(self) -> bool:
        """Pull live SOL price and wallet balance.

        Must run before the first trade and on every cycle. Without it,
        `cash` stays 0, equity reads 0 against a non-zero peak, and the §7.4
        drawdown breaker hard-stops the bot the moment it starts.
        """
        ok = True
        try:
            import requests
            r = requests.get("https://api.dexscreener.com/latest/dex/tokens/"
                             + self.SOL_MINT, timeout=10)
            pairs = (r.json() or {}).get("pairs") or []
            usd = max((float(p.get("priceUsd") or 0) for p in pairs), default=0.0)
            if usd > 0:
                self.sol_usd = usd
            else:
                ok = False
        except Exception as e:  # noqa: BLE001
            LOG.error("SOL price fetch failed: %s", e)
            ok = False
        try:
            sol = self.live.sender.sol_balance(self.live.wallet.pubkey)
            if self.sol_usd > 0:
                self.cash = sol * self.sol_usd
        except Exception as e:  # noqa: BLE001
            LOG.error("wallet balance fetch failed: %s", e)
            ok = False
        return ok

    def buy(self, c: Candidate, usd: float, decimals: int = 9) -> Optional[Position]:
        res = self.live.buy(c.mint, usd / self._sol_price())
        if not res.ok:
            LOG.error("BUY FAILED %s — %s", c.symbol, res.error)
            return None
        qty = res.out_amount / 10 ** decimals
        LOG.info("BUY  %-10s $%s  sig=%s", c.symbol, f"{usd:,.0f}", res.signature[:16])
        return Position(
            mint=c.mint, symbol=c.symbol, chain=c.chain,
            pair_address=c.pair_address, decimals=decimals,
            entry_price=usd / qty if qty else c.price_usd,
            entry_time=time.time(), qty=qty, cost_usd=usd,
            entry_liquidity=c.liquidity_usd,
            entry_hourly_volume=c.volume_24h / 24,
            high_water_price=c.price_usd)

    def sell(self, pos: Position, price: float, fraction: float,
             reason: str) -> SellResult:
        if fraction >= 1.0:
            res = self.live.sell_all(pos.mint)
        else:
            res = self.live.sell(pos.mint, int(pos.qty * fraction * 10 ** pos.decimals))
        if not res.ok:
            LOG.error("SELL FAILED %s — %s — POSITION STILL HELD, will retry",
                      pos.symbol, res.error)
            return SellResult(False, error=res.error)
        proceeds = res.out_amount / LAMPORTS_PER_SOL * self._sol_price()
        pos.qty -= pos.qty * fraction
        pos.realised_usd += proceeds
        LOG.info("SELL %-10s %.0f%% -> $%.2f — %s  sig=%s",
                 pos.symbol, fraction * 100, proceeds, reason, res.signature[:16])
        return SellResult(True, proceeds)


# ─────────────────────────── exit engine §6 ────────────────────────────────

@dataclass
class SellResult:
    """Sells must report success. Returning a bare float made it impossible
    to distinguish 'sold for $0' from 'the transaction failed', which let the
    bot delete positions it still held."""
    ok: bool
    proceeds: float = 0.0
    error: str = ""


@dataclass
class ExitSignal:
    fraction: float
    reason: str
    emergency: bool = False


def evaluate_exits(pos: Position, live: Candidate, cfg: Config,
                   sell_ok: bool, exit_impact: float,
                   authority_reappeared: bool = False,
                   insider_dump: bool = False) -> Optional[ExitSignal]:
    """First rule that fires wins. Emergency rules are checked first."""
    price = live.price_usd
    if price <= 0:
        return None
    pos.high_water_price = max(pos.high_water_price, price)
    gain = pos.gain_pct(price)

    # §6.3 emergency — bypass the ladder
    liq_drop = (1 - live.liquidity_usd / pos.entry_liquidity) * 100 \
        if pos.entry_liquidity else 0
    if liq_drop > cfg.liq_drop_exit_pct:
        return ExitSignal(1.0, f"6.3.1 liquidity -{liq_drop:.0f}%", True)
    if insider_dump:
        return ExitSignal(1.0, "6.3.2 insider distribution", True)
    if authority_reappeared:
        return ExitSignal(1.0, "6.3.3 mint/freeze authority restored", True)
    if not sell_ok:
        return ExitSignal(1.0, "6.3.4 sell simulation failing", True)
    if exit_impact > cfg.emergency_impact_pct:
        return ExitSignal(1.0, f"6.3.5 exit impact {exit_impact:.1f}%", True)

    # §6.2 stops
    if gain <= cfg.hard_stop_pct:
        return ExitSignal(1.0, f"6.2.1 hard stop {gain:.1f}%")
    if pos.hours_held() >= cfg.time_stop_hours and gain < cfg.time_stop_min_gain_pct:
        return ExitSignal(1.0, f"6.2.2 time stop, {gain:+.1f}% in 24h")
    hourly = live.volume_24h / 24
    if pos.entry_hourly_volume > 0 and \
            hourly < pos.entry_hourly_volume * cfg.volume_death_pct / 100:
        return ExitSignal(1.0, "6.2.3 volume collapse")

    # §6.1 profit ladder
    if not pos.tp1_done and gain >= cfg.tp1_gain_pct:
        pos.tp1_done = True
        return ExitSignal(cfg.tp1_sell_pct / 100, f"6.1 TP1 at {gain:+.0f}%")
    if not pos.tp2_done and gain >= cfg.tp2_gain_pct:
        pos.tp2_done = True
        return ExitSignal(cfg.tp2_sell_pct / 100 / 0.5, f"6.1 TP2 at {gain:+.0f}%")
    if pos.tp1_done:
        drawdown = (1 - price / pos.high_water_price) * 100
        if drawdown >= cfg.trailing_stop_pct:
            return ExitSignal(1.0, f"6.1 trailing stop -{drawdown:.0f}% from high")
    return None


# ──────────────────────────── breakers §7 ──────────────────────────────────

class CircuitBreakers:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.day_start_equity = cfg.capital_usd
        self.peak_equity = cfg.capital_usd
        self.consecutive_losses = 0
        self.halted_until = 0.0
        self.hard_stopped = False
        self.week_start_ts = time.time()
        self.week_start_equity = cfg.capital_usd
        self.last_feed_ts = 0.0

    def mark_feed_fresh(self) -> None:
        self.last_feed_ts = time.time()

    def record_close(self, pnl_usd: float) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl_usd < 0 else 0
        if self.consecutive_losses >= self.cfg.consecutive_loss_halt:
            self._halt(24, f"{self.consecutive_losses} consecutive losses")

    def check(self, equity: float) -> bool:
        """Returns True if new entries are allowed. Exits are never halted."""
        if self.hard_stopped:
            return False
        self.peak_equity = max(self.peak_equity, equity)

        # §7.5 stale feed — halt entries if prices stopped updating
        if self.last_feed_ts and \
                time.time() - self.last_feed_ts > self.cfg.max_feed_staleness_sec:
            LOG.warning("7.5 price feed stale %.0fs — entries blocked",
                        time.time() - self.last_feed_ts)
            return False

        # §7.3 weekly drawdown
        if time.time() - self.week_start_ts > 7 * 86400:
            self.week_start_ts, self.week_start_equity = time.time(), equity
        weekly = (equity / self.week_start_equity - 1) * 100 \
            if self.week_start_equity else 0.0
        if weekly <= self.cfg.weekly_drawdown_halt_pct:
            self._halt(24 * 7, f"weekly drawdown {weekly:.1f}%")

        total_dd = (equity / self.peak_equity - 1) * 100
        if total_dd <= self.cfg.total_drawdown_stop_pct:
            LOG.critical("7.4 total drawdown %.1f%% — FULL STOP, manual restart required",
                         total_dd)
            self.hard_stopped = True
            return False

        daily = (equity / self.day_start_equity - 1) * 100
        if daily <= self.cfg.daily_loss_halt_pct:
            self._halt(24, f"daily PnL {daily:.1f}%")

        if Path(self.cfg.halt_file).exists():
            LOG.warning("HALT file present — entries blocked")
            return False
        return time.time() >= self.halted_until

    def _halt(self, hours: int, reason: str) -> None:
        if time.time() < self.halted_until:
            return
        self.halted_until = time.time() + hours * 3600
        LOG.warning("CIRCUIT BREAKER: entries halted %dh — %s", hours, reason)


# ────────────────────────────── journal §8.4 ───────────────────────────────

class Journal:
    def __init__(self, path: str):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.db = sqlite3.connect(path, check_same_thread=False)
        LOG.info("state file: %s", path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS decisions(
            ts REAL, mint TEXT, symbol TEXT, action TEXT, detail TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS trades(
            ts REAL, mint TEXT, symbol TEXT, side TEXT,
            price REAL, usd REAL, reason TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS blacklist(
            mint TEXT PRIMARY KEY, reason TEXT, ts REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS positions(
            mint TEXT PRIMARY KEY, data TEXT, updated REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS observations(
            mint TEXT, ts REAL, holders INTEGER, liquidity REAL)""")
        self.db.execute("""CREATE INDEX IF NOT EXISTS obs_mint ON observations(mint, ts)""")
        self.db.commit()

    def decision(self, mint: str, symbol: str, action: str, detail: Any) -> None:
        self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?)",
                        (time.time(), mint, symbol, action,
                         json.dumps(detail, default=str)[:4000]))
        self.db.commit()

    def trade(self, mint: str, symbol: str, side: str,
              price: float, usd: float, reason: str) -> None:
        self.db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?)",
                        (time.time(), mint, symbol, side, price, usd, reason))
        self.db.commit()

    def blacklist(self, mint: str, reason: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO blacklist VALUES (?,?,?)",
                        (mint, reason, time.time()))
        self.db.commit()

    def observe(self, mint: str, holders: int, liquidity: float) -> None:
        self.db.execute("INSERT INTO observations VALUES (?,?,?,?)",
                        (mint, time.time(), holders, liquidity))
        self.db.commit()

    def holder_growth_per_hr(self, mint: str) -> float:
        """Derived from our own observations — no extra API dependency.
        Returns 0.0 until at least two samples 10+ minutes apart exist."""
        rows = self.db.execute(
            "SELECT ts, holders FROM observations WHERE mint=? ORDER BY ts", (mint,)
        ).fetchall()
        if len(rows) < 2:
            return 0.0
        (t0, h0), (t1, h1) = rows[0], rows[-1]
        hours = (t1 - t0) / 3600
        if hours < 0.17 or h0 <= 0:
            return 0.0
        return max(0.0, (h1 - h0) / hours)

    def save_positions(self, positions: dict) -> None:
        self.db.execute("DELETE FROM positions")
        for mint, p in positions.items():
            self.db.execute("INSERT INTO positions VALUES (?,?,?)",
                            (mint, json.dumps(asdict(p)), time.time()))
        self.db.commit()

    def load_positions(self) -> dict:
        out = {}
        try:
            for mint, blob, _ in self.db.execute(
                    "SELECT mint, data, updated FROM positions").fetchall():
                out[mint] = Position(**json.loads(blob))
        except Exception as e:  # noqa: BLE001
            LOG.error("could not restore positions: %s", e)
        return out

    def is_blacklisted(self, mint: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM blacklist WHERE mint=?", (mint,)).fetchone() is not None


# ──────────────────────────────── bot ──────────────────────────────────────

class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        http = HttpClient()
        self.dex = DexScreener(http, cfg)
        self.rpc = SolanaRPC(http, cfg)
        self.jup = Jupiter(http, cfg)
        from safety_data import RugCheck
        self.rugcheck = RugCheck()
        self.screen = SafetyScreen(self.rpc, self.jup, cfg, self.rugcheck)
        self.scorer = Scorer(cfg)
        self.cohort = None       # plug a CohortTracker here once wallets verified
        self.sentiment = None    # plug a SentimentSource here once X API wired
        self.broker = self._make_broker(cfg)
        self.breakers = CircuitBreakers(cfg)
        self.journal = Journal(cfg.db_path)
        self.positions: dict[str, Position] = self.journal.load_positions()
        if self.positions:
            LOG.warning("restored %d open position(s) from previous run: %s",
                        len(self.positions),
                        ", ".join(p.symbol for p in self.positions.values()))
        self._reconcile_wallet()
        if cfg.mode == "LIVE":
            eq = self.equity()
            self.breakers.day_start_equity = eq
            self.breakers.peak_equity = eq
            LOG.info("live equity at start: $%s", f"{eq:,.2f}")

    def _make_broker(self, cfg: Config):
        if cfg.mode == "PAPER":
            return PaperBroker(cfg)
        from execution import JupiterSwap, LiveBroker, Wallet
        w = Wallet()
        LOG.warning("LIVE MODE — real funds at risk on wallet %s", w.pubkey)
        adapter = LiveBrokerAdapter(
            LiveBroker(cfg.solana_rpc, w, JupiterSwap(),
                       cfg.max_slippage_pct, cfg.max_exit_price_impact_pct))
        if not adapter.refresh_state():
            raise RuntimeError(
                "could not read wallet balance or SOL price — refusing to "
                "start live mode blind")
        return adapter

    def holder_count(self, c: Candidate) -> int:
        """Best-effort holder count for the growth signal."""
        rep = self.rugcheck.report(c.mint)
        if not rep:
            return 0
        return int(rep.get("totalHolders") or len(rep.get("topHolders") or []))

    # -- sizing §4 --------------------------------------------------------
    def save_positions(self) -> None:
        self.journal.save_positions(self.positions)

    def _reconcile_wallet(self) -> None:
        """Live mode: the wallet is the source of truth, not the database.

        A crash between a buy confirming and the DB write leaves real tokens
        the bot has no record of. Equally, a restored position may already
        have been sold by hand. Reconcile before trading.
        """
        if self.cfg.mode != "LIVE" or not self.positions:
            return
        try:
            sender = self.broker.live.sender
            owner = self.broker.live.wallet.pubkey
        except AttributeError:
            return
        for mint, pos in list(self.positions.items()):
            try:
                held = sender.token_balance_raw(owner, mint) / 10 ** pos.decimals
            except Exception as e:  # noqa: BLE001
                LOG.error("reconcile failed for %s: %s — keeping position", pos.symbol, e)
                continue
            if held <= 1e-9:
                LOG.warning("%s: DB says %.4f held, wallet says 0 — dropping",
                            pos.symbol, pos.qty)
                del self.positions[mint]
            elif abs(held - pos.qty) / max(pos.qty, 1e-9) > 0.02:
                LOG.warning("%s: qty drift DB %.4f vs wallet %.4f — trusting wallet",
                            pos.symbol, pos.qty, held)
                pos.qty = held
        self.save_positions()

    def position_size(self, c: Candidate) -> float:
        base = self.cfg.capital_usd * self.cfg.base_position_pct / 100
        cap = self.cfg.capital_usd * self.cfg.max_position_pct / 100
        liq_cap = c.liquidity_usd * self.cfg.max_pct_of_liquidity / 100
        return min(base, cap, liq_cap)

    def deployed_usd(self) -> float:
        return sum(p.qty * p.entry_price for p in self.positions.values())

    def equity(self) -> float:
        return self.broker.cash + self.deployed_usd()

    # -- entry pipeline ---------------------------------------------------
    def consider(self, c: Candidate) -> None:
        if c.mint in self.positions or self.journal.is_blacklisted(c.mint):
            return

        gates = discovery_filters(c, self.cfg)
        failed = [g for g in gates if not g.ok]
        if failed:
            self.journal.decision(c.mint, c.symbol, "REJECT_DISCOVERY",
                                  [asdict(g) for g in failed])
            return

        size_usd = self.position_size(c)
        if size_usd < 25:
            return
        info = self.rpc.mint_info(c.mint)
        if not info:
            self.journal.decision(c.mint, c.symbol, "REJECT_NO_MINT_INFO", {})
            return
        decimals = int(info.get("decimals", 9))
        raw_amount = int(size_usd / c.price_usd * 10 ** decimals)

        safety = self.screen.run(c, raw_amount)
        blocking = [g for g in safety if not g.ok]
        if blocking:
            self.journal.decision(c.mint, c.symbol, "REJECT_SAFETY",
                                  [asdict(g) for g in blocking])
            if any(g.verdict is Verdict.REJECT for g in blocking):
                self.journal.blacklist(c.mint, blocking[0].name)
            LOG.info("REJECT %-10s — %s (%s)", c.symbol,
                     blocking[0].name, blocking[0].detail)
            return

        self.journal.observe(c.mint, self.holder_count(c), c.liquidity_usd)
        score, parts = self.scorer.score(
            c,
            cohort_buys=self.cohort.accumulating(c.mint) if self.cohort else 0,
            holder_growth_per_hr=self.journal.holder_growth_per_hr(c.mint),
            sentiment=self.sentiment.snapshot(c.symbol) if self.sentiment else None,
        )
        if score < self.cfg.min_score:
            self.journal.decision(c.mint, c.symbol, "REJECT_SCORE",
                                  {"score": score, **parts})
            return

        # Same coverage floor the analyzer applies. Without this the analyzer
        # reports WATCH at 35% coverage while the bot buys the same token.
        coverage = parts.get("_available_weight", 0.0)
        if coverage < self.cfg.min_signal_coverage:
            self.journal.decision(c.mint, c.symbol, "REJECT_COVERAGE",
                                  {"score": score, "coverage": coverage})
            LOG.info("SKIP   %-10s score %d but only %.0f%% signal coverage",
                     c.symbol, score, coverage)
            return

        if len(self.positions) >= self.cfg.max_concurrent_positions:
            return
        if self.deployed_usd() + size_usd > \
                self.cfg.capital_usd * self.cfg.max_deployed_pct / 100:
            return
        if not self.breakers.check(self.equity()):
            return

        fresh = self.dex.pair(c.chain, c.pair_address)      # rule 5.5, no chasing
        if not fresh or abs(fresh.price_usd / c.price_usd - 1) * 100 > self.cfg.max_chase_pct:
            self.journal.decision(c.mint, c.symbol, "ABORT_CHASE", {})
            return

        pos = self.broker.buy(fresh, size_usd, decimals)
        if pos:
            try:
                from safety_data import ReportView
                rep0 = self.rugcheck.report(c.mint)
                if rep0:
                    t10, _ = ReportView(rep0).holder_concentration()
                    pos.entry_top10 = t10 or 0.0
            except Exception:  # noqa: BLE001
                pass
            self.positions[pos.mint] = pos
            self.save_positions()
            self.journal.trade(pos.mint, pos.symbol, "BUY",
                               pos.entry_price, size_usd, f"score={score}")

    # -- exit pipeline ----------------------------------------------------
    def _insider_dump(self, pos: Position) -> bool:
        """§6.3.2 — top holders offloading.

        Compares current top-10 concentration against what it was at entry.
        A sharp drop means large holders sold into the market. Previously this
        parameter defaulted to False and nothing ever computed it, so the
        emergency exit could never fire.
        """
        if not self.rugcheck or pos.chain != "solana":
            return False
        try:
            from safety_data import ReportView
            rep = self.rugcheck.report(pos.mint)
            if not rep:
                return False
            top10, _ = ReportView(rep).holder_concentration()
            if top10 is None:
                return False
            if pos.entry_top10 <= 0:
                pos.entry_top10 = top10
                return False
            drop = pos.entry_top10 - top10
            if drop >= self.cfg.insider_dump_drop_pct:
                LOG.critical("%s: top-10 concentration fell %.1f%% -> %.1f%% "
                             "(-%.1f pts) — insider distribution",
                             pos.symbol, pos.entry_top10, top10, drop)
                return True
        except Exception as e:  # noqa: BLE001
            LOG.debug("insider check failed: %s", e)
        return False

    def note_feed(self, ok: bool) -> None:
        if ok:
            self.breakers.mark_feed_fresh()

    def refresh_broker(self) -> None:
        if hasattr(self.broker, "refresh_state"):
            self.broker.refresh_state()

    def manage_positions(self) -> None:
        self.refresh_broker()
        for mint, pos in list(self.positions.items()):
            live = self.dex.pair(pos.chain, pos.pair_address)
            self.note_feed(bool(live))
            if not live:
                continue
            raw = int(pos.qty * 10 ** pos.decimals)
            sell_ok, impact, _ = self.jup.can_sell(mint, raw)
            info = self.rpc.mint_info(mint)
            authority_back = bool(info and (info.get("mintAuthority")
                                            or info.get("freezeAuthority")))

            insider_dump = self._insider_dump(pos)
            sig = evaluate_exits(pos, live, self.cfg, sell_ok, impact,
                                 authority_back, insider_dump)
            if not sig:
                continue

            fraction = min(1.0, sig.fraction)
            res = self.broker.sell(pos, live.price_usd, fraction, sig.reason)

            if not res.ok:
                # The position is STILL HELD. Do not forget it — the next loop
                # re-evaluates and retries. Forgetting a position whose sell
                # failed leaves real tokens unmonitored, which is exactly the
                # scenario emergency exits exist for.
                pos.failed_exits += 1
                self.journal.decision(mint, pos.symbol, "SELL_FAILED",
                                      {"reason": sig.reason, "error": res.error,
                                       "attempts": pos.failed_exits})
                if pos.failed_exits in (3, 10, 25):
                    LOG.critical("%s: %d consecutive exit failures — MANUAL "
                                 "INTERVENTION LIKELY NEEDED", pos.symbol,
                                 pos.failed_exits)
                self.save_positions()
                continue

            pos.failed_exits = 0
            self.journal.trade(mint, pos.symbol, "SELL",
                               live.price_usd, res.proceeds, sig.reason)
            if pos.qty <= 1e-9 or fraction >= 1.0:
                pnl = pos.realised_usd - pos.cost_usd
                self.breakers.record_close(pnl)
                LOG.info("CLOSED %-10s PnL $%+.2f", pos.symbol, pnl)
                del self.positions[mint]
            self.save_positions()

    # -- loop -------------------------------------------------------------
    def scan(self, queries: list[str]) -> list[Candidate]:
        out: list[Candidate] = []
        for q in queries:
            out.extend(self.dex.search(q))
            time.sleep(0.5)
        return out

    def run_once(self, queries: list[str]) -> None:
        self.manage_positions()
        if not self.breakers.check(self.equity()):
            LOG.info("entries blocked by circuit breaker")
            return
        for c in self.scan(queries):
            if c.chain in ("solana", "base"):
                self.consider(c)

    def run(self, queries: list[str]) -> None:
        LOG.info("mode=%s capital=$%s max_positions=%d", self.cfg.mode,
                 f"{self.cfg.capital_usd:,.0f}", self.cfg.max_concurrent_positions)
        last_scan = 0.0
        while not self.breakers.hard_stopped:
            try:
                self.manage_positions()
                if time.time() - last_scan > self.cfg.scan_interval_sec:
                    if self.breakers.check(self.equity()):
                        for c in self.scan(queries):
                            if c.chain in ("solana", "base"):
                                self.consider(c)
                    last_scan = time.time()
                time.sleep(self.cfg.exit_check_interval_sec)
            except KeyboardInterrupt:
                LOG.info("shutdown; equity $%s", f"{self.equity():,.2f}")
                return
            except Exception:  # noqa: BLE001
                LOG.exception("loop error — positions still monitored")
                time.sleep(10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single cycle then exit")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--queries", nargs="*", default=["SOL", "USDC"])
    ap.add_argument("--live", action="store_true",
                    help="trade real funds (needs I_UNDERSTAND_LIVE_TRADING=yes)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S")

    cfg = Config(capital_usd=args.capital)
    if args.live:
        if os.getenv("I_UNDERSTAND_LIVE_TRADING") != "yes":
            LOG.critical("--live requires I_UNDERSTAND_LIVE_TRADING=yes and a "
                         "funded SOLANA_PRIVATE_KEY. Refusing to start.")
            return 1
        cfg.mode = "LIVE"

    bot = Bot(cfg)
    if args.once:
        bot.run_once(args.queries)
        LOG.info("equity $%s | open %d", f"{bot.equity():,.2f}", len(bot.positions))
    else:
        bot.run(args.queries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
