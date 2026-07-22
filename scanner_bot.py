"""
scanner_bot.py
בוט סריקת אלטקוינים - Djmere_Bot
סורק Top Gainers בBybit Futures ומנתח עם Claude AI
"""

import os
import asyncio
import requests
import json
from datetime import datetime
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── הגדרות ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("SCANNER_BOT_TOKEN", "8636475923:AAEgZQXIEaTjoVqXDFWID6BW2ZLdgqUyO0U")
CHAT_ID        = os.getenv("SCANNER_CHAT_ID", "8363071027")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# מטבעות לדלג עליהם (גדולים מדי)
SKIP_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "MATICUSDT",
    "AVAXUSDT", "LTCUSDT", "LINKUSDT", "DOTUSDT"
}

MIN_VOLUME_USDT   = 1_000_000   # נפח מינימום 1M USDT
MIN_GAIN_PCT      = 8.0         # עלייה מינימום 8%
TOP_N             = 20          # כמה מטבעות לסרוק
MIN_CONFIDENCE    = 80          # ביטחון מינימום לשליחה
SCAN_INTERVAL_HRS = 4           # כל כמה שעות לסרוק


# ── שליפת Top Gainers מBybit ────────────────────────────
def get_top_gainers() -> list:
    """מושך את הירוקים הגדולים מBybit Futures"""
    try:
        url = "https://api.bybit.com/v5/market/tickers"
        resp = requests.get(url, params={"category": "linear"}, timeout=10)
        data = resp.json()

        coins = []
        for item in data["result"]["list"]:
            symbol = item["symbol"]

            # דלג על מטבעות גדולים ועל לא USDT
            if not symbol.endswith("USDT"):
                continue
            if symbol in SKIP_SYMBOLS:
                continue

            try:
                change_pct = float(item.get("price24hPcnt", 0)) * 100
                volume     = float(item.get("turnover24h", 0))
                price      = float(item.get("lastPrice", 0))
            except (ValueError, TypeError):
                continue

            if change_pct < MIN_GAIN_PCT:
                continue
            if volume < MIN_VOLUME_USDT:
                continue
            if price <= 0:
                continue

            coins.append({
                "symbol":     symbol,
                "price":      price,
                "change_pct": round(change_pct, 2),
                "volume":     volume,
            })

        # מדרג לפי % עלייה
        coins.sort(key=lambda x: x["change_pct"], reverse=True)
        return coins[:TOP_N]

    except Exception as e:
        print(f"שגיאה בשליפת נתונים: {e}")
        return []


# ── שליפת נרות לניתוח ───────────────────────────────────
def get_candles(symbol: str, interval: str = "60", limit: int = 50) -> list:
    """מושך נרות מBybit"""
    try:
        url  = "https://api.bybit.com/v5/market/kline"
        resp = requests.get(url, params={
            "category": "linear",
            "symbol":   symbol,
            "interval": interval,
            "limit":    limit,
        }, timeout=10)
        data = resp.json()
        return data["result"]["list"]
    except Exception:
        return []


