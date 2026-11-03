#!/usr/bin/env python3
"""
foodworks_au_catalogue.py
=========================
FoodWorks Australia (MyFoodLink storefront) catalogue scraper.

Pipeline:
    chooser  ->  <store>.foodworks.com.au/catalogues  ->  /catalogues/<edition_id>
             ->  catalogues.myfoodlink.com/.../pdf/<hash>.pdf
             ->  render pages  ->  DeepSeek vision  ->  JSON per store

Actions (run with no arguments for the cheat sheet):
    stores     discover every store subdomain from the MyFoodWorks chooser
    discover   per store: list its catalogue editions -> catalogues.json
    fetch      resolve each unique edition to a PDF and download it
    render     PDF -> 200 dpi page JPEGs (+ text layer .txt) 
    extract    page JPEG -> DeepSeek vision -> products JSON (per page, per edition)
    merge      build edition/store JSON + products.jsonl + long.csv + summary.json
    verify     consistency checks over the built artifacts
    all        discover -> fetch -> render -> extract -> merge -> verify

Typical:
    python foodworks_au_catalogue.py stores
    python foodworks_au_catalogue.py discover
    python foodworks_au_catalogue.py all --action-workers 6
    python foodworks_au_catalogue.py all --stores aberdeen,adelong --pages 2   # smoke test

Env:
    DEEPSEEK_FLASH_API_KEY (or DEEPSEEK_API_KEY)  -- vision lane
    FOODWORKS_DATA_DIR                            -- optional output root

External binaries:
    Poppler (pdftoppm / pdftotext) or PyMuPDF (pip install pymupdf).
    The script auto-detects; Poppler wins when both are present.
"""

import argparse
import base64
import concurrent.futures as cf
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CHOOSER_URL = "https://chooser.myfoodworks.com.au/"
CATALOGUES_HOST = "https://catalogues.myfoodlink.com"
API_URL = "https://api.deepseek.com/v1/chat/completions"
VISION_MODEL = "deepseek-flash"

RENDER_DPI = 200

# subdomains that appear in chooser markup but are not stores
RESERVED_SUBDOMAINS = {"www", "shop", "chooser", "catalogues", "api", "static", "cdn"}


def build_ssl_context(insecure=False):
    """Windows venvs often ship without a CA bundle ->
    'CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate'.
    Prefer certifi, then the OS trust store via truststore, then ssl default."""
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    return ssl.create_default_context()


SSL_CTX = build_ssl_context()

PAGE_PROMPT = """You are extracting grocery products from ONE page of an Australian \
supermarket catalogue (FoodWorks / MyFoodLink flyer).

Return ONLY a JSON object, no prose, no markdown fences:
{
  "page_type": "products" | "cover" | "lifestyle" | "other",
  "products": [
    {
      "name": "full product name as printed, brand included",
      "size": "pack size / weight / variety text exactly as printed",
      "price": number or null,
      "price_text": "the main price exactly as printed, e.g. '2 FOR $7.50', '$5.75 EA', '$3.99'",
      "is_multi_buy": true/false,
      "multi_buy_qty": integer or null,
      "was_price": number or null,
      "save_text": "saving badge exactly as printed, e.g. 'SAVE $3.20', 'SAVE FROM $4.50', '1/2 PRICE'",
      "offer_text": "percentage/discount badge exactly as printed, e.g. '40% Off', '1/2 Price'",
      "unit_price": "unit rate exactly as printed, e.g. '$1.03 per 100ml', '$31.92 per kg'",
      "is_member_price": true/false,
      "non_member_price": number or null,
      "notes": "any remaining important text, or empty string"
    }
  ]
}

HARD RULES
1. `price` is the total amount the customer pays for the offer as printed.
   - "$5.75 EA"            -> price 5.75,  is_multi_buy false
   - "2 FOR $7.50"         -> price 7.50,  is_multi_buy true, multi_buy_qty 2
   - "$26 / $35" ranges    -> use the LOWEST printed price, note the rest in notes
2. `was_price` ONLY when a genuine "was"/"$X.XX" previous price is printed.
   "SAVE $3.20" or "SAVE FROM $4.50" or "1/2 Price" or "40% Off" are NOT was prices.
   Put those in save_text/offer_text and set was_price to null. Never compute a was price.
3. Capture EVERY product that has a price on the page. Skip recipes, store
   addresses, competition text, subscribe panels and other non-priced blocks.
4. If the page has no priced products at all, return page_type != "products"
   and an empty products array. Do not invent products.
5. Copy text verbatim. Do not translate, do not pretty-print, do not round prices.
6. Member pricing ("trulylocal MEMBER PRICE $62 EA / Non-members $65.00ea"):
   price holds the MEMBER price, is_member_price true, non_member_price 65.00.
   Put the badge text in notes as well.
"""

CHEAT = """
FoodWorks AU catalogue scraper

  python foodworks_au_catalogue.py stores
        Discover every FoodWorks store subdomain (MyFoodWorks chooser).

  python foodworks_au_catalogue.py discover [--stores a,b]
        List each store's catalogue editions -> catalogues.json

  python foodworks_au_catalogue.py fetch [--stores a,b] [--limit N]
  python foodworks_au_catalogue.py render [--pages N]
  python foodworks_au_catalogue.py extract [--pages N] [--workers 6]
  python foodworks_au_catalogue.py merge
  python foodworks_au_catalogue.py verify
  python foodworks_au_catalogue.py all [--stores a,b] [--pages N]

Options that matter
  --data-dir DIR      output root (default ./foodworks-au-data)
  --stores a,b        only these store slugs (discovery cache always full)
  --limit N           first N unique editions only
  --pages N           first N pages per edition (smoke tests; sets partial=true)
  --workers N         parallel HTTP / vision workers (default 6)
  --effort low|high   DeepSeek reasoning effort per page (low = ~4x cheaper)
  --backend vision|text   vision = page JPEG to DeepSeek; text = pdfminer-style
                      text layer only (cheap, loses price<->name pairing)
  --keep-pdf          keep downloaded PDFs after render
  --force             redo work that already exists on disk
"""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)
    return path


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return default


