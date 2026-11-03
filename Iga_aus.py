# import argparse
# import html
# import json
# import os
# import random
# import re
# import sys
# import threading
# import time
# import urllib.error
# import urllib.parse
# import urllib.request
# from concurrent.futures import ThreadPoolExecutor, as_completed

# API_BASE = "https://www.igashop.com.au/api/storefront"
# HEADERS = {
#     "accept": "application/json",
#     "accept-language": "en-US,en;q=0.9",
#     "x-shopping-mode": "11111111-1111-1111-1111-111111111111",  # required
#     "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
#                   "(KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36",
# }
# PAGE = 100          # API max take
# SKIP_CAP = 9500     # stay under the server's 10,000-result query cap

# _print_lock = threading.Lock()
# stats_lock = threading.Lock()


# def log(msg):
#     with _print_lock:
#         print(msg, flush=True)


# def clean_text(raw_text):
#     """Strips HTML tags and converts HTML entities (&trade;, &deg;, etc.) to plain text."""
#     if not raw_text or not isinstance(raw_text, str):
#         return ""
#     # Unescape HTML entities (&deg; -> °, &nbsp; -> space, etc.)
#     text = html.unescape(raw_text)
#     # Strip HTML tags like <br />, <div>, etc.
#     text = re.sub(r"<[^>]+>", " ", text)
#     # Collapse multiple whitespace/newlines into single spaces
#     return " ".join(text.split())


# def transform_item(item):
#     """Flattens and normalizes raw API items into a clean schema."""
#     if not isinstance(item, dict):
#         return {}

#     # Standardize image URLs
#     images = item.get("images") or item.get("image") or []
#     if isinstance(images, dict):
#         images = [images.get("url")] if images.get("url") else []
#     elif isinstance(images, list):
#         images = [img.get("url") if isinstance(img, dict) else str(img) for img in images if img]

#     # Clean description
#     raw_desc = item.get("description") or item.get("longDescription") or ""

#     return {
#         "productId": item.get("productId") or item.get("sku") or "",
#         "sku": item.get("sku") or item.get("productId") or "",
#         "name": item.get("name") or "",
#         "brand": item.get("brand") or "",
#         "description": clean_text(raw_desc),
#         "pricing": {
#             "price": item.get("priceNumeric") if item.get("priceNumeric") is not None else item.get("wholePrice"),
#             "wasPrice": item.get("wasPriceNumeric"),
#             "priceFormatted": item.get("price") or "",
#             "wasPriceFormatted": item.get("wasPrice") or "",
#             "priceLabel": item.get("priceLabel") or "",
#             "pricePerUnit": item.get("pricePerUnit") or ""
#         },
#         "unit": {
#             "label": (item.get("unitOfSize") or {}).get("label") if isinstance(item.get("unitOfSize"), dict) else "",
#             "size": (item.get("unitOfSize") or {}).get("size") if isinstance(item.get("unitOfSize"), dict) else None,
#             "unitOfMeasure": (item.get("unitOfMeasure") or {}).get("label") if isinstance(item.get("unitOfMeasure"), dict) else ""
#         },
#         "images": images,
#         "categories": item.get("categories") or [],
#         "availability": {
#             "inStock": item.get("inStock", True),
#             "isAvailable": item.get("isAvailable", True)
#         }
#     }


# def transform_store(raw_store):
#     """Extracts and formats store info cleanly."""
#     if not isinstance(raw_store, dict):
#         return {}

#     loc = raw_store.get("location") or {}
#     return {
#         "storeId": raw_store.get("id"),
#         "retailerStoreId": raw_store.get("retailerStoreId"),
#         "name": raw_store.get("name"),
#         "status": raw_store.get("status"),
#         "type": raw_store.get("type"),
#         "address": {
#             "addressLine1": raw_store.get("addressLine1") or "",
#             "addressLine2": raw_store.get("addressLine2") or "",
#             "city": raw_store.get("city") or "",
#             "state": raw_store.get("countyProvinceState") or "",
#             "postCode": raw_store.get("postCode") or "",
#             "country": raw_store.get("country") or ""
#         },
#         "contact": {
#             "email": raw_store.get("email") or "",
#             "phone": raw_store.get("phone") or ""
#         },
#         "location": {
#             "latitude": loc.get("latitude"),
#             "longitude": loc.get("longitude")
#         },
#         "openingHours": raw_store.get("openingHours") or "",
#         "timeZone": raw_store.get("timeZone") or "",
#         "currency": raw_store.get("currency") or "AUD"
#     }


