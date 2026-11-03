# #!/usr/bin/env python3
# """
# SPAR Australia (MyFoodLink Shopfront) store-wise product scraper.

# Source of truth:  https://shop.spar.com.au/            <- store chooser (the ONLY store list)
# Per store:        https://<slug>.shop.spar.com.au/     <- one storefront per store
# Specials listing: https://<slug>.shop.spar.com.au/search?q[]=special:1&page=N   (48/page)
# Full catalogue:   /search/sidebar_facets.json  -> 27 department listings, each paginated

# Output layout mirrors the Harris Farm AU scraper:
#     stores.json                 store manifest (discovered, never hardcoded)
#     stores/<id>-<slug>.json     one file per store, all products
#     products.jsonl              one line per product, prices nested per store
#     long.csv                    long rows: product x store
#     summary.json                declared vs fetched vs failed, fill rates
#     run_manifest.json           resume state
#     raw/<slug>/...              raw HTML pages (rebuild without refetching)

# Pure stdlib. Works on macOS and Windows (cmd.exe).

#     python spar_au_scraper.py                # cheat sheet
#     python spar_au_scraper.py stores         # discover stores -> stores.json
#     python spar_au_scraper.py diagnose       # one request, says reachable / blocked
#     python spar_au_scraper.py fetch --mode specials
#     python spar_au_scraper.py fetch --mode catalog  --workers 6
#     python spar_au_scraper.py fetch --mode all
#     python spar_au_scraper.py build          # raw -> per-store json + jsonl + csv
#     python spar_au_scraper.py all            # stores -> fetch -> build
#     python spar_au_scraper.py verify         # structural checks, non-zero exit on real problems
# """

# import argparse
# import csv
# import errno
# import glob
# import html as htmllib
# import json
# import os
# import queue
# import random
# import re
# import sys
# import threading
# import time
# import urllib.error
# import urllib.request
# from datetime import datetime, timezone

# # ---------------------------------------------------------------- config

# UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
#       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# CHOSER = "https://shop.spar.com.au/"
# PER_PAGE = 48                     # fixed by the platform, no override
# TRANSIENT_CODES = {400, 403, 408, 429, 500, 502, 503, 504}
# MAX_TRANSIENT_ATTEMPTS = 5
# MAX_BLOCK_ATTEMPTS = 2

# DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out-spar-au")

# # Known, verified boundary of this source (checked 2026-10-14): the shopfront
# # renders image URLs with the opaque CDN path truncated in the middle -
# #   .../imagery/assets/derivations/icon/256/256/true/eyJpZC...ZSJ9?signature=...
# # The literal "..." is in the HTML and such a URL answers 403, so no usable image
# # URL can be collected over plain HTTP. We export image_alt + the product page URL
# # instead of inventing a URL. A JS/browser session is needed for real images.
# IMAGE_NOTE = ("image_url is null by design: the shopfront truncates the CDN image path "
#               "server-side (literal '...' in the HTML, such URLs 403). Use image_alt and "
#               "url, or scrape images from a JS session.")


# # ---------------------------------------------------------------- errors

# class BlockedResponse(Exception):
#     """Body was empty / HTML bot-wall / not what we asked for. NOT transient."""


# class HttpFail(Exception):
#     def __init__(self, status, url, body_head=""):
#         super().__init__("HTTP %s %s :: %s" % (status, url, body_head[:160]))
#         self.status = status
#         self.url = url
#         self.body_head = body_head


# # ---------------------------------------------------------------- http

# _local = threading.local()


# def _opener():
#     if not hasattr(_local, "op"):
#         _local.op = urllib.request.build_opener()
#     return _local.op


# def http_get(url, timeout=60, accept="text/html"):
#     """One GET with bounded retries. Raises BlockedResponse / HttpFail."""
#     last = None
#     block_tries = 0
#     tries = 0
#     while tries < max(MAX_TRANSIENT_ATTEMPTS, MAX_BLOCK_ATTEMPTS):
#         tries += 1
#         req = urllib.request.Request(url, headers={
#             "User-Agent": UA,
#             "Accept": accept,
#             "Accept-Language": "en-AU,en;q=0.9",
#         })
#         try:
#             with _opener().open(req, timeout=timeout) as f:
#                 raw = f.read()
#             if not raw:
#                 block_tries += 1
#                 last = BlockedResponse("empty body from %s" % url)
#                 if block_tries >= MAX_BLOCK_ATTEMPTS:
#                     raise last
#                 time.sleep(1 + random.random())
#                 continue
#             return raw.decode("utf-8", "ignore")
#         except urllib.error.HTTPError as e:
#             body = ""
#             try:
#                 body = e.read().decode("utf-8", "ignore")
#             except Exception:
#                 pass
#             if e.code in TRANSIENT_CODES and tries < MAX_TRANSIENT_ATTEMPTS:
#                 last = HttpFail(e.code, url, body)
#                 time.sleep(min(30, (2 ** tries) + random.random() * 1.5))
#                 continue
#             raise HttpFail(e.code, url, body)
#         except (urllib.error.URLError, OSError) as e:
#             last = e
#             if tries < MAX_TRANSIENT_ATTEMPTS:
#                 time.sleep(min(30, (2 ** tries) + random.random() * 1.5))
#                 continue
#             raise
#     raise last if isinstance(last, Exception) else RuntimeError("unreachable")


# # ---------------------------------------------------------------- parsing

# CARD_SPLIT = re.compile(r'<div class="TalkerGrid__Item" role="listitem">')


# def _text(frag):
#     return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", frag))).strip()


# def clean_name(raw, size=None):
#     """Normalise the merchant's display name.

#     Seen in the live data: zero-width spaces, trailing commas, and one unexpanded
#     template '[<name> , Null]' where the size placeholder never got substituted.
#     Returns (name, size) - size is only ever filled in, never overwritten.
#     """
#     n = (raw or "").replace("\u200b", "").replace("\ufeff", "").strip()
#     m = re.match(r"^\[(.*?)\s*,\s*([^\[\]]*)\]$", n, re.S)
#     if m:
#         base, tail = m.group(1).strip(), m.group(2).strip()
#         n = base
#         if not size and tail and tail.lower() not in ("null", "nil", "none", ""):
#             size = tail
#     n = re.sub(r"[\s,]+$", "", n).strip()
#     return n, size


# def span_text_at(s, i):
#     """Text of the <span> that starts at s.index(i), walking nested spans properly.

#     The sticker label nests an inner span, so a lazy `.*?</span>` regex stops at the
#     inner close tag and you lose the saving amount ('save' instead of 'save 99c').
#     """
#     j = s.find(">", i)
#     if j == -1:
#         return None
#     depth, k = 1, j + 1
#     while depth > 0:
#         no = s.find("<span", k)
#         nc = s.find("</span>", k)
#         if nc == -1:
#             break
#         if no != -1 and no < nc:
#             depth += 1
#             k = no + 5
#         else:
#             depth -= 1
#             k = nc + 7
#     inner = s[j + 1:k - 7] if depth == 0 else s[j + 1:k]
#     return _text(inner)


# def _money(s):
#     if s is None:
#         return None
#     s = re.sub(r"[^0-9.]", "", s)
#     try:
#         return float(s) if s else None
#     except ValueError:
#         return None


# def parse_card(chunk):
#     """Parse one TalkerGrid item. Returns None for non-product cards (catalogue promos)."""
#     name_m = re.search(r'<span class="talker__product-name">(.*?)</span>', chunk, re.S)
#     if not name_m:
#         return None                                   # catalogue edition tile, not a product
#     raw_name = _text(name_m.group(1))

#     size_m = re.search(r'<span class="weak size talker__name__size">(.*?)</span>', chunk, re.S)
#     size = _text(size_m.group(1)) if size_m else None
#     name, size = clean_name(raw_name, size)

#     title_m = re.search(r'<div class="talker__name talker__section" title="([^"]*)"', chunk)
#     full_name = htmllib.unescape(title_m.group(1)) if title_m else ""
#     full_name = re.sub(r"\s+", " ", full_name.replace("\u200b", "")).strip()
#     if not full_name or full_name.startswith("["):
#         full_name = name + (" " + size if size else "")

#     url_m = re.search(r'<a href="(/lines/[^"]+)"', chunk)
#     alt_m = re.search(r'<img[^>]+alt="([^"]*)"', chunk)
#     pid_m = re.search(r"&quot;pid&quot;:&quot;([0-9a-f]+)&quot;", chunk)
#     lid_m = re.search(r'id="line_([0-9a-f]+)"', chunk)

