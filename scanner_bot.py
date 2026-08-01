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
from zoneinfo import ZoneInfo
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
TOP_N              = 10
MIN_CONFIDENCE     = 70
SCAN_INTERVAL_SECS = 3600
ISRAEL_TZ           = ZoneInfo("Asia/Jerusalem")
AUTO_SCAN_START_HOUR = 10   # auto-scans run starting 10:00 Israel time
AUTO_SCAN_END_HOUR   = 22   # auto-scans stop at 22:00 Israel time (manual /scan always works)

# ---------------------------------------------------------------------------
# KISS strategy confirmation filter (Range -> Sweep -> Change of State)
# Runtime ON/OFF toggle. When ON, a trade idea is only kept if the KISS
# direction on the 4H candles agrees with the existing strategy's direction.
#
# This uses real fractal pivot detection (swing highs/lows) rather than a
# fixed lookback window, and a proper CHoCH definition: after a liquidity
# sweep of a swing point, structure only "changes" when price closes beyond
# the most recent OPPOSITE swing point that formed during that move - not
# merely by returning inside some arbitrary range.
# ---------------------------------------------------------------------------
KISS_FILTER_ENABLED = True   # default state; toggle live with /kiss on|off
KISS_PIVOT_LEFT      = 2     # candles required on each side to confirm a pivot
KISS_PIVOT_RIGHT      = 2
KISS_SWEEP_WINDOW     = 3    # how many of the most recent candles can count as "the sweep"


def find_pivots(highs: list, lows: list, left: int = 2, right: int = 2) -> tuple:
    """
    Fractal-style swing point detection.
    A pivot high at i: highs[i] is strictly the max within [i-left, i+right].
    A pivot low  at i: lows[i]  is strictly the min within [i-left, i+right].
    Returns (pivot_highs, pivot_lows), each a list of (index, price) tuples
    in chronological order.
    """
    pivot_highs, pivot_lows = [], []
    n = len(highs)
    for i in range(left, n - right):
        h_window = highs[i - left:i + right + 1]
        l_window = lows[i - left:i + right + 1]
        if highs[i] == max(h_window) and h_window.count(highs[i]) == 1:
            pivot_highs.append((i, highs[i]))
        if lows[i] == min(l_window) and l_window.count(lows[i]) == 1:
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows


