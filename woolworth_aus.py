# import argparse
# import csv
# import json
# import os
# import random
# import re
# import sys
# import time
# import urllib.error
# import urllib.request

# try:
#     sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# except Exception:
#     pass

# BASE = "https://www.woolworths.com.au"
# LANDING = BASE + "/shop/storelocator"
# UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
#       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# HERE = os.path.dirname(os.path.abspath(__file__))
# OUT = os.environ.get("WWAU_OUT", os.path.join(HERE, "wwau_out"))

# PAGE_SIZE = 36
# BATCH_CODES = 20            # /apis/ui/products/<list> - keep at 20

# # Australia sweep centers: every Woolworths store is within 1000 km of one of these.
# # Max=1000 is the API's ceiling; per-center counts are printed so a capped query is visible.
# CENTERS = [
#     ("sydney", -33.865143, 151.2099),
#     ("melbourne", -37.8136, 144.9631),
#     ("brisbane", -27.4698, 153.0251),
#     ("perth", -31.9523, 115.8613),
#     ("adelaide", -34.9285, 138.6007),
#     ("hobart", -42.8821, 147.3272),
#     ("darwin", -12.4634, 130.8456),
#     ("cairns", -16.9186, 145.7781),
#     ("alice-springs", -23.6980, 133.8807),
#     ("canberra", -35.2809, 149.1300),
#     ("townsville", -19.2589, 146.8169),
#     ("port-hedland", -20.3124, 118.6100),
#     ("broome", -17.9614, 122.2359),
#     ("kalgoorlie", -30.7490, 121.4660),
#     ("esperance", -33.8608, 121.8897),
#     ("albany", -35.0269, 117.8837),
#     ("mildura", -34.1855, 142.1621),
#     ("rockhampton", -23.3791, 150.5100),
#     ("port-macquarie", -31.4333, 152.9083),
# ]

# # ---------------------------------------------------------------------------
# # errors / logging
# # ---------------------------------------------------------------------------
# class BlockedResponse(Exception):
#     """Egress blocked or a bot-wall page: NOT transient, do not grind on retries."""

#     def __init__(self, url, status, body_head):
#         self.url, self.status, self.body_head = url, status, body_head
#         super().__init__("HTTP %s on %s | body starts: %r"
#                          % (status, url, (body_head or "")[:160]))

# class CredentialsError(Exception):
#     pass

# def log(msg):
#     print(msg, flush=True)

# def atomic_write(path, text):
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as f:
#         f.write(text)
#     os.replace(tmp, path)

# def safe_name(s):
#     return re.sub(r"[^A-Za-z0-9 -]", "", s or "").strip().replace(" ", "") or "unnamed"

# def jitter_sleep(base):
#     time.sleep(base + random.uniform(0, base * 0.4))

# # ---------------------------------------------------------------------------
# # transports: direct HTTP, or a HEADED browser (Akamai blocks everything else)
# # ---------------------------------------------------------------------------
# class HttpTransport(object):
#     name = "http"

#     def call(self, path, method="GET", body=None, timeout=30):
#         url = path if path.startswith("http") else BASE + path
#         data = json.dumps(body).encode("utf-8") if body is not None else None
#         req = urllib.request.Request(url, data=data, method=method, headers={
#             "User-Agent": UA,
#             "Accept": "application/json, text/plain, /",
#             "Accept-Language": "en-AU,en;q=0.9",
#             "Content-Type": "application/json",
#         })
#         try:
#             with urllib.request.urlopen(req, timeout=timeout) as r:
#                 return r.status, r.read().decode("utf-8", "replace")
#         except urllib.error.HTTPError as e:
#             return e.code, e.read().decode("utf-8", "replace")[:4000]
#         except Exception as e:
#             return -1, "%s: %s" % (type(e).__name__, str(e)[:200])

#     def close(self):
#         pass

# class BrowserTransport(object):
#     """Headed Chromium. headless=True is blocked by Akamai - do not set it."""
#     name = "browser"

#     def __init__(self, headless=False, reload_every=25):
#         from playwright.sync_api import sync_playwright
#         self._pw = sync_playwright().start()
#         self._browser = self._pw.chromium.launch(
#             headless=headless,
#             args=["--disable-blink-features=AutomationControlled"])
#         self._ctx = self._browser.new_context(
#             user_agent=UA, locale="en-AU", viewport={"width": 1400, "height": 950})
#         self._page = self._ctx.new_page()
#         self._page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
#         self._page.wait_for_timeout(3000)
#         self._calls = 0
#         self._reload_every = reload_every

#     def _reload(self):
#         try:
#             self._page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
#             self._page.wait_for_timeout(1500)
#         except Exception:
#             pass

#     def call(self, path, method="GET", body=None, timeout=45):
#         js = """async (a) => {
#             const [method, url, body, ms] = a;
#             const ctl = new AbortController();
#             const t = setTimeout(() => ctl.abort(), ms);
#             try {
#                 const opts = {method: method,
#                               headers: {'Accept': 'application/json',
#                                         'Content-Type': 'application/json'},
#                               signal: ctl.signal};
#                 if (body !== null) opts.body = JSON.stringify(body);
#                 const r = await fetch(url, opts);
#                 const txt = await r.text();
#                 clearTimeout(t);
#                 return {status: r.status, text: txt};
#             } catch (e) {
#                 clearTimeout(t);
#                 return {status: -1, text: String(e)};
#             }
#         }"""
#         if self._calls and self._calls % self._reload_every == 0:
#             self._reload()
#         self._calls += 1
#         try:
#             res = self._page.evaluate(js, [method, path, body, timeout * 1000])
#         except Exception:
#             self._reload()
#             res = self._page.evaluate(js, [method, path, body, timeout * 1000])
#         return res.get("status", -1), res.get("text", "") or ""

