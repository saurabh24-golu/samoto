#!/usr/bin/env python3
"""
supabarn_au_brochure.py — Supabarn (AU) weekly brochure scraper.

Same shape as the Drakes scraper: discover -> PDF -> render -> vision -> JSON per store.

WHAT IS DIFFERENT FROM DRAKES
-----------------------------
supabarn.com.au/brochure has NO pdf href. The chain is:

    www.supabarn.com.au/brochure               (Wix page)
      -> iframe  https://www-supabarn-com-au.filesusr.com/html/<id>.html
           -> iframe  https://e.issuu.com/embed.html?d=<docname>&u=<user>
                -> https://issuu.com/<user>/docs/<docname>
                     -> https://image.isu.pub/<docid>/jpg/page_N.jpg

So the "PDF" is an Issuu publication. We pull its page JPEGs (2717x2994,
~1 MB each against the public CDN), rebuild a real PDF locally from them, then
run page-by-page vision extraction on the rebuilt PDF. No login, no cookies,
no bot wall — but the image CDN 403s unless you send a full desktop Chrome UA
*and* `Referer: https://issuu.com/`. That single header pair is the whole trick.

LOCATIONS
---------
Supabarn publishes ONE brand-wide brochure (ACT + NSW in one 8-page doc).
Unlike Drakes, there are no per-store supplements on the store pages. So:
  - stores.json           -> all 21 locations (8 Supabarn + 13 SupaExpress),
                             address/phone/hours/state scraped per store page
  - stores/<slug>.json    -> that store's items (brand brochure, filtered by
                             item region: "all" or the store's state)
Items are identical across stores unless the flyer prints a state restriction
(`region` field). Every store file carries `items_shared_across_stores` so you
can tell the difference between "no store-specific data" and "scraper failed".

USAGE
-----
    export DEEPSEEK_API_KEY=sk-...
    python supabarn_au_brochure.py                      # full run
    python supabarn_au_brochure.py --pages 1-3          # partial (flagged)
    python supabarn_au_brochure.py --backend none       # download only, no LLM
    python supabarn_au_brochure.py --issuu-url https://issuu.com/supabarn/docs/<next-week>
    python supabarn_au_brochure.py --stores supabarn-crace,supaexpress-banks

Requirements: requests, Pillow.  (pip install requests pillow)
Windows/Linux/macOS. Resumable: existing page JPEGs and page JSONs are reused,
so a killed run continues where it stopped.
"""

import argparse
import base64
import html as html_mod
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import requests
from PIL import Image

SITE = "https://www.supabarn.com.au"
BROCHURE_PAGE = SITE + "/brochure"
STORES_PAGE = SITE + "/stores"

# The image CDN requires BOTH of these. A bare "Mozilla/5.0" gets 403.
# Do NOT advertise avif/webp in Accept: Issuu then serves a non-JPEG body and
# every downstream JPEG check fails while the HTTP status still says 200.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
IMG_HEADERS = {"User-Agent": UA, "Referer": "https://issuu.com/",
               "Accept": "image/jpeg"}
HTML_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

DEFAULT_MODEL = "deepseek-flash"          # only DeepSeek model with image input
DEFAULT_BASE = "https://api.deepseek.com/v1"
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
def http_get(url, *, headers=None, retries=4, timeout=60, ok=(200,)):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers or HTML_HEADERS, timeout=timeout)
            if r.status_code in ok:
                return r
            last = f"HTTP {r.status_code} for {url}"
            if r.status_code in (403, 404):
                break                      # don't hammer a deterministic 403
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 ** attempt)
    raise RuntimeError(last or f"GET failed: {url}")


def ua_needed_hint(url):
    return (f"{url}\n"
            "If this is an image.isu.pub URL: it 403s without a full Chrome "
            "User-Agent AND 'Referer: https://issuu.com/'.")


# --------------------------------------------------------------------------- #
# 1. discovery
# --------------------------------------------------------------------------- #
def _unescape_all(text):
    """html-unescape then JSON-unescape. Reading raw Wix HTML without both
    yields zero matches and looks exactly like 'no brochure published'."""
    t = html_mod.unescape(text)
    return t.replace("\\/", "/").replace('\\"', '"')


