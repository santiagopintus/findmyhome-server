"""
ArgenpProp Scraper
==================
Scrapes departamentos (apartments) for sale in Núñez and Belgrano (Buenos Aires)
within a USD price range and outputs a structured JSON file.

Usage:
    pip install -r requirements.txt
    python scrapers/argenprop_scraper.py

Output:
    output/argenprop_results_YYYY-MM-DD_HH-MM-SS.json
"""

import json
import logging
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── BASE CONFIGURATION ────────────────────────────────────────────────────────
BASE_URL = "https://www.argenprop.com"

# Path to the shared, format-agnostic search filters file.
# Resolved relative to this file so the scraper works from any working directory.
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "search_filters.json",
)

BACKOFF_BASE_SECONDS = 2   # sleep = BACKOFF_BASE ** attempt (internal, not in config)
MAX_RETRIES = 3            # default; overridden by config.scraping.max_retries

# Set to True to fetch each individual property page for full description
# and amenities list. Significantly increases runtime (1-2s delay per listing).
FETCH_DETAIL_PAGES = False

# Fetch each property's detail page to extract coordinates (lat/lng).
# ArgenProp embeds them as data-latitude / data-longitude on the Leaflet container.
# Adds ~1-2s per listing. Set to False to skip coordinate enrichment.
FETCH_COORDINATES = True

# ── CSS SELECTORS ─────────────────────────────────────────────────────────────
# All parsing targets defined here — update these when ArgenProp changes their HTML.
SEL_LISTING_ITEM   = "div.listing__item, article.listing__item"
SEL_CARD_LINK      = "a[href]"
SEL_PRICE          = "div.card__price, p.card__price, span.card__price"
SEL_STREET_ADDRESS = "p.card__title--primary, h3.card__title--primary"
SEL_ADDRESS        = "p.card__address, span.card__address"
SEL_MAIN_FEATURES  = "ul.card__main-features li"
SEL_DESCRIPTION    = "p.card__info, div.card__info, p.card__description"
SEL_PHOTOS_BOX     = "div.card__photos-box"
SEL_PAGINATION     = "div.pagination a, li.pagination__page a"
SEL_TOTAL_RESULTS  = "h1, h2.listing-top__title"

# Icon class fragments used to identify feature types in the features list
ICON_BEDROOMS  = "icon-bed"
ICON_BATHROOMS = "icon-bath"
ICON_SURFACE   = "icon-square_meter"
ICON_ROOMS     = "icon-ambientes"

# Detail page selectors (used only if FETCH_DETAIL_PAGES = True)
SEL_DETAIL_DESCRIPTION = [
    "div.description-text",
    "div.property-description",
    "section.description p",
    "div.posting-description",
]
SEL_DETAIL_FEATURES = [
    "ul.property-features li",
    "div.amenities li",
    "ul.features li",
    "ul.property-amenities li",
]

# ── OUTPUT ────────────────────────────────────────────────────────────────────
OUTPUT_DIR      = "output"
OUTPUT_FILENAME = "argenprop_results_{timestamp}.json"

