# #!/usr/bin/env python3
# """
# Foodland AU weekly catalogue -> structured JSON.  Complete pipeline.

#     harvest  : pull the current weekly catalogue page scans off the aggregator
#     extract  : vision/OCR each page into structured product records
#     validate : assert the output actually holds together
#     merge    : write foodland_products.json + .jsonl

# WHY THIS IS NOT A PDF PIPELINE
# ------------------------------
# foodlandsa.com.au/catalogue/ is a WordPress page holding three promo teaser
# JPGs. There is no PDF anywhere on it (grep -ci pdf == 0) and no priced flyer.
# Foodland is not on Flipp either (Flipp is North America only). The full
# 40-page weekly catalogue is republished by latestcatalogues.com (kiim /
# leafletscdn) as full-resolution scans -- that is what this script reads.

# THE ONE THING THAT WILL BITE YOU
# --------------------------------
# The per-page image URL carries an HMAC signature you cannot compute:

#     https://na.leafletscdn.com/thumbor/<HMAC>/0x0/filters:format(webp):quality(65)/com.au/data/<shop>/<leaflet>/<n>.jpg

# So you must scrape the signed URL from the viewer endpoint, then download it
# verbatim. Stripping the thumbor prefix and hitting /com.au/data/... directly
# 404s, and changing quality(65)/0x0 breaks the signature. The HMAC ends with
# '=' -- a [A-Za-z0-9_-]+ regex silently misses it and every page reports
# "no image URL".

# USAGE
# -----
#   # 0. ONLY HAVE A DEEPSEEK KEY? This is your one-liner:
#   python foodland_aus.py --backend deepseek --api-key YOUR_KEY
#   #    model must stay deepseek-flash (the script defaults to it) --
#   #    deepseek-v4-pro is text-only and will return 0 products per page.

#   # 1. list catalogues live right now (ids change weekly - never hardcode)
#   python foodland_aus.py --list

#   # 2. harvest pages + extract + validate + merge, in one go
#   python foodland_aus.py --out ./foodland --backend deepseek \
#          --api-key YOUR_KEY --pages 1-40

#   # 3. re-extract from pages you already downloaded (skips the download)
#   python foodland_aus.py --input ./foodland/pages --out ./foodland

#   # 4. extract from a real flyer PDF instead (needs: pip install pymupdf)
#   python foodland_aus.py --input flyer.pdf --out ./out --backend deepseek --api-key KEY

#   # 5. offline, no API, degraded text-only output
#   python foodland_aus.py --input ./pages --backend tesseract

# BACKENDS
# --------
#   deepseek   DeepSeek's own API. NO EXTRA KEY NEEDED IF YOU ALREADY HAVE ONE.
#              Model MUST be deepseek-flash -- it reads images. deepseek-v4-pro,
#              deepseek-chat and deepseek-reasoner are TEXT-ONLY and will reply
#              "I can't view the image". deepseek-flash is a reasoning model, so
#              it spends completion tokens thinking: keep max tokens high (the
#              script raises it to 8000 automatically) or the answer comes back
#              empty with every token burned on reasoning.
#                python foodland_aus.py --backend deepseek --api-key YOUR_KEY
#              Verified: 39/39 prices correct vs a vision-model reference across
#              pages 3, 12, 28, 40.
#   fal        fal.ai any-llm/vision. Needs FAL_KEY. ~5s/page.
#   openai     ANY OpenAI-compatible /chat/completions endpoint. Use --base-url
#              to point at OpenAI, Gemini, Groq, or a local Ollama / vLLM:
#                --backend openai --base-url http://localhost:11434/v1 \
#                  --model qwen2.5vl:7b --api-key ollama
#              Gemini (free tier, good vision):
#                --backend openai \
#                  --base-url https://generativelanguage.googleapis.com/v1beta/openai \
#                  --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY
#   tesseract  Fully offline OCR. NO structured fields -- it returns raw text
#              lines only, price/unit-price parsing is heuristic and lossy.
#              Use as a fallback or for cross-checking, not as your primary.

#   Check your lane actually works before planning a batch: a successful
#   GET /v1/models tells you nothing about billing credits. Make one real call.
# """
# from __future__ import annotations

# import argparse
# import base64
# import concurrent.futures as futures
# import glob
# import json
# import os
# import re
# import subprocess
# import sys
# import time
# import urllib.error
# import urllib.request

# # --------------------------------------------------------------------------
# # config
# # --------------------------------------------------------------------------
# UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
#       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# AGG_BASE = "https://www.latestcatalogues.com"
# LIST_URL = AGG_BASE + "/foodland/"
# FAL_URL = "https://fal.run/fal-ai/any-llm/vision"

# HREF_RE = re.compile(r'href="(/foodland/foodland-catalogue-[^"]+)"')
# IMG_RE = re.compile(                      # NOTE the '=' -- signature ends with it
#     r'https://na\.leafletscdn\.com/thumbor/[A-Za-z0-9_\-=]+/0x0/'
#     r'filters:format\(webp\):quality\(\d+\)/com\.au/data/\d+/\d+/\d+\.jpg[^"\'\\ ]*')
# VAR_RE = lambda n: re.compile(r'var\s+' + n + r'\s*=\s*"?(\d+)"?')   # noqa: E731

# CATEGORIES = {"pantry", "dairy", "meat", "produce", "bakery", "frozen",
#               "drinks", "liquor", "household", "health", "pet", "other"}
# PAGE_TYPES = {"cover", "product_grid", "editorial", "promo", "index",
#               "back_cover"}
# ROW_KEYS = ["name", "brand", "size", "price", "unit_price", "was_price",
#             "save", "category", "confidence"]

# # The schema we ask the model for. Built as a dict so there is no brace
# # escaping anywhere near a format string.
# ROW_SCHEMA = {k: (0.0 if k in ("price", "was_price", "confidence") else "")
#               for k in ROW_KEYS}
# ROW_SCHEMA["category"] = "pantry|dairy|meat|produce|bakery|frozen|drinks|liquor|household|health|pet|other"
# PAGE_SCHEMA = {"page_type": "cover|product_grid|editorial|promo|index|back_cover",
#                "validity": "date range exactly as printed, else empty",
#                "promo_headline": "main promo text on the page, else empty",
#                "products": [ROW_SCHEMA],
#                "unreadable": ["offers visible but too small/blurry to trust"],
#                "notes": ""}

# PROMPT = (
#     "Extract EVERY product offer on this Australian supermarket (Foodland SA) "
#     "catalogue page.\n"
#     "Return ONLY raw JSON. No markdown fences, no commentary.\n"
#     "Schema:\n" + json.dumps(PAGE_SCHEMA) + "\n"
#     "Rules:\n"
#     "- One array entry per distinct offer. If one offer covers several "
#     "varieties, put every variety in the name field exactly as printed.\n"
#     "- price = advertised price in AUD as a number only (3.75, not \"$3.75\"). "
#     "Use 0 if it cannot be read.\n"
#     "- If priced by weight, put the number in price and 'per kg' in size.\n"
#     "- was_price: fill ONLY if a was/previous price is actually printed on the "
#     "page. Flyers usually print a SAVE UP TO figure or a HALF PRICE tag instead "
#     "- do NOT reconstruct a was price from those. Leave 0.\n"
#     "- unit_price and save: copy verbatim as printed, else empty.\n"
#     "- Never invent products or prices. Anything too small or blurry to read "
#     "confidently goes in unreadable, and that row's confidence drops below 0.5.\n"
#     "- validity: copy a date range ONLY if this page prints one.\n"
# )

# VALID_BACKENDS = ("fal", "openai", "deepseek", "tesseract")

# # DeepSeek: deepseek-flash HAS vision, deepseek-v4-pro does NOT (it replies
# # "I can't view the image"). deepseek-flash is a reasoning model, so it burns
# # completion tokens thinking before it answers -- give it a large budget or the
# # content comes back empty with all tokens spent on reasoning.
# DEEPSEEK_BASE = "https://api.deepseek.com/v1"
# DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
# DEEPSEEK_NO_VISION = ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner")
# DEEPSEEK_MIN_TOKENS = 8000


