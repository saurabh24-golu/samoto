# # #!/usr/bin/env python3
# # """
# # Drakes AU (drakes.com.au) weekly catalogues -> per-store JSON, extracted with the
# # DeepSeek vision API.

# #     download  : every catalogue PDF linked on drakes.com.au/catalogue/
# #     render    : PDF pages -> JPG (+ the free text layer dumped next to them)
# #     extract   : DeepSeek vision reads each page image -> structured products
# #     verify    : assert the output holds together, write ONE JSON PER STORE

# # WHY THERE IS NO "SCRAPE THE PDF LINK" STEP
# # ------------------------------------------
# # drakes.com.au/catalogue/ has no <a href="....pdf">. Each store tile points at a
# # FlowPaper flipbook (https://<hash>.flowpaper.com/<PubName>/), and the flipbook's
# # inline viewer config is what names the PDF:

# #     PDFFile:"docs/Week-40-SA-WEB.pdf?reload=..."

# # so the real file lives at  https://<hash>.flowpaper.com/<PubName>/docs/<name>.pdf
# # Missing that indirection is the whole difficulty; after it the PDF is a plain
# # 20 MB / 24-page download with no auth, no cookies, no bot wall.

# # GOOD NEWS, CHECKED LIVE: these PDFs HAVE a text layer (~1.2K chars/page), so
# # --backend text can structure them with no vision and no image cost. Vision is
# # still the default because the text layer loses the visual price<->product
# # pairing (a rendered page reads "$6.90 ... 90 kg" as separate lines), which is
# # exactly what a flyer is made of.

# # USAGE
# # -----
# #   # 0. no-arg cheat sheet
# #   python drakes_au_catalogue.py

# #   # 1. what is live right now (store, validity, pub, pdf)
# #   python drakes_au_catalogue.py --list

# #   # 2. is my machine + key able to do this at all? (run this first)
# #   python drakes_au_catalogue.py --diagnose

# #   # 3. the whole thing: 6 stores, all pages, DeepSeek vision
# #   python drakes_au_catalogue.py --run --backend deepseek --api-key sk-xxxx
# #   #    (or export DEEPSEEK_API_KEY and drop --api-key)

# #   # 4. one store, 2 pages, cheap smoke test before you spend anything
# #   python drakes_au_catalogue.py --run --stores hackham --pages 1-2 \
# #          --backend deepseek --api-key sk-xxxx

# #   # 5. download + render only, no API spend (resume later, nothing is redone)
# #   python drakes_au_catalogue.py --run --stage download
# #   python drakes_au_catalogue.py --run --stage render

# #   # 6. re-extract failed pages after a network blip (cached pages are skipped)
# #   python drakes_au_catalogue.py --run --backend deepseek --api-key sk-xxxx

# #   # 7. no API at all: structure the PDF text layer with the text model
# #   python drakes_au_catalogue.py --run --backend text --api-key sk-xxxx

# #   # 8. integrity report on what you just produced
# #   python verify_output.py --out ./drakes

# # Everything is resumable: an interrupted run continues where it stopped, PDFs
# # over 100 KB, page images and per-page JSON are all skipped when already present.

# # OUTPUT LAYOUT
# # -------------
# #   drakes/
# #     catalogues.json            discovery cache (store, validity, pub, pdf url)
# #     pdfs/<store-slug>.pdf      the downloaded catalogue, one per store
# #     pages/<store-slug>/pNN.jpg rendered page images
# #     text/<store-slug>/pNN.txt  free PDF text layer for that page
# #     pages_json/<store-slug>/pNN.json   per-page extraction (the resume unit)
# #     json/<store-slug>.json     >>> PER-STORE DELIVERABLE (all pages merged)
# #     json/<store-slug>.jsonl    same rows, one JSON object per line
# #     summary.json               run summary + problems/warnings
# #     run.log                    full log

# # BACKENDS
# # --------
# #   deepseek  (default) DeepSeek vision. Model MUST be deepseek-flash: it is the
# #             only DeepSeek model with image input (deepseek-v4-pro is text-only
# #             and answers "I can't view the image" -> 0 products every page).
# #             reasoning_effort defaults to "low" here: measured 13s/page vs 51s at
# #             "high" on the same page, same 11 products. Raise it with
# #             --reasoning-effort high if you want max care on a dense page.
# #   text      Text-layer only, no image sent. Cheapest, works with a text-only
# #             model, but price<->product pairing is less reliable.
# #   none      No extraction. Download + render only (same as --stage download/render).

# # Requires: pip install pymupdf      (everything else is the stdlib)
# # Tested on macOS and Windows (cmd.exe); paths use os.path.join throughout.
# # """
# # from __future__ import annotations

# # import argparse
# # import base64
# # import glob
# # import html
# # import json
# # import os
# # import queue
# # import random
# # import re
# # import sys
# # import threading
# # import time
# # import urllib.error
# # import urllib.parse
# # import urllib.request

# # # ---------------------------------------------------------------------------
# # # config
# # # ---------------------------------------------------------------------------
# # UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
# #       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# # CATALOGUE_PAGE = "https://drakes.com.au/catalogue/"

# # DEEPSEEK_BASE = "https://api.deepseek.com/v1"
# # DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"     # the ONLY DeepSeek model with vision
# # DEEPSEEK_NO_VISION = ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner",
# #                       "deepseek-reasoner-v4", "deepseek-v3")
# # DEEPSEEK_MIN_TOKENS = 8000                   # reasoning model eats the budget first
# # DEEPSEEK_DEFAULT_TIMEOUT = 300

# # ROW_KEYS = ["name", "brand", "size", "price", "unit_price", "was_price", "save",
# #             "category", "confidence"]
# # NUM_KEYS = ("price", "was_price", "confidence")
# # CATEGORIES = ("pantry", "dairy", "meat", "seafood", "deli", "produce", "bakery",
# #               "frozen", "drinks", "liquor", "household", "health", "baby", "pet",
# #               "general_merchandise", "other")
# # PAGE_TYPES = ("cover", "product_grid", "editorial", "promo", "index", "back_cover",
# #               "competition", "liquor", "general_merchandise")

# # ROW_SCHEMA = {k: (0.0 if k in NUM_KEYS else "") for k in ROW_KEYS}
# # PAGE_SCHEMA = {
# #     "page_type": "cover|product_grid|editorial|promo|index|back_cover|competition|general_merchandise|liquor",
# #     "validity": "date range exactly as printed on THIS page, else empty",
# #     "promo_headline": "main promo headline printed on the page, else empty",
# #     "products": [ROW_SCHEMA],
# #     "unreadable": ["offers visible but too small/blurry to trust"],
# #     "notes": "",
# # }

# # PROMPT = (
# #     "Extract EVERY product offer from this Australian supermarket (Drakes "
# #     "Supermarkets) catalogue page.\n"
# #     "Return ONLY raw JSON. No markdown fences, no commentary.\n"
# #     "Schema:\n" + json.dumps(PAGE_SCHEMA) + "\n"
# #     "Rules:\n"
# #     "- One array entry per distinct advertised offer. If one offer covers several\n"
# #     "  varieties, put every variety in the name exactly as printed.\n"
# #     "- price = the advertised shelf price in AUD as a number only (6.90, not\n"
# #     "  \"$6.90\"). If the offer is priced by weight put the number in price and\n"
# #     "  'per kg' in size. Use 0 only if it truly cannot be read.\n"
# #     "- unit_price = the small unit price as printed (\"$2.25 per 100g\"), else \"\".\n"
# #     "- was_price: fill ONLY if a was/previous price is actually printed on the\n"
# #     "  page. Flyers usually print SAVE $X, 1/2 PRICE or 2 FOR $Y instead - do NOT\n"
# #     "  reconstruct a was price from those, leave 0.\n"
# #     "- save = the SAVE / half-price claim copied verbatim (\"SAVE $4\", \"1/2 PRICE\"),\n"
# #     "  else \"\".\n"
# #     "- category = one of: " + ", ".join(CATEGORIES) + "\n"
# #     "- Never invent products, brands, pack sizes or prices. Anything too small or\n"
# #     "  blurry to read confidently goes in unreadable and its confidence drops below 0.5.\n"
# #     "- confidence = your own 0..1 confidence that price+name of this row are correct.\n"
# #     "- validity: copy a date range ONLY if this page prints one.\n"
# # )


# # # ---------------------------------------------------------------------------
# # # small helpers
# # # ---------------------------------------------------------------------------
# # _print_lock = threading.Lock()


# # class BlockedResponse(RuntimeError):
# #     """Body was not JSON / not a PDF: bot wall, intercepting proxy, bad egress.

# #     Deliberately distinct from a JSONDecodeError so a blocked network never gets
# #     diagnosed as a schema change three sessions later.
# #     """


# # class CredentialError(RuntimeError):
# #     """401/403 - never retried, the key or the account is the problem."""


# # def log(msg, logfile=None):
# #     line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
# #     with _print_lock:
# #         sys.stderr.write(line + "\n")
# #         sys.stderr.flush()
# #         if logfile:
# #             try:
# #                 with open(logfile, "a", encoding="utf-8") as f:
# #                     f.write(line + "\n")
# #             except OSError:
# #                 pass


# # def slugify(s):
# #     s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
# #     return s or "unnamed"


# # def atomic_write_json(path, obj):
# #     tmp = path + ".tmp"
# #     with open(tmp, "w", encoding="utf-8") as f:
# #         json.dump(obj, f, ensure_ascii=False, indent=1)
# #     os.replace(tmp, path)


# # def load_key(name, explicit=None):
# #     """explicit arg > environment > a .env near cwd / script / home."""
# #     if explicit:
# #         return explicit
# #     if os.environ.get(name):
# #         return os.environ[name]
# #     here = os.path.dirname(os.path.abspath(__file__))
# #     for p in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env"),
# #               os.path.expanduser("~/.hermes/.env"),
# #               os.path.expanduser("~/.deepseek.env")):
# #         if os.path.exists(p):
# #             try:
# #                 with open(p, encoding="utf-8", errors="replace") as f:
# #                     for line in f:
# #                         line = line.strip()
# #                         if line.startswith(name + "="):
# #                             return line.split("=", 1)[1].strip().strip('"').strip("'")
# #             except OSError:
# #                 continue
# #     return None


# # def http(url, data=None, headers=None, timeout=120, tries=3, label="", logfile=None):
# #     """GET/POST with a retry policy that separates transient from permanent.

# #     transient (429/5xx/timeouts) -> backoff + jitter, up to `tries`
# #     permanent (400/401/403/404/422) -> raise now, retrying identical bytes is
# #     pointless and burns minutes per item
# #     """
# #     body = json.dumps(data).encode() if data is not None else None
# #     last = None
# #     for attempt in range(tries):
# #         try:
# #             req = urllib.request.Request(url, data=body, headers=headers or {})
# #             with urllib.request.urlopen(req, timeout=timeout) as r:
# #                 return r.read()
# #         except urllib.error.HTTPError as e:
# #             detail = e.read().decode("utf-8", "replace")[:300]
# #             last = "HTTP {}: {}".format(e.code, detail)
# #             if e.code in (401, 403):
# #                 raise CredentialError("{} -> HTTP {} {}".format(label or url, e.code, detail))
# #             if e.code in (400, 404, 422):
# #                 raise RuntimeError("{} -> HTTP {} {}".format(label or url, e.code, detail))
# #             if e.code == 429:
# #                 time.sleep(min(60, 5 * (attempt + 1) + random.random() * 3))
# #                 continue
# #             time.sleep(2 * (attempt + 1))
# #         except Exception as e:                                        # noqa: BLE001
# #             last = "{}: {}".format(type(e).__name__, e)
# #             time.sleep(2 * (attempt + 1) + random.random())
# #     raise RuntimeError("{} failed after {} tries -> {}".format(label or url, tries, last))


# # # ---------------------------------------------------------------------------
# # # stage 1: discovery
# # # ---------------------------------------------------------------------------
# # # The catalogue tiles live in a JSON blob inside an HTML attribute, so the doc is
# # # HTML-unescaped first and then JSON-unescaped (\\/ -> /). Order is
# # # label -> link.url -> title -> description.
# # TILE_RE = re.compile(
# #     r'"label"\s*:\s*"([^"]{2,80})"'          # "Specials available in South Australia"
# #     r'.{0,600}?"link"\s*:\s*\{[^{}]{0,200}?"url"\s*:\s*"(https://([a-z0-9]+)\.flowpaper\.com/([^"/]+)/[^"]*)"'
# #     r'.{0,200}?"title"\s*:\s*"([^"]{1,60})"'  # "South Australia"
# #     r'.{0,200}?"description"\s*:\s*"([^"]{0,160})"',   # the on-sale date range
# #     re.S)


# # def discover(logfile=None):
# #     """[{store, label, state, sub, pub, url, validity, flowpaper_url}, ...]

# #     Never hardcode the publication names: they change every week (Week40SAWEB
# #     next week is Week41SAWEB) and there is no stable id anywhere.
# #     """
# #     raw = http(CATALOGUE_PAGE, headers={"User-Agent": UA}, label="drakes catalogue page",
# #                logfile=logfile).decode("utf-8", "replace")
# #     doc = html.unescape(raw).replace("\\/", "/")
# #     out, seen = [], set()
# #     for label, fp_url, sub, pub, title, desc in TILE_RE.findall(doc):
# #         if pub in seen:
# #             continue
# #         seen.add(pub)
# #         low = (title + " " + label).lower()
# #         out.append({
# #             "store": html.unescape(title).strip(),
# #             "slug": slugify(html.unescape(title)),
# #             "label": html.unescape(label).strip(),
# #             "state": "QLD" if "queensland" in low else "SA",
# #             "sub": sub,
# #             "pub": pub,
# #             "flowpaper_url": fp_url,
# #             "validity": html.unescape(desc).strip(),
# #         })
# #     return out


