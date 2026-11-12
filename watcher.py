"""PS5 Pro stock watcher for Canadian retailers (GTA delivery / pickup).

Stores: Best Buy (API), Walmart, Amazon.ca, EB Games, Visions, Canada Computers,
London Drugs, Memory Express, Costco, Staples. When a PS5 Pro becomes orderable
at a normal price (sold by the retailer, not a reseller), it sends an email
through Gmail and a desktop notification (when run on a desktop).

  python watcher.py                one check pass
  python watcher.py --dry-run      check, but send nothing
  python watcher.py --test-email   send a test email
  python watcher.py --only ebgames,visions

Settings: config.json. Secrets come from environment variables or a local .env:
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EMAIL_TO (optional, defaults to GMAIL_ADDRESS)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
LOG_FILE = HERE / "watcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ps5pro")

MAX_PRICE = float(CONFIG["max_price_cad"])
PRO_RE = re.compile(r"(playstation|ps)\s*(®|�)?\s*5\s*(®|�)?\s*pro\b", re.I)
EXCLUDE_RE = re.compile(
    r"refurb|renewed|restored|pre-?owned|\bused\b|open box|^(case|stand|cover|skin)\b|"
    r"\b(vertical stand|protective case|cooling|cooler|fan|dock|skin|faceplate|cover|covers|"
    r"stand for|compatible with|for ps5|charging|headset|controller)\b|^disc drive|^internal|^search\b", re.I)


@dataclass
class Hit:
    retailer: str
    title: str
    price: float | None
    url: str
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.retailer}|{self.url}"


def is_console(title: str) -> bool:
    return bool(PRO_RE.search(title or "")) and not EXCLUDE_RE.search(title or "")


def price_ok(price: float | None) -> bool:
    return price is None or price <= MAX_PRICE


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\$\s?(\d[\d,]*(?:\s?\.\s?\d\d)?)", text.replace("\xa0", " "))
    if not m:
        return None
    return float(re.sub(r"[\s,]", "", m.group(1)))


# --------------------------------------------------------------------------- Best Buy (JSON API)
def check_bestbuy() -> list[Hit]:
    from curl_cffi import requests
    s = requests.Session(impersonate="chrome")
    r = s.get("https://www.bestbuy.ca/api/v2/json/search",
              params={"query": "playstation 5 pro console", "lang": "en-CA", "pageSize": 24}, timeout=30)
    r.raise_for_status()
    prods = {p["sku"]: p for p in r.json().get("products", [])
             if is_console(p.get("name", "")) and not p.get("isMarketplace") and not p.get("seller")}
    if not prods:
        raise RuntimeError("no PS5 Pro SKUs found in Best Buy search (site change?)")
    r = s.get("https://www.bestbuy.ca/ecomm-api/availability/products",
              params={"accept": "application/vnd.bestbuy.standardproduct.v1+json",
                      "accept-language": "en-CA", "locations": CONFIG.get("bestbuy_store_ids", ""),
                      "postalCode": CONFIG["postal_code"], "skus": "|".join(prods)}, timeout=30)
    r.raise_for_status()
    hits = []
    for a in r.json()["availabilities"]:
        p = prods[a["sku"]]
        price = p.get("salePrice") or p.get("regularPrice")
        ship, pick = a.get("shipping", {}), a.get("pickup", {})
        log.info("  bestbuy %s ship=%s pickup=%s $%s", a["sku"], ship.get("status"), pick.get("status"), price)
        if not price_ok(price):
            continue
        ways = []
        if ship.get("purchasable"):
            ways.append(f"ships to you ({ship.get('status')}, qty {ship.get('quantityRemaining')})")
        stores = [f"{l.get('name')} ({l.get('quantityOnHand')})" for l in pick.get("locations", [])
                  if l.get("hasInventory") or (l.get("quantityOnHand") or 0) > 0]
        if stores or pick.get("purchasable"):
            ways.append(f"store pickup: {', '.join(stores) or pick.get('status')}")
        if ways:
            hits.append(Hit("Best Buy", p["name"], price, "https://www.bestbuy.ca" + p["productUrl"], "; ".join(ways)))
    return hits


# --------------------------------------------------------------------------- Walmart (page JSON)
WALMART_URL = "https://www.walmart.ca/en/search?q=playstation+5+pro+console"


def check_walmart(page=None, store=None) -> list[Hit]:
    """Fast path: plain request. If Walmart rate-limits it (common from cloud servers), load the page in Chrome."""
    from curl_cffi import requests
    raw, why = None, ""
    try:
        r = requests.get(WALMART_URL, impersonate="chrome", timeout=30)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        raw, why = (m.group(1) if m else None), f"HTTP {r.status_code}"
    except Exception as e:
        why = str(e)[:80]
    if raw is None and page is not None:
        log.info("  walmart: plain request failed (%s), using browser", why)
        goto(page, WALMART_URL, 6000)
        raw = evaluate(page, "() => (document.getElementById('__NEXT_DATA__') || {}).textContent || null")
    if not raw:
        raise RuntimeError(f"Walmart page had no product data ({why}, probably bot-blocked)")
    data = json.loads(raw)

    def walk(o):
        if isinstance(o, dict):
            if "usItemId" in o and "name" in o:
                yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)

    hits, seen = [], set()
    for it in walk(data):
        name = it.get("name", "")
        if it["usItemId"] in seen or not PRO_RE.search(name):
            continue
        seen.add(it["usItemId"])
        seller = (it.get("sellerName") or "").strip()
        status = (it.get("availabilityStatusV2") or {}).get("value") or it.get("availabilityStatusDisplayValue")
        price = it.get("price")
        if seller.lower().startswith("walmart"):
            log.info("  walmart %s seller=%s status=%s $%s", it["usItemId"], seller, status, price)
            if str(status).upper().replace(" ", "_") == "IN_STOCK" and price_ok(price) and is_console(name):
                url = "https://www.walmart.ca" + (it.get("canonicalUrl") or f"/en/ip/{it['usItemId']}").split("?")[0]
                hits.append(Hit("Walmart", name, price, url, f"sold by {seller}"))
    log.info("  walmart: %d PS5 Pro listings seen (resellers ignored)", len(seen))
    return hits


# --------------------------------------------------------------------------- Browser stores
def open_browser(pw):
    """Real Chrome in a visible window gets past Cloudflare far more often than headless.
    On Linux servers run under xvfb-run so a 'visible' window exists."""
    headless = os.environ.get("HEADLESS", "0") == "1"
    args = ["--disable-blink-features=AutomationControlled"]
    if not headless and sys.platform == "win32":
        args.append("--window-position=-2400,-2400")  # keep it off-screen
    try:
        return pw.chromium.launch(channel="chrome", headless=headless, args=args)
    except Exception:
        return pw.chromium.launch(headless=headless, args=args)


BLOCK_RE = re.compile(r"just a moment|access denied|attention required|robot|captcha|security verification", re.I)


def _settle(page):
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    for _ in range(10):  # wait out any redirects
        try:
            return page.title()
        except Exception:
            page.wait_for_timeout(1000)
    return page.title()


def goto(page, url, wait_ms):
    """Open url; if an anti-bot interstitial shows, wait for it to clear, then reload once."""
    for attempt in range(2):
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        title = _settle(page)
        for _ in range(4):
            if not BLOCK_RE.search(title):
                page.wait_for_timeout(1500)
                return _settle(page)
            page.wait_for_timeout(5000)
            title = _settle(page)
    raise RuntimeError(f"blocked by anti-bot page ({title!r})")


def evaluate(page, js):
    for _ in range(3):
        try:
            return page.evaluate(js)
        except Exception as e:
            if "context was destroyed" not in str(e):
                raise
            page.wait_for_timeout(3000)
            _settle(page)
    return page.evaluate(js)


def check_amazon(page, store) -> list[Hit]:
    hits = []
    for asin in store["asins"]:
        url = f"https://www.amazon.ca/dp/{asin}"
        for _try in range(3):
            goto(page, url if _try == 0 else "https://www.amazon.ca/", 2500)
            if _try:
                goto(page, url, 3000)
            if not page.query_selector('form[action*="validateCaptcha"]'):
                break
        info = evaluate(page, """() => {
          const q = s => document.querySelector(s);
          const txt = s => (q(s) || {}).innerText || '';
          return {title: txt('#productTitle').trim(), avail: txt('#availability').trim(),
                  atc: !!q('#add-to-cart-button'),
                  price: txt('#corePrice_feature_div .a-offscreen') || txt('#buybox .a-price .a-offscreen'),
                  box: txt('#buybox, #desktop_buybox, #rightCol').slice(0, 2000),
                  captcha: !!q('form[action*="validateCaptcha"]')};
        }""")
        if info["captcha"]:
            raise RuntimeError("Amazon served a captcha")
        price = parse_price(info["price"])
        by_amazon = "Amazon.ca" in info["box"]
        log.info("  amazon %s atc=%s price=%s by_amazon=%s avail=%r", asin, info["atc"], price, by_amazon,
                 info["avail"][:50])
        if info["atc"] and price and price_ok(price) and is_console(info["title"]):
            hits.append(Hit("Amazon.ca", info["title"], price, url,
                            ("sold by Amazon.ca" if by_amazon else "third-party seller, check before buying")))
    return hits


TILES_JS = r"""() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.title || a.getAttribute('aria-label') || '').trim();
    if (!/(playstation|ps)\s*\S?\s*5\s*\S?\s*pro\b/i.test(t) || t.length > 200) continue;
    const href = a.href.split('?')[0];
    if (seen.has(href)) continue; seen.add(href);
    let el = a;
    for (let i = 0; i < 8 && el.parentElement; i++) {
      el = el.parentElement;
      if (/\$\s?\d/.test(el.innerText) && el.innerText.length > t.length + 20) break;
    }
    out.push({title: t.split('\n')[0], href, text: el.innerText.replace(/\s+/g, ' ').slice(0, 600)});
  }
  return out;
}"""


def check_tiles(page, store) -> list[Hit]:
    """Search/category page: find PS5 Pro product tiles and read each tile's stock wording."""
    goto(page, store["url"], store.get("wait_ms", 8000))
    tiles = evaluate(page, TILES_JS)
    hits, consoles = [], 0
    for t in tiles:
        if not is_console(t["title"]):
            continue
        consoles += 1
        price = parse_price(t["text"])
        in_stock = re.search(store["in_stock"], t["text"], re.I)
        out = re.search(store["out_of_stock"], t["text"], re.I)
        log.info("  %s %r $%s in=%s out=%s", store["id"], t["title"][:50], price,
                 in_stock and in_stock.group(0), out and out.group(0))
        if in_stock and not out and price and price_ok(price):
            hits.append(Hit(store["name"], t["title"], price, t["href"], in_stock.group(0)))
    if consoles == 0 and store.get("expect_listing", True):
        raise RuntimeError(f"no PS5 Pro listing found on {store['url']} (page layout changed or blocked)")
    return hits


