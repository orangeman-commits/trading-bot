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


def _obs_db():
    """Same store the engine uses, so CLI and app share observations."""
    import sqlite3
    from sniper_bot import app_data_dir
    path = app_data_dir() / "bot_state.db"
    db = sqlite3.connect(str(path))
    db.execute("""CREATE TABLE IF NOT EXISTS observations(
        mint TEXT, ts REAL, holders INTEGER, liquidity REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS obs_mint ON observations(mint, ts)")
    db.commit()
    return db


def record_and_measure_growth(mint: str, holders: int,
                              liquidity: float) -> tuple[Optional[float], str]:
    """Persist this observation and return (holders_per_hour, note).

    Holder growth is a rate, so it needs two samples. The first analysis of a
    token can only record; the second, ten or more minutes later, can measure.
    This is real data rather than an API call — which is why it works without
    any key, and why it cannot be conjured from a single run.
    """
    try:
        db = _obs_db()
        rows = db.execute(
            "SELECT ts, holders FROM observations WHERE mint=? ORDER BY ts",
            (mint,)).fetchall()
        db.execute("INSERT INTO observations VALUES (?,?,?,?)",
                   (mint, time.time(), holders, liquidity))
        db.commit()

        usable = [(t, h) for t, h in rows if h and h > 0]
        if not usable:
            return None, f"first observation ({holders:,} holders) — run again in 10+ min"
        t0, h0 = usable[0]
        hours = (time.time() - t0) / 3600
        if hours < 0.17:
            return None, f"only {hours*60:.0f} min of history — need 10+ min"
        rate = (holders - h0) / hours
        span = f"{hours:.1f}h" if hours >= 1 else f"{hours*60:.0f}m"
        # Negative rates are returned as-is: losing holders is a real signal,
        # not an absence of one.
        note = f"{rate:+.0f} holders/hr over {span} ({h0:,} → {holders:,})"
        if rate < 0:
            note += "  ⚠ LOSING HOLDERS"
        return rate, note
    except Exception as e:  # noqa: BLE001
        LOG.debug("growth tracking failed: %s", e)
        return None, "tracking unavailable"

# DexScreener chainId -> GoPlus chain id. GoPlus has no Robinhood Chain
# support yet (launched 2026-07-01), so that entry is None and the screen
# degrades to "unverifiable" rather than silently passing.
EVM_CHAIN_IDS = {
    "base": 8453, "ethereum": 1, "arbitrum": 42161, "optimism": 10,
    "bsc": 56, "polygon": 137, "avalanche": 43114,
    # Robinhood Chain (4663). GoPlus coverage is partial here — honeypot in
    # particular comes back unknown — but tax, open-source and proxy do
    # resolve, so mapping it to None discarded real data.
    "robinhood": 4663, "robinhoodchain": 4663,
}


# ────────────────────────────── model ──────────────────────────────────────

@dataclass
class GeckoData:
    pool_count: int = 0
    total_liquidity: float = 0.0
    total_volume: float = 0.0
    buyers: int = 0
    sellers: int = 0
    wallet_ratio: Optional[float] = None
    repeat_buy_skew: Optional[float] = None
    buys_per_buyer: Optional[float] = None
    unique_traders: int = 0
    true_low_24h: Optional[float] = None
    true_high_24h: Optional[float] = None
    candles_used: int = 0
    note: str = ""


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
    gates_passed: list = field(default_factory=list)
    score: int = 0
    score_parts: dict = field(default_factory=dict)
    verdict: str = "UNKNOWN"
    reasons: list = field(default_factory=list)
    attention: Optional[dict] = None
    holder_count: int = 0
    growth_note: str = ""
    gecko: Optional[GeckoData] = None
    depth_used: float = 0.0


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
                   hard_stop_pct: float = -35.0,
                   true_low: Optional[float] = None) -> Levels:
    if true_low and 0 < true_low < price:
        # Real traded low from candles. Strictly better than reconstruction,
        # which is blind to intra-period wicks and typically sits well above
        # the actual low — putting stops inside the day's real range.
        base, window = true_low, "24h candle low"
    else:
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
                   max_liq_pct: float = 0.5,
                   partial_coverage: bool = False) -> Sizing:
    """The output most signal posts omit entirely."""
    s = Sizing()
    s.max_by_liquidity = liquidity * max_liq_pct / 100
    s.max_by_rule = min(capital * base_pos_pct / 100, capital * max_pos_pct / 100)
    if partial_coverage:
        s.max_by_rule *= 0.5        # §4.8 — worse information, smaller bet

    # Risk-based: lose `risk_pct` of capital if the stop hits.
    if stop_pct < 0:
        s.max_by_risk = (capital * risk_pct / 100) / (abs(stop_pct) / 100)
    else:
        s.max_by_risk = 0.0

    options = {
        "liquidity (§4.3 — exit capacity)": s.max_by_liquidity,
        ("position cap (§4.8 halved — partial coverage)" if partial_coverage
         else "position cap (§4.1/4.2)"): s.max_by_rule,
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

    # ── GeckoTerminal: real candles, all pools, dollar flow ────────────
    gd = None
    try:
        from gecko import GeckoTerminal
        gt = GeckoTerminal()
        agg = gt.aggregate(c.chain, c.mint)
        if agg:
            f = agg["flow"]
            gd = GeckoData(
                pool_count=agg["pool_count"],
                total_liquidity=agg["total_liquidity"],
                total_volume=agg["total_volume_24h"],
                buyers=f.buyers, sellers=f.sellers,
                wallet_ratio=f.wallet_ratio,
                repeat_buy_skew=f.repeat_buy_skew,
                buys_per_buyer=f.buys_per_buyer,
                unique_traders=f.unique_traders)
            candles = gt.ohlcv(c.chain, agg["primary_pool"].address, "hour", 1, 168)
            sw = gt.swing_low(candles, 24)
            if sw:
                gd.true_low_24h, gd.true_high_24h, gd.candles_used = sw
            else:
                gd.note = "no candle data"
        else:
            gd = GeckoData(note="token not indexed by GeckoTerminal")
    except Exception as e:  # noqa: BLE001
        gd = GeckoData(note=f"unavailable: {e}")
    rep.gecko = gd

    rep.levels = compute_levels(c.price_usd, rep.chg_1h, rep.chg_6h, rep.chg_24h,
                                cfg.hard_stop_pct,
                                true_low=gd.true_low_24h if gd else None)
    # Exit capacity spans every pool a router can split across, not just the
    # deepest one. Falls back to the single-pool figure when unavailable.
    depth = (gd.total_liquidity if gd and gd.total_liquidity > c.liquidity_usd
             else c.liquidity_usd)
    rep.sizing = compute_sizing(
        capital, depth, rep.levels.stop_pct,
        partial_coverage=c.chain in cfg.partial_coverage_chains)
    rep.depth_used = depth

    # Discovery + safety gates
    for g in discovery_filters(c, cfg):
        # Gate 1.7 counts transactions and cannot tell manufactured volume
        # (many tiny buys, no net dollars) from real accumulation (many buys
        # AND real dollars in). When GeckoTerminal gives us flow, use it.
        if g.name == "1.5_vol_liq_ratio" and gd and gd.total_liquidity > 0 \
                and gd.total_volume > 0:
            # DexScreener reports ONE pool. A token trading across 20 pools
            # has its real ratio computed from aggregate depth and volume;
            # gating on the fragment produced a 0.4x reading where the true
            # figure was 1.13x.
            agg_ratio = gd.total_volume / gd.total_liquidity
            detail = (f"{agg_ratio:.2f}x across {gd.pool_count} pools "
                      f"(single-pool view said {c.vol_liq_ratio:.2f}x)")
            if cfg.min_vol_liq_ratio <= agg_ratio <= cfg.max_vol_liq_ratio:
                rep.gates_passed.append(f"1.5_vol_liq_ratio = {detail}")
            else:
                rep.gates_failed.append(f"1.5_vol_liq_ratio = {detail}")
            continue

        if g.name == "1.7_buy_sell_ratio" and gd and gd.wallet_ratio is not None:
            # Judge on UNIQUE WALLETS, not transaction counts. A handful of
            # wallets making repeat buys inflates the txn ratio without
            # representing real demand.
            wr, skew = gd.wallet_ratio, gd.repeat_buy_skew
            detail = (f"txn {g.detail} but wallets {wr:.2f} "
                      f"({gd.buyers:,} buyers / {gd.sellers:,} sellers)")
            if skew and skew >= cfg.max_repeat_buy_skew:
                rep.gates_failed.append(
                    f"1.7_wallet_ratio = {detail}, repeat-buy skew {skew:.1f}x "
                    f"— {gd.buys_per_buyer:.1f} buys per buyer, manufactured")
            elif cfg.min_buy_sell_ratio <= wr <= cfg.max_buy_sell_ratio:
                rep.gates_passed.append(f"1.7_wallet_ratio = {detail}")
            else:
                rep.gates_failed.append(f"1.7_wallet_ratio = {detail}")
            continue
        if g.ok:
            rep.gates_passed.append(f"{g.name} = {g.detail}")
        else:
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
                else:
                    rep.gates_passed.append(f"{g.name} = {g.detail}")
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
                    else:
                        rep.gates_passed.append(f"{name} = {detail}")
            except Exception as e:  # noqa: BLE001
                rep.gates_unknown.append(f"EVM safety screen error: {e}")
    else:
        rep.gates_unknown.append(f"no safety adapter for chain '{c.chain}'")

    growth, growth_note = None, "not tracked on this chain"
    if c.chain == "solana":
        try:
            from safety_data import RugCheck
            rc_rep = RugCheck().report(c.mint)
            total = int((rc_rep or {}).get("totalHolders") or 0)
            if total > 0:
                growth, growth_note = record_and_measure_growth(
                    c.mint, total, c.liquidity_usd)
                rep.holder_count = total
        except Exception as e:  # noqa: BLE001
            growth_note = f"tracking error: {e}"
    rep.growth_note = growth_note

    # Score on aggregate depth and volume. Scoring volume_quality off the
    # single-pool ratio gave PONS 0.1/15 on a 0.42x reading when the real
    # figure across 20 pools was 1.13x.
    scored = c
    if gd and gd.total_liquidity > c.liquidity_usd and gd.total_volume > 0:
        from dataclasses import replace as _replace
        scored = _replace(c, liquidity_usd=gd.total_liquidity,
                          volume_24h=gd.total_volume)
    rep.score, rep.score_parts = Scorer(cfg).score(
        scored, holder_growth_per_hr=growth, sentiment=sentiment)

    # Verdict
    partial = c.chain in cfg.partial_coverage_chains

    if rep.gates_failed:
        rep.verdict = "AVOID"
        rep.reasons.append(f"{len(rep.gates_failed)} hard gate(s) failed")
    elif rep.gates_unknown and not partial:
        rep.verdict = "INSUFFICIENT DATA"
        rep.reasons.append(f"{len(rep.gates_unknown)} gate(s) unverifiable — "
                           f"missing data counts as a rejection")
    elif rep.gates_unknown and partial:
        # §0b exception: this chain has no honeypot or LP-lock provider. The
        # gates still cannot be evaluated — we are choosing to proceed without
        # them, at half size, rather than pretending they passed.
        rep.reasons.append(
            f"{len(rep.gates_unknown)} gate(s) UNVERIFIABLE on {c.chain} "
            f"(§0b) — you cannot prove this token is sellable or that its LP "
            f"is locked. Position halved per §4.8")
    elif rep.score < cfg.min_score:
        rep.verdict = "WATCH"
        rep.reasons.append(f"score {rep.score} below threshold {cfg.min_score}")
    elif rep.score_parts.get("_available_weight", 0) < 50:
        # Never call something ELIGIBLE on a partial picture. With no cohort
        # list and no holder history, only 35% of the signal weight is live —
        # a high score there means "structurally sound", not "good trade".
        rep.verdict = "WATCH"
        rep.reasons.append(
            f"score {rep.score} but only "
            f"{rep.score_parts['_available_weight']:.0f}% of signals measurable "
            f"— structural checks only, no smart-money or attention data")
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
    if c.quote_symbol not in cfg.allowed_quotes and \
            c.chain in cfg.equity_quote_chains:
        rep.reasons.append(
            f"quoted in {c.quote_symbol}, a tokenised equity — your P&L moves "
            f"with both this token AND {c.quote_symbol}. Exiting routes through "
            f"{c.quote_symbol}'s own liquidity, which is not screened here")

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
        (f"liquidity  ${r.gecko.total_liquidity:,.0f} across {r.gecko.pool_count} pools"
         if r.gecko and r.gecko.total_liquidity > r.liquidity * 1.2
         else f"liquidity  ${r.liquidity:,.0f}"),
        (f"volume 24h ${r.gecko.total_volume:,.0f}   "
         f"(vol/liq {r.gecko.total_volume/r.gecko.total_liquidity:.1f}x, "
         f"{r.gecko.pool_count} pools)"
         if r.gecko and r.gecko.total_liquidity > 0 and r.gecko.total_volume > 0
         else f"volume 24h ${r.volume_24h:,.0f}   (vol/liq {r.vol_liq:.1f}x)"),
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

    eff_vl = r.vol_liq
    if r.gecko and r.gecko.total_liquidity > 0 and r.gecko.total_volume > 0:
        eff_vl = r.gecko.total_volume / r.gecko.total_liquidity
    if eff_vl > 25:
        out.append(f"           ⚠ vol/liq {eff_vl:.0f}x suggests wash trading")
    elif eff_vl < 1.5:
        out.append(f"           ⚠ vol/liq {eff_vl:.1f}x — thin, hard to exit")

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
    if r.depth_used > r.liquidity * 1.2:
        out.append(f"  liquidity cap from ${r.depth_used:,.0f} aggregate depth, "
                   f"not the ${r.liquidity:,.0f} single-pool figure")
    if S.exit_impact_pct is not None:
        out.append(f"  exit impact  {S.exit_impact_pct:.2f}% at that size")

    g = r.gecko
    if g and (g.pool_count or g.note):
        out += ["", "GECKOTERMINAL"]
        if g.pool_count:
            out.append(f"  pools        {g.pool_count}  "
                       f"(total liq ${g.total_liquidity:,.0f})")
            if r.liquidity > 0 and g.total_liquidity > r.liquidity * 1.2:
                out.append(f"  ⚠ DexScreener shows ${r.liquidity:,.0f} — "
                           f"single pool, understates depth")
            out.append(f"  volume 24h   ${g.total_volume:,.0f} across all pools")
            if g.wallet_ratio is not None:
                out.append(f"  wallets      {g.buyers:,} buyers / "
                           f"{g.sellers:,} sellers  (ratio {g.wallet_ratio:.2f})")
            if g.repeat_buy_skew:
                tag = ("normal" if g.repeat_buy_skew < 2 else
                       "elevated" if g.repeat_buy_skew < 4 else "MANUFACTURED")
                out.append(f"  repeat-buy   {g.buys_per_buyer:.1f} buys/buyer, "
                           f"skew {g.repeat_buy_skew:.1f}x — {tag}")
        if g.true_low_24h:
            rng = (g.true_high_24h / g.true_low_24h - 1) * 100
            out.append(f"  24h true low  ${g.true_low_24h:.8g}")
            out.append(f"  24h true high ${g.true_high_24h:.8g}   "
                       f"(range {rng:.0f}%, {g.candles_used} candles)")
        if g.note:
            out.append(f"  {g.note}")

    if r.holder_count:
        out += ["", f"HOLDERS  {r.holder_count:,}", f"  {r.growth_note}"]

    avail = (r.score_parts or {}).get("_available_weight", 0)
    out += ["", f"SCORE  {r.score}/100   (from {avail:.0f}% of signal weight)"]
    for k, v in (r.score_parts or {}).items():
        if k.startswith("_"):
            continue
        note = "  (unmeasured)" if v == 0 and k in (
            "smart_money", "sentiment", "holder_growth") else ""
        out.append(f"  {k:<16} {v:>6.1f}{note}")

    if r.attention:
        out += ["", "ATTENTION", f"  {r.attention}"]
    else:
        out += ["", "ATTENTION  not configured (needs X API) — unmeasured"]

    if r.gates_failed:
        out += ["", "FAILED GATES"] + [f"  ✕ {g}" for g in r.gates_failed]
    if r.gates_unknown:
        out += ["", "UNVERIFIABLE"] + [f"  ? {g}" for g in r.gates_unknown]

    out += ["", "NOTES"] + [f"  • {x}" for x in r.reasons]
    used_candles = bool(r.gecko and r.gecko.true_low_24h)
    out += ["", "Levels are arithmetic on observed structure, not forecasts.",
            "Base from real candle lows (GeckoTerminal)." if used_candles else
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
