# Sniper Pro Bot — Solana Memecoin Sniper

A professional Solana memecoin sniping bot for Pump.fun with real-time dashboard, full TP/SL/trailing stop execution, honeypot detection, and multi-position support.

## Architecture

- **Backend/Frontend:** Python + Flask (single app, API + HTML templates)
- **Port:** 5000 (0.0.0.0)
- **Blockchain:** Solana via `solana` + `solders` Python libraries
- **Custom SDK:** `pump_sdk.py` — Pump.fun buy + sell transactions

## Project Structure

```
app.py           - Flask API routes
bot_logic.py     - Trading engine, monitoring thread, safety checks, TP/SL execution
pump_sdk.py      - Pump.fun buy + sell SDK (fully implemented)
templates/
  index.html     - Professional dark trading terminal UI
positions.json   - Persisted active positions (auto-generated at runtime)
requirements.txt - Python dependencies
.env.example     - Environment variable template
```

## Features

- **Buy** tokens on Pump.fun bonding curve
- **Auto-sell** when Take Profit is hit
- **Auto-sell** when Stop Loss is hit
- **Trailing Stop Loss** — SL rises with price to lock in gains
- **Multi-position** — track multiple tokens simultaneously
- **Honeypot / rug detection** via RugCheck.xyz + DexScreener + Pump.fun metadata
- **Safety score** (0–100) shown per position
- **Quick presets** — Safe / Degen / Scalp / Moon
- **Adjustable** slippage, priority fee, trailing stop per trade
- **Live dashboard** — real-time P&L, progress bars, activity log
- **Crash recovery** — positions restored from disk on restart

## Environment Variables

Copy `.env.example` to `.env`:
- `PRIVATE_KEY` — Solana wallet private key (base58)
- `RPC_URL` — Solana RPC endpoint (default: mainnet-beta public)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/status`         | All positions + wallet balance |
| POST | `/api/start`          | Open a new position |
| POST | `/api/stop`           | Stop one or all positions |
| POST | `/api/check`          | Run safety/honeypot check |
| GET  | `/api/position/<addr>`| Single position detail |
| GET  | `/api/health`         | Health check |

## Running

```bash
python app.py  # dev
gunicorn --bind=0.0.0.0:5000 app:app  # production
```

## Deployment

Configured as VM deployment (always-on) — required for the background monitoring thread.

## Key Dependencies

- flask==3.0.0, solana==0.34.3, solders==0.21.0
- requests==2.31.0, python-dotenv==1.0.0, base58==2.1.1, gunicorn==21.2.0
