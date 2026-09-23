#!/usr/bin/env python3
"""
S&P 500 tarayıcı -> Telegram

Gün içi (ABD seansı, 30 dk'da bir): %5+ hareket, hacimli 52H zirve/dip kırılımı
Gün sonu (kapanış sonrası, sessiz): sade dille üç grup sinyal özeti
09:00 İstanbul (Salı-Cmt): kapanış sonrası hareketler (+ bilanço / haber notu)
16:00 İstanbul (hafta içi): piyasa özeti + açılış öncesi hareketler
Pazartesi 09:00: haftanın bilanço takvimi
Cumartesi 10:00: sinyal karnesi

Kullanım: python scanner.py [auto|test|intraday|eod|afterhours|premarket|
                              afterhours-now|premarket-now|weekly|scorecard|market]
"""
import html
import io
import json
import os
import re
import sys
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
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
    "eod_group_max": 5,           # gün sonu özetinde her gruptaki en fazla hisse
    "ext_move_pct": 3.0,          # seans dışı raporlar için hareket eşiği (%)
    "ext_min_dollar": 1_000_000,  # seans dışı işlem hacmi alt sınırı ($)
    "ext_min_bars": 2,            # hareketin en az kaç ayrı 15 dk'lık dilimde görülmesi gerektiği
    "ext_max": 8,                 # seans dışı raporda yön başına en fazla hisse
    "intraday_max": 15,           # gün içi mesajdaki en fazla satır
    "log_days": 120,              # sinyal karnesinin kaç günlük geçmişe baktığı
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
NASDAQ_EARN = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_ECON = "https://api.nasdaq.com/api/calendar/economicevents"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


# ---------------------------------------------------------------- yardımcılar
def notify(text, silent=False):
    if len(text) > 4000:  # Telegram sınırı; satır bütünlüğünü koruyarak kes
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"
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


def tr_date(d):
    return f"{d.day} {TR_MONTHS[d.month - 1]}"


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def log_signal(state, t, key, price, d):
    """Sinyal karnesi için kayıt. Aynı gün/hisse/sinyal bir kez tutulur."""
    if not price or price != price:
        return
    log = state.setdefault("signal_log", [])
    entry = {"d": d.isoformat(), "t": t, "k": key, "p": round(float(price), 4)}
    if not any(e["d"] == entry["d"] and e["t"] == t and e["k"] == key for e in log[-300:]):
        log.append(entry)


def prune_log(state):
    cutoff = (date.today() - timedelta(days=SETTINGS["log_days"])).isoformat()
    state["signal_log"] = [e for e in state.get("signal_log", []) if e["d"] >= cutoff]


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


def download(tickers, period, min_ok=100):
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
    if len(data) < min_ok:
        raise RuntimeError("Yeterli fiyat verisi alınamadı (Yahoo erişim sorunu olabilir)")
    return data


def last_date(d):
    return pd.Timestamp(d.index[-1]).date()