# # def flowpaper_config(entry, logfile=None):
# #     """The flipbook page names the PDF. That indirection is the whole trick."""
# #     page = http(entry["flowpaper_url"], headers={"User-Agent": UA},
# #                 label="flowpaper " + entry["pub"], logfile=logfile).decode("utf-8", "replace")

# #     def grab(name):
# #         m = re.search(name + r'\s*:\s*"([^"]*)"', page)
# #         return m.group(1) if m else ""

# #     pdf = grab("PDFFile").split("?")[0]          # strip ?reload=<ts>
# #     img_template = grab("HighResIMGFiles") or grab("IMGFiles")
# #     if not pdf and not img_template:
# #         raise RuntimeError("flowpaper {} exposes neither PDFFile nor IMGFiles "
# #                            "(dead publication, or the viewer markup changed)"
# #                            .format(entry["pub"]))
# #     t = re.search(r"<title>([^<]+)</title>", page)
# #     entry["pdf_path"] = pdf
# #     entry["pdf_url"] = (urllib.parse.urljoin(entry["flowpaper_url"], pdf)
# #                         if pdf else "")
# #     entry["img_template"] = img_template              # fallback when there is no PDF
# #     entry["pub_title"] = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
# #     return entry


# # # ---------------------------------------------------------------------------
# # # stage 2: download
# # # ---------------------------------------------------------------------------
# # def download_pdf(entry, dest, logfile=None, force=False):
# #     """Resumable: >100 KB and starting with %PDF is considered complete."""
# #     if (not force and os.path.exists(dest) and os.path.getsize(dest) > 100_000):
# #         with open(dest, "rb") as f:
# #             if f.read(5).startswith(b"%PDF"):
# #                 return os.path.getsize(dest), "cached"
# #     if not entry.get("pdf_url"):
# #         raise RuntimeError("no pdf url for {}".format(entry["store"]))
# #     part = dest + ".part"
# #     offset = os.path.getsize(part) if os.path.exists(part) else 0
# #     headers = {"User-Agent": UA, "Referer": entry["flowpaper_url"]}
# #     if offset:
# #         headers["Range"] = "bytes={}-".format(offset)
# #     try:
# #         req = urllib.request.Request(entry["pdf_url"], headers=headers)
# #         with urllib.request.urlopen(req, timeout=180) as r:
# #             if offset and r.status != 206:          # server ignored the Range
# #                 offset = 0
# #             mode = "ab" if offset else "wb"
# #             got = 0
# #             with open(part, mode) as f:
# #                 while True:
# #                     chunk = r.read(262144)
# #                     if not chunk:
# #                         break
# #                     f.write(chunk)
# #                     got += len(chunk)
# #     except urllib.error.HTTPError as e:
# #         raise RuntimeError("pdf download HTTP {}: {}".format(
# #             e.code, e.read().decode("utf-8", "replace")[:200]))
# #     with open(part, "rb") as f:
# #         magic = f.read(5)
# #     if not magic.startswith(b"%PDF"):
# #         head = open(part, "rb").read(200)
# #         os.remove(part)
# #         raise BlockedResponse(
# #             "{} did not return a PDF (first bytes {!r}) - block page or wrong URL"
# #             .format(entry["pdf_url"], head[:80]))
# #     os.replace(part, dest)
# #     return os.path.getsize(dest), "downloaded {} + {} bytes".format(offset, got)


# # # ---------------------------------------------------------------------------
# # # stage 3: render
# # # ---------------------------------------------------------------------------
# # def render_pdf(pdf_path, pages_dir, text_dir, dpi=200, force=False, logfile=None):
# #     """PDF -> one JPG per page + the text layer next to it (free, no API)."""
# #     try:
# #         import pymupdf                                                # noqa: PLC0415
# #     except ImportError:
# #         raise SystemExit("PDF rendering needs pymupdf:   pip install pymupdf")
# #     os.makedirs(pages_dir, exist_ok=True)
# #     os.makedirs(text_dir, exist_ok=True)
# #     doc = pymupdf.open(pdf_path)
# #     made, text_pages, chars = [], 0, 0
# #     for i, page in enumerate(doc, 1):
# #         img = os.path.join(pages_dir, "p{:02d}.jpg".format(i))
# #         txt = os.path.join(text_dir, "p{:02d}.txt".format(i))
# #         if force or not os.path.exists(img):
# #             pix = page.get_pixmap(dpi=dpi, alpha=False)
# #             pix.save(img, jpg_quality=88)
# #         made.append(img)
# #         if force or not os.path.exists(txt):
# #             body = page.get_text().strip()
# #             with open(txt, "w", encoding="utf-8") as f:
# #                 f.write(body)
# #         try:
# #             with open(txt, encoding="utf-8") as f:
# #                 body = f.read()
# #         except OSError:
# #             body = ""
# #         chars += len(body)
# #         if len(body) > 80:
# #             text_pages += 1
# #     n = doc.page_count
# #     doc.close()
# #     log("    {} pages rendered @{}dpi, text layer on {}/{} pages ({} chars)".format(
# #         n, dpi, text_pages, n, chars), logfile)
# #     return made, n, text_pages


# # # ---------------------------------------------------------------------------
# # # stage 4: backends
# # # ---------------------------------------------------------------------------
# # def resolve_config(a):
# #     """Resolve the lane BEFORE downloading anything.

# #     A missing key should cost zero bandwidth: the first version of this pipeline
# #     shape harvested everything and only then complained it had no key.
# #     """
# #     cfg = {"timeout": a.timeout, "model": a.model, "max_tokens": a.max_tokens,
# #            "base_url": a.base_url, "effort": a.reasoning_effort,
# #            "text_hint": a.text_hint, "backend": a.backend}
# #     if a.backend == "none":
# #         return cfg
# #     # Both API lanes talk to DeepSeek by default. --base-url's argparse default is
# #     # the OpenAI URL, so for this script that value means "unset" -- treating it as
# #     # a real URL sent a DeepSeek key to OpenAI and produced a confusing 401.
# #     cfg["base_url"] = (DEEPSEEK_BASE if a.base_url == "https://api.openai.com/v1"
# #                        else a.base_url)
# #     if a.model == "gpt-4o-mini":
# #         cfg["model"] = DEEPSEEK_DEFAULT_MODEL
# #     if cfg["max_tokens"] < DEEPSEEK_MIN_TOKENS:
# #         print("  raising max tokens {} -> {} (reasoning tokens are billed "
# #               "before the answer)".format(cfg["max_tokens"], DEEPSEEK_MIN_TOKENS))
# #         cfg["max_tokens"] = DEEPSEEK_MIN_TOKENS
# #     if a.backend == "deepseek":
# #         model = cfg["model"]
# #         if any(m in model for m in DEEPSEEK_NO_VISION):
# #             print("  WARNING: {} has NO vision input - it will refuse the image and\n"
# #                   "           every page will come back with 0 products.\n"
# #                   "           use {} instead.".format(model, DEEPSEEK_DEFAULT_MODEL))
# #     key = (a.api_key or os.environ.get("sk-00dfe5f2ff0d45d990244e2e05a3eafd")
# #            or load_key("sk-00dfe5f2ff0d45d990244e2e05a3eafd"))
# #     if not key:
# #         raise SystemExit(
# #             "No DeepSeek API key found.\n"
# #             "  Pass --api-key sk-xxxx, or set the DEEPSEEK_API_KEY environment "
# #             "variable,\n  or put DEEPSEEK_API_KEY=sk-xxxx in a .env next to this script.\n"
# #             "  Keys: https://platform.deepseek.com/api_keys\n"
# #             "  No key at all? --backend none only downloads and renders the PDFs.")
# #     cfg["key"] = key
# #     return cfg


# # def _json_from_text(txt):
# #     """Models wrap JSON in fences, prose, or both. Dig it out."""
# #     s = (txt or "").strip()
# #     s = re.sub(r"^```(?:json)?\s*", "", s)
# #     s = re.sub(r"\s*```$", "", s).strip()
# #     if not s:
# #         raise ValueError("empty model content (did reasoning eat the whole "
# #                          "max_completion_tokens budget? raise --max-tokens)")
# #     try:
# #         return json.loads(s)
# #     except json.JSONDecodeError:
# #         pass
# #     start, end = s.find("{"), s.rfind("}")
# #     if start != -1 and end > start:
# #         return json.loads(s[start:end + 1])
# #     raise ValueError("no JSON object in model output: " + s[:200])


# # def _post_chat(cfg, messages, label, max_tokens=None):
# #     body = {"model": cfg["model"], "messages": messages,
# #             "max_completion_tokens": max_tokens or cfg["max_tokens"]}
# #     if cfg.get("effort"):
# #         # DeepSeek accepts the OpenAI-style key. A bogus value silently falls
# #         # back to "high" (= 4x slower, 4x more reasoning tokens, same answer).
# #         body["reasoning_effort"] = cfg["effort"]
# #     headers = {"Authorization": "Bearer " + cfg["key"],
# #                "Content-Type": "application/json"}
# #     url = cfg["base_url"].rstrip("/") + "/chat/completions"
# #     raw = http(url, data=body, headers=headers, timeout=cfg["timeout"],
# #                tries=4, label=label)
# #     try:
# #         out = json.loads(raw)
# #     except json.JSONDecodeError:
# #         raise BlockedResponse("chat endpoint returned non-JSON: "
# #                               + raw.decode("utf-8", "replace")[:200])
# #     ch = out.get("choices") or [{}]
# #     content = (ch[0].get("message") or {}).get("content", "")
# #     if not content.strip():
# #         raise ValueError("empty content, finish_reason={!r}, usage={}".format(
# #             ch[0].get("finish_reason"), out.get("usage")))
# #     return _json_from_text(content), out.get("usage") or {}


# # def call_vision(img_path, cfg, text_path=None):
# #     with open(img_path, "rb") as f:
# #         b = base64.b64encode(f.read()).decode()
# #     prompt = PROMPT
# #     if cfg.get("text_hint") and text_path and os.path.exists(text_path):
# #         try:
# #             with open(text_path, encoding="utf-8") as f:
# #                 hint = f.read().strip()[:2500]
# #         except OSError:
# #             hint = ""
# #         if hint:
# #             prompt += ("\nOCR HINT for this page (raw text layer, NOT in visual "
# #                        "order - use it to fix the SPELLING of names/brands, never "
# #                        "to pair a price with a product):\n" + hint + "\n")
# #     return _post_chat(cfg, [{"role": "user", "content": [
# #         {"type": "text", "text": prompt},
# #         {"type": "image_url", "image_url": {
# #             "url": "data:image/jpeg;base64," + b, "detail": "high"}}]}],
# #         label=os.path.basename(img_path))


# # def call_text(text_path, cfg):
# #     try:
# #         with open(text_path, encoding="utf-8") as f:
# #             body = f.read().strip()
# #     except OSError:
# #         body = ""
# #     if len(body) < 40:
# #         return {"page_type": "editorial", "products": [], "unreadable": [],
# #                 "notes": "no text layer on this page"}, {}
# #     prompt = (PROMPT +
# #               "\nThere is NO image in this request. Below is the page's raw text "
# #               "layer, top-to-bottom. Rebuild the offers from it, keeping each "
# #               "price with the product name it is printed next to.\nTEXT:\n" + body[:12000])
# #     return _post_chat(cfg, [{"role": "user", "content": prompt}],
# #                       label=os.path.basename(text_path))


# # BACKENDS = {"deepseek": call_vision, "text": call_text}


# # # ---------------------------------------------------------------------------
# # # normalise + validate
# # # ---------------------------------------------------------------------------
# # def normalise(rec, page_no, backend):
# #     rec = rec if isinstance(rec, dict) else {}
# #     if rec.get("page_type") not in PAGE_TYPES:
# #         rec["page_type"] = "product_grid"
# #     rows = []
# #     for pr in rec.get("products") or []:
# #         if not isinstance(pr, dict):
# #             continue
# #         row = {}
# #         for k in ROW_KEYS:
# #             v = pr.get(k, 0.0 if k in NUM_KEYS else "")
# #             if k in NUM_KEYS:
# #                 try:
# #                     v = float(str(v).replace("$", "").replace(",", "").strip() or 0)
# #                 except ValueError:
# #                     v = 0.0
# #             else:
# #                 v = str(v or "").strip()
# #             row[k] = v
# #         if not row["name"]:
# #             continue
# #         cat = row["category"].lower().replace(" ", "_").replace("&", "and")
# #         row["category"] = cat if cat in CATEGORIES else "other"
# #         row["confidence"] = min(1.0, max(0.0, row["confidence"]))
# #         # the model echoed the price back into was_price -> not a real was price
# #         if row["was_price"] and abs(row["was_price"] - row["price"]) < 1e-9:
# #             row["was_price"] = 0.0
# #         if row["was_price"] and row["price"] and row["was_price"] < row["price"]:
# #             row["was_price"] = 0.0          # "was" cheaper than "now" is nonsense
# #         rows.append(row)
# #     rec["products"] = rows
# #     rec["product_count"] = len(rows)
# #     rec["page"] = page_no
# #     rec["backend"] = backend
# #     for k in ("unreadable", "notes", "validity", "promo_headline"):
# #         if k == "unreadable":
# #             rec[k] = [str(u) for u in (rec.get(k) or [])]
# #         else:
# #             rec[k] = str(rec.get(k) or "")
# #     return rec


