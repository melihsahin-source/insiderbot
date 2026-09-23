#!/usr/bin/env python3
"""
Insider & yatırımcı bildirim botu: SEC EDGAR -> Telegram

Alarm 1: Yöneticilerin açık piyasadan yüklü hisse alımları (Form 4, kod "P")
         + aynı şirkette birden fazla içeriden kişinin alım yapması (küme alım)
Alarm 2: Takip listesindeki yatırımcıların yeni bildirimleri (13F, 13D/G, Form 4)
"""
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_PATH = BASE / "state.json"

UA = os.environ.get("SEC_USER_AGENT", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ATOM = "{http://www.w3.org/2005/Atom}"
FORM4_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4"
              "&company=&dateb=&owner=include&start={start}&count=100&output=atom")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
_last_call = 0.0


# ---------------------------------------------------------------- yardımcılar
def sec_get(url):
    """SEC saniyede 10 istek sınırı koyuyor; biz ~7/sn ile kalıyoruz."""
    global _last_call
    r = None
    for attempt in range(4):
        wait = 0.15 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = session.get(url, timeout=30)
        if r.status_code == 200:
            return r
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        break
    r.raise_for_status()
    raise RuntimeError(f"SEC yanıt vermedi: {url}")


def notify(text):
    if not (TG_TOKEN and TG_CHAT):
        print("---- [Telegram ayarlı değil, mesaj konsola yazıldı] ----\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    if r.status_code != 200:
        print("Telegram hatası:", r.text, file=sys.stderr)
    time.sleep(1)  # Telegram hız sınırı


def current_price(ticker):
    """Hissenin güncel (birkaç dakika gecikmeli) fiyatı; alınamazsa None."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.replace(".", "-"))
        p = getattr(t.fast_info, "last_price", None)
        if not p or p != p:
            p = t.history(period="5d")["Close"].iloc[-1]
        return float(p)
    except Exception as ex:
        print(f"{ticker} fiyatı alınamadı: {ex}", file=sys.stderr)
        return None


def days_ago(iso):
    try:
        n = (date.today() - date.fromisoformat(iso)).days
    except ValueError:
        return ""
    return " (bugün)" if n <= 0 else f" ({n} gün önce)"


def price_info(ticker, ref_price, ref_label):
    """(mesaj satırı, güncel fiyat)"""
    p = current_price(ticker)
    if p is None or not ref_price:
        return "", p
    return f"\nGüncel fiyat: <b>${p:,.2f}</b> ({ref_label} bu yana {(p / ref_price - 1) * 100:+.1f}%)", p


def log_signal(state, ticker, key, price):
    """Haftalık sinyal karnesi için kayıt (karneyi scanner.py hazırlar)."""
    if not price:
        return
    state.setdefault("signal_log", []).append({
        "d": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        "t": ticker.replace(".", "-"), "k": key, "p": round(float(price), 4)})


def esc(s):
    return html.escape(s or "")


def money(v):
    if v >= 1e9:
        return f"${v / 1e9:.2f} milyar"
    if v >= 1e6:
        return f"${v / 1e6:.2f} milyon"
    return f"${v:,.0f}"


def txt(node, path):
    v = node.findtext(path)
    return v.strip() if v else ""


def is_true(node, tag):
    return txt(node, tag).lower() in ("1", "true")


def load_state():
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        s = {}
    s.setdefault("seen_form4", [])
    s.setdefault("recent_buys", [])
    s.setdefault("seen_watch", {})
    return s


# ---------------------------------------------------------------- Alarm 1
def latest_form4(seen, max_pages):
    """EDGAR 'son bildirimler' akışından henüz görülmemiş Form 4'leri döndürür."""
    found = {}
    for page in range(max_pages):
        root = ET.fromstring(sec_get(FORM4_FEED.format(start=page * 100)).content)
        entries = root.findall(f"{ATOM}entry")
        if not entries:
            break
        hit_seen = False
        for e in entries:
            m = re.search(r"accession-number=([\d-]+)", e.findtext(f"{ATOM}id", ""))
            link = e.find(f"{ATOM}link")
            if not m or link is None:
                continue
            acc = m.group(1)
            if acc in seen:
                hit_seen = True
                continue
            found.setdefault(acc, link.get("href"))
        if hit_seen:  # buradan sonrası zaten önceki çalışmada görüldü
            break
    return found


def fetch_form4_xml(index_url):
    folder = index_url.rsplit("/", 1)[0] + "/"
    items = sec_get(folder + "index.json").json()["directory"]["item"]
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml")]
    if not xmls:
        return None
    return ET.fromstring(sec_get(folder + xmls[0]).content)


def parse_form4(root):
    """Sadece açık piyasa alımlarını (kod P) toplar. Alım yoksa None."""
    if txt(root, "documentType") != "4":  # 4/A (düzeltme) bildirimlerini atla
        return None
    owners, roles = [], []
    for ro in root.findall("reportingOwner"):
        owners.append(txt(ro, "reportingOwnerId/rptOwnerName"))
        rel = ro.find("reportingOwnerRelationship")
        if rel is None:
            continue
        if is_true(rel, "isOfficer"):
            roles.append(txt(rel, "officerTitle") or "Yönetici")
        if is_true(rel, "isDirector"):
            roles.append("YK Üyesi")
        if is_true(rel, "isTenPercentOwner"):
            roles.append("%10+ ortak")

    shares = value = 0.0
    dates = set()
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if txt(t, "transactionCoding/transactionCode") != "P":
            continue
        if txt(t, "transactionAmounts/transactionAcquiredDisposedCode/value") != "A":
            continue
        try:
            s = float(txt(t, "transactionAmounts/transactionShares/value"))
            p = float(txt(t, "transactionAmounts/transactionPricePerShare/value"))
        except ValueError:
            continue
        shares += s
        value += s * p
        d = txt(t, "transactionDate/value")
        if d:
            dates.add(d[:10])
    if value <= 0:
        return None
    return {
        "issuer": txt(root, "issuer/issuerName"),
        "ticker": txt(root, "issuer/issuerTradingSymbol").upper() or "?",
        "owner": " & ".join(o for o in owners if o) or "?",
        "roles": ", ".join(dict.fromkeys(roles)) or "İçeriden kişi",
        "shares": shares,
        "value": value,
        "avg_price": value / shares if shares else 0,
        "dates": sorted(dates),
    }


def run_form4(state):
    cfg = CONFIG["insider"]
    seen_list = state["seen_form4"]
    seen = set(seen_list)
    bootstrap = not seen  # ilk çalışma: geçmişi kaydet, bildirim atma
    new = latest_form4(seen, 1 if bootstrap else cfg["max_pages"])
    print(f"Form 4: {len(new)} yeni bildirim" + (" (ilk çalışma, sessiz)" if bootstrap else ""))

    now = datetime.now(timezone.utc)
    window = timedelta(days=cfg["cluster_days"])
    recent = [b for b in state["recent_buys"]
              if now - datetime.fromisoformat(b["at"]) < window]

    for acc, href in new.items():
        seen_list.append(acc)
        if bootstrap:
            continue
        try:
            root = fetch_form4_xml(href)
            buy = parse_form4(root) if root is not None else None
        except Exception as ex:  # tek bir bildirim tüm çalışmayı bozmasın
            print(f"  {acc} okunamadı: {ex}", file=sys.stderr)
            continue
        if not buy:
            continue

        tk = buy["ticker"]
        if buy["value"] >= cfg["min_value_usd"]:
            pline, cur = price_info(tk, buy['avg_price'], 'yönetici alımından')
            log_signal(state, tk, "insider", cur)
            msg = (
                f"🟢 <b>Yönetici alımı: {esc(tk)}</b>\n"
                f"{esc(buy['issuer'])}\n"
                f"Alan: {esc(buy['owner'])} ({esc(buy['roles'])})\n"
                f"Tutar: <b>{money(buy['value'])}</b> "
                f"({buy['shares']:,.0f} hisse, ort. ${buy['avg_price']:,.2f})\n"
                f"İşlem tarihi: {', '.join(buy['dates'])}{days_ago(max(buy['dates'], default=''))}"
                f"{pline}\n"
                f'<a href="{href}">SEC bildirimi</a>'
            )
            notify(msg)

        if buy["value"] >= cfg["cluster_min_value_usd"]:
            before = {b["owner"] for b in recent if b["ticker"] == tk}
            recent.append({"ticker": tk, "owner": buy["owner"],
                           "value": buy["value"], "at": now.isoformat()})
            if buy["owner"] not in before and len(before) + 1 == cfg["cluster_min_insiders"]:
                group = [b for b in recent if b["ticker"] == tk]
                lines = "\n".join(f"• {esc(b['owner'])}: {money(b['value'])}" for b in group)
                total = sum(b["value"] for b in group)
                pline, cur = price_info(tk, buy['avg_price'], 'son alımdan')
                log_signal(state, tk, "cluster", cur)
                msg = (
                    f"🔥 <b>Küme alım: {esc(tk)}</b>\n"
                    f"Son {cfg['cluster_days']} günde {len(before) + 1} farklı içeriden kişi "
                    f"alım yaptı (toplam {money(total)}):\n{lines}"
                    f"{pline}\n"
                    f'<a href="{href}">Son bildirim</a>'
                )
                notify(msg)

    state["seen_form4"] = seen_list[-6000:]
    state["recent_buys"] = recent


# ---------------------------------------------------------------- Alarm 2
def form_label(form):
    f = form.replace("SCHEDULE ", "SC ")
    amend = " (düzeltme)" if f.endswith("/A") else ""
    if f.startswith("13F"):
        return "13F: çeyreklik portföy bildirimi" + amend
    if f.startswith("SC 13D"):
        return "13D: %5+ pay, aktif niyetli" + amend
    if f.startswith("SC 13G"):
        return "13G: %5+ pay, pasif" + amend
    if f.startswith("4"):
        return "Form 4: hisse işlemi" + amend
    return form


def local(el):
    return el.tag.split("}")[-1]


def lfind(el, name):
    for c in el.iter():
        if local(c) == name:
            return (c.text or "").strip()
    return ""


def holdings_13f(cik, acc):
    """13F bilgi tablosu: {cusip: {name, value($), shares}}; opsiyonlar hariç."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
    items = sec_get(folder + "index.json").json()["directory"]["item"]
    xmls = [i for i in items if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
    if not xmls:
        return None
    best = max(xmls, key=lambda i: int(str(i.get("size") or "0").strip() or 0))
    root = ET.fromstring(sec_get(folder + best["name"]).content)
    out = {}
    for el in root.iter():
        if local(el) != "infoTable" or lfind(el, "putCall"):
            continue
        cusip = lfind(el, "cusip")
        try:
            value, shares = float(lfind(el, "value") or 0), float(lfind(el, "sshPrnamt") or 0)
        except ValueError:
            continue
        h = out.setdefault(cusip, {"name": lfind(el, "nameOfIssuer").title(), "value": 0.0, "shares": 0.0})
        h["value"] += value
        h["shares"] += shares
    return out


def diff_13f_message(inv, cik, r, i):
    """r: submissions 'recent' dizileri, i: yeni 13F-HR'nin sırası. Mesaj ya da None."""
    prev_i = next((j for j in range(i + 1, len(r["form"])) if r["form"][j] == "13F-HR"), None)
    if prev_i is None:
        return None
    cur = holdings_13f(cik, r["accessionNumber"][i])
    prev = holdings_13f(cik, r["accessionNumber"][prev_i])
    if not cur or not prev:
        return None

    new = sorted(((v["value"], v["name"]) for c, v in cur.items() if c not in prev), reverse=True)
    sold = sorted(((v["value"], v["name"]) for c, v in prev.items() if c not in cur), reverse=True)
    up, down = [], []
    for c, v in cur.items():
        if c in prev and prev[c]["shares"] > 0:
            ch = v["shares"] / prev[c]["shares"] - 1
            if ch >= 0.10:
                up.append((v["value"], v["name"], ch))
            elif ch <= -0.10:
                down.append((v["value"], v["name"], ch))
    up.sort(reverse=True)
    down.sort(reverse=True)

    period = (r.get("reportDate") or [""] * len(r["form"]))[i]
    total = sum(v["value"] for v in cur.values())
    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
           f"{r['accessionNumber'][i].replace('-', '')}/{r['accessionNumber'][i]}-index.htm")
    parts = [f"🐋 <b>{esc(inv['name'])}: çeyreklik portföy değişiklikleri</b>",
             f"Dönem sonu: {period} · Bildirim: {r['filingDate'][i]}",
             f"Toplam portföy: {money(total)} ({len(cur)} pozisyon)"]
    if new:
        parts.append("\n🆕 <b>Yeni alımlar</b>")
        parts += [f"• {esc(n)} ({money(v)})" for v, n in new[:6]]
    if up:
        parts.append("\n⬆️ <b>Artırdı</b>")
        parts += [f"• {esc(n)} ({ch:+.0%}, {money(v)})" for v, n, ch in up[:6]]
    if down:
        parts.append("\n⬇️ <b>Azalttı</b>")
        parts += [f"• {esc(n)} ({ch:+.0%}, kalan {money(v)})" for v, n, ch in down[:6]]
    if sold:
        parts.append("\n❌ <b>Tamamen sattı</b>")
        parts += [f"• {esc(n)} (önceki çeyrek {money(v)})" for v, n in sold[:6]]
    if not (new or up or down or sold):
        parts.append("\nÖnceki çeyreğe göre kayda değer bir değişiklik yok.")
    parts.append(f"\n<i>13F bildirimleri çeyrek sonundan 45 güne kadar gecikmeli yayımlanır; "
                 f"değişiklikler geçmiş bir döneme aittir.</i>\n<a href=\"{url}\">SEC bildirimi</a>")
    return "\n".join(parts)


