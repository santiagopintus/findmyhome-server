"""
RE/MAX Scraper
==============
Scrapes property listings from RE/MAX Argentina using their server-side rendered
listing page, which embeds the full dataset as JSON inside a <script> tag.

Coordinates (latitude/longitude) are already included in the listing JSON —
no detail-page fetch required.

Usage:
    pip install -r requirements.txt
    python scrapers/remax_scraper.py

Output:
    output/remax_results_YYYY-MM-DD_HH-MM-SS.json
"""

import json
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse, urljoin

import requests
from bs4 import BeautifulSoup

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── BASE CONFIGURATION ────────────────────────────────────────────────────────
BASE_URL = "https://www.remax.com.ar"

# RE/MAX photo CDN base — rawValue is appended directly.
# Example: "listings/uuid/photoId" → "https://img.remax.com.ar/listings/uuid/photoId"
PHOTO_CDN = "https://img.remax.com.ar/"

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "search_filters.json",
)

BACKOFF_BASE_SECONDS = 2
MAX_RETRIES = 3

# ── REMAX SEARCH URL PARAMETERS ───────────────────────────────────────────────
# These are fixed query parameters for the RE/MAX listing search.
# The `locations` and `pricein` params are built dynamically from the config.
#
# RE/MAX location IDs for target neighborhoods (Buenos Aires Capital):
NEIGHBORHOOD_IDS = {
    "Belgrano":      "25006@Belgrano",
    "Núñez":         "25022@Nunez",
    "Saavedra":      "25035@Saavedra",
    "Villa Urquiza": "25054@Villa%20Urquiza",
    "Coghlan":       "25012@Coghlan",
    "Palermo":       "25029@Palermo",
    "Colegiales":    "25011@Colegiales",
    "Villa Devoto":  "25047@Villa%20Devoto",
    "Caballito":     "25009@Caballito",
    "Nuñez":         "25022@Nunez",   # alternate accent
}

# Fixed filters matching the project's property/operation criteria.
# parking spaces and min covered surface are set dynamically in build_remax_url().
FIXED_PARAMS = {
    "sort":               "-createdAt",
    "in:operationId":     "1",                      # sale
    "in:eStageId":        "0,1,2,3,4",
    "eq:entrepreneurship":"false",
    "in:typeId":          "9,10,11,1,2,3,4,5,6,7,8,12",  # all property types
    "eq:aptCredit":       "true",
    "landingPath":        "",
    "filterCount":        "7",
    "viewMode":           "listViewMode",
}

# ── OUTPUT ────────────────────────────────────────────────────────────────────
OUTPUT_DIR      = "output"
OUTPUT_FILENAME = "remax_results_{timestamp}.json"

# ── HTTP HEADERS ──────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":       "keep-alive",
    "Cache-Control":    "max-age=0",
}


# ── HTTP LAYER ────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    """Create a requests.Session with browser headers and warm-up cookie."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(BASE_URL, timeout=20)
        log.info("Session warmed up — homepage status: %d", resp.status_code)
    except requests.RequestException as exc:
        log.warning("Homepage warm-up failed (will still attempt scraping): %s", exc)
    return session


def fetch_with_retry(
    session: requests.Session,
    url: str,
    max_retries: int = MAX_RETRIES,
    backoff_base: float = BACKOFF_BASE_SECONDS,
) -> requests.Response | None:
    """GET with exponential-backoff retry. Returns Response on 200, None if all fail."""
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                sleep_time = backoff_base ** attempt
                log.info("Retry %d/%d for %s (sleeping %.1fs)", attempt, max_retries, url, sleep_time)
                time.sleep(sleep_time)

            resp = session.get(url, timeout=20)

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

        except requests.Timeout:
            log.warning("Timeout on attempt %d for %s", attempt + 1, url)
        except requests.ConnectionError as exc:
            log.warning("Connection error on attempt %d: %s", attempt + 1, exc)

    log.error("All %d attempts failed for %s", max_retries + 1, url)
    return None


# ── CONFIG ────────────────────────────────────────────────────────────────────

def load_config(config_path: str = CONFIG_FILE) -> dict:
    """Load the shared search_filters.json file."""
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ── URL BUILDER ───────────────────────────────────────────────────────────────