def rsi(close, n=14):
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def insider_state():
    try:
        return json.loads(INSIDER_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def insider_tickers():
    return {b["ticker"].replace(".", "-") for b in insider_state().get("recent_buys", [])}


# ---------------------------------------------------------------- bilanço & haber
def earnings_on(day):
    """{ticker: 'bmo' | 'amc' | '?'} ya da alınamazsa None."""
    try:
        r = requests.get(NASDAQ_EARN, params={"date": day.isoformat()}, headers=NASDAQ_HEADERS, timeout=30)
        r.raise_for_status()
        rows = (r.json().get("data") or {}).get("rows") or []
    except Exception as ex:
        print(f"Bilanço takvimi alınamadı ({day}):", ex, file=sys.stderr)
        return None
    out = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper().replace(".", "-")
        when = str(row.get("time") or "")
        if sym:
            out[sym] = "bmo" if "pre" in when else "amc" if "after" in when else "?"
    return out


def latest_news(t, hours=24):
    """Son X saatteki ilk haber başlığı; yoksa ya da alınamazsa None."""
    try:
        items = yf.Ticker(t).news or []
    except Exception:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for it in items:
        c = it.get("content") or it
        title = c.get("title")
        when = None
        ts = c.get("pubDate") or c.get("displayTime")
        try:
            if ts:
                when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            elif it.get("providerPublishTime"):
                when = datetime.fromtimestamp(int(it["providerPublishTime"]), timezone.utc)
        except (ValueError, TypeError):
            when = None
        if title and when and when >= cutoff:
            return str(title)
    return None


def move_note(t, earned):
    if t in earned:
        return "📑 Bilanço açıkladı"
    title = latest_news(t)
    if title:
        return f"📰 {esc(title[:120])}"
    return "⚠️ Bilanço ya da haber bulunamadı, veri hatası olabilir"


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

    hits = []  # (öncelik, satır, ticker, sinyal, fiyat)
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
            hits.append((2, f"🚀 {link(t)} {name}: 52 haftalık zirveyi kırdı, hacim {pace:.1f}x ({chg:+.1f}%)", t, "intra_high", price))
        elif price < lo52 and pace >= SETTINGS["breakout_volume_x"]:
            hits.append((2, f"⚠️ {link(t)} {name}: 52 haftalık dibi kırdı, hacim {pace:.1f}x ({chg:+.1f}%)", t, "intra_low", price))
        elif abs(chg) >= SETTINGS["move_pct"]:
            icon = "📈" if chg > 0 else "📉"
            hits.append((1 + abs(chg) / 100, f"{icon} {link(t)} {name}: {chg:+.1f}% (${price:,.2f})", t,
                         "intra_up" if chg > 0 else "intra_down", price))

    hits.sort(key=lambda h: h[0], reverse=True)
    day["tickers"] = sorted(done | {h[2] for h in hits})
    state["intraday"] = day
    if not forced:
        for h in hits:
            log_signal(state, h[2], h[3], h[4], today)
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
    groups = {"bull": [], "rebound": [], "bear": []}
    for t, d in data.items():
        if len(d) < 260:
            continue
        c, v = d["Close"], d["Volume"]
        s50, s200, r = c.rolling(50).mean(), c.rolling(200).mean(), rsi(c)
        chg = (c.iloc[-1] / c.iloc[-2] - 1) * 100
        avg_vol = v.iloc[-21:-1].mean()
        volx = v.iloc[-1] / avg_vol if avg_vol > 0 else 0
        notes, keys, bull, bear, oversold = [], [], 0, 0, False

        if s50.iloc[-2] <= s200.iloc[-2] and s50.iloc[-1] > s200.iloc[-1]:
            bull += 2; keys.append("golden")
            notes.append("Kısa vadeli trendi uzun vadeli trendini yukarı kesti (golden cross), yükseliş trendi güçleniyor.")
        if s50.iloc[-2] >= s200.iloc[-2] and s50.iloc[-1] < s200.iloc[-1]:
            bear += 2; keys.append("death")
            notes.append("Kısa vadeli trendi uzun vadeli trendinin altına indi (death cross), trend zayıflıyor.")

        hi, lo = c.iloc[-253:-1].max(), c.iloc[-253:-1].min()
        if c.iloc[-1] > hi and volx >= S["eod_breakout_volume_x"]:
            bull += 2; keys.append("high52")
            notes.append(f"Normalin {volx:.1f} katı hacimle yılın en yüksek seviyesinde kapandı, güçlü alım ilgisi var.")
        elif c.iloc[-1] < lo and volx >= S["eod_breakout_volume_x"]:
            bear += 2; keys.append("low52")
            notes.append(f"Normalin {volx:.1f} katı hacimle yılın en düşük seviyesinde kapandı, satış baskısı sürüyor.")
        elif volx >= S["volume_spike_x"]:
            if chg > 0:
                bull += 1; keys.append("vol_up")
                notes.append(f"Normalin {volx:.1f} katı hacimle %{chg:.1f} yükseldi, yoğun alım var.")
            else:
                bear += 1; keys.append("vol_down")
                notes.append(f"Normalin {volx:.1f} katı hacimle %{abs(chg):.1f} düştü, yoğun satış var.")

        if r.iloc[-2] >= S["rsi_low"] and r.iloc[-1] < S["rsi_low"]:
            oversold = True; keys.append("oversold")
            notes.append(f"Kısa sürede çok satıldı (RSI {r.iloc[-1]:.0f}). Tepki yükselişi gelebilir, ama önce düşüşün sebebine bak.")
        if r.iloc[-2] <= S["rsi_high"] and r.iloc[-1] > S["rsi_high"]:
            bear += 1; keys.append("overbought")
            notes.append(f"Kısa sürede çok yükseldi (RSI {r.iloc[-1]:.0f}), kâr satışları gelebilir.")

        score = bull + bear + (1 if oversold else 0)
        if score > 0 and t in insiders:
            bull += 2; score += 2
            notes.append("Son 14 günde şirket yöneticisi kendi cebinden hisse aldı.")
        if score < S["min_score"]:
            continue
        if not forced:
            for k in keys:
                log_signal(state, t, k, c.iloc[-1], last_date(d))
        group = "rebound" if oversold else ("bull" if bull >= bear else "bear")
        groups[group].append((score, abs(chg), t, c.iloc[-1], chg, notes))

    total = sum(len(g) for g in groups.values())
    print(f"Gün sonu: {total} güçlü sinyal")
    if not total:
        return
    titles = {
        "bull": "🟢 <b>Alım ilgisi / yükseliş sinyali</b>",
        "rebound": "🟡 <b>Sert düşüş sonrası tepki adayları</b>",
        "bear": "🔴 <b>Satış baskısı / düşüş sinyali</b>",
    }
    parts = [f"📊 <b>Gün sonu özeti</b> ({tr_date(last_date(next(iter(data.values()))))})"]
    for key in ("bull", "rebound", "bear"):
        items = sorted(groups[key], key=lambda x: (x[0], x[1]), reverse=True)[:S["eod_group_max"]]
        if not items:
            continue
        parts.append("\n" + titles[key])
        for _, _, t, price, chg, notes in items:
            parts.append(f"• {link(t)} {esc(names.get(t, ''))} ${price:,.2f} ({chg:+.1f}%)\n  " + " ".join(notes))
    parts.append("\n<i>Bu liste bakmaya değer adayları gösterir, al/sat önerisi değildir.</i>")
    notify("\n".join(parts), silent=True)


# ---------------------------------------------------------------- seans dışı
REG, PRE, POST = (570, 960), (240, 570), (960, 1200)  # NY saatiyle dakika aralıkları


def ext_download(tickers):
    """15 dakikalık, seans dışını da içeren son 5 günlük veri."""
    data = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="5d", interval="15m", prepost=True,
                                 auto_adjust=False, group_by="column", threads=True, progress=False)
                if df is not None and not df.empty:
                    break
            except Exception as ex:
                print("Veri hatası:", ex, file=sys.stderr)
            time.sleep(10 * (attempt + 1))
        if df is None or df.empty:
            continue
        idx = df.index if df.index.tz is not None else df.index.tz_localize(NY)
        df.index = idx.tz_convert(NY)
        for t in chunk:
            try:
                sub = df.xs(t, axis=1, level=1)[["Close", "Volume"]].dropna(subset=["Close"])
            except (KeyError, ValueError):
                continue
            if len(sub) > 10:
                data[t] = sub
        time.sleep(2)
    print(f"{len(data)}/{len(tickers)} hissenin seans dışı verisi alındı")
    if len(data) < 100:
        raise RuntimeError("Yeterli seans dışı veri alınamadı (Yahoo erişim sorunu olabilir)")
    return data


