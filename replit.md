# Sniper Pro Bot

A cryptocurrency trading bot for sniping tokens on the Solana blockchain, specifically targeting the Pump.fun bonding curve platform. Includes a real-time web dashboard.

## Architecture

- **Backend/Frontend:** Python + Flask (single app serves both API and HTML templates)
- **Port:** 5000 (0.0.0.0)
- **Blockchain:** Solana via `solana` and `solders` Python libraries
- **Custom SDK:** `pump_sdk.py` for Pump.fun smart contract interaction

## Project Structure

```
app.py          - Flask app entry point (API routes + template serving)
bot_logic.py    - Core trading engine and price monitoring (background thread)
pump_sdk.py     - Solana/Pump.fun transaction building SDK
Pump_sdk.py     - Duplicate/case variant of pump_sdk.py
templates/      - HTML dashboard (index.html)
Template/       - Alternate templates directory
requirements.txt - Python dependencies
.env.example    - Environment variable template
```

## Environment Variables

Copy `.env.example` to `.env` and fill in:
- `PRIVATE_KEY` - Solana wallet private key (base58 encoded)
- `RPC_URL` - Solana RPC endpoint (defaults to mainnet-beta public node)

## Running

```bash
python app.py
```

Runs on `0.0.0.0:5000`.

## Deployment

Configured as a VM deployment (always-on) using gunicorn, since the bot requires a persistent background monitoring thread.

## Key Dependencies

- flask==3.0.0
- solana==0.34.3
- solders==0.21.0
- requests==2.31.0
- python-dotenv==1.0.0
- base58==2.1.1
- gunicorn==21.2.0