def check_product(page, store) -> list[Hit]:
    """Single product page: decide from the page's own stock wording / buy button."""
    title = goto(page, store["url"], store.get("wait_ms", 8000))
    text = re.sub(r"\s+", " ", page.inner_text("body"))
    if not PRO_RE.search(title + " " + text[:3000]):
        raise RuntimeError(f"product page no longer shows a PS5 Pro ({title!r})")
    price = parse_price(text[text.lower().find("price"):][:200]) if "price" in text.lower() else parse_price(text)
    in_stock = re.search(store["in_stock"], text, re.I)
    out = re.search(store["out_of_stock"], text, re.I)
    log.info("  %s $%s in=%s out=%s", store["id"], price, in_stock and in_stock.group(0), out and out.group(0))
    if in_stock and not out and price_ok(price):
        return [Hit(store["name"], title.split("|")[0].strip(), price, store["url"], in_stock.group(0))]
    return []


BROWSER_CHECKS = {"walmart": check_walmart, "amazon": check_amazon, "tiles": check_tiles, "product": check_product}


# --------------------------------------------------------------------------- Notify
def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def send_email(subject: str, body: str) -> bool:
    load_env()
    user = os.environ.get("GMAIL_ADDRESS")
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = os.environ.get("EMAIL_TO") or user
    if not user or not pw:
        log.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set, cannot send email")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    log.info("email sent to %s: %s", to, subject)
    return True