# # --------------------------------------------------------------------------
# # env / keys
# # --------------------------------------------------------------------------
# def load_key(name, explicit=None):
#     """explicit arg > environment > .env files near cwd/script/home."""
#     if explicit:
#         return explicit
#     if os.environ.get(name):
#         return os.environ[name]
#     here = os.path.dirname(os.path.abspath(__file__))
#     for p in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env"),
#               os.path.expanduser(".hermes/.env"),
#               os.path.expanduser("~/.hermes/.env")):
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


# def resolve_backend_config(a):
#     """Resolve the vision lane BEFORE downloading anything.

#     Fail fast: the original version harvested all 40 pages and only then
#     complained that no key was configured. Check here instead.
#     """
#     cfg = {"timeout": a.timeout, "model": a.model, "max_tokens": a.max_tokens,
#            "base_url": a.base_url, "tesseract_exe": a.tesseract_exe}
#     if a.backend == "tesseract":
#         return cfg
#     if a.backend == "fal":
#         key = load_key("FAL_KEY", a.fal_key)
#         if not key:
#             raise SystemExit(
#                 "No FAL_KEY found.\n"
#                 "  Option 1 - fal.ai key (paid, ~5s/page):\n"
#                 "      python foodland_aus.py --backend fal --fal-key YOUR_KEY\n"
#                 "  Option 2 - FREE Google AI Studio key, same script, different lane:\n"
#                 "      python foodland_aus.py --backend openai \\\n"
#                 "        --base-url https://generativelanguage.googleapis.com/v1beta/openai \\\n"
#                 "        --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY\n"
#                 "  Option 3 - no key at all, fully offline (degraded, text-only output):\n"
#                 "      python foodland_aus.py --backend tesseract\n"
#                 "  Key already downloaded pages are kept - any of the above resumes from them.")
#         cfg["key"] = key
#         return cfg
#     if a.backend == "deepseek":
#         cfg["base_url"] = a.base_url if a.base_url != "https://api.openai.com/v1" \
#             else DEEPSEEK_BASE
#         if a.model == "gpt-4o-mini":
#             cfg["model"] = DEEPSEEK_DEFAULT_MODEL
#         if any(m in cfg["model"] for m in DEEPSEEK_NO_VISION):
#             print(f"  WARNING: {cfg['model']} has NO vision - it will refuse the image.")
#             print(f"           use {DEEPSEEK_DEFAULT_MODEL} instead.")
#         if cfg["max_tokens"] < DEEPSEEK_MIN_TOKENS:
#             print(f"  raising max tokens {cfg['max_tokens']} -> {DEEPSEEK_MIN_TOKENS} "
#                   f"(reasoning model eats the budget before answering)")
#             cfg["max_tokens"] = DEEPSEEK_MIN_TOKENS
#         key = (a.api_key or os.environ.get("DEEPSEEK_API_KEY")
#                or load_key("DEEPSEEK_API_KEY"))
#         if not key:
#             raise SystemExit(
#                 "No DeepSeek key found.\n"
#                 "  Pass --api-key, or set DEEPSEEK_API_KEY in your environment.\n"
#                 "      python foodland_aus.py --backend deepseek --api-key YOUR_KEY\n"
#                 "  Model note: deepseek-flash reads images, deepseek-v4-pro does not.")
#         cfg["key"] = key
#         return cfg
#     # openai-compatible
#     key = (a.api_key or os.environ.get("VISION_API_KEY")
#            or os.environ.get("GEMINI_API_KEY")
#            or load_key("OPENAI_API_KEY"))
#     if not key:
#         raise SystemExit(
#             "No API key for the openai-compatible backend.\n"
#             "  Pass --api-key, or set GEMINI_API_KEY / OPENAI_API_KEY / VISION_API_KEY.\n"
#             "  Free option: create a key at https://aistudio.google.com/apikey then:\n"
#             "      python foodland_aus.py --backend openai \\\n"
#             "        --base-url https://generativelanguage.googleapis.com/v1beta/openai \\\n"
#             "        --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY\n"
#             "  Or point --base-url at a local Ollama (no key needed):\n"
#             "      --base-url http://localhost:11434/v1 --model qwen2.5vl:7b --api-key ollama")
#     cfg["key"] = key
#     return cfg


# def http(url, data=None, headers=None, timeout=180, tries=3, label=""):
#     body = json.dumps(data).encode() if data is not None else None
#     last = None
#     for a in range(tries):
#         try:
#             req = urllib.request.Request(url, data=body, headers=headers or {})
#             with urllib.request.urlopen(req, timeout=timeout) as r:
#                 return r.read()
#         except urllib.error.HTTPError as e:
#             detail = e.read().decode("utf-8", "replace")[:300]
#             last = f"HTTP {e.code}: {detail}"
#             # 400/422 = bad payload, retrying identical bytes won't help
#             if e.code in (400, 401, 403, 404, 422):
#                 break
#         except Exception as e:                                     # noqa: BLE001
#             last = f"{type(e).__name__}: {e}"
#         time.sleep(2 * (a + 1))
#     raise RuntimeError(f"{label or url} failed -> {last}")


# # --------------------------------------------------------------------------
# # stage 1: harvest
# # --------------------------------------------------------------------------
# def discover():
#     """[{'leaflet_id','shop_id','pages','url','title'}, ...] -- ids change weekly."""
#     html = http(LIST_URL, headers={"User-Agent": UA}).decode("utf-8", "replace")
#     out, seen = [], set()
#     for href in HREF_RE.findall(html):
#         if href in seen:
#             continue
#         seen.add(href)
#         page = http(AGG_BASE + href, headers={"User-Agent": UA}).decode("utf-8", "replace")
#         lid, sid, pg = (VAR_RE("pageBrochureId").search(page),
#                         VAR_RE("currentShopId").search(page),
#                         VAR_RE("maxPages").search(page))
#         if not (lid and sid and pg):
#             continue
#         t = re.search(r"<title>([^<]+)</title>", page)
#         out.append({"leaflet_id": int(lid.group(1)), "shop_id": int(sid.group(1)),
#                     "pages": int(pg.group(1)), "url": AGG_BASE + href,
#                     "title": re.sub(r"\s+", " ", t.group(1)).strip()[:110] if t else ""})
#     return out


# def signed_page_url(leaflet_id, page_no, catalogue_url):
#     """The HMAC is per-URL; scrape it, never compute it. 400s on POST."""
#     q = (f"{AGG_BASE}/brochure/brochure-page/?brochureId={leaflet_id}"
#          f"&page={page_no}&current_url={catalogue_url}")
#     html = http(q, headers={"User-Agent": UA, "Referer": catalogue_url}).decode("utf-8", "replace")
#     m = IMG_RE.search(html)
#     return None if not m else m.group(0).replace("&amp;", "&")


# def harvest(leaflet_id, pages, catalogue_url, outdir, sleep=0.4):
#     """Resumable: existing files >20KB are kept, so a killed run just continues."""
#     os.makedirs(outdir, exist_ok=True)
#     saved = []
#     for p in range(1, pages + 1):
#         dest = os.path.join(outdir, f"p{p:02d}.jpg")
#         if os.path.exists(dest) and os.path.getsize(dest) > 20_000:
#             print(f"  p{p:02d} cached")
#             saved.append(dest)
#             continue
#         try:
#             url = signed_page_url(leaflet_id, p, catalogue_url)
#             if not url:
#                 print(f"  p{p:02d} NO IMAGE URL (regex missing the signature?)")
#                 continue
#             blob = http(url, headers={"User-Agent": UA, "Referer": catalogue_url})
#             if len(blob) < 20_000:
#                 print(f"  p{p:02d} too small ({len(blob)}B) - skipped")
#                 continue
#             with open(dest, "wb") as f:
#                 f.write(blob)
#             saved.append(dest)
#             print(f"  p{p:02d} ok {len(blob) // 1024}KB")
#         except Exception as e:                                     # noqa: BLE001
#             print(f"  p{p:02d} FAIL {e}")
#         time.sleep(sleep)
#     with open(os.path.join(outdir, "_manifest.json"), "w") as f:
#         json.dump({"leaflet_id": leaflet_id, "pages": len(saved),
#                    "catalogue_url": catalogue_url, "files": saved}, f, indent=2)
#     return saved


