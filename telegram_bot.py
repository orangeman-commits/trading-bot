#!/usr/bin/env python3
"""
Telegram control surface for the trading bot.

The Telegram layer is a REMOTE CONTROL, not a second trading engine. All rules
live in sniper_bot.py; this just exposes status, alerts, and manual overrides.

╔══════════════════════════════════════════════════════════════════════════╗
║  SECURITY — READ THIS                                                    ║
║                                                                          ║
║  Telegram bot usernames are publicly discoverable. Anyone can find your  ║
║  bot and send it /buy. TELEGRAM_ALLOWED_IDS is the ONLY thing standing   ║
║  between a stranger and your wallet. If it is unset, this bot refuses    ║
║  to start. Do not "temporarily" disable it.                              ║
║                                                                          ║
║  Also: your bot token in a group chat, a leaked screenshot, or a shared  ║
║  server all expose the control surface. Run it on a machine you control, ║
║  in a private 1:1 chat, with a wallet holding only the trading float.    ║
╚══════════════════════════════════════════════════════════════════════════╝

Setup:
    pip install python-telegram-bot>=21 pynacl
    # 1. Talk to @BotFather -> /newbot -> copy token
    # 2. Talk to @userinfobot -> copy your numeric user id
    export TELEGRAM_BOT_TOKEN='123456:ABC...'
    export TELEGRAM_ALLOWED_IDS='11122233'     # comma-separated, no spaces
    python telegram_bot.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from sniper_bot import Bot, Config

logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s",
                    level=logging.INFO, datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("tg")

ALLOWED: set[int] = set()
PENDING: dict[str, dict] = {}          # confirmation token -> action
CONFIRM_TTL = 120                      # seconds before a confirmation expires


# ──────────────────────────────── auth ─────────────────────────────────────

def restricted(fn):
    """Every handler must be wrapped. Unauthorised attempts are logged with
    the sender id so you can see if your bot has been discovered."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED:
            LOG.warning("DENIED uid=%s username=%s text=%r",
                        getattr(user, "id", "?"), getattr(user, "username", "?"),
                        (update.effective_message.text or "")[:80]
                        if update.effective_message else "")
            if update.effective_message:
                await update.effective_message.reply_text("Not authorised.")
            return
        return await fn(update, context)
    return wrapper


def esc(s) -> str:
    return html.escape(str(s))


# ─────────────────────────────── commands ──────────────────────────────────

@restricted
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    await update.message.reply_text(
        f"<b>Trading bot online</b>\n"
        f"mode: <code>{esc(bot.cfg.mode)}</code>\n\n"
        "/status — equity, mode, breakers\n"
        "/positions — open positions and PnL\n"
        "/scan — run one scan cycle now\n"
        "/why &lt;mint&gt; — why a token was rejected\n"
        "/analyze &lt;mint&gt; — levels, sizing, verdict\n"
        "/buy &lt;asset&gt; &lt;usd&gt; [venue] — manual entry\n"
        "/sell &lt;mint&gt; &lt;pct&gt; — manual exit\n"
        "/panic — market-sell everything\n"
        "/halt — block new entries\n"
        "/resume — clear halt\n"
        "/balances — per-venue balances",
        parse_mode=ParseMode.HTML)


@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    b = bot.breakers
    halted = time.time() < b.halted_until
    eq = bot.equity()
    pnl = (eq / bot.cfg.capital_usd - 1) * 100
    await update.message.reply_text(
        f"<b>Status</b>\n"
        f"mode      <code>{esc(bot.cfg.mode)}</code>\n"
        f"equity    <code>${eq:,.2f}</code> ({pnl:+.2f}%)\n"
        f"deployed  <code>${bot.deployed_usd():,.2f}</code>\n"
        f"open      <code>{len(bot.positions)}/{bot.cfg.max_concurrent_positions}</code>\n"
        f"entries   <code>{'HALTED' if halted or b.hard_stopped else 'ACTIVE'}</code>\n"
        f"losses    <code>{b.consecutive_losses} consecutive</code>\n"
        f"hardstop  <code>{b.hard_stopped}</code>",
        parse_mode=ParseMode.HTML)