# def http_get(url, timeout=45):
#     """One GET with retry/backoff. Raises on repeated failure."""
#     last_err = None
#     for attempt in range(5):
#         try:
#             req = urllib.request.Request(url, headers=HEADERS)
#             with urllib.request.urlopen(req, timeout=timeout) as r:
#                 return json.loads(r.read().decode("utf-8"))
#         except urllib.error.HTTPError as e:
#             last_err = e
#             if e.code in (429, 500, 502, 503, 504, 408):
#                 backoff = (2 ** attempt) + random.random()
#                 time.sleep(backoff)
#                 continue
#             raise
#         except (urllib.error.URLError, TimeoutError, OSError) as e:
#             last_err = e
#             time.sleep((2 ** attempt) + random.random())
#     raise last_err


# def paginate(url_prefix, declared_total=None, delay=0.15, on_page=None):
#     """Yield item lists page by page using skip. Stops on empty page or when
#     skip passes the declared total (whichever is smaller)."""
#     skip = 0
#     while True:
#         sep = "&" if "?" in url_prefix else "?"
#         data = http_get(f"{url_prefix}{sep}take={PAGE}&skip={skip}")
#         items = data.get("items") or []
#         if not items:
#             break
#         total = data.get("total")
#         if declared_total is None and total is not None:
#             declared_total = total
#         yield items, total
#         if on_page:
#             on_page(len(items), total)
#         skip += len(items)
#         if declared_total is not None and skip >= declared_total:
#             break
#         if skip >= SKIP_CAP:
#             break
#         time.sleep(delay)


# def fetch_stores(out_dir):
#     """All locations. One page is enough (523 < take cap), but loop anyway."""
#     stores, url, seen = [], f"{API_BASE}/stores?take=1000", set()
#     while url:
#         d = http_get(url)
#         for s in d.get("items", []):
#             sid = s.get("retailerStoreId")
#             if sid not in seen:
#                 seen.add(sid)
#                 stores.append(s)
#         nxt = (d.get("pagination") or {}).get("_links", {}).get("next", {}).get("href")
#         if nxt:
#             url = nxt if nxt.startswith("http") else ("https://www.igashop.com.au" + nxt)
#         else:
#             url = None
#     stores.sort(key=lambda s: s.get("retailerStoreId", "0"))
    
#     clean_stores = [transform_store(s) for s in stores]
#     with open(os.path.join(out_dir, "stores.json"), "w") as f:
#         json.dump({
#             "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#             "total": len(clean_stores),
#             "stores": clean_stores
#         }, f, indent=2)
#     return stores


# def scrape_store_promo(store, out_dir, delay):
#     sid = store["retailerStoreId"]
#     url = f"{API_BASE}/stores/{sid}/promotions/products"
#     items, totals = [], []
#     for batch, total in paginate(url, delay=delay):
#         items.extend(batch)
#         totals.append(total)
#     return items, (max(totals) if totals else 0)


# def store_catalog_partitions(store, delay):
#     sid = store["retailerStoreId"]
#     d = http_get(f"{API_BASE}/stores/{sid}/categoryHierarchy")
#     partitions = []

#     def probe(cid, extra=""):
#         url = f"{API_BASE}/stores/{sid}/categories/{cid}/search"
#         if extra:
#             url += f"?{extra}"
#         return http_get(url + ("&" if extra else "?") + "take=1").get("total", 0)

#     def walk(node, depth=0):
#         cid = node.get("identifier")
#         children = node.get("children") or []
#         if not cid:
#             for c in children:
#                 walk(c, depth + 1)
#             return
#         total = probe(cid)
#         if total == 0:
#             return
#         if total < SKIP_CAP:
#             partitions.append((cid, "", total))
#             return
#         if children:
#             for c in children:
#                 walk(c, depth + 1)
#             return
#         facets = probe_facets(sid, cid)
#         brands = facets.get("Brand") or []
#         log(f"   [store {sid}] leaf {cid} total={total} -> splitting by {len(brands)} brands")
#         if not brands:
#             partitions.append((cid, "", total))
#         for b in brands:
#             name = b.get("val") or b.get("value") or b.get("name")
#             if not name:
#                 continue
#             enc = urllib.parse.quote(f"Brand:{name}", safe="")
#             bt = probe(cid, f"f={enc}")
#             if bt:
#                 partitions.append((cid, f"f={enc}", bt))