def part(sub, day, rng):
    idx = sub.index
    mins = idx.hour * 60 + idx.minute
    mask = (idx.date == day) & (mins >= rng[0]) & (mins < rng[1])
    return sub[mask]


def sessions(data):
    """Normal seans verisi olan günler (örnek hisselerden)."""
    sample = list(data.values())[:40]
    days = sorted({d for sub in sample for d in set(sub.index.date)})
    return [d for d in days if sum(len(part(sub, d, REG)) > 0 for sub in sample) >= 10]


def fmt_shares(n):
    return f"{n / 1e6:.1f} milyon" if n >= 1e6 else f"{n / 1e3:.0f} bin"


def collect(data, day_base, day_ext, rng):
    """Seans dışı hareketler. Tek bir sapkın işlemi elemek için son dilimlerin
    medyan fiyatına bakılır ve hareketin birden fazla dilimde görülmesi istenir."""
    S = SETTINGS
    rows = []
    for t, sub in data.items():
        reg, ext = part(sub, day_base, REG), part(sub, day_ext, rng)
        if reg.empty or ext.empty:
            continue
        traded = ext[ext["Volume"] > 0] if (ext["Volume"] > 0).any() else ext
        if len(traded) < S["ext_min_bars"]:
            continue
        base = reg["Close"].iloc[-1]
        last = float(traded["Close"].iloc[-3:].median())
        vol = float(ext["Volume"].sum())
        chg = (last / base - 1) * 100
        if abs(chg) < S["ext_move_pct"]:
            continue
        if vol > 0 and vol * last < S["ext_min_dollar"]:
            continue  # çok az işlemle oluşmuş, güvenilmez hareket
        rows.append((t, chg, last, vol))
    return rows