def build_remax_url(config: dict, page: int = 0) -> str:
    """
    Build a RE/MAX listings search URL from the shared config.

    RE/MAX URL structure:
      https://www.remax.com.ar/listings/buy?page=N&pageSize=24&...
        &pricein=1:{min}:{max}
        &in:totalRooms=3,4
        &locations=in::::{neighborhood_ids}:::

    Location IDs are comma-separated RE/MAX internal IDs defined in NEIGHBORHOOD_IDS.
    Pagination is 0-indexed.

    RE/MAX URL translation rules:
      neighborhoods: ["Belgrano", "Núñez"] → "25006@Belgrano,25022@Nunez"
      price: {min: 150000, max: 180000}     → "pricein=1:150000:180000"
      bedrooms: [2, 3]                       → "in:totalRooms=3,4" (rooms = bedrooms + 1)
      parking_spots_min: 1                   → "eq:parkingSpaces=1"
    """
    loc      = config.get("location", {})
    price    = config.get("price", {})
    features = config.get("features", {})

    # Neighborhoods → RE/MAX location IDs
    neighborhoods = loc.get("neighborhoods", [])
    loc_ids = []
    for name in neighborhoods:
        loc_id = NEIGHBORHOOD_IDS.get(name)
        if loc_id:
            loc_ids.append(loc_id)
        else:
            log.warning("No RE/MAX location ID for neighborhood %r — skipping", name)

    locations_param = f"in::::{','.join(loc_ids)}:::"

    # Price range — RE/MAX format: "pricein=currencyId:0:max"
    # RE/MAX only supports an upper price bound in the URL (min is always 0).
    # The price.min from config is enforced client-side via filter_listing().
    # currency 1 = USD, 2 = ARS
    price_max = int(price.get("max", 0))
    pricein   = f"1:0:{price_max}"

    # Rooms — RE/MAX uses totalRooms which equals ambientes (rooms including living)
    # Config has bedrooms [2,3]; in RE/MAX totalRooms = bedrooms + 1 for standard apartments.
    # Fall back to dormitorios_min if bedrooms list is not specified.
    bedrooms = sorted(features.get("bedrooms", []))
    if not bedrooms:
        dormitorios_min_val = int(features.get("dormitorios_min", 0))
        if dormitorios_min_val > 0:
            bedrooms = [dormitorios_min_val]
    if bedrooms:
        total_rooms = [str(b + 1) for b in bedrooms]
        rooms_param = ",".join(total_rooms)
    else:
        rooms_param = ""

    # Parking spaces and min covered surface come from config (dynamic)
    parking_min = int(features.get("parking_spots_min", 0))
    superficie_cubierta_min = float(features.get("superficie_cubierta_min", 0))

    params: dict = {
        "page":     str(page),
        "pageSize": "24",
        **FIXED_PARAMS,
        "pricein":        pricein,
        "locations":      locations_param,
    }
    if rooms_param:
        params["in:totalRooms"] = rooms_param
    if parking_min > 0:
        params["eq:parkingSpaces"] = str(parking_min)
    if superficie_cubierta_min > 0:
        params["gte:dimensionCovered"] = str(int(superficie_cubierta_min))

    base = f"{BASE_URL}/listings/buy"
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base}?{query}"


# ── JSON EXTRACTION ───────────────────────────────────────────────────────────