def list_issuu_docs(user):
    """Doc slugs from the publisher profile, NEWEST FIRST (Issuu's own order).

    Why not read them off supabarn.com.au/brochure? Because that page embeds the
    brochure through a Wix HTML component: the iframe src
    (www-supabarn-com-au.filesusr.com/html/<id>.html) is injected client-side and
    appears NOWHERE in the server HTML or in any inline script. curl/requests on
    /brochure returns 700 KB of Wix boilerplate and zero brochure references.
    The publisher profile is plain HTML and lists every doc, so that is the
    reliable discovery point.
    """
    r = http_get(f"https://issuu.com/{user}")
    txt = _unescape_all(r.text)
    slugs = re.findall(r"/docs/([A-Za-z0-9_\-\.]{4,120})", txt)
    seen, out = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    if not out:
        raise RuntimeError(f"No /docs/ links on https://issuu.com/{user}")
    return out


def doc_meta(user, slug):
    r = http_get(f"https://issuu.com/{user}/docs/{slug}")
    txt = _unescape_all(r.text)
    title = ""
    m = re.search(r'property="og:title" content="([^"]*)"', txt)
    if m:
        title = html_mod.unescape(m.group(1)).strip()
    if not title:
        m = re.search(r"<title>([^<]*)</title>", txt)
        title = html_mod.unescape(m.group(1)).strip() if m else ""
    ids = sorted(set(re.findall(r"image\.isu\.pub/(\d{10,16}-[0-9a-f]{32})/", txt)))
    if not ids:
        raise RuntimeError(f"no image.isu.pub doc id on {r.url} (deleted/gated?)")
    valid_from, valid_to = parse_validity(title)
    return {"user": user, "docname": slug, "docid": ids[0],
            "doc_url": r.url, "title": title,
            "valid_from": valid_from, "valid_to": valid_to}


def pick_current_doc(user, max_probe=5):
    """Walk the newest docs and take the one whose printed validity window
    contains today. Publishers upload next week's doc a few days early, so
    'newest' alone hands you a brochure that hasn't started yet."""
    slugs = list_issuu_docs(user)
    today = date.today().isoformat()
    metas, notes = [], []
    for slug in slugs[:max_probe]:
        try:
            m = doc_meta(user, slug)
        except RuntimeError as e:
            notes.append(f"{slug}: {e}")
            continue
        metas.append(m)
        vf, vt = m["valid_from"], m["valid_to"]
        if vf and vt and vf <= today <= vt:
            return m, metas, notes
        notes.append(f"{slug}: window {vf}..{vt} does not include {today}")
    if not metas:
        raise RuntimeError("Could not resolve any Issuu doc. " + " | ".join(notes[:3]))
    best = metas[0]
    notes.append(f"no doc window covers {today}; falling back to newest "
                 f"({best['docname']}, {best['valid_from']}..{best['valid_to']})")
    return best, metas, notes


def parse_validity(title):
    """'SUPABARN BROCHURE Available Wednesday 21st October until Tuesday 27th
    October 2026' -> ('2026-10-21', '2026-10-27'). Returns (None, None) if absent."""
    if not title:
        return None, None
    rx = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+"
                    r"(january|february|march|april|may|june|july|august|september|"
                    r"october|november|december)\s*(\d{4})?", re.I)
    hits = rx.findall(title)
    if len(hits) < 2:
        return None, None
    (d1, m1, y1), (d2, m2, y2) = hits[0], hits[-1]
    year = int(y2 or y1 or date.today().year)
    try:
        start = date(year, MONTHS[m1.lower()], int(d1))
    except ValueError:
        return None, None
    end_year = year if y2 else year
    if y2:
        end_year = int(y2)
    try:
        end = date(end_year, MONTHS[m2.lower()], int(d2))
    except ValueError:
        return None, None
    if end < start and not y2:                  # Dec -> Jan rollover
        end = end.replace(year=end.year + 1)
    return start.isoformat(), end.isoformat()


def resolve_doc(issuu_url):
    """Deprecated shim: kept so old invocations still resolve. Use doc_meta."""
    m2 = re.search(r"issuu\.com/([^/]+)/docs/([^/?#]+)", issuu_url)
    if m2:
        return doc_meta(m2.group(1), m2.group(2))
    m = re.search(r"[?&]d=([^&]+)", issuu_url)
    u = re.search(r"[?&]u=([^&]+)", issuu_url)
    if m and u:
        return doc_meta(u.group(1), m.group(1))
    raise RuntimeError(f"Cannot parse Issuu URL: {issuu_url}")