def desktop_notify(title: str, text: str):
    try:
        import subprocess
        if sys.platform == "win32":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                  f"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode({json.dumps(title)}))>$null;"
                  f"$t.Item(1).AppendChild($x.CreateTextNode({json.dumps(text)}))>$null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PS5 Pro Watcher').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, creationflags=0x08000000)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title {json.dumps(title)}"], timeout=20)
    except Exception as e:
        log.warning("desktop notification failed: %s", e)


# --------------------------------------------------------------------------- Main
def run(dry_run: bool, only: set[str] | None):
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("in_stock", {}); state.setdefault("failures", {}); state.setdefault("warned", {})
    hits: list[Hit] = []
    checked: dict[str, str] = {}

    def attempt(name, fn, *a):
        if only and name not in only:
            return
        try:
            hits.extend(fn(*a))
            checked[name] = "ok"
            state["failures"][name] = 0
        except Exception as e:
            checked[name] = f"error: {str(e).splitlines()[0][:160]}"
            state["failures"][name] = state["failures"].get(name, 0) + 1
            log.warning("%s failed (%d in a row): %s", name, state["failures"][name], str(e)[:300])

    attempt("bestbuy", check_bestbuy)

    stores = [s for s in CONFIG["browser_stores"] if s.get("enabled", True) and (not only or s["id"] in only)]
    if stores:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = open_browser(pw)
            ctx = browser.new_context(locale="en-CA", timezone_id="America/Toronto",
                                      viewport={"width": 1366, "height": 900})
            for store in stores:
                page = ctx.new_page()
                attempt(store["id"], BROWSER_CHECKS[store["type"]], page, store)
                page.close()
            browser.close()

    now = time.time()
    new_hits, current = [], {}
    for h in hits:
        prev = state["in_stock"].get(h.key)
        current[h.key] = asdict(h)
        # alert on first sighting, and again every 3h while it stays orderable
        if not prev or now - prev.get("alerted_at", 0) > 3 * 3600:
            new_hits.append(h)
            current[h.key]["alerted_at"] = now
        else:
            current[h.key]["alerted_at"] = prev["alerted_at"]
    state["in_stock"] = current
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_checked"] = checked
    log.info("run done: %s", json.dumps(checked))
    log.info("%d orderable listing(s), %d new", len(hits), len(new_hits))

    if new_hits and not dry_run:
        lines = [f"PS5 Pro is ORDERABLE right now (price cap ${MAX_PRICE:,.0f}, GTA {CONFIG['postal_code']}):\n"]
        for h in new_hits:
            price = f"${h.price:,.2f}  " if h.price else ""
            lines.append(f"* {h.retailer}: {h.title}\n  {price}{h.detail}\n  {h.url}\n")
        lines.append("\nGo fast, restocks usually sell out in minutes.")
        retailers = ", ".join(sorted({h.retailer for h in new_hits}))
        try:
            send_email(f"PS5 Pro IN STOCK at {retailers}", "\n".join(lines))
        except Exception as e:
            log.error("email failed: %s", e)
        desktop_notify("PS5 Pro in stock!", f"{retailers}: {new_hits[0].url}")

    # one warning email when a store has been failing for a long time, cleared when it recovers
    limit = CONFIG.get("failure_alert_after_runs", 36)
    quiet = {s["id"] for s in CONFIG["browser_stores"] if s.get("warn_on_failure") is False}
    for name, n in state["failures"].items():
        if n >= limit and name not in quiet and not state["warned"].get(name) and not dry_run:
            try:
                if send_email(f"PS5 Pro watcher: {name} check is failing",
                              f"The {name} check has failed {n} times in a row.\nLast error: {checked.get(name)}\n"
                              "The other stores are still being checked."):
                    state["warned"][name] = True
            except Exception as e:
                log.error("warning email failed: %s", e)
        elif n == 0:
            state["warned"].pop(name, None)

    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-email", action="store_true")
    ap.add_argument("--only", help="comma-separated store ids, e.g. bestbuy,ebgames")
    args = ap.parse_args()
    load_env()
    if args.test_email:
        ok = send_email("PS5 Pro watcher: test email",
                        "This is a test. If you got this, PS5 Pro stock alerts will reach you.")
        sys.exit(0 if ok else 1)
    run(args.dry_run, set(args.only.split(",")) if args.only else None)