#     sell_m = re.search(r'<strong class="price__sell"[^>]*>([^<]+)</strong>', chunk)
#     units_m = re.search(r'<span class="price__units weak">\s*([^<]+?)\s*</span>', chunk, re.S)
#     was_m = re.search(r'<span class="talker__prices__was weak">\s*was \$([\d.,]+)\s*</span>', chunk, re.S)
#     up_m = re.search(r'talker__prices__comparison--UnitPrice">([^<]+)</span>', chunk)
#     save_m = re.search(r"talker__sticker--Saving.*?<span class=\"talker__sticker__label\">(.*?)</span>",
#                        chunk, re.S)
#     cls_m = re.search(r'class="talker([^"]*)"', chunk)
#     cls = cls_m.group(1) if cls_m else ""
#     ctrl_m = re.search(r'data-line-control="([^"]+)"', chunk)

#     qty = None
#     if ctrl_m:
#         try:
#             qty = (json.loads(htmllib.unescape(ctrl_m.group(1))) or {}).get("quantity")
#         except Exception:
#             qty = None

#     price = _money(sell_m.group(1)) if sell_m else None
#     was = _money(was_m.group(1)) if was_m else None
#     up_text = htmllib.unescape(re.sub(r"<[^>]+>", " ", up_m.group(1))).strip().replace("  ", " ") \
#         if up_m else None
#     up_val = up_unit = up_base = None
#     if up_text:
#         m = re.search(r"\$([\d.]+)\s*per\s*(.+)$", up_text)
#         if m:
#             up_val = _money("$" + m.group(1))
#             up_unit = m.group(2).strip()
#             bm = re.search(r"per\s*([\d.]+)\s*([a-zA-Z]+)", up_text)
#             if bm:
#                 up_base = "%s%s" % (bm.group(1), bm.group(2))
#     save_text = None
#     promo_text = None
#     deal_text = None
#     deal_url = None
#     # Stickers are siblings, and they are NOT all <span>: a multi-buy/deal sticker is an
#     # <a href="/deals/<id>"> with its own label, e.g. "2 for $5". Split on the sticker
#     # boundary and classify by THAT sticker's own classes - otherwise a tag sticker's
#     # label ("Gluten Free") lands in save_text and the deal label is dropped entirely.
#     opens = list(re.finditer(r'<(?:span|a) class="talker__sticker(?: ([^"]*))?"'
#                              r'(?:\s+href="([^"]*)")?', chunk))
#     for i, om in enumerate(opens):
#         scls = om.group(1) or ""
#         href = om.group(2)
#         end = opens[i + 1].start() if i + 1 < len(opens) else len(chunk)
#         seg = chunk[om.start():end]
#         li = seg.find("talker__sticker__label")
#         if li == -1:
#             continue
#         txt = span_text_at(seg, li)
#         if not txt:
#             continue
#         if "talker__sticker--Saving" in scls:
#             save_text = save_text or txt
#         elif "talker__sticker--Deal" in scls or "talker__sticker--Discount" in scls \
#                 or "talker__sticker--Special" in scls:
#             deal_text = deal_text or txt
#             if href and not deal_url:
#                 deal_url = htmllib.unescape(href)
#         else:
#             promo_text = promo_text or txt

#     return {
#         "pid": pid_m.group(1) if pid_m else None,
#         "line_id": lid_m.group(1) if lid_m else None,
#         "name": name,
#         "raw_name": raw_name,
#         "size": size,
#         "full_name": full_name,
#         "url": url_m.group(1) if url_m else None,
#         "image_url": None,            # see IMAGE_NOTE below - source truncates it
#         "image_alt": htmllib.unescape(alt_m.group(1)) if alt_m else None,
#         "price": price,
#         "was_price": was,
#         "saving": round(was - price, 2) if (was is not None and price is not None) else None,
#         "price_units": _text(units_m.group(1)) if units_m else None,
#         "unit_price": up_val,
#         "unit_price_text": up_text,
#         "unit_price_unit": up_unit,
#         "unit_price_base": up_base,
#         "save_text": save_text,
#         "promo_text": promo_text,
#         "deal_text": deal_text,
#         "deal_url": deal_url,
#         "is_special": ("talker--Special" in cls) or bool(re.search(r'\bspecial\b', cls)),
#         "tags": sorted(set(re.findall(r"talker--Tag-([a-z0-9\-]+)", chunk))),
#         "is_weighed": bool(qty and qty.get("unit")),
#         "qty_unit": (qty or {}).get("unit"),
#     }


# def parse_listing(page_html):
#     return [c for c in (parse_card(ch) for ch in CARD_SPLIT.split(page_html)[1:]) if c]


# def results_count(page_html):
#     m = re.search(r"<span>([\d,]+) results</span>", page_html)
#     return int(m.group(1).replace(",", "")) if m else None


# def max_page(page_html):
#     pgs = [int(x) for x in re.findall(r"[?&]page=(\d+)", page_html)]
#     return max(pgs) if pgs else 1


# def page_has_products(page_html):
#     return 'talker__product-name' in page_html


# # ---------------------------------------------------------------- store discovery

# STORECARD_SPLIT = re.compile(r'<div class="StoreCard(?:\s[^"]*)?">')


# def parse_store_cards(page_html):
#     out = []
#     for chunk in STORECARD_SPLIT.split(page_html)[1:]:
#         idm = re.search(r'href="/([0-9a-f]{24})/i_choose_you"', chunk)
#         if not idm:
#             continue
#         nm = re.search(r'<span class="StoreCard__Name">(.*?)</span>', chunk, re.S)
#         dt = re.search(r'<span class="StoreCard__Details">(.*?)</span>', chunk, re.S)
#         hours = None
#         addr_lines = []
#         if dt:
#             parts = [x.strip() for x in re.split(r"<br\s*/?>", dt.group(1))]
#             for p in parts:
#                 t = _text(p)
#                 if not t:
#                     continue
#                 if t.lower().startswith("open today"):
#                     hours = t
#                 else:
#                     addr_lines.append(t)
#         tel = re.search(r'href="tel:([^"]+)"', chunk)
#         directions = re.search(r'href="(http://maps\.google\.com[^"]+)"', chunk)
#         cat = re.search(r'href="(https://[a-z0-9\-]+\.shop\.spar\.com\.au/catalogues)"', chunk)
#         cta = re.search(r'href="/%s/i_choose_you"[^>]*>(.{0,3000}?)</a>' % idm.group(1), chunk, re.S)
#         out.append({
#             "store_id": idm.group(1),
#             "name": _text(nm.group(1)) if nm else None,
#             "hours_today": hours,
#             "address_lines": addr_lines,
#             "address": ", ".join(addr_lines),
#             "phone": tel.group(1) if tel else None,
#             "directions_url": htmllib.unescape(directions.group(1)) if directions else None,
#             "catalogue_url": cat.group(1) if cat else None,
#             "chooser_cta": _text(cta.group(1)) if cta else None,
#             "chooser_url": CHOSER + idm.group(1) + "/i_choose_you",
#         })
#     return out


# def _final_url(url):
#     """Follow redirects manually, return the final URL (chooser -> canonical store subdomain)."""
#     class NoRedirect(urllib.request.HTTPRedirectHandler):
#         def redirect_request(self, req, fp, code, msg, headers, newurl):
#             raise _Redirect(newurl)

#     class _Redirect(Exception):
#         def __init__(self, url):
#             self.url = url
#             super().__init__(url)

#     op = urllib.request.build_opener(NoRedirect)
#     try:
#         op.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=45)
#     except _Redirect as r:
#         return r.url
#     except urllib.error.HTTPError as e:
#         if e.code in (301, 302, 303, 307, 308):
#             return e.headers.get("Location")
#         raise
#     return url


# STATE_EXPAND = {
#     "new south wales": "NSW", "queensland": "QLD", "victoria": "VIC",
#     "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
#     "northern territory": "NT", "australian capital territory": "ACT",
# }


# def usps_codes(text):
#     """Pull (state, postcode) out of an Australian address line."""
#     t = text or ""
#     m = re.search(r"\b(NSW|QLD|VIC|SA|WA|TAS|NT|ACT)\b[\s,]*(\d{4})", t)
#     if m:
#         return m.group(1), m.group(2)
#     m = re.search(r"\b(NSW|QLD|VIC|SA|WA|TAS|NT|ACT)\b", t)
#     if m:
#         return m.group(1), None
#     low = t.lower()
#     for k, v in STATE_EXPAND.items():
#         if k in low:
#             pc = re.search(r"\b(\d{4})\b", t)
#             return v, (pc.group(1) if pc else None)
#     return None, None