#     def close(self):
#         try:
#             self._browser.close()
#         finally:
#             try:
#                 self._pw.stop()
#             except Exception:
#                 pass

# class Api(object):
#     """Retry policy: transient -> backoff; blocked -> 2 tries then raise."""

#     def __init__(self, transport, delay=0.4, verbose=True):
#         self.t = transport
#         self.delay = delay
#         self.verbose = verbose
#         self.consecutive_failures = 0

#     def raw(self, path, method="GET", body=None):
#         return self.t.call(path, method, body)

#     def call(self, path, method="GET", body=None, attempts=4):
#         last = None
#         for i in range(attempts):
#             status, text = self.t.call(path, method, body)
#             if status == 200 and text:
#                 try:
#                     out = json.loads(text)
#                 except ValueError:
#                     last = BlockedResponse(path, status, text)
#                     break                       # non-JSON body = bot wall, not transient
#                 self.consecutive_failures = 0
#                 jitter_sleep(self.delay)
#                 return out
#             if status in (401, 403):
#                 last = BlockedResponse(path, status, text)
#                 break                          # credentials / egress: never retry
#             if status == -1:
#                 last = RuntimeError("request failed: %s" % text[:120])
#             else:
#                 last = RuntimeError("HTTP %s: %s" % (status, text[:160]))
#             wait = 2 ** (i + 1)
#             if self.verbose:
#                 log("    retry %d/%d in %ss (%s) %s"
#                     % (i + 1, attempts, wait, status, path.split("?")[0][:70]))
#             time.sleep(wait + random.uniform(0, 1))
#         self.consecutive_failures += 1
#         raise last if last else RuntimeError("call failed: %s" % path)

# def pick_transport(mode, headless, delay):
#     """auto: probe with urllib, fall back to a headed browser on 403 / non-JSON."""
#     if mode == "browser":
#         log("[transport] headed Chromium (Playwright)")
#         return BrowserTransport(headless=headless)
#     probe = HttpTransport()
#     status, text = probe.call("/apis/ui/settings")
#     ok = False
#     try:
#         ok = status == 200 and isinstance(json.loads(text), (list, dict))
#     except Exception:
#         ok = False
#     if mode == "http":
#         if not ok:
#             raise BlockedResponse("/apis/ui/settings", status, text)
#         log("[transport] direct HTTP")
#         return probe
#     if ok:
#         log("[transport] direct HTTP works from this IP")
#         return probe
#     log("[transport] direct HTTP blocked (HTTP %s) -> headed Chromium fallback" % status)
#     return BrowserTransport(headless=headless)

# # ---------------------------------------------------------------------------
# # response helpers
# # ---------------------------------------------------------------------------
# def products_from(obj):
#     """Pull product dicts out of either response shape.

#     browse/category -> Products live in Bundles[].Products[]
#     Search/products -> Products[].Products[]   (tile groups)
#     /apis/ui/products/<codes> -> a flat list
#     """
#     out = []
#     if isinstance(obj, list):
#         for it in obj:
#             if isinstance(it, dict) and it.get("Stockcode"):
#                 out.append(it)
#         return out
#     for key in ("Bundles", "Products", "products", "Items"):
#         v = obj.get(key)
#         if not isinstance(v, list):
#             continue
#         for it in v:
#             if not isinstance(it, dict):
#                 continue
#             if it.get("Stockcode"):
#                 out.append(it)
#                 continue
#             for inner_key in ("Products", "Items"):
#                 inner = it.get(inner_key)
#                 if isinstance(inner, list):
#                     for p in inner:
#                         if isinstance(p, dict) and p.get("Stockcode"):
#                             out.append(p)
#     return out

# def trim_product(p, extra=None):
#     row = {
#         "stockcode": p.get("Stockcode"),
#         "barcode": p.get("Barcode"),
#         "name": p.get("Name"),
#         "displayName": p.get("DisplayName"),
#         "brand": p.get("Brand"),
#         "price": p.get("Price"),
#         "wasPrice": p.get("WasPrice"),
#         "savingsAmount": p.get("SavingsAmount"),
#         "instorePrice": p.get("InstorePrice"),
#         "instoreWasPrice": p.get("InstoreWasPrice"),
#         "cupString": p.get("CupString"),
#         "isOnSpecial": p.get("IsOnSpecial"),
#         "isHalfPrice": p.get("IsHalfPrice"),
#         "isOnlineOnly": p.get("IsOnlineOnly"),
#         "isAvailable": p.get("IsAvailable"),
#         "isInStock": p.get("IsInStock"),
#         "packageSize": p.get("PackageSize"),
#         "urlFriendlyName": p.get("UrlFriendlyName"),
#         "source": p.get("Source"),
#         "image": p.get("MediumImageFile") or p.get("SmallImageFile"),
#     }
#     if extra:
#         row.update(extra)
#     return row

# def category_tree(api):
#     j = api.call("/apis/ui/PiesCategoriesWithSpecials")
#     return j.get("Categories", j if isinstance(j, list) else [])

# def walk_tree(nodes, path=(), acc=None):
#     """Yield (url_path, nodeid, name, depth, node) for every node."""
#     acc = [] if acc is None else acc
#     for n in nodes:
#         if not isinstance(n, dict):
#             continue
#         seg = n.get("UrlFriendlyName")
#         new_path = tuple(list(path) + ([seg] if seg else []))
#         acc.append(("/shop/browse/" + "/".join(new_path), n.get("NodeId"),
#                     n.get("Description"), len(new_path), n))
#         walk_tree(n.get("Children") or [], new_path, acc)
#     return acc

