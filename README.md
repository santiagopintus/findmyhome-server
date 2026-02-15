# encontremos-casa

Property listing scrapers for Buenos Aires real estate research.

## Search Filters

All search criteria live in **`config/search_filters.json`** — a single shared file
used by every scraper in this project. Edit it to change what you're looking for;
no scraper code needs to change.

```json
{
  "property": {
    "type": "departamento",
    "operation": "sale"
  },
  "location": {
    "city": "Buenos Aires",
    "neighborhoods": ["Belgrano", "Núñez", "Saavedra", "Villa Urquiza"]
  },
  "price": {
    "currency": "USD",
    "min": 150000,
    "max": 180000
  },
  "features": {
    "bedrooms": [2, 3],
    "parking_spots_min": 1
  },
  "scraping": {
    "max_pages": 10,
    "delay_between_requests_seconds": [1.0, 2.0],
    "max_retries": 3
  }
}
```

### Field reference

| Field | Type | Description |
|---|---|---|
| `property.type` | string | `"departamento"`, `"casa"`, `"ph"`, etc. |
| `property.operation` | string | `"sale"` or `"rent"` |
| `location.neighborhoods` | array | Display names — each scraper normalizes to its own slug format |
| `price.currency` | string | `"USD"` or `"ARS"` |
| `price.min` / `price.max` | number | Price range in the chosen currency |
| `features.bedrooms` | array | Accepted bedroom counts — `[2, 3]` means 2 OR 3 bedrooms |
| `features.parking_spots_min` | number | Minimum parking spots. `0` or omit = no filter |
| `scraping.max_pages` | number | Hard cap on pages fetched per scraper run |
| `scraping.delay_between_requests_seconds` | [min, max] | Random delay range between page requests |
| `scraping.max_retries` | number | HTTP retry attempts per failed request |

## Scrapers

### ArgenProp Scraper

```bash
pip install -r requirements.txt
python scrapers/argenprop_scraper.py
```

Reads `config/search_filters.json` and builds the ArgenProp search URL automatically:

```
https://www.argenprop.com/departamentos/venta/
  belgrano-o-nunez-o-saavedra-o-villa-urquiza/
  2-dormitorios-o-3-dormitorios/
  dolares-150000-180000
  ?1-o-mas-cocheras
```

Output is written to `output/argenprop_results_YYYY-MM-DD_HH-MM-SS.json`.

**ArgenProp URL translation rules** (in `scrapers/argenprop_scraper.py`):

| Config field | ArgenProp URL segment |
|---|---|
| `neighborhoods: ["Belgrano", "Núñez"]` | `belgrano-o-nunez` (path segment) |
| `bedrooms: [2, 3]` | `2-dormitorios-o-3-dormitorios` (path segment) |
| `price: {min: 150000, max: 180000, currency: "USD"}` | `dolares-150000-180000` (path segment) |
| `parking_spots_min: 1` | `?1-o-mas-cocheras` (query param) |

Common neighborhood display names: `Belgrano`, `Núñez`, `Palermo`, `Villa Urquiza`,
`Colegiales`, `Saavedra`, `Villa Devoto`, `Caballito`.

**Scraper-only setting** (not in config): set `FETCH_DETAIL_PAGES = True` at the top of
`argenprop_scraper.py` to fetch each property's detail page for full description and
amenities list. Off by default — significantly increases runtime.

#### Output Schema

```json
{
  "metadata": {
    "total_results": 446,
    "scraped_at": "2026-02-13T19:30:00+00:00",
    "search_criteria": {
      "neighborhoods": ["Núñez", "Belgrano"],
      "price_min": 150000,
      "price_max": 160000,
      "property_type": "departamentos",
      "operation": "venta"
    },
    "listings_count": 45
  },
  "listings": [
    {
      "id": "19702981",
      "title": "Montañeses al 2700",
      "price_usd": 155000,
      "price_currency": "USD",
      "location": {
        "neighborhood": "Belgrano",
        "street_address": "Montañeses al 2700",
        "city": "Capital Federal"
      },
      "property_details": {
        "rooms": 2,
        "bedrooms": 1,
        "bathrooms": 1,
        "surface_total_m2": null,
        "surface_covered_m2": 45.0
      },
      "description": "Departamento a estrenar...",
      "images": [
        "https://www.argenprop.com/static-content/19702981/photo1_u_small.jpg"
      ],
      "url": "https://www.argenprop.com/departamento-en-venta-en-belgrano-2-ambientes--19702981",
      "source": "argenprop",
      "scraped_at": "2026-02-13T19:30:00+00:00",
      "features": []
    }
  ]
}
```

