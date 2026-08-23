# Trading Rulebook v1.0

Every rule below is a hard, machine-checkable condition. No rule depends on
judgement, "feel", or discretionary override. If a rule cannot be evaluated
because data is missing, the answer is **NO TRADE** — missing data is a fail,
never a pass.

---

## 0. Scope

| Parameter | Value |
|---|---|
| Chains traded | Solana, Base, Robinhood Chain |
| Venues | On-chain AMMs (Jupiter on Solana, Uniswap V3/V4 on EVM) |
| Robinhood Crypto (brokerage) | **Not traded.** Custody of stables + majors only |
| Asset class | Newly launched low-cap tokens |
| Mode | `PAPER` until 30 consecutive days of logged paper results exist |

**Robinhood is two different things.** The *brokerage* (trading.robinhood.com)
lists a few dozen majors and cannot trade new tokens — custody only. *Robinhood
Chain* is a permissionless Arbitrum-Orbit L2, chain ID 4663, mainnet since
2026-07-01, and is traded like any other EVM chain.

Binance stays excluded: by the time a cashtag-driven token lists there, the
move this bot hunts is over.

---

## 0b. Chain support matrix

| | Solana | Base | Robinhood Chain |
|---|---|---|---|
| Chain id | — | 8453 | 4663 |
| Router | Jupiter | Uniswap V3 | Uniswap V3 **and V4** |
| Common quotes | SOL, USDC | WETH, USDC | WETH, USDG, **tokenised equities** |
| Safety provider | RugCheck | GoPlus | GoPlus (partial) |
| Honeypot check | Jupiter full-size sell sim | GoPlus | **unavailable** |
| LP lock | RugCheck, primary pool | GoPlus | **unavailable** |
| Holder concentration | RugCheck | GoPlus | GoPlus (partial) |
| Proxy contract | n/a | GoPlus | GoPlus |

### Robinhood Chain: what trading it actually costs you

Three gates cannot be evaluated there — honeypot, LP lock, and full holder
concentration. Under §2 that is an automatic rejection, so trading this chain
requires an explicit exception (`partial_coverage_chains` in Config). Taking
that exception means accepting, in writing:

1. **You cannot prove the token is sellable before you buy.** On Solana a
   full-size Jupiter quote proves the exit exists. There is no equivalent here
   until an EVM sell simulation is built (see below).
2. **You cannot prove the LP is locked.** The pool can be pulled.
3. **Equity-quoted pairs carry two exposures.** An AI/NVDA pair moves with the
   token *and* with NVDA, and your exit routes through NVDA's own liquidity,
   which is not screened.
4. **Proxy contracts are common here.** Every other gate describes the contract
   as deployed today; a proxy means that can be replaced tomorrow.

Position size on chains with partial coverage is **halved** (§4.8) because the
information is worse, not because the tokens are.

### Not yet implemented for execution

- **`RH_ROUTER` and `RH_WETH` are unset.** Deliberately: a wrong router address
  sends funds to an arbitrary contract. Source them from official docs and
  confirm on the block explorer.
- **Uniswap V4 is not supported.** Several Robinhood Chain pools are V4, which
  uses a singleton PoolManager rather than V3's router interface. The current
  adapter builds V3 `exactInputSingle` calls and will fail or misroute on V4
  pools.
- **No EVM sell simulation.** The honest substitute for a honeypot check is an
  `eth_call` simulating the sell before committing. Until that exists, gate 2.8
  is unverifiable on all EVM chains.

---

## 1. Discovery filters (universe construction)

A token enters the candidate set only if **all** hold:

| # | Rule | Threshold | Rationale |
|---|---|---|---|
| 1.1 | Pair age | ≥ 15 min, no upper limit | Age is not a quality signal. The small floor stands only because a pair minutes old has no sell history and an unreadable holder graph. Set `max_pair_age_hr = 0` for no ceiling |
| 1.2 | Liquidity (USD) | ≥ $40,000 | Below this you cannot exit a meaningful size |
| 1.3 | FDV | ≥ $150k, no upper limit | Floor filters dust. The ceiling was removed: a $40M token is a different trade from a $400k one, but not a disqualified one. Set `max_fdv_usd` > 0 to re-impose a cap |
| 1.4 | 24h volume | ≥ $150,000 | Needs real two-sided flow |
| 1.5 | Volume / liquidity ratio | ≥ 1.5 and ≤ 25 | Below = dead. **Above = wash trading** |
| 1.6 | Unique 24h traders | ≥ 250 | Distinguishes a crowd from a bot farm |
| 1.7 | Buy/sell txn ratio | ≥ 0.8 and ≤ 3.0 | > 3.0 is manufactured, not organic |
| 1.8 | Quote asset | SOL, WETH, USDC only | Exotic quote pairs are an exit trap |

---

## 2. Safety gates (hard, non-negotiable, all must pass)

This is the layer that preserves capital. It runs **before** any sentiment or
whale analysis. One failure = permanent blacklist for that mint.