# def browse(api, cat_url, cat_id, name, page, is_special=False, store_id=None):
#     body = {
#         "categoryId": cat_id,
#         "pageNumber": page,
#         "pageSize": PAGE_SIZE,
#         "sortType": "TraderRelevance",
#         "url": cat_url,
#         "location": cat_url,
#         "formatObject": json.dumps({"name": name or ""}),
#         "isSpecial": bool(is_special),
#         "isHideUnavailableProducts": False,
#         "isBundle": False,
#         "isMobile": False,
#         "isRegisteredRewardCardPromotion": False,
#         "isHideEverydayMarketProducts": False,
#         "filters": [],
#         "categoryVersion": "v2",
#     }
#     if store_id:
#         body["storeId"] = str(store_id)
#     return api.call("/apis/ui/browse/category", "POST", body)

# def paged_browse(api, cat_url, cat_id, name, is_special=False, store_id=None,
#                  max_pages=1000, seen=None, on_page=None):
#     """Follow pages until empty / all-new-stockcodes stop. Returns (rows, declared)."""
#     seen = set() if seen is None else seen
#     rows, declared, dup_streak = [], None, 0
#     for page in range(1, max_pages + 1):
#         j = browse(api, cat_url, cat_id, name, page, is_special, store_id)
#         if declared is None:
#             try:
#                 declared = int(j.get("TotalRecordCount") or 0)
#             except Exception:
#                 declared = 0
#         items = products_from(j)
#         fresh = [p for p in items if str(p.get("Stockcode")) not in seen]
#         if not items:
#             on_page and on_page(page, 0, len(rows))
#             break
#         if not fresh:
#             dup_streak += 1
#             if dup_streak >= 2:
#                 on_page and on_page(page, 0, len(rows))
#                 break
#         else:
#             dup_streak = 0
#         for p in fresh:
#             seen.add(str(p.get("Stockcode")))
#         rows.extend(fresh)
#         on_page and on_page(page, len(fresh), len(rows))
#         if declared and len(rows) >= declared:
#             break
#     return rows, declared

# # ---------------------------------------------------------------------------
# # run-state helpers (resume + summary)
# # ---------------------------------------------------------------------------
# class RunState(object):
#     def __init__(self, mode, resume):
#         self.dir = os.path.join(OUT, mode)
#         self.jl = os.path.join(OUT, "%s.jsonl" % mode)
#         self.summary_path = os.path.join(OUT, "_summary.json")
#         self.progress_path = os.path.join(OUT, "progress.json")
#         os.makedirs(self.dir, exist_ok=True)
#         self.progress = {}
#         self.summary = []
#         if os.path.exists(self.progress_path):
#             try:
#                 self.progress = json.load(open(self.progress_path, encoding="utf-8"))
#             except Exception:
#                 self.progress = {}
#         self.mode = mode
#         self.resume = resume

#     def done(self, key):
#         return bool(self.resume and self.progress.get(self.mode, {}).get(key, {}).get("done"))

#     def mark(self, key, **info):
#         self.progress.setdefault(self.mode, {})[key] = dict(info, done=True)
#         atomic_write(self.progress_path, json.dumps(self.progress, indent=1))

#     def item_file(self, key):
#         return os.path.join(self.dir, safe_name(key) + ".json")

#     def write_item(self, key, payload):
#         atomic_write(self.item_file(key),
#                      json.dumps(payload, ensure_ascii=False, indent=1))

#     def append_rows(self, rows):
#         if not rows:
#             return
#         with open(self.jl, "a", encoding="utf-8") as f:
#             for r in rows:
#                 f.write(json.dumps(r, ensure_ascii=False) + "\n")

#     def add_summary(self, row):
#         self.summary.append(row)
#         atomic_write(self.summary_path,
#                      json.dumps({"mode": self.mode, "items": self.summary},
#                                 ensure_ascii=False, indent=1))

#     def write_csv(self, name, rows):
#         if not rows:
#             return
#         path = os.path.join(OUT, name)
#         cols = list(rows[0].keys())
#         with open(path, "w", newline="", encoding="utf-8") as f:
#             w = csv.DictWriter(f, fieldnames=cols)
#             w.writeheader()
#             for r in rows:
#                 w.writerow(r)
#         log("  csv -> %s (%d rows)" % (path, len(rows)))

# # ---------------------------------------------------------------------------
# # commands
# # ---------------------------------------------------------------------------
# def cmd_stores(api, state):
#     log("[stores] sweeping %d centers (Max=1000 each)" % len(CENTERS))
#     by_no, per_center, capped = {}, {}, []
#     for tag, lat, lon in CENTERS:
#         key = "center:%s" % tag
#         if state.done(key):
#             log("  %-14s skipped (done)" % tag)
#             continue
#         path = ("/apis/ui/StoreLocator/Stores?Max=1000&Division=SUPERMARKETS,EG,AMPOL"
#                 "&Facility=&latitude=%s&longitude=%s" % (lat, lon))
#         t0 = time.time()
#         j = api.call(path)
#         stores = j.get("Stores", []) if isinstance(j, dict) else []
#         for s in stores:
#             if s.get("StoreNo"):
#                 by_no[str(s["StoreNo"])] = s
#         per_center[tag] = len(stores)
#         if len(stores) >= 1000:
#             capped.append(tag)
#         log("  %-14s %4d stores (%ds)" % (tag, len(stores), int(time.time() - t0)))
#         state.mark(key)
#         state.add_summary({"center": tag, "fetched": len(stores),
#                            "seconds": int(time.time() - t0), "ok": True})

