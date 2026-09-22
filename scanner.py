#!/usr/bin/env python3
"""
S&P 500 teknik tarayıcı -> Telegram

Gün içi (ABD borsası açıkken, 30 dk'da bir):
  - %5+ sert hareket
  - Yüksek hacimle 52 haftalık zirve / dip kırılımı
  Her hisse için günde en fazla 1 bildirim.

Gün sonu (kapanıştan sonra, tek özet mesaj, sessiz bildirim):
  - Golden / death cross, RSI aşırı alım-satım, hacimli 52H zirve kapanışı,
    olağandışı hacim, son 14 günde yönetici alımı (insider bot'tan)
  Sinyaller puanlanır, en güçlü 10 tanesi gönderilir.

Kullanım: python scanner.py [auto|test|intraday|eod]
"""
import html
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ------------------------------------------------------------------ AYARLAR
SETTINGS = {
    "move_pct": 5.0,              # gün içi sert hareket eşiği (%)
    "breakout_volume_x": 1.5,     # gün içi zirve/dip kırılımında gereken hacim çarpanı
    "eod_breakout_volume_x": 2.0, # gün sonu zirve kapanışında gereken hacim çarpanı
    "volume_spike_x": 3.0,        # olağandışı hacim eşiği
    "rsi_low": 30,
    "rsi_high": 70,
    "min_score": 2,               # gün sonu özete girmek için gereken puan
    "eod_max": 10,                # gün sonu özetteki en fazla hisse
    "intraday_max": 15,           # gün içi mesajdaki en fazla satır
}
# ---------------------------------------------------------------------------

BASE = Path(__file__).parent
STATE_PATH = BASE / "scan_state.json"
INSIDER_STATE = BASE / "state.json"
NY = ZoneInfo("America/New_York")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


# ---------------------------------------------------------------- yardımcılar
def notify(text, silent=False):
    if not (TG_TOKEN and TG_CHAT):
        print("---- [Telegram ayarlı değil] ----\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true",
              "disable_notification": "true" if silent else "false"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Telegram hatası:", r.text, file=sys.stderr)


def esc(s):
    return html.escape(str(s or ""))


def link(t):
    return f'<a href="https://finviz.com/quote.ashx?t={t}">{esc(t)}</a>'


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def get_sp500(state):
    """S&P 500 listesini haftada bir günceller; alınamazsa önbelleği kullanır."""
    cache = state.get("sp500")
    if cache and date.fromisoformat(cache["updated"]) > date.today() - timedelta(days=7):
        return cache["list"]

    rows = None
    try:
        r = requests.get(SP500_CSV, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        rows = list(zip(df["Symbol"], df["Security"]))
    except Exception as ex:
        print("S&P 500 CSV alınamadı:", ex, file=sys.stderr)
    if not rows or len(rows) < 400:
        try:
            r = requests.get(SP500_WIKI, headers={"User-Agent": "Mozilla/5.0 (insiderbot)"}, timeout=30)
            df = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
            rows = list(zip(df["Symbol"], df["Security"]))
        except Exception as ex:
            print("S&P 500 Wikipedia alınamadı:", ex, file=sys.stderr)

    if rows and len(rows) >= 400:
        lst = [[str(s).strip().replace(".", "-"), str(n).strip()] for s, n in rows]
        state["sp500"] = {"updated": date.today().isoformat(), "list": lst}
        return lst
    if cache:
        return cache["list"]
    raise RuntimeError("S&P 500 listesi alınamadı")


def download(tickers, period):
    """Yahoo Finance'ten günlük veriyi 100'lük paketler halinde çeker."""
    data = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                                 group_by="column", threads=True, progress=False)
                if df is not None and not df.empty:
                    break
            except Exception as ex:
                print("Veri hatası:", ex, file=sys.stderr)
            time.sleep(10 * (attempt + 1))
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df.xs(t, axis=1, level=1)[["High", "Low", "Close", "Volume"]].dropna()
            except (KeyError, ValueError):
                continue
            if len(sub) > 30:
                data[t] = sub
        time.sleep(2)
    print(f"{len(data)}/{len(tickers)} hissenin verisi alındı")
    if len(data) < 100:
        raise RuntimeError("Yeterli fiyat verisi alınamadı (Yahoo erişim sorunu olabilir)")
    return data


def last_date(d):
    return pd.Timestamp(d.index[-1]).date()


def rsi(close, n=14):
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def insider_tickers():
    """insider bot'un son 14 günde kaydettiği alımlar."""
    try:
        s = json.loads(INSIDER_STATE.read_text(encoding="utf-8"))
        return {b["ticker"].replace(".", "-") for b in s.get("recent_buys", [])}
    except Exception:
        return set()


# ---------------------------------------------------------------- gün içi
def run_intraday(state, sp, forced):
    names = dict(sp)
    now = datetime.now(NY)
    today = now.date()
    data = download(list(names), "1y")
    if not forced:
        data = {t: d for t, d in data.items() if last_date(d) == today}
        if len(data) < 100:
            print("Bugün için veri yok (tatil olabilir)")
            return

    session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now - session_start).total_seconds() / 23400
    elapsed = 1.0 if (forced and not 0 < elapsed < 1) else max(0.1, min(1.0, elapsed))

    day = state.get("intraday", {})
    if day.get("date") != today.isoformat():
        day = {"date": today.isoformat(), "tickers": []}
    done = set(day["tickers"])

    hits = []  # (öncelik, satır, ticker)
    for t, d in data.items():
        if t in done or len(d) < 60:
            continue
        price, prev = d["Close"].iloc[-1], d["Close"].iloc[-2]
        chg = (price / prev - 1) * 100
        avg_vol = d["Volume"].iloc[-21:-1].mean()
        pace = d["Volume"].iloc[-1] / (avg_vol * elapsed) if avg_vol > 0 else 0
        hi52, lo52 = d["High"].iloc[:-1].max(), d["Low"].iloc[:-1].min()
        name = esc(names.get(t, ""))

        if price > hi52 and pace >= SETTINGS["breakout_volume_x"]:
            hits.append((2, f"🚀 {link(t)} {name}: 52 haftalık zirveyi kırdı, hacim {pace:.1f}x ({chg:+.1f}%)", t))
        elif price < lo52 and pace >= SETTINGS["breakout_volume_x"]:
            hits.append((2, f"⚠️ {link(t)} {name}: 52 haftalık dibi kırdı, hacim {pace:.1f}x ({chg:+.1f}%)", t))
        elif abs(chg) >= SETTINGS["move_pct"]:
            icon = "📈" if chg > 0 else "📉"
            hits.append((1 + abs(chg) / 100, f"{icon} {link(t)} {name}: {chg:+.1f}% (${price:,.2f})", t))

    hits.sort(key=lambda h: h[0], reverse=True)
    day["tickers"] = sorted(done | {h[2] for h in hits})
    state["intraday"] = day
    print(f"Gün içi: {len(hits)} yeni hareket")
    if not hits:
        return
    shown = hits[:SETTINGS["intraday_max"]]
    extra = len(hits) - len(shown)
    text = f"⚡ <b>Gün içi hareketler</b> (NY {now:%H:%M})\n" + "\n".join(h[1] for h in shown)
    if extra:
        text += f"\n… ve {extra} hisse daha"
    notify(text)


