# cryptrizon_sniper_bot.py
# CryptRizon — scans ALL Binance spot coins, finds bottom/top zone setups,
# adds 1‑month fallbacks, BOB 1m tracker, coverage %, skip logs, and scheduler.
# Requires: python-telegram-bot >=20,<23  and  requests

import json, os, re, time, requests
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========= CONFIG =========
# Option A: hardcode here
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8346599110:AAFg4yM1npJdS-eAy2d8tkrRLgxCIXbH_gk")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "6968479583")

CONFIG_FILE = "cryptrizon_config.json"
DEFAULT_CONFIG = {
    "symbols_cache": [],             # auto-filled Binance base symbols
    "symbols_cache_updated": 0,
    "symbols_cache_ttl_hours": 12,   # refresh symbol list every 12h
    "fallback_enabled": True,        # 1m fallback when 3m has no bottom/top
    "push_interval_hours": 6,        # auto message cadence
    "bob_days": 30,                  # BOB fixed 1m
    "alpha_days_primary": 90,        # primary = 3 months
    "alpha_days_fallback": 30,       # fallback = 1 month
    "bottom_pct": 0.20,              # ≤20% = bottom zone
    "top_pct": 0.80                  # ≥80% = top zone
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
push_job = None  # JobQueue reference

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
    # FWOGUSDT -> FWOG
    return re.sub(r"(USDT|FDUSD|USDC|BUSD|TUSD|TRY|EUR|BRL|NGN|RUB|GBP|DAI|USTC)$", "", sym, flags=re.I)

def fetch_all_binance_bases() -> List[str]:
    bases = set()
    data = get_json(BINANCE_PRODUCTS)
    if data and isinstance(data.get("data"), list):
        for it in data["data"]:
            s = it.get("s") or it.get("symbol")
            if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                bases.add(_strip_quote(s))
    if not bases:
        data2 = get_json(BINANCE_PRODUCTS_ALT, params={"includeEtf":"false"})
        if data2 and isinstance(data2.get("data"), list):
            for it in data2["data"]:
                s = it.get("s") or it.get("symbol")
                if s and s.upper().endswith(("USDT","FDUSD","USDC","BUSD","TUSD")):
                    bases.add(_strip_quote(s))
    return sorted(bases)

def get_full_binance_universe(force_refresh: bool = False) -> List[str]:
    now = int(time.time())
    ttl = int(CFG.get("symbols_cache_ttl_hours", 12)) * 3600
    if CFG.get("symbols_cache") and not force_refresh and now - int(CFG.get("symbols_cache_updated",0)) < ttl:
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
    if not data: return None
    q = query.lower()
    best = None
    for c in data.get("coins", []):
        if c.get("symbol","").lower() == q or c.get("name","").lower() == q:
            return c.get("id")
        if best is None:
            best = c.get("id")
    return best

def fetch_prices(cg_id: str, days: int) -> Optional[List[float]]:
    data = get_json(f"{CG}/coins/{cg_id}/market_chart",
                    params={"vs_currency":"usd","days":str(days),"interval":"hourly"})
    if not data: return None
    arr = [p[1] for p in data.get("prices", [])]
    return arr or None

def zone_metrics(prices: List[float]) -> Tuple[float,float,float,float]:
    low, high = min(prices), max(prices)
    cur = prices[-1]
    rng = max(high - low, 1e-18)
    pct = (cur - low) / rng  # 0..1
    return cur, low, high, pct

def zone_label(pct: float) -> str:
    if pct <= CFG["bottom_pct"]: return "Bottom"
    if pct >= CFG["top_pct"]:    return "High"
    return "Mid"

def momentum_bias(prices: List[float], hours: int = 12) -> str:
    if len(prices) < 2: return "flat"
    sl = prices[-hours:] if len(prices) >= hours else prices
    slope = sl[-1] - sl[0]
    return "up" if slope > 0 else ("down" if slope < 0 else "flat")

# ---------- BOB (fixed 1m) ----------
def bob_block() -> str:
    prices = fetch_prices("build-on-bnb", CFG["bob_days"])
    if not prices:
        return "🪙 BOB — data unavailable."
    cur, low, high, pct = zone_metrics(prices)
    zone = zone_label(pct)
    bias = momentum_bias(prices)
    return (
        f"🪙 BOB — {CFG['bob_days']}‑Day View\n"
        f"Price: ${fmt(cur)}\n"
        f"Low / High: ${fmt(low)} – ${fmt(high)}\n"
        f"Zone: **{zone}**  (≤{int(CFG['bottom_pct']*100)}% = Bottom, ≥{int(CFG['top_pct']*100)}% = High)\n"
        f"12h Momentum: {bias}\n"
        f"Rule: Buy only in Bottom; Trim in High; Mid = hold/observe.\n"
    )

# ---------- scanning with daily skip log ----------
def scan_symbols(days: int) -> Tuple[List[Dict], int, Dict[str,int]]:
    tokens = get_full_binance_universe(False)
    scanned = 0
    skip_reasons = {"no_cg_match": 0, "no_price_data": 0}
    skip_list: List[Tuple[str,str]] = []
    out: List[Dict] = []

    for sym in tokens:
        cg_id = resolve_cg_id(sym)
        if not cg_id:
            # small heuristic for SOL tickers (FWOG etc.)
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

    # write daily skip log
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with open(f"skip_log_{today}.txt", "w", encoding="utf-8") as f:
        for sym, reason in skip_list:
            f.write(f"{sym}: {reason}\n")

    return out, scanned, skip_reasons

# ---------- build sections ----------
def build_blocks_from_results(results_3m: List[Dict]) -> Tuple[str, bool, bool]:
    bottoms3 = [r for r in results_3m if r["pct"] <= CFG["bottom_pct"]]
    tops3    = [r for r in results_3m if r["pct"] >= CFG["top_pct"]]

    closest_bottom3 = sorted(results_3m, key=lambda r: r["pct"])[:3]
    closest_top3    = sorted(results_3m, key=lambda r: (1 - r["pct"]))[:3]

    parts = []
    # Bottom sniper
    if bottoms3:
        best = min(bottoms3, key=lambda r: r["pct"])
        parts.append(
            "🎯 Bottom‑Zone Sniper (3‑month)\n"
            f"{best['name']} — ${fmt(best['cur'])}\n"
            f"90d L/H: ${fmt(best['low'])} – ${fmt(best['high'])}\n"
            f"Zone: **{best['zone']}** (pos {best['pct']*100:.1f}%)"
        )
    else:
        parts.append("🎯 Bottom‑Zone Sniper (3‑month): **None**")

    # 3 closest to bottom
    if closest_bottom3:
        parts.append("**3 closest to bottom (3‑month):**\n" + "\n".join(
            f"• {r['name']}: ${fmt(r['cur'])} | 90d L/H {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
            for r in closest_bottom3
        ))

    # Top watch
    if tops3:
        top_best = max(tops3, key=lambda r: r["pct"])
        parts.append(
            "\n📈 Top‑Zone Watch (3‑month)\n"
            f"{top_best['name']} — ${fmt(top_best['cur'])}\n"
            f"90d L/H: ${fmt(top_best['low'])} – ${fmt(top_best['high'])}\n"
            f"Zone: **{top_best['zone']}** (pos {top_best['pct']*100:.1f}%)"
        )
    else:
        parts.append("\n📈 Top‑Zone Watch (3‑month): **None**")

    # 3 closest to top
    if closest_top3:
        parts.append("**3 closest to top (3‑month):**\n" + "\n".join(
            f"• {r['name']}: ${fmt(r['cur'])} | 90d L/H {fmt(r['low'])}/{fmt(r['high'])} | Zone {r['zone']} | Pos {r['pct']*100:.1f}%"
            for r in closest_top3
        ))

    text = "\n\n".join(parts)
    return text, bool(bottoms3), bool(tops3)

def build_status_text() -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # 3‑month scan
    res3m, scanned_3m, skip3 = scan_symbols(CFG["alpha_days_primary"])
    symbols_total = len(get_full_binance_universe(False))
    coverage = fmt_pct((scanned_3m / symbols_total) * 100) if symbols_total else "0.0%"
    skip_summary = (
        f"Skipped: {skip3['no_cg_match']} no CG match, "
        f"{skip3['no_price_data']} no/insufficient price data\n"
        f"Details saved to skip_log_{datetime.utcnow().strftime('%Y-%m-%d')}.txt"
    )

    blocks_3m, has_bottom_3m, has_top_3m = build_blocks_from_results(res3m)
    blocks = blocks_3m

    # Fallbacks (1m) if enabled
    if CFG["fallback_enabled"]:
        # Bottom fallback if no 3m bottom
        if not has_bottom_3m:
            res1m, _, _ = scan_symbols(CFG["alpha_days_fallback"])
            bottoms1 = [r for r in res1m if r["pct"] <= CFG["bottom_pct"]]
            if bottoms1:
                best1 = min(bottoms1, key=lambda r: r["pct"])
                blocks += (
                    "\n\n🔁 Fallback: Short‑term bottom (1‑month)\n"
                    f"{best1['name']} — ${fmt(best1['cur'])}\n"
                    f"30d L/H: ${fmt(best1['low'])} – ${fmt(best1['high'])}\n"
                    f"Zone: **{best1['zone']}** (pos {best1['pct']*100:.1f}%)\n"
                    "Label: *Short‑term bottom (1‑month)* — not the primary sniper trigger."
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month): **No coin in bottom zone either.**"

        # Top fallback if no 3m top
        if not has_top_3m:
            res1m_top, _, _ = scan_symbols(CFG["alpha_days_fallback"])
            tops1 = [r for r in res1m_top if r["pct"] >= CFG["top_pct"]]
            if tops1:
                top1 = max(tops1, key=lambda r: r["pct"])
                blocks += (
                    "\n\n🔁 Fallback: Short‑term top (1‑month)\n"
                    f"{top1['name']} — ${fmt(top1['cur'])}\n"
                    f"30d L/H: ${fmt(top1['low'])} – ${fmt(top1['high'])}\n"
                    f"Zone: **{top1['zone']}** (pos {top1['pct']*100:.1f}%)\n"
                    "Label: *Short‑term top (1‑month)* — potential trim/TP area."
                )
            else:
                blocks += "\n\n🔁 Fallback (1‑month Top): **No coin in top zone either.**"

    header = (
        f"📡 CryptRizon Sniper — {now}\n"
        f"Symbols fetched: {symbols_total} | Scanned (3m): {scanned_3m} | Coverage: {coverage}\n"
        f"{skip_summary}\n"
    )
    return f"{header}\n{blocks}\n\n{bob_block()}"

# ---------- Telegram commands ----------
def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CryptRizon Sniper 🤖 (All Binance symbols)\n"
        "/status — Full report\n"
        "/config — View settings\n"
        "/setinterval H — Auto-push interval (hours)\n"
        "/pushnow — Send an update now\n"
        "/refreshalpha — Refresh Binance symbols cache\n"
        "/setzones BOTTOM% TOP% — e.g., /setzones 15 85\n"
        "/zones — show current zone thresholds"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_status_text(), parse_mode="Markdown")

