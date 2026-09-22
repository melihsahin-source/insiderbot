#!/usr/bin/env python3
"""
S&P 500 teknik tarayıcı -> Telegram

Gün içi (ABD borsası açıkken, 30 dk'da bir):
  - %5+ sert hareket
  - Yüksek hacimle 52 haftalık zirve / dip kırılımı
  Her hisse için günde en fazla 1 bildirim.

Gün sonu (kapanıştan sonra, tek özet mesaj, sessiz bildirim):
  Sade dille üç grup: alım ilgisi olanlar, sert düşüş sonrası tepki adayları,
  satış baskısı olanlar.

Seans dışı raporlar:
  - Sabah 09:00 (İstanbul): dün akşam kapanış sonrası en çok hareket edenler
  - 16:00 (İstanbul): açılış öncesi en çok hareket edenler

Kullanım: python scanner.py [auto|test|intraday|eod|afterhours|premarket]
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
    "eod_group_max": 5,           # gün sonu özetinde her gruptaki en fazla hisse
    "ext_move_pct": 3.0,          # seans dışı raporlar için hareket eşiği (%)
    "ext_min_dollar": 1_000_000,  # seans dışı işlem hacmi alt sınırı ($)
    "ext_max": 8,                 # seans dışı raporda yön başına en fazla hisse
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
TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def tr_date(d):
    return f"{d.day} {TR_MONTHS[d.month - 1]}"


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
        notes, bull, bear, oversold = [], 0, 0, False

        if s50.iloc[-2] <= s200.iloc[-2] and s50.iloc[-1] > s200.iloc[-1]:
            bull += 2
            notes.append("Kısa vadeli trendi uzun vadeli trendini yukarı kesti (golden cross), yükseliş trendi güçleniyor.")
        if s50.iloc[-2] >= s200.iloc[-2] and s50.iloc[-1] < s200.iloc[-1]:
            bear += 2
            notes.append("Kısa vadeli trendi uzun vadeli trendinin altına indi (death cross), trend zayıflıyor.")

        hi, lo = c.iloc[-253:-1].max(), c.iloc[-253:-1].min()
        if c.iloc[-1] > hi and volx >= S["eod_breakout_volume_x"]:
            bull += 2
            notes.append(f"Normalin {volx:.1f} katı hacimle yılın en yüksek seviyesinde kapandı, güçlü alım ilgisi var.")
        elif c.iloc[-1] < lo and volx >= S["eod_breakout_volume_x"]:
            bear += 2
            notes.append(f"Normalin {volx:.1f} katı hacimle yılın en düşük seviyesinde kapandı, satış baskısı sürüyor.")
        elif volx >= S["volume_spike_x"]:
            if chg > 0:
                bull += 1
                notes.append(f"Normalin {volx:.1f} katı hacimle %{chg:.1f} yükseldi, yoğun alım var.")
            else:
                bear += 1
                notes.append(f"Normalin {volx:.1f} katı hacimle %{abs(chg):.1f} düştü, yoğun satış var.")

        if r.iloc[-2] >= S["rsi_low"] and r.iloc[-1] < S["rsi_low"]:
            oversold = True
            notes.append(f"Kısa sürede çok satıldı (RSI {r.iloc[-1]:.0f}). Tepki yükselişi gelebilir, ama önce düşüşün sebebine bak.")
        if r.iloc[-2] <= S["rsi_high"] and r.iloc[-1] > S["rsi_high"]:
            bear += 1
            notes.append(f"Kısa sürede çok yükseldi (RSI {r.iloc[-1]:.0f}), kâr satışları gelebilir.")

        score = bull + bear + (1 if oversold else 0)
        if score > 0 and t in insiders:
            bull += 2
            score += 2
            notes.append("Son 14 günde şirket yöneticisi kendi cebinden hisse aldı.")
        if score < S["min_score"]:
            continue
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
    notify("\n".join(parts)[:4000], silent=True)


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


def ext_report(title, rows, names, note):
    S = SETTINGS
    up = sorted([r for r in rows if r[1] > 0], key=lambda r: r[1], reverse=True)[:S["ext_max"]]
    down = sorted([r for r in rows if r[1] < 0], key=lambda r: r[1])[:S["ext_max"]]
    parts = [title]
    for label, items in (("🟢 <b>Yükselenler</b>", up), ("🔴 <b>Düşenler</b>", down)):
        if not items:
            continue
        parts.append("\n" + label)
        for t, chg, price, vol in items:
            extra = f", {fmt_shares(vol)} hisse işlem gördü" if vol > 0 else ""
            parts.append(f"• {link(t)} {esc(names.get(t, ''))}: {chg:+.1f}% (${price:,.2f}){extra}")
    parts.append(f"\n<i>{note}</i>")
    notify("\n".join(parts)[:4000])


def collect(data, day_base, day_ext, rng):
    S = SETTINGS
    rows = []
    for t, sub in data.items():
        reg, ext = part(sub, day_base, REG), part(sub, day_ext, rng)
        if reg.empty or ext.empty:
            continue
        base, last = reg["Close"].iloc[-1], ext["Close"].iloc[-1]
        vol = float(ext["Volume"].sum())
        chg = (last / base - 1) * 100
        if abs(chg) < S["ext_move_pct"]:
            continue
        if vol > 0 and vol * last < S["ext_min_dollar"]:
            continue  # çok az işlemle oluşmuş, güvenilmez hareket
        rows.append((t, chg, last, vol))
    return rows


NEWS_NOTE = ("Seans dışı sert hareketler çoğunlukla bilanço ya da önemli bir haberden kaynaklanır. "
             "Hissenin adına tıklayıp haberleri görebilirsin.")


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
    state["ah_date"] = day.isoformat()
    rows = collect(data, day, day, POST)
    print(f"Kapanış sonrası: {len(rows)} hareket")
    if rows:
        ext_report(f"🌙 <b>Kapanış sonrası hareketler</b> ({tr_date(day)} akşamı, kapanışa göre)",
                   rows, names, NEWS_NOTE)


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
    state["pm_date"] = today.isoformat()
    rows = collect(data, prev[-1], today, PRE)
    now = datetime.now(NY)
    print(f"Açılış öncesi: {len(rows)} hareket")
    if rows:
        ext_report(f"🌅 <b>Açılış öncesi hareketler</b> (NY {now:%H:%M} itibarıyla, dünkü kapanışa göre)",
                   rows, names, NEWS_NOTE)


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
        elif mode == "intraday" or (mode == "auto" and weekday and opens <= now < closes):
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