# def pdf_to_pages(pdf_path, outdir, dpi=200):
#     """Real flyer PDFs: keep the text layer when there is one, always rasterise."""
#     try:
#         import pymupdf                                            # noqa: PLC0415
#     except ImportError:
#         raise SystemExit("PDF input needs pymupdf:  pip install pymupdf")
#     os.makedirs(outdir, exist_ok=True)
#     doc = pymupdf.open(pdf_path)
#     out, text_pages = [], 0
#     for i, page in enumerate(doc):
#         txt = page.get_text().strip()
#         if len(txt) > 80:
#             text_pages += 1
#             with open(os.path.join(outdir, f"p{i + 1:02d}.txt"), "w", encoding="utf-8") as f:
#                 f.write(txt)
#         p = os.path.join(outdir, f"p{i + 1:02d}.jpg")
#         page.get_pixmap(dpi=dpi).save(p)
#         out.append(p)
#     avg = sum(len(open(os.path.join(outdir, f"p{i + 1:02d}.txt"), encoding="utf-8").read())
#               for i in range(text_pages)) if text_pages else 0
#     print(f"  PDF: {doc.page_count} pages, {text_pages} with a text layer"
#           f"{' (avg %d chars)' % (avg // text_pages) if text_pages else ''}")
#     if text_pages == doc.page_count:
#         print("  -> text layer everywhere: the .txt files already contain the data,"
#               " vision is optional here")
#     return out


# # --------------------------------------------------------------------------
# # stage 2: backends
# # --------------------------------------------------------------------------
# def b64(path):
#     with open(path, "rb") as f:
#         return base64.b64encode(f.read()).decode()


# def _json_from_text(txt):
#     """Models wrap JSON in fences, prose, or both. Dig it out."""
#     s = (txt or "").strip()
#     s = re.sub(r"^```(?:json)?\s*", "", s)
#     s = re.sub(r"\s*```$", "", s).strip()
#     try:
#         return json.loads(s)
#     except json.JSONDecodeError:
#         pass
#     start, end = s.find("{"), s.rfind("}")
#     if start != -1 and end > start:
#         return json.loads(s[start:end + 1])
#     raise ValueError("no JSON object in model output")


# def call_fal(img_path, cfg):
#     body = {"prompt": PROMPT, "image_url": "data:image/jpeg;base64," + b64(img_path)}
#     raw = http(FAL_URL, data=body, timeout=cfg["timeout"], label="fal",
#                headers={"Authorization": f"Key {cfg['key']}",
#                         "Content-Type": "application/json"})
#     env = json.loads(raw)
#     if env.get("error"):
#         raise RuntimeError(f"fal error: {env['error']}")
#     return _json_from_text(env.get("output", ""))


# def call_openai(img_path, cfg):
#     """Any OpenAI-compatible /chat/completions: OpenAI, OpenRouter, Ollama, vLLM."""
#     messages = [{"role": "user", "content": [
#         {"type": "text", "text": PROMPT},
#         {"type": "image_url", "image_url": {
#             "url": "data:image/jpeg;base64," + b64(img_path), "detail": "high"}}]}]
#     base = {"model": cfg["model"], "max_completion_tokens": cfg["max_tokens"],
#             "messages": messages}
#     headers = {"Authorization": f"Bearer {cfg['key']}",
#                "Content-Type": "application/json"}
#     url = cfg["base_url"].rstrip("/") + "/chat/completions"
#     # response_format is widely supported but not universal. Try it, and only
#     # fall back when the SERVER rejects the payload -- never when our own code
#     # is broken (a broad except here once hid a bug for two "retries" and let a
#     # zero-call run look like a server incompatibility).
#     variants = ({"response_format": {"type": "json_object"}}, {})
#     for i, extra in enumerate(variants):
#         try:
#             raw = http(url, data={**base, **extra}, headers=headers,
#                        timeout=cfg["timeout"], tries=1, label="openai")
#             out = json.loads(raw)
#             return _json_from_text(out["choices"][0]["message"]["content"])
#         except Exception as e:                                     # noqa: BLE001
#             msg = str(e)
#             server_rejected = ("HTTP 400" in msg or "HTTP 422" in msg
#                                or "response_format" in msg or "json_object" in msg)
#             if i == 0 and server_rejected:
#                 print(f"    (endpoint rejects response_format, retrying plain: {msg[:120]})")
#                 continue
#             raise


# def call_tesseract(img_path, cfg):
#     """Offline OCR. No structured extraction -- honest, lossy text only."""
#     exe = cfg.get("tesseract_exe") or "tesseract"
#     try:
#         r = subprocess.run([exe, img_path, "stdout", "--psm", "6"],
#                            capture_output=True, text=True, timeout=180)
#     except FileNotFoundError:
#         raise SystemExit("tesseract not found. macOS: brew install tesseract | "
#                          "Windows: choco install tesseract | "
#                          "or use --backend fal/openai")
#     text = r.stdout
#     products, seen = [], set()
#     # heuristic: a line with a $ amount is probably an offer line
#     for line in (l.strip() for l in text.splitlines()):
#         if len(line) < 6 or not re.search(r"\$\s?\d", line):
#             continue
#         if line in seen:
#             continue
#         seen.add(line)
#         names = re.sub(r"\$\s?\d+[.,]?\d*.*$", "", line).strip(" .|-")
#         nums = re.findall(r"\$\s?(\d+(?:[.,]\d{2})?)", line)
#         products.append({"name": names or line, "brand": "", "size": "",
#                          "price": float(nums[0].replace(",", ".")) if nums else 0.0,
#                          "unit_price": "", "was_price": 0.0, "save": "",
#                          "category": "other", "confidence": 0.3})
#     return {"page_type": "product_grid", "validity": "", "promo_headline": "",
#             "products": products,
#             "unreadable": ["tesseract backend: unstructured output, every row "
#                            "is a low-confidence heuristic guess"],
#             "notes": "raw tesseract text kept alongside in _ocr_text"}


# BACKENDS = {"fal": call_fal, "openai": call_openai, "deepseek": call_openai,
#             "tesseract": call_tesseract}


# # --------------------------------------------------------------------------
# # stage 3: normalise + validate
# # --------------------------------------------------------------------------
# def normalise(rec, page_no, backend):
#     rec = rec if isinstance(rec, dict) else {}
#     if rec.get("page_type") not in PAGE_TYPES:
#         rec["page_type"] = "product_grid"
#     valid = []
#     for pr in rec.get("products") or []:
#         if not isinstance(pr, dict):
#             continue
#         row = {}
#         for k in ROW_KEYS:
#             v = pr.get(k, ROW_SCHEMA[k])
#             if k in ("price", "was_price", "confidence"):
#                 try:
#                     v = float(str(v).replace("$", "").replace(",", "").strip() or 0)
#                 except ValueError:
#                     v = 0.0
#             else:
#                 v = str(v or "").strip()
#             row[k] = v
#         if not row["name"]:
#             continue
#         if row["category"] not in CATEGORIES:
#             row["category"] = "other"
#         row["confidence"] = min(1.0, max(0.0, row["confidence"]))
#         # did the model just echo the price back into was_price?
#         if row["was_price"] and abs(row["was_price"] - row["price"]) < 1e-9:
#             row["was_price"] = 0.0
#         valid.append(row)
#     rec["products"] = valid
#     rec["product_count"] = len(valid)
#     rec["page"] = page_no
#     rec["backend"] = backend
#     rec["unreadable"] = [str(u) for u in (rec.get("unreadable") or [])]
#     rec["notes"] = str(rec.get("notes") or "")
#     rec["validity"] = str(rec.get("validity") or "")
#     rec["promo_headline"] = str(rec.get("promo_headline") or "")
#     return rec