# def discover_stores(outdir=None):
#     page = http_get(CHOSER)
#     cards = parse_store_cards(page)
#     stores = []
#     for c in cards:
#         final = _final_url(c["chooser_url"]) or ""
#         m = re.match(r"https?://([a-z0-9\-]+)\.shop\.spar\.com\.au", final)
#         sub = m.group(1) if m else None
#         state, postcode = usps_codes(c["address"])
#         st = dict(c)
#         st.update({
#             "slug": sub,
#             "subdomain": ("%s.shop.spar.com.au" % sub) if sub else None,
#             "homepage": ("https://%s.shop.spar.com.au/" % sub) if sub else None,
#             "state": state,
#             "postcode": postcode,
#         })
#         if not sub:
#             st["resolve_error"] = "no subdomain from %s (got %r)" % (c["chooser_url"], final)
#         stores.append(st)
#     if outdir:
#         write_json(os.path.join(outdir, "stores.json"), {
#             "source": CHOSER,
#             "fetched_at": utcnow(),
#             "count": len(stores),
#             "stores": stores,
#         })
#     return stores


# # ---------------------------------------------------------------- io helpers

# def utcnow():
#     return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# def ensure_dir(p):
#     try:
#         os.makedirs(p)
#     except OSError as e:
#         if e.errno != errno.EEXIST:
#             raise


# def write_json(path, obj):
#     ensure_dir(os.path.dirname(path))
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as f:
#         json.dump(obj, f, ensure_ascii=False, indent=1)
#         f.flush()
#         os.fsync(f.fileno())
#     os.replace(tmp, path)


# def read_json(path, default=None):
#     try:
#         with open(path, encoding="utf-8") as f:
#             return json.load(f)
#     except Exception:
#         return default


# def load_manifest(outdir):
#     return read_json(os.path.join(outdir, "run_manifest.json"),
#                      {"done": {}, "started_at": utcnow()})


# def save_manifest(outdir, man):
#     man["updated_at"] = utcnow()
#     write_json(os.path.join(outdir, "run_manifest.json"), man)


# # ---------------------------------------------------------------- fetch

# def listing_urls(mode, slug, dept_slug=None, page=1):
#     base = "https://%s.shop.spar.com.au" % slug
#     if dept_slug:
#         q = "q%%5B%%5D=category%%3A%s" % dept_slug
#     elif mode == "specials":
#         q = "q%5B%5D=special%3A1"
#     else:
#         q = ""
#     sep = "&" if q else "?"
#     if page > 1:
#         return "%s/search?%s%spage=%d" % (base, q, sep, page)
#     return "%s/search?%s" % (base, q)


# def fetch_departments(slug):
#     """Department facets declared by the store itself."""
#     url = "https://%s.shop.spar.com.au/search/sidebar_facets.json" % slug
#     data = json.loads(http_get(url, accept="application/json"))
#     depts = []
#     for sec in data.get("more_sections", []):
#         if sec.get("group") != "categories":
#             continue
#         for f in sec.get("facets", []):
#             u = f.get("url", "")
#             m = re.search(r"category%3A([a-z0-9\-]+)", u)
#             if m:
#                 depts.append({"slug": m.group(1), "name": f.get("name"), "url": u})
#     return depts


# def raw_path(outdir, slug, tag, page):
#     return os.path.join(outdir, "raw", slug, "%s-p%03d.html" % (tag, page))


# def fetch_page(outdir, slug, tag, url, page, keep_raw=True, force=False):
#     p = raw_path(outdir, slug, tag, page)
#     if os.path.exists(p) and not force:
#         with open(p, encoding="utf-8") as f:
#             body = f.read()
#         if body:
#             return body, "cache"
#     body = http_get(url)
#     if not page_has_products(body) and page <= 1:
#         raise BlockedResponse("page 1 of %s has no product cards (url=%s)" % (slug, url))
#     if keep_raw:
#         ensure_dir(os.path.dirname(p))
#         tmp = p + ".tmp"
#         with open(tmp, "w", encoding="utf-8") as f:
#             f.write(body)
#         os.replace(tmp, p)
#     return body, "net"


# def fetch_store(outdir, store, mode="specials", workers=4, keep_raw=True, force=False,
#                 max_pages=None, log=print):
#     """Fetch every page a store needs. Returns per-store result dict."""
#     slug = store.get("slug")
#     man = None
#     jobs = []          # (tag, page, url)

#     if mode in ("specials", "all"):
#         u1 = listing_urls("specials", slug, page=1)
#         body, _ = fetch_page(outdir, slug, "specials", u1, 1, keep_raw, force)
#         declared = results_count(body)
#         last = max_page(body)
#         if max_pages:
#             last = min(last, max_pages)
#         for p in range(1, last + 1):
#             jobs.append(("specials", p, listing_urls("specials", slug, page=p)))
#         log("  %-16s specials: declared=%s pages=%d" % (slug, declared, last))
#     if mode in ("catalog", "all"):
#         try:
#             depts = fetch_departments(slug)
#         except Exception as e:
#             log("  ! %s department facets failed: %s" % (slug, e))
#             depts = []
#         for d in depts:
#             try:
#                 u1 = listing_urls("catalog", slug, dept_slug=d["slug"], page=1)
#                 body, _ = fetch_page(outdir, slug, "cat-" + d["slug"], u1, 1, keep_raw, force)
#                 last = max_page(body)
#                 if max_pages:
#                     last = min(last, max_pages)
#                 for p in range(1, last + 1):
#                     jobs.append(("cat-" + d["slug"], p,
#                                  listing_urls("catalog", slug, dept_slug=d["slug"], page=p)))
#             except Exception as e:
#                 log("  ! %s dept %s: %s" % (slug, d["slug"], e))
#         log("  %-16s catalogue: %d departments, %d pages queued" % (slug, len(depts), len(jobs)))

#     if man is None:
#         man = {}

#     q = queue.Queue()
#     for j in jobs:
#         q.put(j)
#     results = {"ok": 0, "failed": 0, "errors": [], "pages": len(jobs), "declared": None}
#     lock = threading.Lock()

#     def worker():
#         while True:
#             try:
#                 tag, page, url = q.get_nowait()
#             except queue.Empty:
#                 return
#             key = "%s|%s|%s|%d" % (slug, mode, tag, page)
#             try:
#                 body, src = fetch_page(outdir, slug, tag, url, page, keep_raw, force)
#                 n = len(parse_listing(body))
#                 with lock:
#                     results["ok"] += 1
#                     man[key] = {"status": "ok", "cards": n, "at": utcnow()}
#                 log("  %-16s %-28s p%03d: %d cards (%s)" % (slug, tag, page, n, src))
#             except Exception as e:
#                 with lock:
#                     results["failed"] += 1
#                     results["errors"].append("%s: %s" % (key, e))
#                     man[key] = {"status": "error", "error": str(e)[:240], "at": utcnow()}
#                 log("  ! %-16s %-28s p%03d FAILED: %s" % (slug, tag, page, e))
#             finally:
#                 q.task_done()
#                 if results["failed"] >= 3 and results["ok"] == 0:
#                     log("  !! %s: %d failures, 0 successes - aborting this store (transport suspect)"
#                         % (slug, results["failed"]))
#                     while True:
#                         try:
#                             q.get_nowait()
#                             q.task_done()
#                         except queue.Empty:
#                             break

#     threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
#     t0 = time.time()
#     for t in threads:
#         t.start()
#     for t in threads:
#         t.join()
#     results["seconds"] = round(time.time() - t0, 1)
#     return results, man


# # ---------------------------------------------------------------- build

# def store_file_name(store):
#     return "%s-%s.json" % (store["store_id"], store.get("slug") or "unknown")


# def store_products(outdir, store, mode="specials", keep_depts=True):
#     """Re-parse every cached page for one store. Returns (products, meta)."""
#     slug = store["slug"]
#     prods = {}
#     order = []
#     depts_seen = {}
#     declared_by_tag = {}
#     pages_ok = pages_missing = 0
#     stop = "declared-reached"

#     tags = []
#     if mode in ("specials", "all"):
#         tags.append(("specials", "specials"))
#     if mode in ("catalog", "all"):
#         try:
#             for d in fetch_departments(slug):
#                 tags.append(("cat-" + d["slug"], d["name"]))
#         except Exception:
#             for fn in sorted(os.listdir(os.path.join(outdir, "raw", slug))):
#                 m = re.match(r"(cat-[a-z0-9\-]+)-p\d+\.html$", fn)
#                 if m and (m.group(1), None) not in tags:
#                     tags.append((m.group(1), m.group(1)[4:]))
#     if not tags:
#         tags = [("specials", "specials")]