# ── ניתוח Claude ─────────────────────────────────────────
def analyze_with_claude(symbol: str, price: float, change_pct: float, candles_1h: list, candles_4h: list) -> dict:
    """שולח נתונים לClaude לניתוח"""

    def candles_summary(candles: list, n: int = 20) -> str:
        lines = []
        for c in candles[:n]:
            # [time, open, high, low, close, volume]
            try:
                lines.append(f"O:{c[1]} H:{c[2]} L:{c[3]} C:{c[4]} V:{c[5]}")
            except Exception:
                pass
        return "\n".join(lines)

    prompt = f"""אתה טריידר מקצועי המשתמש בשיטות ICT/SMC, Wyckoff, EMA וVolume Analysis.

מטבע: {symbol}
מחיר נוכחי: ${price}
עלייה 24H: +{change_pct}%

נרות 1H (אחרונים):
{candles_summary(candles_1h)}

נרות 4H (אחרונים):
{candles_summary(candles_4h)}

בצע ניתוח מהיר וענה בפורמט JSON בלבד:
{{
  "direction": "LONG" או "SHORT" או "SKIP",
  "confidence": מספר 0-100,
  "entry": מחיר כניסה,
  "sl": מחיר סטופ לוס,
  "tp": מחיר טייק פרופיט,
  "reason": "סיבה קצרה בעברית (מקסימום 20 מילים)"
}}

אם אין הזדמנות ברורה, החזר direction: "SKIP".
החזר JSON בלבד, ללא טקסט נוסף."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        text = resp.json()["content"][0]["text"].strip()
        # נקה backticks אם יש
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"שגיאת Claude ל-{symbol}: {e}")
        return {"direction": "SKIP", "confidence": 0}


# ── פונקציית סריקה ראשית ─────────────────────────────────
async def run_scan(bot: Bot):
    """מריץ סריקה מלאה ושולח תוצאות"""
    now = datetime.now().strftime("%d/%m %H:%M")
    print(f"[{now}] מתחיל סריקה...")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"🔍 *סריקת שוק מתחילה...*\n{now}",
        parse_mode="Markdown"
    )

    gainers = get_top_gainers()
    if not gainers:
        await bot.send_message(chat_id=CHAT_ID, text="⚠️ לא נמצאו מטבעות מתאימים כרגע.")
        return

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"📊 נמצאו *{len(gainers)}* מטבעות ירוקים — מנתח...",
        parse_mode="Markdown"
    )

    results = []
    for coin in gainers:
        symbol     = coin["symbol"]
        candles_1h = get_candles(symbol, "60",  50)
        candles_4h = get_candles(symbol, "240", 30)

        analysis = analyze_with_claude(
            symbol,
            coin["price"],
            coin["change_pct"],
            candles_1h,
            candles_4h,
        )

        confidence = analysis.get("confidence", 0)
        direction  = analysis.get("direction", "SKIP")

        if direction != "SKIP" and confidence >= MIN_CONFIDENCE:
            results.append({**coin, **analysis})

        await asyncio.sleep(1)  # מניעת rate limit

    # שליחת תוצאות
    if not results:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=f"😐 *סריקה הושלמה*\nלא נמצאו הזדמנויות עם {MIN_CONFIDENCE}%+ ביטחון כרגע.\nהשוק עדיין חם — אסרוק שוב בעוד {SCAN_INTERVAL_HRS} שעות.",
            parse_mode="Markdown"
        )
        return

    # כותרת
    header = f"🔥 *תוצאות סריקה — {now}*\n{len(results)} הזדמנויות נמצאו!\n{'─'*25}"
    await bot.send_message(chat_id=CHAT_ID, text=header, parse_mode="Markdown")

    # כל הזדמנות בהודעה נפרדת
    for i, r in enumerate(results, 1):
        direction_emoji = "📈 LONG" if r["direction"] == "LONG" else "📉 SHORT"
        msg = (
            f"*{i}. {r['symbol']}*\n"
            f"כיוון: {direction_emoji}\n"
            f"עלייה 24H: +{r['change_pct']}%\n"
            f"מחיר: ${r['price']}\n"
            f"כניסה: `${r.get('entry', r['price'])}`\n"
            f"SL: `${r.get('sl', '—')}`\n"
            f"TP: `${r.get('tp', '—')}`\n"
            f"ביטחון: *{r['confidence']}%*\n"
            f"📝 {r.get('reason', '')}"
        )
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        await asyncio.sleep(0.3)

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"✅ סריקה הושלמה | הסריקה הבאה בעוד {SCAN_INTERVAL_HRS} שעות",
    )


# ── פקודות Telegram ──────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Djmere Scanner Bot*\n\n"
        "פקודות זמינות:\n"
        "/scan — סריקה מיידית\n"
        "/top — רשימת הירוקים כרגע (ללא ניתוח)\n"
        "/status — סטטוס הבוט\n",
        parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ מתחיל סריקה, זה יקח כ-2 דקות...")
    await run_scan(context.bot)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """מציג רשימת ירוקים בלי ניתוח"""
    gainers = get_top_gainers()
    if not gainers:
        await update.message.reply_text("לא נמצאו מטבעות כרגע.")
        return
    lines = [f"🔥 *Top Gainers — Bybit Futures*\n"]
    for i, c in enumerate(gainers[:15], 1):
        lines.append(f"{i}. {c['symbol']} +{c['change_pct']}% | ${c['price']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    await update.message.reply_text(
        f"✅ *הבוט פעיל*\n"
        f"⏰ {now}\n"
        f"🔄 סריקה אוטומטית כל {SCAN_INTERVAL_HRS} שעות\n"
        f"📊 ביטחון מינימום: {MIN_CONFIDENCE}%\n"
        f"📈 עלייה מינימום: {MIN_GAIN_PCT}%",
        parse_mode="Markdown"
    )


# ── סריקה אוטומטית כל X שעות ────────────────────────────
async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    await run_scan(context.bot)


# ── הרצה ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("top",    cmd_top))
    app.add_handler(CommandHandler("status", cmd_status))

    # סריקה אוטומטית
    app.job_queue.run_repeating(
        auto_scan_job,
        interval=SCAN_INTERVAL_HRS * 3600,
        first=60,  # סריקה ראשונה אחרי דקה
    )

    print("🚀 Djmere Scanner Bot מופעל!")
    app.run_polling()


if __name__ == "__main__":
    main()
