# Sniper Pro — Solana Pump.fun Sniper Bot

## Stack
- **Backend**: Python / Flask (app.py)
- **Frontend**: Dark terminal UI (templates/index.html — single-page, no build step)
- **Blockchain**: Solana via `solana-py` + `solders`
- **Run**: `python app.py` — workflow "Start application" on port 5000

## Architecture

### Core Files
| File | Purpose |
|------|---------|
| `app.py` | Flask API + bot instantiation + all endpoints |
| `bot_logic.py` | Trading engine — 3 modes, monitor loop, scanner loop, safety, P&L |
| `pump_sdk.py` | Pump.fun buy + sell SDK (Jito tip, CloseAccount, 200k compute limit) |
| `jito.py` | Jito tip instruction builder + bundle submission |
| `scanner.py` | Token scanner (Pump.fun new / DexScreener trending) + on-chain safety |
| `notifier.py` | Telegram notifications (buy, sell, safety block, test) |
| `templates/index.html` | Full trading dashboard UI |

### Three Operating Modes
- **DEMO**: Paper trading — full scan + safety checks but zero real transactions. Tracks virtual P&L using live prices.
- **SEMI-AUTO**: User manually triggers buys via UI. Automated TP/SL/trailing stop execution.
- **FULL-AUTO**: Autonomous scanner → safety filter → buy → sell loop. Runs every 15s.

### Transaction Optimizations
- **Compute limit**: Hard 200k (prevents overpaying vs 1.4M default)
- **Buy fee**: 100k μL default (configurable per trade)
- **Sell fee**: 800k μL default (emergency exit power, configurable per trade)
- **Jito tip**: 0.001 SOL per TX, rotated across 8 tip accounts, skips public mempool
- **CloseAccount**: Bundled into every sell — recovers ~0.002 SOL ATA rent back to wallet

### Safety Filters (pre-buy, <200ms)
1. RugCheck API — "Good"/"Excellent" required
2. Top-10 holder concentration check — abort if >30% (bundle risk)
3. Mint authority disabled check
4. Freeze authority disabled check
5. DexScreener liquidity check
6. Pump.fun dev holdings check

### Risk Management
- Take Profit: configurable (default 75%)
- Stop Loss: configurable (default 15%)
- Trailing Stop: activates at configurable gain (default 20%)
- All three run in the monitor thread, polling every 4s

## Environment Variables (.env)
```
PRIVATE_KEY          # Solana wallet private key (base58)
RPC_URL              # Helius / QuickNode / Shyft / public
JITO_BLOCK_ENGINE_URL# Jito block engine for bundle submission
TELEGRAM_BOT_TOKEN   # From @BotFather
TELEGRAM_CHAT_ID     # From @userinfobot
AUTO_BUY_SOL         # Full-Auto buy size (default 0.05)
AUTO_TP              # Full-Auto take profit % (default 75)
AUTO_SL              # Full-Auto stop loss % (default 15)
AUTO_TS              # Full-Auto trailing stop % (default 20)
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard UI |
| POST | `/api/start` | Open position (buy or demo) |
| POST | `/api/stop` | Sell position or stop-all |
| GET | `/api/status` | Full status, positions, log, scan results |
| POST | `/api/mode` | Switch Demo/Semi/Full-Auto |
| POST | `/api/check` | Run safety check on a token |
| GET | `/api/scan` | Get scanner results |
| POST | `/api/scan/start` | Start auto-scanner |
| GET | `/api/config/get` | Get masked config status |
| POST | `/api/config/save` | Save secrets to .env (server-side only) |
| POST | `/api/notify/test` | Send Telegram test message |
| GET | `/api/health` | Health check |

## Deployment
- **Dev**: `python app.py` (Flask dev server)
- **Prod**: VM deployment only (not serverless — background threads required)
- Uses gunicorn for production: `gunicorn -w 1 -b 0.0.0.0:5000 app:app`
- Background threads (monitor loop, scanner loop) must survive between requests

## Security Notes
- All secrets written to `.env` via `/api/config/save` — server-side only
- Frontend never receives actual key values — only masked previews (first4…last2)
- `.env` is gitignored