def ext_lines(rows, names, earned):
    S = SETTINGS
    up = sorted([r for r in rows if r[1] > 0], key=lambda r: r[1], reverse=True)[:S["ext_max"]]
    down = sorted([r for r in rows if r[1] < 0], key=lambda r: r[1])[:S["ext_max"]]
    parts = []
    for label, items in (("🟢 <b>Yükselenler</b>", up), ("🔴 <b>Düşenler</b>", down)):
        if not items:
            continue
        parts.append("\n" + label)
        for t, chg, price, vol in items:
            extra = f", {fmt_shares(vol)} hisse işlem gördü" if vol > 0 else ""
            parts.append(f"• {link(t)} {esc(names.get(t, ''))}: {chg:+.1f}% (${price:,.2f}){extra}\n"
                         f"  {move_note(t, earned)}")
    return parts


def run_afterhours(state, sp, forced):
    names = dict(sp)
    data = ext_download(list(names))
    days = sessions(data)
    if not days:
        print("Seans verisi bulunamadı")
        return
    day = days[-1]
    if not forced and state.get("ah_date") == day.isoformat():
        print("Bu günün kapanış sonrası raporu zaten gönderildi")
        return
    if not forced:  # elle yapılan denemeler otomatik raporu engellemesin
        state["ah_date"] = day.isoformat()
    rows = collect(data, day, day, POST)
    print(f"Kapanış sonrası: {len(rows)} hareket")
    if not rows:
        return
    earn = earnings_on(day) or {}
    earned = {t for t, w in earn.items() if w in ("amc", "?")}
    if not forced:
        for t, chg, price, _ in rows:
            log_signal(state, t, "ah_up" if chg > 0 else "ah_down", price, day)
    notify("\n".join(
        [f"🌙 <b>Kapanış sonrası hareketler</b> ({tr_date(day)} akşamı, kapanışa göre)"]
        + ext_lines(rows, names, earned)
    ))


# ---------------------------------------------------------------- piyasa özeti
MARKET = [("ES=F", "S&P 500 vadeli"), ("NQ=F", "Nasdaq vadeli"), ("^VIX", "VIX"),
          ("^TNX", "10Y"), ("DX-Y.NYB", "Dolar endeksi"), ("CL=F", "Petrol (WTI)"),
          ("GC=F", "Altın"), ("BTC-USD", "Bitcoin")]