def page_count(docid, cap=80):
    """Issuu has no public pagecount endpoint we can trust; probe the CDN.
    A page past the end returns 403/404. thumb_medium is ~10 KB, so probing
    80 pages costs nothing."""
    n = 0
    misses = 0
    for i in range(1, cap + 1):
        url = f"https://image.isu.pub/{docid}/jpg/page_{i}_thumb_medium.jpg"
        try:
            r = requests.get(url, headers=IMG_HEADERS, timeout=30)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                n = i
                misses = 0
            else:
                misses += 1
                if misses >= 2:
                    break
        except requests.RequestException:
            misses += 1
            if misses >= 2:
                break
    if n == 0:
        raise RuntimeError(f"page probe found 0 pages for docid {docid}")
    return n


# --------------------------------------------------------------------------- #
# 2. download pages + rebuild a real PDF
# --------------------------------------------------------------------------- #
def download_pages(docid, count, img_dir, workers=6):
    os.makedirs(img_dir, exist_ok=True)
    paths = {}

    def fetch(i):
        p = os.path.join(img_dir, f"page_{i:02d}.jpg")
        if os.path.exists(p) and os.path.getsize(p) > 50_000:
            return i, p, "cached"
        url = f"https://image.isu.pub/{docid}/jpg/page_{i}.jpg"
        r = http_get(url, headers=IMG_HEADERS, timeout=120)
        if r.content[:2] != b"\xff\xd8":
            raise RuntimeError("not a JPEG: " + ua_needed_hint(url))
        with open(p, "wb") as f:
            f.write(r.content)
        return i, p, "ok"

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch, i): i for i in range(1, count + 1)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                idx, path, state = fut.result()
                paths[idx] = path
            except Exception as e:
                failed.append((i, str(e)))
    if failed:
        raise RuntimeError("page download failed: " + json.dumps(failed[:5]))
    return [paths[i] for i in sorted(paths)]


def build_pdf(image_paths, pdf_path):
    os.makedirs(os.path.dirname(pdf_path) or ".", exist_ok=True)
    imgs = [Image.open(p).convert("RGB") for p in image_paths]
    w, h = imgs[0].size
    scale = 200 / 25.4                      # ~200 dpi page box
    first, rest = imgs[0], imgs[1:]
    first.save(pdf_path, "PDF", resolution=200.0, save_all=True,
               append_images=rest, quality=90)
    return {"pdf": pdf_path, "pages": len(imgs), "px": [w, h],
            "inch": [round(w / scale, 2), round(h / scale, 2)],
            "mb": round(os.path.getsize(pdf_path) / 1e6, 2)}


# --------------------------------------------------------------------------- #
# 3. stores
# --------------------------------------------------------------------------- #
def discover_stores():
    r = http_get(STORES_PAGE)
    doc = _unescape_all(r.text)
    # slugs may contain an apostrophe (supaexpress-o'connor). Allow it here and
    # percent-encode at fetch time; a [a-z0-9-] class silently truncates to
    # "supaexpress-o" and yields a 404 that looks like a dead store page.
    slugs = re.findall(r"https://www\.supabarn\.com\.au/stores/([A-Za-z0-9\-'%]+)", doc)
    seen, out = set(), []
    for s in slugs:
        if s and s != "stores" and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def store_filename(slug):
    """Filesystem-safe name; apostrophes survive Windows/macOS but confuse
    shell loops and URLs, so normalise them away."""
    return re.sub(r"[^a-z0-9\-]+", "-", slug.lower()).strip("-")


def name_from_slug(slug):
    prefix, _, rest = slug.partition("-")
    brand = "SupaExpress" if prefix == "supaexpress" else "Supabarn"
    words = [w for w in rest.split("-") if w]
    out = []
    for w in words:
        out.append("O'Connor" if w == "o" and words[-1] == "connor" else w.capitalize())
    label = " ".join(dict.fromkeys(out)) or rest.title()
    # "o" + "connor" -> "O'Connor"
    label = re.sub(r"\bO Connor\b", "O'Connor", label)
    return f"{brand} {label}".strip()