#     if not by_no:
#         log("[stores] nothing fetched - see the error above; refusing to write a 0-store file")
#         return 1

#     rows = sorted(by_no.values(), key=lambda s: (s.get("State") or "", s.get("Suburb") or ""))
#     atomic_write(os.path.join(OUT, "wwau_stores.json"),
#                  json.dumps({"fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
#                              "perCenter": per_center, "count": len(rows),
#                              "stores": rows}, ensure_ascii=False, indent=1))
#     flat = [{
#         "StoreNo": s.get("StoreNo"), "Name": s.get("Name"),
#         "Division": s.get("Division"), "AddressLine1": s.get("AddressLine1"),
#         "Suburb": s.get("Suburb"), "State": s.get("State"),
#         "Postcode": s.get("Postcode"), "Latitude": s.get("Latitude"),
#         "Longitude": s.get("Longitude"), "Phone": s.get("Phone"),
#         "IsOpen": s.get("IsOpen"),
#         "Today": (s.get("TradingHours") or [{}])[0].get("TradingHourForDisplay", "")
#         if isinstance(s.get("TradingHours"), list) and s.get("TradingHours") else "",
#     } for s in rows]
#     cols = list(flat[0].keys())
#     with open(os.path.join(OUT, "wwau_stores.csv"), "w", newline="",
#               encoding="utf-8") as f:
#         w = csv.DictWriter(f, fieldnames=cols)
#         w.writeheader()
#         for r in flat:
#             w.writerow(r)

#     div, st = {}, {}
#     for s in rows:
#         div[s.get("Division")] = div.get(s.get("Division"), 0) + 1
#         st[s.get("State")] = st.get(s.get("State"), 0) + 1
#     log("\n[stores] %d unique locations -> %s/wwau_stores.json (+ .csv)"
#         % (len(rows), OUT))
#     log("  by division: %s" % json.dumps(div))
#     log("  by state   : %s" % json.dumps(st))
#     if capped:
#         log("  NOTE: these centers hit the API ceiling (Max=1000): %s "
#             "- union still covers AU, but re-run if a state looks thin." % capped)
#     return 0

# def specials_nodes(tree):
#     """The real specials buckets only.

#     depth 2 under the specials group  -> /shop/browse/specials/<bucket>
#       (half-price, online-only-specials, seasonal-price, lower-shelf-price,
#        everyday-low-price, buy-more-save-more, bundles, everyday-market-specials)
#     plus each department's own specials leaf -> /shop/browse/<dept>/<dept>-specials

#     Deeper nodes are ordinary product categories that merely hang off a specials
#     bucket (e.g. online-only-specials/gift-ideas) - crawling them wastes hours and
#     adds nothing. '<dept>-specials' leaves are real second deal set, keep them.
#     """
#     out, seen = [], set()
#     for url, nid, name, depth, node in walk_tree(tree):
#         segs = [s for s in url.split("/") if s]
#         if segs[:2] != ["shop", "browse"]:
#             continue
#         rest = segs[2:]
#         keep = False
#         if rest and rest[0] == "specials" and len(rest) == 2:
#             keep = True
#         elif rest and rest[-1].endswith("-specials"):
#             keep = True
#         if keep and url not in seen:
#             seen.add(url)
#             out.append((url, nid, name))
#     return out

# def cmd_specials(api, state, limit_cats=0):
#     tree = category_tree(api)
#     cats = specials_nodes(tree)
#     if limit_cats:
#         cats = cats[:limit_cats]
#     log("[specials] %d specials categories" % len(cats))
#     total = 0
#     for idx, (url, nid, name) in enumerate(cats, 1):
#         if state.done(url):
#             log("[%d/%d] %s: skipped (done)" % (idx, len(cats), name))
#             continue
#         t0 = time.time()
#         try:
#             rows, declared = paged_browse(api, url, nid, name, is_special=False)
#         except BlockedResponse as e:
#             if "AuthenticationException" in (e.body_head or ""):
#                 log("[%d/%d] %s: login-gated, skipping" % (idx, len(cats), name))
#                 state.add_summary({"url": url, "name": name, "ok": False,
#                                    "skipped": "login-required"})
#                 state.mark(url, count=0, skipped="login-required")
#                 continue
#             log("[%d/%d] %s: BLOCKED %s" % (idx, len(cats), name, e))
#             state.add_summary({"url": url, "name": name, "ok": False,
#                                "error": str(e)[:200]})
#             if api.consecutive_failures >= 3:
#                 log("  aborting: 3 consecutive failures")
#                 return 1
#             continue
#         rows = [trim_product(p, {"categoryUrl": url, "categoryName": name}) for p in rows]
#         state.write_item(name or nid, {"url": url, "categoryId": nid, "name": name,
#                                        "declared": declared, "count": len(rows),
#                                        "products": rows})
#         state.append_rows(rows)
#         state.mark(url, count=len(rows), declared=declared)
#         secs = int(time.time() - t0)
#         state.add_summary({"url": url, "name": name, "declared": declared,
#                            "fetched": len(rows), "ok": True, "seconds": secs})
#         total += len(rows)
#         log("[%d/%d] %-40s %4d/%s products (%ds)"
#             % (idx, len(cats), name[:40], len(rows), declared or "?", secs))
#     log("\n[specials] %d product rows -> %s/specials.jsonl" % (total, OUT))
#     return 0

