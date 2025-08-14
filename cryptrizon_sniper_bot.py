import os
import json
import asyncio
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ------------------------
# Load Config
# ------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
BOTTOM_PERCENT = int(os.getenv("BOTTOM_PERCENT", "15"))
TOP_PERCENT = int(os.getenv("TOP_PERCENT", "85"))
PUSH_INTERVAL_HOURS = int(os.getenv("PUSH_INTERVAL_HOURS", "0"))

# ------------------------
# Logging
# ------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------
# Binance API
# ------------------------
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"

def get_all_binance_symbols():
    try:
        data = requests.get(BINANCE_TICKER_URL, timeout=10).json()
        return [x["symbol"] for x in data if x["symbol"].endswith("USDT")]
    except Exception as e:
        logger.error(f"Error fetching symbols: {e}")
        return []

def get_symbol_data(symbol):
    try:
        data = requests.get(f"{BINANCE_TICKER_URL}?symbol={symbol}", timeout=10).json()
        return data
    except Exception:
        return None

# ------------------------
# Analysis Logic
# ------------------------
def is_in_bottom_zone(price, low, high):
    if high == low:
        return False
    percent = ((price - low) / (high - low)) * 100
    return percent <= BOTTOM_PERCENT

def is_in_top_zone(price, low, high):
    if high == low:
        return False
    percent = ((price - low) / (high - low)) * 100
    return percent >= TOP_PERCENT

def build_status_text():
    symbols = get_all_binance_symbols()
    bottom_list = []
    top_list = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for sym in symbols:
        data = get_symbol_data(sym)
        if not data:
            continue
        price = float(data.get("lastPrice", 0))
        low = float(data.get("lowPrice", 0))
        high = float(data.get("highPrice", 0))

        if is_in_bottom_zone(price, low, high):
            bottom_list.append(f"{sym} — {price}")
        elif is_in_top_zone(price, low, high):
            top_list.append(f"{sym} — {price}")

    text = f"📊 *CryptRizon Sniper Report* — {now}\n"
    text += f"Bottom {BOTTOM_PERCENT}% zone:\n" + ("\n".join(bottom_list) if bottom_list else "None") + "\n\n"
    text += f"Top {TOP_PERCENT}% zone:\n" + ("\n".join(top_list) if top_list else "None")
    return text

# ------------------------
# Async Helper
# ------------------------
async def compute_report_async() -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, build_status_text)

# ------------------------
# Command Handlers
# ------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "CryptRizon Sniper 🤖 (All Binance symbols)\n"
        "/status — Full report\n"
        "/setzones BOTTOM% TOP% — e.g., /setzones 15 85\n"
        "/pushnow — Send an update now"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Scanning all Binance symbols…")
    try:
        text = await compute_report_async()
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"⚠️ Scan failed: {e}")

async def pushnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    msg = await update.message.reply_text("⏳ Generating report…")
    try:
        text = await compute_report_async()
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"⚠️ Push failed: {e}")

async def setzones_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOTTOM_PERCENT, TOP_PERCENT
    try:
        bottom, top = map(int, context.args)
        BOTTOM_PERCENT, TOP_PERCENT = bottom, top
        await update.message.reply_text(f"✅ Zones updated: Bottom {bottom}%, Top {top}%")
    except Exception:
        await update.message.reply_text("⚠️ Usage: /setzones 15 85")

# ------------------------
# Main
# ------------------------
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pushnow", pushnow_cmd))
    app.add_handler(CommandHandler("setzones", setzones_cmd))

    if ADMIN_CHAT_ID and PUSH_INTERVAL_HOURS > 0:
        async def scheduled_job(ctx):
            try:
                text = await compute_report_async()
                await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"⚠️ Scheduled scan failed: {e}")

        app.job_queue.run_repeating(lambda ctx: app.create_task(scheduled_job(ctx)),
                                    interval=PUSH_INTERVAL_HOURS * 60 * 60, first=10)

    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
