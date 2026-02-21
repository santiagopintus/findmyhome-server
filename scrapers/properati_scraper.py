"""
Properati Scraper
=================
Scrapes departamentos (and houses) for sale in Belgrano, Núñez, Saavedra,
and Villa Urquiza (Buenos Aires) within a USD price range and outputs a
structured JSON file compatible with the shared scraper schema.

Properati uses geo-ID query parameters for neighborhood filtering. The IDs
live in config/search_filters.json under "properati.geo_ids".

URL format (verified Feb 2026):
  https://www.properati.com.ar/s/venta
    ?geos=3697%2C3657%2C3652%2C3698     ← comma-separated geo IDs (URL-encoded)
    &bedrooms=3
    &minPrice=100000
    &maxPrice=175000
    &propertyType=apartment%2Chouse
    &page=2                              ← pages 2+

The site is Cloudflare-protected; this scraper uses cloudscraper.
Primary extraction: __NEXT_DATA__ JSON (Next.js SSR).
Fallback:           BeautifulSoup HTML card parsing.

⚠ The HTML CSS selectors and __NEXT_DATA__ key paths in this file are
  best-effort guesses. Run once, inspect the output/response, and tune
  SEL_* constants and _find_listings_in_next_data() candidates if needed.

Usage:
    python scrapers/properati_scraper.py

Output:
    output/properati_results_YYYY-MM-DD_HH-MM-SS.json
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import cloudscraper
from bs4 import BeautifulSoup

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── BASE CONFIGURATION ────────────────────────────────────────────────────────
BASE_URL = "https://www.properati.com.ar"

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "search_filters.json",
)

BACKOFF_BASE_SECONDS = 2
MAX_RETRIES          = 3

# Fetch each property detail page for description, coordinates, and full images.
FETCH_DETAIL_PAGES = True

# Properati's typical page size — used to estimate last page from total count.
# ⚠ Verify against live responses and adjust if actual results-per-page differs.
ITEMS_PER_PAGE = 20

SOURCE_NAME     = "properati"
OUTPUT_DIR      = "output"
OUTPUT_FILENAME = "properati_results_{timestamp}.json"

# ── PROPERTY TYPE MAPPING ─────────────────────────────────────────────────────
PROP_TYPE_MAP = {
    "departamento": "apartment",
    "casa":         "house",
}

# ── CSS SELECTORS — LIST PAGE (verified Feb 2026 against live HTML) ───────────
# Cards use <article data-test="normalListingRetis"> with data-url and data-idanuncio.
SEL_LISTING_CARD  = "article[data-test='normalListingRetis']"
SEL_CARD_TITLE    = "a[data-test='snippet__title']"
SEL_CARD_PRICE    = "div[data-test='snippet__price']"
SEL_CARD_LOCATION = "div[data-test='snippet__location']"
SEL_CARD_BEDROOMS = "span[data-test='bedrooms-value']"
SEL_CARD_BATHROOMS = "span[data-test='full-bathrooms-value']"
SEL_CARD_AGENCY   = "span[data-test='agency-name']"
SEL_TOTAL_RESULTS = "h1"

# ── CSS SELECTORS — DETAIL PAGE ───────────────────────────────────────────────
SEL_DETAIL_DESCRIPTION = [
    "[data-test='description-content']",
    "[data-test='description']",
    ".description-body",
    "section.description",
    "div.property-description p",
    "p.description",
]

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
                log.info(
                    "Retry %d/%d for %s (sleeping %.1fs)",
                    attempt, max_retries, url, sleep_time,
                )
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

def build_properati_url(config: dict, page: int = 1) -> str:
    """
    Translate the shared config into a Properati search URL.

    URL structure:
      https://www.properati.com.ar/s/venta
        ?geos=3697%2C3657%2C3652%2C3698   ← comma-separated geo IDs (URL-encoded)
        &bedrooms=3
        &amenities=car_park                ← when parking_spots_min >= 1
        &maxPrice=175000
        &minPrice=100000
        &propertyType=apartment%2Chouse
        &page=2                            ← pages 2+
    """
    loc           = config.get("location", {})
    price         = config.get("price", {})
    features      = config.get("features", {})
    prop          = config.get("property", {})
    properati_cfg = config.get("properati", {})

    # Geo IDs — match config neighborhood names to numeric codes
    geo_map       = properati_cfg.get("geo_ids", {})
    neighborhoods = loc.get("neighborhoods", [])
    geo_ids       = [str(geo_map[n]) for n in neighborhoods if n in geo_map]
    geos          = ",".join(geo_ids)

    # Property type
    prop_type     = prop.get("type", "departamento")
    en_type       = PROP_TYPE_MAP.get(prop_type, "apartment")
    property_type = f"{en_type},house" if en_type == "apartment" else en_type

    # Price
    price_min = int(price.get("min", 0))
    price_max = int(price.get("max", 0))

    # Bedrooms (minimum)
    bedrooms_list = features.get("bedrooms", [])
    min_bedrooms  = min(bedrooms_list) if bedrooms_list else int(features.get("dormitorios_min", 0))

    # Parking (cochera)
    parking_min = int(features.get("parking_spots_min", 0))

    params: dict = {}
    if geos:
        params["geos"] = geos
    if min_bedrooms > 0:
        params["bedrooms"] = min_bedrooms
    if parking_min >= 1:
        params["amenities"] = "car_park"
    if price_max > 0:
        params["maxPrice"] = price_max
    if price_min > 0:
        params["minPrice"] = price_min
    params["propertyType"] = property_type
    if page > 1:
        params["page"] = page

    return f"{BASE_URL}/s/venta?{urlencode(params)}"


# ── JSON EXTRACTION ───────────────────────────────────────────────────────────

def _extract_next_data(html: str) -> dict | None:
    """Extract the Next.js __NEXT_DATA__ JSON embedded in the page."""
    soup = BeautifulSoup(html, "lxml")
    tag  = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            pass

    # Regex fallback for cases where BS4 misses the tag
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


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    cleaned = str(val).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# ── JSON ITEM PARSER ──────────────────────────────────────────────────────────

def _parse_listing_from_next_data(item: dict) -> dict | None:
    """
    Parse one listing dict from Properati's __NEXT_DATA__ results array.

    ⚠ The exact field names depend on the live Next.js page structure.
      If this returns empty results, log the raw item and adjust the
      field names below to match what Properati actually sends.
    """
    prop_id = str(item.get("id") or item.get("propertyId") or item.get("listingId") or "")
    if not prop_id:
        return None

    title    = item.get("title") or item.get("name")
    url_path = item.get("url") or item.get("permalink") or item.get("link") or ""
    url      = url_path if url_path.startswith("http") else BASE_URL + url_path

    # Price — may be a nested dict or a bare number
    price_data = item.get("price") or {}
    if isinstance(price_data, dict):
        price_amount   = price_data.get("amount") or price_data.get("value")
        price_currency = (price_data.get("currency") or price_data.get("currency_id") or "").upper() or None
    else:
        price_amount   = price_data if isinstance(price_data, (int, float)) else None
        price_currency = (item.get("currency") or "USD").upper()

    price_usd: float | None = float(price_amount) if price_amount is not None else None

    # Location
    loc_data      = item.get("location") or item.get("address") or {}
    neighborhood  = (
        loc_data.get("neighborhood") or loc_data.get("neighbourhoodName")
        or loc_data.get("barrio") or None
    )
    street_address = loc_data.get("street") or loc_data.get("streetAddress") or None
    city           = loc_data.get("city") or loc_data.get("cityName") or "Buenos Aires"

    lat = loc_data.get("lat") or loc_data.get("latitude")
    lng = loc_data.get("lng") or loc_data.get("longitude")
    coordinates: dict | None = None
    if lat is not None and lng is not None:
        try:
            coordinates = {"latitude": float(lat), "longitude": float(lng)}
        except (TypeError, ValueError):
            pass

    # Property details — check nested dict first, then top-level keys
    details         = item.get("details") or item.get("propertyDetails") or {}
    rooms           = _safe_int(details.get("rooms") or item.get("rooms") or item.get("ambientes"))
    bedrooms        = _safe_int(details.get("bedrooms") or item.get("bedrooms") or item.get("dormitorios"))
    bathrooms       = _safe_int(details.get("bathrooms") or item.get("bathrooms") or item.get("banos"))
    surface_total   = _safe_float(details.get("totalSurface") or item.get("totalArea") or item.get("surface_total"))
    surface_covered = _safe_float(details.get("coveredSurface") or item.get("coveredArea") or item.get("surface_covered"))

    # Images
    images: list[str] = []
    for pic in (item.get("photos") or item.get("images") or item.get("pictures") or []):
        if isinstance(pic, str):
            images.append(pic)
        elif isinstance(pic, dict):
            src = pic.get("url") or pic.get("src") or pic.get("image")
            if src:
                images.append(src)

    return {
        "id":             prop_id,
        "title":          title,
        "price_usd":      price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood":   neighborhood,
            "street_address": street_address,
            "city":           city,
            "coordinates":    coordinates,
        },
        "property_details": {
            "rooms":              rooms,
            "bedrooms":           bedrooms,
            "bathrooms":          bathrooms,
            "surface_total_m2":   surface_total,
            "surface_covered_m2": surface_covered,
        },
        "description": item.get("description"),
        "images":      images,
        "url":         url,
        "source":      SOURCE_NAME,
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
        "features":    [],
    }


def _find_listings_in_next_data(next_data: dict) -> list[dict]:
    """
    Navigate __NEXT_DATA__ to the listings array.
    Tries several candidate paths — extend if none match after live testing.
    """
    candidates = [
        lambda d: d["props"]["pageProps"]["listings"],
        lambda d: d["props"]["pageProps"]["results"],
        lambda d: d["props"]["pageProps"]["searchResults"]["listings"],
        lambda d: d["props"]["pageProps"]["searchResults"]["results"],
        lambda d: d["props"]["pageProps"]["initialState"]["listings"],
        lambda d: d["props"]["pageProps"]["data"]["listings"],
        lambda d: d["props"]["pageProps"]["properties"],
    ]
    for fn in candidates:
        try:
            result = fn(next_data)
            if isinstance(result, list) and result:
                log.info("JSON extraction: found %d items in __NEXT_DATA__", len(result))
                return result
        except (KeyError, IndexError, TypeError):
            continue

    log.warning("Could not locate listings list in __NEXT_DATA__ — activating HTML fallback")
    return []


def _find_total_in_next_data(next_data: dict) -> int | None:
    """Try several known paths for total listing count in __NEXT_DATA__."""
    candidates = [
        lambda d: d["props"]["pageProps"]["total"],
        lambda d: d["props"]["pageProps"]["searchResults"]["total"],
        lambda d: d["props"]["pageProps"]["pagination"]["total"],
        lambda d: d["props"]["pageProps"]["data"]["total"],
        lambda d: d["props"]["pageProps"]["count"],
    ]
    for fn in candidates:
        try:
            val = fn(next_data)
            if isinstance(val, (int, float)):
                return int(val)
        except (KeyError, IndexError, TypeError):
            continue
    return None


# ── HTML FALLBACK PARSERS ─────────────────────────────────────────────────────

def _parse_card_html(card) -> dict | None:
    """
    Parse one <article data-test='normalListingRetis'> card.

    Key attributes (verified Feb 2026):
      data-url        → full property URL (/detalle/...)
      data-idanuncio  → UUID property ID
    Child selectors use data-test attributes.
    """
    # ID and URL live directly on the <article> element
    url     = card.get("data-url", "")
    prop_id = card.get("data-idanuncio") or None
    if not url:
        return None
    if not url.startswith("http"):
        url = BASE_URL + url

    # Title
    title_el = card.select_one(SEL_CARD_TITLE)
    title    = title_el.get_text(strip=True) or None if title_el else None

    # Price
    price_el = card.select_one(SEL_CARD_PRICE)
    price_usd, price_currency = None, None
    if price_el:
        price_text = price_el.get_text(strip=True)
        if any(s in price_text.upper() for s in ("USD", "U$S", "US$")):
            price_currency = "USD"
        elif "$" in price_text:
            price_currency = "ARS"
        numeric = re.sub(r"[^\d]", "", price_text)
        try:
            price_usd = float(numeric) if numeric else None
        except ValueError:
            pass

    # Location — card text is "Belgrano, Capital Federal"; split on first comma
    loc_el    = card.select_one(SEL_CARD_LOCATION)
    loc_text  = loc_el.get_text(strip=True) if loc_el else ""
    loc_parts = [p.strip() for p in loc_text.split(",", 1)]
    neighborhood = loc_parts[0] or None
    city_from_card = loc_parts[1] if len(loc_parts) > 1 else "Buenos Aires"

    # Bedrooms / bathrooms — each in its own dedicated element
    bed_el  = card.select_one(SEL_CARD_BEDROOMS)
    bath_el = card.select_one(SEL_CARD_BATHROOMS)
    bedrooms  = _safe_int(bed_el.get_text(strip=True))  if bed_el  else None
    bathrooms = _safe_int(bath_el.get_text(strip=True)) if bath_el else None

    # Images — <img src="https://img.properati.com/...">
    images: list[str] = []
    for img in card.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "img.properati.com" in src:
            images.append(src)

    return {
        "id":             prop_id,
        "title":          title,
        "price_usd":      price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood":   neighborhood,
            "street_address": None,
            "city":           city_from_card,
            "coordinates":    None,
        },
        "property_details": {
            "rooms":              None,   # not in list card; fetched from detail page
            "bedrooms":           bedrooms,
            "bathrooms":          bathrooms,
            "surface_total_m2":   None,
            "surface_covered_m2": None,
        },
        "description": None,
        "images":      images,
        "url":         url,
        "source":      SOURCE_NAME,
        "scraped_at":  datetime.now(timezone.utc).isoformat(),
        "features":    [],
    }


# ── PAGE PARSER ───────────────────────────────────────────────────────────────

def parse_listing_page(html: str, url: str) -> tuple[list[dict], int | None]:
    """
    Parse a Properati search results page.
    Tries __NEXT_DATA__ first; falls back to HTML card parsing.
    Returns (listings, total_count).
    """
    total: int | None = None

    # Primary: __NEXT_DATA__ JSON
    next_data = _extract_next_data(html)
    if next_data:
        total     = _find_total_in_next_data(next_data)
        raw_items = _find_listings_in_next_data(next_data)
        if raw_items:
            listings = []
            for item in raw_items:
                try:
                    parsed = _parse_listing_from_next_data(item)
                    if parsed:
                        listings.append(parsed)
                except Exception as exc:
                    log.warning("Failed to parse JSON item: %s", exc)
            return listings, total

    # Fallback: HTML card parsing
    soup = BeautifulSoup(html, "lxml")

    if total is None:
        for sel in SEL_TOTAL_RESULTS.split(", "):
            el = soup.select_one(sel.strip())
            if el:
                m = re.search(r"(\d[\d.]*)", el.get_text())
                if m:
                    total = int(m.group(1).replace(".", ""))
                    break

    cards = soup.select(SEL_LISTING_CARD)
    log.info("HTML fallback: found %d card elements (url: %s)", len(cards), url)

    listings = []
    for card in cards:
        try:
            parsed = _parse_card_html(card)
            if parsed:
                listings.append(parsed)
        except Exception as exc:
            log.warning("Failed to parse HTML card: %s", exc)

    return listings, total


# ── DETAIL PAGE ENRICHMENT ────────────────────────────────────────────────────

def fetch_detail_page(
    scraper: cloudscraper.CloudScraper,
    listing: dict,
    delay_range: list[float],
) -> dict:
    """
    Fetch the property detail page to enrich description, coordinates, and images.
    Tries __NEXT_DATA__ first, then Schema.org JSON-LD, then HTML selectors.
    Modifies listing in-place; always returns it (even on failure).
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

    # ── 1. Breadcrumb → neighborhood + city ──────────────────────────────────
    # Breadcrumb text: "Venta | Casas | Capital Federal | Belgrano"
    # Last segment = neighborhood, second-to-last = city.
    bc_el = soup.find(attrs={"data-test": "breadcrumb"})
    if bc_el:
        bc_parts = [p.strip() for p in bc_el.get_text(separator="|", strip=True).split("|") if p.strip()]
        if len(bc_parts) >= 2:
            if not listing["location"].get("neighborhood"):
                listing["location"]["neighborhood"] = bc_parts[-1]
            if not listing["location"].get("city") or listing["location"]["city"] == "Buenos Aires":
                listing["location"]["city"] = bc_parts[-2]

    # ── 3. Bedrooms / bathrooms / area from first non-empty data-test elements ─
    details = listing["property_details"]

    def _first_nonempty(dt_value: str) -> str | None:
        for el in soup.find_all(attrs={"data-test": dt_value}):
            txt = el.get_text(strip=True)
            if txt:
                return txt
        return None

    if details["bedrooms"] is None:
        txt = _first_nonempty("bedrooms-value")
        if txt:
            m = re.search(r"(\d+)", txt)
            if m:
                details["bedrooms"] = int(m.group(1))

    if details["bathrooms"] is None:
        txt = _first_nonempty("full-bathrooms-value")
        if txt:
            m = re.search(r"(\d+)", txt)
            if m:
                details["bathrooms"] = int(m.group(1))

    if details["surface_total_m2"] is None:
        txt = _first_nonempty("area-value")
        if txt:
            m = re.search(r"([\d.,]+)", txt)
            if m:
                details["surface_total_m2"] = _safe_float(m.group(1))

    return listing


