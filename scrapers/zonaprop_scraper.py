"""
ZonaProp Scraper
================
Scrapes departamentos (apartments) for sale in Belgrano, Núñez, Saavedra,
and Villa Urquiza (Buenos Aires) within a USD price range and outputs a
structured JSON file compatible with the argenprop_results format.

ZonaProp is protected by Cloudflare, so this scraper uses the `cloudscraper`
library instead of raw `requests`.

Usage:
    pip install -r requirements.txt
    python scrapers/zonaprop_scraper.py

Output:
    output/zonaprop_results_YYYY-MM-DD_HH-MM-SS.json
"""

import base64
import json
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin

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
BASE_URL = "https://www.zonaprop.com.ar"

# Path to the shared, format-agnostic search filters file.
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "search_filters.json",
)

BACKOFF_BASE_SECONDS = 2
MAX_RETRIES = 3

# Set to True to fetch each individual property page for full description
# and amenities list. Significantly increases runtime (1-2s delay per listing).
FETCH_DETAIL_PAGES = False

# Fetch each property's detail page to extract coordinates (lat/lng).
# ZonaProp encodes them as Base64 strings in inline JS: const mapLatOf = "..."
# Adds ~1-2s per listing. Set to False to skip coordinate enrichment.
FETCH_COORDINATES = True

# ── CSS SELECTORS ─────────────────────────────────────────────────────────────
# All parsing targets defined here — update these when ZonaProp changes their HTML.
#
# Key ZonaProp quirks (verified against live HTML 2026-02):
#   • Listing cards use data-qa="posting PROPERTY" and carry data-id / data-to-posting.
#   • The clickable URL is in data-to-posting, NOT in a child <a href>.
#   • Features are a single concatenated text node inside POSTING_CARD_FEATURES
#     e.g. "65 m² tot.3 amb.2 dorm.1 baño1 coch."
#   • Images sit inside POSTING_CARD_GALLERY as plain <img src="...">.
SEL_LISTING_ITEM  = "[data-qa='posting PROPERTY']"
SEL_PRICE         = "[data-qa='POSTING_CARD_PRICE']"
SEL_EXPENSES      = "[data-qa='expensas']"
SEL_FEATURES      = "[data-qa='POSTING_CARD_FEATURES']"
SEL_LOCATION      = "[data-qa='POSTING_CARD_LOCATION']"
SEL_DESCRIPTION   = "[data-qa='POSTING_CARD_DESCRIPTION']"
SEL_GALLERY       = "[data-qa='POSTING_CARD_GALLERY']"
SEL_PAGINATION    = "a[href*='pagina-']"
SEL_TOTAL_RESULTS = "h1"

# Feature text patterns — ZonaProp concatenates all features into a single element.
# Examples: "65 m² tot.3 amb.2 dorm.1 baño1 coch."
PAT_ROOMS           = re.compile(r"(\d+)\s*amb", re.IGNORECASE)
PAT_BEDROOMS        = re.compile(r"(\d+)\s*dorm", re.IGNORECASE)
PAT_BATHROOMS       = re.compile(r"(\d+)\s*ba[ñn]", re.IGNORECASE)
PAT_SURFACE_TOTAL   = re.compile(r"([\d.,]+)\s*m[²2]\s*tot", re.IGNORECASE)
PAT_SURFACE_COVERED = re.compile(r"([\d.,]+)\s*m[²2]\s*(?:cub|cubiertos?)?", re.IGNORECASE)

# Detail page selectors (used only if FETCH_DETAIL_PAGES = True)
SEL_DETAIL_DESCRIPTION = [
    "[data-qa='POSTING_DESCRIPTION']",
    "div.description-text",
    "section.description p",
]
SEL_DETAIL_FEATURES = [
    "[data-qa='POSTING_FEATURES'] li",
    "ul.property-features li",
    "div.amenities li",
]

# ── OUTPUT ────────────────────────────────────────────────────────────────────
OUTPUT_DIR      = "output"
OUTPUT_FILENAME = "zonaprop_results_{timestamp}.json"

# ── HTTP HEADERS ──────────────────────────────────────────────────────────────
# cloudscraper handles Cloudflare challenges; these headers add extra realism.
HEADERS = {
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Cache-Control": "max-age=0",
}


