"""Barcode -> product details for adding supplements.

Lookup order: products already saved in this household (any profile), then
Open Food Facts (free, open data; covers many supplements and protein powders),
then UPCitemdb's free trial endpoint (100 lookups/day per IP, good US retail
coverage). Results are cached in SQLite. Nothing is invented: every field comes
from one of these sources, and the response says which.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger("home-health.barcode")

UA = "HomeHealth/1.0 (self-hosted household health dashboard)"
FOUND_TTL = 30 * 86400
MISS_TTL = 86400

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = ("product_name,product_name_en,generic_name,brands,serving_size,serving_quantity,"
              "serving_quantity_unit,quantity,nutriments,image_front_small_url,image_front_url,image_url")
UPC_URL = "https://api.upcitemdb.com/prod/trial/lookup?upc={code}"

# Words on labels -> the app's units.
FORM_UNITS = {
    "capsule": "capsule", "capsules": "capsule", "caps": "capsule", "cap": "capsule", "vcaps": "capsule",
    "veggie caps": "capsule", "vegcaps": "capsule",
    "tablet": "tablet", "tablets": "tablet", "tabs": "tablet", "tab": "tablet", "caplet": "tablet", "caplets": "tablet",
    "softgel": "softgel", "softgels": "softgel", "soft gels": "softgel", "gelcap": "softgel", "gelcaps": "softgel",
    "scoop": "scoop", "scoops": "scoop", "drop": "drop", "drops": "drop",
    "gummy": "gummy", "gummies": "gummy", "chewable": "tablet", "chewables": "tablet",
    "packet": "packet", "packets": "packet", "stick": "packet", "sticks": "packet",
}
MASS_UNITS = {"g": "g", "gr": "g", "gram": "g", "grams": "g", "mg": "mg", "mcg": "mcg", "µg": "mcg", "ug": "mcg",
              "iu": "IU", "ml": "mL"}

# Nutrients shown on the product sheet (Open Food Facts stores masses in grams).
NUTRIENTS = [
    ("energy-kcal", "Calories", "kcal"), ("proteins", "Protein", "g"), ("carbohydrates", "Carbs", "g"),
    ("sugars", "Sugar", "g"), ("fat", "Fat", "g"), ("fiber", "Fiber", "g"), ("sodium", "Sodium", "mg"),
    ("vitamin-d", "Vitamin D", "mcg"), ("vitamin-b12", "Vitamin B12", "mcg"), ("vitamin-c", "Vitamin C", "mg"),
    ("vitamin-a", "Vitamin A", "mcg"), ("vitamin-k", "Vitamin K", "mcg"), ("folates", "Folate", "mcg"),
    ("calcium", "Calcium", "mg"), ("iron", "Iron", "mg"), ("magnesium", "Magnesium", "mg"),
    ("zinc", "Zinc", "mg"), ("potassium", "Potassium", "mg"), ("caffeine", "Caffeine", "mg"),
]
TO_G = {"g": 1, "mg": 1e-3, "mcg": 1e-6, "kcal": 1}


def normalize(raw: str) -> str | None:
    """Digits only; GTIN-8/12/13/14 with a valid check digit, as 13 digits where possible."""
    code = re.sub(r"\D", "", raw or "")
    if len(code) not in (8, 12, 13, 14):
        return None
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        return None
    return code.zfill(13) if len(code) == 12 else code


def _get_json(url: str, timeout: float = 8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https hosts)
        return json.loads(r.read())


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None  # drop NaN/negatives


def dose_from_text(text: str) -> tuple[float, str] | None:
    """'CoQ10 200mg Softgels' -> (200, 'mg'). Only an explicit number+unit counts."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(mcg|µg|ug|mg|iu|g)\b", text or "", re.I)
    if not m:
        return None
    amt = float(m.group(1).replace(",", "."))
    return (amt, MASS_UNITS[m.group(2).lower()]) if amt > 0 else None