# ── FILTER AND DEDUP ──────────────────────────────────────────────────────────

def filter_listing(listing: dict, config: dict) -> bool:
    """Client-side verification: price/currency, minimum bedrooms, minimum surface."""
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

    features        = config.get("features", {})
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
    """Remove duplicates by property ID. First occurrence wins."""
    seen:   set[str]  = set()
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
    last_page:     int        = max_pages

    for page in range(1, max_pages + 1):
        url = build_properati_url(config, page)
        log.info("── Page %d / max %d ──────────────────────────", page, max_pages)
        log.info("URL: %s", url)

        resp = fetch_with_retry(scraper, url)
        if resp is None:
            log.error("Failed to fetch page %d — stopping pagination", page)
            break

        page_listings, page_total = parse_listing_page(resp.text, url)

        if page == 1 and page_total is not None:
            total_results = page_total
            pages_needed  = (total_results + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            last_page     = min(pages_needed, max_pages)
            log.info(
                "Site reports %d total results | Pages to scrape: %d (cap: %d)",
                total_results, last_page, max_pages,
            )

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

    preview_url = build_properati_url(config, page=1)
    log.info("=== Properati Scraper starting ===")
    log.info("Config: %s", CONFIG_FILE)
    log.info("Search URL (page 1): %s", preview_url)
    log.info(
        "Price: %s %s–%s | Neighborhoods: %s | Max pages: %d",
        currency, price_min, price_max,
        config.get("location", {}).get("neighborhoods", []),
        max_pages,
    )

    scraper = make_scraper()

    # Phase 1: scrape all pages
    raw_listings, total_results = scrape_all_pages(scraper, config)
    log.info("Raw listings collected: %d", len(raw_listings))

    # Phase 2: filter
    filtered = [l for l in raw_listings if filter_listing(l, config)]
    skipped  = len(raw_listings) - len(filtered)
    if skipped:
        log.info("Filtered out %d listing(s) outside %s %s–%s", skipped, currency, price_min, price_max)
    log.info("After price filter: %d listings", len(filtered))

    # Phase 3: deduplicate
    unique = deduplicate(filtered)
    log.info("After deduplication: %d unique listings", len(unique))

    # Phase 4: enrich from detail pages
    if FETCH_DETAIL_PAGES:
        log.info("Fetching detail pages for %d listings...", len(unique))
        for i, listing in enumerate(unique, 1):
            log.info("Detail page %d/%d: %s", i, len(unique), listing.get("url", ""))
            unique[i - 1] = fetch_detail_page(scraper, listing, delay_range)

    # Phase 5: save
    output   = build_output(unique, total_results, config, search_url=preview_url)
    filepath = save_output(output)
    log.info("=== Done. %d listings saved to %s ===", len(unique), filepath)


if __name__ == "__main__":
    main()