def run_watchlist(state):
    cutoff = (date.today() - timedelta(days=CONFIG["watchlist_lookback_days"])).isoformat()
    forms = set(CONFIG["watchlist_forms"])
    for inv in CONFIG["watchlist"]:
        cik = str(inv["cik"]).zfill(10)
        try:
            data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        except Exception as ex:
            print(f"{inv['name']} okunamadı: {ex}", file=sys.stderr)
            continue
        r = data["filings"]["recent"]
        bootstrap = cik not in state["seen_watch"]
        seen_list = state["seen_watch"].setdefault(cik, [])
        seen = set(seen_list)
        count = 0
        for i, (form, acc, fdate) in enumerate(zip(r["form"], r["accessionNumber"], r["filingDate"])):
            if fdate < cutoff or form not in forms or acc in seen:
                continue
            seen_list.append(acc)
            count += 1
            if bootstrap:
                continue
            if form == "13F-HR":
                try:
                    msg = diff_13f_message(inv, cik, r, i)
                except Exception as ex:
                    print(f"13F karşılaştırması yapılamadı: {ex}", file=sys.stderr)
                    msg = None
                if msg:
                    notify(msg)
                    continue
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{acc.replace('-', '')}/{acc}-index.htm")
            notify(
                f"🐋 <b>{esc(inv['name'])}</b>\n"
                f"Yeni bildirim: {esc(form_label(form))}\n"
                f"Tarih: {fdate}\n"
                f'<a href="{url}">SEC bildirimi</a>'
            )
        state["seen_watch"][cik] = seen_list[-500:]
        print(f"{inv['name']}: {count} yeni" + (" (ilk çalışma, sessiz)" if bootstrap else ""))