# def cmd_catalog(api, state, limit_cats=0, max_pages=1000):
#     tree = category_tree(api)
#     leaves = [(url, nid, name) for url, nid, name, depth, node in walk_tree(tree)
#               if not (node.get("Children") or [])]
#     if limit_cats:
#         leaves = leaves[:limit_cats]
#     log("[catalog] %d leaf categories" % len(leaves))
#     total = 0
#     for idx, (url, nid, name) in enumerate(leaves, 1):
#         if state.done(url):
#             continue
#         t0 = time.time()
#         try:
#             rows, declared = paged_browse(api, url, nid, name, max_pages=max_pages)
#         except BlockedResponse as e:
#             state.add_summary({"url": url, "name": name, "ok": False,
#                                "error": str(e)[:200]})
#             if api.consecutive_failures >= 3:
#                 log("  aborting: 3 consecutive failures")
#                 return 1
#             continue
#         rows = [trim_product(p, {"categoryUrl": url, "categoryName": name}) for p in rows]
#         state.write_item(name or nid, {"url": url, "categoryId": nid, "name": name,
#                                        "declared": declared, "count": len(rows),
#                                        "products": rows})
#         state.append_rows(rows)
#         state.mark(url, count=len(rows), declared=declared)
#         state.add_summary({"url": url, "name": name, "declared": declared,
#                            "fetched": len(rows), "ok": True,
#                            "seconds": int(time.time() - t0)})
#         total += len(rows)
#         log("[%d/%d] %-40s %4d/%s (%ds)" % (idx, len(leaves), (name or "")[:40],
#                                             len(rows), declared or "?",
#                                             int(time.time() - t0)))
#     log("\n[catalog] %d product rows -> %s/catalog.jsonl" % (total, OUT))
#     return 0

# def cmd_per_store(api, state, limit=0, store_ids=None, cat_url=None, cat_id=None,
#                   name=None, max_pages=5):
#     """
#     Store-scoped sweep. Prices are national (see header) - what changes per store is
#     assortment/availability. We browse a category with storeId in the body and record
#     per-store presence.
#     """
#     sf = os.path.join(OUT, "wwau_stores.json")
#     if not os.path.exists(sf):
#         log("[per-store] no wwau_stores.json - run stores first")
#         return 1
#     stores = json.load(open(sf, encoding="utf-8"))["stores"]
#     if store_ids:
#         want = set(str(s) for s in store_ids)
#         stores = [s for s in stores if str(s.get("StoreNo")) in want]
#     if limit:
#         stores = stores[:limit]
#     cat_url = cat_url or "/shop/browse/specials/half-price"
#     cat_id = cat_id or "specialsgroup.3676"
#     name = name or "Half Price"
#     log("[per-store] %d stores x %s (storeId in body)" % (len(stores), cat_url))

#     for idx, s in enumerate(stores, 1):
#         sid = str(s.get("StoreNo"))
#         if state.done(sid):
#             continue
#         t0 = time.time()
#         try:
#             rows, declared = paged_browse(api, cat_url, cat_id, name,
#                                           is_special=True, store_id=sid,
#                                           max_pages=max_pages)
#         except BlockedResponse as e:
#             state.add_summary({"store": sid, "ok": False, "error": str(e)[:200]})
#             if api.consecutive_failures >= 3:
#                 log("  aborting: 3 consecutive failures")
#                 return 1
#             continue
#         rows = [trim_product(p, {"categoryUrl": cat_url, "storeNo": sid}) for p in rows]
#         state.write_item("%s_%s" % (s.get("Name"), sid),
#                          {"storeNo": sid, "storeName": s.get("Name"),
#                           "suburb": s.get("Suburb"), "state": s.get("State"),
#                           "categoryUrl": cat_url, "declared": declared,
#                           "count": len(rows), "products": rows})
#         state.append_rows(rows)
#         state.mark(sid, count=len(rows), declared=declared)
#         state.add_summary({"store": sid, "name": s.get("Name"), "declared": declared,
#                            "fetched": len(rows), "ok": True,
#                            "seconds": int(time.time() - t0)})
#         log("[%d/%d] %-34s %4d/%s (%ds)"
#             % (idx, len(stores), (s.get("Name") or "")[:34], len(rows),
#                declared or "?", int(time.time() - t0)))
#     log("\n[per-store] done -> %s/per-store/" % OUT)
#     return 0

# def cmd_diagnose(api):
#     log("== diagnose: one request per endpoint, plus the store-variance proof ==\n")
#     checks = [
#         ("storeLocator", "/apis/ui/StoreLocator/Stores?Max=30&Division=SUPERMARKETS"
#                          "&Facility=&latitude=-33.865143&longitude=151.2099"),
#         ("categoryTree", "/apis/ui/PiesCategoriesWithSpecials"),
#         ("settings", "/apis/ui/settings"),
#     ]
#     for label, path in checks:
#         status, text = api.raw(path)
#         n, verdict = 0, ""
#         try:
#             j = json.loads(text)
#             if label == "storeLocator":
#                 n = len(j.get("Stores", []))
#             elif label == "categoryTree":
#                 n = len(j.get("Categories", []))
#             else:
#                 n = len(j)
#             verdict = "OK"
#         except Exception:
#             verdict = "NOT JSON -> blocked egress or intercepting proxy"
#         log("  %-14s HTTP %-4s %8d bytes  %-6s %s" % (label, status, len(text), n, verdict))

#     j = api.call("/apis/ui/PiesCategoriesWithSpecials")
#     nodes = walk_tree(j.get("Categories", []))
#     leaves = [x for x in nodes if not (x[4].get("Children") or [])]
#     log("  category tree : %d nodes, %d leaves" % (len(nodes), len(leaves)))

#     body = browse(api, "/shop/browse/specials/half-price", "specialsgroup.3676",
#                   "Half Price", 1)
#     rows = products_from(body)
#     log("  browse half-price: %d products on page 1, TotalRecordCount=%s"
#         % (len(rows), body.get("TotalRecordCount")))

