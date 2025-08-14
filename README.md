# CryptRizon — Binance-wide Bottom/Top Sniper

Scans **all Binance spot coins** and reports:
- 🎯 Bottom‑Zone Sniper (3‑month), with 1‑month fallback
- 📈 Top‑Zone Watch (3‑month), with 1‑month fallback
- 🪙 BOB (Build On BNB) 30‑day special tracker
- Coverage %, counts scanned, and a daily skip log file

## Commands
- `/status` — full report on demand
- `/config` — view settings
- `/setinterval H` — auto push cadence (hours)
- `/pushnow` — send report now
- `/refreshalpha` — refresh Binance symbols cache
- `/setzones BOTTOM% TOP%` — e.g., `/setzones 15 85`
- `/zones` — show current zone thresholds

## Quick start — local or VPS
```bash
# Ubuntu
sudo apt update
sudo apt install -y git python3 python3-venv

# clone your repo
git clone https://github.com/<YOU>/CryptRizon.git
cd CryptRizon

# set config (A: hardcode in .py OR B: env file)
# A) edit cryptrizon_sniper_bot.py and set BOT_TOKEN + ADMIN_CHAT_ID
# B) use .env:
cp .env.example .env
# edit .env values
export $(grep -v '^#' .env | xargs)

# venv + install
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# run once (you'll get an immediate Telegram report)
python cryptrizon_sniper_bot.py