KEY_EVENTS = [("core pce", "Çekirdek PCE enflasyonu"), ("pce", "PCE enflasyonu"),
              ("core cpi", "Çekirdek enflasyon (TÜFE)"), ("cpi", "Enflasyon (TÜFE)"),
              ("ppi", "Üretici fiyatları (ÜFE)"), ("nonfarm", "Tarım dışı istihdam"),
              ("unemployment rate", "İşsizlik oranı"), ("jobless claims", "Haftalık işsizlik başvuruları"),
              ("interest rate decision", "Fed faiz kararı"), ("fomc", "Fed (FOMC)"),
              ("powell", "Powell konuşması"), ("gdp", "Büyüme (GSYH)"),
              ("retail sales", "Perakende satışlar"), ("ism", "ISM endeksi"),
              ("consumer confidence", "Tüketici güveni"), ("michigan", "Michigan tüketici güveni"),
              ("durable goods", "Dayanıklı mal siparişleri")]


def quote(sym):
    try:
        fi = yf.Ticker(sym).fast_info
        last, prev = float(fi.last_price), float(fi.previous_close)
        if last != last or prev != prev or prev == 0:
            return None
        return last, prev
    except Exception:
        return None


def vix_mood(v):
    if v < 15:
        return "piyasa sakin"
    if v < 20:
        return "normal"
    if v < 30:
        return "gergin"
    return "korku yüksek"


def econ_events(day):
    try:
        r = requests.get(NASDAQ_ECON, params={"date": day.isoformat()}, headers=NASDAQ_HEADERS, timeout=30)
        r.raise_for_status()
        rows = (r.json().get("data") or {}).get("rows") or []
    except Exception as ex:
        print("Ekonomik takvim alınamadı:", ex, file=sys.stderr)
        return []
    out, seen = [], set()
    for row in rows:
        if "united states" not in str(row.get("country") or "").lower():
            continue
        name = str(row.get("eventName") or "").lower()
        label = next((tr for k, tr in KEY_EVENTS if k in name), None)
        if not label or label in seen:
            continue
        seen.add(label)
        gmt = str(row.get("gmt") or "").strip()
        m = re.match(r"(\d{1,2}):(\d{2})", gmt)
        when = f"{(int(m[1]) + 3) % 24:02d}:{m[2]}" if m else "Gün içinde"
        cons = html.unescape(str(row.get("consensus") or "")).strip()
        out.append(f"• {when} {label}" + (f" (beklenti: {esc(cons)})" if cons else ""))
    return out[:8]


def market_summary(today, names):
    lines = ["🌍 <b>Piyasa özeti</b>"]
    for sym, label in MARKET:
        q = quote(sym)
        if not q:
            continue
        last, prev = q
        chg = (last / prev - 1) * 100
        if sym == "^VIX":
            lines.append(f"VIX (korku endeksi): {last:.1f} ({vix_mood(last)})")
        elif sym == "^TNX":
            y, py = (last / 10, prev / 10) if last > 20 else (last, prev)
            lines.append(f"10 yıllık tahvil faizi: %{y:.2f} ({(y - py) * 100:+.0f} baz puan)")
        else:
            lines.append(f"{label}: {last:,.2f} ({chg:+.1f}%)")

    events = econ_events(today)
    if events:
        lines.append("\n📆 <b>Bugünkü önemli veriler</b> (İstanbul saati)")
        lines += events

    earn = earnings_on(today)
    if earn:
        bmo = sorted(t for t, w in earn.items() if t in names and w == "bmo")
        amc = sorted(t for t, w in earn.items() if t in names and w != "bmo")
        if bmo or amc:
            lines.append("\n📑 <b>Bugün bilanço açıklayacak S&P 500 şirketleri</b>")
            if bmo:
                lines.append("Açılış öncesi: " + ", ".join(bmo[:20]))
            if amc:
                lines.append("Kapanış sonrası: " + ", ".join(amc[:20]))
    return lines