# # def validate_store(pages, pages_expected):
# #     """pages_expected = the page numbers this run asked for (None = all)."""
# #     problems, warnings = [], []
# #     got = sorted(p["page"] for p in pages)
# #     if pages_expected is not None:
# #         want = {int(n) for n in pages_expected}
# #         missing = sorted(want - set(got))
# #         if missing:
# #             problems.append("missing pages: {}".format(missing[:30]))
# #     dupes = sorted(n for n in set(got) if got.count(n) > 1)
# #     if dupes:
# #         problems.append("duplicate page records: {}".format(dupes))
# #     for p in pages:
# #         if not p["products"]:
# #             if p["page_type"] in ("cover", "index", "back_cover", "competition",
# #                                   "editorial", "promo"):
# #                 warnings.append("page {} ({}) has no priced products - normal for "
# #                                 "that page type".format(p["page"], p["page_type"]))
# #             else:
# #                 problems.append("page {} ({}) produced 0 products - did the model "
# #                                 "refuse the image? (wrong model / truncated base64)"
# #                                 .format(p["page"], p["page_type"]))
# #         if p["unreadable"]:
# #             warnings.append("page {}: {} unreadable entr(y|ies) - review".format(
# #                 p["page"], len(p["unreadable"])))
# #         seen = set()
# #         for r in p["products"]:
# #             if r["name"] in seen:
# #                 warnings.append("page {}: duplicate name {!r}".format(p["page"], r["name"][:40]))
# #             seen.add(r["name"])
# #             if r["price"] == 0:
# #                 warnings.append("page {}: price 0 - {}".format(p["page"], r["name"][:45]))
# #             elif r["price"] > 200:
# #                 warnings.append("page {}: implausible price {} - {}".format(
# #                     p["page"], r["price"], r["name"][:45]))
# #             if r["save"] and r["was_price"] == 0 and r["price"] > 0:
# #                 m = re.search(r"([0-9]+(?:\.[0-9]{2})?)", r["save"])
# #                 if m and abs(float(m.group(1)) - r["price"]) < 0.01:
# #                     warnings.append("page {}: save == price ({}) - likely a "
# #                                     "'2 for $X' or half-price banner misread: {}"
# #                                     .format(p["page"], r["price"], r["name"][:40]))
# #     return problems, warnings


# # def merge_store(entry, pages, out_json_dir, meta):
# #     rows = []
# #     for p in sorted(pages, key=lambda x: x["page"]):
# #         for r in p["products"]:
# #             rows.append({"store": entry["store"], "store_slug": entry["slug"],
# #                          "page": p["page"], "page_type": p["page_type"],
# #                          "catalogue_validity": p["validity"],
# #                          "promo_headline": p["promo_headline"], **r})
# #     doc = {"store": entry["store"], "slug": entry["slug"], "state": entry["state"],
# #            "label": entry["label"], "catalogue_validity": entry["validity"],
# #            "source": {"catalogue_page": CATALOGUE_PAGE,
# #                       "flowpaper": entry["flowpaper_url"],
# #                       "pdf": entry.get("pdf_url", ""),
# #                       "pdf_local": meta.get("pdf_local"),
# #                       "pdf_bytes": meta.get("pdf_bytes")},
# #            "extraction": {"backend": meta.get("backend"),
# #                           "model": meta.get("model"),
# #                           "reasoning_effort": meta.get("effort"),
# #                           "pages_in_pdf": meta.get("pages_total"),
# #                           "pages_requested": meta.get("pages_requested"),
# #                           "partial": bool(meta.get("partial")),
# #                           "pages_extracted": len(pages),
# #                           "pages_failed": meta.get("pages_failed", 0),
# #                           "products_total": len(rows),
# #                           "low_confidence_rows": sum(1 for r in rows if r["confidence"] < 0.9),
# #                           "fields_fill_rate": {
# #                               k: (round(sum(1 for r in rows if r.get(k) not in ("", 0, 0.0))
# #                                         / len(rows), 4) if rows else 0.0)
# #                               for k in ROW_KEYS},
# #                           "seconds": meta.get("seconds")},
# #            "validation": {"problems": meta.get("problems", []),
# #                           "warnings": meta.get("warnings", [])},
# #            "pages": sorted(pages, key=lambda x: x["page"]), "products": rows}
# #     jpath = os.path.join(out_json_dir, entry["slug"] + ".json")
# #     jlpath = os.path.join(out_json_dir, entry["slug"] + ".jsonl")
# #     atomic_write_json(jpath, doc)
# #     with open(jlpath, "w", encoding="utf-8") as f:
# #         for r in rows:
# #             f.write(json.dumps(r, ensure_ascii=False) + "\n")
# #     return doc, rows, jpath, jlpath


# # # ---------------------------------------------------------------------------
# # # stage 5: extraction driver (bounded pool + fail-fast)
# # # ---------------------------------------------------------------------------
# # def parse_range(spec, limit):
# #     if not spec:
# #         return list(range(1, limit + 1))
# #     out = []
# #     for part in str(spec).split(","):
# #         part = part.strip()
# #         if "-" in part:
# #             a, b = part.split("-", 1)
# #             out += list(range(int(a), int(b) + 1))
# #         elif part:
# #             out.append(int(part))
# #     return [n for n in out if 1 <= n <= limit]


# # def extract_pages(jobs, cfg, workers, force, logfile):
# #     """A queue.Queue + exactly N worker threads, so --workers really bounds
# #     concurrency. (ThreadPoolExecutor.submit in a loop does not.)"""
# #     work = queue.Queue()
# #     for j in jobs:
# #         work.put(j)
# #     lock = threading.Lock()
# #     state = {"done": 0, "ok": 0, "fail": 0, "abort": False, "reason": ""}
# #     results = {}

# #     def worker():
# #         while True:
# #             if state["abort"]:
# #                 return
# #             try:
# #                 store, page_no, img, txt, dest_dir = work.get_nowait()
# #             except queue.Empty:
# #                 return
# #             os.makedirs(dest_dir, exist_ok=True)
# #             dest = os.path.join(dest_dir, "p{:02d}.json".format(page_no))
# #             tag = "{} p{:02d}".format(store["slug"], page_no)
# #             status, rec, note = None, None, ""
# #             if not force and os.path.exists(dest):
# #                 try:
# #                     rec = json.load(open(dest, encoding="utf-8"))
# #                     status, note = "cached", "{} products [cached]".format(
# #                         rec.get("product_count", 0))
# #                 except (OSError, json.JSONDecodeError):
# #                     rec = None
# #             if rec is None:
# #                 t0 = time.time()
# #                 for attempt in (1, 2):
# #                     try:
# #                         fn = BACKENDS[cfg["backend"]]
# #                         raw = (fn(img, cfg, txt) if cfg["backend"] == "deepseek"
# #                                else fn(txt, cfg))
# #                         rec = normalise(raw[0], page_no, cfg["backend"])
# #                         note = "{} products [{}] {:.1f}s".format(
# #                             rec["product_count"], rec["page_type"], time.time() - t0)
# #                         status = "ok"
# #                         break
# #                     except (CredentialError, BlockedResponse) as e:
# #                         status, note = "fail", "{}: {}".format(type(e).__name__, e)
# #                         break                       # not transient - do not retry
# #                     except Exception as e:                            # noqa: BLE001
# #                         if attempt == 2:
# #                             status, note = "fail", "{}: {}".format(type(e).__name__, e)
# #                         else:
# #                             time.sleep(3)
# #                 if rec is not None and status == "ok":
# #                     atomic_write_json(dest, rec)
# #             with lock:
# #                 state["done"] += 1
# #                 state["ok" if status in ("ok", "cached") else "fail"] += 1
# #                 results[page_no] = status
# #                 log("  {:<22} {:>3}/{:<3} {}".format(tag, state["done"], len(jobs), note),
# #                     logfile)
# #                 if state["done"] >= 3 and state["ok"] == 0 and not state["abort"]:
# #                     state["abort"] = True
# #                     state["reason"] = ("first {} pages all failed and none succeeded "
# #                                        "- aborting instead of grinding through the "
# #                                        "rest. Last error: {}".format(state["done"], note))
# #             work.task_done()

# #     threads = [threading.Thread(target=worker, daemon=True)
# #                for _ in range(max(1, min(workers, len(jobs))))]
# #     for t in threads:
# #         t.start()
# #     for t in threads:
# #         t.join()
# #     if state["abort"]:
# #         log("  ABORT: " + state["reason"], logfile)
# #     return results, state


# # # ---------------------------------------------------------------------------
# # # main
# # # ---------------------------------------------------------------------------
# # CHEATSHEET = """
# # Drakes AU catalogue -> per-store JSON  (DeepSeek vision)

# #   python drakes_au_catalogue.py --list                    # what is live now
# #   python drakes_au_catalogue.py --diagnose                # can this machine do it?
# #   python drakes_au_catalogue.py --run --backend deepseek --api-key sk-XXXX
# #   python drakes_au_catalogue.py --run --stores hackham --pages 1-2 --backend deepseek --api-key sk-XXXX
# #   python drakes_au_catalogue.py --run --stage download    # PDFs only, no API spend
# #   python drakes_au_catalogue.py --run --stage render      # PDFs -> page images
# #   python drakes_au_catalogue.py --run --backend text --api-key sk-XXXX
# #   python verify_output.py --out ./drakes                  # integrity report

# # Output -> ./drakes/json/<store>.json  (one file per store, all pages merged)
# # Keys needed: --api-key, or DEEPSEEK_API_KEY in the environment / a .env file.
# # First run? Do --list, then --diagnose, then the 2-page smoke test. Everything
# # resumes: nothing already downloaded or extracted is redone.
# # """


# # def cmd_list(logfile):
# #     cats = discover(logfile)
# #     if not cats:
# #         print("no catalogues found on {} - page markup changed?".format(CATALOGUE_PAGE))
# #         return 1
# #     print("{} catalogues live on {}\n".format(len(cats), CATALOGUE_PAGE))
# #     print("  {:<20} {:<8} {:<26} {}".format("STORE", "STATE", "PUBLICATION", "ON SALE"))
# #     for c in cats:
# #         print("  {:<20} {:<8} {:<26} {}".format(c["store"][:20], c["state"],
# #                                                 c["pub"][:26], c["validity"][:44]))
# #     print("\nslugs: " + ", ".join(c["slug"] for c in cats))
# #     return 0


# # def cmd_diagnose(a, logfile):
# #     """One request per hop. Print the verdict, not a stack trace."""
# #     # Resolve the key first: a missing/broken key should be reported before
# #     # anything else, and --backend none is the only lane that needs no key.
# #     cfg = {}
# #     if a.backend != "none":
# #         try:
# #             cfg = resolve_config(a)
# #         except SystemExit as e:
# #             print("0) api key\n   FAIL {}".format(e))
# #             return 1
# #     print("1) drakes catalogue page")
# #     try:
# #         cats = discover(logfile)
# #         print("   OK  {} catalogues discovered".format(len(cats)))
# #     except Exception as e:                                            # noqa: BLE001
# #         print("   FAIL {}: {}".format(type(e).__name__, e))
# #         return 1
# #     print("\n2) flowpaper viewer -> pdf url")
# #     for c in cats:
# #         try:
# #             flowpaper_config(c, logfile)
# #             print("   OK  {:<22} {:<28} {}".format(c["store"][:22], c["pub"][:28],
# #                                                    c["pdf_url"][-46:]))
# #         except Exception as e:                                        # noqa: BLE001
# #             print("   FAIL {:<22} {}: {}".format(c["store"][:22], type(e).__name__, e))
# #     print("\n3) pdf download (first 200 KB only)")
# #     for c in cats[:1]:
# #         try:
# #             req = urllib.request.Request(c.get("pdf_url", ""),
# #                                          headers={"User-Agent": UA, "Range": "bytes=0-204800"})
# #             with urllib.request.urlopen(req, timeout=120) as r:
# #                 head = r.read(2048)
# #             print("   OK  HTTP {} first bytes {!r}".format(r.status, head[:8]))
# #         except Exception as e:                                        # noqa: BLE001
# #             print("   FAIL {}: {}".format(type(e).__name__, e))
# #     print("\n4) deepseek api  ({}, key {}...)".format(
# #         cfg["base_url"], (cfg["key"][:8] + "…") if cfg.get("key") else "MISSING"))
# #     try:
# #         req = urllib.request.Request(cfg["base_url"] + "/models",
# #                                      headers={"Authorization": "Bearer " + cfg["key"]})
# #         with urllib.request.urlopen(req, timeout=60) as r:
# #             models = json.loads(r.read())
# #         ids = [m["id"] for m in models.get("data", [])]
# #         vision = [m["id"] for m in models.get("data", [])
# #                   if "image" in (m.get("input_modalities") or [])]
# #         print("   OK  key works. models: {}".format(", ".join(ids)))
# #         print("   vision-capable: {}".format(", ".join(vision) or "NONE"))
# #         if cfg["backend"] == "deepseek" and cfg["model"] not in vision:
# #             print("   PROBLEM: {} cannot read images - use {} or --backend text"
# #                   .format(cfg["model"], DEEPSEEK_DEFAULT_MODEL))
# #             return 1
# #         print("   NOTE: a models list succeeding says nothing about credit. Make one "
# #               "real call (the 2-page smoke test) before planning a full run.")
# #     except urllib.error.HTTPError as e:
# #         body = e.read().decode("utf-8", "replace")[:200]
# #         print("   FAIL HTTP {}: {}".format(e.code, body))
# #         if e.code in (401, 403):
# #             print("   -> the key was rejected. Common causes: wrong key, a key for a "
# #                   "different\n      provider, an intercepting proxy / corporate TLS "
# #                   "inspection, or a key\n      pasted with surrounding quotes or a "
# #                   "'Bearer ' prefix already in it.")
# #         return 1
# #     except CredentialError as e:
# #         print("   FAIL credentials: {}".format(e))
# #         return 1
# #     except Exception as e:                                            # noqa: BLE001
# #         print("   FAIL {}: {}".format(type(e).__name__, e))
# #         return 1
# #     return 0


