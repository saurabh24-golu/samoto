# #!/usr/bin/env python3
# """
# Campbells AU (campbells.sorted.au) scraper  --  Metcash "Sorted" B2B storefront.

# Sibling of iga_au_scraper.py. Stdlib only (py3.9+), no browser, no cookies.
# Plain HTTPS + JSON against the public SAP Commerce (OCC v2) backend.

# Public (anonymous) surface -- verified live:
#   * Branch directory .......... GET /occ/v2/{bs}/findmystore/AU?fields=CCC_FULL
#   * Category tree ............. GET /occ/v2/{bs}/category/campbells/subcategories?categoryCode={code}
#                                 recursive; root = code "1" (Open Catalogue)
#   * Product codes/paths ....... https://campbells.sorted.au/sitemap.xml -> 48 product sitemaps
#   * Product detail ............ GET /occ/v2/{bs}/users/anonymous/products/{code}?fields=CCC_PDP_FULL
#                                 gtin, tradeGtin, manufacturer, metcash item code, pack size,
#                                 unit, breadcrumbs, classifications (nutrition/allergen), image.

# NOT public: prices / offers / promos. The storefront is B2B: every price-bearing
# endpoint lives under /users/{userId}/... and returns 401 for anonymous callers
# ("Full authentication is required"). products/{code}.offers[] and prrpPriceMap[]
# come back empty anonymously and anonymous search returns totalResults=0.
# Price mode therefore needs a Campbells customer login (see --mode promo).

# Usage:
#   PYTHONPATH="" python3 campbells_au_scraper.py --mode all
#   PYTHONPATH="" python3 campbells_au_scraper.py --mode catalog --workers 8
#   PYTHONPATH="" python3 campbells_au_scraper.py --mode promo --email you@x.com --password '...'
# """

# import argparse
# import json
# import os
# import re
# import sys
# import threading
# import time
# import urllib.error
# import urllib.parse
# import urllib.request
# import xml.etree.ElementTree as ET

# BASE_SITE = "campbellsretailersspa"
# OCC_BASE = "https://api.sorted.au"
# FRONTEND = "https://campbells.sorted.au"
# OCC = "{}/occ/v2/{}".format(OCC_BASE, BASE_SITE)
# TOKEN_URL = "{}/authorizationserver/oauth/token".format(OCC_BASE)
# OAUTH_CLIENT_ID = "mobile_android_public"  # from the storefront bundle; secret not shipped publicly

# UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
#       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# CHUNK_SIZE = 500
# CATEGORY_ROOT = "1"

# _print_lock = threading.Lock()


# def log(msg):
#     with _print_lock:
#         sys.stderr.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
#         sys.stderr.flush()


# # --------------------------------------------------------------------------- io

# def ensure_dir(path):
#     if path and not os.path.isdir(path):
#         os.makedirs(path, exist_ok=True)


# def atomic_write_json(path, obj):
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as fh:
#         json.dump(obj, fh, ensure_ascii=False, indent=1)
#     os.replace(tmp, path)


# def atomic_write_text(path, text):
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as fh:
#         fh.write(text)
#     os.replace(tmp, path)


# def load_json(path, default=None):
#     if os.path.exists(path):
#         with open(path, encoding="utf-8") as fh:
#             return json.load(fh)
#     return default


# class Progress:
#     """Resumable per-task progress; atomic on every save."""

#     def __init__(self, path):
#         self.path = path
#         self.data = load_json(path, {}) or {}
#         self._lock = threading.Lock()

#     def done(self, key):
#         return key in self.data.get("completed", [])

#     def mark(self, key):
#         with self._lock:
#             self.data.setdefault("completed", []).append(key)
#             self.data["updatedAtUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
#             atomic_write_json(self.path, self.data)

#     def failed(self, key, err):
#         with self._lock:
#             self.data.setdefault("failed", {})[key] = str(err)[:300]
#             atomic_write_json(self.path, self.data)


# # ------------------------------------------------------------------------ fetch

# def http_get(url, headers=None, fields=None, timeout=90, retries=4, accept="application/json"):
#     """GET with exponential backoff. Returns raw bytes."""
#     hdrs = {"accept": accept, "user-agent": UA}
#     if headers:
#         hdrs.update(headers)
#     last = None
#     for attempt in range(retries):
#         try:
#             req = urllib.request.Request(url, headers=hdrs)
#             with urllib.request.urlopen(req, timeout=timeout) as resp:
#                 return resp.read()
#         except urllib.error.HTTPError as exc:
#             body = b""
#             try:
#                 body = exc.read()
#             except Exception:
#                 pass
#             # 4xx other than 429 are permanent -- do not burn retries
#             if exc.code in (400, 401, 403, 404, 406, 410):
#                 raise RuntimeError("HTTP {} {} :: {}".format(
#                     exc.code, url, body[:200].decode("utf-8", "replace")))
#             last = exc
#         except Exception as exc:  # noqa: BLE001 - network layer
#             last = exc
#         time.sleep(min(2 ** attempt, 8))
#     raise RuntimeError("GET failed after {} tries: {} :: {}".format(retries, url, last))


# def get_json(path_or_url, headers=None, retries=4, timeout=90):
#     url = path_or_url if path_or_url.startswith("http") else OCC + path_or_url
#     raw = http_get(url, headers=headers, retries=retries, timeout=timeout)
#     return json.loads(raw.decode("utf-8"))


# # ----------------------------------------------------------------------- stores

# def scrape_stores(out_dir, refresh=False):
#     path = os.path.join(out_dir, "stores.json")
#     if os.path.exists(path) and not refresh:
#         log("stores: cached")
#         return load_json(path)

#     log("stores: fetching branch directory")
#     grouped = get_json("/findmystore/AU?fields=CCC_FULL")
#     banners = []
#     for state, arr in grouped.items():
#         for store in arr:
#             addr = store.get("address") or {}
#             geo = store.get("geoPoint") or {}
#             region = ((addr.get("region") or {}).get("isocode")
#                       if isinstance(addr.get("region"), dict) else addr.get("region"))
#             banners.append({
#                 "retailerStoreId": store.get("name"),
#                 "displayName": store.get("displayName"),
#                 "state": state,
#                 "tradingName": store.get("tradingName"),
#                 "addressLine1": addr.get("line1"),
#                 "addressLine2": addr.get("line2"),
#                 "town": addr.get("town"),
#                 "postcode": addr.get("postalCode"),
#                 "region": region,
#                 "country": ((addr.get("country") or {}).get("isocode")
#                             if isinstance(addr.get("country"), dict) else addr.get("country")),
#                 "latitude": geo.get("latitude"),
#                 "longitude": geo.get("longitude"),
#                 "website": store.get("website"),
#                 "managerName": store.get("managerName"),
#                 "managerEmail": store.get("managerEmail"),
#                 "managerPhone": store.get("managerPhone"),
#                 "openingHours": store.get("openingHours"),
#             })
#     banners.sort(key=lambda s: (s["state"] or "", s["retailerStoreId"] or ""))
#     doc = {
#         "source": FRONTEND,
#         "baseSite": BASE_SITE,
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#         "declaredTotal": len(banners),
#         "byState": {k: len(v) for k, v in sorted(grouped.items())},
#         "stores": banners,
#     }
#     atomic_write_json(path, doc)
#     log("stores: {} branches".format(len(banners)))
#     return doc


# # ------------------------------------------------------------------- categories

# def scrape_categories(out_dir, refresh=False, workers=12, max_passes=0):
#     """
#     The subcategories endpoint returns the requested node PLUS two levels below it,
#     so a single call on the root does not reach the leaves. We therefore expand every
#     discovered node until the call stops yielding new codes, then rebuild the tree
#     from the collected parent -> children edges.

#     max_passes counts expansion passes after the root call (0 = unlimited/exhaustive).
#     Pass 1 expands every node the root call revealed (depths 2 and 3) and already
#     resolves the whole current Campbells tree (20 departments / 4,058 nodes / depth 5)
#     in ~30s. Pass 2 re-probes the deepest nodes and is only needed if the tree grows
#     deeper; exhaustive mode costs ~10 min at 6 workers.
#     """
#     tree_path = os.path.join(out_dir, "categories.json")
#     if os.path.exists(tree_path) and not refresh:
#         log("categories: cached")
#         return load_json(tree_path)

#     log("categories: walking from root '{}'".format(CATEGORY_ROOT))
#     meta = {}      # code -> {code,name,url}
#     edges = {}     # code -> [child codes]

