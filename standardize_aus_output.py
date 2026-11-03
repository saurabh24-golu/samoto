#!/usr/bin/env python3
"""Package existing Australian scraper outputs into a dated, DB-friendly layout.

This is an output-only step: run the existing retailer scraper first, then run:

    python standardize_aus_output.py --retailer Drakes --source ./drakes

Output (date uses local time, DDMMYYYY):

    AUS03112026/Drakes/Stores.json
    AUS03112026/Drakes/ascot_park.json
    AUS03112026/Drakes/ascot-park.pdf

The extractor code and its network/OCR logic remain unchanged. The exporter
accepts existing store JSON files (including nested ``stores/`` and ``json/``
directories), normalizes product rows to one stable key set, and keeps any
retailer-specific original fields under ``source_fields``.
"""

import argparse
import json
import os
import re
import shutil
from datetime import date, datetime

# Stable keys for every product row, regardless of retailer/source schema.
PRODUCT_FIELDS = (
    "retailer", "store_name", "store_location", "store_id",
    "product_id", "sku", "name", "brand", "size", "price", "currency",
    "was_price", "unit_price", "saving", "saving_text", "category",
    "is_special", "availability", "product_url", "image_url",
    "page", "offer_text", "source_fields",
)

# Stable shape for every store record in Stores.json.
STORE_FIELDS = (
    "retailer", "store_name", "store_location", "store_id", "state",
    "address", "postcode", "phone", "website", "source_fields",
)

IGNORED_JSON = {
    "stores.json", "Stores.json", "index.json", "summary.json",
    "run_report.json", "run_summary.json", "progress.json",
    "catalogues.json", "editions.json", "categories.json",
    "categories_flat.json", "harrisfarm_stores.json",
    "harrisfarm_specials_raw.json", "harrisfarm_specials_raw_rest.json",
    "foodland_products.json", "brochure_current.json",
}


def slugify(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return value.strip("._-") or "unknown_store"


def first(record, *keys):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def store_list(source):
    """Find known store registries; tolerate list and retailer-specific wrappers."""
    candidates = (
        "Stores.json", "stores.json", "harrisfarm_stores.json", "wwau_stores.json",
    )
    for filename in candidates:
        doc = load_json(os.path.join(source, filename))
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            rows = doc.get("stores")
            if isinstance(rows, dict):
                return list(rows.values())
            if isinstance(rows, list):
                return rows
    return []


def product_rows(doc):
    """Extract a product array from the common scraper output envelopes."""
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return []
    for key in ("products", "items"):
        rows = doc.get(key)
        if isinstance(rows, list):
            return rows
    return []


def store_metadata(doc, fallback_name, retailer):
    if not isinstance(doc, dict):
        doc = {}
    nested = doc.get("store") if isinstance(doc.get("store"), dict) else {}
    name = first(nested, "name", "store_name", "displayName", "storeName")
    name = name or first(doc, "store_name", "storeName", "name", "displayName") or fallback_name
    location = first(nested, "location", "address")
    if isinstance(location, dict):
        location = ", ".join(str(v) for v in location.values() if v not in (None, ""))
    location = location or first(doc, "address", "addressLine1", "location", "suburb") or ""
    sid = first(nested, "store_id", "storeId", "retailerStoreId", "id")
    sid = sid or first(doc, "store_id", "storeId", "retailerStoreId", "id")
    return {
        "retailer": retailer,
        "store_name": str(name),
        "store_location": str(location),
        "store_id": sid,
        "state": first(nested, "state", "region") or first(doc, "state", "region"),
        "address": first(nested, "address", "addressLine1") or first(doc, "address", "addressLine1"),
        "postcode": first(nested, "postcode", "postCode", "postalCode") or first(doc, "postcode", "postCode", "postalCode"),
        "phone": first(nested, "phone", "managerPhone") or first(doc, "phone", "managerPhone"),
        "website": first(nested, "homepage", "site", "store_url", "url") or first(doc, "homepage", "site", "store_url", "url"),
        "source_fields": {k: v for k, v in doc.items() if k not in ("products", "items", "store")},
    }


def normalize_product(row, retailer, store):
    if not isinstance(row, dict):
        row = {}
    reserved = {
        "retailer", "store_name", "store_location", "store_id", "product_id", "sku",
        "name", "brand", "size", "price", "currency", "was_price", "unit_price",
        "saving", "saving_text", "category", "is_special", "availability",
        "product_url", "image_url", "page", "offer_text", "source_fields",
    }
    price = first(row, "price", "priceCents", "priceNumeric", "wholePrice")
    if "priceCents" in row and price is not None:
        try:
            price = float(price) / 100.0
        except (TypeError, ValueError):
            pass
    pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
    unit = row.get("unit") if isinstance(row.get("unit"), dict) else {}
    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    return {
        "retailer": retailer,
        "store_name": store["store_name"],
        "store_location": store["store_location"],
        "store_id": store["store_id"],
        "product_id": first(row, "product_id", "productId", "articleNo", "stockcode", "code", "sku"),
        "sku": first(row, "sku", "articleNo", "stockcode", "productId"),
        "name": first(row, "name", "title", "full_name", "displayName", "shelfTicketName"),
        "brand": first(row, "brand", "vendor", "manufacturer"),
        "size": first(row, "size", "packSize", "packageSize") or unit.get("size") or unit.get("label"),
        "price": price if price is not None else pricing.get("price"),
        "currency": first(row, "currency", "currencyCode") or "AUD",
        "was_price": first(row, "was_price", "wasPrice", "compare_at_price") or pricing.get("wasPrice"),
        "unit_price": first(row, "unit_price", "unitPrice", "unitPriceDisplay") or pricing.get("pricePerUnit"),
        "saving": first(row, "saving", "savingAmount"),
        "saving_text": first(row, "save", "save_text", "save_text", "saveText", "badge", "save_amount"),
        "category": first(row, "category", "department", "product_type", "productType"),
        "is_special": first(row, "is_special", "isOnSpecial", "on_special"),
        "availability": first(row, "isAvailable", "isInStock", "available_for_sale") or availability,
        "product_url": first(row, "url", "productUrl", "product_page"),
        "image_url": first(row, "image_url", "imageUrl", "image") or (row.get("images") or [None])[0],
        "page": first(row, "page"),
        "offer_text": first(row, "offer_text", "promo_text", "deal_text", "multi_buy", "price_text"),
        "source_fields": {k: v for k, v in row.items() if k not in reserved},
    }


def source_files(source):
    found = []
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d.lower() not in {"pages", "pages_json", "pdf", "pdfs", "brochure_pdf", "raw", "catalog", "promo"}]
        for filename in files:
            if not filename.lower().endswith(".json") or filename in IGNORED_JSON:
                continue
            # The root-level dataset/index files are not per-store documents.
            if filename.lower() in {"products.json", "foodland_products.json"}:
                continue
            path = os.path.join(root, filename)
            if product_rows(load_json(path)):
                found.append(path)
    return sorted(found)