#     for tag, dept_name in tags:
#         p = 1
#         while p <= 400:
#             fp = raw_path(outdir, slug, tag, p)
#             if not os.path.exists(fp):
#                 if p == 1:
#                     pages_missing += 1
#                 break
#             with open(fp, encoding="utf-8") as f:
#                 body = f.read()
#             pages_ok += 1
#             if p == 1:
#                 n = results_count(body)
#                 if n is not None:
#                     declared_by_tag[tag] = n
#             cards = parse_listing(body)
#             if not cards:
#                 if p > 1:
#                     break
#             for c in cards:
#                 key = c.get("pid") or c.get("url") or (c.get("full_name"), c.get("price"))
#                 if key in prods:
#                     if keep_depts and dept_name and dept_name not in prods[key]["departments"]:
#                         prods[key]["departments"].append(dept_name)
#                     if tag not in prods[key]["source_listings"]:
#                         prods[key]["source_listings"].append(tag)
#                     continue
#                 c = dict(c)
#                 c["departments"] = [dept_name] if dept_name else []
#                 c["source_listings"] = [tag]
#                 prods[key] = c
#                 order.append(key)
#             p += 1
#         else:
#             stop = "page-guard"

#     # drop product rows with no pid (cannot join across stores)
#     products = [prods[k] for k in order]
#     declared_union = sum(declared_by_tag.values())
#     declared_main = max(declared_by_tag.values()) if declared_by_tag else None
#     if declared_main and len(products) < declared_main:
#         stop = "declared-short(%d/%d)" % (len(products), declared_main)
#     meta = {
#         "counts": {
#             "products": len(products),
#             "with_pid": sum(1 for x in products if x.get("pid")),
#             "with_was_price": sum(1 for x in products if x.get("was_price") is not None),
#             "with_unit_price": sum(1 for x in products if x.get("unit_price") is not None),
#             "weighed": sum(1 for x in products if x.get("is_weighed")),
#             "specials": sum(1 for x in products if x.get("is_special")),
#         },
#         "listings": {"declared_results": declared_main,
#                      "declared_by_listing": declared_by_tag,
#                      "declared_union_of_listings": declared_union,
#                      "pages_read": pages_ok,
#                      "pages_missing": pages_missing, "stop_reason": stop},
#         "departments": sorted(set(d for x in products for d in x["departments"])),
#     }
#     return products, meta


# def name_key(s):
#     return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# def build(outdir, stores, mode="specials", log=print):
#     ensure_dir(os.path.join(outdir, "stores"))
#     per_store = []
#     by_pid = {}
#     docs = []
#     for st in stores:
#         if not st.get("slug"):
#             log("  ! skipping %s (%s)" % (st.get("name"), st.get("resolve_error")))
#             continue
#         rawdir = os.path.join(outdir, "raw", st["slug"])
#         if not os.path.isdir(rawdir):
#             log("  - %-16s no raw pages, skipped" % st["slug"])
#             continue
#         products, meta = store_products(outdir, st, mode=mode)
#         if not products:
#             log("  ! %-16s parsed 0 products (raw dir exists) - refusing to write empty file"
#                 % st["slug"])
#             per_store.append({"store_id": st["store_id"], "slug": st["slug"],
#                               "name": st.get("name"), "products": 0, "note": "empty"})
#             continue
#         doc = {
#             "store": {
#                 "store_id": st["store_id"], "slug": st["slug"],
#                 "name": st.get("name"), "subdomain": st.get("subdomain"),
#                 "homepage": st.get("homepage"), "address": st.get("address"),
#                 "address_lines": st.get("address_lines"), "state": st.get("state"),
#                 "postcode": st.get("postcode"), "phone": st.get("phone"),
#                 "hours_today": st.get("hours_today"),
#                 "directions_url": st.get("directions_url"),
#                 "catalogue_url": st.get("catalogue_url"),
#             },
#             "source": {
#                 "platform": "myfoodlink-shopfront",
#                 "mode": mode,
#                 "specials_url": "https://%s.shop.spar.com.au/specials" % st["slug"] if mode != "catalog" else None,
#                 "all_products_url": "https://%s.shop.spar.com.au/search" % st["slug"],
#                 "page_size": PER_PAGE,
#             },
#             "fetched_at": utcnow(),
#             "listed_url": "https://%s.shop.spar.com.au/" % st["slug"],
#             "counts": meta["counts"],
#             "listings": meta["listings"],
#             "departments": meta["departments"],
#             "notes": [IMAGE_NOTE],
#             "products": products,
#         }
#         write_json(os.path.join(outdir, "stores", store_file_name(st)), doc)
#         docs.append((st, products))
#         log("  + %-16s %5d products -> stores/%s" % (st["slug"], len(products),
#                                                      store_file_name(st)))
#         per_store.append({"store_id": st["store_id"], "slug": st["slug"], "name": st.get("name"),
#                           "state": st.get("state"), "postcode": st.get("postcode"),
#                           "products": len(products), "specials": meta["counts"]["specials"],
#                           "with_was_price": meta["counts"]["with_was_price"],
#                           "file": "stores/" + store_file_name(st)})
#         for p in products:
#             # Join anchor = the global product slug (100% fill on every store).
#             # pid (md5 product id) is only emitted by SOME stores, so it rides along
#             # per store-price instead of being the grouping key.
#             key = p.get("url") or (("pid:" + p["pid"]) if p.get("pid") else None)
#             if not key:
#                 continue
#             row = by_pid.setdefault(key, {
#                 "key": key, "pid": p.get("pid"), "slug": key.replace("/lines/", ""),
#                 "name": p["name"], "size": p["size"], "full_name": p["full_name"],
#                 "name_key": name_key(p["full_name"]),
#                 "price_units": p["price_units"], "image_url": p["image_url"],
#                 "image_alt": p["image_alt"],
#                 "departments": [], "stores": {},
#             })
#             if row["pid"] is None and p.get("pid"):
#                 row["pid"] = p["pid"]
#             for d in p["departments"]:
#                 if d not in row["departments"]:
#                     row["departments"].append(d)
#             row["stores"][st["store_id"]] = {
#                 "slug": st["slug"], "name": st.get("name"),
#                 "price": p["price"], "was_price": p["was_price"], "saving": p["saving"],
#                 "save_text": p["save_text"], "promo_text": p["promo_text"],
#                 "deal_text": p["deal_text"],
#                 "deal_url": ("https://%s.shop.spar.com.au%s" % (st["slug"], p["deal_url"]))
#                 if p["deal_url"] else None,
#                 "unit_price": p["unit_price"],
#                 "unit_price_text": p["unit_price_text"], "is_special": p["is_special"],
#                 "tags": p["tags"], "line_id": p["line_id"], "pid": p.get("pid"),
#                 "product_page": ("https://%s.shop.spar.com.au%s" % (st["slug"], p["url"]))
#                 if p["url"] else None,
#             }

#     # products.jsonl  (one line per product, prices nested per store)
#     jl = os.path.join(outdir, "products.jsonl")
#     with open(jl, "w", encoding="utf-8") as f:
#         for k in sorted(by_pid, key=lambda k: by_pid[k]["full_name"].lower()):
#             f.write(json.dumps(by_pid[k], ensure_ascii=False) + "\n")

#     # long.csv  (product x store)
#     csvp = os.path.join(outdir, "long.csv")
#     n_rows = 0
#     with open(csvp, "w", newline="", encoding="utf-8") as f:
#         w = csv.writer(f)
#         w.writerow(["key", "slug", "pid", "name", "size", "price_units", "store_id",
#                     "store_slug", "store_name", "price", "was_price", "saving", "save_text",
#                     "deal_text", "promo_text", "unit_price", "unit_price_unit", "is_special",
#                     "tags", "departments", "deal_url", "product_page"])
#         for k in sorted(by_pid, key=lambda k: by_pid[k]["full_name"].lower()):
#             row = by_pid[k]
#             for sid, s in sorted(row["stores"].items(), key=lambda kv: kv[1]["slug"] or ""):
#                 w.writerow([row["key"], row["slug"], row["pid"] or s.get("pid") or "",
#                             row["full_name"], row["size"], row["price_units"], sid,
#                             s["slug"], s["name"], s["price"], s["was_price"], s["saving"],
#                             s["save_text"], s.get("deal_text"), s.get("promo_text"),
#                             s["unit_price"], s["unit_price_text"], int(bool(s["is_special"])),
#                             "|".join(s["tags"]), "|".join(row["departments"]),
#                             s.get("deal_url"), s["product_page"]])
#                 n_rows += 1

