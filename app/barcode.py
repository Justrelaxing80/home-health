"""Barcode -> product details for adding supplements.

Sources, all free:
  * products already saved in this household (any profile);
  * the NIH Dietary Supplement Label Database (DSLD): ~200k US supplement labels
    with per-serving amounts of every ingredient;
  * Open Food Facts: open data, worldwide, many protein powders and vitamins;
  * USDA FoodData Central branded foods (shakes, bars, powders). Uses the shared
    DEMO_KEY unless FDC_API_KEY is set (free key at api.data.gov/signup);
  * UPCitemdb's free trial endpoint (100 lookups/day per IP) for retail titles
    and photos.
DSLD and Open Food Facts are asked in parallel; the other two only when needed.
The best match leads and the others fill its gaps. Results are cached in SQLite.
Nothing is invented: every field comes from one of these sources, and the
response says which. Products can also be found by name (search()).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger("home-health.barcode")

UA = "HomeHealth/1.0 (self-hosted household health dashboard)"
FOUND_TTL = 30 * 86400
MISS_TTL = 86400
CACHE_VERSION = 2  # bump when sources change, so older answers (especially misses) are fetched again

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = ("product_name,product_name_en,generic_name,brands,serving_size,serving_quantity,"
              "serving_quantity_unit,quantity,nutriments,image_front_small_url,image_front_url,image_url")
UPC_URL = "https://api.upcitemdb.com/prod/trial/lookup?upc={code}"
OFF_SEARCH = "https://search.openfoodfacts.org/search"
DSLD = "https://api.ods.od.nih.gov/dsld/v9"
FDC_SEARCH = "https://api.nal.usda.gov/fdc/v1/foods/search"
SRC_DSLD, SRC_OFF, SRC_FDC, SRC_UPC = ("NIH Supplement Label Database", "Open Food Facts",
                                       "USDA FoodData Central", "UPCitemdb")
PRIORITY = [SRC_DSLD, SRC_OFF, SRC_FDC, SRC_UPC]

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


def same_code(a: str, b: str) -> bool:
    a, b = re.sub(r"\D", "", a or "").lstrip("0"), re.sub(r"\D", "", b or "").lstrip("0")
    return bool(a) and a == b


def _upc12(code: str) -> str | None:
    return code[1:] if len(code) == 13 and code.startswith("0") else (code if len(code) == 12 else None)


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
    m = re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])|\d+(?:[.,]\d+)?)\s*(mcg|µg|ug|mg|iu|g)\b", text or "", re.I)
    if not m:
        return None
    num = m.group(1)  # "2,000" is two thousand; "2,5" (European decimal comma) is two and a half
    amt = float(num.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", num) else num.replace(",", "."))
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


DSLD_UNITS = {"gram": "g", "milligram": "mg", "microgram": "mcg", "milliliter": "mL", "calorie": "kcal",
              "kcal": "kcal", "iu": "IU", "g": "g", "mg": "mg", "mcg": "mcg", "ml": "mL"}
MACRO_NAMES = {"calories": "Calories", "protein": "Protein", "total carbohydrates": "Carbs",
               "total carbohydrate": "Carbs", "carbohydrate": "Carbs", "total fat": "Fat", "fat": "Fat",
               "total sugars": "Sugar", "sugars": "Sugar", "sugar": "Sugar", "dietary fiber": "Fiber"}
KEEP_UPPER = {"IU", "DHA", "EPA", "MCT", "BCAA", "HMB", "MSM", "CBD", "NAC", "SAM-E", "GABA", "ZMA", "DIM", "HCL",
              "CLA", "ALA", "USA", "MK-7"}
LOWER = {"mg", "mcg", "g", "ml", "oz", "fl", "lb", "lbs", "ct", "and", "with", "of", "for", "in", "the", "a"}


def tidy_case(t: str) -> str:
    """'VITAMIN D3 5000 IU SOFTGELS' -> 'Vitamin D3 5000 IU Softgels'. Leaves mixed-case text alone."""
    if not t or not t.isupper():
        return t
    out = []
    for i, w in enumerate(t.split()):
        core = w.strip("(),.;:")
        if re.fullmatch(r"\d+(\.\d+)?(MG|MCG|G|ML|OZ|LB|LBS|CT)", core):
            out.append(w.lower())
        elif core in KEEP_UPPER or re.fullmatch(r"[A-Z]{1,2}\d{1,2}", core):  # IU, B12, D3, K2
            out.append(w)
        elif i and core.lower() in LOWER:
            out.append(w.lower())
        else:
            out.append("-".join(p.capitalize() for p in w.split("-")))
    return " ".join(out)


def _unit_word(u: str) -> tuple[str, str]:
    """DSLD unit like 'Softgel(s)' -> ('softgel', 'softgel'); 'Gram(s)' -> ('g', 'gram')."""
    w = re.sub(r"\((s|es|ies)\)", "", (u or "").strip(), flags=re.I).strip().lower()
    return (FORM_UNITS.get(w) or FORM_UNITS.get(w + "s") or DSLD_UNITS.get(w) or MASS_UNITS.get(w) or w), w


def _count_text(qty, unit: str) -> str:
    """(40, 'Softgel(s)') -> '40 softgels'; (30.4, 'Gram(s)') -> '30.4 g'."""
    if _num(qty) is None:
        return ""
    unit_, word = _unit_word(unit)
    q = int(qty) if float(qty).is_integer() else qty
    if unit_ in ("g", "mg", "mcg", "mL", "IU", "kcal"):
        return f"{q} {unit_}"
    if q != 1 and not word.endswith("s"):
        word = word[:-1] + "ies" if re.search(r"[^aeiou]y$", word) else word + "s"
    return f"{q} {word}".strip()


def _fmt_val(v: float) -> float | int:
    r = round(v, 2 if v < 10 else 1 if v < 100 else 0)
    return int(r) if float(r).is_integer() else r


def _dsld_nutrition(label: dict) -> tuple[list[dict], list[tuple[float, str]]]:
    """Per-serving rows (first serving size) and the active ingredient amounts in mass units."""
    out, actives = [], []
    for r in label.get("ingredientRows") or []:
        q = next((x for x in r.get("quantity") or [] if x.get("servingSizeOrder", 1) == 1), None)
        if not q or _num(q.get("quantity")) is None:
            continue
        v, raw_unit = float(q["quantity"]), (q.get("unit") or "").strip()
        name = (r.get("name") or "").strip()
        label_ = MACRO_NAMES.get(name.lower(), name)
        if raw_unit.upper() in ("NP", "") or not name or (v == 0 and label_ not in MACRO_NAMES.values()):
            continue
        unit = "kcal" if label_ == "Calories" else _unit_word(raw_unit)[0]
        out.append({"label": label_[:40], "value": _fmt_val(v), "unit": unit if len(unit) <= 12 else raw_unit[:12]})
        if label_ not in MACRO_NAMES.values() and unit in ("g", "mg", "mcg", "IU") and v > 0:
            actives.append((v, unit))
    return out[:18], actives


def _dsld_product(label: dict, code: str = "") -> dict:
    brand = tidy_case((label.get("brandName") or "").strip())
    name = _clean_name(tidy_case(label.get("fullName") or ""), brand)
    ss = (label.get("servingSizes") or [{}])[0]
    serving_qty, serving_unit = ss.get("minQuantity"), ss.get("unit") or ""
    serving = (float(serving_qty), _unit_word(serving_unit)[0]) if _num(serving_qty) else None
    serving_text = _count_text(serving_qty, serving_unit) if serving else ""
    # Powders list grams plus a note such as "1 rounded scoop": that's how people measure it.
    note = re.sub(r"\s+", " ", ss.get("notes") or "").strip()
    nm = re.match(r"(\d+(?:\.\d+)?|one|two)\b", note, re.I)
    note_form = form_from_text(note) if nm else None
    if note_form and len(note) <= 40:
        q = {"one": 1.0, "two": 2.0}.get(nm.group(1).lower()) or float(nm.group(1))
        if serving and serving[1] in ("g", "mL"):
            serving_text = f"{note.lower()} ({serving_text})"
        serving = (q, note_form)
    nc = (label.get("netContents") or [{}])[0]
    nutrition, actives = _dsld_nutrition(label)
    dose = dose_from_text(name) or (actives[0] if len(actives) == 1 else None)
    amount, unit = dose or serving or (None, form_from_text(name) or "")
    if amount is not None and float(amount).is_integer():
        amount = int(amount)
    return {
        "name": name, "brand": brand, "source": SRC_DSLD,
        "source_url": f"https://dsld.od.nih.gov/label/{label.get('id')}", "image_url": "",
        "amount": amount, "unit": unit,
        "serving_text": serving_text,
        "package": _count_text(nc.get("quantity"), nc.get("unit") or "") or (nc.get("display") or ""),
        "nutrition": nutrition, "code": code or normalize(label.get("upcSku") or "") or "",
    }


def _from_dsld(code: str) -> dict | None:
    c12 = _upc12(code)
    phrase = f"{c12[0]} {c12[1:6]} {c12[6:11]} {c12[11]}" if c12 else code  # DSLD writes UPCs this way
    d = _get_json(f"{DSLD}/search-filter?q={urllib.parse.quote(chr(34) + phrase + chr(34))}&size=6")
    hits = [h for h in d.get("hits") or [] if h.get("_id")]
    src = lambda h: h.get("_source") or {}  # noqa: E731
    # Newest on-market label first (one product often has several label versions).
    on = sorted([h for h in hits if not src(h).get("offMarket")], key=lambda h: src(h).get("entryDate", ""), reverse=True)
    off = sorted([h for h in hits if src(h).get("offMarket")], key=lambda h: src(h).get("entryDate", ""), reverse=True)
    for h in (on + off)[:4]:
        label = _get_json(f"{DSLD}/label/{urllib.parse.quote(str(h['_id']))}")
        if isinstance(label, dict) and same_code(label.get("upcSku") or "", code):
            return _dsld_product(label, code)
    return None


FDC_NUTRIENTS = {"Energy": ("Calories", "kcal"), "Protein": ("Protein", "g"),
                 "Carbohydrate, by difference": ("Carbs", "g"), "Total lipid (fat)": ("Fat", "g"),
                 "Total Sugars": ("Sugar", "g"), "Fiber, total dietary": ("Fiber", "g"), "Sodium, Na": ("Sodium", "mg"),
                 "Calcium, Ca": ("Calcium", "mg"), "Iron, Fe": ("Iron", "mg"), "Potassium, K": ("Potassium", "mg"),
                 "Vitamin D (D2 + D3)": ("Vitamin D", "mcg"), "Caffeine": ("Caffeine", "mg")}
FDC_SCALE = {"G": 1, "MG": 1e-3, "UG": 1e-6, "KCAL": 1}


def _from_fdc(code: str) -> dict | None:
    key = os.environ.get("FDC_API_KEY") or "DEMO_KEY"
    q = _upc12(code) or code
    d = _get_json(f"{FDC_SEARCH}?api_key={urllib.parse.quote(key)}&dataType=Branded&pageSize=5&query={q}")
    f = next((x for x in d.get("foods") or [] if same_code(x.get("gtinUpc") or "", code)), None)
    if not f:
        return None
    brand = tidy_case((f.get("brandName") or f.get("brandOwner") or "").strip())
    name = _clean_name(tidy_case(f.get("description") or ""), brand)
    if not name:
        return None
    size, size_unit = _num(f.get("servingSize")), (f.get("servingSizeUnit") or "").lower()
    by_weight = bool(size) and size_unit in ("g", "grm", "ml", "mlt")
    su = "g" if size_unit in ("g", "grm") else "mL"
    household = (f.get("householdServingFullText") or "").split("|")[0].strip()
    serving_text = household
    if by_weight:
        serving_text = f"{household} ({_fmt_val(size)} {su})" if household else f"{_fmt_val(size)} {su}"
    nutrition = []
    if by_weight:  # search results are per 100 g / 100 mL
        for n in f.get("foodNutrients") or []:
            m = FDC_NUTRIENTS.get(n.get("nutrientName"))
            v, un = _num(n.get("value")), (n.get("unitName") or "").upper()
            if not m or v is None or un not in FDC_SCALE or (m[1] == "kcal") != (un == "KCAL"):
                continue
            val = v * size / 100 * (1 if m[1] == "kcal" else FDC_SCALE[un] / TO_G[m[1]])
            if val or m[0] in ("Calories", "Protein", "Carbs", "Fat"):
                nutrition.append({"label": m[0], "value": _fmt_val(val), "unit": m[1]})
    serving = parse_serving(household) or (parse_serving(f"{size} {su}") if by_weight else None)
    amount, unit = dose_from_text(name) or serving or (None, form_from_text(name) or "")
    return {
        "name": name, "brand": brand, "source": SRC_FDC,
        "source_url": f"https://fdc.nal.usda.gov/food-details/{f.get('fdcId')}/nutrients", "image_url": "",
        "amount": amount, "unit": unit, "serving_text": serving_text,
        "package": (f.get("packageWeight") or "").strip(), "nutrition": nutrition,
    }


def _search_dsld(q: str) -> list[dict]:
    d = _get_json(f"{DSLD}/search-filter?q={urllib.parse.quote(q)}&size=12")
    out = []
    for h in d.get("hits") or []:
        src = h.get("_source") or {}
        brand = tidy_case((src.get("brandName") or "").strip())
        name = _clean_name(tidy_case(src.get("fullName") or ""), brand)
        if not name or not str(h.get("_id") or "").isdigit():
            continue
        nc = (src.get("netContents") or [{}])[0]
        out.append({"source": "dsld", "ref": str(h["_id"]), "name": name, "brand": brand,
                    "detail": _count_text(nc.get("quantity"), nc.get("unit") or ""), "image_url": "",
                    "off_market": bool(src.get("offMarket"))})
    out.sort(key=lambda r: r["off_market"])  # stable: keeps relevance order otherwise
    return out


def _search_off(q: str) -> list[dict]:
    d = _get_json(f"{OFF_SEARCH}?q={urllib.parse.quote(q)}&page_size=12"
                  "&fields=code,product_name,brands,quantity,image_front_small_url")
    out = []
    for h in d.get("hits") or []:
        brands = h.get("brands") or ""
        brand = (brands[0] if isinstance(brands, list) and brands else str(brands).split(",")[0]).strip()
        name = _clean_name(h.get("product_name") or "", brand)
        code = normalize(str(h.get("code") or ""))
        if not name or not code:
            continue
        img = h.get("image_front_small_url") or ""
        out.append({"source": "off", "ref": code, "name": name, "brand": brand,
                    "detail": (h.get("quantity") or "").strip(), "image_url": img if img.startswith("https://") else "",
                    "off_market": False})
    return out


def search(q: str) -> dict:
    """Find products by name (for barcodes no database knows). Both sources are asked in parallel."""
    q = re.sub(r"\s+", " ", q or "").strip()[:80]
    if len(q) < 2:
        return {"results": [], "error": None}
    results, errors = _parallel([_search_dsld, _search_off], q)
    a, b = results.get(_search_dsld) or [], results.get(_search_off) or []
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in ("of", "the", "and", "with", "for")]

    def score(r):
        hay = f"{r['brand']} {r['name']}".lower()
        return sum(1 for w in words if re.search(rf"\b{re.escape(w)}", hay))
    # Open Food Facts search is fuzzy: keep only results containing (nearly) all the words.
    b = [r for r in b if score(r) >= max(1, len(words) - 1)]
    merged, seen = [], set()
    for i in range(max(len(a), len(b))):  # interleave supplement labels and Open Food Facts
        for lst in (a, b):
            if i < len(lst):
                k = (lst[i]["brand"].lower(), lst[i]["name"].lower())
                if k not in seen:
                    seen.add(k)
                    merged.append(lst[i])
    merged.sort(key=lambda r: (-score(r), r["off_market"]))  # stable: source relevance breaks ties
    err = "Couldn't reach the product databases. Check the server's internet connection." \
        if len(errors) == 2 else None
    return {"results": merged[:20], "error": err}


def product(source: str, ref: str) -> dict | None:
    """Full details for a search result."""
    if source == "dsld" and ref.isdigit():
        label = _get_json(f"{DSLD}/label/{ref}")
        return _dsld_product(label) if isinstance(label, dict) and label.get("fullName") else None
    code = normalize(ref) if source == "off" else None
    if code:
        r = _from_off(code)
        return {**r, "code": code} if r else None
    return None


def _parallel(fns, arg):
    results, errors = {}, []
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futs = {fn: ex.submit(fn, arg) for fn in fns}
    for fn, fut in futs.items():
        try:
            r = fut.result()
        except Exception as e:  # noqa: BLE001
            log.warning("%s(%s) failed: %s", fn.__name__, arg, e)
            errors.append(fn.__name__)
            continue
        if r:
            results[fn] = r
    return results, errors


HEADLINE = ("Calories", "Protein", "Carbs", "Fat", "Sugar", "Fiber")


def _merge(found: list[dict]) -> dict | None:
    """The best source leads; the others fill what it lacks (dose, photo, package, brand, nutrition)."""
    if not found:
        return None
    found = sorted(found, key=lambda r: PRIORITY.index(r["source"]))
    result, used = dict(found[0]), [found[0]["source"]]
    for other in found[1:]:
        filled = False
        if result.get("amount") is None and other.get("amount") is not None:
            result["amount"], result["unit"], filled = other["amount"], other["unit"], True
        for k in ("image_url", "package", "brand", "serving_text", "nutrition"):
            if not result.get(k) and other.get(k):
                result[k], filled = other[k], True
        # Headline numbers a label version left out (e.g. protein) from another label of the same barcode.
        have = {n["label"] for n in result.get("nutrition") or []}
        extra = [n for n in other.get("nutrition") or [] if n["label"] in HEADLINE and n["label"] not in have]
        if extra and result.get("nutrition"):
            result["nutrition"] = list(result["nutrition"]) + extra
            filled = True
        if filled:
            used.append(other["source"])
            result.setdefault("extra_source_url", other["source_url"])
    result["source"] = " + ".join(used)
    return result


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
        if data.get("v") == CACHE_VERSION and time.time() - cached["fetched_at"] < (FOUND_TTL if data.get("found") else MISS_TTL):
            return data
    hits, errors = _parallel([_from_dsld, _from_off], code)
    found = list(hits.values())
    best = _merge(found)
    later = []
    if not best:
        later = [_from_fdc, _from_upcitemdb]
    elif best.get("amount") is None or not best.get("image_url"):
        later = [_from_upcitemdb]  # retail titles carry the dose; listings carry a photo
    if later:
        more, errs = _parallel(later, code)
        found += list(more.values())
        errors += errs
    result = _merge(found)
    if result:
        result.pop("code", None)
    data = {"code": code, "found": bool(result), **(result or {}), "v": CACHE_VERSION}
    if result or not errors:  # don't cache "not found" when a source was unreachable
        db.execute("INSERT INTO barcode_cache (code, data, fetched_at) VALUES (?,?,?) "
                   "ON CONFLICT(code) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                   (code, json.dumps(data), int(time.time())))
    if not result and errors:
        data["error"] = "Couldn't reach the product databases. Check the server's internet connection."
    return data