def run_premarket(state, sp, forced):
    names = dict(sp)
    today = datetime.now(NY).date()
    if not forced and state.get("pm_date") == today.isoformat():
        print("Bugünün açılış öncesi raporu zaten gönderildi")
        return
    data = ext_download(list(names))
    prev = [d for d in sessions(data) if d < today]
    has_pre = sum(len(part(sub, today, PRE)) > 0 for sub in list(data.values())[:40]) >= 10
    if not prev or not has_pre:
        print("Bugün açılış öncesi işlem yok (tatil ya da hafta sonu olabilir)")
        return
    if not forced:
        state["pm_date"] = today.isoformat()

    parts = market_summary(today, names)
    rows = collect(data, prev[-1], today, PRE)
    print(f"Açılış öncesi: {len(rows)} hareket")
    if rows:
        earn_prev = earnings_on(prev[-1]) or {}
        earn_today = earnings_on(today) or {}
        earned = {t for t, w in earn_prev.items() if w != "bmo"} | {t for t, w in earn_today.items() if w != "amc"}
        now = datetime.now(NY)
        parts.append(f"\n🌅 <b>Açılış öncesi hareketler</b> (NY {now:%H:%M}, dünkü kapanışa göre)")
        parts += ext_lines(rows, names, earned)
        if not forced:
            for t, chg, price, _ in rows:
                log_signal(state, t, "pm_up" if chg > 0 else "pm_down", price, today)
    else:
        parts.append("\n🌅 Açılış öncesinde %3'ü aşan belirgin bir hareket yok.")
    notify("\n".join(parts))


def run_market(state, sp):
    """Sadece piyasa özeti (elle deneme için)."""
    notify("\n".join(market_summary(datetime.now(NY).date(), dict(sp))))


# ---------------------------------------------------------------- haftalık bilanço
def run_weekly(state, sp):
    names = dict(sp)
    today = datetime.now(NY).date()
    monday = today - timedelta(days=today.weekday())
    parts = ["📅 <b>Bu hafta bilanço açıklayacak S&P 500 şirketleri</b>"]
    found, failed = False, 0
    for i in range(5):
        d = monday + timedelta(days=i)
        earn = earnings_on(d)
        if earn is None:
            failed += 1
            continue
        hits = {t: w for t, w in earn.items() if t in names}
        if not hits:
            continue
        found = True
        parts.append(f"\n<b>{TR_DAYS[i]} ({tr_date(d)})</b>")
        for label, sel in (("Açılış öncesi", "bmo"), ("Kapanış sonrası", "amc"), ("Saati belirsiz", "?")):
            ts = sorted(t for t, w in hits.items() if w == sel)
            if ts:
                more = f" ve {len(ts) - 25} şirket daha" if len(ts) > 25 else ""
                parts.append(f"{label}: " + ", ".join(ts[:25]) + more)
    if failed == 5:
        print("Bilanço takvimi alınamadı, haftalık mesaj gönderilmedi")
        return
    if not found:
        parts.append("\nBu hafta S&P 500'de bilanço açıklayacak şirket yok.")
    parts.append("\n<i>Bilanço günlerinde hisseler sert hareket edebilir; pozisyon açmadan önce tarihe dikkat et.</i>")
    notify("\n".join(parts))


# ---------------------------------------------------------------- sinyal karnesi
LABELS = {
    "insider": "👤 Yönetici alımı", "cluster": "🔥 Küme alım",
    "golden": "🟢 Golden cross", "death": "🔴 Death cross",
    "high52": "🚀 Hacimli 52H zirve kapanışı", "low52": "Hacimli 52H dip kapanışı",
    "vol_up": "Hacimli yükseliş", "vol_down": "Hacimli düşüş",
    "oversold": "🟡 Aşırı satım (tepki adayı)", "overbought": "Aşırı alım",
    "intra_up": "⚡ Gün içi sert yükseliş", "intra_down": "⚡ Gün içi sert düşüş",
    "intra_high": "⚡ Gün içi 52H zirve kırılımı", "intra_low": "⚡ Gün içi 52H dip kırılımı",
    "ah_up": "🌙 Kapanış sonrası yükseliş", "ah_down": "🌙 Kapanış sonrası düşüş",
    "pm_up": "🌅 Açılış öncesi yükseliş", "pm_down": "🌅 Açılış öncesi düşüş",
}
BEARISH = {"death", "low52", "vol_down", "overbought", "intra_down", "intra_low", "ah_down", "pm_down"}


