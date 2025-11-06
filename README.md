# Hyperliquid Momentum Trader

This repository now focuses on the lightweight Hyperliquid trading backend.  It provides:

- A Telegram trade monitor that streams filled orders for any set of wallet addresses
- A momentum-based live trading loop with configurable thresholds, size caps, and risk controls
- Optional rolling analytics (Sharpe, max drawdown) to track strategy quality in real time

All legacy NASDAQ/SSE AI-agent assets, data files, and UI artifacts have been removed to keep the codebase minimal.

## Repository Layout

- `backend/main.py` – CLI entry point for monitoring, dry-runs, and live trading
- `backend/trader.py` – moving-average momentum strategy, risk controls, analytics logging
- `backend/monitor_positions.py` – utility for polling open positions
- `backend/api.py` – FastAPI service that exposes wallet summaries, fills, and metrics
- `backend/requirements.txt` – Python dependencies for the backend services
- `backend/.env` – runtime secrets (ignored by git); copy from `.env.example` and edit locally
- `frontend/` – Vite + React single-page app for visualizing wallet data

## Prerequisites

- Python 3.11 or newer (tested with 3.13)
- Hyperliquid account (wallet + vault)
- Optional: Telegram bot token + chat id for trade alerts

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example backend/.env
cp frontend/.env.example frontend/.env
```

Edit `backend/.env` with your details:

```bash
TELEGRAM_BOT_TOKEN="123:abc"      # optional
TELEGRAM_CHAT_ID="123456789"      # optional
WALLET_ADDRESSES=["0xabc..."]      # monitored addresses
HYPERLIQUID_PRIVATE_KEY="0x..."    # required for --hl-execute
HYPERLIQUID_ACCOUNT_ADDRESS=""     # optional override; defaults to wallet
HYPERLIQUID_VAULT_ADDRESS=""       # optional for subaccounts
HYPERLIQUID_BASE_URL=""            # leave empty for mainnet
```

> The CLI automatically loads `backend/.env`.  Secrets in the project root are no longer referenced.

### Frontend `.env`

Edit `frontend/.env` with the API base (defaults shown):

```bash
VITE_API_BASE_URL="http://localhost:8000/api"
```

## Funding From OKX

Live trades will fail with `Insufficient margin` unless your Hyperliquid account holds USDC collateral.  To fund it from an OKX wallet:

1. Log in to [hyperliquid.xyz](https://hyperliquid.xyz) and open **Wallet → Deposit**.
2. Select **Arbitrum One** (USDC) and copy the deposit address (it matches the wallet in `WALLET_ADDRESSES`).
3. In OKX, withdraw USDC to that address using the same network (`Arbitrum One`).  Keep a small amount of ETH on Arbitrum for gas if you plan to move funds later.
4. Wait for the bridge confirmations (typically 1–2 minutes).  Re-run the snippet below to confirm margin:

   ```bash
   python - <<'PY'
   from hyperliquid.info import Info
   from eth_account import Account

   info = Info(skip_ws=True)
   wallet = Account.from_key("0x<private-key>")
   state = info.user_state(wallet.address)
   print(state.get("marginSummary", {}))
   PY
   ```

5. Start with a small `--hl-max-usd` value and scale once the vault shows a positive `portfolioMarginUsd`.

## Usage

### Trade Monitor

```bash
cd backend
python main.py --mode trades
```

### REST API

Spin up the monitoring API (serves summaries, fills, and metrics for the React app):

```bash
cd backend
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Key endpoints:

- `GET /api/health` – service heartbeat
- `GET /api/wallets` – tracked wallet list
- `GET /api/wallets/{address}` – balances and open positions
- `GET /api/wallets/{address}/fills?limit=50` – recent fills
- `GET /api/wallets/{address}/metrics` – coin-level aggregates derived from fills

Set `API_ALLOWED_ORIGINS` in the environment if you need to relax CORS beyond `http://localhost:5173`.

### Frontend Dashboard

Launch the Vite dev server after the API is reachable:

```bash
cd frontend
npm install  # run once if dependencies are missing
npm run dev
```

Open the printed URL (defaults to `http://localhost:5173`). Use the wallet selector to switch addresses, review balances, open positions, and a live feed of fills.

## Deployment (Vercel + GitHub)

Automatic deploys are configured with `vercel.json`. To ship every push on `main`:

1. **Bootstrap git** – from the project root run `rm -rf .git && git init` if you want a clean history, then create a new remote pointing at `git@github.com:ch0002ic/hyperliquid-monitor.git` (or HTTPS) and commit the full tree.
2. **Push once** – `git add . && git commit -m "Initial commit" && git branch -M main && git remote add origin <repo-url> && git push -u origin main`.
3. **Link Vercel** – in the Vercel dashboard create a project, import the GitHub repo, and keep the default `main` branch auto-deploy setting.
4. **Set environment variables** – add `VITE_API_BASE_URL=https://<your-vercel-domain>/api` on the **Production** and **Preview** tabs so the SPA calls the colocated API. Optionally set `API_ALLOWED_ORIGINS=https://<your-vercel-domain>` for stricter CORS.
5. **Trigger deploys** – subsequent pushes to `main` (or configured branches) will build the React app from `frontend` and expose the FastAPI serverless function at `/api/*`.

### Dry-Run Strategy Loop

```bash
python main.py \
    --mode live-trade \
    --hl-dry-run \
    --hl-coins BTC,ETH \
    --hl-short-window 24 \
    --hl-long-window 96 \
    --hl-threshold 0.0025 \
    --hl-flat-band 0.025 \
    --hl-max-usd 25 \
    --hl-slippage 0.005 \
    --hl-analytics \
    --hl-analytics-window 96
```

### Live Trading

```bash
python main.py \
    --mode live-trade \
    --hl-execute \
    --hl-coins BTC,ETH \
    --hl-short-window 24 \
    --hl-long-window 96 \
    --hl-threshold-long 0.0035 \
    --hl-threshold-short 0.0025 \
    --hl-flat-band 0.02 \
    --hl-max-usd 20 \
    --hl-slippage 0.005 \
    --hl-poll-seconds 180 \
    --hl-sleep-between 1 \
    --hl-analytics \
    --hl-analytics-window 96
```

- Provide `HYPERLIQUID_PRIVATE_KEY` via env or `--hl-private-key`.
- Add `--skip-telegram` if you do not want notifications.
- Use `Ctrl+C` to exit safely; the loop catches interrupts and stops cleanly.

## Troubleshooting

- **`Insufficient margin`** – deposit USDC into Hyperliquid or reduce `--hl-max-usd` until collateral is available.
- **`Invalid price`** – tighten the `--hl-slippage` buffer; the trader places IOC limit orders at `mid_price * (1 ± slippage)`.
- **Telegram failures** – verify bot + chat IDs and that the `.env` file is loaded before starting the monitor.
- **Analytics disabled** – omit `--hl-analytics` if you prefer a minimal log.  With the flag enabled, rolling Sharpe and max drawdown per coin are logged each iteration.

## Next Steps

With the repository cleaned, consider:

1. Funding the Hyperliquid vault from OKX as described above.
2. Re-running a dry-run sweep to confirm signal quality after any threshold changes.
3. Starting a small live session once `portfolioMarginUsd` is non-zero.

Happy trading!