# # def cmd_run(a):
# #     outdir = os.path.abspath(a.out)
# #     logfile = os.path.join(outdir, "run.log")
# #     os.makedirs(outdir, exist_ok=True)
# #     for sub in ("pdfs", "pages", "text", "pages_json", "json"):
# #         os.makedirs(os.path.join(outdir, sub), exist_ok=True)

# #     cfg = resolve_config(a)
# #     log("drakes_au_catalogue pid={} stage={} backend={}{}".format(
# #         os.getpid(), a.stage, a.backend,
# #         " model=" + cfg["model"] if a.backend != "none" else ""), logfile)

# #     cache = os.path.join(outdir, "catalogues.json")
# #     if os.path.exists(cache) and not a.refresh:
# #         cat_doc = json.load(open(cache, encoding="utf-8"))
# #         cats = cat_doc.get("catalogues", [])
# #         log("discovery cache: {} catalogues ({} - use --refresh to re-discover)"
# #             .format(len(cats), os.path.basename(cache)), logfile)
# #     else:
# #         cats = discover(logfile)
# #         atomic_write_json(cache, {"source": CATALOGUE_PAGE,
# #                                   "discovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
# #                                   "catalogues": cats})
# #         log("discovered {} catalogues -> {}".format(len(cats), cache), logfile)
# #     if not cats:
# #         raise SystemExit("no catalogues found - drakes.com.au markup changed?")

# #     if a.stores:
# #         want = {s.strip().lower() for s in a.stores.split(",")}
# #         all_cats = cats
# #         cats = [c for c in cats if c["slug"] in want or c["store"].lower() in want]
# #         if not cats:
# #             raise SystemExit("no catalogue matches --stores {}".format(a.stores))
# #     else:
# #         all_cats = cats

# #     summary = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "out": outdir,
# #                "stage": a.stage, "backend": a.backend,
# #                "model": cfg.get("model"), "stores": {}, "problems": []}
# #     t_start = time.time()

# #     if a.stage in ("all", "download", "render"):
# #         log("--- stage 1/3: resolve + download PDFs", logfile)
# #         for c in cats:
# #             try:
# #                 if not c.get("pdf_url"):
# #                     flowpaper_config(c, logfile)
# #                 dest = os.path.join(outdir, "pdfs", c["slug"] + ".pdf")
# #                 size, how = download_pdf(c, dest, logfile, force=a.force)
# #                 c["pdf_local"] = dest
# #                 c["pdf_bytes"] = size
# #                 log("  {:<20} {:>6.1f} MB  {}".format(c["store"], size / 1048576.0, how),
# #                     logfile)
# #             except Exception as e:                                    # noqa: BLE001
# #                 c["pdf_error"] = "{}: {}".format(type(e).__name__, e)
# #                 log("  {:<20} FAIL {}".format(c["store"], c["pdf_error"]), logfile)
# #                 summary["problems"].append("{}: {}".format(c["store"], c["pdf_error"]))
# #         atomic_write_json(os.path.join(outdir, "catalogues.json"),
# #                           {"source": CATALOGUE_PAGE, "catalogues": all_cats})

# #     if a.stage in ("all", "render"):
# #         log("--- stage 2/3: render pages + text layer", logfile)
# #         for c in cats:
# #             pdf = c.get("pdf_local")
# #             if not pdf or not os.path.exists(pdf):
# #                 log("  {:<20} skipped (no pdf)".format(c["store"]), logfile)
# #                 continue
# #             try:
# #                 render_pdf(pdf, os.path.join(outdir, "pages", c["slug"]),
# #                            os.path.join(outdir, "text", c["slug"]),
# #                            dpi=a.dpi, force=a.force, logfile=logfile)
# #             except Exception as e:                                    # noqa: BLE001
# #                 log("  {:<20} RENDER FAIL {}".format(c["store"], e), logfile)
# #                 summary["problems"].append("{}: render {}".format(c["store"], e))

# #     if a.stage in ("all", "extract", "merge") and a.backend != "none":
# #         log("--- stage 3/3: extract with {} (workers={})".format(a.backend, a.workers), logfile)
# #         jobs = []
# #         for c in cats:
# #             pages_dir = os.path.join(outdir, "pages", c["slug"])
# #             # Per-store page-json dir: sharing one flat dir makes every store
# #             # overwrite the previous store's p01.json, silently.
# #             pj_dir = os.path.join(outdir, "pages_json", c["slug"])
# #             imgs = sorted(glob.glob(os.path.join(pages_dir, "p[0-9][0-9].jpg")))
# #             if not imgs:
# #                 log("  {:<20} no page images - run --stage render first".format(c["store"]),
# #                     logfile)
# #                 continue
# #             want = set(parse_range(a.pages, len(imgs)))
# #             for img in imgs:
# #                 n = int(re.search(r"p(\d+)\.", os.path.basename(img)).group(1))
# #                 if n in want:
# #                     jobs.append((c, n, img,
# #                                  os.path.join(outdir, "text", c["slug"],
# #                                               "p{:02d}.txt".format(n)), pj_dir))
# #         if not jobs:
# #             log("no pages to extract", logfile)
# #         else:
# #             log("  {} pages queued".format(len(jobs)), logfile)
# #             results, state = extract_pages(jobs, cfg, a.workers, a.force, logfile)
# #             summary["extract"] = {"queued": len(jobs), "ok": state["ok"],
# #                                   "fail": state["fail"], "aborted": state["abort"],
# #                                   "abort_reason": state["reason"]}
# #             if state["abort"]:
# #                 summary["problems"].append(state["reason"])
# #             elif state["fail"] and state["ok"] == 0:
# #                 # A short run never trips the >=3 fail-fast guard, so say it here
# #                 # rather than letting "0 products" pass as a clean result.
# #                 summary["problems"].append(
# #                     "all {} queued page(s) failed and none succeeded".format(state["fail"]))

# #     if a.stage in ("all", "render", "extract", "merge"):
# #         log("--- merge: one JSON per store -> json/", logfile)
# #         for c in cats:
# #             pj = os.path.join(outdir, "pages_json", c["slug"])
# #             files = sorted(glob.glob(os.path.join(pj, "p[0-9][0-9].json")))
# #             if not files:
# #                 log("  {:<20} nothing extracted yet - skipped".format(c["store"]), logfile)
# #                 summary["stores"][c["slug"]] = {"store": c["store"], "products": 0,
# #                                                 "pages": 0,
# #                                                 "note": "nothing extracted"}
# #                 continue
# #             # Merge from DISK, not from this run's results: a repair run with
# #             # --pages 4 must rebuild the full store file, not truncate it to p4.
# #             pages = []
# #             for f in files:
# #                 try:
# #                     pages.append(json.load(open(f, encoding="utf-8")))
# #                 except (OSError, json.JSONDecodeError) as e:
# #                     log("  {} unreadable page json {}: {}".format(c["slug"], f, e), logfile)
# #             pages_dir = os.path.join(outdir, "pages", c["slug"])
# #             all_imgs = sorted(glob.glob(os.path.join(pages_dir, "p[0-9][0-9].jpg")))
# #             want = parse_range(a.pages, len(all_imgs))
# #             want_set = set(want)
# #             pages = [p for p in pages if p["page"] in want_set] if a.pages else pages
# #             meta = {"backend": a.backend, "model": cfg.get("model"),
# #                     "effort": cfg.get("effort"),
# #                     "pages_total": len(all_imgs),
# #                     "pages_requested": len(want),
# #                     "partial": len(want) < len(all_imgs),
# #                     "pages_failed": 0,
# #                     "pdf_local": c.get("pdf_local"), "pdf_bytes": c.get("pdf_bytes"),
# #                     "pdf_url": c.get("pdf_url"), "seconds": round(time.time() - t_start)}
# #             problems, warnings = validate_store(pages, want)
# #             if meta["partial"]:
# #                 warnings.append("PARTIAL RUN: pages {} of {} in this PDF ({} of {}) - "
# #                                 "re-run without --pages for the full store"
# #                                 .format(want[0], want[-1], len(want), len(all_imgs)))
# #             meta["problems"], meta["warnings"] = problems, warnings
# #             doc, rows, jpath, jlpath = merge_store(c, pages, os.path.join(outdir, "json"), meta)
# #             summary["stores"][c["slug"]] = {
# #                 "store": c["store"], "pages": len(pages), "pages_in_pdf": meta["pages_total"],
# #                 "products": len(rows), "low_confidence": doc["extraction"]["low_confidence_rows"],
# #                 "json": os.path.relpath(jpath, outdir), "problems": len(problems),
# #                 "warnings": len(warnings)}
# #             log("  {:<20} {:>3} pages  {:>4} products  {:>3} low-conf  {} problems"
# #                 .format(c["store"], len(pages), len(rows),
# #                         doc["extraction"]["low_confidence_rows"], len(problems)), logfile)
# #             summary["problems"] += ["{}: {}".format(c["store"], p) for p in problems]

# #     summary["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
# #     summary["seconds"] = round(time.time() - t_start)
# #     total_rows = sum(s.get("products", 0) for s in summary["stores"].values())
# #     summary["products_total"] = total_rows
# #     atomic_write_json(os.path.join(outdir, "summary.json"), summary)

# #     print("\nstores   {}".format(len(summary["stores"])))
# #     print("products {}".format(total_rows))
# #     print("elapsed  {}s".format(summary["seconds"]))
# #     print("json     {}".format(os.path.join(outdir, "json")))
# #     print("summary  {}".format(os.path.join(outdir, "summary.json")))
# #     if summary["problems"]:
# #         print("\nPROBLEMS ({}):".format(len(summary["problems"])))
# #         for p in summary["problems"][:20]:
# #             print("  - " + p)
# #     else:
# #         print("\nno structural problems found.")
# #     return 1 if (summary["problems"] and a.strict) else 0


# # def main():
# #     ap = argparse.ArgumentParser(
# #         description="Drakes AU catalogues -> per-store JSON via DeepSeek vision",
# #         formatter_class=argparse.RawDescriptionHelpFormatter, epilog=CHEATSHEET)
# #     ap.add_argument("--run", action="store_true", help="run the pipeline")
# #     ap.add_argument("--list", action="store_true", help="list live catalogues and exit")
# #     ap.add_argument("--diagnose", action="store_true",
# #                     help="probe drakes, flowpaper, the pdf host and the api key, then exit")
# #     ap.add_argument("--stage", default="all",
# #                     choices=["all", "download", "render", "extract", "merge"],
# #                     help="stop after this stage (default all)")
# #     ap.add_argument("--stores", help="comma-separated slugs, e.g. hackham,queensland")
# #     ap.add_argument("--pages", help="page range, e.g. 1-40 or 1,5,9 (default: all)")
# #     ap.add_argument("--out", default="./drakes", help="output dir (default ./drakes)")
# #     ap.add_argument("--backend", default="deepseek", choices=["deepseek", "text", "none"])
# #     ap.add_argument("--workers", type=int, default=4, help="concurrent pages (default 4)")
# #     ap.add_argument("--dpi", type=int, default=200, help="page render dpi (default 200)")
# #     ap.add_argument("--api-key", help="DeepSeek key (else DEEPSEEK_API_KEY)")
# #     ap.add_argument("--base-url", default="https://api.openai.com/v1")
# #     ap.add_argument("--model", default="gpt-4o-mini",
# #                     help="defaults to deepseek-flash; v4-pro/chats have NO vision")
# #     ap.add_argument("--max-tokens", type=int, default=8000)
# #     ap.add_argument("--reasoning-effort", default="low", choices=["low", "high", "max"],
# #                     help="deepseek-flash effort: low=13s/page, high=51s/page, same rows")
# #     ap.add_argument("--timeout", type=int, default=DEEPSEEK_DEFAULT_TIMEOUT)
# #     ap.add_argument("--text-hint", action="store_true",
# #                     help="send the PDF text layer alongside the image (spelling help, "
# #                          "slower; off by default)")
# #     ap.add_argument("--force", action="store_true", help="redo cached work")
# #     ap.add_argument("--refresh", action="store_true",
# #                     help="re-discover catalogues instead of using catalogues.json")
# #     ap.add_argument("--strict", action="store_true",
# #                     help="exit non-zero when problems were found")
# #     a = ap.parse_args()

# #     if a.list:
# #         return cmd_list(None)
# #     if a.diagnose:
# #         os.makedirs(os.path.abspath(a.out), exist_ok=True)
# #         return cmd_diagnose(a, os.path.join(os.path.abspath(a.out), "run.log"))
# #     if not a.run:
# #         print(CHEATSHEET)
# #         return 0
# #     return cmd_run(a)


# # if __name__ == "__main__":
# #     sys.exit(main())













# #!/usr/env python3
# """
# Drakes AU (drakes.com.au) weekly catalogues -> per-store JSON, extracted with the
# DeepSeek vision API + hybrid text layer.
# """
# from __future__ import annotations

# import argparse
# import base64
# import glob
# import html
# import json
# import os
# import queue
# import random
# import re
# import sys
# import threading
# import time
# import urllib.error
# import urllib.parse
# import urllib.request
# from datetime import datetime

# # ---------------------------------------------------------------------------
# # config
# # ---------------------------------------------------------------------------
# UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
#       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# CATALOGUE_PAGE = "https://drakes.com.au/catalogue/"

