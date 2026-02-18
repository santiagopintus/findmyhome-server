"""
MercadoLibre ("meli") Scraper
==============================
Scrapes departamentos for sale in Belgrano, Núñez, Saavedra and Villa Urquiza
(Buenos Aires) within a USD price range and outputs a structured JSON file
compatible with the shared scraper schema.

MercadoLibre is Cloudflare-protected, so this scraper uses `cloudscraper`.
Data is extracted primarily from the embedded `window.__PRELOADED_STATE__` JSON
blob. Falls back to HTML polycard parsing if the JSON is absent or empty.
Detail pages use Next.js and expose a `<script id="__NEXT_DATA__">` blob.

Usage:
    python scrapers/meli_scraper.py

Output:
    output/meli_results_YYYY-MM-DD_HH-MM-SS.json
"""

import json
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone

import cloudscraper
from bs4 import BeautifulSoup, Tag

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── BASE CONFIGURATION ────────────────────────────────────────────────────────
BASE_URL = "https://inmuebles.mercadolibre.com.ar"

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "search_filters.json",
)

BACKOFF_BASE_SECONDS = 2
MAX_RETRIES          = 3

# List-page cards are sparse (no description, often no coords).
# Fetching detail pages fills those gaps.
FETCH_DETAIL_PAGES = True
FETCH_COORDINATES  = True

ITEMS_PER_PAGE = 48   # MercadoLibre's fixed page size; used for offset pagination

SOURCE_NAME     = "meli"
OUTPUT_DIR      = "output"
OUTPUT_FILENAME = "meli_results_{timestamp}.json"

# ── CSS SELECTORS — LIST PAGE (Polaris design, verified Feb 2026) ─────────────
SEL_LISTING_ITEM  = "li.ui-search-layout__item"
SEL_CARD_LINK     = "a.poly-component__title"           # carries href + text title
SEL_PRICE         = "span.andes-money-amount__fraction" # numeric price part
SEL_PRICE_SYMBOL  = "span.andes-money-amount__currency-symbol"
SEL_LOCATION      = "span.poly-component__location"
SEL_ATTRIBUTES    = "ul.poly-attributes-list li"        # feature chips
SEL_IMAGES        = "img.poly-component__picture"
SEL_TOTAL_RESULTS = "span.ui-search-breadcrumb__quantity-results"

# ── CSS SELECTORS — DETAIL PAGE ───────────────────────────────────────────────
SEL_DETAIL_DESCRIPTION = [
    "p.ui-pdp-description__content",
    "div.ui-pdp-description p",
    "section.description p",
]
SEL_DETAIL_FEATURES = [
    "ul.ui-pdp-features li",
    "div.ui-pdp-specs__table tr",
    "section.ui-pdp-characteristics li",
]

# ── ATTRIBUTE IDs (MercadoLibre items API) ────────────────────────────────────
ATTR_ROOMS          = "ROOMS"
ATTR_BEDROOMS       = "BEDROOMS"
ATTR_BATHROOMS      = "BATHROOMS"
ATTR_TOTAL_AREA     = "TOTAL_AREA"
ATTR_COVERED_AREA   = "COVERED_AREA"

# ── REGEX PATTERNS — chip text (HTML fallback) ────────────────────────────────
PAT_ROOMS           = re.compile(r"(\d+)\s*amb",                    re.IGNORECASE)
PAT_BEDROOMS        = re.compile(r"(\d+)\s*dorm",                   re.IGNORECASE)
PAT_BATHROOMS       = re.compile(r"(\d+)\s*ba[ñn]",                 re.IGNORECASE)
PAT_SURFACE_TOTAL   = re.compile(r"([\d.,]+)\s*m[²2]\s*tot",        re.IGNORECASE)
PAT_SURFACE_COVERED = re.compile(r"([\d.,]+)\s*m[²2]\s*(?:cub|cubiertos?)?", re.IGNORECASE)

# ── HTTP HEADERS ──────────────────────────────────────────────────────────────
HEADERS = {
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Cache-Control":   "max-age=0",
}


# ── HTTP LAYER ────────────────────────────────────────────────────────────────