def parse_store(slug):
    from urllib.parse import quote
    r = http_get(f"{SITE}/stores/{quote(slug, safe='-')}")
    doc = _unescape_all(r.text)
    txt = re.sub(r"<script.*?</script>", " ", doc, flags=re.S | re.I)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = html_mod.unescape(re.sub(r"<[^>]+>", " ", txt))
    txt = re.sub(r"\s+", " ", txt.replace("\u200b", " "))

    name = ""
    m = re.search(r"-->\s*((?:Supabarn|SupaExpress)\s+[A-Za-z' \-]{2,30}?)"
                  r"(?=\s+(?:Corporate Store|top of page|\d))", txt, re.I)
    if m:
        name = re.sub(r"\s+", " ", m.group(1)).strip()
    if not name:
        name = name_from_slug(slug)

    hours = ""
    m = re.search(r"Trading Hours:\s*(.+?)(?:Previous Store|Store List|Next Store|Home |$)", txt)
    if m:
        hours = re.sub(r"\s+", " ", m.group(1)).strip(" ​")

    body = txt
    m = re.search(r"Trading Hours:", txt)
    before = txt[:m.start()] if m else txt
    phone = ""
    pm = re.search(r"(0\d(?:[\s\-]?\d){8})", before)
    addr_raw = before
    if pm:
        phone = re.sub(r"\s+", " ", pm.group(1)).strip()
        addr_raw = before[:pm.start()]
    addr_raw = addr_raw.split("top of page")[-1]
    addr_raw = addr_raw.split("Corporate Store")[-1]
    addr = re.sub(r"\s+", " ", addr_raw).replace("\u200b", "").strip(" ,")
    # store pages repeat the breadcrumb ("Home Supabarn Harden ​ 45 Neill Street")
    addr = re.sub(r"^Home\s+", "", addr)
    if name and addr.lower().startswith(name.lower()):
        addr = addr[len(name):].strip(" ,")
    addr = re.sub(r"\s*,\s*Australia\s*$", "", addr).strip(" ,")

    sm = re.search(r"\b(ACT|NSW|VIC|QLD|SA|WA|TAS|NT)\b", addr)
    state = sm.group(1) if sm else ""

    return {
        "slug": slug,
        "file": store_filename(slug),
        "name": name or slug,
        "brand": "SupaExpress" if slug.startswith("supaexpress") else "Supabarn",
        "corporate_store": "Corporate Store" in before,
        "state": state,
        "address": addr,
        "phone": phone,
        "trading_hours": hours,
        "store_url": f"{SITE}/stores/{slug}",
    }


# --------------------------------------------------------------------------- #
# 4. vision extraction
# --------------------------------------------------------------------------- #
PROMPT = """You are extracting supermarket specials from one page of the Supabarn
("The Barn"/SupaBarn Farmers Market) weekly brochure, an Australian grocery flyer.

Layout notes:
- Departments are announced by black header bars (e.g. "BUTCHERY, DELI & CHEESE",
  "SEAFOOD", "FRUIT & VEG", "GROCERY", "BAKERY", "DELI", "CHILLED").
- Each product cell: an orange badge circle top-left ("SAVE OVER 30%", "SAVE 40%",
  "1/2 PRICE", "Great Value"), a white price tag with the price and "kg"/"ea",
  a photo, then the product name and a small unit price line ("$25.98 per kg",
  "$3.45 per 100g").
- Pages may also contain editorial/promo blocks with no product (ignore them) or
  multi-buys ("2 FOR $26", "ANY 2 FOR $8").

Return ONE JSON object, no prose, no markdown fence:

{"page_type": "products" | "cover" | "editorial" | "mixed",
 "department": "main department on this page or ''",
 "region_restriction": "all" | "ACT" | "NSW",
 "items": [
  {"department": "BUTCHERY, DELI & CHEESE",
   "name": "BALZANELLI AUSTRALIAN RINDLESS BACON 500g",
   "size": "500g",
   "price": 12.99,
   "price_uom": "ea" | "kg" | "per 100g",
   "unit_price": 25.98,
   "unit_price_uom": "kg" | "100g",
   "save_amount": 6.00,
   "save_pct": 30,
   "badge": "SAVE OVER 30%",
   "multi_buy": "",
   "was_price": null,
   "notes": ""
  }
]}

Rules:
- price is the number printed on the price tag, as a float, no "$". Never invent it.
- price_uom: "kg" when the tag says kg, "ea" when it says ea/each, "per 100g" if the
  tag itself is a per-100g price.
- save_amount: copy the SAVE $ strip EXACTLY, digit for digit ("SAVE $6.25" -> 6.25).
  It is a small orange strip printed next to the price. Do NOT compute it from the
  price and do NOT derive it from the percentage badge. If it is illegible or absent,
  use null - a wrong number is worse than null.
- save_pct only if a percentage is printed. Do not compute it.
- unit_price_uom: "kg", "100g", "100ml", "L" or "ml" - whichever unit the unit-price
  line actually prints (dairy/ice cream is usually per 100ml, not per 100g).
- multi_buy: copy the printed multi-buy text ("2 FOR $26"), else "".
- was_price only if an actual crossed-out/original price is printed, else null.
- name: the product description verbatim, INCLUDING the pack size when printed there.
- size: pack size alone ("500g", "1kg", "each", "pkt", "") - empty if not shown.
- badge: the badge/circle text verbatim, else "".
- region_restriction: if the page prints a state restriction banner ("ACT stores
  only", "NSW only"), use that state for the page and for every item; otherwise
  "all".
- Skip pure decoration, logos, social handles, page numbers.
- If the page has no priced products at all, return page_type "cover"/"editorial"
  and an empty items array. Do not invent items to fill the array."""