def write_json(path, obj):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_env_key(*names):
    """Read a key from the environment, falling back to common .env files."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val.strip()
    candidates = [
        os.path.expanduser("~/.hermes/.env"),
        os.path.expanduser("~/.env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() in names:
                        return v.strip().strip('"').strip("'")
        except (IOError, OSError):
            continue
    return None


def http_get(url, timeout=45, retries=3, referer=None):
    """GET with UA + retry/backoff. Returns (status, body_bytes, headers)."""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-AU,en;q=0.9",
            "Referer": referer or "https://www.foodworks.com.au/",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                return resp.getcode(), resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                return exc.code, body, {}
            last_err = "HTTP %s" % exc.code
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise SystemExit(
                    "TLS failed (%s).\n"
                    "Your Python has no CA bundle. Fix with ONE of:\n"
                    "  pip install certifi truststore\n"
                    "  python -m pip install --upgrade certifi\n"
                    "  python foodworks_au_catalogue.py ... --insecure   (last resort, skips verification)"
                    % reason)
            last_err = "%s: %s" % (type(exc).__name__, exc)
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("GET failed %s (%s)" % (url, last_err))


def strip_tags(doc):
    doc = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", doc)
    text = re.sub(r"(?s)<[^>]+>", "\n", doc)
    text = html.unescape(text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def month_range(text):
    """'Wednesday, Oct 21 - Tuesday, Oct 27' -> ('2026-10-21', '2026-10-27')."""
    months = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
    hits = re.findall(r"([A-Za-z]{3,9})\s+(\d{1,2})(?:,?\s*(\d{4}))?", text or "")
    out = []
    year = time.gmtime().tm_year
    for mon, day, yr in hits:
        m = months.get(mon[:3].lower())
        if not m:
            continue
        if yr:
            year = int(yr)
        out.append((year, m, int(day)))
    if len(out) < 2:
        return None, None
    (y1, m1, d1), (y2, m2, d2) = out[0], out[1]
    if (m2, d2) < (m1, d1) and (m2, d2) < (6, 30):   # Dec 28 - Jan 3
        y2 = y1 + 1
    return ("%04d-%02d-%02d" % (y1, m1, d1), "%04d-%02d-%02d" % (y2, m2, d2))


# --------------------------------------------------------------------------
# 1. stores
# --------------------------------------------------------------------------

def action_stores(args):
    status, body, _ = http_get(CHOOSER_URL, timeout=60)
    if status != 200:
        raise SystemExit("chooser returned HTTP %s" % status)
    doc = body.decode("utf-8", "replace")

    # The chooser renders one card per store; the store's own site is the anchor.
    found = {}
    for slug in re.findall(
            r'href="(?:https?:)?//([a-z0-9][a-z0-9-]*)\.foodworks\.com\.au', doc):
        if slug in RESERVED_SUBDOMAINS:
            continue
        found.setdefault(slug, "https://%s.foodworks.com.au" % slug)

    # Enrich with the visible store name / address when the card markup allows it.
    for block in re.split(r'(?i)<a\b', doc):
        m = re.search(r'//([a-z0-9][a-z0-9-]*)\.foodworks\.com\.au', block)
        if not m or m.group(1) not in found:
            continue
        name = re.search(r'(?i)class="[^"]*Store(?:Card)?__Name[^"]*"[^>]*>\s*([^<]{2,90})', block)
        addr = re.search(r'(?i)class="[^"]*Store(?:Card)?__(?:Address|Location)[^"]*"[^>]*>\s*([^<]{4,160})', block)
        entry = found[m.group(1)]
        found[m.group(1)] = {
            "slug": m.group(1),
            "site": entry,
            "catalogues_url": entry + "/catalogues",
            "name": html.unescape(name.group(1)).strip() if name else None,
            "address": html.unescape(addr.group(1)).strip() if addr else None,
        }

    stores = []
    for slug in sorted(found):
        item = found[slug]
        if isinstance(item, str):
            item = {"slug": slug, "site": item,
                    "catalogues_url": item + "/catalogues",
                    "name": None, "address": None}
        stores.append(item)

    write_json(os.path.join(args.data_dir, "stores.json"), {
        "source": CHOOSER_URL,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(stores),
        "stores": stores,
    })
    log("stores: %d discovered -> %s" % (len(stores), os.path.join(args.data_dir, "stores.json")))
    for s in stores[:5]:
        log("  %s" % s["slug"])
    if len(stores) > 5:
        log("  ...")


# --------------------------------------------------------------------------
# 2. discover editions per store
# --------------------------------------------------------------------------

TILE_RE = re.compile(r'<a\s+class="([^"]*CatalogueEdition[^"]*)"\s+href="/catalogues/([a-f0-9]{16,32})"')


def parse_edition_tiles(doc):
    """-> [{id, series, dates_text, cover, is_current}] parsed from the tiles."""
    out = []
    for m in TILE_RE.finditer(doc):
        cls, edition_id = m.group(1), m.group(2)
        # slice to the next tile so fields never bleed between cards
        nxt = TILE_RE.search(doc, m.end())
        chunk = doc[m.end(): nxt.start() if nxt else m.end() + 2500]
        series = re.search(r'CatalogueEdition__SeriesName"[^>]*>(.*?)</', chunk, re.S)
        dates = re.search(r'CatalogueEdition__Dates"[^>]*>(.*?)</', chunk, re.S)
        cover = re.search(r'CatalogueEdition__Image"[^>]*background-image:\s*url\(([^)]+)\)', chunk)
        out.append({
            "id": edition_id,
            "is_current": "CurrentEdition" in cls,
            "series": html.unescape(re.sub(r"\s+", " ", series.group(1))).strip() if series else None,
            "dates_text": html.unescape(re.sub(r"\s+", " ", dates.group(1))).strip() if dates else None,
            "cover": cover.group(1).strip().strip("'\"") if cover else None,
        })
    # de-dupe on id, keeping the first (current) occurrence
    seen, uniq = set(), []
    for e in out:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        uniq.append(e)
    return uniq


def discover_one(store):
    slug = store["slug"]
    try:
        status, body, _ = http_get(store["catalogues_url"], timeout=45)
    except RuntimeError as exc:
        return slug, [], str(exc)
    if status != 200:
        return slug, [], "HTTP %s" % status
    editions = parse_edition_tiles(body.decode("utf-8", "replace"))
    return slug, editions, None


def action_discover(args):
    store_data = read_json(os.path.join(args.data_dir, "stores.json"))
    if not store_data:
        raise SystemExit("stores.json missing - run: python %s stores" % os.path.basename(__file__))
    stores = store_data["stores"]

    # NOTE: always discover the FULL list. Filtering here would shrink the cache
    # and the next unfiltered run would silently see a subset of stores.
    wanted = None
    if args.stores:
        wanted = {s.strip().lower() for s in args.stores.split(",") if s.strip()}

    log("discover: %d stores, %d workers" % (len(stores), args.workers))
    by_store, errors, failed = {}, [], 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(discover_one, s): s for s in stores}
        for i, fut in enumerate(cf.as_completed(futures), 1):
            slug, editions, err = fut.result()
            if err:
                failed += 1
                errors.append({"store": slug, "error": err})
            by_store[slug] = editions
            if i % 25 == 0 or i == len(stores):
                log("  %d/%d stores scanned" % (i, len(stores)))

    unique = {}
    for slug, editions in by_store.items():
        for e in editions:
            unique.setdefault(e["id"], []).append(slug)

    catalogues = {
        "source": CHOOSER_URL,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stores_with_catalogues": sum(1 for v in by_store.values() if v),
        "stores_scanned": len(stores),
        "store_errors": errors,
        "unique_editions": len(unique),
        "edges": sum(len(v) for v in by_store.values()),
        "by_store": by_store,
        "edition_owners": unique,
    }
    write_json(os.path.join(args.data_dir, "catalogues.json"), catalogues)
    log("discover: %d stores with catalogues, %d unique editions, %d store-edition edges, %d errors"
        % (catalogues["stores_with_catalogues"], len(unique), catalogues["edges"], failed))
    if wanted:
        log("discover: --stores filter applies to fetch/extract, cache kept complete")
    return catalogues


# --------------------------------------------------------------------------
# 3. fetch PDFs
# --------------------------------------------------------------------------

PDF_RE = re.compile(r'https://catalogues\.myfoodlink\.com/catalogue_assets/'
                    r'([a-f0-9]{16,32})/pdf/([a-f0-9]{32,64})\.pdf')


def page_count_pdf(path):
    """Count pages: pdfinfo -> PyMuPDF -> raw /Type /Page scan."""
    if shutil.which("pdfinfo"):
        try:
            out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=60).stdout
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            if m:
                return int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        pass
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return len(re.findall(rb"/Type\s*/Page[^s]", raw))
    except (IOError, OSError):
        return 0


def resolve_edition(edition_id, owner_slug, data_dir, timeout=45):
    """Hit the edition page from an owning store -> PDF url + dates."""
    url = "https://%s.foodworks.com.au/catalogues/%s" % (owner_slug, edition_id)
    try:
        status, body, _ = http_get(url, timeout=timeout, referer="https://%s.foodworks.com.au/catalogues" % owner_slug)
    except RuntimeError as exc:
        return {"id": edition_id, "owner": owner_slug, "error": str(exc)}
    if status != 200:
        return {"id": edition_id, "owner": owner_slug, "error": "HTTP %s" % status}

    doc = body.decode("utf-8", "replace")
    pdf = PDF_RE.search(doc)
    lines = strip_tags(doc)
    dates_text = next((ln for ln in lines if re.search(r"\b(?:mon|tue|wed|thu|fri|sat|sun)", ln, re.I)), None)
    valid_from, valid_to = month_range(dates_text)
    title = next((ln for ln in lines if re.search(r"(?i)catalog", ln)), None)
    return {
        "id": edition_id,
        "owner": owner_slug,
        "source_url": url,
        "pdf_url": pdf.group(0) if pdf else None,
        "dates_text": dates_text,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "title": title,
        "error": None if pdf else "no PDF link on edition page",
    }


def download_pdf(url, dest, referer=None):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer or "https://www.foodworks.com.au/"})
    with urllib.request.urlopen(req, timeout=180, context=SSL_CTX) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    os.replace(tmp, dest)
    return os.path.getsize(dest)


def action_fetch(args):
    cat = read_json(os.path.join(args.data_dir, "catalogues.json"))
    if not cat:
        raise SystemExit("catalogues.json missing - run: discover")
    owners = cat["edition_owners"]

    wanted = {s.strip().lower() for s in args.stores.split(",")} if args.stores else None
    edition_ids = []
    for eid, slugs in owners.items():
        if wanted and not (wanted & set(slugs)):
            continue
        edition_ids.append(eid)
    edition_ids.sort()
    if args.limit:
        edition_ids = edition_ids[:args.limit]

    ensure_dir(os.path.join(args.data_dir, "pdf"))
    editions_path = os.path.join(args.data_dir, "editions.json")
    editions = read_json(editions_path, {}) or {}

    log("fetch: %d unique editions, %d workers" % (len(edition_ids), args.workers))

    def work(eid):
        rec = editions.get(eid)
        owner = owners[eid][0]
        if rec and rec.get("pdf_url") and not args.force:
            pdf_path = os.path.join(args.data_dir, "pdf", eid + ".pdf")
            if os.path.isfile(pdf_path) and rec.get("page_count"):
                return eid, rec, "cached"
        rec = resolve_edition(eid, owner, args.data_dir)
        if rec.get("pdf_url"):
            pdf_path = os.path.join(args.data_dir, "pdf", eid + ".pdf")
            try:
                if args.force or not os.path.isfile(pdf_path):
                    rec["bytes"] = download_pdf(rec["pdf_url"], pdf_path, referer=rec.get("source_url"))
                else:
                    rec["bytes"] = os.path.getsize(pdf_path)
                rec["page_count"] = page_count_pdf(pdf_path)
                rec["pdf_path"] = pdf_path
            except Exception as exc:                    # noqa: BLE001 - report, don't die
                rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        return eid, rec, rec.get("error") or "ok"

    ok = failed = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, eid) for eid in edition_ids]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            eid, rec, state = fut.result()
            rec["stores"] = owners[eid]
            editions[eid] = rec
            if state in ("ok", "cached"):
                ok += 1
            else:
                failed += 1
                log("  FAIL %s (%s)" % (eid, state))
            if i % 10 == 0 or i == len(edition_ids):
                log("  %d/%d editions resolved" % (i, len(edition_ids)))
                write_json(editions_path, editions)     # progressive commit

    write_json(editions_path, editions)
    pages = sum(int(editions[e].get("page_count") or 0) for e in edition_ids)
    log("fetch: %d ok, %d failed, %d pages queued" % (ok, failed, pages))


# --------------------------------------------------------------------------
# 4. render pages
# --------------------------------------------------------------------------

HAVE_POPPLER = bool(shutil.which("pdftoppm"))
HAVE_FITZ = False
try:
    import fitz  # noqa: F401 - PyMuPDF
    HAVE_FITZ = True
except Exception:
    pass


def render_pdf(pdf_path, out_dir, pages=None, dpi=RENDER_DPI):
    """-> list of (page_no, jpg_path, txt_path). Poppler first, PyMuPDF fallback."""
    ensure_dir(out_dir)
    if HAVE_POPPLER:
        cmd = ["pdftoppm", "-jpeg", "-r", str(dpi)]
        if pages:
            cmd += ["-f", "1", "-l", str(pages)]
        cmd += [pdf_path, os.path.join(out_dir, "p")]
        subprocess.run(cmd, capture_output=True, timeout=600)
        rendered = []
        for name in sorted(os.listdir(out_dir)):
            m = re.match(r"p-?(\d+)\.jpg$", name)
            if m:
                rendered.append((int(m.group(1)), os.path.join(out_dir, name)))
        # poppler emits p-01.jpg; normalise to p01.jpg so page numbers and text
        # files never disagree between runs
        for page_no, jpg in list(rendered):
            target = os.path.join(out_dir, "p%02d.jpg" % page_no)
            if jpg != target:
                os.replace(jpg, target)
                rendered = [(n, target if n == page_no else p) for n, p in rendered]
        for page_no, jpg in rendered:
            txt = os.path.join(out_dir, "p%02d.txt" % page_no)
            if not os.path.isfile(txt):
                try:
                    with open(txt, "w", encoding="utf-8") as fh:
                        subprocess.run(["pdftotext", "-f", str(page_no), "-l", str(page_no), pdf_path, "-"],
                                       stdout=fh, stderr=subprocess.DEVNULL, timeout=120)
                except (OSError, subprocess.SubprocessError):
                    pass
        return [(n, j, os.path.join(out_dir, "p%02d.txt" % n)) for n, j in rendered]

    if HAVE_FITZ:
        import fitz
        out = []
        zoom = dpi / 72.0
        with fitz.open(pdf_path) as doc:
            total = doc.page_count if not pages else min(pages, doc.page_count)
            for idx in range(total):
                page = doc.load_page(idx)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                jpg = os.path.join(out_dir, "p%02d.jpg" % (idx + 1))
                pix.save(jpg)
                txt = os.path.join(out_dir, "p%02d.txt" % (idx + 1))
                with open(txt, "w", encoding="utf-8") as fh:
                    fh.write(page.get_text("text"))
                out.append((idx + 1, jpg, txt))
        return out

    raise SystemExit("no PDF renderer: install Poppler (pdftoppm) or `pip install pymupdf`")


def action_render(args):
    editions = read_json(os.path.join(args.data_dir, "editions.json"), {}) or {}
    if not editions:
        raise SystemExit("editions.json missing - run: fetch")
    wanted = {s.strip().lower() for s in args.stores.split(",")} if args.stores else None

    targets = []
    for eid, rec in editions.items():
        if not rec.get("pdf_url"):
            continue
        if wanted and not (wanted & set(rec.get("stores") or [])):
            continue
        targets.append(eid)
    targets.sort()
    if args.limit:
        targets = targets[:args.limit]

    log("render: %d editions at %d dpi (poppler=%s, pymupdf=%s)"
        % (len(targets), RENDER_DPI, HAVE_POPPLER, HAVE_FITZ))
    total = 0
    for n, eid in enumerate(targets, 1):
        pdf = os.path.join(args.data_dir, "pdf", eid + ".pdf")
        if not os.path.isfile(pdf):
            log("  skip %s (no pdf)" % eid)
            continue
        out_dir = os.path.join(args.data_dir, "pages", eid)
        if args.force and os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        try:
            rendered = render_pdf(pdf, out_dir, pages=args.pages)
        except Exception as exc:                        # noqa: BLE001
            log("  FAIL render %s: %s" % (eid, exc))
            continue
        total += len(rendered)
        log("  [%d/%d] %s: %d pages" % (n, len(targets), eid, len(rendered)))
    log("render: %d pages on disk" % total)


# --------------------------------------------------------------------------
# 5. extract (DeepSeek vision)
# --------------------------------------------------------------------------

class ApiError(Exception):
    pass


def vision_page(jpg_path, txt_path, api_key, effort="low", timeout=240, retries=4):
    with open(jpg_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")

    text_layer = ""
    if txt_path and os.path.isfile(txt_path):
        try:
            with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
                text_layer = fh.read()[:4000]
        except (IOError, OSError):
            text_layer = ""

    user_content = [{
        "type": "text",
        "text": PAGE_PROMPT + ("\n\nText layer of the SAME page (for spelling only - it destroys "
                               "price/name pairing, never trust it for which price belongs to which "
                               "product):\n" + text_layer if text_layer else ""),
    }, {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64," + b64},
    }]
    body = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": user_content}],
        "reasoning_effort": effort,          # OpenAI-style key, NOT "effort"
        "max_completion_tokens": 8000,       # reasoning tokens are billed first
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    payload = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(API_URL, data=payload, headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            if not content.strip():
                raise ApiError("empty content (raise max_completion_tokens; reasoning spent the budget)")
            return parse_page_json(content), data.get("usage") or {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise ApiError("auth failed HTTP %s: %s" % (exc.code, detail))
            if exc.code == 402:
                raise ApiError("out of credit HTTP 402: %s" % detail)
            last = ApiError("HTTP %s: %s" % (exc.code, detail))
        except (urllib.error.URLError, OSError) as exc:
            last = ApiError("%s: %s" % (type(exc).__name__, exc))
        except (ValueError, ApiError) as exc:
            last = ApiError(str(exc))
        if attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
    raise last or ApiError("unknown failure")


def parse_page_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\s*|\s*```$", "", content, flags=re.I | re.S)
    try:
        obj = json.loads(content)
    except ValueError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ApiError("model returned no JSON object")
        obj = json.loads(m.group(0))
    if isinstance(obj, list):
        obj = {"page_type": "products", "products": obj}
    if not isinstance(obj, dict):
        raise ApiError("unexpected JSON shape: %s" % type(obj).__name__)
    products = obj.get("products")
    if not isinstance(products, list):
        obj["products"] = []
    return obj


def apply_price_flags(row):
    """Backfill member/non-member pricing from free text. Idempotent, runs at
    extract time AND at merge time so old page JSONs gain the fields too."""
    blob = " ".join(str(row.get(k) or "") for k in ("notes", "save_text", "offer_text", "price_text"))
    non_member = re.search(r"non-?\s*members?\s*\$?\s*(\d+(?:\.\d{1,2})?)", blob, re.I)
    member = bool(re.search(r"member\s*price", blob, re.I))
    row["is_member_price"] = member
    row["non_member_price"] = to_money(non_member.group(1)) if non_member else None
    if row["non_member_price"] is not None and row.get("price") is not None:
        if row["non_member_price"] <= row["price"]:
            row["non_member_price"] = None      # must be the higher of the two
    return row


def normalise_products(page_obj, page_no):
    """Coerce model output; no silent fabrication of was_price."""
    out = []
    for row in page_obj.get("products") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        price = to_money(row.get("price"))
        if not name:
            continue

        qty = row.get("multi_buy_qty")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = None
        multi = bool(row.get("is_multi_buy")) or bool(qty and qty > 1)
        if not multi:
            qty = None
        if qty and qty < 2:
            qty, multi = None, False

        was = to_money(row.get("was_price"))
        if was is not None and (price is None or was <= price):
            was = None                     # a "was" below the now price is a misread
        if price is not None and price > 200:
            was = was                      # keep, verify flags it
        each = round(price / qty, 2) if (multi and price is not None and qty) else None

        out.append({
            "page": page_no,
            "name": name,
            "brand": (row.get("brand") or "").strip() or None,
            "size": (row.get("size") or "").strip() or None,
            "price": price,
            "each_price": each,
            "is_multi_buy": multi,
            "multi_buy_qty": qty,
            "price_text": (row.get("price_text") or "").strip() or None,
            "was_price": was,
            "save_text": (row.get("save_text") or "").strip() or None,
            "offer_text": (row.get("offer_text") or "").strip() or None,
            "unit_price": (row.get("unit_price") or "").strip() or None,
            "notes": (row.get("notes") or "").strip() or None,
        })
    return [apply_price_flags(r) for r in out]


def to_money(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", str(val).replace(",", "") if str(val).count(",") and "." in str(val) else str(val))
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", ".")), 2)
    except ValueError:
        return None


def action_extract(args):
    api_key = load_env_key("DEEPSEEK_FLASH_API_KEY", "DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_FLASH_API_KEY / DEEPSEEK_API_KEY not set (env or ~/.hermes/.env)")

    editions = read_json(os.path.join(args.data_dir, "editions.json"), {}) or {}
    wanted = {s.strip().lower() for s in args.stores.split(",")} if args.stores else None

    jobs = []
    for eid, rec in sorted(editions.items()):
        if wanted and not (wanted & set(rec.get("stores") or [])):
            continue
        pages_dir = os.path.join(args.data_dir, "pages", eid)
        if not os.path.isdir(pages_dir):
            continue
        jpgs = sorted((f for f in os.listdir(pages_dir) if f.endswith(".jpg")),
                      key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        if not jpgs:
            continue
        if args.pages:
            jpgs = jpgs[:args.pages]
        if args.limit:
            jobs.append((eid, jpgs))
        else:
            jobs.append((eid, jpgs))

    log("extract: %d editions, backend=%s, effort=%s, workers=%d"
        % (len(jobs), args.backend, args.effort, args.workers))

    stats = {"pages_ok": 0, "pages_failed": 0, "products": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}
    t_start = time.time()
    lock_failures = []

    def do_page(job):
        eid, page_no, jpg, txt = job
        out_dir = os.path.join(args.data_dir, "pages_json", eid)   # per-edition, never flat
        out_path = os.path.join(out_dir, "p%02d.json" % page_no)
        if os.path.isfile(out_path) and not args.force:
            cached = read_json(out_path) or {}
            return eid, page_no, cached, {"cached": True, "usage": {}}
        t0 = time.time()
        if args.backend == "text":
            with open(txt, "r", encoding="utf-8", errors="replace") as fh:
                obj = {"page_type": "text_layer", "products": [], "text": fh.read()}
            usage = {}
        else:
            obj, usage = vision_page(jpg, txt, api_key, effort=args.effort)
        obj = {"page": page_no, "edition": eid,
               "backend": args.backend,
               "elapsed_s": round(time.time() - t0, 2),
               "usage": usage,
               "page_type": obj.get("page_type"),
               "products": normalise_products(obj, page_no)}
        write_json(out_path, obj)
        return eid, page_no, obj, usage

    all_jobs = []
    for eid, jpgs in jobs:
        for jpg_name in jpgs:
            page_no = int(re.search(r"(\d+)", jpg_name).group(1))
            jpg = os.path.join(args.data_dir, "pages", eid, jpg_name)
            txt = os.path.join(args.data_dir, "pages", eid, "p%02d.txt" % page_no)
            all_jobs.append((eid, page_no, jpg, txt))

    if args.backend == "vision":
        # fail fast on a broken key instead of burning the whole run
        probe = all_jobs[0]
        if not (os.path.isfile(os.path.join(args.data_dir, "pages_json", probe[0], "p%02d.json" % probe[1]))
                and not args.force):
            try:
                vision_page(probe[2], probe[3], api_key, effort=args.effort, retries=1)
                log("  auth probe ok")
            except (ApiError, OSError) as exc:
                raise SystemExit("auth probe failed, aborting before spending on %d pages: %s"
                                 % (len(all_jobs), exc))

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(do_page, j) for j in all_jobs]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            try:
                eid, page_no, obj, usage = fut.result()
            except Exception as exc:                    # noqa: BLE001
                stats["pages_failed"] += 1
                lock_failures.append(str(exc))
                log("  FAIL page: %s" % exc)
                continue
            if usage.get("cached"):
                stats["pages_cached"] = stats.get("pages_cached", 0) + 1
            else:
                stats["pages_ok"] += 1
                stats["products"] += len(obj["products"])
                stats["prompt_tokens"] += usage.get("prompt_tokens") or 0
                stats["completion_tokens"] += usage.get("completion_tokens") or 0
            if i % 10 == 0 or i == len(all_jobs):
                log("  %d/%d pages  (%d products, %d failed)"
                    % (i, len(all_jobs), stats["products"], stats["pages_failed"]))

    stats["seconds"] = round(time.time() - t_start, 1)
    write_json(os.path.join(args.data_dir, "extract_stats.json"), stats)
    log("extract: %d pages ok, %d failed, %d products, %.0fs, tokens %d in / %d out"
        % (stats["pages_ok"], stats["pages_failed"], stats["products"],
           stats["seconds"], stats["prompt_tokens"], stats["completion_tokens"]))
    if stats["pages_failed"]:
        log("  first failures: %s" % lock_failures[:3])


# --------------------------------------------------------------------------
# 6. merge
# --------------------------------------------------------------------------

def load_edition(eid, data_dir):
    """Rebuild an edition from DISK (per-page JSONs), not from this run's memory."""
    pages_dir = os.path.join(data_dir, "pages_json", eid)
    if not os.path.isdir(pages_dir):
        return None
    files = sorted((f for f in os.listdir(pages_dir) if re.match(r"p\d+\.json$", f)),
                   key=lambda n: int(re.search(r"(\d+)", n).group(1)))
    pages, products = [], []
    for fn in files:
        obj = read_json(os.path.join(pages_dir, fn)) or {}
        pages.append({"page": obj.get("page"), "page_type": obj.get("page_type"),
                      "products": len(obj.get("products") or [])})
        products.extend(apply_price_flags(p) for p in (obj.get("products") or []))
    return {"pages": pages, "products": products}