# ---------------------------------------------------------------- gün sonu
def run_eod(state, sp, forced):
    names = dict(sp)
    today = datetime.now(NY).date()
    state["eod_date"] = today.isoformat()
    data = download(list(names), "2y")
    if not forced:
        data = {t: d for t, d in data.items() if last_date(d) == today}
        if len(data) < 100:
            print("Bugün işlem günü değil, özet yok")
            return

    insiders = insider_tickers()
    S = SETTINGS
    results = []
    for t, d in data.items():
        if len(d) < 260:
            continue
        c, v = d["Close"], d["Volume"]
        s50, s200, r = c.rolling(50).mean(), c.rolling(200).mean(), rsi(c)
        chg = (c.iloc[-1] / c.iloc[-2] - 1) * 100
        avg_vol = v.iloc[-21:-1].mean()
        volx = v.iloc[-1] / avg_vol if avg_vol > 0 else 0
        sig, score = [], 0

        if s50.iloc[-2] <= s200.iloc[-2] and s50.iloc[-1] > s200.iloc[-1]:
            sig.append("🟢 Golden cross"); score += 2
        if s50.iloc[-2] >= s200.iloc[-2] and s50.iloc[-1] < s200.iloc[-1]:
            sig.append("🔴 Death cross"); score += 2
        if r.iloc[-2] >= S["rsi_low"] and r.iloc[-1] < S["rsi_low"]:
            sig.append(f"RSI aşırı satım ({r.iloc[-1]:.0f})"); score += 1
        if r.iloc[-2] <= S["rsi_high"] and r.iloc[-1] > S["rsi_high"]:
            sig.append(f"RSI aşırı alım ({r.iloc[-1]:.0f})"); score += 1
        if c.iloc[-1] > c.iloc[-253:-1].max() and volx >= S["eod_breakout_volume_x"]:
            sig.append(f"🚀 52H zirve kapanışı (hacim {volx:.1f}x)"); score += 2
        elif volx >= S["volume_spike_x"]:
            sig.append(f"Olağandışı hacim ({volx:.1f}x)"); score += 1
        if score > 0 and t in insiders:
            sig.append("👤 Son 14 günde yönetici alımı"); score += 2

        if score >= S["min_score"]:
            results.append((score, abs(chg), t, c.iloc[-1], chg, sig))

    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    print(f"Gün sonu: {len(results)} güçlü sinyal")
    if not results:
        return
    lines = []
    for i, (score, _, t, price, chg, sig) in enumerate(results[:S["eod_max"]], 1):
        lines.append(f"{i}. {link(t)} {esc(names.get(t, ''))} ${price:,.2f} ({chg:+.1f}%)\n   "
                     + " · ".join(sig))
    notify(
        f"📊 <b>Gün sonu teknik özet</b> ({last_date(next(iter(data.values())))})\n\n"
        + "\n".join(lines)
        + "\n\n<i>Sinyaller inceleme içindir, al/sat önerisi değildir.</i>",
        silent=True,
    )


# ---------------------------------------------------------------- main
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "auto"
    if mode == "test":
        notify("✅ Teknik tarayıcı bağlantısı çalışıyor.")
        return

    state = load_state()
    now = datetime.now(NY)
    weekday = now.weekday() < 5
    opens = now.replace(hour=9, minute=30, second=0, microsecond=0)
    closes = now.replace(hour=16, minute=0, second=0, microsecond=0)

    try:
        sp = get_sp500(state)
        if mode == "intraday" or (mode == "auto" and weekday and opens <= now < closes):
            run_intraday(state, sp, forced=(mode == "intraday"))
        elif mode == "eod" or (mode == "auto" and weekday and now >= closes + timedelta(minutes=10)
                               and state.get("eod_date") != now.date().isoformat()):
            run_eod(state, sp, forced=(mode == "eod"))
        else:
            print("Piyasa kapalı, yapılacak iş yok")
    finally:
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