#     def collect(node, parent=None):
#         code = str(node.get("code"))
#         if not code or code == parent:          # guard against self-referencing nodes
#             return
#         prev = meta.get(code)
#         if prev is None:
#             meta[code] = {"code": code, "name": node.get("name"), "url": node.get("url")}
#         elif not prev.get("name"):
#             prev["name"] = node.get("name")
#         edges.setdefault(code, [])
#         if parent:
#             kids = edges.setdefault(parent, [])
#             if code not in kids:
#                 kids.append(code)
#         for sub in node.get("subCategories") or []:
#             collect(sub, parent=code)

#     def expand(code):
#         try:
#             node = get_json("/category/campbells/subcategories?categoryCode="
#                             + urllib.parse.quote(str(code)))
#         except Exception as exc:  # noqa: BLE001
#             log("categories: expand {} -> {}".format(code, exc))
#             return set()
#         before = set(meta)
#         collect(node)
#         return set(meta) - before

#     collect(get_json("/category/campbells/subcategories?categoryCode=" + CATEGORY_ROOT))

#     # The root call resolves 3 levels, so seed the frontier with every node found
#     # below the root and expand them until the endpoint stops yielding new codes.
#     expanded = {CATEGORY_ROOT}
#     frontier = [c for c in meta if c != CATEGORY_ROOT]
#     guard = 0
#     while frontier and guard < 12:
#         if max_passes and guard >= max_passes:
#             log("categories: max_passes={} reached -- stopping".format(max_passes))
#             break
#         guard += 1
#         nxt = []
#         lock = threading.Lock()
#         sem = threading.Semaphore(max(1, workers))

#         def work(code):
#             new = expand(code)
#             if new:
#                 with lock:
#                     nxt.extend(sorted(new))

#         batch = frontier
#         threads = []
#         for code in batch:
#             sem.acquire()

#             def run(c=code):
#                 try:
#                     work(c)
#                 finally:
#                     sem.release()

#             th = threading.Thread(target=run)
#             th.start()
#             threads.append(th)
#         for th in threads:
#             th.join()

#         expanded.update(batch)
#         frontier = [c for c in dict.fromkeys(nxt) if c not in expanded]
#         log("categories: pass {} -> {} nodes known, {} frontier".format(
#             guard, len(meta), len(frontier)))

#     # rebuild tree from edges (defensive against cycles)
#     visiting = set()

#     def build(code, depth=1, parent_path=""):
#         m = meta.get(code) or {"code": code, "name": code, "url": None}
#         name = m.get("name") or ""
#         path = (parent_path + "/" + name) if parent_path else name
#         entry = {
#             "code": code,
#             "name": name,
#             "path": path,
#             "depth": depth,
#             "url": m.get("url") or "/Open-Catalogue/c/" + str(code),
#             "productCount": None,
#         }
#         if code in visiting:
#             return entry
#         visiting.add(code)
#         kids = [k for k in edges.get(code, []) if k in meta]
#         if kids:
#             entry["subCategories"] = [build(k, depth + 1, path) for k in kids]
#         visiting.discard(code)
#         return entry

#     tree = build(CATEGORY_ROOT)

#     flat = []

#     def flatten(node, ancestors=()):
#         chain = ancestors + ((node["code"], node["name"]),)
#         flat.append({
#             "code": node["code"],
#             "name": node["name"],
#             "path": node["path"],
#             "breadcrumb": [{"code": c, "name": n} for c, n in chain],
#             "depth": node["depth"],
#             "url": node["url"],
#             "isLeaf": "subCategories" not in node,
#         })
#         for k in node.get("subCategories", []):
#             flatten(k, chain)

#     flatten(tree)
#     doc = {
#         "source": FRONTEND,
#         "baseSite": BASE_SITE,
#         "rootCode": CATEGORY_ROOT,
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#         "nodeCount": len(flat),
#         "leafCount": sum(1 for f in flat if f["isLeaf"]),
#         "maxDepth": max((f["depth"] for f in flat), default=0),
#         "tree": tree,
#     }
#     atomic_write_json(tree_path, doc)
#     atomic_write_json(os.path.join(out_dir, "categories_flat.json"), flat)
#     log("categories: {} nodes ({} leaves, depth {})".format(
#         len(flat), doc["leafCount"], doc["maxDepth"]))
#     return doc


# # ---------------------------------------------------------------------- catalog

# def harvest_sitemap(out_dir, refresh=False):
#     idx_path = os.path.join(out_dir, "catalog", "sitemap_products.json")
#     if os.path.exists(idx_path) and not refresh:
#         log("catalog: sitemap cached")
#         return load_json(idx_path)

#     ensure_dir(os.path.join(out_dir, "catalog"))
#     log("catalog: reading sitemap index")
#     raw = http_get(FRONTEND + "/sitemap.xml", accept="application/xml")
#     root = ET.fromstring(raw)
#     ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
#     maps = [loc.text for loc in root.findall(".//sm:loc", ns)]
#     log("catalog: {} product sitemaps".format(len(maps)))

#     rx = re.compile(r"/p/([A-Za-z0-9_\-]+)$")
#     rows = {}
#     for i, url in enumerate(maps, 1):
#         body = http_get(url, accept="application/xml")
#         doc = ET.fromstring(body)
#         for loc in doc.findall(".//sm:loc", ns):
#             u = (loc.text or "").strip()
#             if "/p/" not in u:
#                 continue
#             code = rx.search(u)
#             if not code:
#                 continue
#             tail = u.split(FRONTEND + "/", 1)[-1]
#             path = tail.rsplit("/p/", 1)[0]
#             rows[code.group(1)] = {
#                 "code": code.group(1),
#                 "url": u,
#                 "urlPath": path,
#                 "sitemapIndex": i,
#             }
#         if i % 12 == 0:
#             log("catalog: sitemaps {}/{} ({} products)".format(i, len(maps), len(rows)))

#     doc = {
#         "source": FRONTEND + "/sitemap.xml",
#         "sitemapCount": len(maps),
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#         "productCount": len(rows),
#         "products": sorted(rows.values(), key=lambda r: r["code"]),
#     }
#     atomic_write_json(idx_path, doc)
#     log("catalog: {} product codes harvested".format(len(rows)))
#     return doc


# NUTRITION_KEYS = {
#     "nutrition.energykjquantityperserve": "energyKjPerServe",
#     "nutrition.energykjquantityper100g": "energyKjPer100g",
#     "nutrition.proteinquantityper100g": "proteinPer100g",
#     "nutrition.fattotalquantityper100g": "fatTotalPer100g",
#     "nutrition.fatsaturatedquantityper100g": "fatSaturatedPer100g",
#     "nutrition.carbohydratestotalquantityper100g": "carbohydratePer100g",
#     "nutrition.sugarsquantityper100g": "sugarsPer100g",
#     "nutrition.sodiumquantityper100mg": "sodiumPer100mg",
#     "nutrition.allergencontains": "allergenContains",
# }
# CORE_KEYS = {
#     "core.countryoforiginstatement": "countryOfOrigin",
#     "core.netcontentuom": "netContentUom",
#     "core.nameofbrandowner": "brandOwner",
#     "core.subrange": "subrange",
# }


# def flatten_product(pdp_product, source_url, category_path=None):
#     """Normalise one CCC_PDP_FULL product payload."""
#     p = pdp_product or {}
#     features = {}
#     for group in p.get("classifications") or []:
#         for feat in group.get("features") or []:
#             fcode = (feat.get("code") or "").split("MetcashClassification/1.0/")[-1]
#             vals = [fv.get("value") for fv in (feat.get("featureValues") or [])
#                     if fv.get("value") not in (None, "")]
#             features[fcode] = vals[0] if len(vals) == 1 else (vals or None)

#     nutr = {dst: features.get(src) for src, dst in NUTRITION_KEYS.items()}
#     core = {dst: features.get(src) for src, dst in CORE_KEYS.items()}
#     extra = {k: v for k, v in features.items()
#              if k not in NUTRITION_KEYS and k not in CORE_KEYS and v not in (None, [], "")}

#     units = []
#     for cu in p.get("consumerUnits") or []:
#         units.append({
#             "gtin": cu.get("gtin"),
#             "metcashItemCode": cu.get("metcashItemCode"),
#             "qty": cu.get("qty"),
#             "cartonWeight": cu.get("cartonWeight"),
#             "shelfTicketName": cu.get("shelfTicketName") or p.get("shelfticketname"),
#         })