async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = get_full_binance_universe(False)
    msg = (
        f"Config:\n"
        f"• Binance symbols tracked (auto): {len(tokens)}\n"
        f"• Fallback 1m: {CFG['fallback_enabled']}\n"
        f"• Interval: {CFG['push_interval_hours']}h\n"
        f"• Days: 3m={CFG['alpha_days_primary']} / 1m={CFG['alpha_days_fallback']} (BOB={CFG['bob_days']})\n"
        f"• Zones: bottom≤{int(CFG['bottom_pct']*100)}%, top≥{int(CFG['top_pct']*100)}%\n"
        f"• Last symbols refresh: {datetime.utcfromtimestamp(CFG.get('symbols_cache_updated',0)).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await update.message.reply_text(msg)

async def setinterval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /setinterval HOURS (e.g., /setinterval 6)")
    hours = max(1, int(context.args[0]))
    CFG["push_interval_hours"] = hours
    save_cfg(CFG)
    global push_job
    if push_job:
        push_job.schedule_removal()
    push_job = update.application.job_queue.run_repeating(
        lambda ctx: ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=build_status_text(), parse_mode="Markdown"),
        interval=hours*60*60, first=3
    )
    await update.message.reply_text(f"Auto‑push set to every {hours}h.")

async def pushnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    await update.message.chat.send_message(build_status_text(), parse_mode="Markdown")

