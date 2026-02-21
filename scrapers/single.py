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
import scrapers.meli_scraper as _ml
import scrapers.properati_scraper as _pt


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
            full = el.get_text(separator="\n", strip=True)
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


def _parse_meli_detail(html: str, url: str) -> dict | None:
    """
    Parse a MercadoLibre property detail page (server-rendered HTML, no JSON blobs).

    Verified selectors (Feb 2026):
    - Title:    h1
    - Price:    [data-andes-money-amount] text (e.g. "US$170.000"), aria-label fallback
    - Location: .ui-vip-location → "STREET, NEIGHBORHOOD, CITY, PROVINCE"
    - Specs:    tr.andes-table__row th+td key-value pairs
    - Images:   img src containing "D_NQ" and "mlstatic.com" (excludes SVG icons)
    """
    # ── Property ID from URL ─────────────────────────────────────────────────
    id_m = re.search(r"MLA-?(\d+)", url)
    property_id = f"MLA{id_m.group(1)}" if id_m else None

    soup = BeautifulSoup(html, "lxml")

    # ── Title ────────────────────────────────────────────────────────────────
    title: str | None = None
    h1 = soup.select_one("h1")
    if h1:
        title = h1.get_text(strip=True) or None

    # ── Price ─────────────────────────────────────────────────────────────────
    price_usd: float | None = None
    price_currency: str | None = None
    price_el = soup.select_one("[data-andes-money-amount]")
    if price_el:
        raw_price = price_el.get_text(strip=True)
        if any(s in raw_price for s in ("US$", "U$S", "USD")):
            price_currency = "USD"
        elif "$" in raw_price:
            price_currency = "ARS"
        numeric = re.sub(r"[^\d.,]", "", raw_price).replace(".", "").replace(",", ".")
        try:
            price_usd = float(numeric) if numeric else None
        except ValueError:
            pass
    # Fallback: aria-label="170000 dólares"
    if price_usd is None:
        m = re.search(r'aria-label="([\d]+)\s+d[oó]lares"', html)
        if m:
            price_usd = float(m.group(1))
            price_currency = "USD"

    # ── Location from .ui-vip-location ───────────────────────────────────────
    neighborhood: str | None = None
    street_addr:  str | None = None
    city_raw = "Buenos Aires"
    loc_el = soup.select_one(".ui-vip-location")
    if loc_el:
        loc_text = loc_el.get_text(separator=", ", strip=True)
        cleaned  = re.sub(
            r"Ubicaci[oó]n e informaci[oó]n de la zona\s*",
            "", loc_text, flags=re.IGNORECASE,
        ).strip()
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(parts) >= 3:
            street_addr  = parts[0] or None
            neighborhood = parts[1] or None
            city_raw     = parts[2] or "Buenos Aires"
        elif len(parts) == 2:
            neighborhood = parts[0] or None
            city_raw     = parts[1] or "Buenos Aires"
        elif len(parts) == 1:
            neighborhood = parts[0]

    # ── Coordinates from Schema.org JSON-LD ──────────────────────────────────
    coordinates: dict | None = None
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            d = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        geo = d.get("geo") or {}
        lat = geo.get("latitude")
        lng = geo.get("longitude")
        if lat and lng:
            try:
                coordinates = {"latitude": float(lat), "longitude": float(lng)}
            except (TypeError, ValueError):
                pass
            break

    # ── Property details from spec table rows ─────────────────────────────────
    # Each row: <th> key </th> <td> value </td>
    specs: dict[str, str] = {}
    for row in soup.select("tr.andes-table__row"):
        th = row.select_one("th")
        td = row.select_one("td")
        if th and td:
            specs[th.get_text(strip=True)] = td.get_text(strip=True)

    def _spec_float(key: str) -> float | None:
        val = specs.get(key)
        if not val:
            return None
        m2 = re.search(r"([\d.,]+)", val)
        return _ml._safe_float(m2.group(1)) if m2 else None

    def _spec_int(key: str) -> int | None:
        val = specs.get(key)
        if not val:
            return None
        m2 = re.search(r"(\d+)", val)
        return int(m2.group(1)) if m2 else None

    rooms           = _spec_int("Ambientes")
    bedrooms        = _spec_int("Dormitorios")
    bathrooms       = _spec_int("Ba\u00f1os")
    surface_total   = _spec_float("Superficie total")
    surface_covered = _spec_float("Superficie cubierta")

    # ── Description ──────────────────────────────────────────────────────────
    description: str | None = None
    for sel in _ml.SEL_DETAIL_DESCRIPTION:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if text:
                description = text
                break

    # ── Images — only real listing photos (D_NQ prefix, not SVG icons) ───────
    images: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "mlstatic.com" in src and "D_NQ" in src and src not in seen:
            seen.add(src)
            images.append(src)

    if not property_id:
        return None

    return {
        "id":             property_id,
        "title":          title,
        "price_usd":      price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood":   neighborhood,
            "street_address": street_addr,
            "city":           city_raw,
            "coordinates":    coordinates,
        },
        "property_details": {
            "rooms":              rooms,
            "bedrooms":           bedrooms,
            "bathrooms":          bathrooms,
            "surface_total_m2":   surface_total,
            "surface_covered_m2": surface_covered,
        },
        "description": description,
        "images":      images,
        "url":         url,
        "source":      "meli",
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
        "features":    [],
    }


def _scrape_meli(url: str) -> dict | None:
    scraper = _ml.make_scraper()
    resp = _ml.fetch_with_retry(scraper, url)
    if resp is None:
        return None
    return _parse_meli_detail(resp.text, url)


def _scrape_properati(url: str) -> dict | None:
    """
    Scrape a single Properati property detail page.
    Tries __NEXT_DATA__ first, then Schema.org JSON-LD, then HTML selectors.
    """
    scraper = _pt.make_scraper()
    resp    = _pt.fetch_with_retry(scraper, url)
    if resp is None:
        return None

    # Bootstrap a minimal listing dict and enrich it from the detail page
    id_m    = re.search(r"/(\d+)/?(?:\?|$)|[-_](\d+)/?(?:\?|$)", url)
    prop_id = None
    if id_m:
        prop_id = id_m.group(1) or id_m.group(2)

    listing: dict = {
        "id":             prop_id,
        "title":          None,
        "price_usd":      None,
        "price_currency": None,
        "location": {
            "neighborhood":   None,
            "street_address": None,
            "city":           "Buenos Aires",
            "coordinates":    None,
        },
        "property_details": {
            "rooms":              None,
            "bedrooms":           None,
            "bathrooms":          None,
            "surface_total_m2":   None,
            "surface_covered_m2": None,
        },
        "description": None,
        "images":      [],
        "url":         url,
        "source":      "properati",
        "scraped_at":  __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "features":    [],
    }

    return _pt.fetch_detail_page(scraper, listing, [1.0, 2.0])


# ── DOMAIN DISPATCH TABLE ──────────────────────────────────────────────────────

VALID_DOMAINS: dict[str, callable] = {
    "www.argenprop.com":                  _scrape_argenprop,
    "www.zonaprop.com.ar":                _scrape_zonaprop,
    "www.remax.com.ar":                   _scrape_remax,
    "inmuebles.mercadolibre.com.ar":      _scrape_meli,
    "departamento.mercadolibre.com.ar":   _scrape_meli,
    "www.properati.com.ar":               _scrape_properati,
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
