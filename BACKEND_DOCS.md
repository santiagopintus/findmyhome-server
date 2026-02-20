# Backend Docs — Encontremos Casa

Encontremos Casa is a property listing aggregator for Buenos Aires. It scrapes four Argentine real estate sites (ArgenProp, ZonaProp, RE/MAX, MercadoLibre), normalizes the data, stores it in MongoDB, and serves it through a FastAPI REST API consumed by the Next.js frontend.

---

## Project Structure

```
encontremos-casa/
├── run.py                         # Master pipeline: scrape → parse → upload
├── requirements.txt
├── .env                           # MONGODB_URI (git-ignored)
│
├── config/
│   └── search_filters.json        # Single source of truth for all scrapers
│
├── scrapers/
│   ├── argenprop_scraper.py       # requests + BeautifulSoup (no Cloudflare)
│   ├── zonaprop_scraper.py        # cloudscraper (Cloudflare bypass)
│   ├── remax_scraper.py           # embedded JSON extraction
│   ├── meli_scraper.py            # cloudscraper + __NEXT_DATA__ JSON
│   └── single.py                  # on-demand single-URL dispatcher
│
├── parser/
│   └── parser.py                  # normalize, remap keys, compute flags
│
├── db/
│   ├── upload.py                  # bulk upsert parsed JSON → MongoDB
│   └── read.py                    # inspect collection (summary / sample / lookup)
│
├── api/
│   ├── main.py                    # FastAPI app, CORS, lifespan
│   ├── db.py                      # Motor async MongoDB connection
│   ├── models.py                  # Pydantic schemas
│   └── routes/
│       ├── properties.py          # CRUD + favourite toggle
│       └── scrape.py              # POST /scrape, POST /scrape/batch
│
├── output/                        # raw scraper JSON (timestamped)
└── parsed/                        # normalized JSON ready for upload
```

---

## Full Pipeline: `python run.py`

```
[Step 1] All 4 scrapers in parallel threads
  argenprop_scraper.py → output/argenprop_results_*.json
  zonaprop_scraper.py  → output/zonaprop_results_*.json
  remax_scraper.py     → output/remax_results_*.json
  meli_scraper.py      → output/meli_results_*.json

[Step 2] parser.py (sequential, aborts on failure)
  Reads output/*.json → parsed/parsed_listings_*.json

[Step 3] db/upload.py (sequential, aborts on failure)
  Reads parsed/*.json → bulk upserts into MongoDB earthbnb.properties
```

- Scraper failures are tolerated (pipeline continues with remaining results).
- Parser and uploader failures abort the run.
- Each subprocess logs with a `[name]` prefix for easy filtering.

---

## Configuration: `config/search_filters.json`

All scrapers load this file at startup — no code changes needed to adjust search criteria.

```json
{
  "property": {
    "type": "departamento",
    "operation": "sale"
  },
  "location": {
    "country": "Argentina",
    "city": "Buenos Aires",
    "neighborhoods": ["Belgrano", "Núñez", "Saavedra", "Villa Urquiza"]
  },
  "price": {
    "currency": "USD",
    "min": 100000,
    "max": 175000
  },
  "features": {
    "bedrooms": [3],
    "parking_spots_min": 1
  },
  "scraping": {
    "max_pages": 10,
    "delay_between_requests_seconds": [1.0, 2.0],
    "max_retries": 3
  }
}
```

Each scraper translates these fields into its own URL format independently.

---

## Scrapers

### ArgenProp (`scrapers/argenprop_scraper.py`)

- **HTTP:** `requests` with session warm-up (hits homepage first to set cookies, avoiding 403s)
- **Parsing:** BeautifulSoup on server-rendered HTML
- **URL format:**
  ```
  /departamentos/venta/belgrano-o-nunez/3-dormitorios/dolares-100000-175000?1-o-mas-cocheras
  ```
- **Flags:** `FETCH_DETAIL_PAGES = False` (toggle to enrich with full description/amenities, adds ~1-2s per listing); `FETCH_COORDINATES = True` (extracts lat/lng from Leaflet map container attributes)
- **Dedup:** by property `id` within the run

### ZonaProp (`scrapers/zonaprop_scraper.py`)

- **HTTP:** `cloudscraper` (auto-solves Cloudflare challenges)
- **Parsing:** BeautifulSoup with `data-qa` attribute selectors
- **URL format:**
  ```
  /departamentos-venta-belgrano-nunez-desde-3-habitaciones-mas-de-1-garage-desde-100000-hasta-175000-dolar.html
  ```
- **Features:** concatenated text node parsed via regex (e.g. `"65 m² tot.3 amb.2 dorm.1 baño"`)
- **Coordinates:** Base64-encoded lat/lng in inline JS (`const mapLatOf = "..."`)
- **Property URL:** in `data-to-posting` attribute, not `<a href>`

### RE/MAX (`scrapers/remax_scraper.py`)

