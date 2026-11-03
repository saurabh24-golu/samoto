import argparse
import html
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://www.aldi.com.au"
SITEMAP_CATEGORIES = BASE + "/sitemap_categories.xml"
SITEMAP_STORES = BASE + "/sitemap_stores.xml"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PAGE_SIZE = 30  # verified: 30 product tiles per category page
MAX_PAGES = 500  # hard safety cap per category
REQUEST_TIMEOUT = 40
RETRIES = 4
BACKOFF_BASE = 1.5

# Pure-duplicate aggregate categories: their pages repeat products already
# carried by the real category tree. Skipping keeps runtime sane.
SKIP_CATEGORIES = {
    "1588161435960214",  # the-people-s-picks — whole-range marketing rollup
}

# ---- HTTP Layer -----------------------------------------------------------


def fetch(url, timeout=REQUEST_TIMEOUT, retries=RETRIES):
    """GET url -> decoded text."""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            time.sleep(BACKOFF_BASE**attempt + 0.3 * attempt)
    raise RuntimeError(
        "GET %s failed after %d tries: %s" % (url, retries, last_err)
    )


def fetch_xml(url):
    text = fetch(url)
    return re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text)


# ---- Parsing Helpers ------------------------------------------------------

TILE_START = re.compile(
    r'<div id="product-tile-(\d{9,20})" class="product-teaser-item'
)
HREF_RE = re.compile(r'href="(/product/[^"]+)"')
IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>')
TITLE_RE = re.compile(r'class="product-tile"[^>]*title="([^"]*)"')
TILE_TYPE_RE = re.compile(r'data-tile-type="([^"]*)"')
DATA_TEST_RE = re.compile(
    r'data-test="product-tile__([a-z-]+)"[^>]*>(.*?)</(?:p|div)>', re.S
)
PRICE_REGION_RE = re.compile(r'data-test="product-tile__price".*?</a>', re.S)
REGULAR_SPAN_RE = re.compile(
    r"base-price__regular[^>]*>\s*<span>\s*\$(\d+(?:\.\d{1,2})?)\s*</span>"
)
PRICE_NUM_RE = re.compile(r"\$(\d+(?:\.\d{1,2})?)")
REAL_TILE_HINT = re.compile(r'data-test="product-tile__name"')


def strip_tags(seg):
    seg = re.sub(r"<[^>]+>", " ", seg)
    return html.unescape(re.sub(r"\s+", " ", seg)).strip()


def parse_tile(chunk, article_no):
    """Parse one product tile chunk into a structured dict."""
    m = TILE_TYPE_RE.search(chunk)
    tile_type = html.unescape(m.group(1)) if m else None
    if not REAL_TILE_HINT.search(chunk):
        return None  # Filter non-purchasable product tile
    href_m = HREF_RE.search(chunk)
    img_m = IMG_RE.search(chunk)
    title_m = TITLE_RE.search(chunk)

    fields = {}
    for dm in DATA_TEST_RE.finditer(chunk):
        key, body = dm.group(1), dm.group(2)
        fields.setdefault(key, strip_tags(body))

    price_cents = None
    price_text = None
    pm = PRICE_REGION_RE.search(chunk)
    price_source = pm.group(0) if pm else chunk
    sm = REGULAR_SPAN_RE.search(price_source)
    if sm:
        price_text = "$" + sm.group(1)
        price_cents = int(round(float(sm.group(1)) * 100))
    else:
        nums = list(PRICE_NUM_RE.finditer(price_source))
        if nums:
            nm = nums[-1]
            price_text = "$" + nm.group(1)
            price_cents = int(round(float(nm.group(1)) * 100))

    return {
        "articleNo": article_no,
        "name": fields.get("name")
        or (html.unescape(title_m.group(1)) if title_m else None),
        "brand": fields.get("brandname"),
        "size": fields.get("unit-of-measurement"),
        "priceCents": price_cents,
        "price": price_text,
        "comparisonPrice": fields.get("comparison-price"),
        "imageUrl": img_m.group(1) if img_m else None,
        "productUrl": (BASE + href_m.group(1)) if href_m else None,
        "tileType": tile_type,
    }


def parse_category_page(page_html):
    """Return product records found on one category page."""
    out = []
    idx = 0
    while True:
        m = TILE_START.search(page_html, idx)
        if not m:
            break
        start = m.start()
        end_m = re.search(r"add-to-shopping-list-button", page_html[m.end() :])
        end = (
            m.end() + end_m.end()
            if end_m
            else min(start + 12000, len(page_html))
        )
        chunk = page_html[start:end]
        rec = parse_tile(chunk, m.group(1))
        if rec and rec.get("articleNo"):
            out.append(rec)
        idx = end
    return out


# ---- Crawl Orchestration --------------------------------------------------


def category_parts(url):
    """Split a category URL into (url, path, categoryId, displayName)."""
    m = re.search(r"/products/(.*?)/k/(\d+)/?$", url)
    if not m:
        return None
    path = m.group(1)
    return url, path, m.group(2), path.split("/")[-1].replace("-", " ").title()


