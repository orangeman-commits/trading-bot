#!/usr/bin/env python3
"""Offline verification of the rule engine. No network. Run: python test_rules.py"""
import time
from sniper_bot import (Config, Candidate, Position, evaluate_exits,
                        Scorer, CircuitBreakers, discovery_filters)

CFG = Config()


def cand(price=1.0, liq=100_000, vol24=500_000, fdv=1_000_000,
         buys=600, sells=400, age_min=120, quote="SOL"):
    return Candidate(chain="solana", mint="M", symbol="TKN", pair_address="P",
                     price_usd=price, liquidity_usd=liq, fdv_usd=fdv,
                     volume_24h=vol24, txns_buys_24h=buys, txns_sells_24h=sells,
                     pair_created_ms=int((time.time() - age_min * 60) * 1000),
                     quote_symbol=quote)


def pos(entry=1.0, hours_ago=0.0, liq=100_000, hourly_vol=20_833):
    return Position(mint="M", symbol="TKN", chain="solana", pair_address="P",
                    decimals=9, entry_price=entry,
                    entry_time=time.time() - hours_ago * 3600,
                    qty=1000.0, cost_usd=1000.0, entry_liquidity=liq,
                    entry_hourly_volume=hourly_vol, high_water_price=entry)


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    return ok


results = []
print("\n§6.1 profit ladder")
p = pos()
s = evaluate_exits(p, cand(price=2.05), CFG, True, 1.0)
results.append(check("TP1 at +105% sells 50%", (s.fraction, s.reason[:6]), (0.5, "6.1 TP")))
p.qty = 500
s = evaluate_exits(p, cand(price=4.10), CFG, True, 1.0)
results.append(check("TP2 at +310% sells half of remainder", s.fraction, 0.5))
p.high_water_price = 5.0
s = evaluate_exits(p, cand(price=3.0), CFG, True, 1.0)
results.append(check("trailing stop -40% from high", s.fraction, 1.0))

print("\n§6.2 stops")
results.append(check("hard stop at -36%",
                     evaluate_exits(pos(), cand(price=0.64), CFG, True, 1.0).fraction, 1.0))
results.append(check("time stop, flat after 25h",
                     evaluate_exits(pos(hours_ago=25), cand(price=1.05), CFG, True, 1.0).reason[:5],
                     "6.2.2"))
results.append(check("volume collapse",
                     evaluate_exits(pos(), cand(price=1.1, vol24=50_000), CFG, True, 1.0).reason[:5],
                     "6.2.3"))

print("\n§6.3 emergency (must beat the profit ladder)")
results.append(check("liquidity -40% overrides a +105% TP1",
                     evaluate_exits(pos(), cand(price=2.05, liq=60_000), CFG, True, 1.0).reason[:5],
                     "6.3.1"))
results.append(check("failing sell simulation",
                     evaluate_exits(pos(), cand(price=1.5), CFG, False, 1.0).reason[:5], "6.3.4"))
results.append(check("authority restored",
                     evaluate_exits(pos(), cand(price=1.5), CFG, True, 1.0,
                                    authority_reappeared=True).reason[:5], "6.3.3"))
results.append(check("exit impact 15%",
                     evaluate_exits(pos(), cand(price=1.5), CFG, True, 15.0).reason[:5], "6.3.5"))

print("\n§6 no-op")
results.append(check("healthy +40% position holds",
                     evaluate_exits(pos(), cand(price=1.4), CFG, True, 1.0), None))

print("\n§1 discovery")
results.append(check("washed vol/liq ratio 40x rejected",
                     [g.name for g in discovery_filters(cand(vol24=4_000_000), CFG) if not g.ok],
                     ["1.5_vol_liq_ratio"]))
results.append(check("buy/sell 5.0 rejected as manufactured",
                     [g.name for g in discovery_filters(cand(buys=1000, sells=200), CFG) if not g.ok],
                     ["1.7_buy_sell_ratio"]))
results.append(check("12-minute-old pair rejected",
                     [g.name for g in discovery_filters(cand(age_min=12), CFG) if not g.ok],
                     ["1.1_pair_age"]))
results.append(check("clean pair passes all discovery gates",
                     all(g.ok for g in discovery_filters(cand(), CFG)), True))

print("\n§3 scoring")
sc = Scorer(CFG)
no_smart, _ = sc.score(cand())
with_smart, _ = sc.score(cand(), cohort_buys=3, holder_growth_per_hr=80)
spike, _ = sc.score(cand(), cohort_buys=3, holder_growth_per_hr=80,
                    sentiment={"baseline_hourly_mentions": 10,
                               "current_hourly_mentions": 200,
                               "authenticity_weighted_mentions": 300})
results.append(check(f"no smart money -> below threshold ({no_smart})",
                     no_smart < CFG.min_score, True))
results.append(check(f"cohort + growth -> tradeable ({with_smart})",
                     with_smart >= CFG.min_score, True))
results.append(check(f"mention spike penalises score ({spike} < {with_smart})",
                     spike < with_smart, True))

print("\n§7 breakers")
b = CircuitBreakers(CFG)
for _ in range(4):
    b.record_close(-100)
results.append(check("4 consecutive losses halts entries", b.check(9_000), False))
b2 = CircuitBreakers(CFG)
b2.check(6_000)
results.append(check("-40% drawdown hard-stops", b2.hard_stopped, True))


# ── regression tests for the audit fixes ──────────────────────────────────
print("\nAUDIT FIXES")
import os, sqlite3
from sniper_bot import (Journal, PaperBroker, SellResult, Scorer,
                        discovery_filters as df)

# 1. failed sell must not mutate the position
_p = pos()
_b = PaperBroker(Config(db_path="/tmp/audit.db"))
_r = _b.sell(_p, 0.0, 1.0, "should fail")
results.append(check("failed sell reports ok=False", _r.ok, False))
results.append(check("failed sell leaves qty intact", _p.qty, 1000.0))

# 2. positions persist and restore
if os.path.exists("/tmp/audit.db"): os.remove("/tmp/audit.db")
_j = Journal("/tmp/audit.db")
_j.save_positions({"M": _p})
results.append(check("position restored from db",
                     _j.load_positions()["M"].symbol, "TKN"))
os.remove("/tmp/audit.db")

# 3. holder decline scores worse than never-measured
_sc = Scorer(CFG)
_c = cand(liq=620_000, vol24=2_400_000)
_none, _ = _sc.score(_c, holder_growth_per_hr=None)
_drop, _ = _sc.score(_c, holder_growth_per_hr=-80)
results.append(check("losing holders scores below unmeasured", _drop < _none, True))

# 4. coverage floor exists in Config and matches analyzer
results.append(check("bot has coverage floor", CFG.min_signal_coverage, 50.0))

# 5. gate 1.6 now implemented
_names = [g.name for g in df(cand(), CFG)]
results.append(check("1.6_unique_traders implemented",
                     any("1.6" in n for n in _names), True))

print(f"\n{sum(results)}/{len(results)} checks passed")
raise SystemExit(0 if all(results) else 1)
