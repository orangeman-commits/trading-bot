# Solana Trading Bot

Rule-driven scanner, safety screen, and execution engine. Implements `RULES.md`.

## Install
    pip install requests solders base58

## 1. Verify the safety data feed FIRST
    python safety_data.py <a_known_mint>

Every field must print a value. Any `None` means that gate will reject every
token until the field mapping in `ReportView` is corrected — RugCheck's schema
has changed before.

## 2. Paper trade
    python sniper_bot.py --once -v      # one cycle
    python sniper_bot.py                # continuous

Executes against real quoted prices, moves no funds. Run 30 days (rule 8.1).

## 3. Live
    export SOLANA_PRIVATE_KEY='...'          # base58 or JSON array
    export I_UNDERSTAND_LIVE_TRADING=yes
    export JUPITER_API_KEY='...'             # optional, higher rate limits
    export RUGCHECK_API_KEY='...'            # optional
    export SOLANA_RPC='https://...'          # Helius/Triton; public RPC will throttle
    python sniper_bot.py --live

Use a dedicated wallet holding only the current trading allocation.

## Kill switch
    touch HALT      # blocks new entries within one loop cycle

Exits are never halted — by design.

## Telegram control

    pip install "python-telegram-bot>=21" pynacl
    # @BotFather -> /newbot -> token ; @userinfobot -> your numeric id
    export TELEGRAM_BOT_TOKEN='123456:ABC...'
    export TELEGRAM_ALLOWED_IDS='11122233'
    python telegram_bot.py

**The allowlist is the whole security model.** Bot usernames are publicly
discoverable; without `TELEGRAM_ALLOWED_IDS` any stranger who finds your bot
can command your wallet. It refuses to start unset. Never run it in a group.

Commands: `/status` `/positions` `/scan` `/why <mint>` `/buy` `/sell` `/panic`
`/halt` `/resume` `/balances`. Mutating commands require an inline-button
confirmation that expires in 120s, and `/buy` still enforces the §4.2 size cap.

Alerts push on entry, exit, and hard stop. If Telegram is unreachable, trading
and exits continue — alerting is best-effort by design.

## Analyzing a single token

    python analyze.py <mint> --capital 10000

Outputs entry zones, stop, R:R, position size, gate results, and a verdict.

Entry/stop levels are **arithmetic on observed structure, not forecasts**.
Prior prices are reconstructed from DexScreener's 1h/6h/24h percentage changes,
so intra-period wick lows are invisible — real swing lows may sit lower.

The output most signal posts omit is the sizing block: how much you can buy
while still being able to exit (capped at 0.5% of pool liquidity), and what
your own sell does to the price at that size.

Also available as `/analyze <mint>` in Telegram.

## Venues

Robinhood is **two separate things**, and the bot treats them separately:

| Venue | Type | Assets | Status |
|---|---|---|---|
| Solana | SVM chain | any SPL mint, via Jupiter | working |
| Base | EVM L2 (8453) | any ERC-20, via Uniswap V3 | working |
| Robinhood Chain | EVM L2 (4663) | any ERC-20 + Stock Tokens | needs router address |
| Robinhood Crypto | brokerage API | ~two dozen majors | working |

**Robinhood Chain** launched mainnet 2026-07-01 on the Arbitrum Orbit stack —
chain ID 4663, RPC `rpc.mainnet.chain.robinhood.com`, gas in ETH, Uniswap live
from day one. Because it is EVM, `evm_venue.py` serves it and Base with the
same code.

To enable it, set `RH_ROUTER` and `RH_WETH` from the official chain docs and
confirm both on `robinhoodchain.blockscout.com`. They are deliberately unset —
a wrong router address sends funds to an arbitrary contract, and I could not
verify these.

    python evm_venue.py     # preflight: shows what is and isn't configured

**Robinhood Crypto** (the brokerage API at `trading.robinhood.com`, Ed25519,
US accounts) is unrelated to the chain. Majors only — routing a fresh mint
there fails by design.

### EVM-specific risks
- **Approvals are exact-amount, never unlimited**, and revoked after a full
  exit. Unlimited approvals are the most common way EVM traders get drained
  long after a trade closes.
- **`verify()` checks `eth_chainId` before every swap.** A phishing-RPC
  ecosystem grew up around Robinhood Chain post-launch; a malicious RPC serves
  fake balances and quotes. This check is not optional.
- **Robinhood Chain's sequencer is centralized and permissioned**, and
  transactions can be screened. Your bot's orders can be delayed or refused in
  ways that cannot happen on Solana or Base.
- **Safety screening differs per VM.** RugCheck is Solana-only; EVM chains use
  GoPlus (`is_honeypot`, buy/sell tax, LP lock, blacklist functions).

## Files
| File | Role |
|---|---|
| `RULES.md` | The rulebook. Change thresholds here first, then in `Config` |
| `sniper_bot.py` | Scanner, gates, scoring, position manager, exit engine |
| `execution.py` | Jupiter Swap v1, signing, send/confirm |
| `safety_data.py` | RugCheck client + schema verifier |
| `telegram_bot.py` | Telegram control surface + alerting |
| `analyze.py` | Single-token analysis: levels, sizing, verdict |
| `launcher.py` | Desktop GUI (packaged by `tradingbot.spec`) |
| `venues.py` | Venue router + Solana / EVM / brokerage adapters |
| `evm_venue.py` | Base + Robinhood Chain (EVM) swaps, approvals, GoPlus |
| `test_rules.py` | Offline rule-engine tests (no network) |

## Still unimplemented
- **§3.1 smart-money cohort** — needs verified-PnL wallet list + funding-graph
  dedupe. `cohort_enabled=False`; its 30 points redistribute to on-chain signals.
- **§3.2 sentiment** — needs X API. `self.sentiment = None`; scores 0.
- **§1.6 unique traders** — not exposed by DexScreener.
- **§2.7 deployer history**, **§2.11 impersonation** — partially covered by
  RugCheck danger risks.
- **Base chain** — no EVM adapter; Base tokens return UNKNOWN and are rejected.