# def validate(pages, expected=None):
#     """expected = the page numbers that SHOULD be present (a subset is fine)."""
#     problems, warnings = [], []
#     nos = sorted(p["page"] for p in pages)
#     if expected is not None:
#         want = set(int(n) for n in expected)
#         miss = sorted(want - set(nos))
#         if miss:
#             problems.append(f"missing pages: {miss}")
#     dupes = [n for n in set(nos) if nos.count(n) > 1]
#     if dupes:
#         problems.append(f"duplicate page records: {sorted(dupes)}")
#     seen = set()
#     for p in pages:
#         for r in p["products"]:
#             for k in ROW_KEYS:
#                 if k not in r:
#                     problems.append(f"p{p['page']} row missing {k}")
#                     break
#             if list(r.keys()) != ROW_KEYS:
#                 extra = [k for k in r if k not in ROW_KEYS]
#                 problems.append(f"p{p['page']} row has unexpected keys {extra}")
#             if r["category"] not in CATEGORIES:
#                 problems.append(f"p{p['page']} bad category {r['category']!r}")
#             if not isinstance(r["price"], float):
#                 problems.append(f"p{p['page']} price not numeric: {r['name'][:40]}")
#             if r["price"] == 0 and "competition" not in p["notes"].lower():
#                 warnings.append(f"p{p['page']} price 0 (no price printed?): {r['name'][:45]}")
#             key = (p["page"], r["name"])
#             if key in seen:
#                 problems.append(f"p{p['page']} duplicate product {r['name'][:40]!r}")
#             seen.add(key)
#     for p in pages:
#         if not p["products"]:
#             problems.append(
#                 f"page {p['page']} produced 0 products - did the model refuse the "
#                 f"image? (wrong model, or a genuine non-product page)")
#         if p["unreadable"]:
#             warnings.append(f"page {p['page']} needs review: {len(p['unreadable'])} unreadable")
#     low = sum(1 for p in pages for r in p["products"] if r["confidence"] < 0.9)
#     return problems, warnings, low


# def merge(pages, outdir, meta):
#     rows = [{"page": p["page"], "page_type": p["page_type"],
#              "catalogue_validity": p["validity"],
#              "promo_headline": p["promo_headline"], **r}
#             for p in sorted(pages, key=lambda x: x["page"]) for r in p["products"]]
#     agg = {"source": meta,
#            "extraction": {"backend": meta.get("backend"),
#                           "pages_extracted": len(pages),
#                           "products_total": len(rows)},
#            "pages": sorted(pages, key=lambda x: x["page"]), "products": rows}
#     j = os.path.join(outdir, "foodland_products.json")
#     jl = os.path.join(outdir, "foodland_products.jsonl")
#     with open(j, "w", encoding="utf-8") as f:
#         json.dump(agg, f, ensure_ascii=False, indent=1)
#     with open(jl, "w", encoding="utf-8") as f:
#         for r in rows:
#             f.write(json.dumps(r, ensure_ascii=False) + "\n")
#     return agg, rows, j, jl


# # --------------------------------------------------------------------------
# # driver
# # --------------------------------------------------------------------------
# def parse_range(spec, limit):
#     if not spec:
#         return list(range(1, limit + 1))
#     out = []
#     for part in spec.split(","):
#         if "-" in part:
#             a, b = part.split("-", 1)
#             out += list(range(int(a), int(b) + 1))
#         else:
#             out.append(int(part))
#     return [n for n in out if 1 <= n <= limit]


# def main():
#     ap = argparse.ArgumentParser(
#         description="Foodland AU weekly catalogue -> structured JSON",
#         formatter_class=argparse.RawDescriptionHelpFormatter,
#         epilog=__doc__.split("USAGE")[-1][:1200])
#     ap.add_argument("--list", action="store_true", help="list live catalogues and exit")
#     ap.add_argument("--id", type=int, help="leaflet id to harvest (default: newest)")
#     ap.add_argument("--input", help="dir of page images or a flyer PDF (skips harvest)")
#     ap.add_argument("--out", default="./foodland", help="output dir (default ./foodland)")
#     ap.add_argument("--backend", default="deepseek", choices=VALID_BACKENDS)
#     ap.add_argument("--pages", help="page range for extraction, e.g. 1-40 or 1,5,9")
#     ap.add_argument("--workers", type=int, default=4, help="parallel extraction (default 4)")
#     ap.add_argument("--sleep", type=float, default=0.4, help="pause between page downloads")
#     ap.add_argument("--no-harvest", action="store_true", help="extract existing pages only")
#     # backend knobs
#     ap.add_argument("--fal-key")
#     ap.add_argument("--api-key")
#     ap.add_argument("--base-url", default="https://api.openai.com/v1")
#     ap.add_argument("--model", default="deepseek-flash")
#     ap.add_argument("--max-tokens", type=int, default=8000)
#     ap.add_argument("--timeout", type=int, default=180)
#     ap.add_argument("--tesseract-exe")
#     a = ap.parse_args()

#     outdir = os.path.abspath(a.out)
#     pages_dir = os.path.join(outdir, "pages")
#     json_dir = os.path.join(outdir, "json")
#     os.makedirs(outdir, exist_ok=True)
#     os.makedirs(json_dir, exist_ok=True)

#     # ---------------------------------------------------------------- list
#     if a.list:
#         cats = discover()
#         print(f"{len(cats)} catalogues live on {LIST_URL}")
#         for c in cats:
#             print(f"  {c['leaflet_id']:>7}  {c['pages']:>3}p  {c['title']}")
#         return 0

#     # ------------------------------------------------------- preflight (key)
#     # Do this BEFORE downloading: a missing key should cost you zero bandwidth.
#     cfg = resolve_backend_config(a)
#     print(f"[1/5] backend={a.backend}"
#           + (f" model={cfg['model']} @ {cfg['base_url']}"
#              if a.backend in ("openai", "deepseek") else ""))

#     # ---------------------------------------------------------------- source
#     meta = {"retailer": "Foodland SA (AU)",
#             "aggregator": "latestcatalogues.com / leafletscdn", "backend": a.backend}
#     if a.input:
#         src = os.path.abspath(a.input)
#         if os.path.isfile(src) and src.lower().endswith(".pdf"):
#             print(f"[2/5] PDF -> page images: {src}")
#             files = pdf_to_pages(src, pages_dir)
#             meta["source_pdf"] = src
#         elif os.path.isdir(src):
#             files = sorted(glob.glob(os.path.join(src, "p[0-9]*.jpg")) +
#                            glob.glob(os.path.join(src, "p[0-9]*.png")))
#             if not files:
#                 raise SystemExit(f"no p*.jpg / p*.png page images in {src}")
#             print(f"[2/5] reusing {len(files)} page images from {src}")
#             meta["page_scans_dir"] = src
#         else:
#             raise SystemExit(f"--input not found: {src}")
#     else:
#         print("[2/5] discovering catalogues...")
#         cats = discover()
#         if not cats:
#             raise SystemExit("no catalogues found (aggregator layout changed?)")
#         sel = next((c for c in cats if c["leaflet_id"] == a.id), cats[0])
#         if a.id and sel["leaflet_id"] != a.id:
#             print(f"  warning: {a.id} not in current listing; using {sel['leaflet_id']}")
#         print(f"  -> {sel['title']} ({sel['pages']} pages, id {sel['leaflet_id']})")
#         if a.no_harvest:
#             files = sorted(glob.glob(os.path.join(pages_dir, "p[0-9]*.jpg")))
#             print(f"[2/5] --no-harvest: {len(files)} existing pages")
#             if not files:
#                 raise SystemExit(f"nothing in {pages_dir}")
#         else:
#             print(f"[2/5] harvesting {sel['pages']} pages -> {pages_dir}")
#             files = harvest(sel["leaflet_id"], sel["pages"], sel["url"],
#                             pages_dir, sleep=a.sleep)
#         meta.update({"catalogue": sel["title"], "leaflet_id": sel["leaflet_id"],
#                      "catalogue_url": sel["url"], "pages_in_catalogue": sel["pages"],
#                      "page_scans_dir": pages_dir, "page_image_size": "1550x2537"})

#     if not files:
#         raise SystemExit("no pages to extract")

#     # ---------------------------------------------------------------- backend
#     fn = BACKENDS[a.backend]

#     # ---------------------------------------------------------------- extract
#     todo = [f for f in files if int(re.search(r"p(\d+)\.", f).group(1))
#             in parse_range(a.pages, len(files))]
#     todo_nos = sorted(int(re.search(r"p(\d+)\.", f).group(1)) for f in todo)
#     print(f"[3/5] extracting {len(todo)} pages -> {json_dir}")

#     def do(path):
#         n = int(re.search(r"p(\d+)\.", path).group(1))
#         dest = os.path.join(json_dir, f"p{n:02d}.json")
#         if os.path.exists(dest):                       # resumable
#             return n, json.load(open(dest, encoding="utf-8")), "cached"
#         for attempt in (1, 2):
#             try:
#                 rec = normalise(fn(path, cfg), n, a.backend)
#                 with open(dest, "w", encoding="utf-8") as f:
#                     json.dump(rec, f, ensure_ascii=False, indent=1)
#                 return n, rec, f"{rec['product_count']} products [{rec['page_type']}]"
#             except Exception as e:                                 # noqa: BLE001
#                 if attempt == 2:
#                     return n, None, f"FAIL {e}"

