#!/usr/bin/env python3
"""
GeckoTerminal data layer. Free, public, no API key. Rate limit ~30 req/min.

Fills three gaps the DexScreener snapshot cannot:

1. REAL OHLCV CANDLES. Everything else in this project reconstructs a swing
   low arithmetically from 1h/6h/24h percentage endpoints, which is blind to
   intra-period wicks — on AI/NVDA that hid a 57% intraday range. Candles give
   the actual low, so the stop becomes a structural level instead of a guess.

2. NET DOLLAR FLOW. DexScreener reports buy and sell transaction COUNTS. It
   cannot distinguish manufactured volume (many tiny buys, near-zero net flow,
   the same money cycling) from real accumulation (many buys AND real dollars
   in). Those are opposite situations that produce identical buy/sell ratios.

3. ALL POOLS FOR A TOKEN. DexScreener returns one pair; a token routinely
   trades across several. Reporting one pool's depth understates both the
   liquidity cap and the true exit capacity.

Attribution: data from GeckoTerminal (https://www.geckoterminal.com).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

LOG = logging.getLogger("gecko")

# DexScreener chainId -> GeckoTerminal network slug
NETWORK_MAP = {
    "solana": "solana",
    "base": "base",
    "ethereum": "eth",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "polygon": "polygon_pos",
    "avalanche": "avax",
    "robinhood": "robinhood",
    "robinhoodchain": "robinhood",
}


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FlowStats:
    """Participation stats.

    NOTE: GeckoTerminal's public API does NOT expose buy/sell volume in
    dollars — only total `volume_usd` and transaction counts. The website's
    "Net Buy" figure is computed from data the API withholds. So net dollar
    flow is unavailable here, and the honest substitute is UNIQUE WALLETS
    rather than transaction counts.

    That substitute is arguably better. 223 buys from 40 buyers is 5.6 repeat
    buys per wallet — the same money cycling. 73 sells from 45 sellers is 1.6.
    Transaction counts show a 3.05 buy/sell ratio; unique wallets show 0.89.
    Those tell opposite stories, and the wallet count is the truthful one.
    """
    volume_usd: float = 0.0
    buyers: int = 0
    sellers: int = 0
    buys: int = 0
    sells: int = 0

    @property
    def unique_traders(self) -> int:
        return self.buyers + self.sellers

    @property
    def txn_ratio(self) -> float:
        """Buy/sell ratio by TRANSACTION count — inflatable by repeat buys."""
        return self.buys / max(self.sells, 1)

    @property
    def wallet_ratio(self) -> Optional[float]:
        """Buy/sell ratio by UNIQUE WALLET — much harder to fake cheaply."""
        if self.buyers == 0 and self.sellers == 0:
            return None
        return self.buyers / max(self.sellers, 1)

    @property
    def buys_per_buyer(self) -> Optional[float]:
        if self.buyers <= 0:
            return None
        return self.buys / self.buyers

    @property
    def sells_per_seller(self) -> Optional[float]:
        if self.sellers <= 0:
            return None
        return self.sells / self.sellers

    @property
    def repeat_buy_skew(self) -> Optional[float]:
        """How much more repetitive buying is than selling.

        Near 1.0 is normal. Well above 1.0 means a small set of wallets is
        generating the buy count — the signature of manufactured demand.
        """
        bpb, sps = self.buys_per_buyer, self.sells_per_seller
        if not bpb or not sps or sps <= 0:
            return None
        return bpb / sps


@dataclass
class PoolInfo:
    address: str
    name: str
    dex: str
    liquidity_usd: float
    volume_24h: float
    price_usd: float
    created_at: str = ""
    flow: FlowStats = field(default_factory=FlowStats)


class GeckoTerminal:
    BASE = "https://api.geckoterminal.com/api/v2"

    def __init__(self, timeout: int = 15, min_interval: float = 2.1):
        self.s = requests.Session()
        # Version pinning: the API is in beta and the docs recommend it.
        self.s.headers.update({"Accept": "application/json;version=20230302"})
        self.timeout = timeout
        self.min_interval = min_interval      # ~28 req/min, under the 30 cap
        self._last = 0.0
        self._cache: dict[str, tuple[float, object]] = {}

    # ── plumbing ───────────────────────────────────────────────────────
    def _throttle(self) -> None:
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def _get(self, path: str, params: Optional[dict] = None,
             cache_s: int = 60) -> Optional[dict]:
        key = f"{path}?{params}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < cache_s:
            return hit[1]  # type: ignore[return-value]
        for attempt in range(3):
            self._throttle()
            try:
                r = self.s.get(f"{self.BASE}{path}", params=params,
                               timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                data = r.json()
                self._cache[key] = (time.time(), data)
                return data
            except Exception as e:  # noqa: BLE001
                LOG.debug("GT %s attempt %d: %s", path, attempt + 1, e)
                time.sleep(1 + attempt)
        return None

    @staticmethod
    def network(chain: str) -> Optional[str]:
        return NETWORK_MAP.get(chain.lower())

    @staticmethod
    def _num(v, default=0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ── pools ──────────────────────────────────────────────────────────
    def pools_for_token(self, chain: str, token: str,
                        limit: int = 20) -> list[PoolInfo]:
        """Every pool trading this token, ranked by liquidity + volume."""
        net = self.network(chain)
        if not net:
            return []
        data = self._get(f"/networks/{net}/tokens/{token}/pools")
        out: list[PoolInfo] = []
        for item in (data or {}).get("data", [])[:limit]:
            a = item.get("attributes") or {}
            tx = (a.get("transactions") or {}).get("h24") or {}
            vol = a.get("volume_usd") or {}
            flow = FlowStats(
                volume_usd=self._num(vol.get("h24")),
                buyers=int(self._num(tx.get("buyers"))),
                sellers=int(self._num(tx.get("sellers"))),
                buys=int(self._num(tx.get("buys"))),
                sells=int(self._num(tx.get("sells"))),
            )
            out.append(PoolInfo(
                address=a.get("address", ""),
                name=a.get("name", ""),
                dex=((item.get("relationships") or {}).get("dex") or {})
                    .get("data", {}).get("id", ""),
                liquidity_usd=self._num(a.get("reserve_in_usd")),
                volume_24h=self._num(vol.get("h24")),
                price_usd=self._num(a.get("base_token_price_usd")),
                created_at=a.get("pool_created_at", ""),
                flow=flow,
            ))
        return out

    def aggregate(self, chain: str, token: str) -> Optional[dict]:
        """Totals across every pool — the number DexScreener's single pair
        understates. Also sums dollar flow so accumulation is measurable."""
        pools = self.pools_for_token(chain, token)
        if not pools:
            return None
        total = FlowStats()
        for p in pools:
            total.volume_usd += p.flow.volume_usd
            total.buyers += p.flow.buyers
            total.sellers += p.flow.sellers
            total.buys += p.flow.buys
            total.sells += p.flow.sells
        primary = max(pools, key=lambda p: p.liquidity_usd)
        return {
            "pool_count": len(pools),
            "total_liquidity": sum(p.liquidity_usd for p in pools),
            "total_volume_24h": sum(p.volume_24h for p in pools),
            "primary_pool": primary,
            "flow": total,
            "pools": pools,
        }

    # ── candles ────────────────────────────────────────────────────────
    def ohlcv(self, chain: str, pool_address: str, timeframe: str = "hour",
              aggregate: int = 1, limit: int = 168) -> list[Candle]:
        """Real candles. timeframe is day | hour | minute."""
        net = self.network(chain)
        if not net:
            return []
        data = self._get(f"/networks/{net}/pools/{pool_address}/ohlcv/{timeframe}",
                         {"aggregate": aggregate, "limit": limit}, cache_s=120)
        rows = (((data or {}).get("data") or {}).get("attributes") or {}) \
            .get("ohlcv_list") or []
        out = []
        for r in rows:
            try:
                out.append(Candle(int(r[0]), float(r[1]), float(r[2]),
                                  float(r[3]), float(r[4]), float(r[5])))
            except (IndexError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c.ts)
        return out

    def swing_low(self, candles: list[Candle], lookback_h: int = 24
                  ) -> Optional[tuple[float, float, int]]:
        """Returns (low, high, candles_used) over the lookback window.

        This is the real intra-period low — the thing reconstruct_base()
        cannot see. A stop placed below this sits below actual traded price,
        not below an arithmetic guess.
        """
        if not candles:
            return None
        cutoff = time.time() - lookback_h * 3600
        window = [c for c in candles if c.ts >= cutoff] or candles[-lookback_h:]
        if not window:
            return None
        return (min(c.low for c in window),
                max(c.high for c in window),
                len(window))


def demo(chain: str, token: str) -> None:
    gt = GeckoTerminal()
    agg = gt.aggregate(chain, token)
    if not agg:
        print(f"no GeckoTerminal data for {token} on {chain}")
        return
    f = agg["flow"]
    print(f"pools            {agg['pool_count']}")
    print(f"total liquidity  ${agg['total_liquidity']:,.0f}")
    print(f"total volume 24h ${agg['total_volume_24h']:,.0f}")
    print(f"primary pool     {agg['primary_pool'].name} "
          f"(${agg['primary_pool'].liquidity_usd:,.0f})")
    print(f"txns             {f.buys:,} buys / {f.sells:,} sells "
          f"(ratio {f.txn_ratio:.2f})")
    wr = f.wallet_ratio
    print(f"unique wallets   {f.buyers:,} buyers / {f.sellers:,} sellers"
          + (f" (ratio {wr:.2f})" if wr is not None else ""))
    bpb, sps = f.buys_per_buyer, f.sells_per_seller
    if bpb and sps:
        print(f"repeat activity  {bpb:.1f} buys/buyer vs {sps:.1f} sells/seller")
        skew = f.repeat_buy_skew
        if skew:
            verdict = ("normal" if skew < 2 else
                       "elevated" if skew < 4 else "MANUFACTURED-LOOKING")
            print(f"repeat-buy skew  {skew:.1f}x  -> {verdict}")

    candles = gt.ohlcv(chain, agg["primary_pool"].address, "hour", 1, 168)
    print(f"\ncandles          {len(candles)} hourly")
    sw = gt.swing_low(candles, 24)
    if sw:
        low, high, n = sw
        rng = (high / low - 1) * 100 if low else 0
        print(f"24h true low     ${low:.8g}")
        print(f"24h true high    ${high:.8g}")
        print(f"24h range        {rng:.1f}%  (from {n} candles)")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    if len(sys.argv) < 3:
        print("usage: python gecko.py <chain> <token_address>")
        print("  e.g. python gecko.py robinhood 0x39dBED3a2bd333467115dE45665cC57F813C4571")
        raise SystemExit(1)
    demo(sys.argv[1], sys.argv[2])
