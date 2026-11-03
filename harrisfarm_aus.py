#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harrisfarm_specials.py — Harris Farm Markets (harrisfarm.com.au) online-specials extractor.

WHY THIS IS NOT A products.json SCRAPER
---------------------------------------
harrisfarm.com.au is Shopify, but /collections/<handle>/products.json only gives you
the *base* price (`variant.price`, the location "1" web bucket) — and that is NOT the
price any store sells at. Real prices are per-store metafields on the product:

    custom.price_location_<store#>        e.g. price_location_70 -> "0.75"
    custom.unit_price_location_<store#>   e.g. unit_price_location_70 -> "4.99"  (/kg)
    custom.special_locations              '["10","24",...,"87"]'  stores on special
    custom.available_locations            '["1","10",...]'        stores that stock it
    local.uom / local.conversion          "/ each" / "1"

Checked 2026-10-09: for store 70 (West End) the store price differed from the base
price on 1723 / 2027 products. A products.json-only scrape is wrong by default.

Those metafields are readable through the PUBLIC Shopify Storefront GraphQL API
(version 2025-10) with the public token embedded in every page as
<meta name="shopify-storefront-token">. No login, no cookies, no bot wall.

Two independent cross-checks that the metafield model is right:
  * the collection page's product cards carry
      data-browse-price="0.75" data-browse-location="70" data-browse-unit-price="4.99"
    which matches price_location_70 / unit_price_location_70 exactly;
  * product pages render the same per-store price (checked 5 products);
  * card data-uom / data-conversion-factor match local.uom / local.conversion.

THREE TRAPS IN THIS DATA (all verified against the live shop 2026-10-09)
-----------------------------------------------------------------------
1. variant.price is NOT a store's regular price. It is the location-"1" web bucket, and it only
   means the same thing as a store price on items sold per countable unit. On weight/volume items
   it is a different unit: "Whittakers Chocolate Peanut Slab 50g" carries base 15.99 (uom / 100g)
   while both the product page and price_location_70 show 2.00. So this script only computes a
   saving when base_price_trusted=1; everywhere else saving_vs_base is null and base_price is a
   reference you must not chart. There is no reliable regular price in this feed - say so, do not
   invent one.
2. price_location_<store> == 0.0 is HF's "not sellable at this store right now" marker, and 806
   (product, store) pairs still sit in special_locations while carrying it. Not free produce.
   price_location_<store> >= 999.99 is "cannot be sold there at all" (liquor at the QLD stores).
   Both are surfaced as price_status, never silently dropped and never treated as prices.
3. The collection has 2029 products but /products.json only returns 1724: the REST view hides
   products that carry no online-store publication. The Storefront API is the authoritative set,
   so the REST fallback lane is a floor, not the data.

Pure stdlib, Python 3.8+, runs the same on macOS and on Windows (D:\\... cmd.exe).

COMMANDS
--------
  python harrisfarm_specials.py                       # cheat sheet, nothing runs
  python harrisfarm_specials.py stores                # store list (id -> name)
  python harrisfarm_specials.py diagnose              # 1 request: token, HTTP, counts, verdict
  python harrisfarm_specials.py fetch                 # download the collection (resumable)
  python harrisfarm_specials.py build                 # raw cache -> jsonl / csv / per-store
  python harrisfarm_specials.py all                   # fetch + build (the normal run)
  python harrisfarm_specials.py verify                # audit the outputs, non-zero exit on a real problem

  python harrisfarm_specials.py all --stores 70,84    # only these stores' price columns
  python harrisfarm_specials.py all --collection grocery-specials
  python harrisfarm_specials.py all --lane rest       # fallback lane: base prices ONLY (no per-store)
  python harrisfarm_specials.py all --fresh           # wipe cache/state and re-download
  python harrisfarm_specials.py fetch --limit 50      # smoke test (marks output partial)

OUTPUTS (own filenames, one owner: this script)
------------------------------------------------
  harrisfarm_stores.json            store registry (id, name, gid, source)
  harrisfarm_specials_raw.json      raw GraphQL product nodes (the cache build reads)
  harrisfarm_specials.jsonl         one flat row per product, prices nested per store
  harrisfarm_specials_prices.csv    LONG: one row per (product x store) - the analysis file
  harrisfarm_store_summary.csv      per store: sellable count, specials count, avg special price
  stores/<store_slug>.json          per store: that store's specials (Drakes-style per-store file)
  harrisfarm_run_summary.json       counts, timing, lane, fill rates, partial/failed flags
  harrisfarm_state.json             resume state (cursors, page files)
  pages/page-0001.json ...          atomic per-page cache (resume granularity)