#     offers = p.get("offers") or []
#     prices = []
#     for off in offers:
#         pr = (off.get("price") or {})
#         prices.append({
#             "offerId": off.get("id"),
#             "priceValue": pr.get("value"),
#             "formattedValue": pr.get("formattedValue"),
#             "currencyIso": pr.get("currencyIso"),
#             "priceType": (off.get("priceData") or {}).get("priceType")
#                          if isinstance(off.get("priceData"), dict) else None,
#             "unitPrice": (off.get("priceData") or {}).get("unitPrice") if isinstance(off.get("priceData"), dict) else None,
#             "startDate": off.get("startDate"),
#             "endDate": off.get("endDate"),
#         })

#     return {
#         "code": p.get("code"),
#         "name": p.get("name"),
#         "shelfTicketName": p.get("shelfticketname"),
#         "manufacturer": p.get("manufacturer"),
#         "gtin": p.get("gtin"),
#         "tradeGtin": p.get("tradeGtin"),
#         "metcashItemCode": p.get("metcashItemCode"),
#         "primarySupplier": p.get("primarySupplier"),
#         "primarySupplierCode": p.get("primarySupplierCode"),
#         "commodityType": (p.get("commodityType") or {}).get("code") if isinstance(p.get("commodityType"), dict) else None,
#         "subCommodity": p.get("subCommodity"),
#         "department": p.get("department"),
#         "packSize": p.get("packSize"),
#         "packageQuantity": p.get("packageQuantity"),
#         "taxName": p.get("taxName"),
#         "availableDate": p.get("availableDate"),
#         "unit": p.get("unit"),
#         "unitPriceDisplay": p.get("unitPriceDisplay"),
#         "consumerUnits": units,
#         "breadcrumbs": [{"code": b.get("categoryCode"), "name": b.get("name"), "url": b.get("url")}
#                         for b in (p.get("breadcrumbs") or [])],
#         "categoryPath": category_path,
#         "imageUrl": p.get("cloudinaryImgUrl"),
#         "url": p.get("url"),
#         "sourceUrl": source_url,
#         "nutrition": nutr,
#         "core": core,
#         "attributes": extra,
#         "offers": prices,
#         "priceAvailable": bool(prices),
#     }


# def scrape_catalog(out_dir, workers=6, delay=0.0, limit=None, offset=0, refresh=False,
#                    details=True, chunk_size=CHUNK_SIZE):
#     """
#     Harvest codes from the sitemap, then pull PDP detail for each.

#     Chunk boundaries are deterministic (sorted by code, fixed chunk_size), so a
#     partial run is resumable: chunks already present in index.json are skipped and
#     every product is assigned to exactly one chunk. A bounded worker pool of
#     `workers` threads does the fetching -- `workers` == concurrent HTTP requests.
#     """
#     import collections
#     import queue as queue_mod

#     cat_dir = os.path.join(out_dir, "catalog")
#     ensure_dir(cat_dir)
#     sm = harvest_sitemap(out_dir, refresh=refresh)
#     products = sm["products"]
#     if offset:
#         products = products[offset:]
#     if limit:
#         products = products[:limit]

#     if not details:
#         atomic_write_json(os.path.join(cat_dir, "index.json"), {
#             "mode": "codes-only",
#             "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#             "productCount": len(products),
#             "chunks": [],
#         })
#         atomic_write_json(os.path.join(cat_dir, "products_codes.json"), products)
#         return sm

#     # deterministic chunk assignment
#     chunks = [products[i:i + chunk_size] for i in range(0, len(products), chunk_size)]
#     chunk_of = {}
#     need_per_chunk = {}
#     for ci, chunk in enumerate(chunks):
#         codes = [r["code"] for r in chunk]
#         need_per_chunk[ci] = set(codes)
#         for c in codes:
#             chunk_of[c] = ci

#     index_path = os.path.join(cat_dir, "index.json")
#     index = load_json(index_path, {}) or {"chunks": []}
#     if refresh:
#         index = {"chunks": []}
#     done_chunks = {c["chunkIndex"] for c in index.get("chunks", [])}
#     pending = [r for r in products if chunk_of[r["code"]] not in done_chunks]

#     log("catalog: {} products | {} chunks | {} pending ({} chunks already done)".format(
#         len(products), len(chunks), len(pending), len(done_chunks)))
#     if not pending:
#         log("catalog: nothing to do")
#         return sm

#     work = queue_mod.Queue()
#     for row in pending:
#         work.put(row)

#     buffers = collections.defaultdict(dict)
#     write_lock = threading.Lock()
#     counters = {"n": 0, "err": 0}
#     t0 = time.time()

#     def write_chunk(ci):
#         buf = buffers.get(ci, {})
#         chunk_codes = [r["code"] for r in chunks[ci]]
#         items = [buf[c] for c in chunk_codes if c in buf]
#         fname = "chunk-{:04d}.json".format(ci)
#         payload = {
#             "chunkIndex": ci,
#             "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#             "itemCount": len(items),
#             "errorCount": sum(1 for i in items if "error" in i),
#             "items": items,
#         }
#         atomic_write_json(os.path.join(cat_dir, fname), payload)
#         index.setdefault("chunks", []).append({
#             "file": fname, "chunkIndex": ci, "itemCount": len(items),
#             "errorCount": payload["errorCount"],
#             "firstCode": chunk_codes[0], "lastCode": chunk_codes[-1],
#         })
#         index["chunks"].sort(key=lambda c: c["chunkIndex"])
#         index["mode"] = "pdp-full"
#         index["baseSite"] = BASE_SITE
#         index["chunkSize"] = chunk_size
#         index["productCount"] = len(products)
#         index["fetchedAtUtc"] = payload["fetchedAtUtc"]
#         atomic_write_json(index_path, index)
#         buffers.pop(ci, None)

#     def worker():
#         while True:
#             try:
#                 row = work.get_nowait()
#             except queue_mod.Empty:
#                 return
#             code = row["code"]
#             url = (OCC + "/users/anonymous/products/" + urllib.parse.quote(code)
#                    + "?fields=CCC_PDP_FULL")
#             try:
#                 doc = json.loads(http_get(url, accept="application/json").decode("utf-8"))
#                 item = flatten_product(doc.get("product"), url, row.get("urlPath"))
#             except Exception as exc:  # noqa: BLE001
#                 item = {"code": code, "urlPath": row.get("urlPath"),
#                         "sourceUrl": url, "error": str(exc)[:200]}
#             if delay:
#                 time.sleep(delay)
#             ci = chunk_of[code]
#             with write_lock:
#                 buffers[ci][code] = item
#                 counters["n"] += 1
#                 if "error" in item:
#                     counters["err"] += 1
#                 complete = len(buffers[ci]) >= len(need_per_chunk[ci])
#                 if complete:
#                     write_chunk(ci)
#                     el = time.time() - t0
#                     rate = counters["n"] / el if el else 0
#                     eta = (len(pending) - counters["n"]) / rate if rate else 0
#                     log("catalog: {}/{} items | {} chunks | {:.1f}/s | eta {:.0f} min | err {}".format(
#                         counters["n"], len(pending), len(index.get("chunks", [])),
#                         rate, eta / 60.0, counters["err"]))

#     threads = [threading.Thread(target=worker) for _ in range(max(1, workers))]
#     for th in threads:
#         th.start()
#     for th in threads:
#         th.join()

#     # flush any partial buffers (shouldn't happen, but be safe)
#     with write_lock:
#         for ci in list(buffers.keys()):
#             write_chunk(ci)

#     log("catalog: finished {} items, {} errors, {} chunks".format(
#         counters["n"], counters["err"], len(index.get("chunks", []))))
#     return sm


# # --------------------------------------------------------------- merge/complete

# def merge_categories(out_dir):
#     """
#     The category-tree endpoint is NOT a superset of reality: node 330 (SEASONAL)
#     omits child 33001 (CHRISTMAS - GENERAL, 312 live products), 163 (DESSERTS) omits
#     16301 (CHILLED DESSERTS), and 86 codes in total are referenced by live products
#     but never returned by /category/campbells/subcategories.

#     So build the complete taxonomy as the UNION of the API tree and every product's
#     own breadcrumbs (the latter is authoritative for where a product actually sits),
#     with product counts. Writes categories_complete.json + categories_complete_flat.json.
#     """
#     import collections
#     import glob

#     tree_path = os.path.join(out_dir, "categories.json")
#     cat_dir = os.path.join(out_dir, "catalog")
#     if not os.path.exists(tree_path):
#         raise SystemExit("run --mode categories first")
#     tree_doc = load_json(tree_path)

#     nodes = {}          # code -> {code, name, parent, inApiTree, children[]}
#     def add(code, name, parent=None, in_api=False):
#         n = nodes.get(code)
#         if n is None:
#             n = nodes[code] = {"code": code, "name": name, "parent": parent,
#                                "inApiTree": in_api, "children": []}
#         else:
#             if name and not n.get("name"):
#                 n["name"] = name
#             if parent and not n.get("parent"):
#                 n["parent"] = parent
#             if in_api:
#                 n["inApiTree"] = True
#         return n