#     pages, fails = [], 0
#     with futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
#         for fu in futures.as_completed([ex.submit(do, p) for p in todo]):
#             n, rec, msg = fu.result()
#             print(f"  p{n:02d} {msg}")
#             if not rec:
#                 fails += 1
#     # Merge from DISK, not from this run's results: a repair run like
#     # `--pages 1` must rebuild the full dataset, not truncate the output to the
#     # one page it re-extracted. Per-page JSON is the source of truth.
#     pages = [json.load(open(f, encoding="utf-8"))
#              for f in sorted(glob.glob(os.path.join(json_dir, "p[0-9][0-9].json")))]
#     if not pages:
#         raise SystemExit("extraction produced nothing - check your backend/key")

#     # ---------------------------------------------------------------- validate
#     print("[4/5] validating + merging")
#     problems, warnings, low = validate(pages, expected=todo_nos)
#     agg, rows, jpath, jlpath = merge(pages, outdir, meta)
#     agg["extraction"].update({"pages_requested": len(todo), "pages_failed": fails,
#                               "low_confidence_rows": low,
#                               "problem_count": len(problems)})
#     agg["validation"] = {"problems": problems, "warnings": warnings}
#     with open(jpath, "w", encoding="utf-8") as f:
#         json.dump(agg, f, ensure_ascii=False, indent=1)

#     print(f"\npages  {len(pages)}/{len(files)}   products  {len(rows)}   fails  {fails}")
#     print(f"json   {jpath}  ({os.path.getsize(jpath) // 1024} KB)")
#     print(f"jsonl  {jlpath}")
#     if problems:
#         print("\nPROBLEMS (fix before trusting the data):")
#         for p in problems[:20]:
#             print("  -", p)
#     if warnings:
#         print("\nwarnings:")
#         for w in warnings[:15]:
#             print("  -", w)
#     if low:
#         print(f"\n{low} row(s) below 0.9 confidence - review those before publishing.")
#     if not problems:
#         print("\nno structural problems found.")
#     return 0


# if __name__ == "__main__":
#     sys.exit(main())