- **HTTP:** `requests` on server-rendered HTML
- **Parsing:** extracts embedded JSON from a `<script>` tag (full listing dataset, no HTML parsing needed)
- **URL format:**
  ```
  /listings/buy?page=0&pricein=1:0:175000&locations=in::::25006@Belgrano,25022@Nunez::::&in:totalRooms=3,4
  ```
- **Coordinates:** already in the JSON (GeoJSON `[longitude, latitude]` order)
- **Photos:** built as `https://img.remax.com.ar/{rawValue}`
- **Identification:** uses a UUID fingerprint to locate the correct `<script>` among several

### MercadoLibre (`scrapers/meli_scraper.py`)

- **HTTP:** `cloudscraper`
- **List page data:** `window.__PRELOADED_STATE__` JSON blob (regex extraction, with BeautifulSoup fallback using Polaris selectors)
- **Detail page data:** `<script id="__NEXT_DATA__">` JSON blob; `FETCH_DETAIL_PAGES = True` (required for full data)
- **URL format:**
  ```
  /departamentos/venta/propiedades-individuales/mas-de-3-dormitorios/capital-federal/belgrano-o-nunez/_PriceRange_100000USD-175000USD_NoIndex_True_Cocheras_1
  ```
- **Page size:** fixed at 48 items per page
- **Coordinates:** Schema.org JSON-LD on detail page

### single.py — On-Demand Dispatcher

Used by `POST /scrape`. Dispatches to the appropriate scraper based on the URL domain:

| Domain | Scraper |
|--------|---------|
| `www.argenprop.com` | `_scrape_argenprop()` |
| `www.zonaprop.com.ar` | `_scrape_zonaprop()` |
| `www.remax.com.ar` | `_scrape_remax()` |
| `inmuebles.mercadolibre.com.ar` / `departamento.mercadolibre.com.ar` | `_scrape_meli()` |

Returns `None` on failure; domain validation via `VALID_DOMAINS` dict.

---

## Parser (`parser/parser.py`)

**Input:** `output/*.json` (raw, English snake_case)
**Output:** `parsed/parsed_listings_YYYY-MM-DD_HH-MM-SS.json` (normalized, Spanish camelCase)

### Key Transformations

**Key remapping (snake_case → camelCase Spanish):**

| Raw field | Parsed field |
|-----------|-------------|
| `title` | `titulo` |
| `price_usd` | `precioUsd` |
| `source` | `fuente` |
| `scraped_at` | `extraidoEn` |
| `images` | `imagenes` |
| `description` | `descripcion` |
| `location.neighborhood` | `ubicacion.barrio` |
| `location.street_address` | `ubicacion.direccion` |
| `location.city` | `ubicacion.ciudad` |
| `location.coordinates` | `ubicacion.coordenadas` |
| `property_details.rooms` | `detalles.ambientes` |
| `property_details.bedrooms` | `detalles.dormitorios` |
| `property_details.bathrooms` | `detalles.banos` |
| `property_details.surface_total_m2` | `detalles.superficieTotal` |
| `property_details.surface_covered_m2` | `detalles.superficieCubierta` |

**Deduplication:** `(id, fuente)` compound key; first occurrence wins.

**Flag computation** (regex on title + description):

| Flag | Logic |
|------|-------|
| `porEscalera` | "por escalera" present AND "ascensor" absent |
| `balcon` | matches `balc[oó]n` |
| `enConstruccion` | matches "de pozo", "emprendimiento", or "a construir" |
| `aptoCredito` | matches `apto\s+cr[eé]dito\|cr[eé]dito` |
| `cochera` | matches "cochera" or "coche" |

### Parsed Output Schema

```json
{
  "meta": {
    "totalListings": 312,
    "fuentes": ["argenprop", "zonaprop", "remax", "meli"],
    "generadoEn": "2026-02-19T04:00:00+00:00"
  },
  "listings": [
    {
      "id": "19702981",
      "titulo": "Montañeses al 2700",
      "precioUsd": 155000,
      "moneda": "USD",
      "descripcion": "...",
      "imagenes": ["https://..."],
      "url": "https://www.argenprop.com/...",
      "fuente": "argenprop",
      "extraidoEn": "2026-02-19T04:00:00+00:00",
      "caracteristicas": [],
      "ubicacion": {
        "barrio": "Belgrano",
        "direccion": "Montañeses al 2700",
        "ciudad": "Capital Federal",
        "coordenadas": { "latitude": -34.567, "longitude": -58.456 }
      },
      "detalles": {
        "ambientes": 3,
        "dormitorios": 2,
        "banos": 1,
        "superficieTotal": null,
        "superficieCubierta": 65.0
      },
      "flags": {
        "porEscalera": false,
        "balcon": true,
        "enConstruccion": false,
        "aptoCredito": false,
        "cochera": true
      }
    }
  ]
}
```

---

## Database

- **Driver (scripts):** `pymongo` (synchronous)
- **Driver (API):** `motor` (async, for FastAPI)
- **Database:** `earthbnb`
- **Collection:** `properties`
- **Unique index:** compound `(id, fuente)` — prevents duplicates across upserts
- **Upsert strategy:** `UpdateOne(filter={id, fuente}, update=$set, upsert=True)`

