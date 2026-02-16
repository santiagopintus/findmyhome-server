# db/upload.py

Uploads parsed property listings to MongoDB. Read-only from the database's perspective — it only inserts or updates documents, never deletes them.

---

## Prerequisites

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root (already gitignored):

```
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
```

---

## How it works

### Input
Reads every `*.json` file inside the `parsed/` directory. These are produced by `parser/parser.py`. Each file has the shape:

```json
{
  "meta": { "totalListings": 34, "fuentes": [...], "generadoEn": "..." },
  "listings": [ { "id": "...", "fuente": "argenprop", ... } ]
}
```

### Deduplication
Each listing is upserted using the compound key `(id, fuente)`. This means:
- A listing that doesn't exist yet → **inserted**
- A listing already in the DB (same `id` + `fuente`) → **updated in place**
- Running the script twice on the same file is completely safe

A unique index on `(id, fuente)` is created on the first run and is idempotent on subsequent runs.

### Output
Writes to the `earthbnb` database, `properties` collection on the configured Atlas cluster.

---

## Usage

```bash
# Upload all files in parsed/
python db/upload.py

# Upload a specific file
python db/upload.py parsed/parsed_listings_2026-02-15_19-37-29.json
```

### Typical workflow

```bash
python scrapers/argenprop_scraper.py   # → output/argenprop_results_....json
python scrapers/zonaprop_scraper.py    # → output/zonaprop_results_....json
python scrapers/remax_scraper.py       # → output/remax_results_....json
python parser/parser.py                # → parsed/parsed_listings_....json
python db/upload.py                    # → upserts into MongoDB
```

---

## Database schema

**Database:** `earthbnb`
**Collection:** `properties`
**Index:** `{ id: 1, fuente: 1 }` (unique)

Each document is a parsed listing with camelCase Spanish keys:

| Field | Type | Description |
|---|---|---|
| `id` | string | Source-assigned property ID |
| `fuente` | string | Scraper source (`argenprop`, `zonaprop`, `remax`) |
| `titulo` | string | Listing title |
| `precioUsd` | number | Price in USD |
| `moneda` | string | Currency code |
| `descripcion` | string | Full description |
| `imagenes` | array | Photo URLs |
| `url` | string | Original listing URL |
| `extraidoEn` | string | ISO timestamp of when it was scraped |
| `caracteristicas` | array | Feature tags from the listing |
| `ubicacion` | object | `{ barrio, direccion, ciudad, coordenadas }` |
| `detalles` | object | `{ ambientes, dormitorios, banos, superficieTotal, superficieCubierta }` |
| `flags` | object | Boolean enrichment: `porEscalera`, `balcon`, `enConstruccion`, `aptoCredito`, `cochera` |