#     log("\n  -- store variance (Sydney 1550 vs Perth 4365 vs Darwin 5652) --")
#     search = {"EnableAdReRanking": False, "ExcludeSearchTypes": ["UntraceableVendors"],
#               "Filters": [], "GpBoost": 0, "IsHideEverydayMarketProducts": False,
#               "IsHideUnavailableProducts": False,
#               "IsRegisteredRewardCardPromotion": False, "IsSpecial": False,
#               "PageNumber": 1, "PageSize": 36, "SortType": "TraderRelevance"}
#     maps = {}
#     for tag, sid in (("sydney", "1550"), ("perth", "4365"), ("darwin", "5652")):
#         b = dict(search, SearchTerm="milk",
#                  Location="/shop/search/products?searchTerm=milk", StoreId=sid)
#         j = api.call("/apis/ui/Search/products", "POST", b)
#         m = {}
#         for p in products_from(j):
#             m[str(p.get("Stockcode"))] = p.get("Price")
#         maps[tag] = m
#         log("    %-7s %3d products returned" % (tag, len(m)))
#     keys = set(maps["sydney"]) | set(maps["perth"]) | set(maps["darwin"])
#     price_diff = [k for k in keys
#                   if len({maps[t].get(k) for t in maps if k in maps[t]}) > 1]
#     log("    price differences across the 3 stores: %d" % len(price_diff))
#     log("    assortment differences: %d items not returned by all 3"
#         % sum(1 for k in keys if any(k not in m for m in maps.values())))
#     log("\n  CONCLUSION: prices national, availability store-scoped "
#         "(re-run this after any site change).")
#     return 0

# # ---------------------------------------------------------------------------
# def main():
#     ap = argparse.ArgumentParser(description="Woolworths AU scraper")
#     ap.add_argument("mode", choices=["stores", "specials", "catalog", "per-store",
#                                      "diagnose"])
#     ap.add_argument("--transport", choices=["auto", "http", "browser"], default="auto")
#     ap.add_argument("--headless", action="store_true",
#                     help="only useful on a clean/AU IP; Akamai blocks headless")
#     ap.add_argument("--delay", type=float, default=0.4)
#     ap.add_argument("--limit", type=int, default=0,
#                     help="limit categories (specials/catalog) or stores (per-store)")
#     ap.add_argument("--max-pages", type=int, default=1000)
#     ap.add_argument("--stores", default="", help="per-store: comma separated StoreNo")
#     ap.add_argument("--category", default="", help="per-store: category url")
#     ap.add_argument("--category-id", default="", help="per-store: categoryId")
#     ap.add_argument("--category-name", default="", help="per-store: display name")
#     ap.add_argument("--resume", action="store_true")
#     ap.add_argument("--out", default="")
#     a = ap.parse_args()

#     global OUT
#     if a.out:
#         OUT = a.out
#     os.makedirs(OUT, exist_ok=True)

#     transport = pick_transport(a.transport, a.headless, a.delay)
#     api = Api(transport, delay=a.delay)
#     state = RunState(a.mode, a.resume)
#     try:
#         if a.mode == "diagnose":
#             return cmd_diagnose(api)
#         if a.mode == "stores":
#             return cmd_stores(api, state)
#         if a.mode == "specials":
#             return cmd_specials(api, state, a.limit)
#         if a.mode == "catalog":
#             return cmd_catalog(api, state, a.limit, a.max_pages)
#         if a.mode == "per-store":
#             ids = [s.strip() for s in a.stores.split(",") if s.strip()]
#             return cmd_per_store(api, state, a.limit, ids,
#                                  a.category or None, a.category_id or None,
#                                  a.category_name or None, a.max_pages)
#     except BlockedResponse as e:
#         log("\nBLOCKED: %s" % e)
#         log("Fix the egress: run with --transport browser (headed Chromium), or from "
#             "an AU/clean IP with --transport http.")
#         return 2
#     except KeyboardInterrupt:
#         log("\ninterrupted - re-run with --resume")
#         return 130
#     finally:
#         transport.close()
#     return 0

# if __name__ == "__main__":
#     sys.exit(main())












import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "https://www.woolworths.com.au"
LANDING = BASE + "/shop/storelocator"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("WWAU_OUT", os.path.join(HERE, "wwau_out"))

PAGE_SIZE = 36

# ---------------------------------------------------------------------------
# errors / logging
# ---------------------------------------------------------------------------
class BlockedResponse(Exception):
    def __init__(self, url, status, body_head):
        self.url, self.status, self.body_head = url, status, body_head
        super().__init__("HTTP %s on %s | body starts: %r"
                         % (status, url, (body_head or "")[:160]))

def log(msg):
    print(msg, flush=True)

def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

def safe_name(s):
    return re.sub(r"[^A-Za-z0-9 -]", "", s or "").strip().replace(" ", "") or "unnamed"

def jitter_sleep(base):
    time.sleep(base + random.uniform(0, base * 0.4))

# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------
class HttpTransport(object):
    name = "http"

    def call(self, path, method="GET", body=None, timeout=30):
        url = path if path.startswith("http") else BASE + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, /",
            "Accept-Language": "en-AU,en;q=0.9",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:4000]
        except Exception as e:
            return -1, "%s: %s" % (type(e).__name__, str(e)[:200])

    def close(self):
        pass

