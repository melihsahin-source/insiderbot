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
            notify(
                f"🟢 <b>Yönetici alımı: {esc(tk)}</b>\n"
                f"{esc(buy['issuer'])}\n"
                f"Alan: {esc(buy['owner'])} ({esc(buy['roles'])})\n"
                f"Tutar: <b>{money(buy['value'])}</b> "
                f"({buy['shares']:,.0f} hisse, ort. ${buy['avg_price']:,.2f})\n"
                f"İşlem tarihi: {', '.join(buy['dates'])}\n"
                f'<a href="{href}">SEC bildirimi</a>'
            )

        if buy["value"] >= cfg["cluster_min_value_usd"]:
            before = {b["owner"] for b in recent if b["ticker"] == tk}
            recent.append({"ticker": tk, "owner": buy["owner"],
                           "value": buy["value"], "at": now.isoformat()})
            if buy["owner"] not in before and len(before) + 1 == cfg["cluster_min_insiders"]:
                group = [b for b in recent if b["ticker"] == tk]
                lines = "\n".join(f"• {esc(b['owner'])}: {money(b['value'])}" for b in group)
                total = sum(b["value"] for b in group)
                notify(
                    f"🔥 <b>Küme alım: {esc(tk)}</b>\n"
                    f"Son {cfg['cluster_days']} günde {len(before) + 1} farklı içeriden kişi "
                    f"alım yaptı (toplam {money(total)}):\n{lines}\n"
                    f'<a href="{href}">Son bildirim</a>'
                )

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
        for form, acc, fdate in zip(r["form"], r["accessionNumber"], r["filingDate"]):
            if fdate < cutoff or form not in forms or acc in seen:
                continue
            seen_list.append(acc)
            count += 1
            if bootstrap:
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
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    if failed == 2:
        sys.exit(1)


if __name__ == "__main__":
    main()