def extract_listings_json(html: str) -> dict | None:
    """
    RE/MAX embeds the full listings dataset as JSON inside a <script> tag.

    The JSON structure is:
      { "<cache_key>": { "b": { "data": {
          "data": [...listings],
          "page": 0,
          "pageSize": 24,
          "totalPages": N,
          "totalItems": M
      }}}}

    Returns the inner `data` dict (containing `data`, `totalPages`, etc.)
    or None if not found.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script"):
        txt = tag.string or ""
        # The data array contains entityId UUIDs as a reliable fingerprint
        if '"entityId"' not in txt and '"dimensionCovered"' not in txt:
            continue
        try:
            outer = json.loads(txt)
            # Navigate: outer[first_key]["b"]["data"]
            first_key = next(iter(outer))
            inner = outer[first_key]["b"]["data"]
            if isinstance(inner.get("data"), list):
                log.debug("Found RE/MAX listings JSON (key=%r)", first_key)
                return inner
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
            continue
    return None


# ── PHOTO URL BUILDER ─────────────────────────────────────────────────────────

def build_photo_urls(photos_raw: list[dict]) -> list[str]:
    """
    Convert RE/MAX photo rawValue references to full CDN URLs.

    Input:  [{"rawValue": "listings/uuid/photoId"}, ...]
    Output: ["https://img.remax.com.ar/listings/uuid/photoId", ...]
    """
    urls = []
    for p in photos_raw:
        raw = p.get("rawValue", "")
        if raw:
            urls.append(PHOTO_CDN + raw)
    return urls


# ── LISTING PARSER ────────────────────────────────────────────────────────────

def parse_listing(raw: dict) -> dict | None:
    """
    Normalise a single raw RE/MAX listing dict into the shared output schema.

    RE/MAX provides coordinates directly in GeoJSON format:
      location.coordinates = [longitude, latitude]  (note: lng first)

    Coordinates are always populated — no detail page fetch needed.
    """
    entity_id  = raw.get("entityId") or str(raw.get("id") or "")
    listing_id = str(raw.get("id") or "")

    if not entity_id:
        return None

    # URL — built from slug
    slug     = raw.get("slug", "")
    full_url = f"{BASE_URL}/propiedades/{slug}" if slug else None

    # Title
    title = raw.get("title")

    # Price
    price_usd: float | None = None
    price_currency: str | None = None
    currency_val = (raw.get("currency") or {}).get("value", "").upper()
    price_raw    = raw.get("price")
    if isinstance(price_raw, (int, float)) and price_raw > 0:
        price_usd      = float(price_raw)
        price_currency = "USD" if currency_val == "USD" else currency_val or None

    # Location
    geo_label = raw.get("geoLabel", "")   # "Belgrano, Capital Federal"
    address   = raw.get("displayAddress")

    neighborhood: str | None = None
    city: str | None = None
    if geo_label:
        parts = [p.strip() for p in geo_label.split(",", 1)]
        neighborhood = parts[0] or None
        city = parts[1] if len(parts) > 1 else "Buenos Aires"

    # Coordinates — GeoJSON order is [longitude, latitude]
    coordinates: dict | None = None
    loc_data = raw.get("location") or {}
    coords = loc_data.get("coordinates")
    if isinstance(coords, list) and len(coords) == 2:
        try:
            coordinates = {
                "latitude":  float(coords[1]),
                "longitude": float(coords[0]),
            }
        except (TypeError, ValueError):
            pass

    # Property details
    rooms    = raw.get("totalRooms")
    bedrooms = raw.get("bedrooms")
    bathrooms = raw.get("bathrooms")
    surface_total   = raw.get("dimensionTotalBuilt") or raw.get("dimensionLand") or None
    surface_covered = raw.get("dimensionCovered")

    # Photos
    images = build_photo_urls(raw.get("photos") or [])

    return {
        "id": listing_id,
        "title": title,
        "price_usd": price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood": neighborhood,
            "street_address": address,
            "city": city or "Buenos Aires",
            "coordinates": coordinates,
        },
        "property_details": {
            "rooms":              int(rooms)     if rooms     is not None else None,
            "bedrooms":           int(bedrooms)  if bedrooms  is not None else None,
            "bathrooms":          int(bathrooms) if bathrooms is not None else None,
            "surface_total_m2":   float(surface_total)   if surface_total   is not None else None,
            "surface_covered_m2": float(surface_covered) if surface_covered is not None else None,
        },
        "description": None,   # not included in the listing JSON; requires detail page
        "images": images,
        "url": full_url,
        "source": "remax",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "features": [],
    }


# ── FILTERS AND DEDUPLICATION ─────────────────────────────────────────────────

def filter_listing(listing: dict, config: dict) -> bool:
    """
    Keep only listings matching the configured currency, price range,
    minimum bedrooms, and minimum covered surface area.
    """
    price_cfg = config.get("price", {})
    currency  = price_cfg.get("currency", "USD")
    price_min = price_cfg.get("min", 0)
    price_max = price_cfg.get("max", float("inf"))

    if listing.get("price_currency") != currency:
        return False
    price = listing.get("price_usd")
    if price is None:
        return False
    if not (price_min <= price <= price_max):
        return False

    features = config.get("features", {})

    dormitorios_min = int(features.get("dormitorios_min", 0))
    if dormitorios_min > 0:
        bedrooms_val = (listing.get("property_details") or {}).get("bedrooms")
        if bedrooms_val is not None and bedrooms_val < dormitorios_min:
            return False

    superficie_min = float(features.get("superficie_cubierta_min", 0))
    if superficie_min > 0:
        covered = (listing.get("property_details") or {}).get("surface_covered_m2")
        if covered is not None and covered < superficie_min:
            return False

    return True


def deduplicate(listings: list[dict]) -> list[dict]:
    """Remove duplicate listings by property ID. First occurrence wins."""
    seen: set[str] = set()
    unique: list[dict] = []
    for listing in listings:
        prop_id = listing.get("id")
        if prop_id is None:
            unique.append(listing)
        elif prop_id not in seen:
            seen.add(prop_id)
            unique.append(listing)
        else:
            log.debug("Duplicate ID %s — skipping", prop_id)
    return unique


# ── PAGINATION ENGINE ─────────────────────────────────────────────────────────

def scrape_all_pages(
    session: requests.Session,
    config: dict,
) -> tuple[list[dict], int | None]:
    """
    Scrape all listing pages (0-indexed) up to config.scraping.max_pages.
    Returns (all_listings, total_item_count_from_site).
    """
    scraping_cfg = config.get("scraping", {})
    max_pages    = int(scraping_cfg.get("max_pages", 10))
    delay_range  = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    all_listings: list[dict] = []
    total_items: int | None  = None
    last_page: int = 0   # RE/MAX pages are 0-indexed

    for page in range(max_pages):
        url = build_remax_url(config, page)
        log.info("── Page %d / max %d ──────────────────────────", page, max_pages - 1)
        log.info("URL: %s", url)

        resp = fetch_with_retry(session, url)
        if resp is None:
            log.error("Failed to fetch page %d — stopping", page)
            break

        inner = extract_listings_json(resp.text)
        if inner is None:
            log.error("Could not extract JSON from page %d — stopping", page)
            break

        if page == 0:
            total_items = inner.get("totalItems")
            last_page   = min(int(inner.get("totalPages", 1)) - 1, max_pages - 1)
            log.info(
                "Site reports %s total items | %s total pages (hard cap: %d)",
                total_items, inner.get("totalPages"), max_pages,
            )

        raw_listings = inner.get("data") or []
        log.info("Page %d: %d raw listings in JSON", page, len(raw_listings))

        page_listings = []
        for raw in raw_listings:
            parsed = parse_listing(raw)
            if parsed:
                page_listings.append(parsed)

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

    return all_listings, total_items


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def build_output(
    listings: list[dict],
    total_items: int | None,
    search_criteria: dict,
    search_url: str | None = None,
) -> dict:
    return {
        "metadata": {
            "total_results": total_items,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "search_criteria": search_criteria,
            "search_url": search_url,
            "listings_count": len(listings),
        },
        "listings": listings,
    }


def save_output(data: dict) -> str:
    """Write output to output/remax_results_YYYY-MM-DD_HH-MM-SS.json."""
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
    config = load_config()
    price_cfg    = config.get("price", {})
    scraping_cfg = config.get("scraping", {})

    price_min = price_cfg.get("min", 0)
    price_max = price_cfg.get("max", 0)
    currency  = price_cfg.get("currency", "USD")
    max_pages = scraping_cfg.get("max_pages", 10)

    preview_url = build_remax_url(config, page=0)
    log.info("=== RE/MAX Scraper starting ===")
    log.info("Config: %s", CONFIG_FILE)
    log.info("Search URL (page 0): %s", preview_url)
    log.info(
        "Price: %s %s–%s | Neighborhoods: %s | Max pages: %d",
        currency, price_min, price_max,
        config.get("location", {}).get("neighborhoods", []),
        max_pages,
    )

    session = make_session()

    # Phase 1: scrape all pages
    raw_listings, total_items = scrape_all_pages(session, config)
    log.info("Raw listings collected: %d", len(raw_listings))

    # Phase 2: filter by price/currency (client-side verification)
    filtered = [l for l in raw_listings if filter_listing(l, config)]
    skipped  = len(raw_listings) - len(filtered)
    if skipped:
        log.info("Filtered out %d listing(s) outside %s %s–%s range", skipped, currency, price_min, price_max)
    log.info("After price filter: %d listings", len(filtered))

    # Phase 3: deduplicate
    unique = deduplicate(filtered)
    log.info("After deduplication: %d unique listings", len(unique))

    # Phase 4: build and save output
    output   = build_output(unique, total_items, config, search_url=preview_url)
    filepath = save_output(output)

    log.info("=== Done. %d listings saved to %s ===", len(unique), filepath)


if __name__ == "__main__":
    main()