#     fill = {}
#     fields = ["name", "size", "price", "was_price", "unit_price", "save_text", "url"]
#     total_rows = sum(len(ps) for _, ps in docs)
#     for key in fields:
#         got = sum(1 for _, ps in docs for p in ps if p.get(key) not in (None, ""))
#         fill[key] = round(100.0 * got / total_rows, 1) if total_rows else None
#     fill["pid"] = round(100.0 * sum(1 for _, ps in docs for p in ps if p.get("pid")) / total_rows, 1) \
#         if total_rows else None

#     summary = {
#         "built_at": utcnow(),
#         "mode": mode,
#         "stores": per_store,
#         "totals": {
#             "stores": len(per_store),
#             "store_rows": sum(r["products"] for r in per_store),
#             "unique_products": len(by_pid),
#             "long_rows": n_rows,
#             "products_in_multiple_stores": sum(1 for v in by_pid.values() if len(v["stores"]) > 1),
#             "dedupe_ratio_percent": round(100.0 * len(by_pid) / total_rows, 1) if total_rows else None,
#         },
#         "fill_rate_percent": fill,
#         "join_key": ("url slug (/lines/<slug>) - the global product slug, 100% fill. "
#                      "'pid' (md5 product id) is emitted by only some stores, so it is exported "
#                      "per store-price and is NOT the grouping key. name_key is a normalised "
#                      "name for fuzzy matching where slugs differ between stores."),
#         "notes": [IMAGE_NOTE],
#         "outputs": {
#             "products.jsonl": jl, "long.csv": csvp,
#             "per_store_dir": os.path.join(outdir, "stores"),
#         },
#     }
#     write_json(os.path.join(outdir, "summary.json"), summary)
#     return summary


# # ---------------------------------------------------------------- verify

# def verify(outdir, log=print):
#     problems = []
#     man = read_json(os.path.join(outdir, "stores.json"), {})
#     stores = man.get("stores", [])
#     summary = read_json(os.path.join(outdir, "summary.json"), {})
#     if not stores:
#         problems.append("stores.json missing or empty")
#     log("stores declared by chooser: %d" % len(stores))
#     if not summary:
#         problems.append("summary.json missing - run build")
#     else:
#         for r in summary.get("stores", []):
#             f = os.path.join(outdir, r["file"]) if r.get("file") else None
#             if not f or not os.path.exists(f):
#                 problems.append("%s: missing store file" % r.get("slug"))
#                 continue
#             doc = read_json(f, {})
#             prods = doc.get("products", [])
#             if len(prods) != r["products"]:
#                 problems.append("%s: summary says %s products, file has %d"
#                                 % (r["slug"], r["products"], len(prods)))
#             no_pid = [p for p in prods if not p.get("url")]
#             pid_fill = 100.0 * sum(1 for p in prods if p.get("pid")) / len(prods) if prods else 0
#             log("  %-16s %5d products, pid fill %.0f%%, %d without pid" %
#                 (r["slug"], len(prods), pid_fill, len(prods) - int(pid_fill * len(prods) / 100)))
#             if no_pid:
#                 problems.append("%s: %d products without a url slug (no cross-store join key)"
#                                 % (r["slug"], len(no_pid)))
#             no_price = [p for p in prods if p.get("price") is None]
#             if no_price:
#                 problems.append("%s: %d products without a price" % (r["slug"], len(no_price)))
#             lst = doc.get("listings") or {}
#             declared_main = lst.get("declared_results")
#             declared_union = lst.get("declared_union_of_listings")
#             # Coverage: the biggest single listing must be fully covered.
#             if declared_main and len(prods) < declared_main * 0.98:
#                 problems.append("%s: %d products < biggest listing's declared %d"
#                                 % (r["slug"], len(prods), declared_main))
#             # Dedupe: the union of listings is an upper bound. More rows than that
#             # means duplicates survived the in-store dedupe.
#             if declared_union and len(prods) > declared_union * 1.02:
#                 problems.append("%s: %d products > union of listings %d (dedupe bug)"
#                                 % (r["slug"], len(prods), declared_union))
#             log("  %-16s declared: biggest=%s union=%s | scraped=%d"
#                 % (r["slug"], declared_main, declared_union, len(prods)))
#         dups = {}
#         for r in summary.get("stores", []):
#             f = os.path.join(outdir, r["file"]) if r.get("file") else None
#             if f and os.path.exists(f):
#                 for p in read_json(f, {}).get("products", []):
#                     dups.setdefault((r["slug"], p.get("line_id")), 0)
#                     dups[(r["slug"], p.get("line_id"))] += 1
#         d = [k for k, v in dups.items() if v > 1]
#         if d:
#             problems.append("%d duplicate (store, line_id) rows" % len(d))

#     # Parser regression check, straight off the raw HTML (no network, no store JSON).
#     # Counts the flags in the markup and compares them with what parse_listing returns:
#     # this is what catches a parser edit that silently zeroes a field (e.g. a shadowed
#     # variable making every is_special false while every other number still looks right).
#     checked = 0
#     for fn in sorted(glob.glob(os.path.join(outdir, "raw", "*", "*.html"))):
#         try:
#             with open(fn, encoding="utf-8") as f:
#                 body = f.read()
#         except OSError:
#             continue
#         cards = CARD_SPLIT.split(body)[1:]
#         cards = [c for c in cards if "talker__product-name" in c]
#         if not cards:
#             continue
#         raw_special = sum(1 for c in cards
#                           if re.search(r'class="talker[^"]*talker--Special', c)
#                           or re.search(r'class="talker[^"]*\bspecial"', c))
#         raw_was = len(re.findall(r"talker__prices__was", body))
#         raw_save = len(re.findall(r"talker__sticker--Saving", body))
#         raw_labels = len(re.findall(r"talker__sticker__label", body))
#         got = parse_listing(body)
#         g_special = sum(1 for x in got if x["is_special"])
#         g_was = sum(1 for x in got if x["was_price"] is not None)
#         g_save = sum(1 for x in got if x["save_text"] or x["deal_text"] or x["promo_text"])
#         if raw_special and g_special == 0:
#             problems.append("%s: %d special cards in HTML but 0 parsed as special "
#                             "(parser regression)" % (os.path.basename(fn), raw_special))
#         if raw_was and g_was == 0:
#             problems.append("%s: %d was-prices in HTML but 0 parsed (parser regression)"
#                             % (os.path.basename(fn), raw_was))
#         if raw_labels and g_save == 0:
#             problems.append("%s: %d sticker labels in HTML but 0 parsed (parser regression)"
#                             % (os.path.basename(fn), raw_labels))
#         checked += 1
#     log("parser regression check: %d raw pages re-parsed against the markup" % checked)

#     # Cross-check against each store's own sitemap (independent source)
#     for st in stores:
#         if not st.get("slug"):
#             continue
#         try:
#             sm = http_get("https://%s.shop.spar.com.au/sitemap.xml" % st["slug"])
#             n = len(re.findall(r"/lines/", sm))
#             sf = read_json(os.path.join(outdir, "stores", store_file_name(st)), {})
#             got = (sf.get("counts") or {}).get("products")
#             log("  %-16s sitemap /lines = %5d   scraped = %s" % (st["slug"], n, got))
#         except Exception as e:
#             log("  %-16s sitemap check skipped: %s" % (st["slug"], e))
#     if problems:
#         log("\n%d PROBLEM(S):" % len(problems))
#         for p in problems:
#             log("  - " + p)
#         return 1
#     log("\nOK: no structural problems found")
#     return 0


# # ---------------------------------------------------------------- cli

# CHEAT = """
# SPAR Australia scraper - quick commands (run from the folder holding this script):

#   python spar_au_scraper.py stores                 discover stores from the chooser -> stores.json
#   python spar_au_scraper.py diagnose               one request, says reachable / blocked / wrong
#   python spar_au_scraper.py fetch --mode specials  specials for every store (default: specials)
#   python spar_au_scraper.py fetch --mode catalog   full catalogue for every store (slower)
#   python spar_au_scraper.py fetch --mode all       both
#   python spar_au_scraper.py build                  cached pages -> per-store json + products.jsonl + long.csv
#   python spar_au_scraper.py all                    stores + fetch + build in one go
#   python spar_au_scraper.py verify                 structural checks (exit != 0 if something is wrong)

# Useful flags:
#   --out DIR            output folder (default: ./out-spar-au)
#   --stores bourke,...  only these store slugs
#   --workers N          concurrent requests per store (default 4, be polite)
#   --mode specials|catalog|all
#   --max-pages N        cap pages per listing (debugging a single store)
#   --force              ignore the raw cache and refetch
#   --keep-raw 0         do not keep raw HTML (saves disk, blocks rebuilds)
# """


