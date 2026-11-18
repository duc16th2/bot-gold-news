#!/usr/bin/env python3
"""
Gold (XAU/USD) daily analysis bot -> Telegram group.

Chạy:
    python bot.py --dry     # in report ra màn hình, không gửi
    python bot.py           # gửi vào Telegram

Biến môi trường (hoặc file .env):
    TELEGRAM_BOT_TOKEN   token lấy từ @BotFather
    TELEGRAM_CHAT_ID     id group (số âm, vd -1001234567890)
"""
import html
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from news import gather, render_news

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
GOLD_TICKERS = ["GC=F", "XAUUSD=X"]  # futures COMEX trước, spot làm dự phòng
DXY_TICKER = "DX-Y.NYB"
US10Y_TICKER = "^TNX"


# ----------------------------- DATA -----------------------------
def fetch(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df.dropna()


def fetch_gold(period: str, interval: str):
    for t in GOLD_TICKERS:
        try:
            df = fetch(t, period, interval)
            if len(df) > 60:
                return df, t
        except Exception as e:
            print(f"[warn] {t} lỗi: {e}", file=sys.stderr)
    raise RuntimeError("Không lấy được dữ liệu vàng từ Yahoo Finance")


# --------------------------- INDICATORS --------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def macd_hist(s):
    line = ema(s, 12) - ema(s, 26)
    return line - ema(line, 9)


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ----------------------------- SCORING ---------------------------
def tech_score(df: pd.DataFrame, label: str):
    """Trả về (điểm -5..+5, danh sách lý do)."""
    c = df["close"]
    last = c.iloc[-1]
    e20, e50, e200 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
    r = rsi(c).iloc[-1]
    h = macd_hist(c).iloc[-1]

    score, why = 0.0, []
    score += 1 if last > e50 else -1
    why.append(f"{label}: giá {'trên' if last > e50 else 'dưới'} EMA50")
    score += 1 if e20 > e50 else -1
    why.append(f"{label}: EMA20 {'>' if e20 > e50 else '<'} EMA50")
    score += 1 if last > e200 else -1
    why.append(f"{label}: giá {'trên' if last > e200 else 'dưới'} EMA200")
    score += 1 if h > 0 else -1
    why.append(f"{label}: MACD hist {'dương' if h > 0 else 'âm'}")
    if r < 30:
        score += 1
        why.append(f"{label}: RSI {r:.0f} (quá bán)")
    elif r > 70:
        score -= 1
        why.append(f"{label}: RSI {r:.0f} (quá mua)")
    else:
        why.append(f"{label}: RSI {r:.0f} (trung tính)")
    return score, why, float(r)


def pct_change(s: pd.Series, days=5) -> float:
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1) * 100


def macro_score():
    """DXY và lợi suất US10Y thường ngược chiều với vàng."""
    score, why = 0, []
    try:
        dxy = fetch(DXY_TICKER, "1mo", "1d")["close"]
        ch = pct_change(dxy)
        if ch > 0.5:
            score -= 1
        elif ch < -0.5:
            score += 1
        why.append(f"DXY {dxy.iloc[-1]:.2f} ({ch:+.2f}% / 5 phiên)")
    except Exception:
        why.append("DXY: không có dữ liệu")
    try:
        y = fetch(US10Y_TICKER, "1mo", "1d")["close"]
        diff = float(y.iloc[-1] - y.iloc[-6])
        if diff > 0.15:
            score -= 1
        elif diff < -0.15:
            score += 1
        why.append(f"US10Y {y.iloc[-1]:.2f}% ({diff:+.2f}đ / 5 phiên)")
    except Exception:
        why.append("US10Y: không có dữ liệu")
    return score, why


# ----------------------------- REPORT ----------------------------
def build_report() -> str:
    daily, ticker = fetch_gold("2y", "1d")
    hourly, _ = fetch_gold("60d", "1h")

    s_d, why_d, rsi_d = tech_score(daily, "D1")
    s_h, why_h, _ = tech_score(hourly, "H1")
    s_m, why_m = macro_score()
    ctx = (f"Điểm kỹ thuật D1 {s_d:+.0f}/5, H1 {s_h:+.0f}/5, vĩ mô {s_m:+d}/2, "
           f"RSI D1 {rsi_d:.0f}")
    news = gather(ctx)
    total = s_d + s_h + s_m + news["bias"]  # -14..+14

    if total >= 4:
        action, icon = "BUY", "🟢"
    elif total <= -4:
        action, icon = "SELL", "🔴"
    else:
        action, icon = "CHỜ (không vào lệnh)", "⚪"
    strength = "mạnh" if abs(total) >= 8 else "vừa" if abs(total) >= 4 else "yếu"

    price = float(daily["close"].iloc[-1])
    prev = float(daily["close"].iloc[-2])
    chg = (price / prev - 1) * 100
    a = float(atr(daily).iloc[-1])

    # pivot từ phiên liền trước
    p = daily.iloc[-2]
    pivot = (p["high"] + p["low"] + p["close"]) / 3
    r1, s1 = 2 * pivot - p["low"], 2 * pivot - p["high"]

    now = datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"🥇 <b>BÁO CÁO VÀNG XAU/USD</b>",
        f"🕗 {now} (GMT+7)",
        "",
        f"💰 Giá: <b>{price:,.2f}</b> ({chg:+.2f}% so với phiên trước)",
        f"📏 ATR(14, D1): {a:,.1f}",
        f"🎯 Pivot {pivot:,.1f} | R1 {r1:,.1f} | S1 {s1:,.1f}",
        "",
        "<b>Kỹ thuật</b>",
        *[f"• {html.escape(x)}" for x in why_d],
        *[f"• {html.escape(x)}" for x in why_h[:2]],
        "",
        "<b>Vĩ mô</b>",
        *[f"• {html.escape(x)}" for x in why_m],
        "",
        *render_news(news),
        f"{icon} <b>Tín hiệu: {action}</b> (điểm {total:+.1f}/14, độ mạnh {strength})",
    ]

    if action.startswith(("BUY", "SELL")):
        sign = 1 if action == "BUY" else -1
        sl = price - sign * 1.5 * a
        tp1 = price + sign * 1.5 * a
        tp2 = price + sign * 3.0 * a
        lines += [
            f"▫️ Entry tham khảo: {price:,.1f}",
            f"▫️ SL: {sl:,.1f}",
            f"▫️ TP1: {tp1:,.1f} | TP2: {tp2:,.1f}",
            "▫️ Rủi ro mỗi lệnh khuyến nghị ≤ 1% tài khoản",
        ]
    if news["events"]:
        lines.append("⚠️ Có tin USD mạnh trong 24h tới: giá có thể quét SL rất nhanh, "
                     "cân nhắc giảm khối lượng hoặc đứng ngoài trước giờ ra tin.")
    lines += [
        "",
        f"<i>Nguồn dữ liệu: Yahoo Finance ({ticker}), RSS tin tức, ForexFactory. "
        "Đây là tín hiệu tự động theo chỉ báo, KHÔNG phải lời khuyên đầu tư. "
        "Forex/CFD có đòn bẩy, rủi ro mất vốn cao.</i>",
    ]
    return "\n".join(lines)


# ---------------------------- TELEGRAM ---------------------------
def send_telegram(text: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    # Telegram giới hạn 4096 ký tự/tin -> tách theo dòng
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for part in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()


def main():
    msg = build_report()
    if "--dry" in sys.argv:
        print(msg)
    else:
        send_telegram(msg)
        print("Đã gửi báo cáo.")


if __name__ == "__main__":
    main()