| # | Gate | Pass condition |
|---|---|---|
| 2.1 | Mint authority | Revoked (`null`) |
| 2.2 | Freeze authority | Revoked (`null`) |
| 2.3 | LP tokens | ≥ 90% burned or locked ≥ 30 days |
| 2.4 | Top-10 holder concentration | < 25% of supply, excluding LP, burn, and known CEX addresses |
| 2.5 | Single largest non-LP holder | < 8% of supply |
| 2.6 | Deployer balance | < 5% of supply |
| 2.7 | Deployer history | Zero prior deploys that lost > 90% within 7 days |
| 2.8 | Sell simulation | A quote for the full intended position size must route and execute |
| 2.9 | Round-trip tax | Buy tax + sell tax < 5% |
| 2.10 | Price impact at exit size | < 4% |
| 2.11 | Metadata | Not impersonating an existing token (name/symbol collision check) |

**Honeypot rule:** 2.8 must simulate selling the *entire* intended position, not
a token dust amount. Many honeypots permit small sells.

---

## 3. Conviction score (soft signals, 0–100)

Gates 1 and 2 decide *eligibility*. Score decides *priority* among eligible
tokens. Minimum score to trade: **62**.

**Scores are normalised against measurable signals, not against 100.** Without
a cohort list and without holder-growth history, 50 of the 100 raw points are
unreachable, so an absolute score could never clear the threshold no matter how
good the token was. The report shows what fraction of the signal set was live.

**A verdict of ELIGIBLE requires ≥ 50% signal coverage.** Below that the best
possible outcome is WATCH — a high score on structural signals alone means
"not obviously broken", not "good trade".

| Signal | Max pts | Method |
|---|---|---|
| Liquidity depth & growth | 20 | Log-scaled vs. floor; LP growing over 6h scores higher |
| Holder count growth rate | 20 | New holders/hour, decelerating growth is penalised |
| Smart-money accumulation | 30 | See §3.1 |
| Volume quality | 15 | Penalise clustered same-size txns (wash signature) |
| Social sentiment | 15 | See §3.2 — **capped at 15 deliberately** |

### 3.1 Smart-money cohort

- Cohort = wallets with **verified** ≥ 6-month realised PnL, ≥ 40 closed trades,
  win rate ≥ 45%. Rebuilt monthly. Wallets are removed after 30 days of no activity.
- Signal fires only when **≥ 3 independent** cohort wallets accumulate within 6h.
- "Independent" = no funding relationship within 3 hops. One whale splitting
  across five wallets counts as **one**.
- Block-zero buyers are **excluded** — those are the team, not smart money.
- Transfers to known CEX deposit addresses are **not** counted as sells.

### 3.2 Social sentiment — treated as a risk input, not a buy signal

Cashtag mention volume for microcaps is largely manufactured, frequently by the
people planning to exit into the resulting bid. Therefore:

- Mentions are weighted by account age (< 90 days = 0.1×), authentic follower
  graph, and posting history diversity.
- Near-duplicate text or tight timing clusters → the entire cluster counts as **one** mention.
- **A vertical mention spike (> 8× the 24h baseline in one hour) subtracts 20
  points and blocks entry for 6 hours.** A spike is a distribution event.
- Sentiment can never be the deciding factor: a token scoring < 55 on the other
  four signals cannot be rescued into a trade by sentiment.

---

## 4. Position sizing

| # | Rule | Value |
|---|---|---|
| 4.1 | Base size per position | 2% of allocated trading capital |
| 4.2 | Hard cap per position | 3% — never scaled up for conviction |
| 4.3 | Liquidity cap | Position ≤ 0.5% of pool liquidity |
| 4.4 | Effective size | `min(4.1, 4.3)` |
| 4.5 | Max concurrent positions | 5 |
| 4.6 | Max total deployed | 20% of allocated capital; remainder in stables |
| 4.7 | Averaging down | **Prohibited.** No exceptions |
| 4.8 | Partial-coverage chains | Position **halved** where honeypot or LP lock cannot be verified |

Rule 4.3 is what makes exits possible. Position size is dictated by the exit,
not the entry.

---

## 5. Entry execution

| # | Rule |
|---|---|
| 5.1 | Max slippage 3%. Exceeded → abort, do not retry with looser slippage |
| 5.2 | Re-verify gates §2.1–2.6 within 60s of submitting |
| 5.3 | Quoted price impact must match §2.10 at real size |
| 5.4 | Priority fee capped at 0.4% of position value |
| 5.5 | Never chase: if price moved > 8% between scan and execution, abort |
| 5.6 | One entry per mint per 24h, regardless of outcome |

---

## 6. Exit rules

Exits are evaluated every 30 seconds per open position. **The first rule that
fires wins.** Exit rules are never disabled, widened, or overridden.

### 6.1 Profit ladder