def run_scorecard(state):
    cutoff = (date.today() - timedelta(days=SETTINGS["log_days"])).isoformat()
    logs = [e for e in state.get("signal_log", []) + insider_state().get("signal_log", [])
            if e["d"] >= cutoff]
    if not logs:
        notify("📋 <b>Sinyal karnesi</b>\nHenüz kayıtlı sinyal yok. Karne, sinyaller birikmeye başladıkça dolacak.")
        return
    data = download(sorted({e["t"] for e in logs} | {"SPY"}), "1y", min_ok=1)
    spy = data.get("SPY")
    if spy is None:
        raise RuntimeError("SPY verisi alınamadı")
    dates = {t: [pd.Timestamp(x).date() for x in d.index] for t, d in data.items()}

    stats = {}
    for e in logs:
        d = data.get(e["t"])
        if d is None:
            continue
        sig_day = date.fromisoformat(e["d"])
        pos = bisect_right(dates[e["t"]], sig_day) - 1
        spos = bisect_right(dates["SPY"], sig_day) - 1
        if pos < 0 or spos < 0:
            continue
        st = stats.setdefault(e["k"], {"n": 0, 5: [], 20: []})
        st["n"] += 1
        for h in (5, 20):
            if pos + h < len(d) and spos + h < len(spy):
                ret = d["Close"].iloc[pos + h] / e["p"] - 1
                sret = spy["Close"].iloc[spos + h] / spy["Close"].iloc[spos] - 1
                hit = ret < 0 if e["k"] in BEARISH else ret > 0
                st[h].append((ret, ret - sret, hit))

    parts = [f"📋 <b>Sinyal karnesi</b> (son {SETTINGS['log_days']} gün)",
             "Sinyalden 5 ve 20 işlem günü sonraki ortalama getiri. Parantez içi: aynı dönemde S&P 500'e göre fark."]
    for k, st in sorted(stats.items(), key=lambda kv: kv[1]["n"], reverse=True):
        parts.append(f"\n<b>{LABELS.get(k, k)}</b> ({st['n']} sinyal)")
        done_any = False
        for h in (5, 20):
            res = st[h]
            if not res:
                continue
            done_any = True
            avg = sum(r[0] for r in res) / len(res) * 100
            exc = sum(r[1] for r in res) / len(res) * 100
            hit = sum(r[2] for r in res) / len(res) * 100
            parts.append(f"  {h} gün: {avg:+.1f}% ({exc:+.1f}%) · isabet %{hit:.0f} · {len(res)} sonuçlandı")
        if not done_any:
            parts.append("  Henüz sonuçlanmadı")
    parts.append("\n<i>Düşüş sinyallerinde isabet, fiyatın düşmesi demek. 20-30 sinyalin altındaki "
                 "sonuçlar tesadüf olabilir; karne zamanla anlamlı hale gelir.</i>")
    notify("\n".join(parts))


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
        if mode == "afterhours":
            run_afterhours(state, sp, forced=False)
        elif mode == "premarket":
            run_premarket(state, sp, forced=False)
        elif mode in ("afterhours-now", "premarket-now"):
            (run_afterhours if mode.startswith("after") else run_premarket)(state, sp, forced=True)
        elif mode == "weekly":
            run_weekly(state, sp)
        elif mode == "scorecard":
            run_scorecard(state)
        elif mode == "market":
            run_market(state, sp)
        elif mode == "intraday" or (mode == "auto" and weekday and opens <= now < closes):
            run_intraday(state, sp, forced=(mode == "intraday"))
        elif mode == "eod" or (mode == "auto" and weekday and now >= closes + timedelta(minutes=10)
                               and state.get("eod_date") != now.date().isoformat()):
            run_eod(state, sp, forced=(mode == "eod"))
        else:
            print("Piyasa kapalı, yapılacak iş yok")
    finally:
        prune_log(state)
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