def _encode(path, max_dim=2600, quality=90):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    if max(w, h) > max_dim:
        s = max_dim / max(w, h)
        im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def vision_page(path, *, api_key, base_url, model, effort="low", max_tokens=8000):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + _encode(path)}},
            ],
        }],
        # reasoning_effort is the big cost lever on deepseek-flash:
        # "low" ~13 s/page, "high" (default) ~51 s/page, same accuracy.
        "reasoning_effort": effort,
        # reasoning tokens are billed BEFORE content; below ~8000 you get an
        # HTTP 200 with EMPTY content and every token spent on thinking.
        "max_completion_tokens": max_tokens,
    }
    hdr = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = None
    for attempt in range(4):
        try:
            r = requests.post(url, headers=hdr, json=payload, timeout=300)
            if r.status_code == 200:
                data = r.json()
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if not content:
                    rt = (data.get("usage") or {}).get("completion_tokens")
                    raise RuntimeError(
                        f"empty content (completion_tokens={rt}) — reasoning ate "
                        f"the budget; raise --max-tokens")
                return _parse_json(content), data.get("usage", {})
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (400, 401, 403, 404):
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 ** attempt)
    raise RuntimeError(last)


def _parse_json(content):
    c = content.strip()
    c = re.sub(r"^```(?:json)?\s*|\s*```$", "", c)
    m = re.search(r"\{.*\}", c, re.S)
    if m:
        c = m.group(0)
    return json.loads(c)


def normalise(parsed, page_no, page_region):
    """Coerce/normalise model output so downstream joins are safe."""
    items = []
    page_type = parsed.get("page_type", "products")
    for raw in parsed.get("items") or []:
        if not isinstance(raw, dict):
            continue
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        def fnum(*keys):
            for k in keys:
                v = raw.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, str):
                    m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
                    if m:
                        return float(m.group(0))
            return None
        price = fnum("price")
        items.append({
            "page": page_no,
            "department": (raw.get("department") or parsed.get("department") or "").strip(),
            "name": name,
            "size": (raw.get("size") or "").strip(),
            "price": price,
            "price_uom": (raw.get("price_uom") or "").strip().lower(),
            "unit_price": fnum("unit_price"),
            "unit_price_uom": (raw.get("unit_price_uom") or "").strip().lower(),
            "save_amount": fnum("save_amount"),
            "save_pct": fnum("save_pct"),
            "badge": (raw.get("badge") or "").strip(),
            "multi_buy": (raw.get("multi_buy") or "").strip(),
            "was_price": fnum("was_price"),
            "region": (raw.get("region_restriction") or parsed.get("region_restriction")
                       or page_region or "all"),
            "notes": (raw.get("notes") or "").strip(),
        })
    return page_type, items