**Field notes:**

- `id` — ArgenProp's internal numeric property ID (from URL slug)
- `price_usd` — Always in USD; ARS listings are filtered out
- `property_details.rooms` — Total ambientes (includes living room)
- `property_details.bedrooms` — Dormitorios only
- `surface_total_m2` / `surface_covered_m2` — Either or both may be `null` if not listed on the card
- `features` — Amenities list (balcón, cochera, etc.); populated only if `FETCH_DETAIL_PAGES = True`
- `description` — Short excerpt from listing card; full text requires `FETCH_DETAIL_PAGES = True`

#### Technical Notes

**Why `requests` works (no Playwright needed)**

ArgenProp is server-side rendered. The 403 errors encountered with simple requests
are caused by missing session cookies, not JavaScript rendering. The scraper fixes this
by warming up the session on the homepage before making search requests.

**Argentine price format**

Argentina uses `.` as a thousands separator and `,` as the decimal separator.
`"USD 159.999"` means USD 159,999 — not USD 159.999. The `parse_price()` function
handles this disambiguation correctly.

**CSS selector maintenance**

All HTML selectors are defined as constants (`SEL_*`) at the top of the file.
If ArgenProp changes their HTML structure, update only those constants.

**Polite scraping**

The scraper waits 1–2 seconds between page requests and includes standard browser
headers. Do not reduce `REQUEST_DELAY_SECONDS` below 1 second.

---

### ZonaProp Scraper

```bash
pip install -r requirements.txt
python scrapers/zonaprop_scraper.py
```

Reads `config/search_filters.json` and builds the ZonaProp search URL automatically:

```
https://www.zonaprop.com.ar/departamentos-venta-belgrano-nunez-saavedra-villa-urquiza-
  desde-2-hasta-3-habitaciones-mas-de-1-garage-desde-150000-hasta-180000-dolar.html
```

Output is written to `output/zonaprop_results_YYYY-MM-DD_HH-MM-SS.json`.

**ZonaProp URL translation rules** (in `scrapers/zonaprop_scraper.py`):

| Config field | ZonaProp URL segment |
|---|---|
| `neighborhoods: ["Belgrano", "Núñez"]` | `belgrano-nunez` (dash-separated, no `-o-`) |
| `bedrooms: [2, 3]` | `desde-2-hasta-3-habitaciones` (range format) |
| `price: {min: 150000, max: 180000, currency: "USD"}` | `desde-150000-hasta-180000-dolar` (range in URL) |
| `parking_spots_min: 1` | `mas-de-1-garage` (minimum threshold) |

**Scraper-only setting** (not in config): set `FETCH_DETAIL_PAGES = True` at the top of
`zonaprop_scraper.py` to fetch each property's detail page for full description and
amenities list. Off by default — significantly increases runtime.

#### Output Schema

Same format as ArgenProp (see Output Schema section above). The `source` field will be
`"zonaprop"` instead of `"argenprop"`.

#### Technical Notes

**Cloudflare protection & `cloudscraper`**

ZonaProp is behind Cloudflare's bot protection with JavaScript challenges. Plain `requests`
returns 403 ("Just a moment..."). The scraper uses the `cloudscraper` library, which
automatically handles Cloudflare's verification challenge without needing a headless browser.

**ZonaProp HTML structure (verified Feb 2026)**

- Listing cards use `data-qa="posting PROPERTY"` with `data-id` attribute for property ID
- Property URL is in `data-to-posting` attribute on the card (not in `<a href>`)
- Features are concatenated into a single text node: `"65 m² tot.3 amb.2 dorm.1 baño1 coch."`
- Images are in `[data-qa='POSTING_CARD_GALLERY']` as plain `<img src="...">` from `imgar.zonapropcdn.com`
- CSS selectors defined as `SEL_*` constants at the top of the file for easy maintenance

**Argentine price format**

Same as ArgenProp: `.` = thousands, `,` = decimal. Handled by `parse_price()` function.

**Polite scraping**

Same delays and retry strategy as ArgenProp: 1–2 seconds between page requests, exponential
backoff on failures, up to 3 retries per page.
