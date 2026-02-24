# findmyhome — Backend

> **Disclaimer:** This project is built for educational purposes only. It is a personal learning tool and is not intended for commercial use or mass data collection.

Python scraper pipeline + FastAPI backend for the **Find My Home** property aggregator.

Scrapes multiple Argentine real estate sites, normalises listings, stores them in MongoDB, and serves them through a REST API consumed by `findmyhome-app`.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI + Uvicorn |
| Async DB driver | Motor (MongoDB) |
| Sync DB driver | PyMongo (scripts) |
| HTTP | `requests` · `cloudscraper` (Cloudflare bypass) |
| HTML parsing | BeautifulSoup4 + lxml |
| Config | `python-dotenv` · `config/search_filters.json` |

---

## Project Layout

```
run.py                     # Master pipeline orchestrator (scrape → parse → upload)
config/search_filters.json # All search params — edit this to change filters

scrapers/
  *_scraper.py             # one scraper per source
  single.py                # on-demand dispatcher (used by POST /scrape)

parser/parser.py           # normalise + remap + compute boolean flags

db/
  upload.py                # bulk upsert to MongoDB
  read.py                  # inspect collection

api/
  main.py                  # FastAPI app · CORS · lifespan · index creation
  db.py                    # Motor connection
  models.py                # Pydantic schemas
  routes/
    properties.py          # GET/POST/PUT/DELETE/PATCH /properties
    scrape.py              # POST /scrape · POST /scrape/batch

output/                    # raw JSON from scrapers (auto-generated, gitignored)
parsed/                    # normalised JSON before DB upload (auto-generated, gitignored)
```

---

## API Reference

### Properties

| Method | Path | Description |
|---|---|---|
| `GET` | `/properties` | Paginated list with filters |
| `GET` | `/properties/{fuente}/{id}` | Single property |
| `POST` | `/properties` | Create |
| `PUT` | `/properties/{fuente}/{id}` | Partial update |
| `DELETE` | `/properties/{fuente}/{id}` | Delete |
| `PATCH` | `/properties/{fuente}/{id}/favourite` | Toggle `favorito` |
| `PATCH` | `/properties/{fuente}/{id}/visited` | Toggle `visitado` |
| `PATCH` | `/properties/{fuente}/{id}/hidden` | Toggle `oculto` |
| `PATCH` | `/properties/{fuente}/{id}/notes` | Update `comentarios` / `flagsManual` |

#### `GET /properties` — query params

| Param | Type | Description |
|---|---|---|
| `barrio` | string | Neighbourhood partial match (case-insensitive) |
| `fuente` | string | Source site identifier |
| `precio_min` / `precio_max` | float | Price range in USD |
| `ambientes` | int | Total rooms |
| `dormitorios` | int | Bedrooms |
| `flags` | string[] | Flag names that must be `true` (repeatable) |
| `exclude_flags` | string[] | Flag names that must not be `true` |
| `favorito` | bool | `true` → favourites only |
| `oculto` | bool | `false` → exclude hidden (default for frontend) |
| `sort_by` | string | `precio` · `superficie_cubierta` · `superficie_total` |
| `sort_order` | string | `asc` (default) · `desc` |
| `page` / `pageSize` | int | Pagination (default: page 1, 20 per page, max 100) |

### Scrape

| Method | Path | Description |
|---|---|---|
| `POST` | `/scrape` | Scrape a single URL on demand |
| `POST` | `/scrape/batch` | Run all 4 scrapers with a custom config (~60–120 s) |

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |

---

## Database Schema

**Collection:** `properties` · **Unique index:** `(id, fuente)`

```
id                  string   — source's internal property ID
fuente              string   — source site identifier
titulo              string
precioUsd           number
moneda              string
descripcion         string
imagenes            string[]
url                 string
extraidoEn          string   — ISO 8601 timestamp
caracteristicas     string[]

ubicacion
  barrio            string
  direccion         string
  ciudad            string
  coordenadas       object   — { lat, lng }

detalles
  ambientes         number
  dormitorios       number
  banos             number
  superficieTotal   number
  superficieCubierta number
  piso              number
  antiguedad        number

flags               — computed by parser/parser.py
  porEscalera       bool
  balcon            bool
  patio             bool
  enConstruccion    bool
  aptoCredito       bool
  cochera           bool
  cocheraOpcional   bool
  reservado         bool

favorito            bool     — user-managed, never overwritten by scraper
visitado            bool     — user-managed
oculto              bool     — user-managed
comentarios         string   — user notes
flagsManual         object   — user-set boolean overrides
```

---

## Scrapers — Technical Notes

Each scraper handles a different anti-bot setup:

- **Server-side rendered sites** — `requests` + BeautifulSoup with session cookie warm-up to avoid 403s.
- **Cloudflare-protected sites** — `cloudscraper` handles the JS challenge without a headless browser.
- **Next.js sites** — structured data extracted directly from the embedded `__NEXT_DATA__` JSON payload instead of parsing HTML.

---

## Architecture

The API is deployed to Render. The scraper pipeline runs locally — a batch job that writes intermediate files to disk and uploads results to MongoDB Atlas.

```
Local machine  →  run.py  →  MongoDB Atlas  ←  Render (API)  ←  Frontend
```

---

## Companion Repo

Frontend: [`findmyhome-app`](../findmyhome-app) — Next.js + React + TypeScript