# DEEPSEEK_BASE = "https://api.deepseek.com/v1"
# DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"     # the ONLY DeepSeek model with vision
# DEEPSEEK_NO_VISION = ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner",
#                       "deepseek-reasoner-v4", "deepseek-v3")
# DEEPSEEK_MIN_TOKENS = 8000
# DEEPSEEK_DEFAULT_TIMEOUT = 300

# ROW_KEYS = ["name", "brand", "size", "price", "unit_price", "was_price", "save",
#             "save_type", "origin", "category", "confidence"]
# NUM_KEYS = ("price", "was_price", "confidence")
# CATEGORIES = ("pantry", "dairy", "meat", "seafood", "deli", "produce", "bakery",
#               "frozen", "drinks", "liquor", "household", "health", "baby", "pet",
#               "general_merchandise", "other")
# PAGE_TYPES = ("cover", "product_grid", "editorial", "promo", "index", "back_cover",
#               "competition", "liquor", "general_merchandise")

# ROW_SCHEMA = {k: (0.0 if k in NUM_KEYS else None if k == "was_price" else "") for k in ROW_KEYS}
# PAGE_SCHEMA = {
#     "page_type": "cover|product_grid|editorial|promo|index|back_cover|competition|general_merchandise|liquor",
#     "validity": "ISO date range (YYYY-MM-DD to YYYY-MM-DD) exactly as printed on THIS page, else empty",
#     "promo_headline": "main promo headline printed on the page, else empty",
#     "products": [ROW_SCHEMA],
#     "offers": [],
#     "unreadable": ["offers visible but too small/blurry to trust"],
#     "notes": "",
# }

# PROMPT = (
#     "Extract EVERY product offer from this Australian supermarket (Drakes "
#     "Supermarkets) catalogue page.\n"
#     "Return ONLY raw JSON. No markdown fences, no commentary.\n"
#     "Schema:\n" + json.dumps(PAGE_SCHEMA) + "\n"
#     "Rules:\n"
#     "- One array entry per distinct advertised offer. If one offer covers several\n"
#     "  varieties, put every variety in the name exactly as printed.\n"
#     "- price = the advertised shelf price in AUD as a number only (6.90, not\n"
#     "  \"$6.90\"). If priced by weight put number in price and 'per kg' in size. Use 0 only if unreadable.\n"
#     "- unit_price = small unit price as printed (\"$2.25 per 100g\"), else \"\".\n"
#     "- was_price = previous price if printed. If SAVE $X is printed, was_price must equal price + X. If not printed, store null.\n"
#     "- save = SAVE / half-price claim copied verbatim (\"SAVE $4\", \"1/2 PRICE\"), else \"\".\n"
#     "- save_type = classify save as '$', 'percent', 'half_price', 'up_to', or ''\n"
#     "- origin = extract country/region prefix like 'Australian' if present in name/brand, else ''\n"
#     "- category = one of: " + ", ".join(CATEGORIES) + "\n"
#     "- Vouchers, coupons, and non-product editorial mentions (e.g. self-serve coffee, hot meals vouchers) go into 'offers', NOT 'products'.\n"
#     "- confidence = 0..1 confidence score.\n"
# )


# # ---------------------------------------------------------------------------
# # small helpers & date normalization
# # ---------------------------------------------------------------------------
# _print_lock = threading.Lock()


# class BlockedResponse(RuntimeError):
#     pass


# class CredentialError(RuntimeError):
#     pass


# def log(msg, logfile=None):
#     line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
#     with _print_lock:
#         sys.stderr.write(line + "\n")
#         sys.stderr.flush()
#         if logfile:
#             try:
#                 with open(logfile, "a", encoding="utf-8") as f:
#                     f.write(line + "\n")
#             except OSError:
#                 pass


# def slugify(s):
#     s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
#     return s or "unnamed"


# def atomic_write_json(path, obj):
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as f:
#         json.dump(obj, f, ensure_ascii=False, indent=1)
#     os.replace(tmp, path)


# def normalize_date_range(text):
#     if not text:
#         return ""
#     # Try parsing patterns like "30/9/2026 to 6/10/2026" or "Wednesday 30th September 2026 until Tuesday 6th October 2026"
#     text_clean = re.sub(r"(st|nd|rd|th)", "", text, flags=re.IGNORECASE)
#     # Extract numbers and months
#     m = re.search(r"(\d{1,2})[/\s](\d{1,2})[/\s](\d{4}).*?(\d{1,2})[/\s](\d{1,2})[/\s](\d{4})", text_clean)
#     if m:
#         try:
#             d1, m1, y1, d2, m2, y2 = map(int, m.groups())
#             dt1 = datetime(y1, m1, d1)
#             dt2 = datetime(y2, m2, d2)
#             return "{} to {}".format(dt1.strftime("%Y-%m-%d"), dt2.strftime("%Y-%m-%d"))
#         except ValueError:
#             pass
#     return text.strip()


# def load_key(name, explicit=None):
#     if explicit:
#         return explicit
#     if os.environ.get(name):
#         return os.environ[name]
#     here = os.path.dirname(os.path.abspath(__file__))
#     for p in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env"),
#               os.path.expanduser("~/.hermes/.env"),
#               os.path.expanduser("~/.deepseek.env")):
#         if os.path.exists(p):
#             try:
#                 with open(p, encoding="utf-8", errors="replace") as f:
#                     for line in f:
#                         line = line.strip()
#                         if line.startswith(name + "="):
#                             return line.split("=", 1)[1].strip().strip('"').strip("'")
#             except OSError:
#                 continue
#     return None


# def http(url, data=None, headers=None, timeout=120, tries=3, label="", logfile=None):
#     body = json.dumps(data).encode() if data is not None else None
#     last = None
#     for attempt in range(tries):
#         try:
#             req = urllib.request.Request(url, data=body, headers=headers or {})
#             with urllib.request.urlopen(req, timeout=timeout) as r:
#                 return r.read()
#         except urllib.error.HTTPError as e:
#             detail = e.read().decode("utf-8", "replace")[:300]
#             last = "HTTP {}: {}".format(e.code, detail)
#             if e.code in (401, 403):
#                 raise CredentialError("{} -> HTTP {} {}".format(label or url, e.code, detail))
#             if e.code in (400, 404, 422):
#                 raise RuntimeError("{} -> HTTP {} {}".format(label or url, e.code, detail))
#             if e.code == 429:
#                 time.sleep(min(60, 5 * (attempt + 1) + random.random() * 3))
#                 continue
#             time.sleep(2 * (attempt + 1))
#         except Exception as e:
#             last = "{}: {}".format(type(e).__name__, e)
#             time.sleep(2 * (attempt + 1) + random.random())
#     raise RuntimeError("{} failed after {} tries -> {}".format(label or url, tries, last))


# # ---------------------------------------------------------------------------
# # stage 1 & 2: discovery & rendering
# # ---------------------------------------------------------------------------
# TILE_RE = re.compile(
#     r'"label"\s*:\s*"([^"]{2,80})"'
#     r'.{0,600}?"link"\s*:\s*\{[^{}]{0,200}?"url"\s*:\s*"(https://([a-z0-9]+)\.flowpaper\.com/([^"/]+)/[^"]*)"'
#     r'.{0,200}?"title"\s*:\s*"([^"]{1,60})"'
#     r'.{0,200}?"description"\s*:\s*"([^"]{0,160})"',
#     re.S)


# def discover(logfile=None):
#     raw = http(CATALOGUE_PAGE, headers={"User-Agent": UA}, label="drakes catalogue page",
#                logfile=logfile).decode("utf-8", "replace")
#     doc = html.unescape(raw).replace("\\/", "/")
#     out, seen = [], set()
#     for label, fp_url, sub, pub, title, desc in TILE_RE.findall(doc):
#         if pub in seen:
#             continue
#         seen.add(pub)
#         low = (title + " " + label).lower()
#         out.append({
#             "store": html.unescape(title).strip(),
#             "slug": slugify(html.unescape(title)),
#             "label": html.unescape(label).strip(),
#             "state": "QLD" if "queensland" in low else "SA",
#             "sub": sub,
#             "pub": pub,
#             "flowpaper_url": fp_url,
#             "validity": normalize_date_range(html.unescape(desc).strip()),
#         })
#     return out


# def flowpaper_config(entry, logfile=None):
#     page = http(entry["flowpaper_url"], headers={"User-Agent": UA},
#                 label="flowpaper " + entry["pub"], logfile=logfile).decode("utf-8", "replace")

#     def grab(name):
#         m = re.search(name + r'\s*:\s*"([^"]*)"', page)
#         return m.group(1) if m else ""

#     pdf = grab("PDFFile").split("?")[0]
#     img_template = grab("HighResIMGFiles") or grab("IMGFiles")
#     if not pdf and not img_template:
#         raise RuntimeError("flowpaper {} exposes neither PDFFile nor IMGFiles".format(entry["pub"]))
#     t = re.search(r"<title>([^<]+)</title>", page)
#     entry["pdf_path"] = pdf
#     entry["pdf_url"] = (urllib.parse.urljoin(entry["flowpaper_url"], pdf) if pdf else "")
#     entry["img_template"] = img_template
#     entry["pub_title"] = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
#     return entry


# def download_pdf(entry, dest, logfile=None, force=False):
#     if (not force and os.path.exists(dest) and os.path.getsize(dest) > 100_000):
#         with open(dest, "rb") as f:
#             if f.read(5).startswith(b"%PDF"):
#                 return os.path.getsize(dest), "cached"
#     if not entry.get("pdf_url"):
#         raise RuntimeError("no pdf url for {}".format(entry["store"]))
#     part = dest + ".part"
#     offset = os.path.getsize(part) if os.path.exists(part) else 0
#     headers = {"User-Agent": UA, "Referer": entry["flowpaper_url"]}
#     if offset:
#         headers["Range"] = "bytes={}-".format(offset)
#     try:
#         req = urllib.request.Request(entry["pdf_url"], headers=headers)
#         with urllib.request.urlopen(req, timeout=180) as r:
#             if offset and r.status != 206:
#                 offset = 0
#             mode = "ab" if offset else "wb"
#             got = 0
#             with open(part, mode) as f:
#                 while True:
#                     chunk = r.read(262144)
#                     if not chunk:
#                         break
#                     f.write(chunk)
#                     got += len(chunk)
#     except urllib.error.HTTPError as e:
#         raise RuntimeError("pdf download HTTP {}: {}".format(
#             e.code, e.read().decode("utf-8", "replace")[:200]))
#     with open(part, "rb") as f:
#         magic = f.read(5)
#     if not magic.startswith(b"%PDF"):
#         head = open(part, "rb").read(200)
#         os.remove(part)
#         raise BlockedResponse("{} did not return a PDF".format(entry["pdf_url"]))
#     os.replace(part, dest)
#     return os.path.getsize(dest), "downloaded"


# def render_pdf(pdf_path, pages_dir, text_dir, dpi=200, force=False, logfile=None):
#     try:
#         import pymupdf
#     except ImportError:
#         raise SystemExit("PDF rendering needs pymupdf:   pip install pymupdf")
#     os.makedirs(pages_dir, exist_ok=True)
#     os.makedirs(text_dir, exist_ok=True)
#     doc = pymupdf.open(pdf_path)
#     made, text_pages, chars = [], 0, 0
#     for i, page in enumerate(doc, 1):
#         img = os.path.join(pages_dir, "p{:02d}.jpg".format(i))
#         txt = os.path.join(text_dir, "p{:02d}.txt".format(i))
#         if force or not os.path.exists(img):
#             pix = page.get_pixmap(dpi=dpi, alpha=False)
#             pix.save(img, jpg_quality=88)
#         made.append(img)
#         if force or not os.path.exists(txt):
#             body = page.get_text().strip()
#             with open(txt, "w", encoding="utf-8") as f:
#                 f.write(body)
#         try:
#             with open(txt, encoding="utf-8") as f:
#                 body = f.read()
#         except OSError:
#             body = ""
#         chars += len(body)
#         if len(body) > 80:
#             text_pages += 1
#     n = doc.page_count
#     doc.close()
#     log("    {} pages rendered @{}dpi, text layer on {}/{} pages".format(
#         n, dpi, text_pages, n), logfile)
#     return made, n, text_pages


# # ---------------------------------------------------------------------------
# # stage 4: backends & hybrid post-processing
# # ---------------------------------------------------------------------------
# def resolve_config(a):
#     cfg = {"timeout": a.timeout, "model": a.model, "max_tokens": a.max_tokens,
#            "base_url": a.base_url, "effort": a.reasoning_effort,
#            "text_hint": a.text_hint, "backend": a.backend}
#     if a.backend == "none":
#         return cfg
#     cfg["base_url"] = (DEEPSEEK_BASE if a.base_url == "https://api.openai.com/v1" else a.base_url)
#     if a.model == "gpt-4o-mini":
#         cfg["model"] = DEEPSEEK_DEFAULT_MODEL
#     if cfg["max_tokens"] < DEEPSEEK_MIN_TOKENS:
#         cfg["max_tokens"] = DEEPSEEK_MIN_TOKENS
#     key = (a.api_key or os.environ.get("DEEPSEEK_API_KEY") or load_key("DEEPSEEK_API_KEY"))
#     if not key:
#         raise SystemExit("No DeepSeek API key found. Pass --api-key or set DEEPSEEK_API_KEY.")
#     cfg["key"] = key
#     return cfg


# def _json_from_text(txt):
#     s = (txt or "").strip()
#     s = re.sub(r"^```(?:json)?\s*", "", s)
#     s = re.sub(r"\s*```$", "", s).strip()
#     if not s:
#         raise ValueError("empty model content")
#     try:
#         return json.loads(s)
#     except json.JSONDecodeError:
#         pass
#     start, end = s.find("{"), s.rfind("}")
#     if start != -1 and end > start:
#         return json.loads(s[start:end + 1])
#     raise ValueError("no JSON object in model output")