def action_merge(args):
    editions = read_json(os.path.join(args.data_dir, "editions.json"), {}) or {}
    cat = read_json(os.path.join(args.data_dir, "catalogues.json"), {}) or {}
    stores = (read_json(os.path.join(args.data_dir, "stores.json"), {}) or {}).get("stores", [])
    store_meta = {s["slug"]: s for s in stores}

    out_dir = ensure_dir(os.path.join(args.data_dir, "out"))
    edition_docs, store_docs = {}, {}
    jsonl_rows, csv_rows = [], []

    for eid, rec in sorted(editions.items()):
        doc = load_edition(eid, args.data_dir)
        if doc is None:
            continue
        expected = int(rec.get("page_count") or 0)
        covered = [p["page"] for p in doc["pages"]]
        edition_doc = {
            "edition_id": eid,
            "owner_store": rec.get("owner"),
            "stores": rec.get("stores") or [],
            "title": rec.get("title"),
            "dates_text": rec.get("dates_text"),
            "valid_from": rec.get("valid_from"),
            "valid_to": rec.get("valid_to"),
            "pdf_url": rec.get("pdf_url"),
            "page_count": expected,
            "pages_extracted": len(covered),
            "partial": bool(expected and len(covered) < expected),
            "product_count": len(doc["products"]),
            "pages": doc["pages"],
            "products": doc["products"],
        }
        edition_docs[eid] = edition_doc
        write_json(os.path.join(out_dir, "edition-%s.json" % eid), edition_doc)

        for slug in edition_doc["stores"]:
            bucket = store_docs.setdefault(slug, {
                "store": slug,
                "name": (store_meta.get(slug) or {}).get("name"),
                "site": (store_meta.get(slug) or {}).get("site"),
                "editions": [], "products": [],
            })
            bucket["editions"].append({
                "edition_id": eid,
                "title": edition_doc["title"],
                "valid_from": edition_doc["valid_from"],
                "valid_to": edition_doc["valid_to"],
                "partial": edition_doc["partial"],
                "product_count": edition_doc["product_count"],
            })
            for p in doc["products"]:
                row = dict(p)
                row["store"] = slug
                row["edition_id"] = eid
                row["valid_from"] = edition_doc["valid_from"]
                row["valid_to"] = edition_doc["valid_to"]
                bucket["products"].append(row)
                jsonl_rows.append(row)
                csv_rows.append(row)

    for slug, bucket in store_docs.items():
        write_json(os.path.join(out_dir, "store-%s.json" % slug), bucket)

    # product x store jsonl (deduped on the natural key)
    seen, uniq = set(), []
    for row in jsonl_rows:
        key = (row["store"], row["edition_id"], row["page"], row["name"],
               row.get("size") or "", row.get("price"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    with open(os.path.join(out_dir, "products.jsonl"), "w", encoding="utf-8") as fh:
        for row in uniq:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    cols = ["store", "edition_id", "valid_from", "valid_to", "page", "name", "brand", "size",
            "price", "each_price", "is_multi_buy", "multi_buy_qty", "price_text",
            "is_member_price", "non_member_price",
            "was_price", "save_text", "offer_text", "unit_price", "notes"]
    with open(os.path.join(out_dir, "long.csv"), "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in uniq:
            writer.writerow(row)

    total_rows = len(uniq)
    priced = sum(1 for r in uniq if r.get("price") is not None)
    summary = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stores_discovered": len(stores),
        "stores_with_catalogues": cat.get("stores_with_catalogues"),
        "stores_in_output": len(store_docs),
        "unique_editions": len(edition_docs),
        "pages": sum(len(e["pages"]) for e in edition_docs.values()),
        "product_rows": total_rows,
        "priced_rows": priced,
        "fill_rates": {
            "price": pct(priced, total_rows),
            "was_price": pct(sum(1 for r in uniq if r.get("was_price") is not None), total_rows),
            "unit_price": pct(sum(1 for r in uniq if r.get("unit_price")), total_rows),
            "size": pct(sum(1 for r in uniq if r.get("size")), total_rows),
            "multi_buy": pct(sum(1 for r in uniq if r.get("is_multi_buy")), total_rows),
            "member_price": pct(sum(1 for r in uniq if r.get("is_member_price")), total_rows),
        },
        "partial_editions": [e["edition_id"] for e in edition_docs.values() if e["partial"]],
        "placeholder_editions": [e["edition_id"] for e in edition_docs.values()
                                 if e["product_count"] == 0],
        "notes": [
            "Store-edition edges > unique editions: shared state-wide flyers are stored once and "
            "fanned out to every store that lists them.",
            "price = amount payable for one offer group; multi-buy rows carry each_price and "
            "multi_buy_qty so '2 FOR $7.50' is 7.50 total / 3.75 each, not a 7.50 single.",
            "was_price is printed on a minority of rows; absence is data, never synthesised.",
        ],
    }
    write_json(os.path.join(out_dir, "summary.json"), summary)
    log("merge: %d editions, %d stores, %d product rows (%d priced)"
        % (len(edition_docs), len(store_docs), total_rows, priced))
    log("      -> %s" % out_dir)


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


# --------------------------------------------------------------------------
# 7. verify
# --------------------------------------------------------------------------

def action_verify(args):
    out_dir = os.path.join(args.data_dir, "out")
    summary = read_json(os.path.join(out_dir, "summary.json"))
    if not summary:
        raise SystemExit("no summary.json - run: merge")
    editions = read_json(os.path.join(args.data_dir, "editions.json"), {}) or {}

    problems, warnings = [], []
    edition_files = [f for f in os.listdir(out_dir) if f.startswith("edition-") and f.endswith(".json")]
    store_files = [f for f in os.listdir(out_dir) if f.startswith("store-") and f.endswith(".json")]

    if len(edition_files) != summary["unique_editions"]:
        problems.append("edition file count %d != summary %d"
                        % (len(edition_files), summary["unique_editions"]))
    if len(store_files) != summary["stores_in_output"]:
        problems.append("store file count %d != summary %d"
                        % (len(store_files), summary["stores_in_output"]))

    rows = []
    with open(os.path.join(out_dir, "products.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if len(rows) != summary["product_rows"]:
        problems.append("products.jsonl rows %d != summary %d"
                        % (len(rows), summary["product_rows"]))

    for idx, eid in enumerate(sorted(editions)):
        doc = read_json(os.path.join(out_dir, "edition-%s.json" % eid))
        if not doc:
            problems.append("edition %s has no output file" % eid)
            continue
        if doc["pages_extracted"] == 0:
            problems.append("edition %s: 0 pages extracted" % eid)
        if doc["product_count"] == 0 and not doc["partial"]:
            types = {p.get("page_type") for p in doc["pages"]}
            if types and types <= {"cover", "lifestyle", "other"}:
                # real case: a 1-page "PLEASE SEE IN-STORE FOR THIS WEEK'S SPECIALS"
                # placeholder PDF. Nothing to extract - not a failure.
                warnings.append("edition %s: placeholder flyer (page_type %s, 0 products)"
                                % (eid, "/".join(sorted(t or "?" for t in types))))
            else:
                problems.append("edition %s: 0 products across %d pages (extraction produced nothing)"
                                % (eid, doc["page_count"]))
        if doc["partial"]:
            warnings.append("edition %s PARTIAL: %d/%d pages (--pages run?)"
                            % (eid, doc["pages_extracted"], doc["page_count"]))
        vf, vt = doc.get("valid_from"), doc.get("valid_to")
        if vf and vt and vt < vf:
            warnings.append("edition %s: valid_to %s < valid_from %s (stale/odd dates on tile)"
                            % (eid, vt, vf))
        for p in doc["products"]:
            if p.get("price") is None:
                warnings.append("edition %s p%s: unpriced row %r" % (eid, p.get("page"), p.get("name"))[:160])
            if p.get("was_price") is not None and p.get("price") is not None \
                    and p["was_price"] <= p["price"]:
                problems.append("edition %s p%s: was_price %s <= price %s (%r)"
                                % (eid, p.get("page"), p["was_price"], p["price"], p.get("name")))

    # a uniformly-zero field is the failure every count-based check misses
    if rows and all(r.get("was_price") is None for r in rows):
        warnings.append("was_price is null on 100% of rows - confirm that matches the flyers "
                        "before treating it as real")
    multi = [r for r in rows if r.get("is_multi_buy")]
    bad_multi = [r for r in multi if not r.get("multi_buy_qty") or not r.get("each_price")]
    if bad_multi:
        problems.append("%d multi-buy rows missing qty/each_price" % len(bad_multi))

    warn_high = sum(1 for r in rows if (r.get("price") or 0) > 200)
    if warn_high:
        warnings.append("%d rows with price > 200 (usually real: passes, bulk packs)" % warn_high)

    log("verify: %d editions, %d stores, %d rows"
        % (summary["unique_editions"], summary["stores_in_output"], len(rows)))
    log("        fill: price %.1f%%, was_price %.1f%%, unit_price %.1f%%, multi-buy %.1f%%"
        % (summary["fill_rates"]["price"], summary["fill_rates"]["was_price"],
           summary["fill_rates"]["unit_price"], summary["fill_rates"]["multi_buy"]))
    for w in warnings[:12]:
        log("  WARN %s" % w)
    if len(warnings) > 12:
        log("  ... %d more warnings" % (len(warnings) - 12))
    for p in problems:
        log("  FAIL %s" % p)
    if problems:
        log("verify: %d problem(s)" % len(problems))
        return 1
    log("verify: OK (%d warnings)" % len(warnings))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    parser = argparse.ArgumentParser(add_help=True, description="FoodWorks AU catalogue scraper")
    parser.add_argument("action", nargs="?", default="help",
                        choices=["help", "stores", "discover", "fetch", "render",
                                 "extract", "merge", "verify", "all"])
    parser.add_argument("--data-dir", default=os.environ.get("FOODWORKS_DATA_DIR") or "foodworks-au-data")
    parser.add_argument("--stores", default=None, help="comma separated store slugs")
    parser.add_argument("--limit", type=int, default=None, help="first N editions")
    parser.add_argument("--pages", type=int, default=None, help="first N pages per edition")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--effort", default="low", choices=["low", "high"])
    parser.add_argument("--backend", default="vision", choices=["vision", "text"])
    parser.add_argument("--keep-pdf", action="store_true")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (only if the CA bundle is broken)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.insecure:
        global SSL_CTX
        SSL_CTX = build_ssl_context(insecure=True)
        log("WARNING: TLS verification disabled (--insecure)")

    args.data_dir = os.path.abspath(args.data_dir)
    ensure_dir(args.data_dir)

    if args.action == "help":
        print(CHEAT)
        return 0
    if args.action == "stores":
        return action_stores(args) or 0
    if args.action == "discover":
        action_discover(args)
        return 0
    if args.action == "fetch":
        return action_fetch(args) or 0
    if args.action == "render":
        return action_render(args) or 0
    if args.action == "extract":
        return action_extract(args) or 0
    if args.action == "merge":
        return action_merge(args) or 0
    if args.action == "verify":
        return action_verify(args)

    # all
    action_stores(args)
    action_discover(args)
    action_fetch(args)
    action_render(args)
    action_extract(args)
    action_merge(args)
    rc = action_verify(args)
    if not args.keep_pdf:
        pdf_dir = os.path.join(args.data_dir, "pdf")
        for name in os.listdir(pdf_dir) if os.path.isdir(pdf_dir) else []:
            if name.endswith(".pdf"):
                os.remove(os.path.join(pdf_dir, name))
        log("cleanup: PDFs removed (use --keep-pdf to retain)")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
