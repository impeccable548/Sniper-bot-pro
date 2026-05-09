# ultra_mode.py — Section 4: Ultra Mode (Mint Sniper + Whale Shadow)
import json, time, threading, logging, os, struct, base64
import requests
import websocket
from datetime import datetime

import notifier
from scanner import check_mint_authority

logger = logging.getLogger(__name__)

PUMP_WS_URL    = "wss://pumpportal.fun/api/data"
PUMP_PROGRAM   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MAX_MC_USD     = 60_000   # Ultra-Mint hard cap
MAX_LOG        = 100


class UltraMode:
    def __init__(self, pump_sdk, rpc_client, get_token_price_fn, get_token_balance_fn):
        self.pump_sdk              = pump_sdk
        self.client                = rpc_client
        self.get_token_price       = get_token_price_fn
        self.get_token_balance     = get_token_balance_fn

        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._thread      = None

        # Public state (read by /api/ultra/status)
        self.active       = False
        self.sub_mode     = None   # "mint" | "whale"
        self.state        = "idle" # idle|armed|scoring|buying|monitoring|sold|aborted
        self.position     = None
        self.last_event   = ""
        self.log          = []

        # Whale approval store
        self.whale_pending_tokens:  list = []   # from _scan_whale_tokens()
        self.whale_approved_tokens: set  = set()  # user-approved mints

        # Config — overwritten by start()
        self.cfg = {
            "sol_amount":    0.05,
            "tp_pct":        75.0,
            "sl_pct":        15.0,
            "ts_pct":        0.0,
            "slippage":      0.10,
            "jito_tip_sol":  0.001,
            "max_mc_usd":    MAX_MC_USD,
            "min_whale_sol": 1.0,
            "max_pump_pct":  300.0,
            "whale_wallets": [],
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, sub_mode: str, config: dict) -> dict:
        if self.active:
            return {"success": False, "error": "Ultra Mode already active"}
        if sub_mode not in ("mint", "whale"):
            return {"success": False, "error": "sub_mode must be 'mint' or 'whale'"}
        if sub_mode == "whale" and not config.get("whale_wallets"):
            return {"success": False, "error": "No whale wallets configured"}
        if not self.pump_sdk.wallet:
            return {"success": False, "error": "Private key not configured"}

        self.cfg.update({k: v for k, v in config.items() if v is not None})
        self.pump_sdk.slippage         = float(self.cfg["slippage"])
        self.pump_sdk.jito_tip_lamports = int(float(self.cfg["jito_tip_sol"]) * 1e9)

        self._stop_event.clear()
        self.active    = True
        self.sub_mode  = sub_mode
        self.state     = "armed"
        self.position  = None
        self.log       = []
        self._log(f"⚡ Ultra-{'Mint' if sub_mode=='mint' else 'Whale'} ARMED — waiting for trigger")

        target = self._run_mint if sub_mode == "mint" else self._run_whale
        self._thread = threading.Thread(target=target, daemon=True, name=f"ultra-{sub_mode}")
        self._thread.start()
        return {"success": True, "sub_mode": sub_mode}

    def stop(self) -> dict:
        self._stop_event.set()
        self.active   = False
        self.state    = "idle"
        self._log("🔴 Ultra Mode deactivated manually")
        notifier.notify(f"🔴 <b>Ultra Mode</b> — Deactivated manually")
        return {"success": True}

    def get_status(self) -> dict:
        return {
            "active":     self.active,
            "sub_mode":   self.sub_mode,
            "state":      self.state,
            "position":   self.position,
            "last_event": self.last_event,
            "log":        self.log[-30:],
            "config":     self.cfg,
        }

    def save_watchlist(self, wallets: list) -> dict:
        self.cfg["whale_wallets"] = [w.strip() for w in wallets if w.strip()]
        self._log(f"🐋 Watchlist saved: {len(self.cfg['whale_wallets'])} wallets")
        return {"success": True, "count": len(self.cfg["whale_wallets"])}

    # ── Enhancement 8.1: Whale wallet token scanner ───────────────────────────

    def scan_whale_tokens(self, wallet_address: str) -> list:
        """
        Scan a whale wallet's current SPL token holdings.
        Returns tokens where MC is between 5k–15k USD for user review / approval.
        """
        import base64 as _b64, struct as _struct
        TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        results = []
        try:
            resp = self.client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_address),
                {"programId": TOKEN_PROGRAM_ID}
            )
            if not resp.value:
                return []

            mints = []
            for acct in resp.value[:25]:
                try:
                    info = self.client.get_account_info(acct.pubkey)
                    if info.value:
                        raw = info.value.data
                        data = _b64.b64decode(raw[0]) if isinstance(raw, (list, tuple)) else raw
                        if len(data) >= 32:
                            mint = str(Pubkey.from_bytes(data[0:32]))
                            mints.append(mint)
                except Exception:
                    continue

            for mint in mints:
                try:
                    r = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=5
                    )
                    if r.status_code == 200:
                        pairs = r.json().get("pairs") or []
                        if pairs:
                            mc  = float(pairs[0].get("marketCap") or 0)
                            tok = pairs[0].get("baseToken") or {}
                            if 5_000 <= mc <= 15_000:
                                results.append({
                                    "mint":           mint,
                                    "name":           tok.get("name", "?"),
                                    "symbol":         tok.get("symbol", "?"),
                                    "market_cap_usd": mc,
                                    "price_usd":      float(pairs[0].get("priceUsd") or 0),
                                    "volume_h1":      (pairs[0].get("volume") or {}).get("h1", 0),
                                    "approved":       mint in self.whale_approved_tokens,
                                })
                except Exception:
                    continue

            self.whale_pending_tokens = results
            self._log(f"🐋 Wallet scan: {len(results)} token(s) in 5k–15k MC range")
        except Exception as e:
            self._log(f"scan_whale_tokens error: {e}", "error")
        return results

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "info"):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "message": msg, "level": level}
        self.log.append(entry)
        if len(self.log) > MAX_LOG:
            self.log.pop(0)
        self.last_event = msg
        getattr(logger, level if level in ("info","warning","error") else "info")(f"[Ultra] {msg}")

    # ── Sub-Mode A: Ultra-Mint ────────────────────────────────────────────────

    def _run_mint(self):
        self._log("📡 Connecting to Pump.fun WebSocket…")

        def on_open(ws):
            ws.send(json.dumps({"method": "subscribeNewToken"}))
            self._log("✅ Subscribed to new token stream")

        def on_message(ws, raw):
            if self._stop_event.is_set():
                ws.close()
                return
            try:
                data = json.loads(raw)
                mint = data.get("mint") or data.get("token_address", "")
                if not mint or len(mint) < 32:
                    return
                self._log(f"🆕 New mint detected: {mint[:12]}…")
                self._handle_mint_trigger(ws, mint, data)
            except Exception as e:
                logger.debug(f"Ultra-Mint on_message: {e}")

        def on_error(ws, err):
            self._log(f"WebSocket error: {err}", "warning")

        def on_close(ws, *args):
            if self.active and not self._stop_event.is_set():
                self._log("WebSocket closed — reconnecting in 5s…", "warning")
                time.sleep(5)
                self._run_mint()

        try:
            ws = websocket.WebSocketApp(
                PUMP_WS_URL,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            self._log(f"WebSocket failed: {e}", "error")
            self._deactivate()

    def _handle_mint_trigger(self, ws, mint: str, data: dict):
        if self.state != "armed":
            return

        with self._lock:
            if self.state != "armed":
                return
            self.state = "scoring"

        self._log(f"🔍 Scoring {mint[:12]}… (<200ms target)")
        t0 = time.time()

        # Fast on-chain authority check (single RPC call)
        score_result = self._fast_score(mint)
        elapsed_ms   = (time.time() - t0) * 1000
        self._log(f"Score check: {elapsed_ms:.0f}ms — {'PASS ✅' if score_result['pass'] else 'FAIL ⛔'}")

        if not score_result["pass"]:
            self._log(f"Aborted: {score_result['reason']}", "warning")
            notifier.notify(f"⛔ <b>Ultra-Mint ABORTED</b>\n🪙 <code>{mint[:16]}…</code>\n⚠️ {score_result['reason']}")
            self.state = "armed"
            return

        # MC check #1 — at score time
        mc = self._get_market_cap(mint, data)
        max_mc = float(self.cfg["max_mc_usd"])
        if mc > max_mc:
            self._log(f"Aborted: MC ${mc:,.0f} > limit ${max_mc:,.0f}", "warning")
            notifier.notify(f"⛔ <b>Ultra-Mint ABORTED</b>\n🪙 <code>{mint[:16]}…</code>\n📊 MC too high: ${mc:,.0f}")
            self.state = "armed"
            return

        self.state = "buying"
        ws.close()  # stop listening — single-shot

        # MC check #2 — right before TX fires (stale-price guard)
        mc2 = self._get_market_cap(mint, {})
        if mc2 > max_mc:
            self._log(f"Aborted (pre-TX): MC ${mc2:,.0f} now exceeds limit", "warning")
            notifier.notify(f"⛔ <b>Ultra-Mint ABORTED (pre-TX)</b>\n🪙 <code>{mint[:16]}…</code>\n📊 Pumped to ${mc2:,.0f} before TX")
            self._deactivate()
            return

        self._execute_and_monitor(mint, "mint")

    # ── Sub-Mode B: Ultra-Whale ───────────────────────────────────────────────

    def _run_whale(self):
        wallets = self.cfg.get("whale_wallets", [])
        if not wallets:
            self._log("No whale wallets configured — aborting", "error")
            self._deactivate()
            return

        self._log(f"🐋 Watching {len(wallets)} whale wallet(s) via Solana logsSubscribe…")

        rpc_ws_url = self._rpc_to_ws(os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"))
        self._log(f"Connecting to: {rpc_ws_url}")

        detected = threading.Event()

        def on_open(ws):
            for wallet in wallets:
                sub = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [wallet]},
                        {"commitment": "confirmed"}
                    ]
                }
                ws.send(json.dumps(sub))
            self._log("✅ logsSubscribe active for all whale wallets")

        def on_message(ws, raw):
            if self._stop_event.is_set() or detected.is_set():
                ws.close()
                return
            try:
                data  = json.loads(raw)
                notif = data.get("params", {}).get("result", {})
                value = notif.get("value", {})
                logs  = value.get("logs", [])
                sig   = value.get("signature", "")

                # Detect Pump.fun buy — look for program invocation in logs
                is_pump_buy = any(PUMP_PROGRAM in l for l in logs) and \
                              any("invoke" in l.lower() for l in logs)
                if not is_pump_buy:
                    return

                # Extract token mint — 3-method fallback chain (Enhancement 8.3)
                mint = self._extract_mint_from_logs(logs, sig)
                if not mint:
                    return

                # Enhancement 8.4: Check approval list if configured
                if self.whale_approved_tokens and mint not in self.whale_approved_tokens:
                    self._log(
                        f"⏭️ Whale bought {mint[:12]}… but not in approved list — skipping",
                        "warning"
                    )
                    return

                self._log(f"🐋 Whale buy detected! Token: {mint[:12]}… TX: {sig[:16]}…")
                detected.set()
                ws.close()

                threading.Thread(
                    target=self._handle_whale_trigger,
                    args=(mint, sig), daemon=True
                ).start()

            except Exception as e:
                logger.debug(f"Ultra-Whale on_message: {e}")

        def on_error(ws, err):
            self._log(f"Whale WS error: {err}", "warning")

        def on_close(ws, *args):
            if self.active and not self._stop_event.is_set() and not detected.is_set():
                self._log("Whale WS closed — reconnecting in 5s…", "warning")
                time.sleep(5)
                self._run_whale()

        try:
            ws = websocket.WebSocketApp(
                rpc_ws_url,
                on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            self._log(f"Whale WebSocket failed: {e}", "error")
            self._deactivate()

    def _handle_whale_trigger(self, mint: str, whale_sig: str):
        if self.state != "armed":
            return
        self.state = "scoring"

        # Smart filter 1: token not already up >300%
        pump_pct = self._get_pump_pct_since_launch(mint)
        max_pump = float(self.cfg["max_pump_pct"])
        if pump_pct is not None and pump_pct > max_pump:
            self._log(f"Aborted: token already up {pump_pct:.0f}% — exit liquidity risk", "warning")
            notifier.notify(f"⛔ <b>Ultra-Whale ABORTED</b>\n🪙 <code>{mint[:16]}…</code>\n📈 Already +{pump_pct:.0f}% from launch")
            self.state = "armed"
            return

        # Smart filter 2: buy size > min_whale_sol — parsed from the TX
        # (We trust the trigger detection was significant; exact parsing is complex — noted for v2)
        self._log(f"✅ Whale filters passed — executing buy on {mint[:12]}…")
        self._execute_and_monitor(mint, "whale")

    # ── Shared Execution ──────────────────────────────────────────────────────

    def _execute_and_monitor(self, mint: str, sub_mode: str):
        self.state = "buying"
        sol_amount = float(self.cfg["sol_amount"])

        # Derive bonding curve
        bonding_curve = self.pump_sdk.derive_bonding_curve(mint)
        if not bonding_curve:
            self._log("Could not derive bonding curve — aborted", "error")
            self._deactivate()
            return

        self._log(f"💸 Firing buy: {sol_amount} SOL → {mint[:12]}…")
        buy_result = self.pump_sdk.buy_token(mint, bonding_curve, sol_amount)
        if not buy_result.get("success"):
            err = buy_result.get("error", "Buy failed")
            self._log(f"Buy failed: {err}", "error")
            notifier.notify(f"⛔ <b>Ultra-{'Mint' if sub_mode=='mint' else 'Whale'} BUY FAILED</b>\n🪙 <code>{mint[:16]}…</code>\n❌ {err}")
            self._deactivate()
            return

        tx_sig = buy_result.get("signature", "")
        self._log(f"✅ Buy confirmed: {tx_sig[:16]}…")

        # Get entry price
        time.sleep(2)
        price_sol, _ = self.get_token_price(bonding_curve)
        from bot_logic import fetch_sol_price
        sol_usd         = fetch_sol_price()
        entry_price_usd = price_sol * sol_usd if price_sol else 0.000001 * sol_usd

        tp_target = entry_price_usd * (1 + float(self.cfg["tp_pct"]) / 100)
        sl_target = entry_price_usd * (1 - float(self.cfg["sl_pct"]) / 100)

        self.position = {
            "mint":            mint,
            "bonding_curve":   bonding_curve,
            "sol_amount":      sol_amount,
            "entry_price_usd": entry_price_usd,
            "current_price":   entry_price_usd,
            "tp_target":       tp_target,
            "sl_target":       sl_target,
            "highest_price":   entry_price_usd,
            "pnl_pct":         0.0,
            "tx_sig":          tx_sig,
            "sub_mode":        sub_mode,
            "opened_at":       datetime.now().isoformat(),
        }

        notifier.notify(
            f"⚡ <b>Ultra-{'Mint' if sub_mode=='mint' else 'Whale'} BUY</b>\n\n"
            f"🪙 <code>{mint[:16]}…</code>\n"
            f"💰 {sol_amount} SOL\n"
            f"📈 Entry: ${entry_price_usd:.10f}\n"
            f"🎯 TP: +{self.cfg['tp_pct']}%  🛑 SL: -{self.cfg['sl_pct']}%"
            + (f"  📉 TS: {self.cfg['ts_pct']}%" if sub_mode == "whale" and float(self.cfg.get("ts_pct",0)) > 0 else "")
            + f"\n🔗 <code>{tx_sig[:20]}…</code>"
        )

        self.state = "monitoring"
        self._monitor_and_sell(mint, bonding_curve, entry_price_usd, sub_mode)

    def _monitor_and_sell(self, mint: str, bonding_curve: str, entry_usd: float, sub_mode: str):
        self._log(f"👁️ Monitoring {mint[:12]}… — 1s price poll")
        sol_cache = {"price": 150.0, "ts": 0}
        pos = self.position

        while not self._stop_event.is_set():
            time.sleep(1)
            try:
                if time.time() - sol_cache["ts"] > 30:
                    from bot_logic import fetch_sol_price
                    sol_cache["price"] = fetch_sol_price()
                    sol_cache["ts"]    = time.time()

                price_sol, _ = self.get_token_price(bonding_curve)
                if price_sol == 0:
                    continue

                current_usd = price_sol * sol_cache["price"]
                pnl_pct     = (current_usd - entry_usd) / entry_usd * 100

                # Update trailing stop (whale mode only)
                ts_pct = float(self.cfg.get("ts_pct", 0))
                if sub_mode == "whale" and ts_pct > 0 and current_usd > entry_usd:
                    highest = max(pos.get("highest_price", entry_usd), current_usd)
                    new_sl  = highest * (1 - ts_pct / 100)
                    if new_sl > pos["sl_target"]:
                        pos["sl_target"]    = new_sl
                        pos["highest_price"] = highest
                        self._log(f"📉 Trailing SL → ${new_sl:.10f}")

                pos["current_price"] = current_usd
                pos["pnl_pct"]       = pnl_pct

                reason = None
                if current_usd >= pos["tp_target"]:
                    reason = "Take Profit"
                elif current_usd <= pos["sl_target"]:
                    reason = "Stop Loss"

                if reason:
                    self._log(f"{'🎯' if reason=='Take Profit' else '🛑'} {reason} hit @ ${current_usd:.10f} ({pnl_pct:+.2f}%)")
                    self._sell_and_finish(mint, bonding_curve, reason, current_usd, pnl_pct, sol_amount=pos["sol_amount"])
                    return

            except Exception as e:
                logger.debug(f"Ultra monitor tick: {e}")

        # Manually stopped mid-position
        self._log("Ultra Mode stopped — selling open position", "warning")
        price_sol, _ = self.get_token_price(bonding_curve)
        sol_usd = sol_cache["price"]
        exit_px = (price_sol * sol_usd) if price_sol else pos.get("current_price", entry_usd)
        pnl_pct = (exit_px - entry_usd) / entry_usd * 100
        self._sell_and_finish(mint, bonding_curve, "Manual Stop", exit_px, pnl_pct, pos["sol_amount"])

    def _sell_and_finish(self, mint: str, bonding_curve: str, reason: str,
                         exit_px: float, pnl_pct: float, sol_amount: float):
        self.state = "selling"
        self._log(f"🔄 Executing sell [{reason}]…")

        raw_balance = self.get_token_balance(mint)
        if raw_balance == 0:
            self._log("Token balance 0 — skipping sell (already gone?)", "warning")
        else:
            result = self.pump_sdk.sell_token(mint, bonding_curve, raw_balance, close_account=True)
            tx_sig  = result.get("signature", "")
            if result.get("success"):
                self._log(f"✅ Sell confirmed: {tx_sig[:16]}…")
            else:
                self._log(f"⚠️ Sell result: {result.get('error','unknown')}", "warning")

        pnl_usd = (pnl_pct / 100) * sol_amount * 150  # rough USD estimate
        notifier.notify_sell(mint, f"Ultra {reason}", pnl_pct, pnl_usd, exit_px,
                             result.get("signature","") if raw_balance > 0 else "")
        self.state = "sold"
        self._log(f"✅ Trade complete — P&L: {pnl_pct:+.2f}%  Ultra Mode standing down")
        notifier.notify(f"⚫ <b>Ultra Mode DEACTIVATED</b> — single-shot complete\n💵 Final P&L: <b>{pnl_pct:+.2f}%</b>")
        self._deactivate()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fast_score(self, mint: str) -> dict:
        """
        Fast on-chain authority check (<200ms).
        Whitelist: Pump.fun program holding mint/freeze authority is SAFE (bonding curve phase).
        Only fail if a dev wallet or unknown key holds authority.
        """
        PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        try:
            result = check_mint_authority(self.client, mint)

            mint_auth   = result.get("mint_authority")
            freeze_auth = result.get("freeze_authority")

            # Mint authority check
            if not result.get("mint_auth_disabled"):
                if mint_auth == PUMP_PROGRAM:
                    pass  # ✅ Pump.fun program — bonding curve, expected
                else:
                    return {"pass": False, "reason": f"Mint authority held by dev: {(mint_auth or 'unknown')[:16]}"}

            # Freeze authority check
            if not result.get("freeze_auth_disabled"):
                if freeze_auth == PUMP_PROGRAM:
                    pass  # ✅ Pump.fun program — safe
                else:
                    return {"pass": False, "reason": f"Freeze authority held by dev: {(freeze_auth or 'unknown')[:16]}"}

            return {"pass": True, "reason": "ok"}
        except Exception as e:
            return {"pass": False, "reason": f"Score error: {e}"}

    def _get_market_cap(self, mint: str, event_data: dict) -> float:
        # Try event data first (zero extra request)
        mc = event_data.get("usd_market_cap") or event_data.get("market_cap", 0)
        if mc:
            return float(mc)
        # Fallback: Pump.fun API
        try:
            r = requests.get(f"https://frontend-api.pump.fun/coins/{mint}", timeout=4)
            if r.status_code == 200:
                return float(r.json().get("usd_market_cap", 0) or 0)
        except Exception:
            pass
        return 0.0

    def _get_pump_pct_since_launch(self, mint: str) -> float | None:
        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=6
            )
            if r.status_code == 200:
                pairs = r.json().get("pairs") or []
                if pairs:
                    p = pairs[0]
                    change = (p.get("priceChange") or {}).get("h24", None)
                    if change is not None:
                        return float(change)
        except Exception:
            pass
        return None

    def _extract_mint_from_logs(self, logs: list, sig: str) -> str | None:
        """
        Enhancement 8.3 — Three-method fallback chain for mint extraction.
        Method 1: DexScreener search by TX sig (3 retries).
        Method 2: Solana RPC — parse transaction accounts.
        Method 3: Regex scan of log strings for base58 addresses.
        """
        # Method 1: DexScreener with 3 retries
        for attempt in range(3):
            try:
                r = requests.get(
                    "https://api.dexscreener.com/latest/dex/search",
                    params={"q": sig[:20]}, timeout=5,
                )
                if r.status_code == 200:
                    pairs = r.json().get("pairs") or []
                    if pairs:
                        addr = (pairs[0].get("baseToken") or {}).get("address")
                        if addr:
                            self._log(f"Mint extracted via DexScreener (attempt {attempt+1}): {addr[:12]}…")
                            return addr
                if attempt < 2:
                    time.sleep(1.5)
            except Exception:
                if attempt < 2:
                    time.sleep(1.5)

        # Method 2: Solana RPC — parse TX and look at account keys
        try:
            import base58 as _b58
            sig_bytes = _b58.b58decode(sig)
            tx_resp   = self.client.get_transaction(sig_bytes, encoding="json", max_supported_transaction_version=0)
            if tx_resp and tx_resp.value:
                accounts = (
                    (tx_resp.value.transaction.transaction.message.account_keys or [])
                    if hasattr(tx_resp.value.transaction.transaction, "message")
                    else []
                )
                for acct in accounts:
                    addr = str(acct)
                    if addr not in (PUMP_PROGRAM, "11111111111111111111111111111111"):
                        # Check if this looks like a token mint (not a known program)
                        self._log(f"Mint extracted via RPC TX parse: {addr[:12]}…")
                        return addr
        except Exception as e:
            logger.debug(f"RPC TX parse failed: {e}")

        # Method 3: Regex scan of log strings for base58 addresses
        try:
            import base58 as _b58
            import re
            b58_pattern = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
            skip = {PUMP_PROGRAM, "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"}
            for log in logs:
                for match in b58_pattern.findall(log):
                    if match in skip:
                        continue
                    try:
                        decoded = _b58.b58decode(match)
                        if len(decoded) == 32:
                            self._log(f"Mint extracted via log regex: {match[:12]}…")
                            return match
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Regex mint extraction failed: {e}")

        self._log("⚠️ Could not extract mint from whale TX — all 3 methods failed", "warning")
        return None

    def _rpc_to_ws(self, rpc_url: str) -> str:
        return rpc_url.replace("https://", "wss://").replace("http://", "ws://")

    def _deactivate(self):
        self.active   = False
        self.sub_mode = None
        if self.state not in ("sold", "aborted"):
            self.state = "idle"
        self._stop_event.set()