"""

import argparse
import csv
import gzip
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SHOP = "https://www.harrisfarm.com.au"
DEFAULT_COLLECTION = "online-specials"
# Newest first: the endpoint 404s when Shopify retires a version, so we walk down.
API_VERSIONS = ["2025-10", "2025-07", "2025-04", "2024-10", "2024-07"]
PAGE_SIZE = 250              # GraphQL max for this connection is fine at 250 (cost 200/query)
REST_PAGE_SIZE = 250
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# Location "1" is not in the JS store map but owns the base price bucket + appears in
# available_locations. Keep it so no row points at an unnamed price.
BASE_LOCATION = {"id": "1", "name": "1 (base/regular price bucket)", "gid": None}

# Baked snapshot 2026-10-09 — only used if the live location-manager.js cannot be read.
# `stores --refresh` re-reads the live file; stale names here are cosmetic, ids are not prices.
FALLBACK_STORES = {
    "10": ("Pennant Hills", "gid://shopify/Location/71928676411"),
    "11": ("Meats Pennant Hills", "gid://shopify/Location/11"),
    "24": ("St Ives", "gid://shopify/Location/71928873019"),
    "28": ("Mosman", "gid://shopify/Location/71928610875"),
    "32": ("Willoughby", "gid://shopify/Location/71928938555"),
    "37": ("Broadway", "gid://shopify/Location/71927922747"),
    "40": ("Erina", "gid://shopify/Location/71928184891"),
    "44": ("Orange", "gid://shopify/Location/71928643643"),
    "48": ("Manly", "gid://shopify/Location/71928479803"),
    "49": ("Mona Vale", "gid://shopify/Location/71928545339"),
    "51": ("Bowral", "gid://shopify/Location/71927889979"),
    "52": ("Cammeray", "gid://shopify/Location/71927955515"),
    "54": ("Potts Point", "gid://shopify/Location/71928709179"),
    "56": ("Boronia Park", "gid://shopify/Location/71927857211"),
    "57": ("Bondi Beach", "gid://shopify/Location/71927627835"),
    "58": ("Drummoyne", "gid://shopify/Location/71928086587"),
    "59": ("Bathurst", "gid://shopify/Location/71927562299"),
    "63": ("Randwick", "gid://shopify/Location/71928741947"),
    "64": ("Leichhardt", "gid://shopify/Location/71928283195"),
    "65": ("Bondi Westfield", "gid://shopify/Location/71927693371"),
    "66": ("Cooks Hill", "gid://shopify/Location/71928053819"),
    "67": ("Lindfield", "gid://shopify/Location/71928315963"),
    "68": ("Albury", "gid://shopify/Location/71927496763"),
    "69": ("Rose Bay", "gid://shopify/Location/71928840251"),
    "70": ("West End", "gid://shopify/Location/71928905787"),
    "73": ("Online", "gid://shopify/Location/73"),
    "74": ("Isle of Capri", "gid://shopify/Location/71928217659"),
    "75": ("Clayfield", "gid://shopify/Location/71927988283"),
    "76": ("Lane Cove", "gid://shopify/Location/71928250427"),
    "77": ("Dural", "gid://shopify/Location/71928152123"),
    "80": ("Majura Park", "gid://shopify/Location/71928381499"),
    "84": ("Redfern", "gid://shopify/Location/71928807483"),
    "85": ("Marrickville", "gid://shopify/Location/71928512571"),
    "86": ("Miranda", "gid://shopify/Location/71971864635"),
    "87": ("Maroubra", "gid://shopify/Location/71971897403"),
}

# Price sentinels observed on this shop (2026-10-09). Real prices are 0 < p < 999:
#   0.0    -> "not available at this store this week". 812 pairs, 806 of them still listed in
#             special_locations (HF zeroes out specials it cannot sell right now).
#   999.99 -> "cannot sell here at all" (liquor at the QLD stores 74/75). Never in special_locations.
PRICE_ZERO = 0.0
PRICE_SENTINEL = 999.0

MAX_TRANSIENT_ATTEMPTS = 5
MAX_BLOCKED_ATTEMPTS = 2
FAIL_FAST_AFTER = 2          # consecutive failed pages with 0 successes -> abort the run


# --------------------------------------------------------------------------- errors
class ScrapeError(Exception):
    pass


class BlockedResponse(ScrapeError):
    """Non-JSON / empty body: a bot wall or an intercepting proxy, NOT a schema change."""

    def __init__(self, status, url, body_head):
        self.status = status
        self.url = url
        self.body_head = body_head
        super().__init__("HTTP %s non-JSON body from %s :: %r" % (status, url, body_head[:160]))


class CredentialError(ScrapeError):
    pass


# --------------------------------------------------------------------------- http
def _decode(raw, headers):
    if (headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw


def http_request(url, data=None, headers=None, timeout=60, expect_json=True):
    """One HTTP request with the retry policy from api-scraper-hardening.

    transient (408/429/5xx)   -> exponential backoff + jitter, MAX_TRANSIENT_ATTEMPTS
    blocked   (non-JSON body) -> 2 attempts then raise BlockedResponse
    credentials (401/403)     -> raise immediately, never retry
    """
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    transient_left = MAX_TRANSIENT_ATTEMPTS
    blocked_left = MAX_BLOCKED_ATTEMPTS
    delay = 1.0
    last_err = None
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = _decode(resp.read(), resp.headers)
                if expect_json:
                    try:
                        return json.loads(raw.decode("utf-8", "replace")), resp.status, dict(resp.headers)
                    except ValueError:
                        blocked_left -= 1
                        last_err = BlockedResponse(resp.status, url, raw)
                        if blocked_left <= 0:
                            raise last_err
                        print("  ! non-JSON body (%d bytes) from %s - retry %d/%d"
                              % (len(raw), url, MAX_BLOCKED_ATTEMPTS - blocked_left, MAX_BLOCKED_ATTEMPTS))
                        time.sleep(2.0)
                        continue
                return raw, resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = _decode(e.read() or b"", e.headers or {})
            except Exception:
                pass
            status = e.code
            if status in (401, 403):
                raise CredentialError(
                    "HTTP %s on %s - the storefront token is wrong/expired, or egress is being "
                    "intercepted. Re-run `diagnose` to re-discover the token." % (status, url))
            if status in (404,) and data is None:
                raise ScrapeError("HTTP 404 %s" % url)
            if status in (408, 425, 429, 500, 502, 503, 504):
                transient_left -= 1
                if transient_left <= 0:
                    raise ScrapeError("HTTP %s after %d attempts on %s :: %r"
                                      % (status, MAX_TRANSIENT_ATTEMPTS, url, body[:160]))
                print("  ~ HTTP %s - retry in %.1fs (%d left)" % (status, delay, transient_left))
                time.sleep(delay + random.uniform(0, 0.4))
                delay = min(delay * 2, 20)
                continue
            raise ScrapeError("HTTP %s on %s :: %r" % (status, url, body[:160]))
        except urllib.error.URLError as e:
            transient_left -= 1
            last_err = ScrapeError("network error on %s :: %s" % (url, e.reason))
            if transient_left <= 0:
                raise last_err
            print("  ~ network error (%s) - retry in %.1fs (%d left)" % (e.reason, delay, transient_left))
            time.sleep(delay + random.uniform(0, 0.4))
            delay = min(delay * 2, 20)


def fetch_text(url, **kw):
    raw, status, headers = http_request(url, expect_json=False, **kw)
    return raw.decode("utf-8", "replace"), status, headers


def _cache_age_hours(state):
    ts = state.get("completed_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_text_atomic(path, text):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- discovery
def discover_token(collection=DEFAULT_COLLECTION):
    """Read the public storefront token out of a collection page. Rotates, so never hardcode."""
    url = "%s/collections/%s" % (SHOP, collection)
    html, status, _ = fetch_text(url)
    m = re.search(r'<meta\s+name="shopify-storefront-token"\s+content="([^"]+)"', html)
    if not m:
        m = re.search(r'shopify-storefront-token"\s+content="([^"]+)"', html)
    if not m:
        raise ScrapeError("storefront token not found in %s (page was %d bytes)" % (url, len(html)))
    return m.group(1), html, status


def discover_stores(html=None, collection=DEFAULT_COLLECTION, verbose=True):
    """Stores come from the theme asset location-manager.js (id -> name). Falls back to a snapshot."""
    try:
        if html is None:
            html, _, _ = fetch_text("%s/collections/%s" % (SHOP, collection))
        m = re.search(r'src="(//www\.harrisfarm\.com\.au/cdn/shop/t/\d+/assets/location-manager\.js[^"]*)"', html)
        if not m:
            raise ScrapeError("location-manager.js asset not referenced in the page")
        js, _, _ = fetch_text("https:" + m.group(1))
        found = re.findall(r'(\d+):\{id:"([^"]+)",name:"([^"]+)"\}', js)
        if len(found) < 10:
            raise ScrapeError("only %d stores parsed out of location-manager.js" % len(found))
        stores = {sid: {"id": sid, "name": name, "gid": gid} for sid, gid, name in found}
        stores.setdefault(BASE_LOCATION["id"], dict(BASE_LOCATION))
        if verbose:
            print("stores: %d read live from location-manager.js" % len(stores))
        return stores, "live:location-manager.js"
    except Exception as e:
        print("  ! live store list unavailable (%s) - using the baked 2026-10-09 snapshot" % e)
        stores = {sid: {"id": sid, "name": n, "gid": g} for sid, (n, g) in FALLBACK_STORES.items()}
        stores.setdefault(BASE_LOCATION["id"], dict(BASE_LOCATION))
        return stores, "fallback:snapshot-2026-10-09"


# --------------------------------------------------------------------------- graphql
def mf_identifiers(store_ids, with_special_text=False):
    items = [("custom", "special_locations"), ("custom", "available_locations"),
             ("local", "uom"), ("local", "conversion"), ("local", "overridecup"),
             ("local", "hideInState")]
    for sid in store_ids:
        items.append(("custom", "price_location_%s" % sid))
        items.append(("custom", "unit_price_location_%s" % sid))
        if with_special_text:
            items.append(("custom", "special_text_location_%s" % sid))
    return ",".join('{namespace:"%s",key:"%s"}' % kv for kv in items)


COLLECTION_QUERY = """
query Coll($handle: String!, $first: Int!, $after: String) {
  collectionByHandle(handle: $handle) {
    id
    title
    products(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges {
        cursor
        node {
          id
          title
          handle
          vendor
          productType
          tags
          featuredImage { url altText }
          variants(first: 10) {
            edges {
              node {
                id
                sku
                title
                price { amount currencyCode }
                compareAtPrice { amount currencyCode }
                availableForSale
                quantityAvailable
                selectedOptions { name value }
              }
            }
          }
          metafields(identifiers: [__MF__]) { key value namespace type }
        }
      }
    }
  }
}
"""


def gql_call(api_version, token, query, variables, timeout=120):
    url = "%s/api/%s/graphql.json" % (SHOP, api_version)
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    data, status, headers = http_request(
        url, data=body, timeout=timeout,
        headers={"Content-Type": "application/json",
                 "X-Shopify-Storefront-Access-Token": token})
    if data.get("errors"):
        msgs = "; ".join(str(e.get("message")) for e in data["errors"])
        # A retired API version is a 404 on the endpoint; anything else is schema/argument trouble.
        raise ScrapeError("GraphQL errors: %s" % msgs[:400])
    return data.get("data") or {}, headers


def negotiate_api_version(token, query, variables):
    last = None
    for v in API_VERSIONS:
        try:
            data, headers = gql_call(v, token, query, variables)
            return v, data, headers
        except ScrapeError as e:
            last = e
            if "404" in str(e) or "Could not find" in str(e):
                print("  ~ API version %s not available - trying the next one" % v)
                continue
            raise
    raise ScrapeError("no usable Storefront API version in %s :: %s" % (API_VERSIONS, last))


# --------------------------------------------------------------------------- fetch
def cmd_fetch(args):
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.join(args.out, "pages"), exist_ok=True)
    state_path = os.path.join(args.out, "harrisfarm_state.json")
    stores, store_source = discover_stores(collection=args.collection)

    if args.fresh:
        for name in os.listdir(os.path.join(args.out, "pages")):
            os.remove(os.path.join(args.out, "pages", name))
        if os.path.exists(state_path):
            os.remove(state_path)
        print("fresh run: page cache wiped")

    wanted = sorted(stores.keys(), key=lambda s: int(s))
    if args.stores:
        wanted = [s for s in wanted if s in args.stores]
    print("collection=%s | stores with price columns: %d | lane=%s"
          % (args.collection, len(wanted), args.lane))

    state = {"collection": args.collection, "next_cursor": None, "pages": [], "done": False}
    if os.path.exists(state_path):
        try:
            with io.open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            if state.get("collection") != args.collection:
                state = {"collection": args.collection, "next_cursor": None, "pages": [], "done": False}
        except ValueError:
            pass

    # A finished cache is only reused while it is fresh; after that we re-download rather than
    # append to a complete set (which would have started from the end cursor and fetched nothing).
    if state.get("done") and not args.fresh:
        age_h = _cache_age_hours(state)
        if age_h is not None and age_h < args.max_cache_age_hours:
            print("cache is complete and %.1fh old (< %.1fh) - nothing to fetch. "
                  "Use --fresh to re-download anyway." % (age_h, args.max_cache_age_hours))
            return 0
        age_txt = "%.1fh" % age_h if age_h is not None else "unknown age"
        print("cache is complete but %s old - re-downloading instead of appending to a finished set"
              % age_txt)
        args.fresh = True
        for name in os.listdir(os.path.join(args.out, "pages")):
            os.remove(os.path.join(args.out, "pages", name))
        state = {"collection": args.collection, "next_cursor": None, "pages": [], "done": False}

    if args.lane == "rest":
        stats = fetch_rest(args, stores, state, state_path)
    else:
        stats = fetch_graphql(args, stores, wanted, state, state_path, store_source)

    stats["store_source"] = store_source
    stats["lane"] = args.lane
    stats["partial"] = bool(args.limit) or not state.get("done")
    if args.lane == "rest":
        stats["per_store_pricing"] = False
    write_json_atomic(state_path, state)
    print(json.dumps(stats, indent=1))
    if stats.get("ok_pages", 0) == 0:
        print("\nABORT: every page failed - the diagnosis is the payload, not the data. See above.")
        return 2
    return 0


def fetch_graphql(args, stores, wanted, state, state_path, store_source):
    t0 = time.time()
    query = COLLECTION_QUERY.replace("__MF__", mf_identifiers(wanted, args.with_special_text))
    token, html, status = discover_token(args.collection)
    print("token discovered (…%s), page HTTP %s, page %d bytes"
          % (token[-8:], status, len(html)))
    st = discover_stores(html=html, collection=args.collection, verbose=False)[0]
    for k, v in st.items():
        stores.setdefault(k, v)

    api_version, _d, headers = negotiate_api_version(
        token, query, {"handle": args.collection, "first": 1, "after": None})
    print("api version: %s | query cost: %s" % (
        api_version, (headers or {}).get("X-Shopify-Storefront-Api-Call-Limit", "n/a")))

    n_products = 0
    ok_pages = 0
    fail_pages = 0
    consecutive_fail = 0
    page_idx = len(state.get("pages") or [])
    cursor = None if args.fresh else state.get("next_cursor")
    if cursor and state.get("pages"):
        print("resuming from cursor of page %d" % page_idx)

    while True:
        page_file = os.path.join(args.out, "pages", "page-%04d.json" % (page_idx + 1))
        try:
            data, headers = gql_call(api_version, token, query,
                                     {"handle": args.collection, "first": PAGE_SIZE, "after": cursor})
            products = (((data.get("collectionByHandle") or {}).get("products")) or {})
            edges = products.get("edges") or []
            nodes = [e["node"] for e in edges]
            info = products.get("pageInfo") or {}
            page_idx += 1
            n_products += len(nodes)
            ok_pages += 1
            consecutive_fail = 0
            write_json_atomic(page_file, {
                "page": page_idx, "cursor_in": cursor, "fetched_at": datetime.now(timezone.utc).isoformat(),
                "count": len(nodes), "next_cursor": info.get("endCursor"),
                "has_next_page": bool(info.get("hasNextPage")), "nodes": nodes})
            state["pages"] = sorted(set((state.get("pages") or []) + [os.path.basename(page_file)]))
            state["next_cursor"] = info.get("endCursor")
            state["done"] = not info.get("hasNextPage")
            if state["done"]:
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
            write_json_atomic(state_path, state)
            print("page %3d: %4d products (total %5d) next=%s cost/pg=%s"
                  % (page_idx, len(nodes), n_products, bool(state["next_cursor"]),
                     (data.get("extensions") or {}).get("cost", {}).get("requestedQueryCost")))
            if args.limit and n_products >= args.limit:
                state["done"] = False
                print("--limit %d reached - stopping, output will be flagged partial" % args.limit)
                break
            if not info.get("hasNextPage"):
                print("collection end reached")
                break
            cursor = info.get("endCursor")
            if not cursor:
                print("! hasNextPage true but no endCursor - stopping rather than looping")
                break
            time.sleep(args.sleep)
        except (ScrapeError, CredentialError) as e:
            fail_pages += 1
            consecutive_fail += 1
            print("  X page %d failed: %s" % (page_idx + 1, e))
            if ok_pages == 0 and consecutive_fail >= FAIL_FAST_AFTER:
                print("  X fail-fast: %d consecutive failures with 0 pages fetched - aborting. "
                      "Run `diagnose` and fix the transport before retrying." % consecutive_fail)
                break
            if consecutive_fail >= FAIL_FAST_AFTER:
                print("  X giving up after %d consecutive failed pages (state kept, re-run to resume)"
                      % consecutive_fail)
                break

    return {"lane": "graphql", "per_store_pricing": True, "api_version": api_version,
            "ok_pages": ok_pages, "failed_pages": fail_pages, "products": n_products,
            "seconds": round(time.time() - t0, 1), "done": bool(state.get("done"))}


def fetch_rest(args, stores, state, state_path):
    """Fallback lane: /collections/<h>/products.json. Base price only - NO per-store prices."""
    t0 = time.time()
    print("!! REST lane: gives variant.price (base/web bucket) only. "
          "custom.price_location_* is NOT exposed here - per-store prices will be empty.")
    all_nodes = []
    page = (len(state.get("pages") or []) or 0) + 1
    ok_pages = 0
    fail_pages = 0
    consecutive_fail = 0
    while True:
        url = "%s/collections/%s/products.json?limit=%d&page=%d" % (SHOP, args.collection, REST_PAGE_SIZE, page)
        try:
            data, status, _ = http_request(url)
            prods = data.get("products") or []
            for p in prods:
                all_nodes.append(rest_to_node(p))
            page_file = os.path.join(args.out, "pages", "rest-page-%04d.json" % page)
            write_json_atomic(page_file, {"page": page, "count": len(prods), "nodes": [rest_to_node(p) for p in prods]})
            state["pages"] = sorted(set((state.get("pages") or []) + [os.path.basename(page_file)]))
            ok_pages += 1
            consecutive_fail = 0
            print("rest page %3d: %4d products (total %5d)" % (page, len(prods), len(all_nodes)))
            write_json_atomic(state_path, state)
            if args.limit and len(all_nodes) >= args.limit:
                state["done"] = False
                break
            if len(prods) < REST_PAGE_SIZE:
                state["done"] = True
                break
            page += 1
            time.sleep(args.sleep)
        except ScrapeError as e:
            fail_pages += 1
            consecutive_fail += 1
            print("  X rest page %d failed: %s" % (page, e))
            if ok_pages == 0 and consecutive_fail >= FAIL_FAST_AFTER:
                break
            if consecutive_fail >= FAIL_FAST_AFTER:
                break
    cache = os.path.join(args.out, "harrisfarm_specials_raw_rest.json")
    write_json_atomic(cache, {"lane": "rest", "collection": args.collection, "nodes": all_nodes})
    return {"lane": "rest", "per_store_pricing": False, "ok_pages": ok_pages,
            "failed_pages": fail_pages, "products": len(all_nodes),
            "seconds": round(time.time() - t0, 1), "done": bool(state.get("done"))}


def rest_to_node(p):
    """Shape a REST product into the GraphQL node shape so `build` has one code path."""
    variants = []
    for v in (p.get("variants") or []):
        variants.append({
            "id": "gid://shopify/ProductVariant/%s" % v.get("id"),
            "sku": v.get("sku"), "title": v.get("title"),
            "price": {"amount": v.get("price"), "currencyCode": "AUD"},
            "compareAtPrice": ({"amount": v.get("compare_at_price"), "currencyCode": "AUD"}
                               if v.get("compare_at_price") else None),
            "availableForSale": bool(v.get("available")),
            "quantityAvailable": None,
            "selectedOptions": [{"name": "Title", "value": v.get("title") or ""}],
        })
    img = ((p.get("images") or [{}])[0] or {}).get("src") if p.get("images") else None
    return {"id": "gid://shopify/Product/%s" % p.get("id"), "title": p.get("title"),
            "handle": p.get("handle"), "vendor": p.get("vendor"),
            "productType": p.get("product_type"), "tags": p.get("tags") or [],
            "featuredImage": ({"url": img} if img else None),
            "variants": {"edges": [{"node": v} for v in variants]},
            "metafields": []}


# --------------------------------------------------------------------------- build
def load_raw(out_dir, collection):
    """Read the raw cache. Prefer page files (merge from disk, never from this run's memory)."""
    nodes = []
    pages_dir = os.path.join(out_dir, "pages")
    files = sorted(f for f in (os.listdir(pages_dir) if os.path.isdir(pages_dir) else [])
                   if f.endswith(".json") and f.startswith("page-"))
    for f in files:
        try:
            with io.open(os.path.join(pages_dir, f), encoding="utf-8") as fh:
                nodes += json.load(fh).get("nodes") or []
        except ValueError:
            print("  ! corrupt page file %s - skipped (re-run `fetch` to repair)" % f)
    if not nodes:
        for cand in ("harrisfarm_specials_raw.json", "harrisfarm_specials_raw_rest.json"):
            path = os.path.join(out_dir, cand)
            if os.path.exists(path):
                with io.open(path, encoding="utf-8") as fh:
                    blob = json.load(fh)
                if isinstance(blob, list):
                    nodes = blob
                else:
                    nodes = blob.get("nodes") or []
                if nodes:
                    print("raw cache loaded from %s" % cand)
                    break
    else:
        write_json_atomic(os.path.join(out_dir, "harrisfarm_specials_raw.json"),
                          {"collection": collection, "page_files": files, "nodes": nodes})
    return nodes, files


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "unnamed"


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# variant.price (the "base"/location-1 bucket) is ONLY comparable to a store price on items sold
# per countable unit. On weight/volume items it is a different unit entirely - verified on the live
# shop 2026-10-09: "Whittakers Chocolate Peanut Slab 50g" has base 15.99 (uom / 100g) while the page
# and price_location_70 both show 2.00; "Marshmallow Kebab 40g" base 39.99 vs real 0.50.
# So a saving is only claimed for countable uoms; everything else keeps base_price as a reference
# with base_price_trusted=0 and no saving. Never chart a discount off an untrusted reference.
COUNTABLE_UOM = re.compile(
    r"^(/\s*)?(each|item|bunch|bag|box|pack|punnet|capsule|tablet|serving|100 sheets|100 units)$",
    re.IGNORECASE)


def base_price_trusted(uom):
    return bool(uom) and bool(COUNTABLE_UOM.match(uom.strip()))


def price_status(p):
    """Classify a store price. Real prices sit in (0, 999); the two outliers are sentinels."""
    if p is None:
        return None
    if p <= PRICE_ZERO:
        return "unavailable_zero"
    if p >= PRICE_SENTINEL:
        return "unavailable_sentinel"
    return "ok"


def flatten_node(node, store_ids):
    """One product node -> flat record with a per-store price map + long-format rows."""
    mf = {}
    for m in (node.get("metafields") or []):
        if m and m.get("key"):
            mf[m["key"]] = m.get("value")
    variants = [e["node"] for e in ((node.get("variants") or {}).get("edges") or [])]
    v0 = variants[0] if variants else {}
    price = (v0.get("price") or {}).get("amount")
    compare_at = ((v0.get("compareAtPrice") or {}).get("amount")) if v0.get("compareAtPrice") else None
    hid = (node.get("id") or "").rsplit("/", 1)[-1]
    handle = node.get("handle")
    img = ((node.get("featuredImage") or {}) or {}).get("url")

    def as_list(key):
        raw = mf.get(key)
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return list(val) if isinstance(val, list) else [str(val)]
        except ValueError:
            return [t.strip() for t in re.split(r"[,\s]+", str(raw)) if t.strip()]

    specials = as_list("special_locations")
    available = as_list("available_locations")
    prices, unit_prices, special_text = {}, {}, {}
    for sid in store_ids:
        p = to_float(mf.get("price_location_%s" % sid))
        if p is not None:
            prices[sid] = p
        up = to_float(mf.get("unit_price_location_%s" % sid))
        if up is not None and up > 0:      # 0.0 = "no unit price computed", not a price
            unit_prices[sid] = up
        t = mf.get("special_text_location_%s" % sid)
        if t:
            special_text[sid] = t

    rec = {
        "product_id": node.get("id"), "numeric_id": int(hid) if hid.isdigit() else None,
        "handle": handle, "title": node.get("title"), "vendor": node.get("vendor"),
        "product_type": node.get("productType"), "tags": node.get("tags") or [],
        "url": "%s/products/%s" % (SHOP, handle) if handle else None,
        "image": img, "sku": v0.get("sku"), "variant_id": v0.get("id"),
        "variant_title": v0.get("title"),
        "uom": mf.get("uom"), "conversion": to_float(mf.get("conversion")),
        "overridecup": mf.get("overridecup"), "hide_in_state": mf.get("hideInState"),
        # variant.price == the base / location-"1" bucket. NOT necessarily a shelf regular price.
        "base_price": to_float(price), "base_compare_at_price": to_float(compare_at),
        "available_locations": available, "special_locations": specials,
        "special_store_count": len(specials),
        "prices": prices, "unit_prices": unit_prices,
        # only present for stores whose price is a sentinel (0 = not available this week,
        # 999.99 = cannot be sold there) - absent means "ok"
        "price_status": {sid: st for sid, st in ((s, price_status(p)) for s, p in prices.items())
                         if st and st != "ok"},
        "base_price_trusted": int(base_price_trusted(mf.get("uom"))),
        "available_for_sale": v0.get("availableForSale"),
        "quantity_available": v0.get("quantityAvailable"),
        "variant_count": len(variants),
    }
    if special_text:
        rec["special_text"] = special_text
    return rec


def build_rows(rec, stores, store_ids):
    rows = []
    for sid in store_ids:
        price = rec["prices"].get(sid)
        base = rec["base_price"]
        on_special = sid in rec["special_locations"]
        st = price_status(price)
        trusted = rec.get("base_price_trusted", 0)
        saving = saving_pct = None
        # a saving is only claimed between two real prices of the SAME unit
        if (on_special and st == "ok" and trusted and base is not None
                and price_status(base) == "ok" and base > price):
            saving = round(base - price, 2)
            saving_pct = round(100.0 * (base - price) / base, 1)
        rows.append({
            "store_id": sid, "store_name": stores.get(sid, {}).get("name", ""),
            "sku": rec["sku"], "title": rec["title"], "product_type": rec["product_type"],
            "uom": rec["uom"], "unit_price": rec["unit_prices"].get(sid),
            "price": price, "price_status": st, "base_price": base,
            "base_price_trusted": trusted, "on_special": int(bool(on_special)),
            "saving_vs_base": saving, "saving_pct_vs_base": saving_pct,
            "in_special_locations": int(sid in rec["special_locations"]),
            "in_available_locations": int(sid in rec["available_locations"]),
            "handle": rec["handle"], "url": rec["url"], "image": rec["image"],
            "vendor": rec["vendor"], "tags": "|".join(rec["tags"] or []),
        })
    return rows


def cmd_build(args):
    stores_path = os.path.join(args.out, "harrisfarm_stores.json")
    if os.path.exists(stores_path) and not args.stores:
        with io.open(stores_path, encoding="utf-8") as fh:
            stores_blob = json.load(fh)
        stores = stores_blob.get("stores") or {}
        store_source = stores_blob.get("source", "cache")
    else:
        stores, store_source = discover_stores(collection=args.collection)
        write_json_atomic(stores_path, {"fetched_at": datetime.now(timezone.utc).isoformat(),
                                        "source": store_source,
                                        "stores": stores, "count": len(stores)})
    if args.stores:
        stores = {k: v for k, v in stores.items() if k in args.stores}

    store_ids = sorted(stores.keys(), key=lambda s: int(s))
    nodes, page_files = load_raw(args.out, args.collection)
    if not nodes:
        print("no raw products found in %s - run `fetch` first" % args.out)
        return 2
    # de-dup on product id; page files can overlap after a resume
    seen, uniq = set(), []
    for n in nodes:
        pid = n.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        uniq.append(n)
    if len(uniq) != len(nodes):
        print("de-duplicated %d duplicate product nodes from overlapping pages" % (len(nodes) - len(uniq)))

    recs = [flatten_node(n, store_ids) for n in uniq]
    if args.limit:
        recs = recs[:args.limit]

    # 1) flat per-product jsonl
    jsonl_path = os.path.join(args.out, "harrisfarm_specials.jsonl")
    with io.open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 2) long CSV: one row per (product x store)
    long_rows = []
    for r in recs:
        long_rows += build_rows(r, stores, store_ids)
    csv_path = os.path.join(args.out, "harrisfarm_specials_prices.csv")
    cols = ["store_id", "store_name", "sku", "title", "product_type", "uom", "unit_price",
            "price", "price_status", "base_price", "base_price_trusted", "on_special",
            "saving_vs_base", "saving_pct_vs_base", "in_special_locations",
            "in_available_locations", "handle", "url", "image", "vendor", "tags"]
    with io.open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in long_rows:
            w.writerow(row)

    # 3) per-store files (Drakes-style) - only the rows that are actually on special there
    store_dir = os.path.join(args.out, "stores")
    os.makedirs(store_dir, exist_ok=True)
    summary = []
    for sid in store_ids:
        name = stores[sid]["name"]
        items = []
        for r in recs:
            if sid not in r["special_locations"]:
                continue
            price = r["prices"].get(sid)
            base = r["base_price"]
            st = price_status(price)
            trusted = r.get("base_price_trusted", 0)
            items.append({
                "sku": r["sku"], "title": r["title"], "product_type": r["product_type"],
                "uom": r["uom"], "price": price, "price_status": st,
                "unit_price": r["unit_prices"].get(sid),
                "base_price": base, "base_price_trusted": trusted,
                "saving_vs_base": (round(base - price, 2)
                                   if st == "ok" and trusted and base is not None
                                   and price_status(base) == "ok" and base > price else None),
                "handle": r["handle"], "url": r["url"], "image": r["image"],
                "vendor": r["vendor"], "tags": r["tags"],
            })
        sellable = sum(1 for r in recs if sid in r["prices"])
        priced = [i["price"] for i in items if price_status(i["price"]) == "ok"]
        blob = {"store_id": sid, "store_name": name,
                "collection": args.collection, "source": SHOP,
                "note": ("price_location_%s is the current price at this store; rows here are the "
                         "products whose special_locations metafield lists this store. "
                         "price_status 'unavailable_zero'/'unavailable_sentinel' = HF's own "
                         "'not sellable here right now' marker, not a price. "
                         "saving_vs_base is only filled when base_price_trusted=1 (item sold per "
                         "countable unit) - on weight/volume items variant.price is a different "
                         "unit and the field is null by design." % sid),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "special_count": len(items), "special_count_priced": len(priced),
                "sellable_product_count": sellable,
                "items": items}
        write_json_atomic(os.path.join(store_dir, "%s-%s.json" % (sid, slugify(name))), blob)
        summary.append({"store_id": sid, "store_name": name, "special_count": len(items),
                        "special_count_priced": len(priced),
                        "sellable_product_count": sellable,
                        "avg_special_price": round(sum(priced) / len(priced), 2) if priced else None})

    sum_path = os.path.join(args.out, "harrisfarm_store_summary.csv")
    with io.open(sum_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["store_id", "store_name", "sellable_product_count",
                                           "special_count", "special_count_priced",
                                           "avg_special_price"])
        w.writeheader()
        for row in summary:
            w.writerow(row)

    # 4) fill rates + run summary
    n = len(recs)
    def rate(fn):
        return round(100.0 * sum(1 for r in recs if fn(r)) / n, 1) if n else 0.0
    run_path = os.path.join(args.out, "harrisfarm_run_summary.json")
    prev = {}
    if os.path.exists(run_path):
        try:
            with io.open(run_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except ValueError:
            prev = {}
    summary_blob = dict(prev)
    summary_blob.update({
        "collection": args.collection, "lane": prev.get("lane", args.lane),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "products": n, "page_files": len(page_files), "stores": len(store_ids),
        "store_source": store_source,
        "long_rows": len(long_rows),
        "fill_rates_pct": {
            "sku": rate(lambda r: bool(r["sku"])),
            "image": rate(lambda r: bool(r["image"])),
            "uom": rate(lambda r: bool(r["uom"])),
            "product_type": rate(lambda r: bool(r["product_type"])),
            "any_store_price": rate(lambda r: bool(r["prices"])),
            "real_store_price": rate(lambda r: any(price_status(p) == "ok" for p in r["prices"].values())),
            "special_locations": rate(lambda r: bool(r["special_locations"])),
            "special_text": rate(lambda r: bool(r.get("special_text"))),
            "compare_at_price": rate(lambda r: r["base_compare_at_price"] is not None),
            "base_price_trusted": rate(lambda r: bool(r.get("base_price_trusted"))),
        },
        "outputs": [os.path.basename(jsonl_path), os.path.basename(csv_path),
                    os.path.basename(sum_path), "stores/", os.path.basename(run_path)],
    })
    write_json_atomic(run_path, summary_blob)

    print("products: %d | long rows: %d | stores: %d" % (n, len(long_rows), len(store_ids)))
    print("fill rates: %s" % json.dumps(summary_blob["fill_rates_pct"]))
    print("wrote: %s" % ", ".join(summary_blob["outputs"]))
    return 0


# --------------------------------------------------------------------------- verify
def cmd_verify(args):
    out = args.out
    problems, notes = [], []
    run_path = os.path.join(out, "harrisfarm_run_summary.json")
    jsonl_path = os.path.join(out, "harrisfarm_specials.jsonl")
    csv_path = os.path.join(out, "harrisfarm_specials_prices.csv")
    for p in (run_path, jsonl_path, csv_path):
        if not os.path.exists(p):
            print("MISSING %s - run `all` first" % p)
            return 2
    with io.open(run_path, encoding="utf-8") as fh:
        run = json.load(fh)
    recs = []
    with io.open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                recs.append(json.loads(line))
    with io.open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if run.get("partial"):
        notes.append("run is flagged PARTIAL (--limit or an unfinished fetch) - do not treat as complete")
    if run.get("lane") == "rest":
        notes.append("lane=rest: per-store prices are EMPTY by construction (products.json has no metafields)")

    print("products in jsonl: %d | run summary says: %d" % (len(recs), run.get("products", -1)))
    if len(recs) != run.get("products"):
        problems.append("jsonl product count != run summary count")

    skus = [r["sku"] for r in recs if r.get("sku")]
    dup_sku = {s for s in skus if skus.count(s) > 1}
    handles = [r["handle"] for r in recs]
    dup_handle = {h for h in handles if handles.count(h) > 1}
    print("unique sku: %d/%d | duplicate sku: %d | duplicate handle: %d"
          % (len(set(skus)), len(skus), len(dup_sku), len(dup_handle)))
    if dup_handle:
        problems.append("%d duplicate handles - a resume probably double-wrote a page" % len(dup_handle))
    if dup_sku:
        notes.append("%d skus appear on more than one product (HF reuses skus across pack sizes) - expected, join on sku+title" % len(dup_sku))

    # every long CSV row must have a store row and a jsonl parent
    bad = 0
    for row in rows:
        if not row.get("store_id"):
            bad += 1
    print("long CSV rows: %d (expected products x stores = %d) | rows without store_id: %d"
          % (len(rows), len(recs) * run.get("stores", 0), bad))
    if bad:
        problems.append("%d long rows missing store_id" % bad)

    # specials actually carried a real price at that store?
    spec_no_price = 0
    zero_sent = 0
    big_sent = 0
    weird = []
    for r in recs:
        for sid in r["special_locations"]:
            p = r["prices"].get(sid)
            st = price_status(p)
            if p is None:
                spec_no_price += 1
            elif st == "unavailable_zero":
                zero_sent += 1
            elif st == "unavailable_sentinel":
                big_sent += 1
    for sid, p in ((s, v) for r in recs for s, v in r["prices"].items()):
        if p is not None and 500 < p < PRICE_SENTINEL:
            weird.append((sid, p))
    print("special pairs with no price at all: %d | sentinel 0.0: %d | sentinel >=999: %d | unexplained 500-999: %d"
          % (spec_no_price, zero_sent, big_sent, len(weird)))
    if spec_no_price:
        notes.append("%d (product, store) pairs are flagged special but carry no price_location metafield" % spec_no_price)
    if zero_sent:
        notes.append("%d special pairs carry the 0.0 sentinel = HF's 'not sellable at this store right now' "
                     "(price_status=unavailable_zero). Not a free product." % zero_sent)
    if big_sent:
        notes.append("%d pairs carry the >=999.99 sentinel (price_status=unavailable_sentinel) - "
                     "liquor at the QLD stores, not sellable there" % big_sent)
    if weird:
        problems.append("%d prices in the unexplained 500-999 band, e.g. %s" % (len(weird), weird[:3]))

    # per-store coverage: which stores have no price columns at all (off the online grid)
    cov = {}
    for r in recs:
        for sid in r["prices"]:
            cov[sid] = cov.get(sid, 0) + 1
    are_stores = set()
    with io.open(os.path.join(out, "harrisfarm_store_summary.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            are_stores.add(row["store_id"])
    off_grid = sorted(s for s in are_stores if cov.get(s, 0) == 0)
    if off_grid and run.get("lane") != "rest":
        notes.append("stores with no price_location_* metafield at all (outside the online pricing grid): "
                     "%s - their per-store files are expected to be empty" % ", ".join(off_grid))

    # per-store files: items must all be on special for that store
    sdir = os.path.join(out, "stores")
    files = sorted(f for f in os.listdir(sdir) if f.endswith(".json")) if os.path.isdir(sdir) else []
    print("per-store files: %d" % len(files))
    if not files:
        problems.append("no per-store files were written")
    for f in files:
        with io.open(os.path.join(sdir, f), encoding="utf-8") as fh:
            blob = json.load(fh)
        if blob["special_count"] != len(blob["items"]):
            problems.append("%s: special_count != len(items)" % f)
        if blob["special_count_priced"] > blob["special_count"]:
            problems.append("%s: special_count_priced > special_count" % f)
        if blob["special_count"] == 0 and blob["store_id"] not in off_grid:
            problems.append("%s: 0 specials but the store is on the grid - suspicious" % f)
    print("fill rates from run summary: %s" % json.dumps(run.get("fill_rates_pct")))
    if (run.get("fill_rates_pct") or {}).get("any_store_price", 100) < 50:
        if run.get("lane") == "rest":
            notes.append("per-store price fill rate is 0 because this run used lane=rest - expected, not a bug")
        else:
            problems.append("fewer than half the products carry any per-store price - wrong lane or a broken query")

    # base_price reference trustworthiness (drives whether a saving may be claimed at all)
    trusted_n = sum(1 for r in recs if r.get("base_price_trusted"))
    print("base_price comparable (countable uom): %d/%d products (%.1f%%) - savings only computed there"
          % (trusted_n, len(recs), 100.0 * trusted_n / len(recs) if recs else 0.0))
    bad_saving = 0
    for row in rows:
        if row.get("saving_vs_base") not in (None, "") and row.get("base_price_trusted") == "0":
            bad_saving += 1
    if bad_saving:
        problems.append("%d rows claim a saving off an untrusted base_price" % bad_saving)

    print("\n--- VERDICT ---")
    for n_ in notes:
        print("  note: %s" % n_)
    if problems:
        for p in problems:
            print("  PROBLEM: %s" % p)
        return 1
    print("  no structural problems found")
    return 0


# --------------------------------------------------------------------------- cli
def cmd_stores(args):
    if args.fresh or not os.path.exists(os.path.join(args.out, "harrisfarm_stores.json")):
        stores, source = discover_stores(collection=args.collection)
        os.makedirs(args.out, exist_ok=True)
        write_json_atomic(os.path.join(args.out, "harrisfarm_stores.json"),
                          {"fetched_at": datetime.now(timezone.utc).isoformat(), "source": source,
                           "stores": stores, "count": len(stores)})
    else:
        with io.open(os.path.join(args.out, "harrisfarm_stores.json"), encoding="utf-8") as fh:
            blob = json.load(fh)
        stores, source = blob["stores"], blob["source"]
    print("source: %s | %d stores" % (source, len(stores)))
    for sid in sorted(stores, key=lambda s: int(s)):
        print("  %3s  %-24s %s" % (sid, stores[sid]["name"], stores[sid].get("gid") or "-"))
    print("\nNote: 59/73 are in the store map but carry no price_location_* metafields today "
          "(they are outside the online pricing grid). Location 1 is the base price bucket.")
    return 0


def cmd_diagnose(args):
    """One request per layer, print what came back, end with a verdict. Run this first."""
    verdict = []
    print("== 1/4 collection page + storefront token ==")
    try:
        token, html, status = discover_token(args.collection)
        print("  HTTP %s | %d bytes | token …%s" % (status, len(html), token[-8:]))
        verdict.append("reachable")
    except Exception as e:
        print("  FAILED: %s" % e)
        verdict.append("unreachable")
        token = None

    print("== 2/4 store list (location-manager.js) ==")
    try:
        stores, source = discover_stores(html=html if token else None, collection=args.collection)
        print("  %d stores from %s" % (len(stores), source))
    except Exception as e:
        print("  FAILED: %s" % e)
        stores = {}

    print("== 3/4 Storefront GraphQL (per-store prices) ==")
    if not token:
        print("  skipped: no token")
        verdict.append("no-token")
    else:
        try:
            wanted = sorted(stores, key=lambda s: int(s))[:3]
            q = COLLECTION_QUERY.replace("__MF__", mf_identifiers(wanted, False))
            v, data, headers = negotiate_api_version(token, q, {"handle": args.collection, "first": 5, "after": None})
            prods = (((data.get("collectionByHandle") or {}).get("products")) or {})
            edges = prods.get("edges") or []
            print("  API %s | first page edges: %d | has_next: %s | cost: %s"
                  % (v, len(edges), (prods.get("pageInfo") or {}).get("hasNextPage"),
                     (data.get("extensions") or {}).get("cost")))
            if edges:
                node = edges[0]["node"]
                mf = {m["key"]: m["value"] for m in (node.get("metafields") or []) if m}
                print("  sample: %s | base_price=%s | price_location_%s=%s | special_locations=%s"
                      % (node.get("title"), node["variants"]["edges"][0]["node"]["price"]["amount"],
                         wanted[0], mf.get("price_location_%s" % wanted[0]), mf.get("special_locations")))
                verdict.append("per-store-pricing-ok")
        except Exception as e:
            print("  FAILED: %s" % e)
            verdict.append("graphql-broken")

    print("== 4/4 REST lane (fallback, base prices only) ==")
    try:
        data, status, _ = http_request("%s/collections/%s/products.json?limit=5" % (SHOP, args.collection))
        print("  HTTP %s | products: %d (no per-store price in this shape)" % (status, len(data.get("products") or [])))
    except Exception as e:
        print("  FAILED: %s" % e)

    print("\nVERDICT: %s" % ", ".join(verdict))
    if "per-store-pricing-ok" in verdict:
        print("Storefront GraphQL works -> run:  python harrisfarm_specials.py all")
    elif "no-token" in verdict or "graphql-broken" in verdict:
        print("Per-store pricing unavailable -> run:  python harrisfarm_specials.py all --lane rest")
        print("  (base prices only; every price_location_* column will be empty)")
    else:
        print("Nothing reachable from this network. Fix egress, then re-run diagnose.")
        return 1
    if stores and set(stores).intersection({"11", "59", "73"}):
        print("Note: stores 11 (Meats Pennant Hills), 59 (Bathurst) and 73 (Online) are in the "
              "theme's store map but carry no price_location_* metafields today - they are outside "
              "the online pricing grid, so their per-store files come back empty. That is real, "
              "not a scrape failure.")
    return 0


def parse_stores_arg(raw):
    if not raw:
        return None
    return {s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()}


CHEAT = """
harrisfarm_specials.py - Harris Farm Markets online specials (+ per-store prices)

  %(p)s stores                       store list: id -> name (live from the theme JS)
  %(p)s diagnose                     one request per layer, prints the verdict. RUN THIS FIRST.
  %(p)s fetch                        download the collection (resumable, atomic per page)
  %(p)s build                        raw cache -> jsonl + long CSV + one file per store
  %(p)s all                          fetch + build  (the normal run)
  %(p)s verify                       audit outputs, non-zero exit on a real problem

Useful flags
  --out DIR            output directory (default ./harrisfarm_data)
  --collection HANDLE  default online-specials (also: grocery-specials, fruit-and-veg-specials)
  --stores 70,84       keep only these stores' price columns
  --lane rest          fallback: base prices only, NO per-store prices (loud warning)
  --limit 50           smoke test; marks the run partial
  --fresh              wipe the page cache + state and re-download
  --with-special-text  also query special_text_location_* (currently empty on this shop)

Nothing was run. Pick a command. Full docs: header of this file.
""" % {"p": os.path.basename(sys.argv[0])}


def main(argv):
    ap = argparse.ArgumentParser(add_help=True, description="Harris Farm Markets online specials extractor")
    ap.add_argument("command", nargs="?", default=None,
                    choices=["stores", "diagnose", "fetch", "build", "all", "verify"])
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "harrisfarm_data"))
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    ap.add_argument("--stores", default=None, help="comma separated store numbers, e.g. 70,84")
    ap.add_argument("--lane", default="graphql", choices=["graphql", "rest"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--max-cache-age-hours", type=float, default=6.0, dest="max_cache_age_hours",
                    help="reuse a completed download if it is younger than this (default 6h)")
    ap.add_argument("--sleep", type=float, default=0.4, help="pause between pages")
    ap.add_argument("--with-special-text", action="store_true", dest="with_special_text")
    args = ap.parse_args(argv)
    args.stores = parse_stores_arg(args.stores)

    if not args.command:
        print(CHEAT)
        return 0

    print("pid=%d | %s | command=%s | out=%s"
          % (os.getpid(), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), args.command, args.out))
    sys.stdout.flush()
    t0 = time.time()
    try:
        if args.command == "stores":
            rc = cmd_stores(args)
        elif args.command == "diagnose":
            rc = cmd_diagnose(args)
        elif args.command == "fetch":
            rc = cmd_fetch(args)
        elif args.command == "build":
            rc = cmd_build(args)
        elif args.command == "all":
            rc = cmd_fetch(args)
            if rc == 0:
                rc = cmd_build(args)
        elif args.command == "verify":
            rc = cmd_verify(args)
        else:
            print(CHEAT)
            rc = 0
    except KeyboardInterrupt:
        print("\ninterrupted - state and pages are on disk, re-run the same command to resume")
        return 130
    except ScrapeError as e:
        print("\nSCRAPE FAILURE: %s" % e)
        return 2
    print("done in %.1fs (exit %d)" % (time.time() - t0, rc))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