#     # 1) API tree
#     def walk(node, parent=None):
#         add(node["code"], node["name"], parent, in_api=True)
#         for k in node.get("subCategories", []):
#             walk(k, node["code"])
#     walk(tree_doc["tree"])
#     for code, n in nodes.items():
#         p = n.get("parent")
#         if p and code not in nodes[p]["children"]:
#             nodes[p]["children"].append(code)

#     # 2) product breadcrumbs
#     direct = collections.Counter()
#     total = collections.Counter()
#     chunks = sorted(glob.glob(os.path.join(cat_dir, "chunk-*.json")))
#     if not chunks:
#         raise SystemExit("no catalog chunks found in {}".format(cat_dir))
#     products = 0
#     for fn in chunks:
#         for item in json.load(open(fn)).get("items", []):
#             bc = [b for b in (item.get("breadcrumbs") or []) if b.get("code")]
#             if not bc:
#                 continue
#             products += 1
#             for i, b in enumerate(bc):
#                 parent = bc[i - 1]["code"] if i else None
#                 add(b["code"], b.get("name"), parent)
#                 if parent:
#                     p = nodes[parent]
#                     if b["code"] not in p["children"]:
#                         p["children"].append(b["code"])
#                 total[b["code"]] += 1                       # every ancestor aggregates
#                 if i == len(bc) - 1:
#                     direct[b["code"]] += 1                  # only the deepest is direct

#     # 3) rebuild
#     def build(code, depth=1, path=""):
#         n = nodes[code]
#         name = n.get("name") or code
#         p = (path + "/" + name) if path else name
#         entry = {
#             "code": code,
#             "name": name,
#             "path": p,
#             "depth": depth,
#             "inApiTree": n["inApiTree"],
#             "directProductCount": direct.get(code, 0),
#             "productCount": total.get(code, direct.get(code, 0)),
#             "url": "/Open-Catalogue/c/" + str(code),
#             "subCategories": [],
#         }
#         seen = set()
#         for c in n["children"]:
#             if c in seen or c == code:
#                 continue
#             seen.add(c)
#             entry["subCategories"].append(build(c, depth + 1, p))
#         if not entry["subCategories"]:
#             del entry["subCategories"]
#         return entry

#     roots = [c for c in nodes if not nodes[c].get("parent")]
#     root_code = "1" if "1" in nodes else (sorted(roots)[0] if roots else None)
#     complete = build(root_code)

#     flat = []
#     def flatten(node, chain=()):
#         c = chain + ((node["code"], node["name"]),)
#         flat.append({k: node[k] for k in
#                      ("code", "name", "path", "depth", "inApiTree",
#                       "directProductCount", "productCount", "url")} | {
#             "breadcrumb": [{"code": a, "name": b} for a, b in c],
#             "isLeaf": "subCategories" not in node,
#         })
#         for k in node.get("subCategories", []):
#             flatten(k, c)
#     flatten(complete)

#     api_codes = {f["code"] for f in (load_json(os.path.join(out_dir, "categories_flat.json")) or [])}
#     doc = {
#         "source": "union of /category/campbells/subcategories and product breadcrumbs",
#         "baseSite": BASE_SITE,
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#         "productsSeen": products,
#         "nodeCount": len(flat),
#         "leafCount": sum(1 for f in flat if f["isLeaf"]),
#         "maxDepth": max((f["depth"] for f in flat), default=0),
#         "inApiTree": sum(1 for f in flat if f["inApiTree"]),
#         "onlyInProducts": sum(1 for f in flat if not f["inApiTree"]),
#         "gapCodes": sorted(c for c in nodes if c not in api_codes),
#         "gapNames": sorted({nodes[c]["name"] for c in nodes if c not in api_codes}),
#         "tree": complete,
#     }
#     atomic_write_json(os.path.join(out_dir, "categories_complete.json"), doc)
#     atomic_write_json(os.path.join(out_dir, "categories_complete_flat.json"), flat)

#     log("merge: {} nodes ({} in API tree, {} recovered from product breadcrumbs)".format(
#         len(flat), doc["inApiTree"], doc["onlyInProducts"]))
#     log("merge: max depth {}, products covered {}, {} gap categories".format(
#         doc["maxDepth"], products, len(doc["gapCodes"])))
#     return doc


# # ------------------------------------------------------------------ promo/price

# def oauth_token(email, password, retries=3):
#     """B2B password grant. client_id comes from the storefront; the secret is not public."""
#     body = urllib.parse.urlencode({
#         "grant_type": "password",
#         "client_id": OAUTH_CLIENT_ID,
#         "username": email,
#         "password": password,
#     }).encode()
#     hdrs = {"content-type": "application/x-www-form-urlencoded",
#             "accept": "application/json", "user-agent": UA}
#     last = None
#     for attempt in range(retries):
#         try:
#             req = urllib.request.Request(TOKEN_URL, data=body, headers=hdrs, method="POST")
#             with urllib.request.urlopen(req, timeout=60) as resp:
#                 return json.loads(resp.read().decode("utf-8"))
#         except urllib.error.HTTPError as exc:
#             detail = exc.read()[:300].decode("utf-8", "replace")
#             raise RuntimeError("OAuth HTTP {} :: {}".format(exc.code, detail))
#         except Exception as exc:  # noqa: BLE001
#             last = exc
#             time.sleep(2 ** attempt)
#     raise RuntimeError("OAuth failed: {}".format(last))


# def scrape_promo(out_dir, email, password, workers=4, branch=None, refresh=False):
#     """
#     Price / promo mode. REQUIRES a Campbells customer login -- the OCC price-bearing
#     endpoints (/users/{userId}/products/search, /users/{userId}/category/{code}/export-products,
#     offers[], prrpPriceMap[]) all return 401 for anonymous callers.
#     """
#     if not email or not password:
#         raise SystemExit(
#             "promo mode needs credentials: --email/--password "
#             "(or env CAMPBELLS_EMAIL / CAMPBELLS_PASSWORD).\n"
#             "Anonymous access cannot read prices on this site -- every price endpoint is\n"
#             "under /users/{userId}/ and returns 401 'Full authentication is required'."
#         )

#     out_cat = os.path.join(out_dir, "promo")
#     ensure_dir(out_cat)
#     log("promo: authenticating as {}".format(email))
#     tok = oauth_token(email, password)
#     access = tok["access_token"]
#     hdrs = {"authorization": "Bearer " + access}
#     if branch:
#         hdrs["x-store-id"] = str(branch)

#     # who am I -> resolve the OCC userId used in /users/{userId}/ paths
#     uid = None
#     for candidate in ("/users/current?fields=FULL", "/users/current"):
#         try:
#             me = get_json(candidate, headers=hdrs)
#             uid = me.get("uid") or me.get("customerId") or me.get("userId")
#             if uid:
#                 break
#         except Exception:  # noqa: BLE001
#             continue
#     uid = uid or email
#     log("promo: userId={}".format(uid))

#     cats = load_json(os.path.join(out_dir, "categories_flat.json")) or []
#     leaves = [c for c in cats if c.get("isLeaf")]
#     if not leaves:
#         log("promo: run --mode categories first for the leaf list")

#     prog = Progress(os.path.join(out_cat, "progress.json"))
#     results = []
#     for leaf in leaves:
#         code = leaf["code"]
#         fname = "cat-{}.json".format(code)
#         fpath = os.path.join(out_cat, fname)
#         if os.path.exists(fpath) and not refresh:
#             results.append(load_json(fpath))
#             continue
#         url = "{}/users/{}/category/{}/export-products?query={}".format(
#             OCC, urllib.parse.quote(str(uid)), urllib.parse.quote(str(code)),
#             urllib.parse.quote("fields=FULL"))
#         try:
#             raw = http_get(url, headers=hdrs, accept="application/json, text/csv, */*")
#             items = []
#             try:
#                 doc = json.loads(raw.decode("utf-8"))
#                 items = doc.get("products") or doc.get("items") or doc
#             except Exception:  # noqa: BLE001 - may be CSV
#                 items = {"_rawCsv": raw.decode("utf-8", "replace")[:500000]}
#             payload = {"categoryCode": code, "categoryPath": leaf.get("path"),
#                        "url": url, "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#                        "items": items}
#             atomic_write_json(fpath, payload)
#             prog.mark(fname)
#             results.append(payload)
#             log("promo: {} -> {} items".format(code, len(items) if isinstance(items, list) else "csv"))
#         except Exception as exc:  # noqa: BLE001
#             prog.failed(fname, exc)
#             log("promo: {} FAILED {}".format(code, exc))