| Trigger | Action |
|---|---|
| +100% | Sell 50% — cost basis is now recovered, remainder is house money |
| +300% | Sell 25% |
| Remaining 25% | Trailing stop, 35% below all-time-high since entry |

### 6.2 Loss and decay stops

| # | Trigger | Action |
|---|---|---|
| 6.2.1 | −35% from entry | Full exit |
| 6.2.2 | < +20% after 24h held | Full exit (capital has an opportunity cost) |
| 6.2.3 | 1h volume < 15% of entry-time hourly average | Full exit |

### 6.3 Emergency exits (bypass the ladder, market-sell everything)

| # | Trigger |
|---|---|
| 6.3.1 | Pool liquidity drops > 25% from entry-time value |
| 6.3.2 | Deployer or any top-5 holder sells > 20% of their balance |
| 6.3.3 | Mint or freeze authority reappears |
| 6.3.4 | Sell simulation fails or round-trip tax rises above 5% |
| 6.3.5 | Exit price impact exceeds 10% |

**6.3.1 is the single most valuable rule in this document.** It is what
separates a −35% stop loss from a −100% rug.

---

## 7. Risk circuit breakers (portfolio level)

| # | Trigger | Action |
|---|---|---|
| 7.1 | Daily realised PnL ≤ −10% of capital | Halt new entries 24h |
| 7.2 | 4 consecutive losing trades | Halt new entries 24h, require manual restart |
| 7.3 | Weekly drawdown ≤ −20% | Halt 7 days, mandatory strategy review |
| 7.4 | Total drawdown ≤ −35% from peak | **Full stop.** Bot does not restart itself |
| 7.5 | RPC / price feed stale > 90s | Halt entries, keep exit monitoring alive |
| 7.6 | 3 failed transactions in 10 min | Halt 1h |

Halts block **entries only**. Exit monitoring never halts.

---

## 8. Operational rules

1. Paper mode for 30 days minimum. Live mode requires manually editing a config
   file, never a CLI flag.
2. Dedicated hot wallet. Only the current trading allocation lives in it.
   Profits sweep to cold storage weekly.
3. Private keys from environment or KMS. Never in code, config files, or logs.
4. Every decision — including rejections and the reason — is logged to durable
   storage. A strategy you cannot audit is a strategy you cannot improve.
5. Kill switch: a `HALT` file on disk stops all entries within one loop cycle.
6. Weekly review: win rate, average win/loss, time-to-exit, and **which gate
   rejected the most tokens that later ran**. Tune from data, not from regret.

---

## 8b. Implementation status

Audited 2026-08-23. Fixed in this pass:

| Issue | Was | Now |
|---|---|---|
| Failed sells | Position deleted even when the transaction failed | Sells return success/failure; position retained and retried |
| Position state | Lost on restart | Persisted to SQLite, reconciled against wallet balance on startup |
| Live equity | `cash=0` at start → instant −100% drawdown → hard stop | Wallet balance and SOL price fetched before trading; refuses to start blind |
| Signal coverage | Analyzer said WATCH at 35%, bot bought anyway | `min_signal_coverage` enforced in both |
| Holder decline | Treated as "unmeasured" | Three-state: None / measured / negative penalised |
| Missing tax or rugged flag | Defaulted to safe values, silently PASSED | Return None → UNKNOWN → rejection |
| §1.6, §7.3, §7.5, `strict_lp_check` | Config values, never enforced | Implemented |
| §6.3.2 insider dump | Parameter existed, nothing computed it | Compares top-10 concentration against entry |

Still outstanding: §3.1 smart-money cohort, §3.2 attention data, §2.7 deployer
history, §2.11 impersonation, EVM safety in the bot (present in the analyzer
only), and the analyzer/executor strategy mismatch below.

**Analyzer and executor still differ.** `analyze.py` produces retracement
entries, a structural stop and R-multiple targets; `sniper_bot.py` buys at
market and exits on the §6 ladder. They agree on gates, scoring and sizing
constraints, but not on entry timing or exit levels. Treat the analyzer as a
screen, not as the plan the bot will follow.

## 9. Known limitations

Stated plainly, because a rulebook that oversells itself is dangerous:

- **Tops cannot be timed.** §6.1 does not catch tops; it guarantees you sell
  into strength and never round-trip a winner to zero. That is the achievable goal.
- **Copy-trading lags.** Detecting a cohort wallet's buy takes seconds to
  minutes. On a fast-moving token that is the entire move. §3.1 is an
  accumulation-confirmation signal, not a front-run.
- **Sentiment is adversarial.** Anything measurable through a public API is
  measurable by the people trying to manipulate you.
- **Gates 2.7 and 2.11 need external datasets** (deployer history, token
  registries) that must be sourced and maintained. Without them, gate coverage
  is incomplete and position sizes should be halved.
- **The base rate is bad.** Most participants in this market segment lose money
  net. This rulebook is designed to make ruin unlikely and to keep losses
  small and survivable. It does not make the strategy profitable.