class Scraper:

    def __init__(self, out_dir, workers, delay, max_pages, refresh):
        self.out_dir = out_dir
        self.workers = workers
        self.delay = delay
        self.max_pages = max_pages
        self.refresh = refresh
        self.progress_path = os.path.join(out_dir, "progress.json")
        os.makedirs(out_dir, exist_ok=True)
        self.progress = self._load_progress()

    def _load_progress(self):
        if self.refresh:
            return {}
        try:
            with open(self.progress_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_progress(self):
        tmp = self.progress_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.progress, f, indent=1)
        os.replace(tmp, self.progress_path)

    def fetch_categories(self):
        urls = fetch_xml(SITEMAP_CATEGORIES)
        cats = []
        for u in urls:
            parts = category_parts(u)
            if parts:
                _, path, cid, name = parts
                cats.append({"id": cid, "name": name, "path": path, "url": u})
        cats.sort(key=lambda c: c["path"])
        self._write_json("categories.json", cats)
        self._write_json(
            "index.json",
            {
                "mode": "categories",
                "fetchedAtUtc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "categoryCount": len(cats),
            },
        )
        print(
            "[categories] %d category URLs written to categories.json"
            % len(cats)
        )
        return cats

    def fetch_stores(self):
        urls = fetch_xml(SITEMAP_STORES)
        stores = []
        for u in urls:
            m = re.search(r"/storelocator/l/([a-z0-9-]+)/?$", u)
            if m:
                stores.append({"slug": m.group(1), "url": u})
        stores.sort(key=lambda s: s["slug"])
        self._write_json("stores.json", stores)
        self._write_json(
            "index.json",
            {
                "mode": "stores",
                "fetchedAtUtc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "storeCount": len(stores),
            },
        )
        print("[stores] %d store slugs written to stores.json" % len(stores))
        return stores

    def crawl_category(self, cat):
        url = cat["url"]
        if self.progress.get(url) == "done" and not self.refresh:
            return cat["id"], None, True
        products = []
        page = 1
        last_had = True
        pages_done = 0
        while last_had and page <= self.max_pages:
            page_url = url + ("?page=%d" % page if page > 1 else "")
            try:
                html_text = fetch(page_url)
            except Exception as e:
                print(
                    "[%s] page %d FAILED (%s) — marking partial"
                    % (cat["id"], page, e)
                )
                break
            tiles = parse_category_page(html_text)
            if not tiles:
                last_had = False
            else:
                products.extend(tiles)
                pages_done = page
                page += 1
                if self.delay:
                    time.sleep(self.delay)
        truncated = page > self.max_pages and last_had
        self.progress[url] = "done"
        self._save_progress()
        return (
            cat["id"],
            {
                "products": products,
                "pages": pages_done,
                "truncated": truncated,
            },
            False,
        )

    def crawl_products(self, cat_ids=None, limit=None):
        cats = self.fetch_categories()
        if cat_ids:
            keep = set(str(c) for c in cat_ids)
            cats = [c for c in cats if c["id"] in keep]
        else:
            cats = [c for c in cats if c["id"] not in SKIP_CATEGORIES]
        if limit:
            cats = cats[:limit]

        by_id = {}
        results = {}
        skipped = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            futs = {ex.submit(self.crawl_category, c): c for c in cats}
            done_n = 0
            for fut in as_completed(futs):
                cid, res, was_skip = fut.result()
                done_n += 1
                if was_skip:
                    skipped += 1
                else:
                    results[cid] = res
                if done_n % 10 == 0 or done_n == len(cats):
                    print(
                        "[products] %d/%d categories done (%ds)"
                        % (done_n, len(cats), int(time.time() - t0))
                    )

        # Merge into structured SKU records
        for cid, res in results.items():
            cat = next((c for c in cats if c["id"] == cid), None)
            for p in res["products"]:
                rec = by_id.get(p["articleNo"])
                if rec is None:
                    p["categories"] = [
                        {
                            "id": cid,
                            "name": cat["name"] if cat else cid,
                            "path": cat["path"] if cat else cid,
                        }
                    ]
                    p["fetchedAt"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                    )
                    p["currency"] = "AUD"
                    by_id[p["articleNo"]] = p
                else:
                    alias = {
                        "id": cid,
                        "name": cat["name"] if cat else cid,
                        "path": cat["path"] if cat else cid,
                    }
                    if alias not in rec["categories"]:
                        rec["categories"].append(alias)

        products = list(by_id.values())
        products.sort(key=lambda p: p["articleNo"])
        
        # Write JSON output
        self._write_json("products.json", products)

        n_raw = sum(
            r["products"] and len(r["products"]) or 0 for r in results.values()
        )
        self._write_json(
            "index.json",
            {
                "mode": "products",
                "fetchedAtUtc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "categoriesCrawled": len(results),
                "categoriesSkippedResume": skipped,
                "rawTiles": n_raw,
                "uniqueProducts": len(products),
                "duplicateTiles": n_raw - len(products),
                "perCategoryPages": {k: v["pages"] for k, v in results.items()},
                "truncatedCategories": [
                    k for k, v in results.items() if v["truncated"]
                ],
                "durationSeconds": int(time.time() - t0),
            },
        )
        print(
            "[products] %d unique products saved to products.json (%ds)"
            % (len(products), int(time.time() - t0))
        )
        return products

    def _write_json(self, name, data):
        path = os.path.join(self.out_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--mode",
        default="products",
        choices=["products", "categories", "stores"],
    )
    ap.add_argument(
        "--out",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "aldi_data"
        ),
    )
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="seconds to sleep between page fetches",
    )
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only crawl first N categories (testing)",
    )
    ap.add_argument(
        "--categories",
        default=None,
        help="comma-separated category ids to crawl (testing)",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="ignore progress.json, re-crawl everything",
    )
    args = ap.parse_args()

    s = Scraper(
        args.out, args.workers, args.delay, args.max_pages, args.refresh
    )
    if args.mode == "categories":
        s.fetch_categories()
    elif args.mode == "stores":
        s.fetch_stores()
    else:
        cat_ids = (
            [c.strip() for c in args.categories.split(",")]
            if args.categories
            else None
        )
        s.crawl_products(cat_ids=cat_ids, limit=args.limit)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrupted — progress.json saved; re-run to resume.",
            file=sys.stderr,
        )
        sys.exit(130)