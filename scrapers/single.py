"""
scrapers/single.py — On-demand single-URL scraper dispatcher.

Validates the domain of the given URL, routes it to the correct scraper module,
and returns a raw listing dict in the shared schema, or None on failure.

Usage:
    from scrapers.single import scrape_url
    listing = scrape_url("https://www.argenprop.com/departamento-...")
"""

import base64
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import scrapers.argenprop_scraper as _ap
import scrapers.zonaprop_scraper as _zp
import scrapers.remax_scraper as _rm


# ── PER-SCRAPER FUNCTIONS ──────────────────────────────────────────────────────

def _scrape_argenprop(url: str) -> dict | None:
    session = _ap.make_session()
    resp = _ap.fetch_with_retry(session, url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    item = soup.select_one(_ap.SEL_LISTING_ITEM)
    if item is None:
        return None

    listing = _ap.parse_single_card(item)
    if listing is None:
        return None

    listing = _ap.fetch_detail_page(session, listing, [1.0, 2.0])
    return listing


def _parse_zonaprop_detail(html: str, url: str) -> dict | None:
    """
    Parse a ZonaProp property detail page (e.g. /propiedades/clasificado/...).

    Detail pages don't carry the list-page card element, so we extract data from:
      - Schema.org JSON-LD <script type="application/ld+json"> Apartment object
      - Inline JS  `const mainFeatures = {...}` — feature values incl. surface covered
      - Inline JS  `'precioVenta': "USD NNNNNN"` — asking price
      - Inline JS  `const mapLatOf/mapLngOf` — Base64-encoded coordinates
      - <img> tags on imgar.zonapropcdn.com/avisos — listing photos
    """
    soup = BeautifulSoup(html, "lxml")

    # ── Property ID from URL ─────────────────────────────────────────────────
    id_m = re.search(r"-(\d+)\.html", url)
    property_id = id_m.group(1) if id_m else None

    # ── JSON-LD Apartment ────────────────────────────────────────────────────
    jsonld: dict = {}
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            d = json.loads(tag.string or "")
            if d.get("@type") == "Apartment":
                jsonld = d
                break
        except Exception:
            pass

    if not jsonld and not property_id:
        return None

    title       = jsonld.get("name")
    description = jsonld.get("description")   # may be truncated; overwritten below
    rooms       = jsonld.get("numberOfRooms")
    bedrooms    = jsonld.get("numberOfBedrooms")
    bathrooms   = jsonld.get("numberOfBathroomsTotal")
    floor_size  = (jsonld.get("floorSize") or {}).get("value")
    surface_total: float | None = float(floor_size) if floor_size is not None else None

    addr         = jsonld.get("address") or {}
    neighborhood = addr.get("addressRegion")
    city_raw     = addr.get("addressLocality", "")
    city         = city_raw.split(",")[0].strip() or "Buenos Aires"
    # Strip the internal posting code appended to the street: "Amenabar 3400 (507260105)"
    street_raw   = addr.get("streetAddress", "")
    street_address = re.sub(r"\s*\(\d+\)\s*$", "", street_raw).strip() or None

    # ── mainFeatures: surface covered + fallbacks ────────────────────────────
    surface_covered: float | None = None
    mf_m = re.search(r"const mainFeatures\s*=\s*(\{[^\n]+\})", html)
    if mf_m:
        try:
            mf = json.loads(mf_m.group(1))
            def _fval(key: str) -> str | None:
                return (mf.get(key) or {}).get("value")

            if _fval("CFT101"):
                surface_covered = float(_fval("CFT101"))
            if surface_total is None and _fval("CFT100"):
                surface_total = float(_fval("CFT100"))
            if rooms is None and _fval("CFT1"):
                rooms = int(_fval("CFT1"))
            if bedrooms is None and _fval("CFT2"):
                bedrooms = int(_fval("CFT2"))
            if bathrooms is None and _fval("CFT3"):
                bathrooms = int(_fval("CFT3"))
        except Exception:
            pass

    # ── Price from inline JS `precioVenta` ───────────────────────────────────
    price_usd: float | None = None
    price_currency: str | None = None
    pv_m = re.search(r"['\"]precioVenta['\"][^:]*:\s*['\"]([^'\"]+)['\"]", html)
    if pv_m:
        price_usd, price_currency = _zp.parse_price(pv_m.group(1))

    # ── Coordinates (Base64-encoded) ─────────────────────────────────────────
    coordinates: dict | None = None
    lat_m = re.search(r'const mapLatOf\s*=\s*"([^"]+)"', html)
    lng_m = re.search(r'const mapLngOf\s*=\s*"([^"]+)"', html)
    if lat_m and lng_m:
        try:
            coordinates = {
                "latitude":  float(base64.b64decode(lat_m.group(1)).decode()),
                "longitude": float(base64.b64decode(lng_m.group(1)).decode()),
            }
        except Exception:
            pass

    # ── Full description (replaces truncated JSON-LD version) ────────────────
    for sel in _zp.SEL_DETAIL_DESCRIPTION:
        el = soup.select_one(sel)
        if el:
            full = el.get_text(separator=" ", strip=True)
            if full:
                description = full
                break

    # ── Images ───────────────────────────────────────────────────────────────
    images: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "imgar.zonapropcdn.com/avisos" in src and src not in seen:
            seen.add(src)
            images.append(src)

    return {
        "id":             property_id,
        "title":          title,
        "price_usd":      price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood":  neighborhood,
            "street_address": street_address,
            "city":          city,
            "coordinates":   coordinates,
        },
        "property_details": {
            "rooms":              int(rooms)     if rooms     is not None else None,
            "bedrooms":           int(bedrooms)  if bedrooms  is not None else None,
            "bathrooms":          int(bathrooms) if bathrooms is not None else None,
            "surface_total_m2":   surface_total,
            "surface_covered_m2": surface_covered,
        },
        "description": description,
        "images":      images,
        "url":         url,
        "source":      "zonaprop",
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
        "features":    [],
    }


def _scrape_zonaprop(url: str) -> dict | None:
    scraper = _zp.make_scraper()
    resp = _zp.fetch_with_retry(scraper, url)
    if resp is None:
        return None
    return _parse_zonaprop_detail(resp.text, url)


def _scrape_remax(url: str) -> dict | None:
    session = _rm.make_session()
    resp = _rm.fetch_with_retry(session, url)
    if resp is None:
        return None

    inner = _rm.extract_listings_json(resp.text)
    if inner is None:
        return None

    data = inner.get("data")
    if not data:
        return None

    raw = data[0]
    return _rm.parse_listing(raw)


# ── DOMAIN DISPATCH TABLE ──────────────────────────────────────────────────────

VALID_DOMAINS: dict[str, callable] = {
    "www.argenprop.com":   _scrape_argenprop,
    "www.zonaprop.com.ar": _scrape_zonaprop,
    "www.remax.com.ar":    _scrape_remax,
}


# ── PUBLIC INTERFACE ───────────────────────────────────────────────────────────

def scrape_url(url: str) -> dict | None:
    """
    Validate the URL domain, dispatch to the correct scraper, and return
    a raw listing dict in the shared scraper schema.

    Raises:
        ValueError: if the domain is not in VALID_DOMAINS.

    Returns:
        dict with the raw listing (English snake_case keys), or None on failure.
    """
    domain = urlparse(url).netloc
    fn = VALID_DOMAINS.get(domain)
    if fn is None:
        raise ValueError(f"Domain not supported: {domain}")
    return fn(url)