class BrowserTransport(object):
    name = "browser"

    def __init__(self, headless=False, reload_every=25):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"])
        self._ctx = self._browser.new_context(
            user_agent=UA, locale="en-AU", viewport={"width": 1400, "height": 950})
        self._page = self._ctx.new_page()
        self._page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(3000)
        self._calls = 0
        self._reload_every = reload_every

    def _reload(self):
        try:
            self._page.goto(LANDING, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(1500)
        except Exception:
            pass

    def call(self, path, method="GET", body=None, timeout=45):
        js = """async (a) => {
            const [method, url, body, ms] = a;
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), ms);
            try {
                const opts = {method: method,
                              headers: {'Accept': 'application/json',
                                        'Content-Type': 'application/json'},
                              signal: ctl.signal};
                if (body !== null) opts.body = JSON.stringify(body);
                const r = await fetch(url, opts);
                const txt = await r.text();
                clearTimeout(t);
                return {status: r.status, text: txt};
            } catch (e) {
                clearTimeout(t);
                return {status: -1, text: String(e)};
            }
        }"""
        if self._calls and self._calls % self._reload_every == 0:
            self._reload()
        self._calls += 1
        try:
            res = self._page.evaluate(js, [method, path, body, timeout * 1000])
        except Exception:
            self._reload()
            res = self._page.evaluate(js, [method, path, body, timeout * 1000])
        return res.get("status", -1), res.get("text", "") or ""

    def close(self):
        try:
            self._browser.close()
        finally:
            try:
                self._pw.stop()
            except Exception:
                pass

class Api(object):
    def __init__(self, transport, delay=0.4, verbose=True):
        self.t = transport
        self.delay = delay
        self.verbose = verbose
        self.consecutive_failures = 0

    def raw(self, path, method="GET", body=None):
        return self.t.call(path, method, body)

    def call(self, path, method="GET", body=None, attempts=4):
        last = None
        for i in range(attempts):
            status, text = self.t.call(path, method, body)
            if status == 200 and text:
                try:
                    out = json.loads(text)
                except ValueError:
                    last = BlockedResponse(path, status, text)
                    break
                self.consecutive_failures = 0
                jitter_sleep(self.delay)
                return out
            if status in (401, 403):
                last = BlockedResponse(path, status, text)
                break
            if status == -1:
                last = RuntimeError("request failed: %s" % text[:120])
            else:
                last = RuntimeError("HTTP %s: %s" % (status, text[:160]))
            wait = 2 ** (i + 1)
            if self.verbose:
                log("    retry %d/%d in %ss (%s) %s"
                    % (i + 1, attempts, wait, status, path.split("?")[0][:70]))
            time.sleep(wait + random.uniform(0, 1))
        self.consecutive_failures += 1
        raise last if last else RuntimeError("call failed: %s" % path)

def pick_transport(mode, headless, delay):
    if mode == "browser":
        log("[transport] headed Chromium (Playwright)")
        return BrowserTransport(headless=headless)
    probe = HttpTransport()
    status, text = probe.call("/apis/ui/settings")
    ok = False
    try:
        ok = status == 200 and isinstance(json.loads(text), (list, dict))
    except Exception:
        ok = False
    if mode == "http":
        if not ok:
            raise BlockedResponse("/apis/ui/settings", status, text)
        log("[transport] direct HTTP")
        return probe
    if ok:
        log("[transport] direct HTTP works from this IP")
        return probe
    log("[transport] direct HTTP blocked (HTTP %s) -> headed Chromium fallback" % status)
    return BrowserTransport(headless=headless)

# ---------------------------------------------------------------------------
# response helpers
# ---------------------------------------------------------------------------
def products_from(obj):
    out = []
    if isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict) and it.get("Stockcode"):
                out.append(it)
        return out
    for key in ("Bundles", "Products", "products", "Items"):
        v = obj.get(key)
        if not isinstance(v, list):
            continue
        for it in v:
            if not isinstance(it, dict):
                continue
            if it.get("Stockcode"):
                out.append(it)
                continue
            for inner_key in ("Products", "Items"):
                inner = it.get(inner_key)
                if isinstance(inner, list):
                    for p in inner:
                        if isinstance(p, dict) and p.get("Stockcode"):
                            out.append(p)
    return out

def trim_product(p, extra=None):
    row = {
        "stockcode": p.get("Stockcode"),
        "barcode": p.get("Barcode"),
        "name": p.get("Name"),
        "displayName": p.get("DisplayName"),
        "brand": p.get("Brand"),
        "price": p.get("Price"),
        "wasPrice": p.get("WasPrice"),
        "savingsAmount": p.get("SavingsAmount"),
        "instorePrice": p.get("InstorePrice"),
        "instoreWasPrice": p.get("InstoreWasPrice"),
        "cupString": p.get("CupString"),
        "isOnSpecial": p.get("IsOnSpecial"),
        "isHalfPrice": p.get("IsHalfPrice"),
        "isOnlineOnly": p.get("IsOnlineOnly"),
        "isAvailable": p.get("IsAvailable"),
        "isInStock": p.get("IsInStock"),
        "packageSize": p.get("PackageSize"),
        "urlFriendlyName": p.get("UrlFriendlyName"),
        "source": p.get("Source"),
        "image": p.get("MediumImageFile") or p.get("SmallImageFile"),
    }
    if extra:
        row.update(extra)
    return row

def category_tree(api):
    j = api.call("/apis/ui/PiesCategoriesWithSpecials")
    return j.get("Categories", j if isinstance(j, list) else [])

def walk_tree(nodes, path=(), acc=None):
    acc = [] if acc is None else acc
    for n in nodes:
        if not isinstance(n, dict):
            continue
        seg = n.get("UrlFriendlyName")
        new_path = tuple(list(path) + ([seg] if seg else []))
        acc.append(("/shop/browse/" + "/".join(new_path), n.get("NodeId"),
                    n.get("Description"), len(new_path), n))
        walk_tree(n.get("Children") or [], new_path, acc)
    return acc