# def main(argv=None):
#     argv = list(sys.argv[1:] if argv is None else argv)
#     if not argv or argv[0] in ("-h", "--help", "help"):
#         print(CHEAT)
#         return 0 if argv and argv[0] in ("-h", "--help", "help") else 0

#     ap = argparse.ArgumentParser(prog="spar_au_scraper.py", add_help=False,
#                                  description="SPAR Australia store-wise scraper")
#     ap.add_argument("action", choices=["stores", "diagnose", "fetch", "build", "all", "verify"])
#     ap.add_argument("--out", default=DEFAULT_OUT)
#     ap.add_argument("--mode", default="specials", choices=["specials", "catalog", "all"])
#     ap.add_argument("--stores", default="")
#     ap.add_argument("--workers", type=int, default=4)
#     ap.add_argument("--max-pages", type=int, default=0)
#     ap.add_argument("--force", action="store_true")
#     ap.add_argument("--keep-raw", type=int, default=1)
#     ap.add_argument("--retry-failed", action="store_true")
#     ap.add_argument("-h", "--help", action="store_true")
#     a = ap.parse_args(argv)
#     if a.help:
#         print(CHEAT)
#         return 0

#     outdir = os.path.abspath(a.out)
#     ensure_dir(outdir)
#     print("pid=%d  out=%s  action=%s" % (os.getpid(), outdir, a.action))
#     sys.stdout.flush()

#     if a.action == "diagnose":
#         print("probing %s" % CHOSER)
#         try:
#             body = http_get(CHOSER)
#             n = len(parse_store_cards(body))
#             print("  CHOSER  HTTP 200, %d bytes, %d store cards -> reachable" % (len(body), n))
#             cards = parse_store_cards(body)
#             if not cards:
#                 print("  VERDICT: reachable but the store list parsed to ZERO cards "
#                       "(layout changed or a bot-wall page)")
#                 return 2
#             slug = cards[0].get("store_id")
#             u = "https://shop.spar.com.au/%s/i_choose_you" % slug
#             print("  %s -> %s" % (u, _final_url(u)))
#             print("  VERDICT: reachable, direct HTTP works")
#             return 0
#         except BlockedResponse as e:
#             print("  VERDICT: BLOCKED - %s" % e)
#             return 2
#         except Exception as e:
#             print("  VERDICT: unreachable/other - %r" % e)
#             return 3

#     stores_path = os.path.join(outdir, "stores.json")
#     stores = (read_json(stores_path) or {}).get("stores")
#     if a.action in ("stores", "all") or not stores:
#         print("discovering stores from the chooser ...")
#         stores = discover_stores(outdir)
#         print("  %d stores, saved to %s" % (len(stores), stores_path))
#         for s in stores:
#             print("   %-16s %-22s %-26s %s" % (s.get("slug"), s.get("name"),
#                                                (s.get("address") or "")[:26], s.get("state")))

#     if a.stores:
#         want = {x.strip() for x in a.stores.split(",") if x.strip()}
#         stores = [s for s in stores if s.get("slug") in want]
#         print("  filtered to %d store(s): %s" % (len(stores), ",".join(sorted(want))))

#     if a.action == "stores":
#         return 0

#     if a.action in ("fetch", "all"):
#         if not stores:
#             print("no stores to fetch")
#             return 1
#         man = load_manifest(outdir)
#         t0 = time.time()
#         grand = {"ok": 0, "failed": 0, "errors": []}
#         for i, st in enumerate(stores, 1):
#             if not st.get("slug"):
#                 print("--- %d/%d  %s  SKIPPED: %s" % (i, len(stores), st.get("name"),
#                                                      st.get("resolve_error")))
#                 continue
#             print("--- %d/%d  %s (%s)  %s" % (i, len(stores), st.get("name"), st["slug"],
#                                               utcnow()))
#             try:
#                 res, sman = fetch_store(outdir, st, mode=a.mode, workers=a.workers,
#                                         keep_raw=bool(a.keep_raw),
#                                         force=a.force, max_pages=a.max_pages or None)
#                 man["done"].update(sman)
#                 grand["ok"] += res["ok"]
#                 grand["failed"] += res["failed"]
#                 grand["errors"] += res["errors"]
#                 print("    -> %d ok / %d failed in %ss" % (res["ok"], res["failed"],
#                                                            res.get("seconds")))
#             except Exception as e:
#                 print("    !! store failed: %s" % e)
#                 grand["errors"].append("%s: %s" % (st["slug"], e))
#             save_manifest(outdir, man)
#             if i == 3 and grand["ok"] == 0 and grand["failed"] >= 3:
#                 print("!! first 3 pages all failed and nothing succeeded - aborting run. "
#                       "Transport is the suspect, not the data.")
#                 return 2
#         print("fetch done: %d ok / %d failed in %.1fs" % (grand["ok"], grand["failed"],
#                                                           time.time() - t0))
#         if grand["errors"]:
#             print("errors (first 10):")
#             for e in grand["errors"][:10]:
#                 print("  " + e)

#     if a.action in ("build", "all"):
#         mode = a.mode
#         print("building from raw cache (mode=%s) ..." % mode)
#         if mode == "all":
#             pass
#         s = build(outdir, stores, mode=mode)
#         print("built: %d stores, %d store rows, %d unique products, %d long rows"
#               % (s["totals"]["stores"], s["totals"]["store_rows"],
#                  s["totals"]["unique_products"], s["totals"]["long_rows"]))
#         print("fill rates: " + ", ".join("%s=%s%%" % (k, v) for k, v in s["fill_rate_percent"].items()))
#         print("outputs: %s  %s  %s" % (s["outputs"]["products.jsonl"], s["outputs"]["long.csv"],
#                                        s["outputs"]["per_store_dir"]))

#     if a.action == "verify":
#         return verify(outdir)
#     return 0


# if __name__ == "__main__":
#     sys.exit(main())


#!/usr/bin/env python3
"""
SPAR Australia (MyFoodLink Shopfront) store-wise product scraper.
Customized version: Outputs ONLY 'stores.json' and 'stores/<id>-<slug>.json' files,
with specific product fields removed.
"""

import argparse
import errno
import html as htmllib
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- config

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CHOSER = "https://shop.spar.com.au/"
PER_PAGE = 48
TRANSIENT_CODES = {400, 403, 408, 429, 500, 502, 503, 504}
MAX_TRANSIENT_ATTEMPTS = 5
MAX_BLOCK_ATTEMPTS = 2

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out-spar-au")

IMAGE_NOTE = ("image_url is null by design: the shopfront truncates the CDN image path "
              "server-side (literal '...' in the HTML, such URLs 403). Use image_alt and "
              "url, or scrape images from a JS session.")


# ---------------------------------------------------------------- errors

class BlockedResponse(Exception):
    """Body was empty / HTML bot-wall / not what we asked for. NOT transient."""


class HttpFail(Exception):
    def __init__(self, status, url, body_head=""):
        super().__init__("HTTP %s %s :: %s" % (status, url, body_head[:160]))
        self.status = status
        self.url = url
        self.body_head = body_head


# ---------------------------------------------------------------- http

_local = threading.local()

def _opener():
    if not hasattr(_local, "op"):
        _local.op = urllib.request.build_opener()
    return _local.op


def http_get(url, timeout=60, accept="text/html"):
    last = None
    block_tries = 0
    tries = 0
    while tries < max(MAX_TRANSIENT_ATTEMPTS, MAX_BLOCK_ATTEMPTS):
        tries += 1
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-AU,en;q=0.9",
        })
        try:
            with _opener().open(req, timeout=timeout) as f:
                raw = f.read()
            if not raw:
                block_tries += 1
                last = BlockedResponse("empty body from %s" % url)
                if block_tries >= MAX_BLOCK_ATTEMPTS:
                    raise last
                time.sleep(1 + random.random())
                continue
            return raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            if e.code in TRANSIENT_CODES and tries < MAX_TRANSIENT_ATTEMPTS:
                last = HttpFail(e.code, url, body)
                time.sleep(min(30, (2 ** tries) + random.random() * 1.5))
                continue
            raise HttpFail(e.code, url, body)
        except (urllib.error.URLError, OSError) as e:
            last = e
            if tries < MAX_TRANSIENT_ATTEMPTS:
                time.sleep(min(30, (2 ** tries) + random.random() * 1.5))
                continue
            raise
    raise last if isinstance(last, Exception) else RuntimeError("unreachable")


# ---------------------------------------------------------------- parsing

CARD_SPLIT = re.compile(r'<div class="TalkerGrid__Item" role="listitem">')

def _text(frag):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", frag))).strip()