# def _post_chat(cfg, messages, label, max_tokens=None):
#     body = {"model": cfg["model"], "messages": messages,
#             "max_completion_tokens": max_tokens or cfg["max_tokens"]}
#     if cfg.get("effort"):
#         body["reasoning_effort"] = cfg["effort"]
#     headers = {"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"}
#     url = cfg["base_url"].rstrip("/") + "/chat/completions"
#     raw = http(url, data=body, headers=headers, timeout=cfg["timeout"], tries=4, label=label)
#     try:
#         out = json.loads(raw)
#     except json.JSONDecodeError:
#         raise BlockedResponse("chat endpoint returned non-JSON")
#     ch = out.get("choices") or [{}]
#     content = (ch[0].get("message") or {}).get("content", "")
#     if not content.strip():
#         raise ValueError("empty content")
#     return _json_from_text(content), out.get("usage") or {}


# def call_vision(img_path, cfg, text_path=None):
#     with open(img_path, "rb") as f:
#         b = base64.b64encode(f.read()).decode()
#     prompt = PROMPT
#     if text_path and os.path.exists(text_path):
#         try:
#             with open(text_path, encoding="utf-8") as f:
#                 hint = f.read().strip()[:2500]
#         except OSError:
#             hint = ""
#         if hint:
#             prompt += ("\nOCR HINT (text layer for spelling, validity, fine print): " + hint + "\n")
#     return _post_chat(cfg, [{"role": "user", "content": [
#         {"type": "text", "text": prompt},
#         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b, "detail": "high"}}]}],
#         label=os.path.basename(img_path))


# def call_text(text_path, cfg):
#     try:
#         with open(text_path, encoding="utf-8") as f:
#             body = f.read().strip()
#     except OSError:
#         body = ""
#     if len(body) < 40:
#         return {"page_type": "editorial", "products": [], "offers": [], "unreadable": [], "notes": "no text layer"}, {}
#     prompt = PROMPT + "\nTEXT LAYER:\n" + body[:12000]
#     return _post_chat(cfg, [{"role": "user", "content": prompt}], label=os.path.basename(text_path))


# BACKENDS = {"deepseek": call_vision, "text": call_text}


# # ---------------------------------------------------------------------------
# # normalise + validate + hybrid text layer enrichment
# # ---------------------------------------------------------------------------
# def normalise(rec, page_no, backend, text_path=None, total_pages=4):
#     rec = rec if isinstance(rec, dict) else {}
    
#     # Read text layer for hybrid enrichment (validity, spelling, fine print, back cover detection)
#     txt_content = ""
#     if text_path and os.path.exists(text_path):
#         try:
#             with open(text_path, encoding="utf-8") as tf:
#                 txt_content = tf.read()
#         except OSError:
#             pass

#     # Page type detection & corrections
#     ptype = rec.get("page_type")
#     if ptype not in PAGE_TYPES:
#         ptype = "product_grid"
    
#     # Detect back cover if last page or contains address/opening hours/legal
#     if page_no == total_pages or any(k in txt_content.lower() for k in ("opening hours", "phone:", "ascot park sa", "unless sold out prior")):
#         if ptype == "cover" or page_no > 1:
#             ptype = "back_cover"
#     rec["page_type"] = ptype

#     # Validity extraction and normalization from text layer if empty
#     validity = rec.get("validity", "")
#     if not validity and txt_content:
#         vm = re.search(r"(?:offer valid|on sale)[:\s]*(.*?)(?:only at|unless|$)", txt_content, re.IGNORECASE)
#         if vm:
#             validity = vm.group(1).strip()
#     rec["validity"] = normalize_date_range(validity)

#     # Offers / vouchers / dropped items
#     offers = rec.get("offers", []) or []
#     rows = []
    
#     # Process product rows
#     for pr in rec.get("products") or []:
#         if not isinstance(pr, dict):
#             continue
#         row = {}
#         for k in ROW_KEYS:
#             if k in ("save_type", "origin"):
#                 continue
#             v = pr.get(k, 0.0 if k in NUM_KEYS else "")
#             if k in NUM_KEYS:
#                 try:
#                     v = float(str(v).replace("$", "").replace(",", "").strip() or 0)
#                 except ValueError:
#                     v = 0.0 if k != "was_price" else None
#             else:
#                 v = str(v or "").strip()
#             row[k] = v

#         name = row["name"]
#         if not name:
#             continue

#         # Spellcheck corrections from text layer / artwork rules (e.g. Pastry -> Pasty)
#         if "Pastry" in name and "Pasty" in txt_content:
#             name = name.replace("Pastry", "Pasty")
#         row["name"] = name

#         # Extract origin ("Australian")
#         origin = ""
#         if name.startswith("Australian "):
#             origin = "Australian"
#             name = name[11:].strip()
#             row["name"] = name
#         elif row["brand"] and "Australian" in row["brand"]:
#             origin = row["brand"]
#             row["brand"] = row["brand"].replace("Australian", "").strip()

#         row["origin"] = origin

#         # Filter out zero-price vouchers or non-product editorial items into offers
#         if row["price"] == 0.0 and any(kw in name.lower() for kw in ("voucher", "kitchen", "coffee", "meal")):
#             offers.append({"name": name, "brand": row["brand"], "save": row["save"], "category": row["category"]})
#             continue

#         cat = row["category"].lower().replace(" ", "_").replace("&", "and")
#         row["category"] = cat if cat in CATEGORIES else "other"
#         row["confidence"] = min(1.0, max(0.0, row["confidence"]))

#         # Save type classification
#         save_str = row["save"]
#         save_type = ""
#         if "1/2 PRICE" in save_str.upper() or "HALF" in save_str.upper():
#             save_type = "half_price"
#         elif "SAVE $" in save_str.upper():
#             save_type = "$"
#         elif "%" in save_str:
#             save_type = "percent"
#         elif "UP TO" in save_str.upper():
#             save_type = "up_to"
#         row["save_type"] = save_type

#         # Was price invariant: if save is SAVE $X, was_price == price + X. Store null (None) if absent.
#         was = row["was_price"]
#         if not was or was == 0.0:
#             if save_type == "$":
#                 m_save = re.search(r"\$([0-9]+(?:\.[0-9]{2})?)", save_str)
#                 if m_save and row["price"] > 0:
#                     was = round(row["price"] + float(m_save.group(1)), 2)
#         row["was_price"] = was if was and was > 0 else None

#         rows.append(row)

#     rec["products"] = rows
#     rec["offers"] = offers
#     rec["product_count"] = len(rows)
#     rec["page"] = page_no
#     rec["backend"] = backend
    
#     for k in ("unreadable", "notes", "validity", "promo_headline"):
#         if k == "unreadable":
#             rec[k] = [str(u) for u in (rec.get(k) or [])]
#         else:
#             rec[k] = str(rec.get(k) or "")
            
#     return rec


# def validate_store(pages, pages_expected):
#     problems, warnings = [], []
#     got = sorted(p["page"] for p in pages)
#     if pages_expected is not None:
#         want = {int(n) for n in pages_expected}
#         missing = sorted(want - set(got))
#         if missing:
#             problems.append("missing pages: {}".format(missing[:30]))
#     return problems, warnings


# def merge_store(entry, pages, out_json_dir, meta):
#     rows = []
#     for p in sorted(pages, key=lambda x: x["page"]):
#         for r in p["products"]:
#             rows.append({"store": entry["store"], "store_slug": entry["slug"],
#                          "page": p["page"], "page_type": p["page_type"],
#                          "catalogue_validity": p["validity"],
#                          "promo_headline": p["promo_headline"], **r})
#     doc = {"store": entry["store"], "slug": entry["slug"], "state": entry["state"],
#            "label": entry["label"], "catalogue_validity": entry["validity"],
#            "source": {"catalogue_page": CATALOGUE_PAGE,
#                       "flowpaper": entry["flowpaper_url"],
#                       "pdf": entry.get("pdf_url", ""),
#                       "pdf_local": meta.get("pdf_local"),
#                       "pdf_bytes": meta.get("pdf_bytes")},
#            "extraction": {"backend": meta.get("backend"),
#                           "model": meta.get("model"),
#                           "reasoning_effort": meta.get("effort"),
#                           "pages_in_pdf": meta.get("pages_total"),
#                           "pages_requested": meta.get("pages_requested"),
#                           "partial": bool(meta.get("partial")),
#                           "pages_extracted": len(pages),
#                           "pages_failed": meta.get("pages_failed", 0),
#                           "products_total": len(rows),
#                           "low_confidence_rows": sum(1 for r in rows if r["confidence"] < 0.9),
#                           "fields_fill_rate": {
#                               k: (round(sum(1 for r in rows if r.get(k) not in (None, "", 0, 0.0))
#                                         / len(rows), 4) if rows else 0.0)
#                               for k in ROW_KEYS},
#                           "seconds": meta.get("seconds")},
#            "validation": {"problems": meta.get("problems", []),
#                           "warnings": meta.get("warnings", [])},
#            "pages": sorted(pages, key=lambda x: x["page"]), "products": rows}
#     jpath = os.path.join(out_json_dir, entry["slug"] + ".json")
#     jlpath = os.path.join(out_json_dir, entry["slug"] + ".jsonl")
#     atomic_write_json(jpath, doc)
#     with open(jlpath, "w", encoding="utf-8") as f:
#         for r in rows:
#             f.write(json.dumps(r, ensure_ascii=False) + "\n")
#     return doc, rows, jpath, jlpath


# # ---------------------------------------------------------------------------
# # stage 5: extraction driver
# # ---------------------------------------------------------------------------
# def parse_range(spec, limit):
#     if not spec:
#         return list(range(1, limit + 1))
#     out = []
#     for part in str(spec).split(","):
#         part = part.strip()
#         if "-" in part:
#             a, b = part.split("-", 1)
#             out += list(range(int(a), int(b) + 1))
#         elif part:
#             out.append(int(part))
#     return [n for n in out if 1 <= n <= limit]


# def extract_pages(jobs, cfg, workers, force, logfile, total_pages=4):
#     work = queue.Queue()
#     for j in jobs:
#         work.put(j)
#     lock = threading.Lock()
#     state = {"done": 0, "ok": 0, "fail": 0, "abort": False, "reason": ""}
#     results = {}

#     def worker():
#         while True:
#             if state["abort"]:
#                 return
#             try:
#                 store, page_no, img, txt, dest_dir = work.get_nowait()
#             except queue.Empty:
#                 return
#             os.makedirs(dest_dir, exist_ok=True)
#             dest = os.path.join(dest_dir, "p{:02d}.json".format(page_no))
#             tag = "{} p{:02d}".format(store["slug"], page_no)
#             status, rec, note = None, None, ""
#             if not force and os.path.exists(dest):
#                 try:
#                     rec = json.load(open(dest, encoding="utf-8"))
#                     status, note = "cached", "{} products [cached]".format(rec.get("product_count", 0))
#                 except (OSError, json.JSONDecodeError):
#                     rec = None
#             if rec is None:
#                 t0 = time.time()
#                 for attempt in (1, 2):
#                     try:
#                         fn = BACKENDS[cfg["backend"]]
#                         raw = (fn(img, cfg, txt) if cfg["backend"] == "deepseek" else fn(txt, cfg))
#                         rec = normalise(raw[0], page_no, cfg["backend"], text_path=txt, total_pages=total_pages)
#                         note = "{} products [{}] {:.1f}s".format(rec["product_count"], rec["page_type"], time.time() - t0)
#                         status = "ok"
#                         break
#                     except Exception as e:
#                         if attempt == 2:
#                             status, note = "fail", "{}: {}".format(type(e).__name__, e)
#                         else:
#                             time.sleep(3)
#                 if rec is not None and status == "ok":
#                     atomic_write_json(dest, rec)
#             with lock:
#                 state["done"] += 1
#                 state["ok" + ("" if status == "fail" else "")] += 1 if status in ("ok", "cached") else 0
#                 results[page_no] = status
#                 log("  {:<22} {:>3}/{:<3} {}".format(tag, state["done"], len(jobs), note), logfile)
#             work.task_done()

#     threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(workers, len(jobs))))]
#     for t in threads:
#         t.start()
#     for t in threads:
#         t.join()
#     return results, state


# # ---------------------------------------------------------------------------
# # main CLI
# # ---------------------------------------------------------------------------
# def cmd_run(a):
#     outdir = os.path.abspath(a.out)
#     logfile = os.path.join(outdir, "run.log")
#     os.makedirs(outdir, exist_ok=True)
#     for sub in ("pdfs", "pages", "text", "pages_json", "json"):
#         os.makedirs(os.path.join(outdir, sub), exist_ok=True)

#     cfg = resolve_config(a)
#     log("drakes_au_catalogue pid={} backend={}".format(os.getpid(), a.backend), logfile)

#     cats = discover(logfile)
#     if a.stores:
#         want = {s.strip().lower() for s in a.stores.split(",")}
#         cats = [c for c in cats if c["slug"] in want or c["store"].lower() in want]

#     summary = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "stores": {}, "problems": []}
#     t_start = time.time()

#     for c in cats:
#         dest = os.path.join(outdir, "pdfs", c["slug"] + ".pdf")
#         try:
#             if not c.get("pdf_url"):
#                 flowpaper_config(c, logfile)
#             size, how = download_pdf(c, dest, logfile, force=a.force)
#             c["pdf_local"] = dest
#             c["pdf_bytes"] = size
#         except Exception as e:
#             summary["problems"].append("{}: {}".format(c["store"], e))