#!/usr/bin/env python3
"""
Foodland AU weekly catalogue -> structured JSON.  Complete pipeline.

    harvest  : pull the current weekly catalogue page scans off the aggregator
    extract  : vision/OCR each page into structured product records
    validate : assert the output actually holds together
    merge    : write foodland_products.json + .jsonl

WHY THIS IS NOT A PDF PIPELINE
------------------------------
foodlandsa.com.au/catalogue/ is a WordPress page holding three promo teaser
JPGs. There is no PDF anywhere on it (grep -ci pdf == 0) and no priced flyer.
Foodland is not on Flipp either (Flipp is North America only). The full
40-page weekly catalogue is republished by latestcatalogues.com (kiim /
leafletscdn) as full-resolution scans -- that is what this script reads.

THE ONE THING THAT WILL BITE YOU
--------------------------------
The per-page image URL carries an HMAC signature you cannot compute:

    https://na.leafletscdn.com/thumbor/<HMAC>/0x0/filters:format(webp):quality(65)/com.au/data/<shop>/<leaflet>/<n>.jpg

So you must scrape the signed URL from the viewer endpoint, then download it
verbatim. Stripping the thumbor prefix and hitting /com.au/data/... directly
404s, and changing quality(65)/0x0 breaks the signature. The HMAC ends with
'=' -- a [A-Za-z0-9_-]+ regex silently misses it and every page reports
"no image URL".

USAGE
-----
  # 0. ONLY HAVE A DEEPSEEK KEY? This is your one-liner:
  python foodland_aus.py --backend deepseek --api-key YOUR_KEY
  #    model must stay deepseek-flash (the script defaults to it) --
  #    deepseek-v4-pro is text-only and will return 0 products per page.

  # 1. list catalogues live right now (ids change weekly - never hardcode)
  python foodland_aus.py --list

  # 2. harvest pages + extract + validate + merge, in one go
  python foodland_aus.py --out ./foodland --backend deepseek \
         --api-key YOUR_KEY --pages 1-40

  # 3. re-extract from pages you already downloaded (skips the download)
  python foodland_aus.py --input ./foodland/pages --out ./foodland

  # 4. extract from a real flyer PDF instead (needs: pip install pymupdf)
  python foodland_aus.py --input flyer.pdf --out ./out --backend deepseek --api-key KEY

  # 5. offline, no API, degraded text-only output
  python foodland_aus.py --input ./pages --backend tesseract

BACKENDS
--------
  deepseek   DeepSeek's own API. NO EXTRA KEY NEEDED IF YOU ALREADY HAVE ONE.
             Model MUST be deepseek-flash -- it reads images. deepseek-v4-pro,
             deepseek-chat and deepseek-reasoner are TEXT-ONLY and will reply
             "I can't view the image". deepseek-flash is a reasoning model, so
             it spends completion tokens thinking: keep max tokens high (the
             script raises it to 8000 automatically) or the answer comes back
             empty with every token burned on reasoning.
               python foodland_aus.py --backend deepseek --api-key YOUR_KEY
             Verified: 39/39 prices correct vs a vision-model reference across
             pages 3, 12, 28, 40.
  fal        fal.ai any-llm/vision. Needs FAL_KEY. ~5s/page.
  openai     ANY OpenAI-compatible /chat/completions endpoint. Use --base-url
             to point at OpenAI, Gemini, Groq, or a local Ollama / vLLM:
               --backend openai --base-url http://localhost:11434/v1 \
                 --model qwen2.5vl:7b --api-key ollama
             Gemini (free tier, good vision):
               --backend openai \
                 --base-url https://generativelanguage.googleapis.com/v1beta/openai \
                 --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY
  tesseract  Fully offline OCR. NO structured fields -- it returns raw text
             lines only, price/unit-price parsing is heuristic and lossy.
             Use as a fallback or for cross-checking, not as your primary.

  Check your lane actually works before planning a batch: a successful
  GET /v1/models tells you nothing about billing credits. Make one real call.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
AGG_BASE = "https://www.latestcatalogues.com"
LIST_URL = AGG_BASE + "/foodland/"
FAL_URL = "https://fal.run/fal-ai/any-llm/vision"

HREF_RE = re.compile(r'href="(/foodland/foodland-catalogue-[^"]+)"')
IMG_RE = re.compile(                      # NOTE the '=' -- signature ends with it
    r'https://na\.leafletscdn\.com/thumbor/[A-Za-z0-9_\-=]+/0x0/'
    r'filters:format\(webp\):quality\(\d+\)/com\.au/data/\d+/\d+/\d+\.jpg[^"\'\\ ]*')
VAR_RE = lambda n: re.compile(r'var\s+' + n + r'\s*=\s*"?(\d+)"?')   # noqa: E731

CATEGORIES = {"pantry", "dairy", "meat", "produce", "bakery", "frozen",
              "drinks", "liquor", "household", "health", "pet", "other"}
PAGE_TYPES = {"cover", "product_grid", "editorial", "promo", "index",
              "back_cover"}
ROW_KEYS = ["name", "brand", "size", "price", "currency", "unit_price", "was_price",
            "save", "category", "confidence"]

# The schema we ask the model for. Built as a dict so there is no brace
# escaping anywhere near a format string.
ROW_SCHEMA = {k: (0.0 if k in ("price", "was_price", "confidence") else "")
              for k in ROW_KEYS}
ROW_SCHEMA["category"] = "pantry|dairy|meat|produce|bakery|frozen|drinks|liquor|household|health|pet|other"
PAGE_SCHEMA = {"page_type": "cover|product_grid|editorial|promo|index|back_cover",
               "validity": "date range exactly as printed, else empty",
               "promo_headline": "main promo text on the page, else empty",
               "products": [ROW_SCHEMA],
               "unreadable": ["offers visible but too small/blurry to trust"],
               "notes": ""}

PROMPT = (
    "Extract EVERY product offer on this Australian supermarket (Foodland SA) "
    "catalogue page.\n"
    "Return ONLY raw JSON. No markdown fences, no commentary.\n"
    "Schema:\n" + json.dumps(PAGE_SCHEMA) + "\n"
    "Rules:\n"
    "- One array entry per distinct offer. If one offer covers several "
    "varieties, put every variety in the name field exactly as printed.\n"
    "- price = advertised price in AUD as a number only (3.75, not \"$3.75\"). "
    "Use 0 if it cannot be read.\n"
    "- currency = currency code or symbol as printed (e.g., \"AUD\" or \"$\"), else \"AUD\".\n"
    "- If priced by weight, put the number in price and 'per kg' in size.\n"
    "- was_price: fill ONLY if a was/previous price is actually printed on the "
    "page. Flyers usually print a SAVE UP TO figure or a HALF PRICE tag instead "
    "- do NOT reconstruct a was price from those. Leave 0.\n"
    "- unit_price and save: copy verbatim as printed, else empty.\n"
    "- Never invent products or prices. Anything too small or blurry to read "
    "confidently goes in unreadable, and that row's confidence drops below 0.5.\n"
    "- validity: copy a date range ONLY if this page prints one.\n"
)

VALID_BACKENDS = ("fal", "openai", "deepseek", "tesseract")

# DeepSeek: deepseek-flash HAS vision, deepseek-v4-pro does NOT (it replies
# "I can't view the image"). deepseek-flash is a reasoning model, so it burns
# completion tokens thinking before it answers -- give it a large budget or the
# content comes back empty with all tokens spent on reasoning.
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_NO_VISION = ("deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner")
DEEPSEEK_MIN_TOKENS = 8000


# --------------------------------------------------------------------------
# env / keys
# --------------------------------------------------------------------------
def load_key(name, explicit=None):
    """explicit arg > environment > .env files near cwd/script/home."""
    if explicit:
        return explicit
    if os.environ.get(name):
        return os.environ[name]
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env"),
              os.path.expanduser(".hermes/.env"),
              os.path.expanduser("~/.hermes/.env")):
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


def resolve_backend_config(a):
    """Resolve the vision lane BEFORE downloading anything.

    Fail fast: the original version harvested all 40 pages and only then
    complained that no key was configured. Check here instead.
    """
    cfg = {"timeout": a.timeout, "model": a.model, "max_tokens": a.max_tokens,
           "base_url": a.base_url, "tesseract_exe": a.tesseract_exe}
    if a.backend == "tesseract":
        return cfg
    if a.backend == "fal":
        key = load_key("FAL_KEY", a.fal_key)
        if not key:
            raise SystemExit(
                "No FAL_KEY found.\n"
                "  Option 1 - fal.ai key (paid, ~5s/page):\n"
                "      python foodland_aus.py --backend fal --fal-key YOUR_KEY\n"
                "  Option 2 - FREE Google AI Studio key, same script, different lane:\n"
                "      python foodland_aus.py --backend openai \\\n"
                "        --base-url https://generativelanguage.googleapis.com/v1beta/openai \\\n"
                "        --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY\n"
                "  Option 3 - no key at all, fully offline (degraded, text-only output):\n"
                "      python foodland_aus.py --backend tesseract\n"
                "  Key already downloaded pages are kept - any of the above resumes from them.")
        cfg["key"] = key
        return cfg
    if a.backend == "deepseek":
        cfg["base_url"] = a.base_url if a.base_url != "https://api.openai.com/v1" \
            else DEEPSEEK_BASE
        if a.model == "gpt-4o-mini":
            cfg["model"] = DEEPSEEK_DEFAULT_MODEL
        if any(m in cfg["model"] for m in DEEPSEEK_NO_VISION):
            print(f"  WARNING: {cfg['model']} has NO vision - it will refuse the image.")
            print(f"           use {DEEPSEEK_DEFAULT_MODEL} instead.")
        if cfg["max_tokens"] < DEEPSEEK_MIN_TOKENS:
            print(f"  raising max tokens {cfg['max_tokens']} -> {DEEPSEEK_MIN_TOKENS} "
                  f"(reasoning model eats the budget before answering)")
            cfg["max_tokens"] = DEEPSEEK_MIN_TOKENS
        key = (a.api_key or os.environ.get("DEEPSEEK_API_KEY")
               or load_key("DEEPSEEK_API_KEY"))
        if not key:
            raise SystemExit(
                "No DeepSeek key found.\n"
                "  Pass --api-key, or set DEEPSEEK_API_KEY in your environment.\n"
                "      python foodland_aus.py --backend deepseek --api-key YOUR_KEY\n"
                "  Model note: deepseek-flash reads images, deepseek-v4-pro does not.")
        cfg["key"] = key
        return cfg
    # openai-compatible
    key = (a.api_key or os.environ.get("VISION_API_KEY")
           or os.environ.get("GEMINI_API_KEY")
           or load_key("OPENAI_API_KEY"))
    if not key:
        raise SystemExit(
            "No API key for the openai-compatible backend.\n"
            "  Pass --api-key, or set GEMINI_API_KEY / OPENAI_API_KEY / VISION_API_KEY.\n"
            "  Free option: create a key at https://aistudio.google.com/apikey then:\n"
            "      python foodland_aus.py --backend openai \\\n"
            "        --base-url https://generativelanguage.googleapis.com/v1beta/openai \\\n"
            "        --model gemini-3.6-flash --api-key YOUR_GEMINI_KEY\n"
            "  Or point --base-url at a local Ollama (no key needed):\n"
            "      --base-url http://localhost:11434/v1 --model qwen2.5vl:7b --api-key ollama")
    cfg["key"] = key
    return cfg


def http(url, data=None, headers=None, timeout=180, tries=3, label=""):
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {detail}"
            # 400/422 = bad payload, retrying identical bytes won't help
            if e.code in (400, 401, 403, 404, 422):
                break
        except Exception as e:                                     # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (a + 1))
    raise RuntimeError(f"{label or url} failed -> {last}")


# --------------------------------------------------------------------------
# stage 1: harvest
# --------------------------------------------------------------------------
def discover():
    """[{'leaflet_id','shop_id','pages','url','title'}, ...] -- ids change weekly."""
    html = http(LIST_URL, headers={"User-Agent": UA}).decode("utf-8", "replace")
    out, seen = [], set()
    for href in HREF_RE.findall(html):
        if href in seen:
            continue
        seen.add(href)
        page = http(AGG_BASE + href, headers={"User-Agent": UA}).decode("utf-8", "replace")
        lid, sid, pg = (VAR_RE("pageBrochureId").search(page),
                        VAR_RE("currentShopId").search(page),
                        VAR_RE("maxPages").search(page))
        if not (lid and sid and pg):
            continue
        t = re.search(r"<title>([^<]+)</title>", page)
        out.append({"leaflet_id": int(lid.group(1)), "shop_id": int(sid.group(1)),
                    "pages": int(pg.group(1)), "url": AGG_BASE + href,
                    "title": re.sub(r"\s+", " ", t.group(1)).strip()[:110] if t else ""})
    return out


def signed_page_url(leaflet_id, page_no, catalogue_url):
    """The HMAC is per-URL; scrape it, never compute it. 400s on POST."""
    q = (f"{AGG_BASE}/brochure/brochure-page/?brochureId={leaflet_id}"
         f"&page={page_no}&current_url={catalogue_url}")
    html = http(q, headers={"User-Agent": UA, "Referer": catalogue_url}).decode("utf-8", "replace")
    m = IMG_RE.search(html)
    return None if not m else m.group(0).replace("&amp;", "&")


def harvest(leaflet_id, pages, catalogue_url, outdir, sleep=0.4):
    """Resumable: existing files >20KB are kept, so a killed run just continues."""
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for p in range(1, pages + 1):
        dest = os.path.join(outdir, f"p{p:02d}.jpg")
        if os.path.exists(dest) and os.path.getsize(dest) > 20_000:
            print(f"  p{p:02d} cached")
            saved.append(dest)
            continue
        try:
            url = signed_page_url(leaflet_id, p, catalogue_url)
            if not url:
                print(f"  p{p:02d} NO IMAGE URL (regex missing the signature?)")
                continue
            blob = http(url, headers={"User-Agent": UA, "Referer": catalogue_url})
            if len(blob) < 20_000:
                print(f"  p{p:02d} too small ({len(blob)}B) - skipped")
                continue
            with open(dest, "wb") as f:
                f.write(blob)
            saved.append(dest)
            print(f"  p{p:02d} ok {len(blob) // 1024}KB")
        except Exception as e:                                     # noqa: BLE001
            print(f"  p{p:02d} FAIL {e}")
        time.sleep(sleep)
    with open(os.path.join(outdir, "_manifest.json"), "w") as f:
        json.dump({"leaflet_id": leaflet_id, "pages": len(saved),
                   "catalogue_url": catalogue_url, "files": saved}, f, indent=2)
    return saved


def pdf_to_pages(pdf_path, outdir, dpi=200):
    """Real flyer PDFs: keep the text layer when there is one, always rasterise."""
    try:
        import pymupdf                                            # noqa: PLC0415
    except ImportError:
        raise SystemExit("PDF input needs pymupdf:  pip install pymupdf")
    os.makedirs(outdir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    out, text_pages = [], 0
    for i, page in enumerate(doc):
        txt = page.get_text().strip()
        if len(txt) > 80:
            text_pages += 1
            with open(os.path.join(outdir, f"p{i + 1:02d}.txt"), "w", encoding="utf-8") as f:
                f.write(txt)
        p = os.path.join(outdir, f"p{i + 1:02d}.jpg")
        page.get_pixmap(dpi=dpi).save(p)
        out.append(p)
    avg = sum(len(open(os.path.join(outdir, f"p{i + 1:02d}.txt"), encoding="utf-8").read())
              for i in range(text_pages)) if text_pages else 0
    print(f"  PDF: {doc.page_count} pages, {text_pages} with a text layer"
          f"{' (avg %d chars)' % (avg // text_pages) if text_pages else ''}")
    if text_pages == doc.page_count:
        print("  -> text layer everywhere: the .txt files already contain the data,"
              " vision is optional here")
    return out


# --------------------------------------------------------------------------
# stage 2: backends
# --------------------------------------------------------------------------
def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _json_from_text(txt):
    """Models wrap JSON in fences, prose, or both. Dig it out."""
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError("no JSON object in model output")


def call_fal(img_path, cfg):
    body = {"prompt": PROMPT, "image_url": "data:image/jpeg;base64," + b64(img_path)}
    raw = http(FAL_URL, data=body, timeout=cfg["timeout"], label="fal",
               headers={"Authorization": f"Key {cfg['key']}",
                        "Content-Type": "application/json"})
    env = json.loads(raw)
    if env.get("error"):
        raise RuntimeError(f"fal error: {env['error']}")
    return _json_from_text(env.get("output", ""))


def call_openai(img_path, cfg):
    """Any OpenAI-compatible /chat/completions: OpenAI, OpenRouter, Ollama, vLLM."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + b64(img_path), "detail": "high"}}]}]
    base = {"model": cfg["model"], "max_completion_tokens": cfg["max_tokens"],
            "messages": messages}
    headers = {"Authorization": f"Bearer {cfg['key']}",
               "Content-Type": "application/json"}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    # response_format is widely supported but not universal. Try it, and only
    # fall back when the SERVER rejects the payload -- never when our own code
    # is broken (a broad except here once hid a bug for two "retries" and let a
    # zero-call run look like a server incompatibility).
    variants = ({"response_format": {"type": "json_object"}}, {})
    for i, extra in enumerate(variants):
        try:
            raw = http(url, data={**base, **extra}, headers=headers,
                       timeout=cfg["timeout"], tries=1, label="openai")
            out = json.loads(raw)
            return _json_from_text(out["choices"][0]["message"]["content"])
        except Exception as e:                                     # noqa: BLE001
            msg = str(e)
            server_rejected = ("HTTP 400" in msg or "HTTP 422" in msg
                               or "response_format" in msg or "json_object" in msg)
            if i == 0 and server_rejected:
                print(f"    (endpoint rejects response_format, retrying plain: {msg[:120]})")
                continue
            raise