def registry_record(raw, retailer):
    if not isinstance(raw, dict):
        raw = {"name": str(raw)}
    name = first(raw, "name", "store_name", "storeName", "displayName", "slug") or "Unknown store"
    location = first(raw, "address", "addressLine1", "suburb", "town", "city", "location") or ""
    if isinstance(location, dict):
        location = ", ".join(str(v) for v in location.values() if v not in (None, ""))
    sid = first(raw, "store_id", "storeId", "retailerStoreId", "StoreNo", "id")
    rec = {key: None for key in STORE_FIELDS}
    rec.update({
        "retailer": retailer, "store_name": str(name), "store_location": str(location),
        "store_id": sid, "state": first(raw, "state", "State", "region"),
        "address": first(raw, "address", "addressLine1", "line1"),
        "postcode": first(raw, "postcode", "postCode", "Postcode", "postalCode"),
        "phone": first(raw, "phone", "Phone", "managerPhone"),
        "website": first(raw, "homepage", "site", "url", "store_url"),
        "source_fields": dict(raw),
    })
    return rec


def export(source, retailer, output_root, keep_images=False):
    source = os.path.abspath(source)
    day = date.today().strftime("%d%m%Y")
    retailer_dir = os.path.join(os.path.abspath(output_root), "AUS" + day, slugify(retailer))
    os.makedirs(retailer_dir, exist_ok=True)

    raw_stores = store_list(source)
    files = source_files(source)
    registry = [registry_record(item, retailer) for item in raw_stores]
    seen_store_ids = {str(r.get("store_id")) for r in registry if r.get("store_id") is not None}
    output_count = 0

    for path in files:
        doc = load_json(path)
        rows = product_rows(doc)
        if not rows:
            continue
        fallback = os.path.splitext(os.path.basename(path))[0]
        metadata = store_metadata(doc, fallback, retailer)
        # Avoid treating shared-edition IDs as store locations where a store
        # record is present in the document.
        location_slug = slugify(metadata["store_name"])
        products = [normalize_product(row, retailer, metadata) for row in rows]
        payload = {
            "retailer": retailer,
            "store": metadata,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "product_count": len(products),
            "products": products,
        }
        with open(os.path.join(retailer_dir, location_slug + ".json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        output_count += 1
        if metadata.get("store_id") is not None:
            seen_store_ids.add(str(metadata["store_id"]))
        print("wrote %s.json (%d products)" % (location_slug, len(products)))

    # Include discovered locations even when a store has no catalogue output.
    registry_by_key = {}
    for record in registry:
        key = str(record.get("store_id") or record.get("store_name"))
        registry_by_key[key] = record
    for path in files:
        doc = load_json(path)
        if isinstance(doc, dict):
            metadata = store_metadata(doc, os.path.splitext(os.path.basename(path))[0], retailer)
            key = str(metadata.get("store_id") or metadata["store_name"])
            registry_by_key.setdefault(key, registry_record(metadata, retailer))

    stores_manifest = {
        "retailer": retailer,
        "country": "AU",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(registry_by_key),
        "stores": list(registry_by_key.values()),
        "store_files_written": output_count,
    }
    with open(os.path.join(retailer_dir, "Stores.json"), "w", encoding="utf-8") as stream:
        json.dump(stores_manifest, stream, ensure_ascii=False, indent=2)

    pdf_count = 0
    for root, dirs, names in os.walk(source):
        dirs[:] = [d for d in dirs if d.lower() not in {"pages", "pages_json", "raw"}]
        for name in names:
            if name.lower().endswith(".pdf"):
                shutil.copy2(os.path.join(root, name), os.path.join(retailer_dir, name))
                pdf_count += 1
    print("\nOutput: %s (%d store JSON files, %d PDFs; images omitted)" %
          (retailer_dir, output_count, pdf_count))
    if not keep_images:
        print("Page images and temporary extraction files are not copied.")
    return retailer_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retailer", required=True, help="retailer folder name, e.g. Drakes")
    parser.add_argument("--source", required=True, help="existing scraper output directory")
    parser.add_argument("--output-root", default=".", help="parent folder for AUSDDMMYYYY")
    parser.add_argument("--keep-images", action="store_true", help="copy image assets if present")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.source):
        parser.error("--source must be an existing directory")
    export(args.source, args.retailer, args.output_root, args.keep_images)


if __name__ == "__main__":
    main()