# --------------------------------------------------------------------------- #
# 5. main
# --------------------------------------------------------------------------- #
def save_consistency(items):
    """The flyer prints BOTH a 'SAVE $X' strip and a '%' badge, and both derive
    from the same was-price. So price+save must reproduce the badge percentage.
    When it doesn't, one of the two numbers was misread. This is the cheapest
    independent check we get without a human re-reading every cell."""
    checked, bad = 0, []
    for it in items:
        p, s = it.get("price"), it.get("save_amount")
        badge = it.get("badge") or ""
        m = re.search(r"(\d{1,2})\s*%", badge)
        if not (p and s and m and s < p * 3):
            continue
        printed = int(m.group(1))
        implied = 100.0 * s / (p + s)
        checked += 1
        over = "over" in badge.lower()
        off = (implied < printed - 6) if over else (abs(implied - printed) > 6)
        if off:
            bad.append({"page": it["page"], "name": it["name"], "price": p,
                        "save_amount": s, "badge": badge,
                        "implied_pct": round(implied, 1), "printed_pct": printed})
    pct = round(100 * (checked - len(bad)) / checked, 1) if checked else None
    return pct, bad


def main():
    ap = argparse.ArgumentParser(description="Supabarn AU weekly brochure -> JSON per store")
    ap.add_argument("--out", default="out", help="output directory (default: out)")
    ap.add_argument("--issuu-url", help="skip discovery and use this Issuu doc/embed URL")
    ap.add_argument("--issuu-user", default="supabarn",
                    help="Issuu publisher account (default: supabarn)")
    ap.add_argument("--pages", help="page list, 1-based, e.g. 1-3 or 2,5,7")
    ap.add_argument("--stores", help="comma list of store slugs (default: all)")
    ap.add_argument("--backend", choices=["vision", "none"], default="vision",
                    help="vision = LLM extraction, none = download PDF + stores only")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=os.environ.get("VISION_BASE_URL") or DEFAULT_BASE)
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=4, help="concurrent vision calls")
    ap.add_argument("--no-vision", action="store_true",
                    help="alias for --backend none")
    args = ap.parse_args()

    if args.backend == "vision" and not args.no_vision and not args.api_key:
        sys.exit("ERROR: DEEPSEEK_API_KEY not set (or pass --api-key). "
                 "Use --backend none to only fetch the PDF + store list.")

    out = args.out
    for sub in ("stores", "pages_json", "brochure_pdf"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    report = {"started": datetime.now().isoformat(timespec="seconds"),
              "problems": [], "warnings": []}
    t0 = time.time()

    # ---- 1. brochure discovery
    print("[1/5] discovering brochure ...", flush=True)
    if args.issuu_url:
        m = re.search(r"issuu\.com/([^/]+)/docs/([^/?#]+)", args.issuu_url)
        user = m.group(1) if m else args.issuu_user
        slug = m.group(2) if m else ""
        if not slug:
            m = re.search(r"[?&]d=([^&]+)", args.issuu_url)
            slug = m.group(1) if m else ""
        doc = doc_meta(user, slug)
        candidates, notes = [doc], ["forced via --issuu-url"]
    else:
        doc, candidates, notes = pick_current_doc(args.issuu_user)

    for n in notes:
        # "does not include <today>" is normal when next week's doc is already
        # uploaded; only surface it if we actually had to fall back.
        if "falling back" in n or "no image.isu.pub" in n:
            report["warnings"].append(n)
    print(f"      doc         : {doc['docname']}")
    print(f"      title       : {doc['title']}")
    print(f"      validity    : {doc['valid_from']} -> {doc['valid_to']}")
    print(f"      doc id      : {doc['docid']}")

    docname = doc["docname"]
    n_pages = page_count(doc["docid"])
    print(f"      pages       : {n_pages}")

    valid_from, valid_to = doc["valid_from"], doc["valid_to"]
    if valid_to and valid_to < date.today().isoformat():
        report["warnings"].append(f"brochure validity ended {valid_to} (stale?)")
        print(f"      WARNING: validity ended {valid_to}")

    # ---- 2. pages + pdf
    print("[2/5] downloading pages ...", flush=True)
    img_dir = os.path.join(out, "pages", docname)
    images = download_pages(doc["docid"], n_pages, img_dir)
    pdf_info = build_pdf(images, os.path.join(out, "brochure_pdf", docname + ".pdf"))
    print(f"      {pdf_info['pages']} pages, {pdf_info['px'][0]}x{pdf_info['px'][1]} px, "
          f"pdf {pdf_info['mb']} MB -> {pdf_info['pdf']}")

    brochure = {
        "retailer": "Supabarn",
        "country": "AU",
        "source_page": BROCHURE_PAGE,
        "issuu_profile": f"https://issuu.com/{doc['user']}",
        "issuu_doc_url": doc["doc_url"],
        "issuu_doc_name": docname,
        "issuu_doc_id": doc["docid"],
        "title": doc["title"],
        "valid_from": valid_from,
        "valid_to": valid_to,
        "page_count": n_pages,
        "pdf": os.path.abspath(pdf_info["pdf"]),
        "pages_dir": os.path.abspath(img_dir),
        "shared_across_stores": True,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }

    # ---- 3. stores
    print("[3/5] discovering stores ...", flush=True)
    slugs = discover_stores()
    if args.stores:
        want = {store_filename(s.strip()) for s in args.stores.split(",") if s.strip()}
        missing = want - {store_filename(s) for s in slugs}
        if missing:
            report["warnings"].append(f"unknown store slugs: {sorted(missing)}")
        slugs = [s for s in slugs if store_filename(s) in want]
    stores = []
    for s in slugs:
        try:
            st = parse_store(s)
        except Exception as e:
            report["problems"].append(f"store {s}: {e}")
            st = {"slug": s, "file": store_filename(s), "name": s, "state": "",
                  "address": "", "phone": "", "trading_hours": "",
                  "store_url": f"{SITE}/stores/{s}"}
        stores.append(st)
    print(f"      {len(stores)} stores "
          f"({sum(1 for s in stores if s.get('brand')=='Supabarn')} Supabarn / "
          f"{sum(1 for s in stores if s.get('brand')=='SupaExpress')} SupaExpress)")
    with open(os.path.join(out, "stores.json"), "w", encoding="utf-8") as f:
        json.dump({"retailer": "Supabarn", "count": len(stores), "stores": stores},
                  f, indent=2, ensure_ascii=False)

    # ---- 4. per-page vision
    all_items, page_types = [], {}
    partial = bool(args.pages)
    if args.backend == "vision" and not args.no_vision:
        wanted = list(range(1, n_pages + 1))
        if args.pages:
            wanted = []
            for part in args.pages.split(","):
                if "-" in part:
                    a, b = part.split("-")
                    wanted += list(range(int(a), int(b) + 1))
                else:
                    wanted.append(int(part))
            wanted = sorted({p for p in wanted if 1 <= p <= n_pages})
        print(f"[4/5] vision extraction on pages {wanted} "
              f"({args.model}, effort={args.effort}, {args.workers} workers) ...", flush=True)

        pj_dir = os.path.join(out, "pages_json", docname)
        os.makedirs(pj_dir, exist_ok=True)

        def work(pno):
            cache = os.path.join(pj_dir, f"page_{pno:02d}.json")
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    return pno, json.load(f), "cached"
            parsed, usage = vision_page(
                images[pno - 1], api_key=args.api_key, base_url=args.base_url,
                model=args.model, effort=args.effort, max_tokens=args.max_tokens)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
            return pno, parsed, "ok"

        ok = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {pool.submit(work, p): p for p in wanted}
            for fut in as_completed(futs):
                pno = futs[fut]
                try:
                    pno, parsed, state = fut.result()
                except Exception as e:
                    # one retry at higher effort before giving up on the page
                    try:
                        parsed, _ = vision_page(
                            images[pno - 1], api_key=args.api_key, base_url=args.base_url,
                            model=args.model, effort="medium",
                            max_tokens=args.max_tokens)
                        state = "retry"
                    except Exception as e2:
                        report["problems"].append(f"page {pno}: {e} / retry: {e2}")
                        page_types[pno] = "failed"
                        print(f"      page {pno:02d} FAILED: {e}")
                        continue
                ptype, items = normalise(parsed, pno, None)
                page_types[pno] = ptype
                all_items.extend(items)
                ok += 1
                print(f"      page {pno:02d} {ptype:9s} {len(items):3d} items ({state})",
                      flush=True)

        if ok == 0:
            report["problems"].append(f"ALL {len(wanted)} queued pages failed")
            print("      FATAL: zero pages extracted")
        if partial:
            print(f"      NOTE: partial run (--pages), {len(wanted)}/{n_pages} pages")
    else:
        print("[4/5] vision skipped (--backend none)")

    # ---- 5. per-store json
    print("[5/5] writing per-store JSON ...", flush=True)
    written = 0
    for st in stores:
        state = st.get("state") or ""
        keep, dropped = [], 0
        for it in all_items:
            r = (it.get("region") or "all").lower()
            if r in ("all", "", state.lower()):
                keep.append(it)
            else:
                dropped += 1
        payload = {
            "store": st,
            "brochure": brochure,
            "items_shared_across_stores": True,
            "items_count": len(keep),
            "items_excluded_other_region": dropped,
            "items": keep,
        }
        with open(os.path.join(out, "stores", st["file"] + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        written += 1
    print(f"      {written} files -> {os.path.join(out,'stores')}/")

    with open(os.path.join(out, "brochure_current.json"), "w", encoding="utf-8") as f:
        json.dump({"brochure": brochure, "items_count": len(all_items),
                   "items": all_items}, f, indent=2, ensure_ascii=False)

    # ---- report + structural checks
    priced = [i for i in all_items if i["price"] is not None]
    depts = sorted({i["department"] for i in all_items if i["department"]})
    regions = sorted({i["region"] for i in all_items})
    save_pct_ok, suspects = save_consistency(all_items)
    report.update({
        "finished": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(time.time() - t0, 1),
        "brochure": {k: brochure[k] for k in
                     ("issuu_doc_name", "valid_from", "valid_to", "page_count")},
        "stores": len(stores),
        "pages_requested": sum(1 for v in page_types.values() if v != "failed"),
        "pages_ok": sum(1 for v in page_types.values() if v not in ("failed",)),
        "pages_failed": sorted(p for p, v in page_types.items() if v == "failed"),
        "page_types": {str(k): v for k, v in sorted(page_types.items())},
        "items": len(all_items),
        "items_without_price": len(all_items) - len(priced),
        "save_amount_fill_pct": round(100 * sum(1 for i in all_items if i["save_amount"])
                                      / max(1, len(all_items)), 1),
        "save_amount_consistency_pct": save_pct_ok,
        "suspect_saves": suspects,
        "unit_price_fill_pct": round(100 * sum(1 for i in all_items if i["unit_price"])
                                     / max(1, len(all_items)), 1),
        "departments": depts,
        "regions": regions,
        "partial": partial,
    })
    if priced and report["items_without_price"] > 0.15 * len(all_items):
        report["problems"].append(
            f"{report['items_without_price']}/{len(all_items)} items have no price")
    if suspects and len(suspects) > 0.2 * max(1, len(all_items)):
        report["problems"].append(
            f"{len(suspects)} rows fail the SAVE$/%badge consistency check "
            f"({save_pct_ok}% consistent) — save_amount is unreliable this run")
    if len(all_items) == 0 and args.backend == "vision":
        report["problems"].append("0 items extracted")
    if partial:
        report["warnings"].append(
            f"partial run: only {len(page_types)}/{n_pages} pages extracted")

    with open(os.path.join(out, "run_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print("\n===== SUMMARY =====")
    print(f"brochure : {docname} ({brochure['valid_from']} -> {brochure['valid_to']}), "
          f"{n_pages} pages")
    print(f"stores   : {len(stores)}")
    print(f"items    : {len(all_items)} ({report['items_without_price']} without price)")
    print(f"saves    : {report['save_amount_fill_pct']}% filled, "
          f"{save_pct_ok}% consistent with badge ({len(suspects)} suspect)")
    print(f"depts    : {', '.join(depts) or '-'}")
    print(f"regions  : {', '.join(regions) or '-'}")
    print(f"time     : {report['seconds']}s")
    if report["problems"]:
        print("PROBLEMS:")
        for p in report["problems"]:
            print("  !", p)
    if report["warnings"]:
        print("WARNINGS:")
        for w in report["warnings"]:
            print("  ~", w)
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