#     for child in (d.get("children") or []):
#         walk(child)
#     return partitions


# def probe_facets(sid, cid):
#     url = f"{API_BASE}/stores/{sid}/categories/{cid}/search?take=1"
#     return http_get(url).get("facets") or {}


# def scrape_store_catalog(store, out_dir, delay):
#     sid = store["retailerStoreId"]
#     partitions = store_catalog_partitions(store, delay)
#     log(f"   [store {sid}] catalog: {len(partitions)} partition(s), "
#         f"expected ~{sum(p[2] for p in partitions):,} (pre-dedupe)")
#     items, seen = [], set()
#     declared = 0
#     for cid, extra, expected in partitions:
#         url = f"{API_BASE}/stores/{sid}/categories/{cid}/search"
#         if extra:
#             url += f"?{extra}"
#         for batch, total in paginate(url, declared_total=expected, delay=delay):
#             declared = max(declared, total or 0)
#             for it in batch:
#                 key = it.get("productId") or it.get("sku")
#                 if key and key in seen:
#                     continue
#                 if key:
#                     seen.add(key)
#                 items.append(it)
#     return items, declared


# def scrape_store(store, mode, out_dir, delay, refresh, stats):
#     sid = store["retailerStoreId"]
#     path = os.path.join(out_dir, f"{sid}.json")
#     if os.path.exists(path) and not refresh:
#         return ("skipped", sid, 0, 0, path)
#     t0 = time.time()
#     try:
#         if mode == "promo":
#             raw_items, declared = scrape_store_promo(store, out_dir, delay)
#         else:
#             raw_items, declared = scrape_store_catalog(store, out_dir, delay)

#         # Format items to clean structure
#         clean_items = [transform_item(it) for it in raw_items]
#         unique = len({it["productId"] for it in clean_items if it.get("productId")})

#         doc = {
#             "store": transform_store(store),
#             "mode": mode,
#             "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#             "declaredTotal": declared,
#             "itemsFetched": len(clean_items),
#             "uniqueProductIds": unique,
#             "items": clean_items,
#         }
#         tmp = path + ".tmp"
#         with open(tmp, "w") as f:
#             json.dump(doc, f, indent=2)
#         os.replace(tmp, path)
#         dt = time.time() - t0
#         log(f"   [store {sid}] {store.get('name','')[:40]:42s} "
#             f"OK {len(clean_items):,} items (declared {declared:,}, unique {unique:,}) "
#             f"in {dt:.0f}s")
#         with stats_lock:
#             stats["done"][sid] = {"name": store.get("name"), "items": len(clean_items),
#                                   "declared": declared, "seconds": round(dt, 1)}
#         return ("ok", sid, len(clean_items), declared, path)
#     except Exception as e:
#         log(f"   [store {sid}] FAILED: {e!r}")
#         with stats_lock:
#             stats["failed"][sid] = str(e)
#         return ("failed", sid, 0, 0, None)


# def write_index(out_dir, stores, stats, mode):
#     rows = []
#     for s in stores:
#         sid = s["retailerStoreId"]
#         st = stats["done"].get(sid)
#         rows.append({
#             "retailerStoreId": sid,
#             "name": s.get("name"),
#             "addressLine1": s.get("addressLine1"),
#             "city": s.get("city"),
#             "state": s.get("countyProvinceState"),
#             "postCode": s.get("postCode"),
#             "latitude": (s.get("location") or {}).get("latitude"),
#             "longitude": (s.get("location") or {}).get("longitude"),
#             "status": "done" if st else ("failed" if sid in stats["failed"] else "skipped"),
#             "itemsFetched": (st or {}).get("items"),
#             "declaredTotal": (st or {}).get("declared"),
#             "file": f"{sid}.json",
#         })
#     idx = {
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#         "mode": mode,
#         "stores": rows
#     }
#     with open(os.path.join(out_dir, "index.json"), "w") as f:
#         json.dump(idx, f, indent=2)
#     return rows


