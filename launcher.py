#!/usr/bin/env python3
"""
Desktop launcher. A double-clicked binary with no window is useless, so this
wraps the engine in a small tkinter GUI: credentials, controls, live log.

KEY HANDLING — read this before you package anything:

  Keys entered here live in memory for the session only. Nothing is written to
  disk. Close the app and you re-enter them.

  This is deliberate. A packaged binary is a single file that gets copied to
  other machines, synced to cloud drives, and attached to messages. Any key
  baked into it or saved beside it travels with it. If you want persistence,
  use OS env vars set outside the app, or a hardware signer — not a config
  file next to the executable.

  Never distribute a build containing your keys. If you share this binary,
  share it empty.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

APP = "Trading Bot"
PAD = {"padx": 8, "pady": 4}


class LogPump(threading.Thread):
    """Bridges the engine's logging into the GUI without blocking it."""

    def __init__(self, q: queue.Queue):
        super().__init__(daemon=True)
        self.q = q


class GuiHandler:
    """Minimal logging handler that pushes records onto a queue."""

    def __init__(self, q: queue.Queue):
        self.q = q
        self.level = 0

    def handle(self, record):
        try:
            self.q.put(f"{time.strftime('%H:%M:%S')}  {record.getMessage()}")
        except Exception:
            pass

    # logging.Handler duck-typing
    def createLock(self): self.lock = None
    def acquire(self): pass
    def release(self): pass
    def setLevel(self, lvl): self.level = lvl
    def setFormatter(self, f): pass
    def addFilter(self, f): pass
    def removeFilter(self, f): pass
    def filter(self, r): return True
    def flush(self): pass
    def close(self): pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.engine = None
        self.thread = None
        self.running = threading.Event()
        self.logq: queue.Queue = queue.Queue()

        root.title(APP)
        root.geometry("720x560")
        root.minsize(640, 480)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._tab_analyze(nb)
        self._tab_control(nb)
        self._tab_creds(nb)
        self._pump()

    # ── analyze ────────────────────────────────────────────────────────
    def _tab_analyze(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Analyze")

        bar = ttk.Frame(f)
        bar.pack(fill="x", **PAD)
        ttk.Label(bar, text="Token address").pack(side="left")
        self.addr = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.addr, width=46)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _ev: self.run_analysis())

        ttk.Label(bar, text="Capital $").pack(side="left", padx=(10, 0))
        self.an_capital = tk.StringVar(value="1000")
        ttk.Entry(bar, textvariable=self.an_capital, width=8).pack(side="left", padx=6)

        self.an_btn = ttk.Button(bar, text="Analyze", command=self.run_analysis)
        self.an_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Clear",
                   command=lambda: self._an_clear()).pack(side="left")

        self.an_status = tk.StringVar(value="Paste a Solana mint or EVM address")
        ttk.Label(f, textvariable=self.an_status,
                  foreground="#666").pack(anchor="w", **PAD)

        self.out = scrolledtext.ScrolledText(f, height=26, wrap="word",
                                             font=("Menlo", 11), spacing1=1)
        self.out.pack(fill="both", expand=True, **PAD)

        for tag, cfg_ in {
            "h1":    {"font": ("Menlo", 16, "bold")},
            "sect":  {"font": ("Menlo", 11, "bold"), "spacing1": 10},
            "good":  {"foreground": "#0a7d28"},
            "bad":   {"foreground": "#c02020"},
            "warn":  {"foreground": "#b06000"},
            "muted": {"foreground": "#777"},
            "big":   {"font": ("Menlo", 13, "bold")},
        }.items():
            self.out.tag_configure(tag, **cfg_)
        self.out.configure(state="disabled")

    def _an_clear(self):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")
        self.an_status.set("Paste a Solana mint or EVM address")

    def _w(self, text, *tags):
        self.out.configure(state="normal")
        self.out.insert("end", text, tags)
        self.out.configure(state="disabled")
        self.out.see("end")

    def run_analysis(self):
        addr = self.addr.get().strip()
        if not addr:
            messagebox.showinfo(APP, "Paste a token address first.")
            return
        try:
            cap = float(self.an_capital.get())
        except ValueError:
            messagebox.showerror(APP, "Capital must be a number.")
            return

        self._an_clear()
        self.an_btn.configure(state="disabled")
        self.an_status.set("Analyzing… (fetching market data and safety report)")

        def work():
            try:
                from analyze import analyze
                r = analyze(addr, "solana", cap)
                self.root.after(0, lambda: self._render(r, addr))
            except Exception as ex:
                self.root.after(0, lambda: self._an_error(ex))
        threading.Thread(target=work, daemon=True).start()

    def _an_error(self, ex):
        self.an_btn.configure(state="normal")
        self.an_status.set("Failed")
        self._w(f"Analysis failed: {ex}\n", "bad")

    def _render(self, r, addr):
        self.an_btn.configure(state="normal")
        if not r:
            self.an_status.set("Not found")
            self._w(f"No trading pair found for {addr}.\n", "bad")
            self._w("Check the address, and that the token has a live DEX pair.\n",
                    "muted")
            return

        self.an_status.set(f"{r.symbol} on {r.chain}")
        self._w(f"{r.symbol}", "h1")
        self._w(f"   {r.mint[:16]}…  {r.chain}  ·  {r.age_hours:.0f}h old\n", "muted")

        A = r.assess
        if A:
            def bar(score):
                filled = int(round(score))
                return "█" * filled + "·" * (10 - filled)
            tone_of = lambda s: ("good" if s >= 7 else "warn" if s >= 4 else "bad")

            self._w("\n")
            for label, val, score in (
                    ("SAFETY", A.safety, A.safety_score),
                    ("LIQUIDITY", A.liquidity_grade, A.liquidity_score),
                    ("MOMENTUM", A.momentum, A.momentum_score)):
                self._w(f"{label:<11} ")
                self._w(f"{val:<12} ", tone_of(score))
                self._w(f"{bar(score)} {score:.1f}/10\n", "muted")
            self._w(f"             {A.momentum_detail}\n", "muted")

            self._w(f"\nENTRY STATUS ", "sect")
            self._w(f"{A.entry_status:<12} ", tone_of(A.entry_score))
            self._w(f"{bar(A.entry_score)} {A.entry_score:.1f}/10\n")
            self._w(f"             {A.entry_reason}\n", "muted")

        # Market
        self._w("\nMARKET\n", "sect")
        self._w(f"  price        ${r.price:.8g}\n")
        for label, v in (("1h", r.chg_1h), ("6h", r.chg_6h), ("24h", r.chg_24h)):
            self._w(f"  {label:<12} ")
            self._w(f"{v:+.1f}%\n", "good" if v >= 0 else "bad")
        gl = r.gecko
        if gl and gl.total_liquidity > r.liquidity * 1.2:
            self._w(f"  liquidity    ${gl.total_liquidity:,.0f} "
                    f"across {gl.pool_count} pools\n")
        else:
            self._w(f"  liquidity    ${r.liquidity:,.0f}\n")
        if gl and gl.total_liquidity > 0 and gl.total_volume > 0:
            eff = gl.total_volume / gl.total_liquidity
            self._w(f"  volume 24h   ${gl.total_volume:,.0f}  ({eff:.1f}x liq)\n")
        else:
            self._w(f"  volume 24h   ${r.volume_24h:,.0f}  ({r.vol_liq:.1f}x liq)\n")
        self._w(f"  fdv          ${r.fdv:,.0f}\n")
        bs = r.buys / max(r.sells, 1)
        self._w(f"  buys/sells   {r.buys:,} / {r.sells:,}  ({bs:.2f})\n",
                "bad" if bs < 0.8 else "")
        if A:
            self._w(f"\n  {A.target_multiple:.0f}x target: FDV would need to reach "
                    f"${A.target_fdv:,.0f}\n", "muted")
        if bs < 0.8:
            self._w("  ⚠ net distribution — sellers outnumber buyers\n", "bad")
        if r.chg_1h < 0 and r.chg_6h < 0 and r.chg_24h > 20:
            self._w("  ⚠ up on 24h but falling on 1h and 6h — move unwinding\n", "bad")

        # Sizing
        S = r.sizing
        self._w("\nPOSITION SIZE\n", "sect")
        self._w(f"  ${S.recommended:,.0f}\n", "big")
        self._w(f"  limited by {S.binding_constraint}\n", "muted")
        self._w(f"    risk budget    ${S.max_by_risk:,.0f}\n", "muted")
        self._w(f"    liquidity cap  ${S.max_by_liquidity:,.0f}\n", "muted")
        self._w(f"    position cap   ${S.max_by_rule:,.0f}\n", "muted")
        if S.exit_impact_pct is not None:
            tag = "bad" if S.exit_impact_pct > 4 else "muted"
            self._w(f"  your exit moves price {S.exit_impact_pct:.2f}%\n", tag)

        # Levels
        L = r.levels
        self._w("\nLEVELS\n", "sect")
        self._w(f"  already +{L.move_pct:.0f}% off the base\n",
                "warn" if L.move_pct > 60 else "muted")
        for k, v in L.entries.items():
            self._w(f"  entry {k:<18} ${v:.8g}  ({(v/r.price-1)*100:+.1f}%)\n")
        self._w(f"  stop  {'':<18} ${L.stop:.8g}  ({L.stop_pct:.1f}%)\n", "bad")
        self._w(f"  {L.stop_basis}\n", "muted")
        self._w(f"  target 2R{'':<14} ${L.targets['2R']:.8g}\n", "good")
        self._w(f"  R:R                     {L.rr_at_moderate:.1f}:1\n")

        # Gates
        self._w(f"\nSAFETY GATES  ({len(r.gates_passed)} passed, "
                f"{len(r.gates_failed)} failed, "
                f"{len(r.gates_unknown)} unverified)\n", "sect")
        for g in r.gates_passed:
            self._w(f"  ✓ {g}\n", "good")
        for g in r.gates_failed:
            self._w(f"  ✕ {g}\n", "bad")
        for g in r.gates_unknown:
            self._w(f"  ? {g}\n", "muted")

        self._w(f"\nSCORE  {r.score}/100\n", "sect")
        avail = (r.score_parts or {}).get("_available_weight", 0)
        self._w(f"  based on {avail:.0f}% of the full signal set\n",
                "warn" if avail < 50 else "muted")
        for k, v in (r.score_parts or {}).items():
            if k.startswith("_"):
                continue
            if v == 0 and k in ("smart_money", "sentiment", "holder_growth"):
                self._w(f"  {k:<18}     —  unmeasured\n", "muted")
            else:
                self._w(f"  {k:<18}{v:>6.1f}\n")

        self._w("\nNOTES\n", "sect")
        for n in r.reasons:
            self._w(f"  • {n}\n", "muted")
        self._w("\nLevels are arithmetic on observed structure, not forecasts.\n",
                "muted")

    # ── controls ───────────────────────────────────────────────────────    # ── controls ───────────────────────────────────────────────────────
    def _tab_control(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Control")

        bar = ttk.Frame(f)
        bar.pack(fill="x", **PAD)

        self.mode = tk.StringVar(value="PAPER")
        ttk.Label(bar, text="Mode").pack(side="left")
        cb = ttk.Combobox(bar, textvariable=self.mode, width=8, state="readonly",
                          values=["PAPER", "LIVE"])
        cb.pack(side="left", padx=6)

        ttk.Label(bar, text="Capital $").pack(side="left", padx=(12, 0))
        self.capital = tk.StringVar(value="1000")
        ttk.Entry(bar, textvariable=self.capital, width=10).pack(side="left", padx=6)

        self.btn_start = ttk.Button(bar, text="Start", command=self.start)
        self.btn_start.pack(side="left", padx=(16, 4))
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(bar, text="Panic Sell", command=self.panic).pack(side="right")

        self.status = tk.StringVar(value="idle")
        ttk.Label(f, textvariable=self.status, foreground="#555").pack(
            anchor="w", **PAD)

        self.log = scrolledtext.ScrolledText(f, height=22, wrap="word",
                                             font=("Menlo", 10))
        self.log.pack(fill="both", expand=True, **PAD)
        self.log.configure(state="disabled")

    # ── credentials ────────────────────────────────────────────────────
    def _tab_creds(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="Credentials")

        ttk.Label(f, text="Session only — nothing is saved to disk.",
                  foreground="#a00").grid(row=0, column=0, columnspan=2,
                                          sticky="w", **PAD)

        self.fields = {}
        rows = [
            ("SOLANA_RPC", "Solana RPC URL", False),
            ("SOLANA_PRIVATE_KEY", "Solana key (base58)", True),
            ("EVM_PRIVATE_KEY", "EVM key (hex)", True),
            ("BASE_RPC", "Base RPC", False),
            ("ROBINHOOD_RPC", "Robinhood Chain RPC", False),
            ("RH_ROUTER", "RH Chain router address", False),
            ("RH_WETH", "RH Chain WETH address", False),
            ("JUPITER_API_KEY", "Jupiter API key (optional)", True),
            ("RUGCHECK_API_KEY", "RugCheck key (optional)", True),
        ]
        for i, (env, label, secret) in enumerate(rows, start=1):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", **PAD)
            var = tk.StringVar(value=os.getenv(env, ""))
            e = ttk.Entry(f, textvariable=var, width=52,
                          show="•" if secret else "")
            e.grid(row=i, column=1, sticky="we", **PAD)
            self.fields[env] = var
        f.columnconfigure(1, weight=1)

        ttk.Button(f, text="Apply to session",
                   command=self.apply_creds).grid(row=len(rows) + 1, column=1,
                                                  sticky="e", **PAD)

    def apply_creds(self):
        n = 0
        for env, var in self.fields.items():
            v = var.get().strip()
            if v:
                os.environ[env] = v
                n += 1
        self.write(f"applied {n} credential(s) to this session")
        messagebox.showinfo(APP, f"{n} value(s) set for this session only.")

    # ── engine lifecycle ───────────────────────────────────────────────
    def start(self):
        if self.running.is_set():
            return
        try:
            capital = float(self.capital.get())
        except ValueError:
            messagebox.showerror(APP, "Capital must be a number.")
            return

        if self.mode.get() == "LIVE":
            if not messagebox.askyesno(
                    APP, "LIVE mode trades real funds.\n\n"
                         "Have you completed 30 days of paper results?\n\n"
                         "Continue?"):
                return
            os.environ["I_UNDERSTAND_LIVE_TRADING"] = "yes"

        try:
            import logging
            from sniper_bot import Bot, Config

            root_logger = logging.getLogger()
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(GuiHandler(self.logq))

            cfg = Config(capital_usd=capital)
            cfg.mode = self.mode.get()
            self.engine = Bot(cfg)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP, f"Startup failed:\n{e}")
            return

        self.running.set()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set(f"running — {self.mode.get()}")
        self.write(f"engine started in {self.mode.get()} mode, ${capital:,.0f}")

    def _loop(self):
        last = 0.0
        while self.running.is_set():
            try:
                self.engine.manage_positions()
                if time.time() - last > self.engine.cfg.scan_interval_sec:
                    if self.engine.breakers.check(self.engine.equity()):
                        self.engine.run_once(["SOL", "USDC"])
                    last = time.time()
                    self.logq.put(
                        f"equity ${self.engine.equity():,.2f} | "
                        f"open {len(self.engine.positions)}")
                if self.engine.breakers.hard_stopped:
                    self.logq.put("HARD STOP — drawdown limit hit")
                    self.running.clear()
            except Exception as e:  # noqa: BLE001
                self.logq.put(f"loop error: {e}")
            for _ in range(self.engine.cfg.exit_check_interval_sec):
                if not self.running.is_set():
                    return
                time.sleep(1)

    def stop(self):
        self.running.clear()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status.set("stopped — positions left open")
        self.write("engine stopped. Open positions are NOT monitored now.")

    def panic(self):
        if not self.engine or not self.engine.positions:
            messagebox.showinfo(APP, "No open positions.")
            return
        n = len(self.engine.positions)
        if not messagebox.askyesno(APP, f"Market-sell all {n} position(s)?"):
            return

        def run():
            for mint, pos in list(self.engine.positions.items()):
                try:
                    live = self.engine.dex.pair(pos.chain, pos.pair_address)
                    price = live.price_usd if live else pos.entry_price
                    self.engine.broker.sell(pos, price, 1.0, "manual panic")
                    self.engine.positions.pop(mint, None)
                    self.logq.put(f"closed {pos.symbol}")
                except Exception as e:  # noqa: BLE001
                    self.logq.put(f"panic sell failed for {pos.symbol}: {e}")
        threading.Thread(target=run, daemon=True).start()

    # ── log plumbing ───────────────────────────────────────────────────
    def write(self, msg: str):
        self.logq.put(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _pump(self):
        try:
            while True:
                line = self.logq.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._pump)


def main() -> int:
    root = tk.Tk()
    app = App(root)

    def on_close():
        if app.running.is_set() and not messagebox.askyesno(
                APP, "Bot is running. Quitting stops exit monitoring "
                     "on open positions.\n\nQuit anyway?"):
            return
        app.running.clear()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