def clean_name(raw, size=None):
    n = (raw or "").replace("\u200b", "").replace("\ufeff", "").strip()
    m = re.match(r"^\[(.*?)\s*,\s*([^\[\]]*)\]$", n, re.S)
    if m:
        base, tail = m.group(1).strip(), m.group(2).strip()
        n = base
        if not size and tail and tail.lower() not in ("null", "nil", "none", ""):
            size = tail
    n = re.sub(r"[\s,]+$", "", n).strip()
    return n, size


def span_text_at(s, i):
    j = s.find(">", i)
    if j == -1:
        return None
    depth, k = 1, j + 1
    while depth > 0:
        no = s.find("<span", k)
        nc = s.find("</span>", k)
        if nc == -1:
            break
        if no != -1 and no < nc:
            depth += 1
            k = no + 5
        else:
            depth -= 1
            k = nc + 7
    inner = s[j + 1:k - 7] if depth == 0 else s[j + 1:k]
    return _text(inner)


def _money(s):
    if s is None:
        return None
    s = re.sub(r"[^0-9.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def parse_card(chunk):
    name_m = re.search(r'<span class="talker__product-name">(.*?)</span>', chunk, re.S)
    if not name_m:
        return None
    raw_name = _text(name_m.group(1))

    size_m = re.search(r'<span class="weak size talker__name__size">(.*?)</span>', chunk, re.S)
    size = _text(size_m.group(1)) if size_m else None
    name, size = clean_name(raw_name, size)

    title_m = re.search(r'<div class="talker__name talker__section" title="([^"]*)"', chunk)
    full_name = htmllib.unescape(title_m.group(1)) if title_m else ""
    full_name = re.sub(r"\s+", " ", full_name.replace("\u200b", "")).strip()
    if not full_name or full_name.startswith("["):
        full_name = name + (" " + size if size else "")

    url_m = re.search(r'<a href="(/lines/[^"]+)"', chunk)
    alt_m = re.search(r'<img[^>]+alt="([^"]*)"', chunk)
    pid_m = re.search(r"&quot;pid&quot;:&quot;([0-9a-f]+)&quot;", chunk)
    lid_m = re.search(r'id="line_([0-9a-f]+)"', chunk)

    sell_m = re.search(r'<strong class="price__sell"[^>]*>([^<]+)</strong>', chunk)
    units_m = re.search(r'<span class="price__units weak">\s*([^<]+?)\s*</span>', chunk, re.S)
    was_m = re.search(r'<span class="talker__prices__was weak">\s*was \$([\d.,]+)\s*</span>', chunk, re.S)
    up_m = re.search(r'talker__prices__comparison--UnitPrice">([^<]+)</span>', chunk)
    cls_m = re.search(r'class="talker([^"]*)"', chunk)
    cls = cls_m.group(1) if cls_m else ""
    ctrl_m = re.search(r'data-line-control="([^"]+)"', chunk)

    qty = None
    if ctrl_m:
        try:
            qty = (json.loads(htmllib.unescape(ctrl_m.group(1))) or {}).get("quantity")
        except Exception:
            qty = None

    price = _money(sell_m.group(1)) if sell_m else None
    was = _money(was_m.group(1)) if was_m else None
    up_text = htmllib.unescape(re.sub(r"<[^>]+>", " ", up_m.group(1))).strip().replace("  ", " ") \
        if up_m else None
    up_val = up_unit = up_base = None
    if up_text:
        m = re.search(r"\$([\d.]+)\s*per\s*(.+)$", up_text)
        if m:
            up_val = _money("$" + m.group(1))
            up_unit = m.group(2).strip()
            bm = re.search(r"per\s*([\d.]+)\s*([a-zA-Z]+)", up_text)
            if bm:
                up_base = "%s%s" % (bm.group(1), bm.group(2))
    save_text = promo_text = deal_text = deal_url = None

    opens = list(re.finditer(r'<(?:span|a) class="talker__sticker(?: ([^"]*))?"'
                             r'(?:\s+href="([^"]*)")?', chunk))
    for i, om in enumerate(opens):
        scls = om.group(1) or ""
        href = om.group(2)
        end = opens[i + 1].start() if i + 1 < len(opens) else len(chunk)
        seg = chunk[om.start():end]
        li = seg.find("talker__sticker__label")
        if li == -1:
            continue
        txt = span_text_at(seg, li)
        if not txt:
            continue
        if "talker__sticker--Saving" in scls:
            save_text = save_text or txt
        elif "talker__sticker--Deal" in scls or "talker__sticker--Discount" in scls \
                or "talker__sticker--Special" in scls:
            deal_text = deal_text or txt
            if href and not deal_url:
                deal_url = htmllib.unescape(href)
        else:
            promo_text = promo_text or txt

    # EXCLUDED unwanted fields: pid, line_id, raw_name, url, image_alt, 
    # unit_price_text, unit_price_unit, unit_price_base, deal_text, deal_url, source_listings
    return {
        "name": name,
        "size": size,
        "full_name": full_name,
        "image_url": None,
        "price": price,
        "was_price": was,
        "saving": round(was - price, 2) if (was is not None and price is not None) else None,
        "price_units": _text(units_m.group(1)) if units_m else None,
        "unit_price": up_val,
        "save_text": save_text,
        "promo_text": promo_text,
        "is_special": ("talker--Special" in cls) or bool(re.search(r'\bspecial\b', cls)),
        "tags": sorted(set(re.findall(r"talker--Tag-([a-z0-9\-]+)", chunk))),
        "is_weighed": bool(qty and qty.get("unit")),
        "qty_unit": (qty or {}).get("unit"),
    }


def parse_listing(page_html):
    return [c for c in (parse_card(ch) for ch in CARD_SPLIT.split(page_html)[1:]) if c]


def results_count(page_html):
    m = re.search(r"<span>([\d,]+) results</span>", page_html)
    return int(m.group(1).replace(",", "")) if m else None


def max_page(page_html):
    pgs = [int(x) for x in re.findall(r"[?&]page=(\d+)", page_html)]
    return max(pgs) if pgs else 1


def page_has_products(page_html):
    return 'talker__product-name' in page_html


# ---------------------------------------------------------------- store discovery

STORECARD_SPLIT = re.compile(r'<div class="StoreCard(?:\s[^"]*)?">')

def parse_store_cards(page_html):
    out = []
    for chunk in STORECARD_SPLIT.split(page_html)[1:]:
        idm = re.search(r'href="/([0-9a-f]{24})/i_choose_you"', chunk)
        if not idm:
            continue
        nm = re.search(r'<span class="StoreCard__Name">(.*?)</span>', chunk, re.S)
        dt = re.search(r'<span class="StoreCard__Details">(.*?)</span>', chunk, re.S)
        hours = None
        addr_lines = []
        if dt:
            parts = [x.strip() for x in re.split(r"<br\s*/?>", dt.group(1))]
            for p in parts:
                t = _text(p)
                if not t:
                    continue
                if t.lower().startswith("open today"):
                    hours = t
                else:
                    addr_lines.append(t)
        tel = re.search(r'href="tel:([^"]+)"', chunk)
        directions = re.search(r'href="(http://maps\.google\.com[^"]+)"', chunk)
        cat = re.search(r'href="(https://[a-z0-9\-]+\.shop\.spar\.com\.au/catalogues)"', chunk)
        cta = re.search(r'href="/%s/i_choose_you"[^>]*>(.{0,3000}?)</a>' % idm.group(1), chunk, re.S)
        out.append({
            "store_id": idm.group(1),
            "name": _text(nm.group(1)) if nm else None,
            "hours_today": hours,
            "address_lines": addr_lines,
            "address": ", ".join(addr_lines),
            "phone": tel.group(1) if tel else None,
            "directions_url": htmllib.unescape(directions.group(1)) if directions else None,
            "catalogue_url": cat.group(1) if cat else None,
            "chooser_cta": _text(cta.group(1)) if cta else None,
            "chooser_url": CHOSER + idm.group(1) + "/i_choose_you",
        })
    return out


def _final_url(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise _Redirect(newurl)

    class _Redirect(Exception):
        def __init__(self, url):
            self.url = url
            super().__init__(url)

    op = urllib.request.build_opener(NoRedirect)
    try:
        op.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=45)
    except _Redirect as r:
        return r.url
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
        raise
    return url


STATE_EXPAND = {
    "new south wales": "NSW", "queensland": "QLD", "victoria": "VIC",
    "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
    "northern territory": "NT", "australian capital territory": "ACT",
}


def usps_codes(text):
    t = text or ""
    m = re.search(r"\b(NSW|QLD|VIC|SA|WA|TAS|NT|ACT)\b[\s,]*(\d{4})", t)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"\b(NSW|QLD|VIC|SA|WA|TAS|NT|ACT)\b", t)
    if m:
        return m.group(1), None
    low = t.lower()
    for k, v in STATE_EXPAND.items():
        if k in low:
            pc = re.search(r"\b(\d{4})\b", t)
            return v, (pc.group(1) if pc else None)
    return None, None