# ---------------------------------------------------------------- main
def main():
    if "--test" in sys.argv:
        notify("✅ Insider bot bağlantısı çalışıyor.")
        if UA and CONFIG["watchlist"]:  # örnek: ilk yatırımcının son 13F değişiklikleri
            inv = CONFIG["watchlist"][0]
            cik = str(inv["cik"]).zfill(10)
            try:
                r = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()["filings"]["recent"]
                i = r["form"].index("13F-HR")
                msg = diff_13f_message(inv, cik, r, i)
                if msg:
                    notify("🔎 <i>Örnek (son bildirim):</i>\n" + msg)
            except Exception as ex:
                print("Örnek 13F gönderilemedi:", ex, file=sys.stderr)
        return
    if not UA or "@" not in UA:
        sys.exit("SEC_USER_AGENT eksik. Örnek: 'Ad Soyad email@ornek.com'")

    state = load_state()
    failed = 0
    for step in (run_watchlist, run_form4):
        try:
            step(state)
        except Exception as ex:
            failed += 1
            print(f"{step.__name__} hata verdi: {ex}", file=sys.stderr)
    cutoff = (date.today() - timedelta(days=120)).isoformat()
    state["signal_log"] = [e for e in state.get("signal_log", []) if e["d"] >= cutoff]
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    if failed == 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