def detect_kiss_signal(candles_4h: list) -> dict:
    """
    Real KISS / SMC-style check on 4H candles:
      1. Range   - defined by actual swing highs/lows (fractal pivots)
      2. Sweep   - a recent candle wicks beyond the last swing high/low
                   (liquidity grab beyond a real structural point)
      3. CHoCH   - price then CLOSES beyond the most recent OPPOSITE swing
                   point that formed during that move, confirming a genuine
                   structural shift (not just "back inside a range")

    Returns: {"direction": "LONG" | "SHORT" | None, "reason": str}
    """
    min_candles = KISS_PIVOT_LEFT + KISS_PIVOT_RIGHT + 10
    if not candles_4h or len(candles_4h) < min_candles:
        return {"direction": None, "reason": "אין מספיק נרות 4H ל-KISS"}

    try:
        # Bybit kline returns newest-first; sort ascending (old -> new)
        sorted_candles = sorted(candles_4h, key=lambda c: int(c[0]))
        highs  = [float(c[2]) for c in sorted_candles]
        lows   = [float(c[3]) for c in sorted_candles]
        closes = [float(c[4]) for c in sorted_candles]
    except Exception as e:
        return {"direction": None, "reason": f"שגיאת פרסור נרות ({e})"}

    pivot_highs, pivot_lows = find_pivots(highs, lows, KISS_PIVOT_LEFT, KISS_PIVOT_RIGHT)
    if not pivot_highs or not pivot_lows:
        return {"direction": None, "reason": "לא נמצאו מספיק פיבוטים ל-KISS"}

    last_idx = len(highs) - 1
    last_close = closes[-1]
    recent_highs = highs[-KISS_SWEEP_WINDOW:]
    recent_lows  = lows[-KISS_SWEEP_WINDOW:]

    # --- Bearish: sweep above last swing high, CHoCH = close below the swing low that formed after it ---
    swing_high_idx, swing_high_price = pivot_highs[-1]
    if swing_high_idx <= last_idx - KISS_SWEEP_WINDOW:
        swept_high = any(h > swing_high_price for h in recent_highs)
        structure_lows = [p for p in pivot_lows if swing_high_idx < p[0] <= last_idx - 1]
        if swept_high and structure_lows:
            structure_low_price = structure_lows[-1][1]
            if last_close < structure_low_price:
                return {
                    "direction": "SHORT",
                    "reason": (f"Sweep מעל שיא מבנה {swing_high_price:.6g} + "
                               f"CHoCH: שבירת שפל מבנה {structure_low_price:.6g}")
                }

    # --- Bullish: sweep below last swing low, CHoCH = close above the swing high that formed after it ---
    swing_low_idx, swing_low_price = pivot_lows[-1]
    if swing_low_idx <= last_idx - KISS_SWEEP_WINDOW:
        swept_low = any(l < swing_low_price for l in recent_lows)
        structure_highs = [p for p in pivot_highs if swing_low_idx < p[0] <= last_idx - 1]
        if swept_low and structure_highs:
            structure_high_price = structure_highs[-1][1]
            if last_close > structure_high_price:
                return {
                    "direction": "LONG",
                    "reason": (f"Sweep מתחת שפל מבנה {swing_low_price:.6g} + "
                               f"CHoCH: שבירת שיא מבנה {structure_high_price:.6g}")
                }

    return {"direction": None, "reason": "אין Sweep+CHoCH מאושר על מבנה 4H"}


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
        coins.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
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
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DjmereBot/1.0)"}
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
        f"אתה טריידר מקצועי שמנתח פעולת מחיר (Price Action). נתח את {symbol} "
        f"מחיר נוכחי {price}$ עלייה של {change_pct}% ב-24 שעות האחרונות.\n\n"
        f"נרות 1H (מהישן לחדש):\n{fmt(candles_1h)}\n\n"
        f"נרות 4H (מהישן לחדש):\n{fmt(candles_4h)}\n\n"
        "בצע ניתוח לפי חמשת הקריטריונים הבאים לפני שאתה קובע כיוון:\n"
        "1. Swing Highs/Lows: זהה שיאים ושפלים משמעותיים בנתונים - אלו אזורי הנזילות.\n"
        "2. Volume: בדוק אם התנועה האחרונה מלווה בנפח מסחר גבוה/חריג (עמודת V), "
        "או שהיא על נפח נמוך וחשודה כחלשה.\n"
        "3. Price Action: קבע אם המחיר עשה Breakout אמיתי מעל/מתחת לרמת מפתח, "
        "או Reject (דחייה עם פתיל ארוך) בשיא/שפל - אלו מובילים למסקנות הפוכות.\n"
        "4. ATR/Volatility: העריך את עומק התנודתיות של הנרות האחרונים ביחס לממוצע, "
        "וסמן אם מדובר ב'רעש' חסר כיוון או ב-Liquidity Sweep אמיתי.\n"
        "5. Pivot Detection: זהה pivot high/low קרובים שיכולים לשמש כאזורי כניסה/יציאה.\n\n"
        "חשוב: בדוק מיצוי לשני הכיוונים באותה רמת קפדנות:\n"
        "- אם מדובר בעלייה חדה שכבר האטה, עם פתילי דחייה בשיא ונפח יורד - "
        "זה מחליש LONG וכיוון להיות SHORT.\n"
        "- אם מדובר בירידה חדה שכבר האטה, עם פתילי דחייה בשפל ונפח יורד - "
        "זה מחליש SHORT וכיוון להיות LONG.\n"
        "אל תניח כברירת מחדל שתנועה חדה = SHORT. בחן את שני הכיוונים באופן שווה "
        "לפני קביעת מסקנה, ותן CONFIDENCE נמוך או SKIP אם התמונה לא ברורה.\n\n"
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
    kiss_status = "פעיל ✅" if KISS_FILTER_ENABLED else "כבוי ⛔"
    await bot.send_message(chat_id=CHAT_ID,
        text=f"🔍 *סריקת שוק מתחילה...*\n{now}\nפילטר KISS: {kiss_status}", parse_mode="Markdown")
    gainers = get_top_gainers()
    if not gainers:
        await bot.send_message(chat_id=CHAT_ID, text="⚠️ לא נמצאו מטבעות.")
        return
    await bot.send_message(chat_id=CHAT_ID,
        text=f"📊 נמצאו *{len(gainers)}* מטבעות — מנתח...", parse_mode="Markdown")
    results = []
    for coin in gainers:
        symbol = coin["symbol"]
        candles_1h = get_candles(symbol, "60")
        candles_4h = get_candles(symbol, "240", limit=50)  # more history -> more reliable pivots for KISS
        analysis = analyze_with_claude(symbol, coin["price"], coin["change_pct"],
                                        candles_1h, candles_4h)

        if analysis.get("direction") == "SKIP" or analysis.get("confidence", 0) < MIN_CONFIDENCE:
            await asyncio.sleep(1)
            continue

        # --- KISS confirmation filter (optional) ---
        if KISS_FILTER_ENABLED:
            kiss = detect_kiss_signal(candles_4h)
            if kiss["direction"] != analysis["direction"]:
                logger.info(f"{symbol}: נדחה ע\"י KISS ({kiss['reason']})")
                await asyncio.sleep(1)
                continue
            analysis["kiss_reason"] = kiss["reason"]

        results.append({**coin, **analysis})
        await asyncio.sleep(1)

    if not results:
        reason = f"עם {MIN_CONFIDENCE}%+" + (" ואישור KISS" if KISS_FILTER_ENABLED else "")
        await bot.send_message(chat_id=CHAT_ID, text=f"😐 לא נמצאו הזדמנויות {reason}")
        return
    await bot.send_message(chat_id=CHAT_ID,
        text=f"🔥 *תוצאות — {now}*\n{len(results)} הזדמנויות!", parse_mode="Markdown")
    for i, r in enumerate(results, 1):
        emoji = "📈 LONG" if r["direction"] == "LONG" else "📉 SHORT"
        msg = (f"*{i}. {r['symbol']}* {emoji}\n"
               f"עלייה: +{r['change_pct']}% | ביטחון: *{r['confidence']}%*\n"
               f"כניסה: `${r.get('entry','?')}` | SL: `${r.get('sl','?')}` | TP: `${r.get('tp','?')}`\n"
               f"📝 {r.get('reason','')}")
        if r.get("kiss_reason"):
            msg += f"\n✅ KISS: {r['kiss_reason']}"
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        await asyncio.sleep(0.5)
    await bot.send_message(chat_id=CHAT_ID, text="✅ סריקה הושלמה | הבאה בעוד שעה")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Djmere Scanner Bot*\n\n"
        "/scan — סריקה\n/top — ירוקים\n/status — סטטוס\n"
        "/kiss — הצג מצב פילטר KISS\n/kiss on — הפעל פילטר KISS\n/kiss off — כבה פילטר KISS",
        parse_mode="Markdown")

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
    kiss_status = "פעיל ✅" if KISS_FILTER_ENABLED else "כבוי ⛔"
    await update.message.reply_text(
        f"✅ פעיל | {datetime.now().strftime('%d/%m %H:%M')} | סריקה כל שעה | {MIN_CONFIDENCE}% מינימום\n"
        f"פילטר KISS: {kiss_status}",
        parse_mode="Markdown")