# ── HTTP HEADERS ──────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# ── HTTP LAYER ────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    """
    Create a requests.Session with browser-like headers.
    Warms up by hitting the homepage so cookies are set before search requests.
    """
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

            resp = session.get(url, timeout=20)

            if resp.status_code == 200:
                return resp

            elif resp.status_code == 403:
                log.warning("403 Forbidden for %s (attempt %d/%d)", url, attempt + 1, max_retries + 1)
                # Longer cooldown for bot-detection triggers
                time.sleep(backoff_base ** (attempt + 2))

            elif resp.status_code == 404:
                log.info("404 Not Found: %s — skipping", url)
                return None  # No point retrying

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

def normalize_neighborhood_slug(name: str) -> str:
    """
    Convert a neighborhood display name to an ArgenProp URL slug.

    Examples:
      "Núñez"       → "nunez"
      "Villa Urquiza" → "villa-urquiza"
      "Belgrano R"  → "belgrano-r"
    """
    # Remove accents via Unicode decomposition
    normalized = unicodedata.normalize("NFD", name.lower())
    ascii_only = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    # Spaces → hyphens; strip anything that isn't a letter, digit, or hyphen
    slugified = re.sub(r"[^a-z0-9-]+", "-", ascii_only.strip())
    return slugified.strip("-")


def build_argenprop_url(config: dict, page: int = 1) -> str:
    """
    Translate the format-agnostic search_filters.json into an ArgenProp search URL.

    URL structure:
      /{property_type}s/venta/{neighborhoods}/{bedrooms}/{price}?{parking}&pagina-{page}

    Example output (page 1):
      /departamentos/venta/belgrano-o-nunez-o-saavedra-o-villa-urquiza/
        2-dormitorios-o-3-dormitorios/dolares-150000-180000?1-o-mas-cocheras

    Example output (page 2, no parking filter):
      /departamentos/venta/belgrano-o-nunez/dolares-150000-180000?pagina-2
    """
    loc      = config.get("location", {})
    price    = config.get("price", {})
    features = config.get("features", {})
    prop     = config.get("property", {})

    # Neighborhoods: ["Belgrano", "Núñez"] → "belgrano-o-nunez"
    neighborhoods = loc.get("neighborhoods", [])
    neighborhoods_slug = "-o-".join(
        normalize_neighborhood_slug(n) for n in neighborhoods
    )

    # Property type slug: "departamento" → "departamentos"
    prop_slug = prop.get("type", "departamento") + "s"

    # Path segments (order matters for ArgenProp)
    path_segments = [prop_slug, "venta", neighborhoods_slug]

    # Bedrooms: [2, 3] → "2-dormitorios-o-3-dormitorios"
    bedrooms = features.get("bedrooms", [])
    if bedrooms:
        path_segments.append(
            "-o-".join(f"{b}-dormitorios" for b in sorted(bedrooms))
        )

    # Price: currency "USD" → "dolares-150000-180000"
    currency  = price.get("currency", "USD").lower()
    price_min = int(price.get("min", 0))
    price_max = int(price.get("max", 0))
    if currency == "usd":
        path_segments.append(f"dolares-{price_min}-{price_max}")

    url = BASE_URL + "/" + "/".join(path_segments)

    # Query params — ArgenProp uses bare params (no key=value), combined with &
    query_parts: list[str] = []

    parking_min = int(features.get("parking_spots_min", 0))
    if parking_min > 0:
        query_parts.append(f"{parking_min}-o-mas-cocheras")

    if page > 1:
        query_parts.append(f"pagina-{page}")

    if query_parts:
        url += "?" + "&".join(query_parts)

    return url


# ── PAGINATION HELPERS ────────────────────────────────────────────────────────

def parse_total_results(soup: BeautifulSoup) -> int | None:
    """
    Extract the total result count from the page heading.
    Targets: <h1>446 Departamentos en Venta en Belgrano, Nuñez</h1>
    """
    for sel in SEL_TOTAL_RESULTS.split(","):
        el = soup.select_one(sel.strip())
        if el:
            text = el.get_text(strip=True)
            match = re.match(r"^([\d.]+)", text)
            if match:
                return int(match.group(1).replace(".", ""))
    return None


def get_last_page(soup: BeautifulSoup, max_pages: int = 10) -> int:
    """
    Scan all pagination links for ?pagina-N patterns.
    Returns the highest page number found, capped at max_pages.
    Returns 1 if no pagination links are found.
    """
    page_numbers = []
    for link in soup.select(SEL_PAGINATION):
        href = link.get("href", "")
        match = re.search(r"pagina-(\d+)", href)
        if match:
            page_numbers.append(int(match.group(1)))

    if not page_numbers:
        return 1

    return min(max(page_numbers), max_pages)


# ── PARSING HELPERS ───────────────────────────────────────────────────────────

def extract_property_id(url_path: str) -> str | None:
    """
    Extract the numeric property ID from a URL slug.

    Input:  "/departamento-en-venta-en-belgrano-2-ambientes--15913361"
    Output: "15913361"

    The ID is always the segment after the last "--" at the end of the path.
    """
    # Strip query string, fragment, trailing slash
    clean = url_path.split("?")[0].split("#")[0].rstrip("/")
    match = re.search(r"--(\d+)$", clean)
    if match:
        return match.group(1)
    log.warning("Could not extract property ID from: %s", url_path)
    return None


def parse_price(price_text: str | None) -> tuple[float | None, str | None]:
    """
    Parse an Argentine-format price string into (amount, currency).

    Argentine format uses "." as thousands separator and "," as decimal separator.
      "USD 159.999"    → (159999.0, "USD")
      "USD 155.000"    → (155000.0, "USD")
      "$ 155.000.000"  → (155000000.0, "ARS")
      "USD 1.500,50"   → (1500.5,   "USD")
      "Consultar"      → (None, None)
    """
    if not price_text:
        return None, None

    text = price_text.strip()

    # Detect currency
    if re.search(r"USD", text, re.IGNORECASE):
        currency = "USD"
    elif "$" in text:
        currency = "ARS"
    else:
        return None, None  # "Consultar precio" or similar

    # Extract numeric characters only (digits, dot, comma)
    numeric_str = re.sub(r"[^\d.,]", "", text).strip()
    if not numeric_str:
        return None, currency

    # Argentine format disambiguation:
    # If there's a comma → it is the decimal separator ("1.500,50" → 1500.50)
    # If there's no comma → all dots are thousands separators ("159.999" → 159999)
    if "," in numeric_str:
        numeric_str = numeric_str.replace(".", "").replace(",", ".")
    else:
        numeric_str = numeric_str.replace(".", "")

    try:
        return float(numeric_str), currency
    except ValueError:
        log.warning("Could not convert price to float: %r", numeric_str)
        return None, currency


def parse_location(address_text: str | None) -> tuple[str | None, str | None]:
    """
    Split "Belgrano, Capital Federal" into ("Belgrano", "Capital Federal").
    Returns (neighborhood, city).
    """
    if not address_text:
        return None, None
    parts = [p.strip() for p in address_text.split(",", 1)]
    neighborhood = parts[0] if parts else None
    city = parts[1] if len(parts) > 1 else "Buenos Aires"
    return neighborhood, city


def extract_leading_int(text: str) -> int | None:
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def extract_leading_float(text: str) -> float | None:
    match = re.search(r"([\d.,]+)", text)
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _apply_text_fallbacks(text: str, result: dict) -> None:
    """
    Fill gaps in the features dict by matching text patterns.
    Called for every feature list item regardless of icon class.
    """
    t = text.lower()

    if result["bedrooms"] is None and re.search(r"\d+\s*dorm", t):
        result["bedrooms"] = extract_leading_int(text)

    if result["bathrooms"] is None and re.search(r"\d+\s*ba[ñn]", t):
        result["bathrooms"] = extract_leading_int(text)

    if result["rooms"] is None and re.search(r"\d+\s*amb", t):
        result["rooms"] = extract_leading_int(text)

    # Surface: "45 m² cubie." or "60 m² tot." — check qualifier
    m2_match = re.search(r"([\d.,]+)\s*m[²2]?\s*(cubie|cub|tot|total)?", t)
    if m2_match:
        val = extract_leading_float(m2_match.group(0))
        qualifier = (m2_match.group(2) or "").lower()
        if "tot" in qualifier and result["surface_total_m2"] is None:
            result["surface_total_m2"] = val
        elif result["surface_covered_m2"] is None:
            result["surface_covered_m2"] = val


def parse_features(item: Tag) -> dict:
    """
    Parse the card__main-features list for rooms, bedrooms, bathrooms, surfaces.

    Primary strategy: identify feature type by icon class (e.g. basico1-icon-bed).
    Fallback strategy: regex on text content for all items.

    Returns dict with keys: rooms, bedrooms, bathrooms, surface_total_m2, surface_covered_m2.
    All values are float/int or None.
    """
    result: dict = {
        "rooms": None,
        "bedrooms": None,
        "bathrooms": None,
        "surface_total_m2": None,
        "surface_covered_m2": None,
    }

    for li in item.select(SEL_MAIN_FEATURES):
        icon = li.select_one("i")
        span = li.select_one("span")
        text = span.get_text(strip=True) if span else li.get_text(strip=True)
        icon_classes = " ".join(icon.get("class", [])) if icon else ""

        # Strategy A: icon-based
        if ICON_BEDROOMS in icon_classes:
            result["bedrooms"] = extract_leading_int(text)
        elif ICON_BATHROOMS in icon_classes:
            result["bathrooms"] = extract_leading_int(text)
        elif ICON_SURFACE in icon_classes:
            val = extract_leading_float(text)
            if "tot" in text.lower():
                result["surface_total_m2"] = val
            else:
                result["surface_covered_m2"] = val
        elif ICON_ROOMS in icon_classes:
            result["rooms"] = extract_leading_int(text)

        # Strategy B: text fallback (always runs, fills remaining gaps)
        _apply_text_fallbacks(text, result)

    return result


def parse_photos(item: Tag) -> list[str]:
    """
    Extract property image URLs from the card photos box.

    Prefers data-src (lazy-loaded real URL) over src.
    Skips placeholder or blank images.
    Also checks li[data-lazy-loader] as an alternative lazy-loading pattern.
    """
    photos_box = item.select_one(SEL_PHOTOS_BOX)
    if not photos_box:
        return []

    seen: set[str] = set()
    images: list[str] = []

    def add_url(url: str | None) -> None:
        if url and url.startswith("http") and "placeholder" not in url.lower():
            if url not in seen:
                seen.add(url)
                images.append(url)

    for img in photos_box.select("img"):
        add_url(img.get("data-src") or img.get("data-lazy") or img.get("src"))

    # Alternative lazy-loading pattern seen in ArgenProp HTML
    for li in photos_box.select("li[data-lazy-loader]"):
        add_url(li.get("data-lazy-loader"))

    return images


# ── CARD PARSING ──────────────────────────────────────────────────────────────

def parse_single_card(item: Tag) -> dict | None:
    """
    Parse one listing__item element into a normalized dict.
    Returns None if the card has no usable link (can't determine URL or ID).
    All fields default to None if not found — never raises.
    """
    # URL and property ID
    link_tag = item.select_one(SEL_CARD_LINK)
    if not link_tag:
        return None

    relative_url = link_tag.get("href", "")
    if not relative_url or not relative_url.startswith("/"):
        return None

    full_url = urljoin(BASE_URL, relative_url)
    property_id = extract_property_id(relative_url)

    # Price
    price_tag = item.select_one(SEL_PRICE)
    price_text = price_tag.get_text(strip=True) if price_tag else None
    price_usd, price_currency = parse_price(price_text)

    # Generic card title — e.g. "Departamento en Venta en Belgrano, Capital Federal"
    # SEL_STREET_ADDRESS (p.card__title--primary) contains this text.
    title_tag = item.select_one(SEL_STREET_ADDRESS)
    card_title = title_tag.get_text(strip=True) if title_tag else None

    # Actual street address — SEL_ADDRESS (p.card__address) holds the street.
    address_tag = item.select_one(SEL_ADDRESS)
    street_address = address_tag.get_text(strip=True) if address_tag else None

    # Extract neighborhood from the generic title text.
    # Format: "Departamento en Venta en Belgrano, Capital Federal"
    # Strategy: split on first comma → take last " en " segment of left side.
    neighborhood: str | None = None
    city: str | None = None
    if card_title:
        parts = card_title.split(",", 1)
        before_comma = parts[0]   # "Departamento en Venta en Belgrano"
        city = parts[1].strip() if len(parts) > 1 else None
        en_parts = before_comma.rsplit(" en ", 1)
        if len(en_parts) > 1:
            neighborhood = en_parts[1].strip()  # "Belgrano"

    # Fallback: infer neighborhood from URL slug
    if not neighborhood and relative_url:
        slug_lower = relative_url.lower()
        if "nunez" in slug_lower or "n%c3%ba%c3%b1ez" in slug_lower:
            neighborhood = "Núñez"
        elif "belgrano" in slug_lower:
            neighborhood = "Belgrano"

    title = card_title

    # Property details
    property_details = parse_features(item)

    # Fallback: extract rooms (ambientes) from URL slug if not found in feature list.
    # URL pattern: "/departamento-en-venta-en-belgrano-2-ambientes--15913361"
    if property_details["rooms"] is None:
        m = re.search(r"-(\d+)-ambientes?", relative_url, re.IGNORECASE)
        if m:
            property_details["rooms"] = int(m.group(1))

    # Description (excerpt visible on card)
    desc_tag = item.select_one(SEL_DESCRIPTION)
    description = desc_tag.get_text(separator=" ", strip=True) if desc_tag else None

    # Photos
    images = parse_photos(item)

    return {
        "id": property_id,
        "title": title,
        "price_usd": price_usd,
        "price_currency": price_currency,
        "location": {
            "neighborhood": neighborhood,
            "street_address": street_address,
            "city": city or "Buenos Aires",
            "coordinates": None,   # populated by fetch_detail_page when FETCH_COORDINATES = True
        },
        "property_details": property_details,
        "description": description,
        "images": images,
        "url": full_url,
        "source": "argenprop",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "features": [],
    }


def parse_listing_cards(soup: BeautifulSoup) -> list[dict]:
    """
    Find all listing__item elements on a page and parse each one.
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


# ── DETAIL PAGE ENRICHMENT (optional) ─────────────────────────────────────────

def fetch_detail_page(session: requests.Session, listing: dict) -> dict:
    """
    Fetch the individual property page to enrich description and features.
    Only called when FETCH_DETAIL_PAGES is True.
    Modifies the listing dict in-place; always returns it (even on failure).
    """
    url = listing.get("url")
    if not url:
        return listing

    time.sleep(random.uniform(*REQUEST_DELAY_SECONDS))
    resp = fetch_with_retry(session, url)
    if resp is None:
        return listing

    soup = BeautifulSoup(resp.text, "lxml")

    # Description
    if not listing.get("description"):
        for sel in SEL_DETAIL_DESCRIPTION:
            el = soup.select_one(sel)
            if el:
                listing["description"] = el.get_text(separator=" ", strip=True)
                break

    # Features/amenities
    if not listing.get("features"):
        for sel in SEL_DETAIL_FEATURES:
            els = soup.select(sel)
            if els:
                listing["features"] = [
                    el.get_text(strip=True) for el in els if el.get_text(strip=True)
                ]
                break

    # Coordinates — ArgenProp renders a Leaflet map whose container div carries
    # data-latitude / data-longitude using Argentine comma-decimal format.
    # Example: data-latitude="-34,56535" → -34.56535
    leaflet = soup.select_one("div.leaflet-container[data-latitude]")
    if leaflet:
        try:
            listing["location"]["coordinates"] = {
                "latitude":  float(leaflet["data-latitude"].replace(",", ".")),
                "longitude": float(leaflet["data-longitude"].replace(",", ".")),
            }
        except (KeyError, ValueError) as exc:
            log.debug("Could not parse Leaflet coordinates: %s", exc)

    return listing


# ── FILTERS AND DEDUPLICATION ─────────────────────────────────────────────────

def filter_listing(listing: dict, config: dict) -> bool:
    """
    Keep only listings matching the configured currency and price range.
    The server-side URL filter handles this too, but we verify client-side
    to catch edge cases from ArgenProp's re-indexing.
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
    """
    Remove duplicate listings by property ID (first occurrence wins).
    Listings without an ID are kept but logged.
    """
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
    session: requests.Session,
    config: dict,
) -> tuple[list[dict], int | None]:
    """
    Scrape all listing pages (up to config.scraping.max_pages) and return
    collected listings along with the total result count reported by the site.
    """
    scraping_cfg  = config.get("scraping", {})
    max_pages     = int(scraping_cfg.get("max_pages", 10))
    delay_range   = scraping_cfg.get("delay_between_requests_seconds", [1.0, 2.0])

    all_listings: list[dict] = []
    total_results: int | None = None
    last_page: int = 1

    for page in range(1, max_pages + 1):
        url = build_argenprop_url(config, page)
        log.info("── Page %d / max %d ──────────────────────────", page, max_pages)
        log.info("URL: %s", url)

        resp = fetch_with_retry(session, url)
        if resp is None:
            log.error("Failed to fetch page %d — stopping pagination", page)
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # On first page: gather metadata and determine page range
        if page == 1:
            total_results = parse_total_results(soup)
            last_page = get_last_page(soup, max_pages)
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

        # Polite delay before the next page request
        delay = random.uniform(delay_range[0], delay_range[1])
        log.info("Waiting %.2fs before next page...", delay)
        time.sleep(delay)

    return all_listings, total_results


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def build_output(
    listings: list[dict],
    total_results: int | None,
    search_criteria: dict,
) -> dict:
    return {
        "metadata": {
            "total_results": total_results,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "search_criteria": search_criteria,
            "listings_count": len(listings),
        },
        "listings": listings,
    }


def save_output(data: dict) -> str:
    """
    Write the output JSON to output/argenprop_results_YYYY-MM-DD_HH-MM-SS.json.
    Creates the output directory if it does not exist.
    Returns the path of the written file.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = OUTPUT_FILENAME.format(timestamp=timestamp)
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("Output written to: %s", filepath)
    return filepath


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load shared search filters
    config = load_config()
    price_cfg    = config.get("price", {})
    scraping_cfg = config.get("scraping", {})

    price_min  = price_cfg.get("min", 0)
    price_max  = price_cfg.get("max", 0)
    currency   = price_cfg.get("currency", "USD")
    max_pages  = scraping_cfg.get("max_pages", 10)
    max_retries = int(scraping_cfg.get("max_retries", MAX_RETRIES))

    preview_url = build_argenprop_url(config, page=1)
    log.info("=== ArgenProp Scraper starting ===")
    log.info("Config: %s", CONFIG_FILE)
    log.info("Search URL (page 1): %s", preview_url)
    log.info(
        "Price: %s %s–%s | Neighborhoods: %s | Max pages: %d",
        currency, price_min, price_max,
        config.get("location", {}).get("neighborhoods", []),
        max_pages,
    )

    session = make_session()

    # Phase 1: scrape all pages
    raw_listings, total_results = scrape_all_pages(session, config)
    log.info("Raw listings collected: %d", len(raw_listings))

    # Phase 2: fetch detail pages for coordinates and/or full description/features
    if FETCH_COORDINATES or FETCH_DETAIL_PAGES:
        log.info(
            "Fetching detail pages for %d listings (coordinates=%s, details=%s)...",
            len(raw_listings), FETCH_COORDINATES, FETCH_DETAIL_PAGES,
        )
        for i, listing in enumerate(raw_listings, 1):
            log.info("Detail page %d/%d: %s", i, len(raw_listings), listing.get("url", ""))
            raw_listings[i - 1] = fetch_detail_page(session, listing)

    # Phase 3: filter by price/currency (client-side verification)
    filtered = [l for l in raw_listings if filter_listing(l, config)]
    skipped = len(raw_listings) - len(filtered)
    if skipped:
        log.info("Filtered out %d listing(s) outside %s %s–%s range", skipped, currency, price_min, price_max)
    log.info("After price filter: %d listings", len(filtered))

    # Phase 4: deduplicate
    unique = deduplicate(filtered)
    log.info("After deduplication: %d unique listings", len(unique))

    # Phase 5: build and save output
    output = build_output(unique, total_results, config)
    filepath = save_output(output)

    log.info("=== Done. %d listings saved to %s ===", len(unique), filepath)


if __name__ == "__main__":
    main()