# ── HTTP LAYER ────────────────────────────────────────────────────────────────

def make_scraper() -> cloudscraper.CloudScraper:
    """
    Create a cloudscraper session (Cloudflare-aware) with browser-like headers.
    Warms up with a homepage hit so CF cookies are set before search requests.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    scraper.headers.update(HEADERS)
    try:
        resp = scraper.get(BASE_URL, timeout=20)
        log.info("Session warmed up — homepage status: %d", resp.status_code)
    except Exception as exc:
        log.warning("Homepage warm-up failed (will still attempt scraping): %s", exc)
    return scraper


def fetch_with_retry(
    scraper: cloudscraper.CloudScraper,
    url: str,
    max_retries: int = MAX_RETRIES,
    backoff_base: float = BACKOFF_BASE_SECONDS,
) -> cloudscraper.CloudScraper | None:
    """
    GET with exponential-backoff retry.
    Returns the Response on 200, None if all attempts fail.
    """
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
    """Load the shared search_filters.json file."""
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ── URL BUILDER ───────────────────────────────────────────────────────────────

def normalize_neighborhood_slug(name: str) -> str:
    """
    Convert a neighborhood display name to a ZonaProp URL slug.

    Examples:
      "Núñez"         → "nunez"
      "Villa Urquiza" → "villa-urquiza"
      "Belgrano R"    → "belgrano-r"
    """
    normalized = unicodedata.normalize("NFD", name.lower())
    ascii_only = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    slugified = re.sub(r"[^a-z0-9-]+", "-", ascii_only.strip())
    return slugified.strip("-")


def build_zonaprop_url(config: dict, page: int = 1) -> str:
    """
    Translate the format-agnostic search_filters.json into a ZonaProp search URL.

    ZonaProp URL structure (one continuous hyphen-slug ending in .html):
      /{type}s-venta-{neighborhoods}-{bedrooms}-{parking}-{price}.html
      /{type}s-venta-{neighborhoods}-{bedrooms}-{parking}-{price}-pagina-{N}.html

    ZonaProp URL translation rules:
      neighborhoods: ["Belgrano", "Núñez"]            → "belgrano-nunez"  (no -o-)
      bedrooms: [2, 3]                                → "desde-2-hasta-3-habitaciones"
      parking_spots_min: 1                            → "mas-de-1-garage"
      price: {min:150000, max:180000, currency:"USD"} → "150000-180000-dolar"
    """
    loc      = config.get("location", {})
    price    = config.get("price", {})
    features = config.get("features", {})
    prop     = config.get("property", {})

    prop_slug = prop.get("type", "departamento") + "s"  # "departamentos"

    neighborhoods_slug = "-".join(
        normalize_neighborhood_slug(n) for n in loc.get("neighborhoods", [])
    )

    segments = [f"{prop_slug}-venta", neighborhoods_slug]

    bedrooms = sorted(features.get("bedrooms", []))
    if bedrooms:
        segments.append(f"desde-{min(bedrooms)}-hasta-{max(bedrooms)}-habitaciones")

    parking_min = int(features.get("parking_spots_min", 0))
    if parking_min > 0:
        segments.append(f"mas-de-{parking_min}-garage")

    currency  = price.get("currency", "USD").lower()
    price_min = int(price.get("min", 0))
    price_max = int(price.get("max", 0))
    if currency == "usd":
        if price_min > 0 and price_max > 0:
            segments.append(f"{price_min}-{price_max}-dolar")
        elif price_max > 0:
            segments.append(f"menos-{price_max}-dolar")
        elif price_min > 0:
            segments.append(f"mas-de-{price_min}-dolar")

    path = "-".join(segments)
    if page > 1:
        path += f"-pagina-{page}"

    return f"{BASE_URL}/{path}.html"


# ── PAGINATION HELPERS ────────────────────────────────────────────────────────

def parse_total_results(soup: BeautifulSoup) -> int | None:
    """
    Extract the total result count from the page <h1>.
    Target: <h1>68 Departamentos apto crédito y balcón ... en venta ...</h1>
    """
    el = soup.select_one(SEL_TOTAL_RESULTS)
    if el:
        text = el.get_text(strip=True)
        m = re.search(r"(\d[\d.]*)", text)
        if m:
            return int(m.group(1).replace(".", ""))
    return None


def get_last_page(soup: BeautifulSoup, max_pages: int = 10) -> int:
    """
    Scan pagination links for pagina-N patterns.
    Returns the highest page number found, capped at max_pages.
    Returns 1 if no pagination links are found.
    """
    page_numbers = []
    for link in soup.select(SEL_PAGINATION):
        href = link.get("href", "")
        m = re.search(r"pagina-(\d+)", href)
        if m:
            page_numbers.append(int(m.group(1)))

    return min(max(page_numbers), max_pages) if page_numbers else 1


# ── PARSING HELPERS ───────────────────────────────────────────────────────────

def parse_price(price_text: str | None) -> tuple[float | None, str | None]:
    """
    Parse an Argentine-format price string into (amount, currency).

    ZonaProp format examples:
      "USD 160.000"   → (160000.0, "USD")
      "$ 1.500.000"   → (1500000.0, "ARS")
      "Consultar"     → (None, None)
    """
    if not price_text:
        return None, None

    text = price_text.strip()

    if re.search(r"USD|u\$s|u\$d", text, re.IGNORECASE):
        currency = "USD"
    elif "$" in text:
        currency = "ARS"
    else:
        return None, None

    numeric_str = re.sub(r"[^\d.,]", "", text).strip()
    if not numeric_str:
        return None, currency

    # Argentine format: "." = thousands separator, "," = decimal separator
    if "," in numeric_str:
        numeric_str = numeric_str.replace(".", "").replace(",", ".")
    else:
        numeric_str = numeric_str.replace(".", "")

    try:
        return float(numeric_str), currency
    except ValueError:
        log.warning("Could not convert price to float: %r", numeric_str)
        return None, currency


def _parse_float(raw: str) -> float | None:
    """Parse an Argentine numeric string (dots as thousands, comma as decimal)."""
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_features(features_text: str) -> dict:
    """
    Parse ZonaProp's concatenated features string into structured fields.

    Example input: "65 m² tot.3 amb.2 dorm.1 baño1 coch."
    Returns dict with keys: rooms, bedrooms, bathrooms,
                            surface_total_m2, surface_covered_m2.
    """
    result: dict = {
        "rooms":              None,
        "bedrooms":           None,
        "bathrooms":          None,
        "surface_total_m2":   None,
        "surface_covered_m2": None,
    }

    t = features_text

    m = PAT_ROOMS.search(t)
    if m:
        result["rooms"] = int(m.group(1))

    m = PAT_BEDROOMS.search(t)
    if m:
        result["bedrooms"] = int(m.group(1))

    m = PAT_BATHROOMS.search(t)
    if m:
        result["bathrooms"] = int(m.group(1))

    # Surface: check total qualifier first, then fall back to covered
    m_tot = PAT_SURFACE_TOTAL.search(t)
    if m_tot:
        result["surface_total_m2"] = _parse_float(m_tot.group(1))
    else:
        m_cov = PAT_SURFACE_COVERED.search(t)
        if m_cov:
            result["surface_covered_m2"] = _parse_float(m_cov.group(1))

    return result


def parse_photos(item: Tag) -> list[str]:
    """
    Extract property image URLs from the gallery element.
    Skips placeholder / logo images (non-aviso CDN paths).
    """
    gallery = item.select_one(SEL_GALLERY)
    if not gallery:
        return []

    seen: set[str] = set()
    images: list[str] = []

    for img in gallery.select("img"):
        url = img.get("src", "")
        # Only keep main listing photos (ZonaProp uses imgar.zonapropcdn.com)
        if url.startswith("http") and "imgar.zonapropcdn.com/avisos" in url:
            if url not in seen:
                seen.add(url)
                images.append(url)

    return images


# ── CARD PARSING ──────────────────────────────────────────────────────────────

def parse_single_card(item: Tag) -> dict | None:
    """
    Parse one [data-qa='posting PROPERTY'] element into a normalised dict.
    Returns None if the card has no usable URL.
    All fields default to None if not found — never raises.
    """
    # ── URL and property ID ──────────────────────────────────────────────────
    # ZonaProp stores the property URL in data-to-posting, not in an <a href>.
    relative_url = item.get("data-to-posting", "")
    if not relative_url:
        return None

    # Strip tracking query params for a clean URL
    relative_url = relative_url.split("?")[0]
    full_url = BASE_URL + relative_url

    # Property ID is directly on the card element
    property_id = str(item.get("data-id") or "") or None
    # Fallback: numeric suffix before .html
    if not property_id:
        m = re.search(r"-(\d+)\.html$", relative_url)
        if m:
            property_id = m.group(1)

    # ── Price ────────────────────────────────────────────────────────────────
    price_tag  = item.select_one(SEL_PRICE)
    price_text = price_tag.get_text(strip=True) if price_tag else None
    price_usd, price_currency = parse_price(price_text)

    # ── Location ─────────────────────────────────────────────────────────────
    # Format: "Villa Urquiza, Capital Federal"
    location_tag  = item.select_one(SEL_LOCATION)
    location_text = location_tag.get_text(strip=True) if location_tag else None

    neighborhood: str | None = None
    city: str | None = None
    if location_text:
        parts = [p.strip() for p in location_text.split(",", 1)]
        neighborhood = parts[0] or None
        city = parts[1] if len(parts) > 1 else "Buenos Aires"

    # ── Features ─────────────────────────────────────────────────────────────
    features_tag  = item.select_one(SEL_FEATURES)
    features_text = features_tag.get_text(strip=True) if features_tag else ""
    property_details = parse_features(features_text)

    # Fallback: rooms from URL slug
    if property_details["rooms"] is None:
        m = re.search(r"-(\d+)-ambientes?", relative_url, re.IGNORECASE)
        if m:
            property_details["rooms"] = int(m.group(1))

    # ── Description ──────────────────────────────────────────────────────────
    desc_tag = item.select_one(SEL_DESCRIPTION)
    description = desc_tag.get_text(separator=" ", strip=True) if desc_tag else None

    # ── Photos ───────────────────────────────────────────────────────────────
    images = parse_photos(item)

    # ── Title: use location as the human-readable title (ZonaProp card pattern) ──
    title = location_text

    return {
        "id": property_id,
        "title": title,
        "price_usd": price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood": neighborhood,
            "street_address": None,   # not exposed on ZonaProp listing cards
            "city": city or "Buenos Aires",
            "coordinates": None,   # populated by fetch_detail_page when FETCH_COORDINATES = True
        },
        "property_details": property_details,
        "description": description,
        "images": images,
        "url": full_url,
        "source": "zonaprop",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "features": [],
    }


def parse_listing_cards(soup: BeautifulSoup) -> list[dict]:
    """
    Find all listing card elements on a page and parse each one.
    Errors in individual cards are caught and logged — they never abort the page.
    """
    items = soup.select(SEL_LISTING_ITEM)
    log.info("Found %d listing items on page", len(items))

    results = []
    for item in items:
        try:
            card = parse_single_card(item)
            if card:
                results.append(card)
        except Exception as exc:
            log.warning("Failed to parse card: %s", exc)

    return results


# ── DETAIL PAGE ENRICHMENT (optional) ────────────────────────────────────────

def fetch_detail_page(
    scraper: cloudscraper.CloudScraper,
    listing: dict,
    delay_range: list[float],
) -> dict:
    """
    Fetch the individual property page to enrich description and features.
    Only called when FETCH_DETAIL_PAGES is True.
    Modifies the listing dict in-place; always returns it (even on failure).
    """
    url = listing.get("url")
    if not url:
        return listing

    time.sleep(random.uniform(delay_range[0], delay_range[1]))
    resp = fetch_with_retry(scraper, url)
    if resp is None:
        return listing

    soup = BeautifulSoup(resp.text, "lxml")

    if not listing.get("description"):
        for sel in SEL_DETAIL_DESCRIPTION:
            el = soup.select_one(sel)
            if el:
                listing["description"] = el.get_text(separator=" ", strip=True)
                break

    if not listing.get("features"):
        for sel in SEL_DETAIL_FEATURES:
            els = soup.select(sel)
            if els:
                listing["features"] = [
                    el.get_text(strip=True) for el in els if el.get_text(strip=True)
                ]
                break

    # Coordinates — ZonaProp encodes lat/lng as Base64 strings in inline JS variables:
    #   const mapLatOf = "LTM0LjU3...";  →  base64-decode  →  "-34.570999..."
    #   const mapLngOf = "LTU4LjUw...";  →  base64-decode  →  "-58.505000..."
    lat_m = re.search(r'const mapLatOf\s*=\s*"([^"]+)"', resp.text)
    lng_m = re.search(r'const mapLngOf\s*=\s*"([^"]+)"', resp.text)
    if lat_m and lng_m:
        try:
            listing["location"]["coordinates"] = {
                "latitude":  float(base64.b64decode(lat_m.group(1)).decode()),
                "longitude": float(base64.b64decode(lng_m.group(1)).decode()),
            }
        except Exception as exc:
            log.debug("Could not decode ZonaProp coordinates: %s", exc)

    return listing


# ── FILTERS AND DEDUPLICATION ─────────────────────────────────────────────────

def filter_listing(listing: dict, config: dict) -> bool:
    """
    Keep only listings matching the configured currency and price range.
    Client-side verification catches edge cases from ZonaProp's re-indexing.
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
    return price_min <= price <= price_max