#     atomic_write_json(os.path.join(out_cat, "index.json"), {
#         "mode": "promo-authenticated",
#         "userId": uid,
#         "branch": branch,
#         "categories": len(leaves),
#         "fetched": len(results),
#         "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
#     })
#     return results


# # ----------------------------------------------------------------------- driver

# def main():
#     ap = argparse.ArgumentParser(description="Campbells AU (sorted.au / Metcash) scraper")
#     ap.add_argument("--mode", default="all",
#                     choices=["stores", "categories", "catalog", "merge", "promo", "all"])
#     ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
#     ap.add_argument("--workers", type=int, default=12,
#                     help="concurrent HTTP requests (12 verified safe; 20 also fine)")
#     ap.add_argument("--delay", type=float, default=0.0)
#     ap.add_argument("--limit", type=int, default=None, help="max products (catalog mode)")
#     ap.add_argument("--offset", type=int, default=0)
#     ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
#     ap.add_argument("--cat-passes", type=int, default=0,
#                     help="category expansion passes after the root call; 0 = exhaustive, "
#                          "1 resolves the full current tree in ~30s")
#     ap.add_argument("--codes-only", action="store_true", help="catalog: sitemap only, skip PDP calls")
#     ap.add_argument("--branch", default=None, help="branch/store code for promo mode")
#     ap.add_argument("--email", default=os.environ.get("CAMPBELLS_EMAIL"))
#     ap.add_argument("--password", default=os.environ.get("CAMPBELLS_PASSWORD"))
#     ap.add_argument("--refresh", action="store_true")
#     args = ap.parse_args()

#     ensure_dir(args.out)
#     started = time.time()
#     log("campbells AU scraper | mode={} out={}".format(args.mode, args.out))

#     if args.mode in ("stores", "all"):
#         scrape_stores(args.out, refresh=args.refresh)
#     if args.mode in ("categories", "all"):
#         scrape_categories(args.out, refresh=args.refresh, workers=args.workers,
#                           max_passes=args.cat_passes)
#     if args.mode in ("catalog", "all"):
#         scrape_catalog(args.out, workers=args.workers, delay=args.delay, limit=args.limit,
#                        offset=args.offset, refresh=args.refresh,
#                        details=not args.codes_only, chunk_size=args.chunk_size)
#     if args.mode in ("merge", "all"):
#         merge_categories(args.out)
#     if args.mode == "promo":
#         scrape_promo(args.out, args.email, args.password, workers=args.workers,
#                      branch=args.branch, refresh=args.refresh)

#     log("done in {:.1f}s".format(time.time() - started))


# if __name__ == "__main__":
#     main()










