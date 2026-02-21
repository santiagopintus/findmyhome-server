# CLAUDE.md — encontremos-casa (Backend)

Python scraper pipeline + FastAPI backend for the Encontremos Casa property aggregator.

## What This Repo Does

Scrapes four Argentine real estate sites (ArgenProp, ZonaProp, RE/MAX, MercadoLibre), normalizes listings, stores them in MongoDB, and serves them through a REST API consumed by `encontremos-casa-app`.

## Tech Stack

- **Language:** Python 3.11+
- **Web framework:** FastAPI + Uvicorn
- **Async DB driver:** Motor (MongoDB)
- **Sync DB driver:** PyMongo (scripts)
- **HTTP:** `requests` (argenprop, remax), `cloudscraper` (zonaprop, meli)
- **HTML parsing:** BeautifulSoup4 + lxml
- **Config:** `python-dotenv`, `config/search_filters.json`

## Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Required environment variable
# Create .env with:
# MONGODB_URI=mongodb+srv://...

# Run the full scrape → parse → upload pipeline
python run.py

# Start the API server
uvicorn api.main:app --reload
# API available at http://localhost:8000
```

## Key Conventions

- **Config is centralized.** All scraper filters (neighborhoods, price range, bedrooms, max pages) live in `config/search_filters.json`. Never hardcode search params in scraper files.
- **DB schema uses Spanish camelCase.** Raw scrapers output English snake_case; the parser in `parser/parser.py` remaps everything to Spanish camelCase before DB upload. The API and frontend speak Spanish camelCase.
- **Upsert by `(id, fuente)`.** This compound key is the unique identity for every property. Never insert without this pair.
- **Scrapers are parallel, DB steps are sequential.** `run.py` runs all 4 scrapers concurrently; parser and uploader run sequentially after all scrapers finish.
- **Scraper failures are tolerated; parser/uploader failures are fatal.** The pipeline continues if one scraper fails but aborts if the parser or uploader fails.
- **Polite scraping.** All scrapers use 1–2s delays between requests and exponential backoff on errors. Do not remove these delays.

## Project Layout

```
run.py                     # Master pipeline orchestrator
config/search_filters.json # All search params (edit this to change filters)
scrapers/
  argenprop_scraper.py     # requests + BeautifulSoup
  zonaprop_scraper.py      # cloudscraper (Cloudflare bypass)
  remax_scraper.py         # embedded JSON extraction
  meli_scraper.py          # cloudscraper + __NEXT_DATA__ JSON
  single.py                # on-demand dispatcher (used by POST /scrape)
parser/parser.py           # normalize + remap + compute flags
db/
  upload.py                # bulk upsert to MongoDB
  read.py                  # inspect collection
api/
  main.py                  # FastAPI app + CORS + lifespan
  db.py                    # Motor connection
  models.py                # Pydantic schemas
  routes/
    properties.py          # GET/POST/PUT/DELETE/PATCH /properties
    scrape.py              # POST /scrape, POST /scrape/batch
output/                    # raw JSON from scrapers (auto-generated)
parsed/                    # normalized JSON before DB upload (auto-generated)
```

## API Endpoints (summary)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/properties` | Paginated list with filters (barrio, fuente, precio, ambientes, flags, favorito) |
| `GET` | `/properties/{fuente}/{id}` | Single property |
| `POST` | `/properties` | Create (rarely used directly) |
| `PUT` | `/properties/{fuente}/{id}` | Partial update |
| `DELETE` | `/properties/{fuente}/{id}` | Delete |
| `PATCH` | `/properties/{fuente}/{id}/favourite` | Toggle `favorito` boolean |
| `PATCH` | `/properties/{fuente}/{id}/visited` | Toggle `visitado` boolean |
| `POST` | `/scrape` | Scrape a single URL on demand |
| `POST` | `/scrape/batch` | Run all 4 scrapers with custom config (~60-120s) |

Full API docs: see `BACKEND_DOCS.md`. Auto-generated docs at `http://localhost:8000/docs` when the server is running.

## DB Schema Quick Reference

Database: `earthbnb` / Collection: `properties`

Unique index: `(id, fuente)`

Key fields: `id`, `fuente`, `titulo`, `precioUsd`, `imagenes`, `url`, `extraidoEn`, `ubicacion.barrio`, `detalles.ambientes/dormitorios/banos/superficieTotal/superficieCubierta`, `flags.porEscalera/balcon/enConstruccion/aptoCredito/cochera`, `favorito`, `visitado`

## Companion Repo

Frontend: `../encontremos-casa-app` (Next.js 16, React 19, TypeScript)

The frontend expects the API at `http://localhost:8000`. CORS is open in development.