async def cmd_kiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global KISS_FILTER_ENABLED
    args = context.args
    if not args:
        status = "פעיל ✅" if KISS_FILTER_ENABLED else "כבוי ⛔"
        await update.message.reply_text(f"פילטר KISS כרגע: {status}\nלשינוי: /kiss on או /kiss off")
        return
    choice = args[0].lower()
    if choice == "on":
        KISS_FILTER_ENABLED = True
        await update.message.reply_text("✅ פילטר KISS הופעל")
    elif choice == "off":
        KISS_FILTER_ENABLED = False
        await update.message.reply_text("⛔ פילטר KISS כובה")
    else:
        await update.message.reply_text("שימוש: /kiss on או /kiss off")

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    now_il = datetime.now(ISRAEL_TZ)
    if not (AUTO_SCAN_START_HOUR <= now_il.hour < AUTO_SCAN_END_HOUR):
        logger.info(f"Skipping auto-scan (outside active hours) — {now_il.strftime('%H:%M')} IL time")
        return
    await run_scan(context.bot)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("scan",   cmd_scan))
    app.add_handler(CommandHandler("top",    cmd_top))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kiss",   cmd_kiss))
    app.job_queue.run_repeating(auto_scan_job, interval=SCAN_INTERVAL_SECS, first=60)
    logger.info("🚀 Scanner Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