def form_from_text(text: str) -> str | None:
    t = (text or "").lower()
    for word in sorted(FORM_UNITS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", t):
            return FORM_UNITS[word]
    return None


def parse_serving(text: str) -> tuple[float, str] | None:
    """'2 capsules' -> (2, 'capsule'); '1 scoop (31 g)' -> (1, 'scoop'); '1 portion (30.4 g)' -> (30.4, 'g')."""
    t = (text or "").strip().lower()
    if not t:
        return None
    m = re.match(r"(\d+(?:[.,]\d+)?)\s*([a-zµ ]+?)\s*(?:\(|$|,)", t)
    if m:
        amt = float(m.group(1).replace(",", "."))
        word = m.group(2).strip()
        if word in FORM_UNITS and amt > 0:
            return amt, FORM_UNITS[word]
        if word in MASS_UNITS and amt > 0:
            return amt, MASS_UNITS[word]
    inner = re.search(r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|µg|g|ml|iu)\b", t)
    if inner:
        return float(inner.group(1).replace(",", ".")), MASS_UNITS[inner.group(2)]
    return None


def _clean_name(name: str, brand: str) -> str:
    n = re.sub(r"\s+", " ", name or "").strip()
    if brand and n.lower().startswith(brand.lower() + " "):
        n = n[len(brand) + 1:]
    n = re.split(r"\s+[-–|]\s+|,\s", n)[0]  # retail titles: "X 200mg Softgels - 40ct, Heart Health"
    return n[:60].strip()


def _nutrition(nm: dict) -> list[dict]:
    out = []
    for key, label, unit in NUTRIENTS:
        v = _num(nm.get(f"{key}_serving"))
        if v is None or (v == 0 and key not in ("energy-kcal", "proteins", "carbohydrates", "fat", "sugars")):
            continue
        val = v / TO_G[unit] if unit != "kcal" else v
        out.append({"label": label, "value": round(val, 2 if val < 10 else 1 if val < 100 else 0), "unit": unit})
    return out


def _from_off(code: str) -> dict | None:
    for c in dict.fromkeys([code, code.lstrip("0").zfill(12), code.lstrip("0").zfill(13)]):
        try:
            d = _get_json(OFF_URL.format(code=c) + "?fields=" + OFF_FIELDS)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        p = d.get("product") if d.get("status") == 1 else None
        if not p:
            continue
        brand = (p.get("brands") or "").split(",")[0].strip()
        name = _clean_name(p.get("product_name_en") or p.get("product_name") or p.get("generic_name") or "", brand)
        if not name:
            continue
        serving_text = (p.get("serving_size") or "").strip()
        dose = dose_from_text(name)
        serving = parse_serving(serving_text)
        amount, unit = dose or serving or (None, form_from_text(name) or "")
        return {
            "name": name, "brand": brand, "source": "Open Food Facts",
            "source_url": f"https://world.openfoodfacts.org/product/{c}",
            "image_url": p.get("image_front_small_url") or p.get("image_front_url") or p.get("image_url") or "",
            "amount": amount, "unit": unit, "serving_text": serving_text,
            "package": (p.get("quantity") or "").strip(),
            "nutrition": _nutrition(p.get("nutriments") or {}),
        }
    return None


def _from_upcitemdb(code: str) -> dict | None:
    c = code[1:] if len(code) == 13 and code.startswith("0") else code
    d = _get_json(UPC_URL.format(code=c))
    items = d.get("items") or []
    if not items:
        return None
    it = items[0]
    title = (it.get("title") or "").strip()
    brand = (it.get("brand") or "").strip()
    name = _clean_name(title, brand)
    if not name:
        return None
    dose = dose_from_text(title)
    form = form_from_text(title)
    amount, unit = dose or (None, form or "")
    imgs = [u for u in it.get("images") or [] if isinstance(u, str) and u.startswith("https://")]
    count = re.search(r"(\d+)\s*(?:ct|count)\b", title, re.I)
    return {
        "name": name, "brand": brand, "source": "UPCitemdb", "source_url": f"https://www.upcitemdb.com/upc/{c}",
        "image_url": imgs[0] if imgs else "", "amount": amount, "unit": unit,
        "serving_text": "", "package": f"{count.group(1)} {form + 's' if form else 'count'}" if count else "",
        "nutrition": [], "title": title,
    }


def _from_household(code: str) -> dict | None:
    s = db.row("SELECT name, brand, image_url, amount, unit FROM supplements WHERE barcode=? "
               "ORDER BY id DESC LIMIT 1", (code,))
    if not s:
        return None
    return {**s, "source": "your saved supplements", "source_url": "", "serving_text": "",
            "package": "", "nutrition": []}


def lookup(raw: str) -> dict:
    code = normalize(raw)
    if not code:
        return {"code": re.sub(r"\D", "", raw or ""), "found": False, "error": "That isn't a valid product barcode."}
    hit = _from_household(code)
    if hit:
        return {"code": code, "found": True, **hit}
    cached = db.row("SELECT data, fetched_at FROM barcode_cache WHERE code=?", (code,))
    if cached:
        data = json.loads(cached["data"])
        if time.time() - cached["fetched_at"] < (FOUND_TTL if data.get("found") else MISS_TTL):
            return data
    result, errors = None, []
    for fn in (_from_off, _from_upcitemdb):
        try:
            found = fn(code)
        except Exception as e:  # noqa: BLE001
            log.warning("barcode %s via %s failed: %s", code, fn.__name__, e)
            errors.append(fn.__name__)
            continue
        if not found:
            continue
        if not result:
            result = found
        else:  # second source only fills gaps (e.g. the dose in a retail title)
            filled = False
            if result.get("amount") is None and found.get("amount") is not None:
                result["amount"], result["unit"], filled = found["amount"], found["unit"], True
            for k in ("image_url", "package", "brand"):
                if not result.get(k) and found.get(k):
                    result[k], filled = found[k], True
            if filled:
                result["source"] += f" + {found['source']}"
                result["extra_source_url"] = found["source_url"]
        if result.get("amount") is not None and result.get("image_url"):
            break
    data = {"code": code, "found": bool(result), **(result or {})}
    if result or not errors:  # don't cache "not found" when a source was unreachable
        db.execute("INSERT INTO barcode_cache (code, data, fetched_at) VALUES (?,?,?) "
                   "ON CONFLICT(code) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                   (code, json.dumps(data), int(time.time())))
    if not result and errors:
        data["error"] = "Couldn't reach the product databases. Check the server's internet connection."
    return data