# def main():
#     ap = argparse.ArgumentParser(description="IGA Australia storefront extractor")
#     ap.add_argument("--mode", choices=["promo", "catalog"], default="promo",
#                     help="promo = specials (default) | catalog = FULL range (much bigger)")
#     ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
#                     help="output directory (default: ./output next to script)")
#     ap.add_argument("--workers", type=int, default=6)
#     ap.add_argument("--delay", type=float, default=0.15,
#                     help="seconds between page requests per store (politeness)")
#     ap.add_argument("--limit", type=int, default=0, help="only first N stores (test runs); 0 = all")
#     ap.add_argument("--offset", type=int, default=0, help="skip first N stores")
#     ap.add_argument("--stores", default="", help="comma list of retailerStoreId to run")
#     ap.add_argument("--refresh", action="store_true", help="re-fetch stores that already have an output file")
#     args = ap.parse_args()

#     os.makedirs(args.out, exist_ok=True)
#     stores = fetch_stores(args.out)
#     if args.stores:
#         wanted = {x.strip() for x in args.stores.split(",") if x.strip()}
#         stores = [s for s in stores if s["retailerStoreId"] in wanted]
#         missing = wanted - {s["retailerStoreId"] for s in stores}
#         if missing:
#             log(f"WARNING: store id(s) not in location list: {sorted(missing)}")
#     stores = stores[args.offset:]
#     if args.limit:
#         stores = stores[:args.limit]
#     log(f"mode={args.mode} | locations to process: {len(stores)} | "
#         f"workers={args.workers} delay={args.delay}s out={args.out}")

#     stats_path = os.path.join(args.out, "progress.json")
#     if args.refresh or not os.path.exists(stats_path):
#         stats = {"done": {}, "failed": {}}
#     else:
#         with open(stats_path) as f:
#             stats = json.load(f)

#     t_start = time.time()
#     with ThreadPoolExecutor(max_workers=args.workers) as ex:
#         futs = {ex.submit(scrape_store, s, args.mode, args.out, args.delay, args.refresh, stats): s for s in stores}
#         for fut in as_completed(futs):
#             try:
#                 fut.result()
#             except Exception as e:
#                 log(f"worker crash: {e!r}")
#     with open(stats_path, "w") as f:
#         json.dump(stats, f, indent=2)

#     rows = write_index(args.out, stores, stats, args.mode)
#     n_ok = sum(1 for r in rows if r["status"] == "done")
#     n_fail = sum(1 for r in rows if r["status"] == "failed")
#     n_skip = sum(1 for r in rows if r["status"] == "skipped")
#     total_items = sum((r["itemsFetched"] or 0) for r in rows)
#     log("=" * 70)
#     log(f"done: {n_ok} | failed: {n_fail} | skipped (already fetched): {n_skip} | "
#         f"total items: {total_items:,} | elapsed {(time.time()-t_start)/60:.1f} min")
#     log(f"stores.json + index.json + per-store JSON in: {args.out}")
#     if n_fail:
#         log(f"failed store ids: {sorted(stats['failed'].keys())} (re-run the same command to retry)")


# if __name__ == "__main__":
#     main()











import json
import time
import requests

# Base configuration from your request
url = "https://www.igashop.com.au/api/storefront/stores/32600/promotions/products"

headers = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-shopping-mode": "11111111-1111-1111-1111-111111111111",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Referer": "https://www.igashop.com.au/specials?utm_source=website&utm_medium=content_cards&utm_campaign=essentials",
}


def fetch_all_products():
    all_products = []
    skip = 0
    take = 20
    page = 1

    while True:
        params = {"take": take, "skip": skip}

        print(f"Fetching page {page} (items {skip} to {skip + take})...")

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=10
            )
            response.raise_for_status()

            data = response.json()

            # Handle different API response structures (list or dictionary with items)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("products") or []
            else:
                items = []

            # Break if no more products are returned
            if not items:
                print("No more items found. Finished pagination.")
                break

            all_products.extend(items)

            # Break if fewer items than requested were returned (last page)
            if len(items) < take:
                print("Reached the final page.")
                break

            skip += take
            page += 1

            # Polite delay between requests to avoid rate limits
            time.sleep(1)

        except requests.exceptions.RequestException as e:
            print(f"Error fetching page {page}: {e}")
            break

    return all_products


if __name__ == "__main__":
    products = fetch_all_products()

    # Save output to a JSON file
    output_filename = "iga_promotions.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=4, ensure_ascii=False)

    print(
        f"\nSuccessfully fetched {len(products)} total items and saved to {output_filename}"
    )