#!/usr/bin/env python3
"""
Token analyzer. Produces entry zones, a stop, position size, and a verdict.

    python analyze.py <mint_or_address> [--capital 10000] [--chain solana]

WHAT THE LEVELS ACTUALLY ARE
────────────────────────────
The entry and stop numbers here are arithmetic on observed price structure,
not predictions. Specifically:

  - Prior prices are reconstructed from DexScreener's percentage changes:
    price_6h_ago = price_now / (1 + change_6h/100). This is exact for the
    endpoints, but it is NOT a full candle history — it cannot see the wick
    lows between those points. Real swing lows may be below what this sees.

  - Entry zones are retracements of the observed move. A retracement level is
    a place where buyers *previously* stepped in. It is not a floor.

  - The stop is placed below the structural base, then widened or rejected
    against the rulebook's -35% hard limit.

Nothing here forecasts direction. What it does do is answer the questions the
typical signal post omits: how much can I size without trapping myself, what
does my own exit do to the price, and where is the thesis dead.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

LOG = logging.getLogger("analyze")

# DexScreener chainId -> GoPlus chain id. GoPlus has no Robinhood Chain
# support yet (launched 2026-07-01), so that entry is None and the screen
# degrades to "unverifiable" rather than silently passing.
EVM_CHAIN_IDS = {
    "base": 8453, "ethereum": 1, "arbitrum": 42161, "optimism": 10,
    "bsc": 56, "polygon": 137, "avalanche": 43114,
    "robinhood": None, "robinhoodchain": None,
}


# ────────────────────────────── model ──────────────────────────────────────

@dataclass
class Levels:
    price: float
    base: float                    # approximate swing low of the observed move
    move_pct: float                # how far it has already run off the base
    entries: dict[str, float] = field(default_factory=dict)
    stop: float = 0.0
    stop_pct: float = 0.0
    stop_basis: str = ""
    targets: dict[str, float] = field(default_factory=dict)
    rr_at_moderate: float = 0.0


@dataclass
class Sizing:
    max_by_risk: float = 0.0       # from % of capital risked
    max_by_liquidity: float = 0.0  # from §4.3, so you can actually exit
    max_by_rule: float = 0.0       # from §4.1/4.2 position caps
    recommended: float = 0.0
    binding_constraint: str = ""
    exit_impact_pct: Optional[float] = None


@dataclass
class Report:
    symbol: str
    mint: str
    chain: str
    price: float
    liquidity: float
    fdv: float
    volume_24h: float
    vol_liq: float
    age_hours: float
    buys: int
    sells: int
    chg_1h: float
    chg_6h: float
    chg_24h: float
    levels: Optional[Levels] = None
    sizing: Optional[Sizing] = None
    gates_failed: list = field(default_factory=list)
    gates_unknown: list = field(default_factory=list)
    score: int = 0
    score_parts: dict = field(default_factory=dict)
    verdict: str = "UNKNOWN"
    reasons: list = field(default_factory=list)
    attention: Optional[dict] = None


# ──────────────────────────── level math ───────────────────────────────────

def reconstruct_base(price: float, chg_1h: float, chg_6h: float,
                     chg_24h: float) -> tuple[float, str]:
    """Approximate the low the current move started from.

    Uses the lowest reconstructed endpoint. Returns (base, which_window).
    Falls back to a volatility proxy when the token only went up.
    """
    pts = {}
    for label, chg in (("1h", chg_1h), ("6h", chg_6h), ("24h", chg_24h)):
        if chg is not None and chg > -99:
            pts[label] = price / (1 + chg / 100)

    if not pts:
        return price * 0.75, "no history (assumed 25%)"

    label = min(pts, key=lambda k: pts[k])
    base = pts[label]

    # If price is below every reconstructed point, it is declining — the
    # "base" is not above current price. Use the observed range instead.
    if base >= price:
        lowest = min(min(pts.values()), price)
        return lowest * 0.92, "declining, using range low"
    return base, label


def compute_levels(price: float, chg_1h: float, chg_6h: float, chg_24h: float,
                   hard_stop_pct: float = -35.0) -> Levels:
    base, window = reconstruct_base(price, chg_1h, chg_6h, chg_24h)
    swing = max(price - base, price * 1e-9)
    move_pct = (price / base - 1) * 100 if base > 0 else 0.0

    entries = {
        "shallow (23.6%)": price - 0.236 * swing,
        "moderate (38.2%)": price - 0.382 * swing,
        "deep (50%)": price - 0.500 * swing,
    }

    entry = entries["moderate (38.2%)"]

    # Structural stop: below the base that defines the move.
    structural = base * 0.96
    struct_pct = (structural / entry - 1) * 100

    if struct_pct < hard_stop_pct:
        # The structure is wider than the rulebook allows. Two honest options:
        # cap the stop (and accept it may be inside the noise) or size down.
        stop = entry * (1 + hard_stop_pct / 100)
        basis = (f"rulebook cap {hard_stop_pct:.0f}% — structural stop would be "
                 f"{struct_pct:.0f}%, wider than allowed")
    else:
        stop = structural
        basis = f"below the {window} base ({base:.8g})"

    stop_pct = (stop / entry - 1) * 100

    # A stop closer than ~12% on a microcap sits inside ordinary intraday
    # noise. It will be hit by random wiggle, not by the thesis breaking.
    if stop_pct > -12:
        basis += "  ⚠ TIGHT — inside typical noise, expect random stop-outs"

    risk = entry - stop
    targets = {"1R": entry + risk, "2R": entry + 2 * risk, "3R": entry + 3 * risk}

    return Levels(price=price, base=base, move_pct=move_pct, entries=entries,
                  stop=stop, stop_pct=stop_pct, stop_basis=basis,
                  targets=targets,
                  rr_at_moderate=(targets["2R"] - entry) / risk if risk > 0 else 0)


def compute_sizing(capital: float, liquidity: float, stop_pct: float,
                   risk_pct: float = 1.0, base_pos_pct: float = 2.0,
                   max_pos_pct: float = 3.0,
                   max_liq_pct: float = 0.5) -> Sizing:
    """The output most signal posts omit entirely."""
    s = Sizing()
    s.max_by_liquidity = liquidity * max_liq_pct / 100
    s.max_by_rule = min(capital * base_pos_pct / 100, capital * max_pos_pct / 100)

    # Risk-based: lose `risk_pct` of capital if the stop hits.
    if stop_pct < 0:
        s.max_by_risk = (capital * risk_pct / 100) / (abs(stop_pct) / 100)
    else:
        s.max_by_risk = 0.0

    options = {
        "liquidity (§4.3 — exit capacity)": s.max_by_liquidity,
        "position cap (§4.1/4.2)": s.max_by_rule,
        f"risk budget ({risk_pct}% of capital)": s.max_by_risk,
    }
    s.binding_constraint = min(options, key=lambda k: options[k])
    s.recommended = max(0.0, options[s.binding_constraint])
    return s


# ────────────────────────────── analysis ───────────────────────────────────

def analyze(token: str, chain: str = "solana", capital: float = 10_000.0,
            sentiment: Optional[dict] = None) -> Optional[Report]:
    from sniper_bot import (Config, DexScreener, HttpClient, Jupiter, Scorer,
                            SolanaRPC, SafetyScreen, discovery_filters, Verdict)

    cfg = Config(capital_usd=capital)
    http = HttpClient()
    dex = DexScreener(http, cfg)

    cands = dex.search(token)
    cands = [c for c in cands if c.mint.lower() == token.lower()] or cands
    if not cands:
        return None
    c = max(cands, key=lambda x: x.liquidity_usd)

    pc = (c.raw.get("priceChange") or {})
    def chg(k):
        try:
            return float(pc.get(k))
        except (TypeError, ValueError):
            return None

    rep = Report(
        symbol=c.symbol, mint=c.mint, chain=c.chain, price=c.price_usd,
        liquidity=c.liquidity_usd, fdv=c.fdv_usd, volume_24h=c.volume_24h,
        vol_liq=c.vol_liq_ratio, age_hours=c.age_minutes / 60,
        buys=c.txns_buys_24h, sells=c.txns_sells_24h,
        chg_1h=chg("h1") or 0.0, chg_6h=chg("h6") or 0.0,
        chg_24h=chg("h24") or 0.0, attention=sentiment)

    rep.levels = compute_levels(c.price_usd, rep.chg_1h, rep.chg_6h, rep.chg_24h,
                                cfg.hard_stop_pct)
    rep.sizing = compute_sizing(capital, c.liquidity_usd, rep.levels.stop_pct)

    # Discovery + safety gates
    for g in discovery_filters(c, cfg):
        if not g.ok:
            rep.gates_failed.append(f"{g.name} = {g.detail}")

    if c.chain == "solana":
        try:
            from safety_data import RugCheck
            rpc = SolanaRPC(http, cfg)
            jup = Jupiter(http, cfg)
            screen = SafetyScreen(rpc, jup, cfg, RugCheck())
            info = rpc.mint_info(c.mint)
            dec = int((info or {}).get("decimals", 9))
            raw = int(rep.sizing.recommended / c.price_usd * 10 ** dec) \
                if c.price_usd > 0 else 0
            if raw > 0:
                ok, impact, _ = jup.can_sell(c.mint, raw)
                rep.sizing.exit_impact_pct = impact if ok else None
            for g in screen.run(c, max(raw, 1)):
                if g.verdict is Verdict.REJECT:
                    rep.gates_failed.append(f"{g.name} = {g.detail}")
                elif g.verdict is Verdict.UNKNOWN:
                    rep.gates_unknown.append(g.name)
        except Exception as e:  # noqa: BLE001
            rep.gates_unknown.append(f"safety screen error: {e}")

    elif c.chain in EVM_CHAIN_IDS:
        gp_id = EVM_CHAIN_IDS[c.chain]
        if gp_id is None:
            rep.gates_unknown.append(
                f"no token-security provider covers {c.chain} — "
                f"LP lock, honeypot, tax and holder checks all UNVERIFIED")
        else:
            try:
                from evm_venue import GoPlusSecurity
                for name, verdict, detail in GoPlusSecurity().gates(
                        gp_id, c.mint, cfg.max_round_trip_tax_pct,
                        cfg.max_top10_pct):
                    if verdict == "REJECT":
                        rep.gates_failed.append(f"{name} = {detail}")
                    elif verdict == "UNKNOWN":
                        rep.gates_unknown.append(f"{name} = {detail}")
            except Exception as e:  # noqa: BLE001
                rep.gates_unknown.append(f"EVM safety screen error: {e}")
    else:
        rep.gates_unknown.append(f"no safety adapter for chain '{c.chain}'")

    rep.score, rep.score_parts = Scorer(cfg).score(c, sentiment=sentiment)

    # Verdict
    if rep.gates_failed:
        rep.verdict = "AVOID"
        rep.reasons.append(f"{len(rep.gates_failed)} hard gate(s) failed")
    elif rep.gates_unknown:
        rep.verdict = "INSUFFICIENT DATA"
        rep.reasons.append(f"{len(rep.gates_unknown)} gate(s) unverifiable — "
                           f"missing data counts as a rejection")
    elif rep.score < cfg.min_score:
        rep.verdict = "WATCH"
        rep.reasons.append(f"score {rep.score} below threshold {cfg.min_score}")
    else:
        rep.verdict = "ELIGIBLE"
        rep.reasons.append(f"all gates passed, score {rep.score}")

    if rep.levels.move_pct > 60:
        rep.reasons.append(
            f"already up {rep.levels.move_pct:.0f}% off the base — you are "
            f"buying a pullback, not a breakout")
    if rep.sizing.exit_impact_pct and rep.sizing.exit_impact_pct > 4:
        rep.reasons.append(
            f"your own exit at recommended size moves price "
            f"{rep.sizing.exit_impact_pct:.1f}%")
    if not sentiment:
        rep.reasons.append("no attention data — social signal unmeasured, not absent")
    return rep


# ─────────────────────────────── output ────────────────────────────────────

def render(r: Report) -> str:
    L, S = r.levels, r.sizing
    icon = {"ELIGIBLE": "●", "WATCH": "◐", "AVOID": "✕",
            "INSUFFICIENT DATA": "?"}.get(r.verdict, "?")

    out = [
        f"{icon}  {r.symbol}  —  {r.verdict}",
        f"   {r.mint[:16]}…  ({r.chain})",
        "",
        f"price      ${r.price:.8g}   "
        f"1h {r.chg_1h:+.1f}%  6h {r.chg_6h:+.1f}%  24h {r.chg_24h:+.1f}%",
        f"liquidity  ${r.liquidity:,.0f}",
        f"volume 24h ${r.volume_24h:,.0f}   (vol/liq {r.vol_liq:.1f}x)",
        f"fdv        ${r.fdv:,.0f}",
        f"age        {r.age_hours:.1f}h   buys/sells {r.buys}/{r.sells}",
    ]

    bs = r.buys / max(r.sells, 1)
    if bs < 0.8:
        out.append(f"           ⚠ {r.sells:,} sells vs {r.buys:,} buys "
                   f"({bs:.2f}) — net distribution")
    if r.chg_1h < 0 and r.chg_6h < 0 and r.chg_24h > 20:
        out.append(f"           ⚠ up on 24h but falling on 1h and 6h — "
                   f"the move is unwinding")

    if r.vol_liq > 25:
        out.append(f"           ⚠ vol/liq {r.vol_liq:.0f}x suggests wash trading")
    elif r.vol_liq < 1.5:
        out.append(f"           ⚠ vol/liq {r.vol_liq:.1f}x — thin, hard to exit")

    out += ["", "LEVELS", f"  base       ${L.base:.8g}  ({L.stop_basis})",
            f"  move       +{L.move_pct:.0f}% off base"]
    out.append("  entries")
    for k, v in L.entries.items():
        out.append(f"    {k:<18} ${v:.8g}   ({(v/r.price-1)*100:+.1f}% from spot)")
    out += [
        f"  stop       ${L.stop:.8g}   ({L.stop_pct:.1f}% from moderate entry)",
        f"  targets    2R ${L.targets['2R']:.8g}   3R ${L.targets['3R']:.8g}",
        f"  R:R        {L.rr_at_moderate:.1f}:1 at the 2R target",
        "",
        "SIZING",
        f"  recommended  ${S.recommended:,.0f}",
        f"  bound by     {S.binding_constraint}",
        f"    risk budget      ${S.max_by_risk:,.0f}",
        f"    liquidity cap    ${S.max_by_liquidity:,.0f}",
        f"    position cap     ${S.max_by_rule:,.0f}",
    ]
    if S.exit_impact_pct is not None:
        out.append(f"  exit impact  {S.exit_impact_pct:.2f}% at that size")

    out += ["", f"SCORE  {r.score}/100"]
    for k, v in (r.score_parts or {}).items():
        out.append(f"  {k:<16} {v:>6.1f}")

    if r.attention:
        out += ["", "ATTENTION", f"  {r.attention}"]
    else:
        out += ["", "ATTENTION  not configured (needs X API) — unmeasured"]

    if r.gates_failed:
        out += ["", "FAILED GATES"] + [f"  ✕ {g}" for g in r.gates_failed]
    if r.gates_unknown:
        out += ["", "UNVERIFIABLE"] + [f"  ? {g}" for g in r.gates_unknown]

    out += ["", "NOTES"] + [f"  • {x}" for x in r.reasons]
    out += ["",
            "Levels are arithmetic on observed structure, not forecasts.",
            "Reconstructed from 1h/6h/24h endpoints — intra-period lows are invisible."]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze a token")
    ap.add_argument("token", help="mint address or contract address")
    ap.add_argument("--chain", default="solana")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    r = analyze(a.token, a.chain, a.capital)
    if not r:
        print(f"No pair found for {a.token}. Check the address and chain.")
        return 1
    print(render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