### Full Document Schema

```
Property {
  id: str                          # source-assigned property ID
  fuente: str                      # "argenprop" | "zonaprop" | "remax" | "meli"
  titulo: str | None
  precioUsd: float | None
  moneda: str | None               # "USD" | "ARS"
  descripcion: str | None
  imagenes: list[str]
  url: str | None
  extraidoEn: str | None           # ISO 8601
  caracteristicas: list[str]
  ubicacion: {
    barrio: str | None
    direccion: str | None
    ciudad: str | None
    coordenadas: { latitude: float, longitude: float } | None
  } | None
  detalles: {
    ambientes: int | None
    dormitorios: int | None
    banos: int | None
    superficieTotal: float | None
    superficieCubierta: float | None
  } | None
  flags: {
    porEscalera: bool
    balcon: bool
    enConstruccion: bool
    aptoCredito: bool
    cochera: bool
  } | None
  favorito: bool
}
```

### `db/read.py` — Inspection Utility

```bash
python db/read.py              # summary: counts by source, price range, flags breakdown
python db/read.py --sample     # 3 random docs per source
python db/read.py --id 19702981 --fuente argenprop  # single doc lookup
```

---

## API

**Framework:** FastAPI + Uvicorn
**Base URL:** `http://localhost:8000`
**CORS:** fully open in development (`allow_origins: ["*"]`)

Start the server:
```bash
uvicorn api.main:app --reload
```

### `GET /properties` — Paginated listing

| Param | Type | Description |
|-------|------|-------------|
| `page` | int | Default 1 |
| `pageSize` | int | Default 20, max 100 |
| `barrio` | string | Case-insensitive regex partial match |
| `fuente` | string | `argenprop` / `zonaprop` / `remax` / `meli` |
| `precio_min` | float | Min price USD |
| `precio_max` | float | Max price USD |
| `ambientes` | int | Exact match |
| `dormitorios` | int | Exact match |
| `flags` | string (repeatable) | Flag names that must be `true` |
| `favorito` | bool | `true` → favorites only |

**Response:**
```json
{
  "total": 312,
  "page": 1,
  "pageSize": 20,
  "pages": 16,
  "results": [ /* Property[] */ ]
}
```

### `GET /properties/{fuente}/{id}` — Single property

Returns a full property document or `404`.

### `POST /properties` — Create property

Body: `PropertyCreate`. Returns `201` on success, `409` if already exists.
Rarely used directly; the scraper pipeline handles inserts via upserts.

### `PUT /properties/{fuente}/{id}` — Partial update

Body: `PropertyUpdate` (all fields optional). Returns updated document.

### `DELETE /properties/{fuente}/{id}` — Delete property

Returns `204` on success.

### `PATCH /properties/{fuente}/{id}/favourite` — Toggle favourite

```json
{ "favorito": true }
```

Returns updated full property document or `404`.

### `POST /scrape` — On-demand single URL

```json
{ "url": "https://www.argenprop.com/departamento-..." }
```

| Status | Meaning |
|--------|---------|
| `201` | New property inserted |
| `200` | Existing property refreshed |
| `400` | Unsupported domain or malformed URL |
| `500` | Scraping/parsing failed |

Returns the full property document on success.

**Supported domains:** `www.argenprop.com`, `www.zonaprop.com.ar`, `www.remax.com.ar`, `inmuebles.mercadolibre.com.ar`, `departamento.mercadolibre.com.ar`

### `POST /scrape/batch` — Full search with custom config

Runs all 4 scrapers in parallel (~60–120s). Body mirrors `search_filters.json`; only `location` and `price` are required:

```json
{
  "location": {
    "city": "Buenos Aires",
    "neighborhoods": ["Belgrano", "Núñez"]
  },
  "price": {
    "currency": "USD",
    "min": 100000,
    "max": 175000
  }
}
```

**Response:**
```json
{
  "inserted": [ /* Property[] */ ],
  "updated": [ /* Property[] */ ],
  "total_inserted": 24,
  "total_updated": 12,
  "errors": [ /* string[] */ ]
}
```

---

## Environment

```bash
# .env
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/earthbnb
```

Required to start the API or run any DB scripts.

---

## Requirements

```
requests==2.31.0
beautifulsoup4==4.12.3
lxml==5.1.0
cloudscraper>=1.2.71
pymongo>=4.6.0
python-dotenv>=1.0.0
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
motor>=3.3.0
```

Install:
```bash
pip install -r requirements.txt
```

---

## Quick Reference

```bash
# Run the full pipeline (scrape → parse → upload)
python run.py

# Start the API server
uvicorn api.main:app --reload

# Inspect the database
python db/read.py
python db/read.py --sample
python db/read.py --id <id> --fuente <fuente>

# Scrape a single property (via API)
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.argenprop.com/departamento-..."}'

# Query properties with filters
curl "http://localhost:8000/properties?barrio=Belgrano&precio_max=150000&flags=cochera&flags=balcon"
```