async def refreshalpha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    get_full_binance_universe(True)
    await update.message.reply_text(f"Symbols cache refreshed. Now tracking ~{len(CFG['symbols_cache'])} base assets.")

async def setzones_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /setzones BOTTOM% TOP%\nExample: /setzones 15 85")
    try:
        bottom = int(context.args[0]); top = int(context.args[1])
        if not (0 <= bottom < top <= 100):
            return await update.message.reply_text("Percentages must be 0–100 and bottom < top.")
        CFG["bottom_pct"] = bottom / 100
        CFG["top_pct"]    = top / 100
        save_cfg(CFG)
        await update.message.reply_text(f"Zones updated:\nBottom ≤ {bottom}%\nTop ≥ {top}%")
    except ValueError:
        await update.message.reply_text("Please enter valid integers for percentages.")

async def zones_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bottom = int(CFG.get("bottom_pct", 0.20) * 100)
    top    = int(CFG.get("top_pct", 0.80) * 100)
    await update.message.reply_text(
        f"Current zones:\n• Bottom ≤ {bottom}% of range\n• Top ≥ {top}% of range"
    )

# ---------- main ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("config", config_cmd))
    app.add_handler(CommandHandler("setinterval", setinterval_cmd))
    app.add_handler(CommandHandler("pushnow", pushnow_cmd))
    app.add_handler(CommandHandler("refreshalpha", refreshalpha_cmd))
    app.add_handler(CommandHandler("setzones", setzones_cmd))
    app.add_handler(CommandHandler("zones", zones_cmd))

    # immediate push on startup
    if ADMIN_CHAT_ID:
        app.job_queue.run_once(
            lambda ctx: ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=build_status_text(), parse_mode="Markdown"),
            when=3
        )

    # scheduled auto‑push
    global push_job
    if ADMIN_CHAT_ID and int(CFG["push_interval_hours"]) > 0:
        push_job = app.job_queue.run_repeating(
            lambda ctx: ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=build_status_text(), parse_mode="Markdown"),
            interval=int(CFG["push_interval_hours"]) * 60 * 60,
            first=5
        )

    app.run_polling()

if __name__ == "__main__":
    main()