def deduplicate(listings: list[dict]) -> list[dict]:
    """Remove duplicate listings by property ID. First occurrence wins."""
    seen_ids: set[str] = set()
    unique: list[dict] = []

    for listing in listings:
        prop_id = listing.get("id")
        if prop_id is None:
            log.warning("Listing without ID — keeping: %s", listing.get("url"))
            unique.append(listing)
        elif prop_id not in seen_ids:
            seen_ids.add(prop_id)
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
    scraping_cfg = config.get("scraping", {})
    max_pages    = int(scraping_cfg.get("max_pages", 10))
    delay_range  = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    all_listings: list[dict] = []
    total_results: int | None = None
    last_page: int = 1

    for page in range(1, max_pages + 1):
        url = build_zonaprop_url(config, page)
        log.info("── Page %d / max %d ──────────────────────────", page, max_pages)
        log.info("URL: %s", url)

        resp = fetch_with_retry(scraper, url)
        if resp is None:
            log.error("Failed to fetch page %d — stopping pagination", page)
            break

        soup = BeautifulSoup(resp.text, "lxml")

        if page == 1:
            total_results = parse_total_results(soup)
            last_page     = get_last_page(soup, max_pages)
            log.info(
                "Site reports %s total results | Pages to scrape: %d (hard cap: %d)",
                total_results, last_page, max_pages,
            )

        page_listings = parse_listing_cards(soup)
        log.info("Page %d: parsed %d cards", page, len(page_listings))

        if not page_listings:
            log.info("No listings found on page %d — stopping early", page)
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
            "total_results": total_results,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "search_criteria": search_criteria,
            "search_url": search_url,
            "listings_count": len(listings),
        },
        "listings": listings,
    }


def save_output(data: dict) -> str:
    """
    Write the output JSON to output/zonaprop_results_YYYY-MM-DD_HH-MM-SS.json.
    Creates the output directory if it does not exist.
    Returns the path of the written file.
    """
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
    delay_range  = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    price_min  = price_cfg.get("min", 0)
    price_max  = price_cfg.get("max", 0)
    currency   = price_cfg.get("currency", "USD")
    max_pages  = scraping_cfg.get("max_pages", 10)

    preview_url = build_zonaprop_url(config, page=1)
    log.info("=== ZonaProp Scraper starting ===")
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

    # Phase 2: filter by price/currency before touching detail pages
    filtered = [l for l in raw_listings if filter_listing(l, config)]
    skipped  = len(raw_listings) - len(filtered)
    if skipped:
        log.info(
            "Filtered out %d listing(s) outside %s %s–%s range",
            skipped, currency, price_min, price_max,
        )
    log.info("After price filter: %d listings", len(filtered))

    # Phase 3: deduplicate before touching detail pages
    unique = deduplicate(filtered)
    log.info("After deduplication: %d unique listings", len(unique))

    # Phase 4: fetch detail pages only for listings that survived filtering
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
