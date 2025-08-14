# cryptrizon_sniper_bot.py
# CryptRizon — scans ALL Binance spot coins, finds bottom/top zone setups,
# adds 1-month fallbacks, BOB 1m tracker, coverage %, skip logs, and scheduler.
# Requires: python-telegram-bot >=20,<23  and  requests

import os
import re
import json
import time
import requests
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    ContextTypes
)

# ========= CONFIG =========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

CONFIG_FILE = "cryptrizon_config.json"
DEFAULT_CONFIG = {
    "symbols_cache": [],
    "symbols_cache_updated": 0,
    "symbols_cache_ttl_hours": 12,
    "fallback_enabled": True,
    "push_interval_hours": 6,
    "bob_days": 30,
    "alpha_days_primary": 90,
    "alpha_days_fallback": 30,
    "bottom_pct": 0.20,
    "top_pct": 0.80
}
# ==========================

CG = "https://api.coingecko.com/api/v3"
BINANCE_PRODUCTS = "https://www.binance.com/bapi/asset/v1/public/asset-service/product/get-products"
BINANCE_PRODUCTS_ALT = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-product-list"

# ---------- config ----------
def load_cfg() -> Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            cfg = json.load(open(CONFIG_FILE, "r", encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    json.dump(DEFAULT_CONFIG, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
    return DEFAULT_CONFIG.copy()

def save_cfg(cfg: Dict):
    json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)

CFG = load_cfg()

# ---------- utils ----------
def get_json(url: str, params: dict = None, headers: dict = None, timeout=25):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fmt(x: float) -> str:
    return f"{x:.10f}".rstrip("0").rstrip(".")

def fmt_pct(x: float) -> str:
    return f"{x:.1f}%"

# ---------- Binance universe ----------
def _strip_quote(sym: str) -> str:
    return re.sub(r"(USDT|FDUSD|USDC|BUSD|TUSD|TRY|EUR|BRL|NGN|RUB|GBP|DAI|USTC)$", "", sym, flags=re.I)

def fetch_all_binance_bases() -> List[str]:
    bases = set()
    data = get_json(BINANCE_PRODUCTS)
    if data and isinstance(data.get("data"), list):
        for it in data["data"]:
            s = it.get("s") or it.get("symbol")
            if s and s.upper().endswith(("USDT", "FDUSD", "USDC", "BUSD", "TUSD")):
                bases.add(_strip_quote(s))
    if not bases:
        data2 = get_json(BINANCE_PRODUCTS_ALT, params={"includeEtf": "false"})
        if data2 and isinstance(data2.get("data"), list):
            for it in data2["data"]:
                s = it.get("s") or it.get("symbol")
                if s and s.upper().endswith(("USDT", "FDUSD", "USDC", "BUSD", "TUSD")):
                    bases.add(_strip_quote(s))
    return sorted(bases)

def get_full_binance_universe(force_refresh: bool = False) -> List[str]:
    now = int(time.time())
    ttl = int(CFG.get("symbols_cache_ttl_hours", 12)) * 3600
    if CFG.get("symbols_cache") and not force_refresh and now - int(CFG.get("symbols_cache_updated", 0)) < ttl:
        return CFG["symbols_cache"]
    tokens = fetch_all_binance_bases()
    if tokens:
        CFG["symbols_cache"] = tokens
        CFG["symbols_cache_updated"] = now
        save_cfg(CFG)
    return tokens

# ---------- price & zones ----------
def resolve_cg_id(query: str) -> Optional[str]:
    data = get_json(f"{CG}/search", params={"query": query})
    if not data:
        return None
    q = query.lower()
    best = None
    for c in data.get("coins", []):
        if c.get("symbol", "").lower() == q or c.get("name", "").lower() == q:
            return c.get("id")
        if best is None:
            best = c.get("id")
    return best

def fetch_prices(cg_id: str, days: int) -> Optional[List[float]]:
    data = get_json(f"{CG}/coins/{cg_id}/market_chart",
                    params={"vs_currency": "usd", "days": str(days), "interval": "hourly"})
    if not data:
        return None
    arr = [p[1] for p in data.get("prices", [])]
    return arr or None

def zone_metrics(prices: List[float]) -> Tuple[float, float, float, float]:
    low, high = min(prices), max(prices)
    cur = prices[-1]
    rng = max(high - low, 1e-18)
    pct = (cur - low) / rng
    return cur, low, high, pct

def zone_label(pct: float) -> str:
    if pct <= CFG["bottom_pct"]:
        return "Bottom"
    if pct >= CFG["top_pct"]:
        return "High"
    return "Mid"

def momentum_bias(prices: List[float], hours: int = 12) -> str:
    if len(prices) < 2:
        return "flat"
    sl = prices[-hours:] if len(prices) >= hours else prices
    slope = sl[-1] - sl[0]
    return "up" if slope > 0 else ("down" if slope < 0 else "flat")

# ---------- scanning ----------
def scan_symbols(days: int) -> Tuple[List[Dict], int, Dict[str, int]]:
    tokens = get_full_binance_universe(False)
    scanned = 0
    skip_reasons = {"no_cg_match": 0, "no_price_data": 0}
    skip_list: List[Tuple[str, str]] = []
    out: List[Dict] = []

    for sym in tokens:
        cg_id = resolve_cg_id(sym)
        if not cg_id:
            cg_id = resolve_cg_id(sym + " solana")
        if not cg_id:
            skip_reasons["no_cg_match"] += 1
            skip_list.append((sym, "no_cg_match"))
            continue

        prices = fetch_prices(cg_id, days)
        if not prices:
            skip_reasons["no_price_data"] += 1
            skip_list.append((sym, "no_price_data"))
            continue

        scanned += 1
        cur, low, high, pct = zone_metrics(prices)
        out.append({"name": sym, "cur": cur, "low": low, "high": high, "pct": pct, "zone": zone_label(pct)})

    today = datetime.utcnow().strftime("%Y-%m-%d")
    with open(f"skip_log_{today}.txt", "w", encoding="utf-8") as f:
        for sym, reason in skip_list:
            f.write(f"{sym}: {reason}\n")

    return out, scanned, skip_reasons

# ---------- build report ----------
def build_status_text() -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    res3m, scanned_3m, skip3 = scan_symbols(CFG["alpha_days_primary"])
    symbols_total = len(get_full_binance_universe(False))
    coverage = fmt_pct((scanned_3m / symbols_total) * 100) if symbols_total else "0.0%"

    header = (
        f"📡 CryptRizon Sniper — {now}\n"
        f"Symbols fetched: {symbols_total} | Scanned (3m): {scanned_3m} | Coverage: {coverage}\n"
        f"Skipped: {skip3['no_cg_match']} no CG match, {skip3['no_price_data']} no price data"
    )
    return header

# ---------- commands ----------
def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CryptRizon Sniper 🤖\n"
        "/status — Full report\n"
        "/config — View settings\n"
        "/setinterval H — Auto-push interval\n"
        "/pushnow — Send an update now"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_status_text())

async def pushnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(build_status_text())

# ---------- async entry ----------
import asyncio

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pushnow", pushnow_cmd))

    # Auto push job
    if ADMIN_CHAT_ID:
        app.job_queue.run_repeating(
            lambda ctx: ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=build_status_text()),
            interval=CFG["push_interval_hours"] * 3600,
            first=5
        )

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