def discover_stores(outdir=None):
    page = http_get(CHOSER)
    cards = parse_store_cards(page)
    stores = []
    for c in cards:
        final = _final_url(c["chooser_url"]) or ""
        m = re.match(r"https?://([a-z0-9\-]+)\.shop\.spar\.com\.au", final)
        sub = m.group(1) if m else None
        state, postcode = usps_codes(c["address"])
        st = dict(c)
        st.update({
            "slug": sub,
            "subdomain": ("%s.shop.spar.com.au" % sub) if sub else None,
            "homepage": ("https://%s.shop.spar.com.au/" % sub) if sub else None,
            "state": state,
            "postcode": postcode,
        })
        if not sub:
            st["resolve_error"] = "no subdomain from %s (got %r)" % (c["chooser_url"], final)
        stores.append(st)
    if outdir:
        write_json(os.path.join(outdir, "stores.json"), {
            "source": CHOSER,
            "fetched_at": utcnow(),
            "count": len(stores),
            "stores": stores,
        })
    return stores


# ---------------------------------------------------------------- io helpers

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(p):
    try:
        os.makedirs(p)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def write_json(path, obj):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------- fetch & build

def listing_urls(mode, slug, dept_slug=None, page=1):
    base = "https://%s.shop.spar.com.au" % slug
    if dept_slug:
        q = "q%%5B%%5D=category%%3A%s" % dept_slug
    elif mode == "specials":
        q = "q%5B%5D=special%3A1"
    else:
        q = ""
    sep = "&" if q else "?"
    if page > 1:
        return "%s/search?%s%spage=%d" % (base, q, sep, page)
    return "%s/search?%s" % (base, q)


def fetch_departments(slug):
    url = "https://%s.shop.spar.com.au/search/sidebar_facets.json" % slug
    data = json.loads(http_get(url, accept="application/json"))
    depts = []
    for sec in data.get("more_sections", []):
        if sec.get("group") != "categories":
            continue
        for f in sec.get("facets", []):
            u = f.get("url", "")
            m = re.search(r"category%3A([a-z0-9\-]+)", u)
            if m:
                depts.append({"slug": m.group(1), "name": f.get("name"), "url": u})
    return depts


def fetch_page_direct(slug, tag, url, page):
    body = http_get(url)
    if not page_has_products(body) and page <= 1:
        raise BlockedResponse("page 1 of %s has no product cards (url=%s)" % (slug, url))
    return body


def fetch_store_products(store, mode="specials", max_pages=None, log=print):
    slug = store.get("slug")
    jobs = []

    if mode in ("specials", "all"):
        u1 = listing_urls("specials", slug, page=1)
        body = fetch_page_direct(slug, "specials", u1, 1)
        last = max_page(body)
        if max_pages:
            last = min(last, max_pages)
        for p in range(1, last + 1):
            jobs.append(("specials", p, listing_urls("specials", slug, page=p)))

    if mode in ("catalog", "all"):
        try:
            depts = fetch_departments(slug)
        except Exception:
            depts = []
        for d in depts:
            try:
                u1 = listing_urls("catalog", slug, dept_slug=d["slug"], page=1)
                body = fetch_page_direct(slug, "cat-" + d["slug"], u1, 1)
                last = max_page(body)
                if max_pages:
                    last = min(last, max_pages)
                for p in range(1, last + 1):
                    jobs.append(("cat-" + d["slug"], p, listing_urls("catalog", slug, dept_slug=d["slug"], page=p)))
            except Exception:
                pass

    prods = {}
    order = []
    declared_by_tag = {}
    pages_ok = 0

    for tag, page, url in jobs:
        try:
            body = fetch_page_direct(slug, tag, url, page)
            pages_ok += 1
            if page == 1:
                n = results_count(body)
                if n is not None:
                    declared_by_tag[tag] = n
            cards = parse_listing(body)
            for c in cards:
                # Use name and price as composite unique identifier since pid/url are removed
                key = (c.get("full_name"), c.get("price"))
                dept_name = tag[4:] if tag.startswith("cat-") else "specials"
                if key in prods:
                    if dept_name and dept_name not in prods[key]["departments"]:
                        prods[key]["departments"].append(dept_name)
                    continue
                c = dict(c)
                c["departments"] = [dept_name] if dept_name else []
                prods[key] = c
                order.append(key)
        except Exception as e:
            log("  ! %-16s %-28s p%03d FAILED: %s" % (slug, tag, page, e))

    products = [prods[k] for k in order]
    declared_main = max(declared_by_tag.values()) if declared_by_tag else None
    
    meta = {
        "counts": {
            "products": len(products),
            "with_was_price": sum(1 for x in products if x.get("was_price") is not None),
            "with_unit_price": sum(1 for x in products if x.get("unit_price") is not None),
            "weighed": sum(1 for x in products if x.get("is_weighed")),
            "specials": sum(1 for x in products if x.get("is_special")),
        },
        "listings": {"declared_results": declared_main, "pages_read": pages_ok},
        "departments": sorted(set(d for x in products for d in x["departments"])),
    }
    return products, meta


def store_file_name(store):
    return "%s-%s.json" % (store["store_id"], store.get("slug") or "unknown")


def run_minimal_extraction(outdir, stores, mode="specials", log=print):
    ensure_dir(os.path.join(outdir, "stores"))
    for st in stores:
        if not st.get("slug"):
            log("  ! skipping %s (%s)" % (st.get("name"), st.get("resolve_error")))
            continue
        log("  - fetching & building store: %s" % st["slug"])
        products, meta = fetch_store_products(st, mode=mode, log=log)
        if not products:
            log("  ! %-16s parsed 0 products - skipping file write" % st["slug"])
            continue
        
        doc = {
            "store": {
                "store_id": st["store_id"], "slug": st["slug"],
                "name": st.get("name"), "subdomain": st.get("subdomain"),
                "homepage": st.get("homepage"), "address": st.get("address"),
                "address_lines": st.get("address_lines"), "state": st.get("state"),
                "postcode": st.get("postcode"), "phone": st.get("phone"),
                "hours_today": st.get("hours_today"),
                "directions_url": st.get("directions_url"),
                "catalogue_url": st.get("catalogue_url"),
            },
            "source": {
                "platform": "myfoodlink-shopfront",
                "mode": mode,
                "specials_url": "https://%s.shop.spar.com.au/specials" % st["slug"] if mode != "catalog" else None,
                "all_products_url": "https://%s.shop.spar.com.au/search" % st["slug"],
                "page_size": PER_PAGE,
            },
            "fetched_at": utcnow(),
            "listed_url": "https://%s.shop.spar.com.au/" % st["slug"],
            "counts": meta["counts"],
            "listings": meta["listings"],
            "departments": meta["departments"],
            "notes": [IMAGE_NOTE],
            "products": products,
        }
        
        file_path = os.path.join(outdir, "stores", store_file_name(st))
        write_json(file_path, doc)
        log("  + saved -> stores/%s" % store_file_name(st))


# ---------------------------------------------------------------- cli

CHEAT = """
SPAR Australia scraper (Minimal JSON-only mode):
  python spar_au_scraper.py run     Discovers stores, fetches catalogs, and saves ONLY:
                                    - out-spar-au/stores.json
                                    - out-spar-au/stores/<store_id>-<slug>.json
"""

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(CHEAT)
        return 0

    ap = argparse.ArgumentParser(prog="spar_au_scraper.py", add_help=False)
    ap.add_argument("action", choices=["run", "stores"])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--mode", default="specials", choices=["specials", "catalog", "all"])
    ap.add_argument("--stores", default="")
    a = ap.parse_args(argv)

    outdir = os.path.abspath(a.out)
    ensure_dir(outdir)
    print("pid=%d  out=%s  action=%s" % (os.getpid(), outdir, a.action))
    sys.stdout.flush()

    stores_path = os.path.join(outdir, "stores.json")
    stores = discover_stores(outdir)
    print("  %d stores discovered, saved to %s" % (len(stores), stores_path))

    if a.stores:
        want = {x.strip() for x in a.stores.split(",") if x.strip()}
        stores = [s for s in stores if s.get("slug") in want]
        print("  filtered to %d store(s)" % len(stores))

    if a.action == "run":
        run_minimal_extraction(outdir, stores, mode=a.mode)
        print("Extraction complete. Unwanted product fields successfully stripped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())