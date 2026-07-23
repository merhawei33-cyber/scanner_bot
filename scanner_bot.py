"""
scanner_bot.py - Djmere Scanner Bot
Compatible with Python 3.14 + python-telegram-bot 21.x
"""

import os
import asyncio
import requests
import json
import logging
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("SCANNER_BOT_TOKEN", "")
CHAT_ID        = os.getenv("SCANNER_CHAT_ID", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

SKIP_SYMBOLS = {
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT",
    "XRPUSDT","ADAUSDT","DOGEUSDT","MATICUSDT",
    "AVAXUSDT","LTCUSDT","LINKUSDT","DOTUSDT"
}

MIN_VOLUME_USDT    = 1_000_000
MIN_GAIN_PCT       = 8.0
TOP_N              = 20
MIN_CONFIDENCE     = 80
SCAN_INTERVAL_SECS = 3600


def get_top_gainers() -> list:
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear"}, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DjmereBot/1.0)"}
        )
        if resp.status_code != 200:
            logger.error(f"Bybit returned status {resp.status_code}: {resp.text[:500]}")
            return []
        try:
            data = resp.json()
        except Exception as je:
            logger.error(f"Bybit non-JSON response ({je}): {resp.text[:500]}")
            return []
        if data.get("retCode") != 0:
            logger.error(f"Bybit API error retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
            return []
        coins = []
        for item in data["result"]["list"]:
            symbol = item["symbol"]
            if not symbol.endswith("USDT") or symbol in SKIP_SYMBOLS:
                continue
            try:
                change_pct = float(item.get("price24hPcnt", 0)) * 100
                volume     = float(item.get("turnover24h", 0))
                price      = float(item.get("lastPrice", 0))
            except Exception:
                continue
            if volume < MIN_VOLUME_USDT or price <= 0:
                continue
            coins.append({"symbol": symbol, "price": price,
                           "change_pct": round(change_pct, 2), "volume": volume})
        coins.sort(key=lambda x: x["change_pct"], reverse=True)
        return coins[:TOP_N]
    except Exception as e:
        logger.error(f"Error: {e}")
        return []
def get_candles(symbol: str, interval: str = "60", limit: int = 30) -> list:
    try:
        resp = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": interval, "limit": limit},
            timeout=10
        )
        return resp.json()["result"]["list"]
    except Exception:
        return []


def analyze_with_claude(symbol, price, change_pct, candles_1h, candles_4h) -> dict:
    def fmt(candles):
        return "\n".join(
            f"O:{c[1]} H:{c[2]} L:{c[3]} C:{c[4]} V:{c[5]}"
            for c in candles[:15] if len(c) >= 6
        )
    prompt = (
        f"אתה טריידר מקצועי. נתח את {symbol} מחיר {price}$ עלייה {change_pct}%\n"
        f"1H נרות:\n{fmt(candles_1h)}\n4H נרות:\n{fmt(candles_4h)}\n"
        "ענה בדיוק בפורמט הזה, שורה לכל שדה, בלי שום דבר נוסף:\n"
        "DIRECTION: LONG או SHORT או SKIP\n"
        "CONFIDENCE: מספר בין 0 ל-100\n"
        "ENTRY: מחיר\n"
        "SL: מחיר\n"
        "TP: מחיר\n"
        "REASON: משפט קצר אחד"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        text = resp.json()["content"][0]["text"].strip()
        result = {"direction": "SKIP", "confidence": 0}
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("DIRECTION:"):
                result["direction"] = line.split(":", 1)[1].strip().upper()
            elif line.upper().startswith("CONFIDENCE:"):
                digits = "".join(c for c in line.split(":", 1)[1] if c.isdigit())
                result["confidence"] = int(digits) if digits else 0
            elif line.upper().startswith("ENTRY:"):
                result["entry"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("SL:"):
                result["sl"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("TP:"):
                result["tp"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("REASON:"):
                result["reason"] = line.split(":", 1)[1].strip()
        return result
    except Exception as e:
        logger.error(f"Claude error {symbol}: {e}")
        return {"direction": "SKIP", "confidence": 0}

async def run_scan(bot: Bot):
    now = datetime.now().strftime("%d/%m %H:%M")
    await bot.send_message(chat_id=CHAT_ID,
        text=f"🔍 *סריקת שוק מתחילה...*\n{now}", parse_mode="Markdown")
    gainers = get_top_gainers()
    if not gainers:
        await bot.send_message(chat_id=CHAT_ID, text="⚠️ לא נמצאו מטבעות.")
        return
    await bot.send_message(chat_id=CHAT_ID,
        text=f"📊 נמצאו *{len(gainers)}* מטבעות — מנתח...", parse_mode="Markdown")
    results = []
    for coin in gainers:
        symbol = coin["symbol"]
        analysis = analyze_with_claude(symbol, coin["price"], coin["change_pct"],
                                        get_candles(symbol, "60"), get_candles(symbol, "240"))
        if analysis.get("direction") != "SKIP" and analysis.get("confidence", 0) >= MIN_CONFIDENCE:
            results.append({**coin, **analysis})
        await asyncio.sleep(1)
    if not results:
        await bot.send_message(chat_id=CHAT_ID, text=f"😐 לא נמצאו הזדמנויות עם {MIN_CONFIDENCE}%+")
        return
    await bot.send_message(chat_id=CHAT_ID,
        text=f"🔥 *תוצאות — {now}*\n{len(results)} הזדמנויות!", parse_mode="Markdown")
    for i, r in enumerate(results, 1):
        emoji = "📈 LONG" if r["direction"] == "LONG" else "📉 SHORT"
        msg = (f"*{i}. {r['symbol']}* {emoji}\n"
               f"עלייה: +{r['change_pct']}% | ביטחון: *{r['confidence']}%*\n"
               f"כניסה: `${r.get('entry','?')}` | SL: `${r.get('sl','?')}` | TP: `${r.get('tp','?')}`\n"
               f"📝 {r.get('reason','')}")
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        await asyncio.sleep(0.5)
    await bot.send_message(chat_id=CHAT_ID, text="✅ סריקה הושלמה | הבאה בעוד שעה")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *Djmere Scanner Bot*\n\n/scan — סריקה\n/top — ירוקים\n/status — סטטוס", parse_mode="Markdown")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ סורק...")
    await run_scan(context.bot)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gainers = get_top_gainers()
    if not gainers:
        await update.message.reply_text("לא נמצאו.")
        return
    lines = ["🔥 *Top Gainers*\n"] + [f"{i}. {c['symbol']} +{c['change_pct']}%" for i, c in enumerate(gainers[:15], 1)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ פעיל | {datetime.now().strftime('%d/%m %H:%M')} | סריקה כל שעה | {MIN_CONFIDENCE}% מינימום", parse_mode="Markdown")

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    await run_scan(context.bot)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("top",    cmd_top))
    app.add_handler(CommandHandler("status", cmd_status))
    app.job_queue.run_repeating(auto_scan_job, interval=SCAN_INTERVAL_SECS, first=60)
    logger.info("🚀 Scanner Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