def call_tesseract(img_path, cfg):
    """Offline OCR. No structured extraction -- honest, lossy text only."""
    exe = cfg.get("tesseract_exe") or "tesseract"
    try:
        r = subprocess.run([exe, img_path, "stdout", "--psm", "6"],
                           capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise SystemExit("tesseract not found. macOS: brew install tesseract | "
                         "Windows: choco install tesseract | "
                         "or use --backend fal/openai")
    text = r.stdout
    products, seen = [], set()
    # heuristic: a line with a $ amount is probably an offer line
    for line in (l.strip() for l in text.splitlines()):
        if len(line) < 6 or not re.search(r"\$\s?\d", line):
            continue
        if line in seen:
            continue
        seen.add(line)
        names = re.sub(r"\$\s?\d+[.,]?\d*.*$", "", line).strip(" .|-")
        nums = re.findall(r"\$\s?(\d+(?:[.,]\d{2})?)", line)
        products.append({"name": names or line, "brand": "", "size": "",
                         "price": float(nums[0].replace(",", ".")) if nums else 0.0,
                         "currency": "AUD", "unit_price": "", "was_price": 0.0, "save": "",
                         "category": "other", "confidence": 0.3})
    return {"page_type": "product_grid", "validity": "", "promo_headline": "",
            "products": products,
            "unreadable": ["tesseract backend: unstructured output, every row "
                           "is a low-confidence heuristic guess"],
            "notes": "raw tesseract text kept alongside in _ocr_text"}


BACKENDS = {"fal": call_fal, "openai": call_openai, "deepseek": call_openai,
            "tesseract": call_tesseract}


# --------------------------------------------------------------------------
# stage 3: normalise + validate
# --------------------------------------------------------------------------
def normalise(rec, page_no, backend):
    rec = rec if isinstance(rec, dict) else {}
    if rec.get("page_type") not in PAGE_TYPES:
        rec["page_type"] = "product_grid"
    valid = []
    for pr in rec.get("products") or []:
        if not isinstance(pr, dict):
            continue
        row = {}
        for k in ROW_KEYS:
            v = pr.get(k, ROW_SCHEMA[k])
            if k in ("price", "was_price", "confidence"):
                try:
                    v = float(str(v).replace("$", "").replace(",", "").strip() or 0)
                except ValueError:
                    v = 0.0
            else:
                v = str(v or "").strip()
            row[k] = v
        if not row["name"]:
            continue
        if row["category"] not in CATEGORIES:
            row["category"] = "other"
        row["confidence"] = min(1.0, max(0.0, row["confidence"]))
        # did the model just echo the price back into was_price?
        if row["was_price"] and abs(row["was_price"] - row["price"]) < 1e-9:
            row["was_price"] = 0.0
        valid.append(row)
    rec["products"] = valid
    rec["product_count"] = len(valid)
    rec["page"] = page_no
    rec["backend"] = backend
    rec["unreadable"] = [str(u) for u in (rec.get("unreadable") or [])]
    rec["notes"] = str(rec.get("notes") or "")
    rec["validity"] = str(rec.get("validity") or "")
    rec["promo_headline"] = str(rec.get("promo_headline") or "")
    return rec


def validate(pages, expected=None):
    """expected = the page numbers that SHOULD be present (a subset is fine)."""
    problems, warnings = [], []
    nos = sorted(p["page"] for p in pages)
    if expected is not None:
        want = set(int(n) for n in expected)
        miss = sorted(want - set(nos))
        if miss:
            problems.append(f"missing pages: {miss}")
    dupes = [n for n in set(nos) if nos.count(n) > 1]
    if dupes:
        problems.append(f"duplicate page records: {sorted(dupes)}")
    seen = set()
    for p in pages:
        for r in p["products"]:
            for k in ROW_KEYS:
                if k not in r:
                    problems.append(f"p{p['page']} row missing {k}")
                    break
            if list(r.keys()) != ROW_KEYS:
                extra = [k for k in r if k not in ROW_KEYS]
                problems.append(f"p{p['page']} row has unexpected keys {extra}")
            if r["category"] not in CATEGORIES:
                problems.append(f"p{p['page']} bad category {r['category']!r}")
            if not isinstance(r["price"], float):
                problems.append(f"p{p['page']} price not numeric: {r['name'][:40]}")
            if r["price"] == 0 and "competition" not in p["notes"].lower():
                warnings.append(f"p{p['page']} price 0 (no price printed?): {r['name'][:45]}")
            key = (p["page"], r["name"])
            if key in seen:
                problems.append(f"p{p['page']} duplicate product {r['name'][:40]!r}")
            seen.add(key)
    for p in pages:
        if not p["products"]:
            problems.append(
                f"page {p['page']} produced 0 products - did the model refuse the "
                f"image? (wrong model, or a genuine non-product page)")
        if p["unreadable"]:
            warnings.append(f"page {p['page']} needs review: {len(p['unreadable'])} unreadable")
    low = sum(1 for p in pages for r in p["products"] if r["confidence"] < 0.9)
    return problems, warnings, low


def merge(pages, outdir, meta):
    rows = [{"page": p["page"], "page_type": p["page_type"],
             "catalogue_validity": p["validity"],
             "promo_headline": p["promo_headline"], **r}
            for p in sorted(pages, key=lambda x: x["page"]) for r in p["products"]]
    agg = {"source": meta,
           "extraction": {"backend": meta.get("backend"),
                          "pages_extracted": len(pages),
                          "products_total": len(rows)},
           "pages": sorted(pages, key=lambda x: x["page"]), "products": rows}
    j = os.path.join(outdir, "foodland_products.json")
    jl = os.path.join(outdir, "foodland_products.jsonl")
    with open(j, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)
    with open(jl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return agg, rows, j, jl


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def parse_range(spec, limit):
    if not spec:
        return list(range(1, limit + 1))
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [n for n in out if 1 <= n <= limit]


def main():
    ap = argparse.ArgumentParser(
        description="Foodland AU weekly catalogue -> structured JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1][:1200])
    ap.add_argument("--list", action="store_true", help="list live catalogues and exit")
    ap.add_argument("--id", type=int, help="leaflet id to harvest (default: newest)")
    ap.add_argument("--input", help="dir of page images or a flyer PDF (skips harvest)")
    ap.add_argument("--out", default="./foodland", help="output dir (default ./foodland)")
    ap.add_argument("--backend", default="deepseek", choices=VALID_BACKENDS)
    ap.add_argument("--pages", help="page range for extraction, e.g. 1-40 or 1,5,9")
    ap.add_argument("--workers", type=int, default=4, help="parallel extraction (default 4)")
    ap.add_argument("--sleep", type=float, default=0.4, help="pause between page downloads")
    ap.add_argument("--no-harvest", action="store_true", help="extract existing pages only")
    # backend knobs
    ap.add_argument("--fal-key")
    ap.add_argument("--api-key")
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--tesseract-exe")
    a = ap.parse_args()

    outdir = os.path.abspath(a.out)
    pages_dir = os.path.join(outdir, "pages")
    json_dir = os.path.join(outdir, "json")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(json_dir, exist_ok=True)

    # ---------------------------------------------------------------- list
    if a.list:
        cats = discover()
        print(f"{len(cats)} catalogues live on {LIST_URL}")
        for c in cats:
            print(f"  {c['leaflet_id']:>7}  {c['pages']:>3}p  {c['title']}")
        return 0

    # ------------------------------------------------------- preflight (key)
    # Do this BEFORE downloading: a missing key should cost you zero bandwidth.
    cfg = resolve_backend_config(a)
    print(f"[1/5] backend={a.backend}"
          + (f" model={cfg['model']} @ {cfg['base_url']}"
             if a.backend in ("openai", "deepseek") else ""))

    # ---------------------------------------------------------------- source
    meta = {"retailer": "Foodland SA (AU)",
            "aggregator": "latestcatalogues.com / leafletscdn", "backend": a.backend}
    if a.input:
        src = os.path.abspath(a.input)
        if os.path.isfile(src) and src.lower().endswith(".pdf"):
            print(f"[2/5] PDF -> page images: {src}")
            files = pdf_to_pages(src, pages_dir)
            meta["source_pdf"] = src
        elif os.path.isdir(src):
            files = sorted(glob.glob(os.path.join(src, "p[0-9]*.jpg")) +
                           glob.glob(os.path.join(src, "p[0-9]*.png")))
            if not files:
                raise SystemExit(f"no p*.jpg / p*.png page images in {src}")
            print(f"[2/5] reusing {len(files)} page images from {src}")
            meta["page_scans_dir"] = src
        else:
            raise SystemExit(f"--input not found: {src}")
    else:
        print("[2/5] discovering catalogues...")
        cats = discover()
        if not cats:
            raise SystemExit("no catalogues found (aggregator layout changed?)")
        sel = next((c for c in cats if c["leaflet_id"] == a.id), cats[0])
        if a.id and sel["leaflet_id"] != a.id:
            print(f"  warning: {a.id} not in current listing; using {sel['leaflet_id']}")
        print(f"  -> {sel['title']} ({sel['pages']} pages, id {sel['leaflet_id']})")
        if a.no_harvest:
            files = sorted(glob.glob(os.path.join(pages_dir, "p[0-9]*.jpg")))
            print(f"[2/5] --no-harvest: {len(files)} existing pages")
            if not files:
                raise SystemExit(f"nothing in {pages_dir}")
        else:
            print(f"[2/5] harvesting {sel['pages']} pages -> {pages_dir}")
            files = harvest(sel["leaflet_id"], sel["pages"], sel["url"],
                            pages_dir, sleep=a.sleep)
        meta.update({"catalogue": sel["title"], "leaflet_id": sel["leaflet_id"],
                     "catalogue_url": sel["url"], "pages_in_catalogue": sel["pages"],
                     "page_scans_dir": pages_dir, "page_image_size": "1550x2537"})

    if not files:
        raise SystemExit("no pages to extract")

    # ---------------------------------------------------------------- backend
    fn = BACKENDS[a.backend]

    # ---------------------------------------------------------------- extract
    todo = [f for f in files if int(re.search(r"p(\d+)\.", f).group(1))
            in parse_range(a.pages, len(files))]
    todo_nos = sorted(int(re.search(r"p(\d+)\.", f).group(1)) for f in todo)
    print(f"[3/5] extracting {len(todo)} pages -> {json_dir}")

    def do(path):
        n = int(re.search(r"p(\d+)\.", path).group(1))
        dest = os.path.join(json_dir, f"p{n:02d}.json")
        if os.path.exists(dest):                       # resumable
            return n, json.load(open(dest, encoding="utf-8")), "cached"
        for attempt in (1, 2):
            try:
                rec = normalise(fn(path, cfg), n, a.backend)
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                return n, rec, f"{rec['product_count']} products [{rec['page_type']}]"
            except Exception as e:                                 # noqa: BLE001
                if attempt == 2:
                    return n, None, f"FAIL {e}"

    pages, fails = [], 0
    with futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for fu in futures.as_completed([ex.submit(do, p) for p in todo]):
            n, rec, msg = fu.result()
            print(f"  p{n:02d} {msg}")
            if not rec:
                fails += 1
    # Merge from DISK, not from this run's results: a repair run like
    # `--pages 1` must rebuild the full dataset, not truncate the output to the
    # one page it re-extracted. Per-page JSON is the source of truth.
    pages = [json.load(open(f, encoding="utf-8"))
             for f in sorted(glob.glob(os.path.join(json_dir, "p[0-9][0-9].json")))]
    if not pages:
        raise SystemExit("extraction produced nothing - check your backend/key")

    # ---------------------------------------------------------------- validate
    print("[4/5] validating + merging")
    problems, warnings, low = validate(pages, expected=todo_nos)
    agg, rows, jpath, jlpath = merge(pages, outdir, meta)
    agg["extraction"].update({"pages_requested": len(todo), "pages_failed": fails,
                              "low_confidence_rows": low,
                              "problem_count": len(problems)})
    agg["validation"] = {"problems": problems, "warnings": warnings}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=1)

    print(f"\npages  {len(pages)}/{len(files)}   products  {len(rows)}   fails  {fails}")
    print(f"json   {jpath}  ({os.path.getsize(jpath) // 1024} KB)")
    print(f"jsonl  {jlpath}")
    if problems:
        print("\nPROBLEMS (fix before trusting the data):")
        for p in problems[:20]:
            print("  -", p)
    if warnings:
        print("\nwarnings:")
        for w in warnings[:15]:
            print("  -", w)
    if low:
        print(f"\n{low} row(s) below 0.9 confidence - review those before publishing.")
    if not problems:
        print("\nno structural problems found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())