@restricted
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    if not bot.positions:
        await update.message.reply_text("No open positions.")
        return
    lines = ["<b>Open positions</b>"]
    for p in bot.positions.values():
        live = await asyncio.to_thread(bot.dex.pair, p.chain, p.pair_address)
        price = live.price_usd if live else p.entry_price
        gain = p.gain_pct(price)
        dd = (1 - price / p.high_water_price) * 100 if p.high_water_price else 0
        lines.append(
            f"\n<b>{esc(p.symbol)}</b>  {gain:+.1f}%\n"
            f"  held {p.hours_held():.1f}h · from high −{dd:.0f}%\n"
            f"  tp1 {'✓' if p.tp1_done else '·'} tp2 {'✓' if p.tp2_done else '·'}\n"
            f"  <code>{esc(p.mint[:20])}…</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    await update.message.reply_text("Scanning…")
    try:
        await asyncio.to_thread(bot.run_once, ["SOL", "USDC"])
        await update.message.reply_text(
            f"Scan complete. Open: {len(bot.positions)} · "
            f"Equity: ${bot.equity():,.2f}")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Scan failed: {esc(e)}")


@restricted
async def cmd_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Shows the most recent rejection reason — the command you'll use most."""
    bot: Bot = ctx.application.bot_data["engine"]
    if not ctx.args:
        await update.message.reply_text("Usage: /why &lt;mint&gt;",
                                        parse_mode=ParseMode.HTML)
        return
    mint = ctx.args[0]
    rows = bot.journal.db.execute(
        "SELECT ts, action, detail FROM decisions WHERE mint LIKE ? "
        "ORDER BY ts DESC LIMIT 3", (f"{mint}%",)).fetchall()
    if not rows:
        await update.message.reply_text("No decisions logged for that mint.")
        return
    out = []
    for ts, action, detail in rows:
        age = (time.time() - ts) / 60
        out.append(f"<b>{esc(action)}</b> ({age:.0f}m ago)\n"
                   f"<code>{esc(detail[:600])}</code>")
    await update.message.reply_text("\n\n".join(out), parse_mode=ParseMode.HTML)


@restricted
async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/analyze <mint> — full report: levels, sizing, gates, verdict."""
    bot: Bot = ctx.application.bot_data["engine"]
    if not ctx.args:
        await update.message.reply_text("Usage: /analyze &lt;mint&gt;",
                                        parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text("Analyzing…")
    try:
        from analyze import analyze, render
        r = await asyncio.to_thread(analyze, ctx.args[0], "solana",
                                    bot.cfg.capital_usd)
        if not r:
            await update.message.reply_text("No pair found for that address.")
            return
        text = render(r)
        for i in range(0, len(text), 3500):
            await update.message.reply_text(
                f"<pre>{esc(text[i:i+3500])}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:  # noqa: BLE001
        LOG.exception("analyze failed")
        await update.message.reply_text(f"Analysis failed: {esc(e)}")


@restricted
async def cmd_balances(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    router = ctx.application.bot_data.get("router")
    if not router:
        bot: Bot = ctx.application.bot_data["engine"]
        await update.message.reply_text(
            f"PAPER mode — simulated cash ${bot.broker.cash:,.2f}")
        return
    bals = await asyncio.to_thread(router.all_balances)
    lines = ["<b>Balances</b>"]
    for venue, items in bals.items():
        lines.append(f"\n<b>{esc(venue)}</b>")
        if not items:
            lines.append("  (none)")
        for k, v in items.items():
            lines.append(f"  {esc(k)}: <code>{esc(v)}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ───────────────────── mutating commands (need confirm) ────────────────────

def _stage(action: dict) -> str:
    token = f"c{int(time.time()*1000)%10_000_000}"
    PENDING[token] = {**action, "ts": time.time()}
    return token


def _confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data=f"ok:{token}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"no:{token}"),
    ]])


@restricted
async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: /buy &lt;mint|SYMBOL&gt; &lt;usd&gt; [solana|base|robinhood]",
            parse_mode=ParseMode.HTML)
        return
    asset, amount = ctx.args[0], ctx.args[1]
    venue = ctx.args[2] if len(ctx.args) > 2 else None
    try:
        usd = float(amount)
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    cap = bot.cfg.capital_usd * bot.cfg.max_position_pct / 100
    if usd > cap:
        await update.message.reply_text(
            f"Refused: ${usd:,.0f} exceeds the §4.2 per-position cap of "
            f"${cap:,.0f}. Manual orders do not bypass sizing rules.")
        return

    token = _stage({"kind": "buy", "asset": asset, "usd": usd, "venue": venue})
    await update.message.reply_text(
        f"<b>Confirm buy</b>\n"
        f"asset <code>{esc(asset)}</code>\n"
        f"size  <code>${usd:,.2f}</code>\n"
        f"venue <code>{esc(venue or 'auto')}</code>\n"
        f"mode  <code>{esc(bot.cfg.mode)}</code>\n\n"
        f"<i>Manual entry skips the safety screen. Expires in {CONFIRM_TTL}s.</i>",
        parse_mode=ParseMode.HTML, reply_markup=_confirm_kb(token))


@restricted
async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /sell &lt;mint&gt; &lt;pct&gt;",
                                        parse_mode=ParseMode.HTML)
        return
    mint, pct = ctx.args[0], float(ctx.args[1])
    token = _stage({"kind": "sell", "asset": mint, "pct": pct})
    await update.message.reply_text(
        f"<b>Confirm sell</b>\n<code>{esc(mint[:24])}…</code> — {pct:.0f}%",
        parse_mode=ParseMode.HTML, reply_markup=_confirm_kb(token))


@restricted
async def cmd_panic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    token = _stage({"kind": "panic"})
    await update.message.reply_text(
        f"<b>PANIC</b> — market-sell all {len(bot.positions)} positions and halt.\n"
        f"<i>Accepts any price. Confirm?</i>",
        parse_mode=ParseMode.HTML, reply_markup=_confirm_kb(token))


@restricted
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    verb, _, token = q.data.partition(":")
    action = PENDING.pop(token, None)

    if not action:
        await q.edit_message_text("Expired or already handled.")
        return
    if verb == "no":
        await q.edit_message_text("Cancelled.")
        return
    if time.time() - action["ts"] > CONFIRM_TTL:
        await q.edit_message_text("Expired — re-issue the command.")
        return

    bot: Bot = ctx.application.bot_data["engine"]
    router = ctx.application.bot_data.get("router")
    await q.edit_message_text("Executing…")

    try:
        if action["kind"] == "panic":
            n = len(bot.positions)
            for mint, pos in list(bot.positions.items()):
                live = await asyncio.to_thread(bot.dex.pair, pos.chain, pos.pair_address)
                price = live.price_usd if live else pos.entry_price
                await asyncio.to_thread(bot.broker.sell, pos, price, 1.0, "manual panic")
                bot.positions.pop(mint, None)
            bot.breakers._halt(24, "manual panic")
            await q.edit_message_text(f"Closed {n} positions. Entries halted 24h.")
            return

        if action["kind"] == "buy":
            if router:
                venue = await asyncio.to_thread(
                    router.resolve, action["asset"], action["venue"])
                res = await asyncio.to_thread(venue.buy, action["asset"], action["usd"])
                msg = (f"✅ Filled on {venue.name}\n<code>{esc(res.ref[:24])}</code>"
                       if res.ok else f"❌ {esc(res.error)}")
            else:
                msg = "PAPER mode — no live venue configured."
            await q.edit_message_text(msg, parse_mode=ParseMode.HTML)
            return

        if action["kind"] == "sell":
            pos = bot.positions.get(action["asset"])
            if not pos:
                await q.edit_message_text("No such open position.")
                return
            live = await asyncio.to_thread(bot.dex.pair, pos.chain, pos.pair_address)
            price = live.price_usd if live else pos.entry_price
            proceeds = await asyncio.to_thread(
                bot.broker.sell, pos, price, action["pct"] / 100, "manual")
            if action["pct"] >= 100:
                bot.positions.pop(action["asset"], None)
            await q.edit_message_text(f"Sold — proceeds ${proceeds:,.2f}")
    except Exception as e:  # noqa: BLE001
        LOG.exception("action failed")
        await q.edit_message_text(f"Failed: {esc(e)}")


@restricted
async def cmd_halt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    bot.breakers._halt(24, "manual /halt")
    await update.message.reply_text("Entries halted. Exit monitoring continues.")


@restricted
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot: Bot = ctx.application.bot_data["engine"]
    if bot.breakers.hard_stopped:
        await update.message.reply_text(
            "Hard-stopped by §7.4 total drawdown. Restart the process "
            "deliberately — this one does not clear from chat.")
        return
    bot.breakers.halted_until = 0.0
    await update.message.reply_text("Entries resumed.")


# ───────────────────── background engine + alerting ────────────────────────

async def engine_loop(app: Application):
    """Runs the trading loop and pushes alerts. Exits are never gated on
    Telegram being reachable — if the network drops, trading continues."""
    bot: Bot = app.bot_data["engine"]
    chat_id = app.bot_data["alert_chat"]
    seen: set[str] = set()
    last_scan = 0.0

    async def alert(text: str):
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            LOG.warning("alert failed (trading unaffected): %s", e)

    while True:
        try:
            before = dict(bot.positions)
            await asyncio.to_thread(bot.manage_positions)

            for mint in set(before) - set(bot.positions):
                p = before[mint]
                pnl = p.realised_usd - p.cost_usd
                await alert(f"{'🟢' if pnl >= 0 else '🔴'} <b>Closed {esc(p.symbol)}</b>  "
                            f"${pnl:+,.2f} after {p.hours_held():.1f}h")

            if time.time() - last_scan > bot.cfg.scan_interval_sec:
                if bot.breakers.check(bot.equity()):
                    await asyncio.to_thread(bot.run_once, ["SOL", "USDC"])
                for mint, p in bot.positions.items():
                    if mint not in seen:
                        seen.add(mint)
                        await alert(f"🔵 <b>Entered {esc(p.symbol)}</b>  "
                                    f"${p.cost_usd:,.0f} @ {p.entry_price:.8f}")
                last_scan = time.time()

            if bot.breakers.hard_stopped:
                await alert("⛔ <b>HARD STOP</b> — §7.4 drawdown limit. "
                            "Trading stopped; manual restart required.")
                return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOG.exception("engine loop error")
        await asyncio.sleep(bot.cfg.exit_check_interval_sec)


async def _post_init(app: Application):
    app.bot_data["task"] = asyncio.create_task(engine_loop(app))
    await app.bot.send_message(
        app.bot_data["alert_chat"],
        f"🚀 Bot started — mode <code>{app.bot_data['engine'].cfg.mode}</code>",
        parse_mode=ParseMode.HTML)


def main() -> int:
    global ALLOWED

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    raw_ids = os.getenv("TELEGRAM_ALLOWED_IDS", "").strip()
    if not token:
        LOG.critical("TELEGRAM_BOT_TOKEN not set")
        return 1
    if not raw_ids:
        LOG.critical(
            "TELEGRAM_ALLOWED_IDS not set. Your bot username is publicly "
            "discoverable — without an allowlist, any stranger can command it. "
            "Refusing to start.")
        return 1
    try:
        ALLOWED = {int(x) for x in raw_ids.split(",") if x.strip()}
    except ValueError:
        LOG.critical("TELEGRAM_ALLOWED_IDS must be comma-separated integers")
        return 1

    cfg = Config()
    live = os.getenv("I_UNDERSTAND_LIVE_TRADING") == "yes" \
        and os.getenv("TRADING_MODE") == "LIVE"
    if live:
        cfg.mode = "LIVE"

    engine = Bot(cfg)
    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["engine"] = engine
    app.bot_data["alert_chat"] = sorted(ALLOWED)[0]

    if live:
        from venues import (EvmVenueAdapter, RobinhoodVenue, SolanaVenue,
                            VenueRouter)
        vs = []
        for factory, label in (
                (lambda: SolanaVenue(cfg.solana_rpc, cfg.max_slippage_pct), "solana"),
                (lambda: EvmVenueAdapter("base", cfg.max_slippage_pct), "base"),
                (lambda: EvmVenueAdapter("robinhood", cfg.max_slippage_pct),
                 "robinhood-chain"),
                (RobinhoodVenue, "robinhood-brokerage")):
            try:
                vs.append(factory())
                LOG.info("venue ready: %s", label)
            except Exception as e:  # noqa: BLE001
                LOG.warning("venue %s unavailable: %s", label, e)
        if vs:
            app.bot_data["router"] = VenueRouter(vs)

    for cmd, fn in (("start", cmd_start), ("help", cmd_start),
                    ("status", cmd_status), ("positions", cmd_positions),
                    ("scan", cmd_scan), ("why", cmd_why),
                    ("analyze", cmd_analyze),
                    ("balances", cmd_balances), ("buy", cmd_buy),
                    ("sell", cmd_sell), ("panic", cmd_panic),
                    ("halt", cmd_halt), ("resume", cmd_resume)):
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(on_callback))

    LOG.info("mode=%s allowlist=%s", cfg.mode, sorted(ALLOWED))
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