#         try:
#             _, total_pages, _ = render_pdf(dest, os.path.join(outdir, "pages", c["slug"]),
#                                            os.path.join(outdir, "text", c["slug"]), dpi=a.dpi, force=a.force, logfile=logfile)
#         except Exception as e:
#             total_pages = 4

#         pages_dir = os.path.join(outdir, "pages", c["slug"])
#         pj_dir = os.path.join(outdir, "pages_json", c["slug"])
#         imgs = sorted(glob.glob(os.path.join(pages_dir, "p[0-9][0-9].jpg")))
#         jobs = []
#         for img in imgs:
#             n = int(re.search(r"p(\d+)\.", os.path.basename(img)).group(1))
#             jobs.append((c, n, img, os.path.join(outdir, "text", c["slug"], "p{:02d}.txt".format(n)), pj_dir))

#         extract_pages(jobs, cfg, a.workers, a.force, logfile, total_pages=len(imgs))

#         files = sorted(glob.glob(os.path.join(pj_dir, "p[0-9][0-9].json")))
#         pages = [json.load(open(f, encoding="utf-8")) for f in files]
#         meta = {"backend": a.backend, "model": cfg.get("model"), "pages_total": len(imgs), "seconds": round(time.time() - t_start)}
#         problems, warnings = validate_store(pages, None)
#         meta["problems"], meta["warnings"] = problems, warnings
#         doc, rows, jpath, jlpath = merge_store(c, pages, os.path.join(outdir, "json"), meta)
#         summary["stores"][c["slug"]] = {"store": c["store"], "products": len(rows)}

#     atomic_write_json(os.path.join(outdir, "summary.json"), summary)
#     print("\nExtraction complete. JSON saved in:", os.path.join(outdir, "json"))
#     return 0


# def main():
#     ap = argparse.ArgumentParser(description="Drakes AU catalogues extractor")
#     ap.add_argument("--run", action="store_true")
#     ap.add_argument("--stores")
#     ap.add_argument("--out", default="./drakes")
#     ap.add_argument("--backend", default="deepseek", choices=["deepseek", "text", "none"])
#     ap.add_argument("--workers", type=int, default=4)
#     ap.add_argument("--dpi", type=int, default=200)
#     ap.add_argument("--api-key")
#     ap.add_argument("--base-url", default="https://api.openai.com/v1")
#     ap.add_argument("--model", default="deepseek-flash")
#     ap.add_argument("--max-tokens", type=int, default=8000)
#     ap.add_argument("--reasoning-effort", default="low")
#     ap.add_argument("--timeout", type=int, default=300)
#     ap.add_argument("--text-hint", action="store_true")
#     ap.add_argument("--force", action="store_true")
#     ap.add_argument("--refresh", action="store_true")
#     ap.add_argument("--strict", action="store_true")
#     a = ap.parse_args()
#     if not a.run:
#         print("Run with --run")
#         return 0
#     return cmd_run(a)


# if __name__ == "__main__":
#     sys.exit(main())










#!/usr/env python3
"""
Drakes AU (drakes.com.au) weekly catalogues -> per-store JSON, extracted with the
DeepSeek vision API + hybrid text layer.
"""
from __future__ import annotations

import argparse
import base64
import glob
import html
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CATALOGUE_PAGE = "https://drakes.com.au/catalogue/"

DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"     # the ONLY DeepSeek model with vision
DEEPSEEK_NO_VISION = ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner",
                      "deepseek-reasoner-v4", "deepseek-v3")
DEEPSEEK_MIN_TOKENS = 8000
DEEPSEEK_DEFAULT_TIMEOUT = 300

ROW_KEYS = ["name", "brand", "size", "price", "unit_price", "was_price", "save",
            "save_type", "origin", "category", "confidence", "multibuy_qty", "multibuy_price"]
NUM_KEYS = ("price", "was_price", "confidence", "multibuy_qty", "multibuy_price")
CATEGORIES = ("pantry", "dairy", "meat", "seafood", "deli", "produce", "bakery",
              "frozen", "drinks", "liquor", "household", "health", "baby", "pet",
              "general_merchandise", "other")
PAGE_TYPES = ("cover", "product_grid", "editorial", "promo", "index", "back_cover",
              "competition", "liquor", "general_merchandise")

ROW_SCHEMA = {k: (0.0 if k in NUM_KEYS else None if k == "was_price" else "") for k in ROW_KEYS}
PAGE_SCHEMA = {
    "page_type": "cover|product_grid|editorial|promo|index|back_cover|competition|general_merchandise|liquor",
    "validity": "ISO date range (YYYY-MM-DD to YYYY-MM-DD) exactly as printed on THIS page, else empty",
    "promo_headline": "main promo headline printed on the page, else empty",
    "products": [ROW_SCHEMA],
    "promos": [],
    "unreadable": ["offers visible but too small/blurry to trust"],
    "notes": "",
}

PROMPT = (
    "Extract EVERY product offer from this Australian supermarket (Drakes "
    "Supermarkets) catalogue page.\n"
    "Return ONLY raw JSON. No markdown fences, no commentary.\n"
    "Schema:\n" + json.dumps(PAGE_SCHEMA) + "\n"
    "Rules:\n"
    "- One array entry per distinct advertised offer. If one offer covers several\n"
    "  varieties, put every variety in the name exactly as printed.\n"
    "- price = the advertised shelf price in AUD as a number only (6.90, not\n"
    "  \"$6.90\"). If priced by weight put number in price and 'per kg' in size. Drop any record with price 0.\n"
    "- unit_price = small unit price as printed (\"$2.25 per 100g\"), else \"\".\n"
    "- was_price = previous price if printed. If SAVE $X is printed, was_price must equal price + X. If not printed, store null.\n"
    "- save = SAVE / half-price claim copied verbatim (\"SAVE $4\", \"1/2 PRICE\"), else \"\".\n"
    "- save_type = classify save as '$', 'percent', 'half_price', 'up_to', or ''\n"
    "- origin = extract country/region prefix like 'Australian' if present in name/brand, else ''\n"
    "- category = one of: " + ", ".join(CATEGORIES) + "\n"
    "- Vouchers, coupons, and non-product editorial mentions (e.g. self-serve coffee descriptions, hot meals vouchers) go into 'promos', NOT 'products'.\n"
    "- If an offer is multi-buy (e.g. '2 for $5'), store multibuy_qty (2) and multibuy_price (5.0), and set price to the per-unit price (2.50).\n"
    "- confidence = 0..1 confidence score.\n"
)


# ---------------------------------------------------------------------------
# small helpers & date normalization
# ---------------------------------------------------------------------------
_print_lock = threading.Lock()


class BlockedResponse(RuntimeError):
    pass


class CredentialError(RuntimeError):
    pass