def make_scraper() -> cloudscraper.CloudScraper:
    """
    Create a Cloudflare-aware scraper session.
    Warm up with a homepage hit so CF challenge cookies are set before searches.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    scraper.headers.update(HEADERS)
    try:
        resp = scraper.get(BASE_URL, timeout=20)
        log.info("Session warmed up — homepage status: %d", resp.status_code)
    except Exception as exc:
        log.warning("Homepage warm-up failed (continuing anyway): %s", exc)
    return scraper


def fetch_with_retry(
    scraper: cloudscraper.CloudScraper,
    url: str,
    max_retries: int = MAX_RETRIES,
    backoff_base: float = BACKOFF_BASE_SECONDS,
) -> cloudscraper.CloudScraper | None:
    """GET with exponential-backoff retry. Returns Response on 200, None on failure."""
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                sleep_time = backoff_base ** attempt
                log.info("Retry %d/%d for %s (sleeping %.1fs)", attempt, max_retries, url, sleep_time)
                time.sleep(sleep_time)

            resp = scraper.get(url, timeout=20)

            if resp.status_code == 200:
                return resp
            elif resp.status_code == 403:
                log.warning("403 Forbidden for %s (attempt %d/%d)", url, attempt + 1, max_retries + 1)
                time.sleep(backoff_base ** (attempt + 2))
            elif resp.status_code == 404:
                log.info("404 Not Found: %s — skipping", url)
                return None
            elif resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                log.warning("429 Rate limited — sleeping %ds", retry_after)
                time.sleep(retry_after)
            else:
                log.warning("HTTP %d for %s", resp.status_code, url)

        except Exception as exc:
            log.warning("Request error on attempt %d: %s", attempt + 1, exc)

    log.error("All %d attempts failed for %s", max_retries + 1, url)
    return None


# ── CONFIG ────────────────────────────────────────────────────────────────────

def load_config(config_path: str = CONFIG_FILE) -> dict:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ── URL BUILDER ───────────────────────────────────────────────────────────────

def normalize_neighborhood_slug(name: str) -> str:
    """
    "Núñez" → "nunez", "Villa Urquiza" → "villa-urquiza"
    """
    normalized = unicodedata.normalize("NFD", name.lower())
    ascii_only  = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9-]+", "-", ascii_only.strip()).strip("-")


def build_meli_url(config: dict, page: int = 1) -> str:
    """
    Translate the shared config into a MercadoLibre search URL.

    URL structure (all segments joined with /):
      https://inmuebles.mercadolibre.com.ar
        /departamentos/venta
        /propiedades-individuales                    ← fixed
        /mas-de-{min_bedrooms}-dormitorios           ← from config.features.bedrooms min
        /capital-federal                             ← fixed for Buenos Aires
        /{n1}-o-{n2}-o-{n3}                         ← slugified neighborhoods joined with -o-
        /_PriceRange_{min}USD-{max}USD_NoIndex_True
        _Cocheras_{parking}                          ← from config.features.parking_spots_min

    Pagination: offset-based. Page 1 has no offset suffix.
    Page N (N > 1): append _Desde_{(N-1)*48}_NoIndex_True
    """
    loc      = config.get("location", {})
    price    = config.get("price", {})
    features = config.get("features", {})

    # Neighborhoods → "belgrano-o-nunez-o-saavedra-o-villa-urquiza"
    neighborhoods     = loc.get("neighborhoods", [])
    neighborhood_slug = "-o-".join(normalize_neighborhood_slug(n) for n in neighborhoods)

    # Bedrooms min → "mas-de-2-dormitorios"
    bedrooms     = sorted(features.get("bedrooms", [2]))
    min_bedrooms = min(bedrooms)
    bedroom_seg  = f"mas-de-{min_bedrooms}-dormitorios"

    # Price
    price_min = int(price.get("min", 0))
    price_max = int(price.get("max", 0))
    price_seg = f"_PriceRange_{price_min}USD-{price_max}USD_NoIndex_True"

    # Parking
    parking_min = int(features.get("parking_spots_min", 0))
    parking_seg = f"_Cocheras_{parking_min}" if parking_min > 0 else ""

    path = (
        f"/departamentos/venta"
        f"/propiedades-individuales"
        f"/{bedroom_seg}"
        f"/capital-federal"
        f"/{neighborhood_slug}"
        f"/{price_seg}{parking_seg}"
    )

    url = BASE_URL + path

    if page > 1:
        offset = (page - 1) * ITEMS_PER_PAGE
        url += f"_Desde_{offset}_NoIndex_True"

    return url


# ── JSON EXTRACTION ───────────────────────────────────────────────────────────

def extract_preloaded_state(html: str) -> dict | None:
    """
    Extract window.__PRELOADED_STATE__ from an inline <script> tag.

    Strategy A: regex on the raw HTML string.
    Strategy B: BeautifulSoup script-tag scan (handles minor HTML entity variants).
    """
    # Strategy A — regex
    pat = re.compile(
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.+)',
        re.DOTALL,
    )
    m = pat.search(html)
    if m:
        # The blob ends at the closing </script> tag — try to extract valid JSON
        candidate = m.group(1)
        # Strip everything from </script> onward
        candidate = re.split(r'</script>', candidate, maxsplit=1)[0].rstrip(";").strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Strategy B — BeautifulSoup script-tag scan
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script"):
        txt = tag.string or ""
        if "__PRELOADED_STATE__" not in txt:
            continue
        idx = txt.find("=", txt.find("__PRELOADED_STATE__"))
        if idx == -1:
            continue
        candidate = txt[idx + 1:].strip().rstrip(";").strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    log.debug("window.__PRELOADED_STATE__ not found in HTML")
    return None


def _extract_next_data(html: str) -> dict | None:
    """
    Extract the Next.js __NEXT_DATA__ JSON from a MercadoLibre detail page.
    """
    soup = BeautifulSoup(html, "lxml")
    tag  = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            pass

    # Fallback: regex
    m = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>\s*(\{.+?\})\s*</script>',
        html, re.DOTALL,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _find_item_in_next_data(next_data: dict) -> dict:
    """
    Navigate the __NEXT_DATA__ tree to find the item dict.
    Tries several candidate paths that MercadoLibre has used.
    """
    candidates = [
        lambda d: d["props"]["pageProps"]["item"],
        lambda d: d["props"]["pageProps"]["initialData"]["item"],
        lambda d: d["initialData"]["item"],
        lambda d: d["props"]["pageProps"]["initialState"]["item"],
        lambda d: d["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["item"],
    ]
    for fn in candidates:
        try:
            result = fn(next_data)
            if result and isinstance(result, dict):
                return result
        except (KeyError, IndexError, TypeError):
            continue
    log.debug("Could not locate item dict in __NEXT_DATA__")
    return {}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _attr_value(attributes: list[dict], attr_id: str) -> str | None:
    """Extract value_name for a given attribute ID from the attributes list."""
    for attr in attributes:
        if attr.get("id") == attr_id:
            return attr.get("value_name")
    return None


def _safe_int(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return None


def _safe_float(val: str | None) -> float | None:
    if val is None:
        return None
    cleaned = str(val).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _normalise_id(raw_id: str) -> str:
    """
    Normalise an MLA ID to the canonical form without dashes.
    "MLA-2152589906" → "MLA2152589906"
    "MLA2152589906"  → "MLA2152589906"
    """
    return re.sub(r"^(MLA)-", r"\1", str(raw_id))


# ── JSON ITEM PARSER ──────────────────────────────────────────────────────────

def parse_item_from_json(item: dict) -> dict | None:
    """
    Parse one item from the __PRELOADED_STATE__ results list into the shared schema.

    Expected item shape (simplified MercadoLibre Items API):
    {
      "id": "MLA2152589906",
      "title": "Departamento 3 ambientes...",
      "price": {"amount": 155000, "currency_id": "USD"},
      "seller_address": {
        "neighborhood": {"name": "Villa Urquiza"},
        "city":         {"name": "Capital Federal"},
        "address_line": "Av. Triunvirato 1234",
        "latitude":     -34.58,
        "longitude":    -58.48
      },
      "permalink":  "https://departamento.mercadolibre.com.ar/MLA-...",
      "pictures":   [{"secure_url": "..."}],
      "thumbnail":  "https://...",
      "attributes": [{"id": "ROOMS", "value_name": "3"}, ...]
    }
    """
    raw_id = item.get("id", "")
    if not raw_id:
        return None

    property_id = _normalise_id(str(raw_id))
    title       = item.get("title")
    permalink   = item.get("permalink") or item.get("url")

    # Price
    price_data     = item.get("price") or {}
    price_amount   = price_data.get("amount")
    price_currency = (price_data.get("currency_id") or "").upper() or None
    price_usd: float | None = float(price_amount) if price_amount is not None else None

    # Location
    seller_addr  = item.get("seller_address") or {}
    neighborhood = (seller_addr.get("neighborhood") or {}).get("name")
    city_raw     = (seller_addr.get("city") or {}).get("name") or "Buenos Aires"
    street_addr  = seller_addr.get("address_line")

    # Coordinates (may be absent on list pages)
    coordinates: dict | None = None
    lat = seller_addr.get("latitude")
    lng = seller_addr.get("longitude")
    if lat is not None and lng is not None:
        try:
            coordinates = {"latitude": float(lat), "longitude": float(lng)}
        except (TypeError, ValueError):
            pass

    # Attributes → property details
    attributes = item.get("attributes") or []
    rooms     = _safe_int(_attr_value(attributes, ATTR_ROOMS))
    bedrooms  = _safe_int(_attr_value(attributes, ATTR_BEDROOMS))
    bathrooms = _safe_int(_attr_value(attributes, ATTR_BATHROOMS))
    surface_total   = _safe_float(_attr_value(attributes, ATTR_TOTAL_AREA))
    surface_covered = _safe_float(_attr_value(attributes, ATTR_COVERED_AREA))

    # Images
    images: list[str] = []
    for pic in (item.get("pictures") or []):
        src = pic.get("secure_url") or pic.get("url")
        if src:
            images.append(src)
    if not images and item.get("thumbnail"):
        images.append(item["thumbnail"])

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
        "description": None,   # not in search-result JSON; fetched from detail page
        "images":      images,
        "url":         permalink,
        "source":      SOURCE_NAME,
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
        "features":    [],
    }


# ── HTML FALLBACK PARSERS ─────────────────────────────────────────────────────

def _parse_price_html(item: Tag) -> tuple[float | None, str | None]:
    """
    Extract price from a polycard HTML element.
    Currency symbol variants: "U$S" / "US$" → USD, "$" → ARS.
    Argentine thousands separator is ".".
    """
    symbol_el   = item.select_one(SEL_PRICE_SYMBOL)
    fraction_el = item.select_one(SEL_PRICE)

    if not fraction_el:
        return None, None

    symbol_text = (symbol_el.get_text(strip=True) if symbol_el else "").upper()
    if any(s in symbol_text for s in ("U$S", "US$", "USD")):
        currency = "USD"
    elif "$" in symbol_text:
        currency = "ARS"
    else:
        currency = None

    numeric = fraction_el.get_text(strip=True).replace(".", "").replace(",", ".")
    try:
        return float(numeric), currency
    except ValueError:
        return None, currency


def _parse_attributes_html(item: Tag) -> dict:
    """
    Parse polycard attribute chips into structured property details.
    Chip text examples: "3 ambientes", "2 dormitorios", "70 m² tot.", "60 m² cub."
    """
    result: dict = {
        "rooms":              None,
        "bedrooms":           None,
        "bathrooms":          None,
        "surface_total_m2":   None,
        "surface_covered_m2": None,
    }

    for chip in item.select(SEL_ATTRIBUTES):
        t = chip.get_text(strip=True)

        m = PAT_ROOMS.search(t)
        if m and result["rooms"] is None:
            result["rooms"] = int(m.group(1))

        m = PAT_BEDROOMS.search(t)
        if m and result["bedrooms"] is None:
            result["bedrooms"] = int(m.group(1))

        m = PAT_BATHROOMS.search(t)
        if m and result["bathrooms"] is None:
            result["bathrooms"] = int(m.group(1))

        m_tot = PAT_SURFACE_TOTAL.search(t)
        if m_tot and result["surface_total_m2"] is None:
            result["surface_total_m2"] = _safe_float(m_tot.group(1))
        else:
            m_cov = PAT_SURFACE_COVERED.search(t)
            if m_cov and result["surface_covered_m2"] is None:
                result["surface_covered_m2"] = _safe_float(m_cov.group(1))

    return result


def _parse_card_html(item: Tag) -> dict | None:
    """
    HTML fallback: parse one li.ui-search-layout__item polycard into a listing dict.
    Only called when JSON extraction yields 0 results.
    """
    link_tag = item.select_one(SEL_CARD_LINK)
    if not link_tag:
        return None

    href = link_tag.get("href", "")
    if not href:
        return None

    id_m = re.search(r"MLA-?(\d+)", href)
    property_id = f"MLA{id_m.group(1)}" if id_m else None

    title            = link_tag.get_text(strip=True) or None
    price_usd, price_currency = _parse_price_html(item)

    location_el   = item.select_one(SEL_LOCATION)
    location_text = location_el.get_text(strip=True) if location_el else None
    neighborhood: str | None = None
    if location_text:
        parts = [p.strip() for p in location_text.split(",", 1)]
        neighborhood = parts[0] or None

    property_details = _parse_attributes_html(item)

    images: list[str] = []
    for img in item.select(SEL_IMAGES):
        src = img.get("data-src") or img.get("src") or ""
        if src.startswith("http") and "mlstatic.com" in src:
            images.append(src)

    return {
        "id":             property_id,
        "title":          title,
        "price_usd":      price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood":   neighborhood,
            "street_address": None,
            "city":           "Buenos Aires",
            "coordinates":    None,
        },
        "property_details": property_details,
        "description":    None,
        "images":         images,
        "url":            href,
        "source":         SOURCE_NAME,
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
        "features":       [],
    }


# ── PAGE PARSER (JSON + HTML) ─────────────────────────────────────────────────

def parse_listing_page(html: str, url: str) -> list[dict]:
    """
    Top-level parser for a MercadoLibre search results page.
    Tries JSON extraction first; falls back to HTML polycard parsing.
    """
    # ── Primary: __PRELOADED_STATE__ JSON ───────────────────────────────────
    state = extract_preloaded_state(html)
    if state:
        log.debug("__PRELOADED_STATE__ top-level keys: %s", list(state.keys()))
        initial = state.get("initialState") or state
        if isinstance(initial, dict):
            log.debug("initialState top-level keys: %s", list(initial.keys()))

        # Try several known paths to the results list
        results: list[dict] = []
        candidates = [
            lambda s: s.get("initialState", {}).get("results", []),
            lambda s: s.get("initialState", {}).get("listingItems", {}).get("results", []),
            lambda s: s.get("results", []),
            lambda s: s.get("initialState", {}).get("search", {}).get("results", []),
            lambda s: s.get("initialState", {}).get("items", []),
        ]
        for fn in candidates:
            try:
                r = fn(state)
                if isinstance(r, list) and r:
                    results = r
                    log.info("JSON extraction: found %d items", len(results))
                    break
            except (KeyError, TypeError, AttributeError):
                continue

        if results:
            listings = []
            for item in results:
                try:
                    parsed = parse_item_from_json(item)
                    if parsed:
                        listings.append(parsed)
                except Exception as exc:
                    log.warning("Failed to parse JSON item: %s", exc)
            return listings
        else:
            log.warning(
                "JSON extraction found 0 results — activating HTML fallback. "
                "Check __PRELOADED_STATE__ key path. URL: %s", url
            )

    # ── Fallback: HTML polycard parsing ─────────────────────────────────────
    soup  = BeautifulSoup(html, "lxml")
    items = soup.select(SEL_LISTING_ITEM)
    log.info("HTML fallback: found %d polycard elements", len(items))

    listings = []
    for item in items:
        try:
            parsed = _parse_card_html(item)
            if parsed:
                listings.append(parsed)
        except Exception as exc:
            log.warning("Failed to parse HTML card: %s", exc)

    return listings


# ── PAGINATION ────────────────────────────────────────────────────────────────

def parse_total_results(html: str) -> int | None:
    """
    Extract total listing count. Tries JSON first, then HTML breadcrumb.
    """
    # JSON path
    state = extract_preloaded_state(html)
    if state:
        initial = state.get("initialState") or state
        if isinstance(initial, dict):
            paging = initial.get("paging") or {}
            total  = paging.get("total")
            if total is not None:
                return int(total)

    # HTML fallback
    soup = BeautifulSoup(html, "lxml")
    el   = soup.select_one(SEL_TOTAL_RESULTS)
    if el:
        text = el.get_text(strip=True)
        m = re.search(r"([\d.]+)", text)
        if m:
            return int(m.group(1).replace(".", ""))

    return None


def get_last_page(total_results: int | None, max_pages: int) -> int:
    if not total_results:
        return 1
    pages = (total_results + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    return min(pages, max_pages)


# ── DETAIL PAGE ENRICHMENT ────────────────────────────────────────────────────

def fetch_detail_page(
    scraper: cloudscraper.CloudScraper,
    listing: dict,
    delay_range: list[float],
) -> dict:
    """
    Fetch the individual MercadoLibre property page to enrich the listing.

    Extraction order:
    1. __NEXT_DATA__ JSON → coordinates, description, full images, surface areas
    2. Schema.org JSON-LD → coordinates (geo.latitude / geo.longitude)
    3. HTML selectors     → description and features as last resort

    Always returns the listing dict (modified in-place on success).
    """
    url = listing.get("url")
    if not url:
        return listing

    time.sleep(random.uniform(delay_range[0], delay_range[1]))
    resp = fetch_with_retry(scraper, url)
    if resp is None:
        return listing

    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # ── 1. __NEXT_DATA__ ─────────────────────────────────────────────────────
    next_data = _extract_next_data(html)
    if next_data:
        item = _find_item_in_next_data(next_data)
        if item:
            _enrich_from_item(listing, item)

    # ── 2. Schema.org JSON-LD for coordinates ────────────────────────────────
    if not listing["location"].get("coordinates"):
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
                    listing["location"]["coordinates"] = {
                        "latitude":  float(lat),
                        "longitude": float(lng),
                    }
                except (TypeError, ValueError):
                    pass
                break

    # ── 3. Spec table (tr.andes-table__row th + td) for property details ─────
    details = listing["property_details"]
    if any(v is None for v in details.values()):
        specs: dict[str, str] = {}
        for row in soup.select("tr.andes-table__row"):
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td:
                specs[th.get_text(strip=True)] = td.get_text(strip=True)

        def _s_int(key: str) -> int | None:
            val = specs.get(key)
            if not val:
                return None
            m = re.search(r"(\d+)", val)
            return int(m.group(1)) if m else None

        def _s_float(key: str) -> float | None:
            val = specs.get(key)
            if not val:
                return None
            m = re.search(r"([\d.,]+)", val)
            return _safe_float(m.group(1)) if m else None

        if details["rooms"] is None:
            details["rooms"] = _s_int("Ambientes")
        if details["bedrooms"] is None:
            details["bedrooms"] = _s_int("Dormitorios")
        if details["bathrooms"] is None:
            details["bathrooms"] = _s_int("Ba\u00f1os")
        if details["surface_total_m2"] is None:
            details["surface_total_m2"] = _s_float("Superficie total")
        if details["surface_covered_m2"] is None:
            details["surface_covered_m2"] = _s_float("Superficie cubierta")

    # ── 4. Location enrichment from .ui-vip-location ─────────────────────────
    loc = listing["location"]
    if not loc.get("neighborhood") or not loc.get("street_address"):
        loc_el = soup.select_one(".ui-vip-location")
        if loc_el:
            loc_text = loc_el.get_text(separator=", ", strip=True)
            cleaned  = re.sub(
                r"Ubicaci[oó]n e informaci[oó]n de la zona\s*",
                "", loc_text, flags=re.IGNORECASE,
            ).strip()
            parts = [p.strip() for p in cleaned.split(",") if p.strip()]
            if len(parts) >= 3:
                if not loc.get("street_address"):
                    loc["street_address"] = parts[0] or None
                if not loc.get("neighborhood"):
                    loc["neighborhood"] = parts[1] or None
                if not loc.get("city") or loc["city"] == "Buenos Aires":
                    loc["city"] = parts[2] or "Buenos Aires"

    # ── 5. HTML fallback for description ─────────────────────────────────────
    if not listing.get("description"):
        for sel in SEL_DETAIL_DESCRIPTION:
            el = soup.select_one(sel)
            if el:
                listing["description"] = el.get_text(separator=" ", strip=True)
                break

    # ── 6. Images — real photos only (D_NQ prefix, excludes SVG icons) ───────
    if len(listing.get("images", [])) <= 1:
        seen_imgs: set[str] = set(listing.get("images", []))
        real_imgs: list[str] = list(listing.get("images", []))
        for img in soup.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            if "mlstatic.com" in src and "D_NQ" in src and src not in seen_imgs:
                seen_imgs.add(src)
                real_imgs.append(src)
        if real_imgs:
            listing["images"] = real_imgs

    return listing


def _enrich_from_item(listing: dict, item: dict) -> None:
    """
    Fill listing gaps from a MercadoLibre item dict (from __NEXT_DATA__).
    Modifies listing in-place. All updates are additive (never overwrite existing data).
    """
    # Coordinates
    if not listing["location"].get("coordinates"):
        seller_addr = item.get("seller_address") or {}
        lat = seller_addr.get("latitude")
        lng = seller_addr.get("longitude")
        if lat is not None and lng is not None:
            try:
                listing["location"]["coordinates"] = {
                    "latitude":  float(lat),
                    "longitude": float(lng),
                }
            except (TypeError, ValueError):
                pass

    # Street address
    if not listing["location"].get("street_address"):
        addr_line = (item.get("seller_address") or {}).get("address_line")
        if addr_line:
            listing["location"]["street_address"] = addr_line

    # Description
    if not listing.get("description"):
        listing["description"] = item.get("description") or None

    # Property details from attributes
    attributes = item.get("attributes") or []
    details    = listing["property_details"]

    mapping = [
        ("rooms",              ATTR_ROOMS,         _safe_int),
        ("bedrooms",           ATTR_BEDROOMS,       _safe_int),
        ("bathrooms",          ATTR_BATHROOMS,      _safe_int),
        ("surface_total_m2",   ATTR_TOTAL_AREA,     _safe_float),
        ("surface_covered_m2", ATTR_COVERED_AREA,   _safe_float),
    ]
    for field, attr_id, converter in mapping:
        if details[field] is None:
            details[field] = converter(_attr_value(attributes, attr_id))

    # Full image set (replace thumbnail-only list)
    if len(listing.get("images", [])) <= 1:
        pics = [
            pic.get("secure_url") or pic.get("url")
            for pic in (item.get("pictures") or [])
            if pic.get("secure_url") or pic.get("url")
        ]
        if pics:
            listing["images"] = pics


# ── FILTER AND DEDUPLICATION ──────────────────────────────────────────────────

def filter_listing(listing: dict, config: dict) -> bool:
    """Client-side price/currency verification."""
    price_cfg = config.get("price", {})
    currency  = price_cfg.get("currency", "USD")
    price_min = price_cfg.get("min", 0)
    price_max = price_cfg.get("max", float("inf"))

    if listing.get("price_currency") != currency:
        return False
    price = listing.get("price_usd")
    if price is None:
        return False
    return price_min <= price <= price_max


def deduplicate(listings: list[dict]) -> list[dict]:
    """Remove duplicates by property ID. First occurrence wins."""
    seen: set[str] = set()
    unique: list[dict] = []
    for listing in listings:
        prop_id = listing.get("id")
        if prop_id is None:
            log.warning("Listing without ID — keeping: %s", listing.get("url"))
            unique.append(listing)
        elif prop_id not in seen:
            seen.add(prop_id)
            unique.append(listing)
        else:
            log.debug("Duplicate property ID %s — skipping", prop_id)
    return unique


# ── PAGINATION ENGINE ─────────────────────────────────────────────────────────

def scrape_all_pages(
    scraper: cloudscraper.CloudScraper,
    config: dict,
) -> tuple[list[dict], int | None]:
    """
    Scrape all listing pages (up to config.scraping.max_pages).
    Returns (all_listings, total_result_count_from_site).
    """
    scraping_cfg  = config.get("scraping", {})
    max_pages     = int(scraping_cfg.get("max_pages", 10))
    delay_range   = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    all_listings:  list[dict] = []
    total_results: int | None = None
    last_page:     int        = 1

    for page in range(1, max_pages + 1):
        url = build_meli_url(config, page)
        log.info("── Page %d / max %d ──────────────────────────", page, max_pages)
        log.info("URL: %s", url)

        resp = fetch_with_retry(scraper, url)
        if resp is None:
            log.error("Failed to fetch page %d — stopping pagination", page)
            break

        if page == 1:
            total_results = parse_total_results(resp.text)
            last_page     = get_last_page(total_results, max_pages)
            log.info(
                "Site reports %s total results | Pages to scrape: %d (cap: %d)",
                total_results, last_page, max_pages,
            )

        page_listings = parse_listing_page(resp.text, url)
        log.info("Page %d: parsed %d listings", page, len(page_listings))

        if not page_listings:
            log.info("No listings on page %d — stopping early", page)
            break

        all_listings.extend(page_listings)

        if page >= last_page:
            log.info("Reached last available page (%d)", last_page)
            break

        delay = random.uniform(delay_range[0], delay_range[1])
        log.info("Waiting %.2fs before next page...", delay)
        time.sleep(delay)

    return all_listings, total_results


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def build_output(
    listings: list[dict],
    total_results: int | None,
    search_criteria: dict,
    search_url: str | None = None,
) -> dict:
    return {
        "metadata": {
            "total_results":   total_results,
            "scraped_at":      datetime.now(timezone.utc).isoformat(),
            "search_criteria": search_criteria,
            "search_url":      search_url,
            "listings_count":  len(listings),
        },
        "listings": listings,
    }


def save_output(data: dict) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename  = OUTPUT_FILENAME.format(timestamp=timestamp)
    filepath  = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Output written to: %s", filepath)
    return filepath


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    config       = load_config()
    price_cfg    = config.get("price", {})
    scraping_cfg = config.get("scraping", {})
    delay_range  = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    price_min = price_cfg.get("min", 0)
    price_max = price_cfg.get("max", 0)
    currency  = price_cfg.get("currency", "USD")
    max_pages = scraping_cfg.get("max_pages", 10)

    preview_url = build_meli_url(config, page=1)
    log.info("=== MercadoLibre Scraper starting ===")
    log.info("Config: %s", CONFIG_FILE)
    log.info("Search URL (page 1): %s", preview_url)
    log.info(
        "Price: %s %s–%s | Neighborhoods: %s | Max pages: %d",
        currency, price_min, price_max,
        config.get("location", {}).get("neighborhoods", []),
        max_pages,
    )

    scraper = make_scraper()

    # Phase 1: scrape all list pages
    raw_listings, total_results = scrape_all_pages(scraper, config)
    log.info("Raw listings collected: %d", len(raw_listings))

    # Phase 2: filter by price/currency
    filtered = [l for l in raw_listings if filter_listing(l, config)]
    skipped  = len(raw_listings) - len(filtered)
    if skipped:
        log.info("Filtered out %d listing(s) outside %s %s–%s range", skipped, currency, price_min, price_max)
    log.info("After price filter: %d listings", len(filtered))

    # Phase 3: deduplicate
    unique = deduplicate(filtered)
    log.info("After deduplication: %d unique listings", len(unique))

    # Phase 4: enrich from detail pages
    if FETCH_COORDINATES or FETCH_DETAIL_PAGES:
        log.info(
            "Fetching detail pages for %d listings (coordinates=%s, details=%s)...",
            len(unique), FETCH_COORDINATES, FETCH_DETAIL_PAGES,
        )
        for i, listing in enumerate(unique, 1):
            log.info("Detail page %d/%d: %s", i, len(unique), listing.get("url", ""))
            unique[i - 1] = fetch_detail_page(scraper, listing, delay_range)

    # Phase 5: build and save output
    output   = build_output(unique, total_results, config, search_url=preview_url)
    filepath = save_output(output)
    log.info("=== Done. %d listings saved to %s ===", len(unique), filepath)


if __name__ == "__main__":
    main()