def browse(api, cat_url, cat_id, name, page, is_special=False, store_id=None):
    body = {
        "categoryId": cat_id,
        "pageNumber": page,
        "pageSize": PAGE_SIZE,
        "sortType": "TraderRelevance",
        "url": cat_url,
        "location": cat_url,
        "formatObject": json.dumps({"name": name or ""}),
        "isSpecial": bool(is_special),
        "isHideUnavailableProducts": False,
        "isBundle": False,
        "isMobile": False,
        "isRegisteredRewardCardPromotion": False,
        "isHideEverydayMarketProducts": False,
        "filters": [],
        "categoryVersion": "v2",
    }
    if store_id:
        body["storeId"] = str(store_id)
    return api.call("/apis/ui/browse/category", "POST", body)

def paged_browse(api, cat_url, cat_id, name, is_special=False, store_id=None,
                 max_pages=1000, seen=None, on_page=None):
    seen = set() if seen is None else seen
    rows, declared, dup_streak = [], None, 0
    for page in range(1, max_pages + 1):
        j = browse(api, cat_url, cat_id, name, page, is_special, store_id)
        if declared is None:
            try:
                declared = int(j.get("TotalRecordCount") or 0)
            except Exception:
                declared = 0
        items = products_from(j)
        fresh = [p for p in items if str(p.get("Stockcode")) not in seen]
        if not items:
            on_page and on_page(page, 0, len(rows))
            break
        if not fresh:
            dup_streak += 1
            if dup_streak >= 2:
                on_page and on_page(page, 0, len(rows))
                break
        else:
            dup_streak = 0
        for p in fresh:
            seen.add(str(p.get("Stockcode")))
        rows.extend(fresh)
        on_page and on_page(page, len(fresh), len(rows))
        if declared and len(rows) >= declared:
            break
    return rows, declared

# ---------------------------------------------------------------------------
# run-state helper (Only outputs JSON per item in 'specials/' folder)
# ---------------------------------------------------------------------------
class RunState(object):
    def __init__(self, mode="specials", resume=False):
        self.dir = os.path.join(OUT, "specials")
        os.makedirs(self.dir, exist_ok=True)
        self.mode = mode
        self.resume = resume

    def done(self, key):
        # Checks if individual item file already exists when resume is enabled
        filename = safe_name(key) + ".json"
        return bool(self.resume and os.path.exists(os.path.join(self.dir, filename)))

    def item_file(self, key):
        return os.path.join(self.dir, safe_name(key) + ".json")

    def write_item(self, key, payload):
        atomic_write(self.item_file(key),
                     json.dumps(payload, ensure_ascii=False, indent=1))

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def specials_nodes(tree):
    out, seen = [], set()
    for url, nid, name, depth, node in walk_tree(tree):
        segs = [s for s in url.split("/") if s]
        if segs[:2] != ["shop", "browse"]:
            continue
        rest = segs[2:]
        keep = False
        if rest and rest[0] == "specials" and len(rest) == 2:
            keep = True
        elif rest and rest[-1].endswith("-specials"):
            keep = True
        if keep and url not in seen:
            seen.add(url)
            out.append((url, nid, name))
    return out

def cmd_specials(api, state, limit_cats=0):
    tree = category_tree(api)
    cats = specials_nodes(tree)
    if limit_cats:
        cats = cats[:limit_cats]
    log("[specials] %d specials categories" % len(cats))
    total = 0
    for idx, (url, nid, name) in enumerate(cats, 1):
        if state.done(name or nid):
            log("[%d/%d] %s: skipped (done)" % (idx, len(cats), name))
            continue
        t0 = time.time()
        try:
            rows, declared = paged_browse(api, url, nid, name, is_special=False)
        except BlockedResponse as e:
            if "AuthenticationException" in (e.body_head or ""):
                log("[%d/%d] %s: login-gated, skipping" % (idx, len(cats), name))
                continue
            log("[%d/%d] %s: BLOCKED %s" % (idx, len(cats), name, e))
            if api.consecutive_failures >= 3:
                log("  aborting: 3 consecutive failures")
                return 1
            continue
        rows = [trim_product(p, {"categoryUrl": url, "categoryName": name}) for p in rows]
        
        # Saves ONLY into output_dir/specials/<category_name>.json
        state.write_item(name or nid, {"url": url, "categoryId": nid, "name": name,
                                       "declared": declared, "count": len(rows),
                                       "products": rows})
        
        secs = int(time.time() - t0)
        total += len(rows)
        log("[%d/%d] %-40s %4d/%s products (%ds)"
            % (idx, len(cats), name[:40], len(rows), declared or "?", secs))
    log("\n[specials] Done! Data saved into -> %s" % state.dir)
    return 0

# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Woolworths AU Specials Scraper")
    ap.add_argument("--transport", choices=["auto", "http", "browser"], default="auto")
    ap.add_argument("--headless", action="store_true",
                    help="only useful on a clean/AU IP; Akamai blocks headless")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--limit", type=int, default=0, help="limit categories (0 for all)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    global OUT
    if a.out:
        OUT = a.out

    transport = pick_transport(a.transport, a.headless, a.delay)
    api = Api(transport, delay=a.delay)
    state = RunState("specials", a.resume)
    try:
        return cmd_specials(api, state, a.limit)
    except BlockedResponse as e:
        log("\nBLOCKED: %s" % e)
        return 2
    except KeyboardInterrupt:
        log("\ninterrupted - re-run with --resume")
        return 130
    finally:
        transport.close()

if __name__ == "__main__":
    sys.exit(main())