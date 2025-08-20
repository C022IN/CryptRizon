# CryptRizon Actions

FastAPI backend for CryptRizon GPT.

## Endpoints

- `/status` — returns full scan & report (Telegram-ready text)
- `/binance/bases` — Binance spot bases
- `/coingecko/search` — resolve token to CoinGecko id
- `/coingecko/prices` — market chart arrays
- `/config` — get or set config (zones, Alpha list)
- `/telegram/push` — send message to Telegram admin chat

## Deployment

### Railway / Render
1. Create repo with files: `app.py`, `requirements.txt`, `Procfile`, `README.md`.
2. Deploy service, set environment vars:
   - BOT_TOKEN (Telegram bot token, optional)
   - ADMIN_CHAT_ID (Telegram chat id, optional)
   - CONFIG_FILE (optional, default cryptrizon_config.json)
3. Railway/Render gives you public HTTPS URL, use it in your GPT Action OpenAPI spec.

### Fly.io (Docker)
Use included Dockerfile, run `flyctl launch`.

## Local Dev
```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Then open http://localhost:8000/status