#!/usr/bin/env python3
"""
Campbells AU (campbells.sorted.au) scraper  --  Metcash "Sorted" B2B storefront.

Sibling of iga_au_scraper.py. Stdlib only (py3.9+), no browser, no cookies.
Plain HTTPS + JSON against the public SAP Commerce (OCC v2) backend.

Public (anonymous) surface -- verified live:
  * Branch directory .......... GET /occ/v2/{bs}/findmystore/AU?fields=CCC_FULL
  * Category tree ............. GET /occ/v2/{bs}/category/campbells/subcategories?categoryCode={code}
                                recursive; root = code "1" (Open Catalogue)
  * Product codes/paths ....... https://campbells.sorted.au/sitemap.xml -> 48 product sitemaps
  * Product detail ............ GET /occ/v2/{bs}/users/anonymous/products/{code}?fields=CCC_PDP_FULL
                                gtin, tradeGtin, manufacturer, metcash item code, pack size,
                                unit, breadcrumbs, classifications (nutrition/allergen), image.

NOT public: prices / offers / promos. The storefront is B2B: every price-bearing
endpoint lives under /users/{userId}/... and returns 401 for anonymous callers
("Full authentication is required"). products/{code}.offers[] and prrpPriceMap[]
come back empty anonymously and anonymous search returns totalResults=0.
Price mode therefore needs a Campbells customer login (see --mode promo).

Usage:
  PYTHONPATH="" python3 campbells_au_scraper.py --mode all
  PYTHONPATH="" python3 campbells_au_scraper.py --mode catalog --workers 8
  PYTHONPATH="" python3 campbells_au_scraper.py --mode promo --email you@x.com --password '...'
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE_SITE = "campbellsretailersspa"
OCC_BASE = "https://api.sorted.au"
FRONTEND = "https://campbells.sorted.au"
OCC = "{}/occ/v2/{}".format(OCC_BASE, BASE_SITE)
TOKEN_URL = "{}/authorizationserver/oauth/token".format(OCC_BASE)
OAUTH_CLIENT_ID = "mobile_android_public"  # from the storefront bundle; secret not shipped publicly

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CHUNK_SIZE = 500
CATEGORY_ROOT = "1"

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        sys.stderr.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()


# --------------------------------------------------------------------------- io

def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


class Progress:
    """Resumable per-task progress; atomic on every save."""

    def __init__(self, path):
        self.path = path
        self.data = load_json(path, {}) or {}
        self._lock = threading.Lock()

    def done(self, key):
        return key in self.data.get("completed", [])

    def mark(self, key):
        with self._lock:
            self.data.setdefault("completed", []).append(key)
            self.data["updatedAtUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            atomic_write_json(self.path, self.data)

    def failed(self, key, err):
        with self._lock:
            self.data.setdefault("failed", {})[key] = str(err)[:300]
            atomic_write_json(self.path, self.data)


# ------------------------------------------------------------------------ fetch

def http_get(url, headers=None, fields=None, timeout=90, retries=4, accept="application/json"):
    """GET with exponential backoff. Returns raw bytes."""
    hdrs = {"accept": accept, "user-agent": UA}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            # 4xx other than 429 are permanent -- do not burn retries
            if exc.code in (400, 401, 403, 404, 406, 410):
                raise RuntimeError("HTTP {} {} :: {}".format(
                    exc.code, url, body[:200].decode("utf-8", "replace")))
            last = exc
        except Exception as exc:  # noqa: BLE001 - network layer
            last = exc
        time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("GET failed after {} tries: {} :: {}".format(retries, url, last))


def get_json(path_or_url, headers=None, retries=4, timeout=90):
    url = path_or_url if path_or_url.startswith("http") else OCC + path_or_url
    raw = http_get(url, headers=headers, retries=retries, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


# ----------------------------------------------------------------------- stores

def scrape_stores(out_dir, refresh=False):
    path = os.path.join(out_dir, "stores.json")
    if os.path.exists(path) and not refresh:
        log("stores: cached")
        return load_json(path)

    log("stores: fetching branch directory")
    grouped = get_json("/findmystore/AU?fields=CCC_FULL")
    banners = []
    for state, arr in grouped.items():
        for store in arr:
            addr = store.get("address") or {}
            geo = store.get("geoPoint") or {}
            region = ((addr.get("region") or {}).get("isocode")
                      if isinstance(addr.get("region"), dict) else addr.get("region"))
            banners.append({
                "retailerStoreId": store.get("name"),
                "displayName": store.get("displayName"),
                "state": state,
                "tradingName": store.get("tradingName"),
                "addressLine1": addr.get("line1"),
                "addressLine2": addr.get("line2"),
                "town": addr.get("town"),
                "postcode": addr.get("postalCode"),
                "region": region,
                "country": ((addr.get("country") or {}).get("isocode")
                            if isinstance(addr.get("country"), dict) else addr.get("country")),
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "website": store.get("website"),
                "managerName": store.get("managerName"),
                "managerEmail": store.get("managerEmail"),
                "managerPhone": store.get("managerPhone"),
                "openingHours": store.get("openingHours"),
            })
    banners.sort(key=lambda s: (s["state"] or "", s["retailerStoreId"] or ""))
    doc = {
        "source": FRONTEND,
        "baseSite": BASE_SITE,
        "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "declaredTotal": len(banners),
        "byState": {k: len(v) for k, v in sorted(grouped.items())},
        "stores": banners,
    }
    atomic_write_json(path, doc)
    log("stores: {} branches".format(len(banners)))
    return doc


# ------------------------------------------------------------------- categories

def scrape_categories(out_dir, refresh=False, workers=12, max_passes=0):
    tree_path = os.path.join(out_dir, "categories.json")
    if os.path.exists(tree_path) and not refresh:
        log("categories: cached")
        return load_json(tree_path)

    log("categories: walking from root '{}'".format(CATEGORY_ROOT))
    meta = {}      # code -> {code,name,url}
    edges = {}     # code -> [child codes]

    def collect(node, parent=None):
        code = str(node.get("code"))
        if not code or code == parent:
            return
        prev = meta.get(code)
        if prev is None:
            meta[code] = {"code": code, "name": node.get("name"), "url": node.get("url")}
        elif not prev.get("name"):
            prev["name"] = node.get("name")
        edges.setdefault(code, [])
        if parent:
            kids = edges.setdefault(parent, [])
            if code not in kids:
                kids.append(code)
        for sub in node.get("subCategories") or []:
            collect(sub, parent=code)

    def expand(code):
        try:
            node = get_json("/category/campbells/subcategories?categoryCode="
                            + urllib.parse.quote(str(code)))
        except Exception as exc:  # noqa: BLE001
            log("categories: expand {} -> {}".format(code, exc))
            return set()
        before = set(meta)
        collect(node)
        return set(meta) - before

    collect(get_json("/category/campbells/subcategories?categoryCode=" + CATEGORY_ROOT))

    expanded = {CATEGORY_ROOT}
    frontier = [c for c in meta if c != CATEGORY_ROOT]
    guard = 0
    while frontier and guard < 12:
        if max_passes and guard >= max_passes:
            log("categories: max_passes={} reached -- stopping".format(max_passes))
            break
        guard += 1
        nxt = []
        lock = threading.Lock()
        sem = threading.Semaphore(max(1, workers))

        def work(code):
            new = expand(code)
            if new:
                with lock:
                    nxt.extend(sorted(new))

        batch = frontier
        threads = []
        for code in batch:
            sem.acquire()

            def run(c=code):
                try:
                    work(c)
                finally:
                    sem.release()

            th = threading.Thread(target=run)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()

        expanded.update(batch)
        frontier = [c for c in dict.fromkeys(nxt) if c not in expanded]
        log("categories: pass {} -> {} nodes known, {} frontier".format(
            guard, len(meta), len(frontier)))

    visiting = set()

    def build(code, depth=1, parent_path=""):
        m = meta.get(code) or {"code": code, "name": code, "url": None}
        name = m.get("name") or ""
        path = (parent_path + "/" + name) if parent_path else name
        entry = {
            "code": code,
            "name": name,
            "path": path,
            "depth": depth,
            "url": m.get("url") or "/Open-Catalogue/c/" + str(code),
            "productCount": None,
        }
        if code in visiting:
            return entry
        visiting.add(code)
        kids = [k for k in edges.get(code, []) if k in meta]
        if kids:
            entry["subCategories"] = [build(k, depth + 1, path) for k in kids]
        visiting.discard(code)
        return entry

    tree = build(CATEGORY_ROOT)

    flat = []

    def flatten(node, ancestors=()):
        chain = ancestors + ((node["code"], node["name"]),)
        flat.append({
            "code": node["code"],
            "name": node["name"],
            "path": node["path"],
            "breadcrumb": [{"code": c, "name": n} for c, n in chain],
            "depth": node["depth"],
            "url": node["url"],
            "isLeaf": "subCategories" not in node,
        })
        for k in node.get("subCategories", []):
            flatten(k, chain)

    flatten(tree)
    doc = {
        "source": FRONTEND,
        "baseSite": BASE_SITE,
        "rootCode": CATEGORY_ROOT,
        "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodeCount": len(flat),
        "leafCount": sum(1 for f in flat if f["isLeaf"]),
        "maxDepth": max((f["depth"] for f in flat), default=0),
        "tree": tree,
    }
    atomic_write_json(tree_path, doc)
    atomic_write_json(os.path.join(out_dir, "categories_flat.json"), flat)
    log("categories: {} nodes ({} leaves, depth {})".format(
        len(flat), doc["leafCount"], doc["maxDepth"]))
    return doc


# ---------------------------------------------------------------------- catalog

def harvest_sitemap(out_dir, refresh=False):
    idx_path = os.path.join(out_dir, "catalog", "sitemap_products.json")
    if os.path.exists(idx_path) and not refresh:
        log("catalog: sitemap cached")
        return load_json(idx_path)

    ensure_dir(os.path.join(out_dir, "catalog"))
    log("catalog: reading sitemap index")
    raw = http_get(FRONTEND + "/sitemap.xml", accept="application/xml")
    root = ET.fromstring(raw)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    maps = [loc.text for loc in root.findall(".//sm:loc", ns)]
    log("catalog: {} product sitemaps".format(len(maps)))

    rx = re.compile(r"/p/([A-Za-z0-9_\-]+)$")
    rows = {}
    for i, url in enumerate(maps, 1):
        body = http_get(url, accept="application/xml")
        doc = ET.fromstring(body)
        for loc in doc.findall(".//sm:loc", ns):
            u = (loc.text or "").strip()
            if "/p/" not in u:
                continue
            code = rx.search(u)
            if not code:
                continue
            tail = u.split(FRONTEND + "/", 1)[-1]
            path = tail.rsplit("/p/", 1)[0]
            rows[code.group(1)] = {
                "code": code.group(1),
                "url": u,
                "urlPath": path,
                "sitemapIndex": i,
            }
        if i % 12 == 0:
            log("catalog: sitemaps {}/{} ({} products)".format(i, len(maps), len(rows)))

    doc = {
        "source": FRONTEND + "/sitemap.xml",
        "sitemapCount": len(maps),
        "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "productCount": len(rows),
        "products": sorted(rows.values(), key=lambda r: r["code"]),
    }
    atomic_write_json(idx_path, doc)
    log("catalog: {} product codes harvested".format(len(rows)))
    return doc


NUTRITION_KEYS = {
    "nutrition.energykjquantityperserve": "energyKjPerServe",
    "nutrition.energykjquantityper100g": "energyKjPer100g",
    "nutrition.proteinquantityper100g": "proteinPer100g",
    "nutrition.fattotalquantityper100g": "fatTotalPer100g",
    "nutrition.fatsaturatedquantityper100g": "fatSaturatedPer100g",
    "nutrition.carbohydratestotalquantityper100g": "carbohydratePer100g",
    "nutrition.sugarsquantityper100g": "sugarsPer100g",
    "nutrition.sodiumquantityper100mg": "sodiumPer100mg",
    "nutrition.allergencontains": "allergenContains",
}
CORE_KEYS = {
    "core.countryoforiginstatement": "countryOfOrigin",
    "core.netcontentuom": "netContentUom",
    "core.nameofbrandowner": "brandOwner",
    "core.subrange": "subrange",
}


def flatten_product(pdp_product, source_url, category_path=None):
    p = pdp_product or {}
    features = {}
    for group in p.get("classifications") or []:
        for feat in group.get("features") or []:
            fcode = (feat.get("code") or "").split("MetcashClassification/1.0/")[-1]
            vals = [fv.get("value") for fv in (feat.get("featureValues") or [])
                    if fv.get("value") not in (None, "")]
            features[fcode] = vals[0] if len(vals) == 1 else (vals or None)

    nutr = {dst: features.get(src) for src, dst in NUTRITION_KEYS.items()}
    core = {dst: features.get(src) for src, dst in CORE_KEYS.items()}
    extra = {k: v for k, v in features.items()
             if k not in NUTRITION_KEYS and k not in CORE_KEYS and v not in (None, [], "")}

    units = []
    for cu in p.get("consumerUnits") or []:
        units.append({
            "gtin": cu.get("gtin"),
            "metcashItemCode": cu.get("metcashItemCode"),
            "qty": cu.get("qty"),
            "cartonWeight": cu.get("cartonWeight"),
            "shelfTicketName": cu.get("shelfTicketName") or p.get("shelfticketname"),
        })

    offers = p.get("offers") or []
    prices = []
    for off in offers:
        pr = (off.get("price") or {})
        prices.append({
            "offerId": off.get("id"),
            "priceValue": pr.get("value"),
            "formattedValue": pr.get("formattedValue"),
            "currencyIso": pr.get("currencyIso"),
            "priceType": (off.get("priceData") or {}).get("priceType")
                         if isinstance(off.get("priceData"), dict) else None,
            "unitPrice": (off.get("priceData") or {}).get("unitPrice") if isinstance(off.get("priceData"), dict) else None,
            "startDate": off.get("startDate"),
            "endDate": off.get("endDate"),
        })

    return {
        "code": p.get("code"),
        "name": p.get("name"),
        "shelfTicketName": p.get("shelfticketname"),
        "manufacturer": p.get("manufacturer"),
        "gtin": p.get("gtin"),
        "tradeGtin": p.get("tradeGtin"),
        "metcashItemCode": p.get("metcashItemCode"),
        "primarySupplier": p.get("primarySupplier"),
        "primarySupplierCode": p.get("primarySupplierCode"),
        "commodityType": (p.get("commodityType") or {}).get("code") if isinstance(p.get("commodityType"), dict) else None,
        "subCommodity": p.get("subCommodity"),
        "department": p.get("department"),
        "packSize": p.get("packSize"),
        "packageQuantity": p.get("packageQuantity"),
        "taxName": p.get("taxName"),
        "availableDate": p.get("availableDate"),
        "unit": p.get("unit"),
        "unitPriceDisplay": p.get("unitPriceDisplay"),
        "consumerUnits": units,
        "breadcrumbs": [{"code": b.get("categoryCode"), "name": b.get("name"), "url": b.get("url")}
                        for b in (p.get("breadcrumbs") or [])],
        "categoryPath": category_path,
        "imageUrl": p.get("cloudinaryImgUrl"),
        "url": p.get("url"),
        "sourceUrl": source_url,
        "nutrition": nutr,
        "core": core,
        "attributes": extra,
        "offers": prices,
        "priceAvailable": bool(prices),
    }


def scrape_catalog(out_dir, workers=6, delay=0.0, limit=None, offset=0, refresh=False,
                   details=True, chunk_size=CHUNK_SIZE):
    import queue as queue_mod

    cat_dir = os.path.join(out_dir, "catalog")
    ensure_dir(cat_dir)
    sm = harvest_sitemap(out_dir, refresh=refresh)
    products = sm["products"]
    if offset:
        products = products[offset:]
    if limit:
        products = products[:limit]

    if not details:
        atomic_write_json(os.path.join(cat_dir, "index.json"), {
            "mode": "codes-only",
            "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "productCount": len(products),
            "chunks": [],
        })
        atomic_write_json(os.path.join(cat_dir, "products_codes.json"), products)
        return sm

    chunks = [products[i:i + chunk_size] for i in range(0, len(products), chunk_size)]
    chunk_of = {}
    need_per_chunk = {}
    for ci, chunk in enumerate(chunks):
        codes = [r["code"] for r in chunk]
        need_per_chunk[ci] = set(codes)
        for c in codes:
            chunk_of[c] = ci

    index_path = os.path.join(cat_dir, "index.json")
    index = load_json(index_path, {}) or {"chunks": []}
    if refresh:
        index = {"chunks": []}
    done_chunks = {c["chunkIndex"] for c in index.get("chunks", [])}
    pending = [r for r in products if chunk_of[r["code"]] not in done_chunks]

    log("catalog: {} products | {} chunks | {} pending ({} chunks already done)".format(
        len(products), len(chunks), len(pending), len(done_chunks)))
    if not pending:
        log("catalog: nothing to do")
        return sm

    work = queue_mod.Queue()
    for row in pending:
        work.put(row)

    buffers = collections.defaultdict(dict)
    write_lock = threading.Lock()
    counters = {"n": 0, "err": 0}
    t0 = time.time()

    def write_chunk(ci):
        buf = buffers.get(ci, {})
        chunk_codes = [r["code"] for r in chunks[ci]]
        items = [buf[c] for c in chunk_codes if c in buf]
        fname = "chunk-{:04d}.json".format(ci)
        payload = {
            "chunkIndex": ci,
            "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "itemCount": len(items),
            "errorCount": sum(1 for i in items if "error" in i),
            "items": items,
        }
        atomic_write_json(os.path.join(cat_dir, fname), payload)
        index.setdefault("chunks", []).append({
            "file": fname, "chunkIndex": ci, "itemCount": len(items),
            "errorCount": payload["errorCount"],
            "firstCode": chunk_codes[0], "lastCode": chunk_codes[-1],
        })
        index["chunks"].sort(key=lambda c: c["chunkIndex"])
        index["mode"] = "pdp-full"
        index["baseSite"] = BASE_SITE
        index["chunkSize"] = chunk_size
        index["productCount"] = len(products)
        index["fetchedAtUtc"] = payload["fetchedAtUtc"]
        atomic_write_json(index_path, index)
        buffers.pop(ci, None)

    def worker():
        while True:
            try:
                row = work.get_nowait()
            except queue_mod.Empty:
                return
            code = row["code"]
            url = (OCC + "/users/anonymous/products/" + urllib.parse.quote(code)
                   + "?fields=CCC_PDP_FULL")
            try:
                doc = json.loads(http_get(url, accept="application/json").decode("utf-8"))
                item = flatten_product(doc.get("product"), url, row.get("urlPath"))
            except Exception as exc:  # noqa: BLE001
                item = {"code": code, "urlPath": row.get("urlPath"),
                        "sourceUrl": url, "error": str(exc)[:200]}
            if delay:
                time.sleep(delay)
            ci = chunk_of[code]
            with write_lock:
                buffers[ci][code] = item
                counters["n"] += 1
                if "error" in item:
                    counters["err"] += 1
                complete = len(buffers[ci]) >= len(need_per_chunk[ci])
                if complete:
                    write_chunk(ci)
                    el = time.time() - t0
                    rate = counters["n"] / el if el else 0
                    eta = (len(pending) - counters["n"]) / rate if rate else 0
                    log("catalog: {}/{} items | {} chunks | {:.1f}/s | eta {:.0f} min | err {}".format(
                        counters["n"], len(pending), len(index.get("chunks", [])),
                        rate, eta / 60.0, counters["err"]))

    threads = [threading.Thread(target=worker) for _ in range(max(1, workers))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    with write_lock:
        for ci in list(buffers.keys()):
            write_chunk(ci)

    log("catalog: finished {} items, {} errors, {} chunks".format(
        counters["n"], counters["err"], len(index.get("chunks", []))))
    
    # Automatically export products by category into a folder after catalog finishes
    export_products_by_category(out_dir)
    return sm


# --------------------------------------------------------------- export by category

def export_products_by_category(out_dir):
    """
    Groups all harvested catalog products by their leaf category (from breadcrumbs)
    and saves each category's products into an individual JSON file inside a 'category_products' folder.
    """
    cat_dir = os.path.join(out_dir, "catalog")
    target_dir = os.path.join(out_dir, "category_products")
    ensure_dir(target_dir)

    chunks = sorted(glob.glob(os.path.join(cat_dir, "chunk-*.json")))
    if not chunks:
        log("export_by_category: no catalog chunks found in {}".format(cat_dir))
        return

    log("export_by_category: grouping products by category...")
    cat_products = collections.defaultdict(list)
    total_products = 0

    for fn in chunks:
        try:
            with open(fn, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data.get("items", []):
                if "error" in item:
                    continue
                bc = [b for b in (item.get("breadcrumbs") or []) if b.get("code")]
                if bc:
                    leaf_code = bc[-1]["code"]
                    cat_products[leaf_code].append(item)
                    total_products += 1
                else:
                    cat_products["uncategorized"].append(item)
                    total_products += 1
        except Exception as exc:
            log("export_by_category: error reading {}: {}".format(fn, exc))

    for cat_code, items in cat_products.items():
        file_path = os.path.join(target_dir, "{}.json".format(cat_code))
        payload = {
            "categoryCode": cat_code,
            "productCount": len(items),
            "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "products": items
        }
        atomic_write_json(file_path, payload)

    log("export_by_category: saved {} products across {} categories into '{}'".format(
        total_products, len(cat_products), target_dir))


# --------------------------------------------------------------- merge/complete

def merge_categories(out_dir):
    tree_path = os.path.join(out_dir, "categories.json")
    cat_dir = os.path.join(out_dir, "catalog")
    if not os.path.exists(tree_path):
        raise SystemExit("run --mode categories first")
    tree_doc = load_json(tree_path)

    nodes = {}
    def add(code, name, parent=None, in_api=False):
        n = nodes.get(code)
        if n is None:
            n = nodes[code] = {"code": code, "name": name, "parent": parent,
                               "inApiTree": in_api, "children": []}
        else:
            if name and not n.get("name"):
                n["name"] = name
            if parent and not n.get("parent"):
                n["parent"] = parent
            if in_api:
                n["inApiTree"] = True
        return n

    def walk(node, parent=None):
        add(node["code"], node["name"], parent, in_api=True)
        for k in node.get("subCategories", []):
            walk(k, node["code"])
    walk(tree_doc["tree"])
    for code, n in nodes.items():
        p = n.get("parent")
        if p and code not in nodes[p]["children"]:
            nodes[p]["children"].append(code)

    direct = collections.Counter()
    total = collections.Counter()
    chunks = sorted(glob.glob(os.path.join(cat_dir, "chunk-*.json")))
    if not chunks:
        raise SystemExit("no catalog chunks found in {}".format(cat_dir))
    products = 0
    for fn in chunks:
        with open(fn, "r", encoding="utf-8") as fh:
            for item in json.load(fh).get("items", []):
                bc = [b for b in (item.get("breadcrumbs") or []) if b.get("code")]
            if not bc:
                continue
            products += 1
            for i, b in enumerate(bc):
                parent = bc[i - 1]["code"] if i else None
                add(b["code"], b.get("name"), parent)
                if parent:
                    p = nodes[parent]
                    if b["code"] not in p["children"]:
                        p["children"].append(b["code"])
                total[b["code"]] += 1
                if i == len(bc) - 1:
                    direct[b["code"]] += 1

    def build(code, depth=1, path=""):
        n = nodes[code]
        name = n.get("name") or code
        p = (path + "/" + name) if path else name
        entry = {
            "code": code,
            "name": name,
            "path": p,
            "depth": depth,
            "inApiTree": n["inApiTree"],
            "directProductCount": direct.get(code, 0),
            "productCount": total.get(code, direct.get(code, 0)),
            "url": "/Open-Catalogue/c/" + str(code),
            "subCategories": [],
        }
        seen = set()
        for c in n["children"]:
            if c in seen or c == code:
                continue
            seen.add(c)
            entry["subCategories"].append(build(c, depth + 1, p))
        if not entry["subCategories"]:
            del entry["subCategories"]
        return entry

    roots = [c for c in nodes if not nodes[c].get("parent")]
    root_code = "1" if "1" in nodes else (sorted(roots)[0] if roots else None)
    complete = build(root_code)

    flat = []
    def flatten(node, chain=()):
        c = chain + ((node["code"], node["name"]),)
        flat.append({k: node[k] for k in
                     ("code", "name", "path", "depth", "inApiTree",
                      "directProductCount", "productCount", "url")} | {
            "breadcrumb": [{"code": a, "name": b} for a, b in c],
            "isLeaf": "subCategories" not in node,
        })
        for k in node.get("subCategories", []):
            flatten(k, c)
    flatten(complete)

    api_codes = {f["code"] for f in (load_json(os.path.join(out_dir, "categories_flat.json")) or [])}
    doc = {
        "source": "union of /category/campbells/subcategories and product breadcrumbs",
        "baseSite": BASE_SITE,
        "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "productsSeen": products,
        "nodeCount": len(flat),
        "leafCount": sum(1 for f in flat if f["isLeaf"]),
        "maxDepth": max((f["depth"] for f in flat), default=0),
        "inApiTree": sum(1 for f in flat if f["inApiTree"]),
        "onlyInProducts": sum(1 for f in flat if not f["inApiTree"]),
        "gapCodes": sorted(c for c in nodes if c not in api_codes),
        "gapNames": sorted({nodes[c]["name"] for c in nodes if c not in api_codes}),
        "tree": complete,
    }
    atomic_write_json(os.path.join(out_dir, "categories_complete.json"), doc)
    atomic_write_json(os.path.join(out_dir, "categories_complete_flat.json"), flat)

    # Also run export by category during merge
    export_products_by_category(out_dir)

    log("merge: {} nodes ({} in API tree, {} recovered from product breadcrumbs)".format(
        len(flat), doc["inApiTree"], doc["onlyInProducts"]))
    log("merge: max depth {}, products covered {}, {} gap categories".format(
        doc["maxDepth"], products, len(doc["gapCodes"])))
    return doc


# ------------------------------------------------------------------ promo/price

def oauth_token(email, password, retries=3):
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": OAUTH_CLIENT_ID,
        "username": email,
        "password": password,
    }).encode()
    hdrs = {"content-type": "application/x-www-form-urlencoded",
            "accept": "application/json", "user-agent": UA}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(TOKEN_URL, data=body, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise RuntimeError("OAuth HTTP {} :: {}".format(exc.code, detail))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError("OAuth failed: {}".format(last))


def scrape_promo(out_dir, email, password, workers=4, branch=None, refresh=False):
    if not email or not password:
        raise SystemExit(
            "promo mode needs credentials: --email/--password "
            "(or env CAMPBELLS_EMAIL / CAMPBELLS_PASSWORD).\n"
            "Anonymous access cannot read prices on this site -- every price endpoint is\n"
            "under /users/{userId}/ and returns 401 'Full authentication is required'."
        )

    out_cat = os.path.join(out_dir, "promo")
    ensure_dir(out_cat)
    log("promo: authenticating as {}".format(email))
    tok = oauth_token(email, password)
    access = tok["access_token"]
    hdrs = {"authorization": "Bearer " + access}
    if branch:
        hdrs["x-store-id"] = str(branch)

    uid = None
    for candidate in ("/users/current?fields=FULL", "/users/current"):
        try:
            me = get_json(candidate, headers=hdrs)
            uid = me.get("uid") or me.get("customerId") or me.get("userId")
            if uid:
                break
        except Exception:  # noqa: BLE001
            continue
    uid = uid or email
    log("promo: userId={}".format(uid))

    cats = load_json(os.path.join(out_dir, "categories_flat.json")) or []
    leaves = [c for c in cats if c.get("isLeaf")]
    if not leaves:
        log("promo: run --mode categories first for the leaf list")

    prog = Progress(os.path.join(out_cat, "progress.json"))
    results = []
    for leaf in leaves:
        code = leaf["code"]
        fname = "cat-{}.json".format(code)
        fpath = os.path.join(out_cat, fname)
        if os.path.exists(fpath) and not refresh:
            results.append(load_json(fpath))
            continue
        url = "{}/users/{}/category/{}/export-products?query={}".format(
            OCC, urllib.parse.quote(str(uid)), urllib.parse.quote(str(code)),
            urllib.parse.quote("fields=FULL"))
        try:
            raw = http_get(url, headers=hdrs, accept="application/json, text/csv, */*")
            items = []
            try:
                doc = json.loads(raw.decode("utf-8"))
                items = doc.get("products") or doc.get("items") or doc
            except Exception:  # noqa: BLE001 - may be CSV
                items = {"_rawCsv": raw.decode("utf-8", "replace")[:500000]}
            payload = {"categoryCode": code, "categoryPath": leaf.get("path"),
                       "url": url, "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "items": items}
            atomic_write_json(fpath, payload)
            prog.mark(fname)
            results.append(payload)
            log("promo: {} -> {} items".format(code, len(items) if isinstance(items, list) else "csv"))
        except Exception as exc:  # noqa: BLE001
            prog.failed(fname, exc)
            log("promo: {} FAILED {}".format(code, exc))

    atomic_write_json(os.path.join(out_cat, "index.json"), {
        "mode": "promo-authenticated",
        "userId": uid,
        "branch": branch,
        "categories": len(leaves),
        "fetched": len(results),
        "fetchedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return results


# ----------------------------------------------------------------------- driver

def main():
    ap = argparse.ArgumentParser(description="Campbells AU (sorted.au / Metcash) scraper")
    ap.add_argument("--mode", default="all",
                    choices=["stores", "categories", "catalog", "merge", "promo", "all"])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--workers", type=int, default=12,
                    help="concurrent HTTP requests (12 verified safe; 20 also fine)")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=None, help="max products (catalog mode)")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    ap.add_argument("--cat-passes", type=int, default=0,
                    help="category expansion passes after the root call; 0 = exhaustive, "
                         "1 resolves the full current tree in ~30s")
    ap.add_argument("--codes-only", action="store_true", help="catalog: sitemap only, skip PDP calls")
    ap.add_argument("--branch", default=None, help="branch/store code for promo mode")
    ap.add_argument("--email", default=os.environ.get("CAMPBELLS_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CAMPBELLS_PASSWORD"))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    ensure_dir(args.out)
    started = time.time()
    log("campbells AU scraper | mode={} out={}".format(args.mode, args.out))

    if args.mode in ("stores", "all"):
        scrape_stores(args.out, refresh=args.refresh)
    if args.mode in ("categories", "all"):
        scrape_categories(args.out, refresh=args.refresh, workers=args.workers,
                          max_passes=args.cat_passes)
    if args.mode in ("catalog", "all"):
        scrape_catalog(args.out, workers=args.workers, delay=args.delay, limit=args.limit,
                       offset=args.offset, refresh=args.refresh,
                       details=not args.codes_only, chunk_size=args.chunk_size)
    if args.mode in ("merge", "all"):
        merge_categories(args.out)
    if args.mode == "promo":
        scrape_promo(args.out, args.email, args.password, workers=args.workers,
                     branch=args.branch, refresh=args.refresh)

    log("done in {:.1f}s".format(time.time() - started))


if __name__ == "__main__":
    main()