def log(msg, logfile=None):
    line = "[{}] {}".format(time.strftime("%H:%M:%S"), msg)
    with _print_lock:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        if logfile:
            try:
                with open(logfile, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "unnamed"


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def normalize_date_range(text):
    if not text:
        return ""
    text_clean = re.sub(r"(st|nd|rd|th)", "", text, flags=re.IGNORECASE)
    m = re.search(r"(\d{1,2})[/\s](\d{1,2})[/\s](\d{4}).*?(\d{1,2})[/\s](\d{1,2})[/\s](\d{4})", text_clean)
    if m:
        try:
            d1, m1, y1, d2, m2, y2 = map(int, m.groups())
            dt1 = datetime(y1, m1, d1)
            dt2 = datetime(y2, m2, d2)
            return "{} to {}".format(dt1.strftime("%Y-%m-%d"), dt2.strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return text.strip()


def load_key(name, explicit=None):
    if explicit:
        return explicit
    if os.environ.get(name):
        return os.environ[name]
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env"),
              os.path.expanduser("~/.hermes/.env"),
              os.path.expanduser("~/.deepseek.env")):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(name + "="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except OSError:
                continue
    return None


def http(url, data=None, headers=None, timeout=120, tries=3, label="", logfile=None):
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP {}: {}".format(e.code, detail)
            if e.code in (401, 403):
                raise CredentialError("{} -> HTTP {} {}".format(label or url, e.code, detail))
            if e.code in (400, 404, 422):
                raise RuntimeError("{} -> HTTP {} {}".format(label or url, e.code, detail))
            if e.code == 429:
                time.sleep(min(60, 5 * (attempt + 1) + random.random() * 3))
                continue
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = "{}: {}".format(type(e).__name__, e)
            time.sleep(2 * (attempt + 1) + random.random())
    raise RuntimeError("{} failed after {} tries -> {}".format(label or url, tries, last))


# ---------------------------------------------------------------------------
# stage 1 & 2: discovery & rendering
# ---------------------------------------------------------------------------
TILE_RE = re.compile(
    r'"label"\s*:\s*"([^"]{2,80})"'
    r'.{0,600}?"link"\s*:\s*\{[^{}]{0,200}?"url"\s*:\s*"(https://([a-z0-9]+)\.flowpaper\.com/([^"/]+)/[^"]*)"'
    r'.{0,200}?"title"\s*:\s*"([^"]{1,60})"'
    r'.{0,200}?"description"\s*:\s*"([^"]{0,160})"',
    re.S)


def discover(logfile=None):
    raw = http(CATALOGUE_PAGE, headers={"User-Agent": UA}, label="drakes catalogue page",
               logfile=logfile).decode("utf-8", "replace")
    doc = html.unescape(raw).replace("\\/", "/")
    out, seen = [], set()
    for label, fp_url, sub, pub, title, desc in TILE_RE.findall(doc):
        if pub in seen:
            continue
        seen.add(pub)
        low = (title + " " + label).lower()
        out.append({
            "store": html.unescape(title).strip(),
            "slug": slugify(html.unescape(title)),
            "label": html.unescape(label).strip(),
            "state": "QLD" if "queensland" in low else "SA",
            "sub": sub,
            "pub": pub,
            "flowpaper_url": fp_url,
            "validity": normalize_date_range(html.unescape(desc).strip()),
        })
    return out


def flowpaper_config(entry, logfile=None):
    page = http(entry["flowpaper_url"], headers={"User-Agent": UA},
                label="flowpaper " + entry["pub"], logfile=logfile).decode("utf-8", "replace")

    def grab(name):
        m = re.search(name + r'\s*:\s*"([^"]*)"', page)
        return m.group(1) if m else ""

    pdf = grab("PDFFile").split("?")[0]
    img_template = grab("HighResIMGFiles") or grab("IMGFiles")
    if not pdf and not img_template:
        raise RuntimeError("flowpaper {} exposes neither PDFFile nor IMGFiles".format(entry["pub"]))
    t = re.search(r"<title>([^<]+)</title>", page)
    entry["pdf_path"] = pdf
    entry["pdf_url"] = (urllib.parse.urljoin(entry["flowpaper_url"], pdf) if pdf else "")
    entry["img_template"] = img_template
    entry["pub_title"] = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
    return entry


def download_pdf(entry, dest, logfile=None, force=False):
    if (not force and os.path.exists(dest) and os.path.getsize(dest) > 100_000):
        with open(dest, "rb") as f:
            if f.read(5).startswith(b"%PDF"):
                return os.path.getsize(dest), "cached"
    if not entry.get("pdf_url"):
        raise RuntimeError("no pdf url for {}".format(entry["store"]))
    part = dest + ".part"
    offset = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": UA, "Referer": entry["flowpaper_url"]}
    if offset:
        headers["Range"] = "bytes={}-".format(offset)
    try:
        req = urllib.request.Request(entry["pdf_url"], headers=headers)
        with urllib.request.urlopen(req, timeout=180) as r:
            if offset and r.status != 206:
                offset = 0
            mode = "ab" if offset else "wb"
            got = 0
            with open(part, mode) as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
    except urllib.error.HTTPError as e:
        raise RuntimeError("pdf download HTTP {}: {}".format(
            e.code, e.read().decode("utf-8", "replace")[:200]))
    with open(part, "rb") as f:
        magic = f.read(5)
    if not magic.startswith(b"%PDF"):
        head = open(part, "rb").read(200)
        os.remove(part)
        raise BlockedResponse("{} did not return a PDF".format(entry["pdf_url"]))
    os.replace(part, dest)
    return os.path.getsize(dest), "downloaded"


def render_pdf(pdf_path, pages_dir, text_dir, dpi=200, force=False, logfile=None):
    try:
        import pymupdf
    except ImportError:
        raise SystemExit("PDF rendering needs pymupdf:   pip install pymupdf")
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(text_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    made, text_pages, chars = [], 0, 0
    for i, page in enumerate(doc, 1):
        img = os.path.join(pages_dir, "p{:02d}.jpg".format(i))
        txt = os.path.join(text_dir, "p{:02d}.txt".format(i))
        if force or not os.path.exists(img):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(img, jpg_quality=88)
        made.append(img)
        if force or not os.path.exists(txt):
            body = page.get_text().strip()
            with open(txt, "w", encoding="utf-8") as f:
                f.write(body)
        try:
            with open(txt, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = ""
        chars += len(body)
        if len(body) > 80:
            text_pages += 1
    n = doc.page_count
    doc.close()
    log("    {} pages rendered @{}dpi, text layer on {}/{} pages".format(
        n, dpi, text_pages, n), logfile)
    return made, n, text_pages


# ---------------------------------------------------------------------------
# stage 4: backends & hybrid post-processing
# ---------------------------------------------------------------------------
def resolve_config(a):
    cfg = {"timeout": a.timeout, "model": a.model, "max_tokens": a.max_tokens,
           "base_url": a.base_url, "effort": a.reasoning_effort,
           "text_hint": a.text_hint, "backend": a.backend}
    if a.backend == "none":
        return cfg
    cfg["base_url"] = (DEEPSEEK_BASE if a.base_url == "https://api.openai.com/v1" else a.base_url)
    if a.model == "gpt-4o-mini":
        cfg["model"] = DEEPSEEK_DEFAULT_MODEL
    if cfg["max_tokens"] < DEEPSEEK_MIN_TOKENS:
        cfg["max_tokens"] = DEEPSEEK_MIN_TOKENS
    key = (a.api_key or os.environ.get("DEEPSEEK_API_KEY") or load_key("DEEPSEEK_API_KEY"))
    if not key:
        raise SystemExit("No DeepSeek API key found. Pass --api-key or set DEEPSEEK_API_KEY.")
    cfg["key"] = key
    return cfg


def _json_from_text(txt):
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    if not s:
        raise ValueError("empty model content")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError("no JSON object in model output")


def _post_chat(cfg, messages, label, max_tokens=None):
    body = {"model": cfg["model"], "messages": messages,
            "max_completion_tokens": max_tokens or cfg["max_tokens"]}
    if cfg.get("effort"):
        body["reasoning_effort"] = cfg["effort"]
    headers = {"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    raw = http(url, data=body, headers=headers, timeout=cfg["timeout"], tries=4, label=label)
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        raise BlockedResponse("chat endpoint returned non-JSON")
    ch = out.get("choices") or [{}]
    content = (ch[0].get("message") or {}).get("content", "")
    if not content.strip():
        raise ValueError("empty content")
    return _json_from_text(content), out.get("usage") or {}


def call_vision(img_path, cfg, text_path=None):
    with open(img_path, "rb") as f:
        b = base64.b64encode(f.read()).decode()
    prompt = PROMPT
    if text_path and os.path.exists(text_path):
        try:
            with open(text_path, encoding="utf-8") as f:
                hint = f.read().strip()[:2500]
        except OSError:
            hint = ""
        if hint:
            prompt += ("\nOCR HINT (text layer for spelling, validity, fine print): " + hint + "\n")
    return _post_chat(cfg, [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b, "detail": "high"}}]}],
        label=os.path.basename(img_path))


def call_text(text_path, cfg):
    try:
        with open(text_path, encoding="utf-8") as f:
            body = f.read().strip()
    except OSError:
        body = ""
    if len(body) < 40:
        return {"page_type": "editorial", "products": [], "promos": [], "unreadable": [],
                "notes": "no text layer on this page"}, {}
    prompt = (PROMPT +
              "\nThere is NO image in this request. Below is the page's raw text "
              "layer, top-to-bottom. Rebuild the offers from it, keeping each "
              "price with the product name it is printed next to.\nTEXT:\n" + body[:12000])
    return _post_chat(cfg, [{"role": "user", "content": prompt}],
                      label=os.path.basename(text_path))


BACKENDS = {"deepseek": call_vision, "text": call_text}


# ---------------------------------------------------------------------------
# normalise + validate
# ---------------------------------------------------------------------------
def normalise(rec, page_no, backend):
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("page_type") not in PAGE_TYPES:
        rec["page_type"] = "product_grid"

    # Support parsing both old 'offers' or separated 'products' and 'promos'
    raw_offers = rec.get("offers") or []
    rec_products = list(rec.get("products") or [])
    promos = list(rec.get("promos") or [])

    for off in raw_offers:
        if isinstance(off, dict):
            # If an offer has a priced item (like coffee), treat it as a product
            p_val = off.get("price")
            if p_val is not None and float(str(p_val).replace("$", "") or 0) > 0:
                rec_products.append(off)
            else:
                promos.append(off)
        else:
            promos.append({"offer": str(off)})

    rows = []
    for pr in rec_products:
        if not isinstance(pr, dict):
            continue
        row = {}
        for k in ROW_KEYS:
            v = pr.get(k, 0.0 if k in NUM_KEYS else None if k == "was_price" else "")
            if k in NUM_KEYS:
                try:
                    v = float(str(v).replace("$", "").replace(",", "").strip() or 0)
                except ValueError:
                    v = 0.0 if k != "was_price" else None
            else:
                v = str(v or "").strip() if v is not None else ""
            row[k] = v

        # Rule: Drop any record with price 0
        if row["price"] == 0.0:
            continue

        # Rule: Multi-buy deals (e.g. "2 for $5")
        # Rule: Multi-buy deals (e.g. "2 for $5")
        combined_text = (row["name"] + " " + row["save"] + " " + str(pr.get("notes", ""))).lower()
        mb_match = re.search(r"(\d+)\s+for\s+\$?([\d\.]+)", combined_text)
        if mb_match:
            qty = int(mb_match.group(1))
            tot_price = float(mb_match.group(2))
            row["multibuy_qty"] = qty
            row["multibuy_price"] = tot_price
            row["price"] = tot_price  # Keep total price for multi-buy deals
        elif row.get("multibuy_qty", 0) > 0 and row.get("multibuy_price", 0) > 0:
            row["price"] = row["multibuy_price"]  # Keep total price for multi-buy deals

        if not row["name"]:
            continue

        cat = row["category"].lower().replace(" ", "_").replace("&", "and")
        row["category"] = cat if cat in CATEGORIES else "other"
        row["confidence"] = min(1.0, max(0.0, row["confidence"]))

        if row["was_price"] is not None and row["was_price"] > 0:
            if abs(row["was_price"] - row["price"]) < 1e-9 or row["was_price"] < row["price"]:
                row["was_price"] = None

        rows.append(row)

    rec["products"] = rows
    rec["promos"] = promos
    rec.pop("offers", None)
    rec["product_count"] = len(rows)
    rec["promo_count"] = len(promos)
    rec["page"] = page_no
    rec["backend"] = backend
    for k in ("unreadable", "notes", "validity", "promo_headline"):
        if k == "unreadable":
            rec[k] = [str(u) for u in (rec.get(k) or [])]
        else:
            rec[k] = str(rec.get(k) or "")
    return rec


def validate_store(pages, pages_expected):
    problems, warnings = [], []
    got = sorted(p["page"] for p in pages)
    if pages_expected is not None:
        want = {int(n) for n in pages_expected}
        missing = sorted(want - set(got))
        if missing:
            problems.append("missing pages: {}".format(missing[:30]))
    dupes = sorted(n for n in set(got) if got.count(n) > 1)
    if dupes:
        problems.append("duplicate page records: {}".format(dupes))
    for p in pages:
        if not p["products"] and p["page_type"] not in ("cover", "index", "back_cover", "editorial", "promo"):
            warnings.append("page {} ({}) has no priced products".format(p["page"], p["page_type"]))
        for r in p["products"]:
            if r["price"] == 0:
                problems.append("page {}: price 0 found - {}".format(p["page"], r["name"][:45]))
    return problems, warnings


def merge_store(entry, pages, out_json_dir, meta):
    rows = []
    promos = []
    for p in sorted(pages, key=lambda x: x["page"]):
        for r in p["products"]:
            rows.append({"store": entry["store"], "store_slug": entry["slug"],
                         "page": p["page"], "page_type": p["page_type"],
                         "catalogue_validity": p["validity"],
                         "promo_headline": p["promo_headline"], **r})
        for pr in p.get("promos", []):
            # Safeguard: handle if promo is a string instead of a dictionary
            if isinstance(pr, str):
                pr_dict = {"offer": pr}
            elif isinstance(pr, dict):
                pr_dict = pr
            else:
                pr_dict = {"offer": str(pr)}
                
            promos.append({"store": entry["store"], "store_slug": entry["slug"],
                           "page": p["page"], "page_type": p["page_type"],
                           "catalogue_validity": p["validity"], **pr_dict})

    doc = {"store": entry["store"], "slug": entry["slug"], "state": entry["state"],
           "label": entry["label"], "catalogue_validity": entry["validity"],
           "source": {"catalogue_page": CATALOGUE_PAGE,
                      "flowpaper": entry["flowpaper_url"],
                      "pdf": entry.get("pdf_url", ""),
                      "pdf_local": meta.get("pdf_local"),
                      "pdf_bytes": meta.get("pdf_bytes")},
           "extraction": {"backend": meta.get("backend"),
                          "model": meta.get("model"),
                          "pages_extracted": len(pages),
                          "products_total": len(rows),
                          "promos_total": len(promos),
                          "seconds": meta.get("seconds")},
           "validation": {"problems": meta.get("problems", []),
                          "warnings": meta.get("warnings", [])},
           "pages": sorted(pages, key=lambda x: x["page"]), 
           "products": rows,
           "promos": promos}
    jpath = os.path.join(out_json_dir, entry["slug"] + ".json")
    jlpath = os.path.join(out_json_dir, entry["slug"] + ".jsonl")
    atomic_write_json(jpath, doc)
    with open(jlpath, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return doc, rows, jpath, jlpath

# ---------------------------------------------------------------------------
# stage 5: extraction driver
# ---------------------------------------------------------------------------
def parse_range(spec, limit):
    if not spec:
        return list(range(1, limit + 1))
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return [n for n in out if 1 <= n <= limit]


def extract_pages(jobs, cfg, workers, force, logfile):
    work = queue.Queue()
    for j in jobs:
        work.put(j)
    lock = threading.Lock()
    state = {"done": 0, "ok": 0, "fail": 0, "abort": False, "reason": ""}

    def worker():
        while True:
            if state["abort"]:
                return
            try:
                store, page_no, img, txt, dest_dir = work.get_nowait()
            except queue.Empty:
                return
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, "p{:02d}.json".format(page_no))
            tag = "{} p{:02d}".format(store["slug"], page_no)
            status, rec, note = None, None, ""
            if not force and os.path.exists(dest):
                try:
                    rec = json.load(open(dest, encoding="utf-8"))
                    status, note = "cached", "{} products [cached]".format(rec.get("product_count", 0))
                except (OSError, json.JSONDecodeError):
                    rec = None
            if rec is None:
                t0 = time.time()
                for attempt in (1, 2):
                    try:
                        fn = BACKENDS[cfg["backend"]]
                        raw = (fn(img, cfg, txt) if cfg["backend"] == "deepseek"
                                else fn(txt, cfg))
                        rec = normalise(raw[0], page_no, cfg["backend"])
                        note = "{} products [{}] {:.1f}s".format(
                            rec["product_count"], rec["page_type"], time.time() - t0)
                        status = "ok"
                        break
                    except (CredentialError, BlockedResponse) as e:
                        status, note = "fail", "{}: {}".format(type(e).__name__, e)
                        break
                    except Exception as e:
                        if attempt == 2:
                            status, note = "fail", "{}: {}".format(type(e).__name__, e)
                        else:
                            time.sleep(3)
                if rec is not None and status == "ok":
                    atomic_write_json(dest, rec)
            with lock:
                state["done"] += 1
                state["ok" if status in ("ok", "cached") else "fail"] += 1
                log("  {:<22} {:>3}/{:<3} {}".format(tag, state["done"], len(jobs), note), logfile)
            work.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(workers, len(jobs))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {}, state


def cmd_run(a):
    outdir = os.path.abspath(a.out)
    logfile = os.path.join(outdir, "run.log")
    os.makedirs(outdir, exist_ok=True)
    for sub in ("pdfs", "pages", "text", "pages_json", "json"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    cfg = resolve_config(a)
    cats = discover(logfile)
    if a.stores:
        want = {s.strip().lower() for s in a.stores.split(",")}
        cats = [c for c in cats if c["slug"] in want or c["store"].lower() in want]

    t_start = time.time()
    for c in cats:
        if not c.get("pdf_url"):
            flowpaper_config(c, logfile)
        dest = os.path.join(outdir, "pdfs", c["slug"] + ".pdf")
        size, _ = download_pdf(c, dest, logfile, force=a.force)
        c["pdf_local"] = dest
        c["pdf_bytes"] = size

        render_pdf(dest, os.path.join(outdir, "pages", c["slug"]),
                   os.path.join(outdir, "text", c["slug"]), dpi=a.dpi, force=a.force, logfile=logfile)

    jobs = []
    for c in cats:
        pages_dir = os.path.join(outdir, "pages", c["slug"])
        pj_dir = os.path.join(outdir, "pages_json", c["slug"])
        imgs = sorted(glob.glob(os.path.join(pages_dir, "p[0-9][0-9].jpg")))
        want = set(parse_range(a.pages, len(imgs)))
        for img in imgs:
            n = int(re.search(r"p(\d+)\.", os.path.basename(img)).group(1))
            if n in want:
                jobs.append((c, n, img, os.path.join(outdir, "text", c["slug"], "p{:02d}.txt".format(n)), pj_dir))

    if jobs:
        extract_pages(jobs, cfg, a.workers, a.force, logfile)

    for c in cats:
        pj = os.path.join(outdir, "pages_json", c["slug"])
        files = sorted(glob.glob(os.path.join(pj, "p[0-9][0-9].json")))
        pages = [json.load(open(f, encoding="utf-8")) for f in files]
        meta = {"backend": a.backend, "model": cfg.get("model"), "seconds": round(time.time() - t_start)}
        merge_store(c, pages, os.path.join(outdir, "json"), meta)

    print("\nExtraction completed successfully. Output saved in {}".format(outdir))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--stores")
    ap.add_argument("--pages")
    ap.add_argument("--out", default="./drakes")
    ap.add_argument("--backend", default="deepseek", choices=["deepseek", "text", "none"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--api-key")
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--timeout", type=int, default=DEEPSEEK_DEFAULT_TIMEOUT)
    ap.add_argument("--text-hint", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not a.run:
        print("Use --run to execute extraction.")
        return 0
    return cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())