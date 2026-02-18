# db/

Database scripts for the `properties` collection in MongoDB Atlas.

| Script | Purpose |
|---|---|
| `upload.py` | Insert / update parsed listings into MongoDB |
| `read.py` | Inspect the collection: summary stats and sample documents |
| `remove.py` | Delete documents matching filter conditions |

> **Note:** There is intentionally no `update.py`. Field values are kept current by re-running the full scrape → parse → upload pipeline, which upserts each document in place.

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
Create a `.env` file in the project root (already gitignored):
```
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
```

---

## Typical workflow

```bash
# 1. Scrape
python scrapers/argenprop_scraper.py
python scrapers/zonaprop_scraper.py
python scrapers/remax_scraper.py

# 2. Parse  (merges output/ → parsed/)
python parser/parser.py

# 3. Upload  (upserts parsed/ → MongoDB)
python db/upload.py

# 4. Verify
python db/read.py

# 5. Clean up bad data if needed
python db/remove.py --no-coords
```

---

## upload.py

Reads every `*.json` from the `parsed/` directory and upserts each listing into the `properties` collection.

### How deduplication works
Each listing is matched by the compound key `(id, fuente)`:
- Document doesn't exist → **inserted**
- Document already exists → **updated in place** (`$set`)
- Running the script twice on the same file is completely safe

A unique index on `(id, fuente)` is created on the first run and is idempotent on subsequent runs.

### Usage
```bash
# Upload all files in parsed/
python db/upload.py

# Upload a specific file
python db/upload.py parsed/parsed_listings_2026-02-15_19-37-29.json
```

### Output
```
21:09:36 [INFO] Connected to MongoDB.
21:09:36 [INFO] Uploading parsed_listings_2026-02-16_00-08-26.json
21:09:37 [INFO]   → 145 inserted, 0 updated
21:09:37 [INFO] === Done. 145 inserted, 0 updated across 1 file(s) ===
```

---

## read.py

Inspects the collection. Prints a summary (total count, per-source breakdown, price range, flag counts) followed by sample documents from each source.

### Usage
```bash
# Summary + 3 samples per source
python db/read.py

# Print full JSON for each sample
python db/read.py --full

# Look up one specific property
python db/read.py --id 18059878 --fuente argenprop
```

### Sample output
```
------------------------------------------------------------
  Collection : earthbnb.properties
  Total docs : 140
  argenprop   : 17 properties
  remax       : 12 properties
  zonaprop    : 111 properties
  Price range : USD 110,000 – 180,000 (avg 160,053)

  Flags:
    porEscalera     : 6
    balcon          : 99
    enConstruccion  : 4
    aptoCredito     : 42
    cochera         : 130
------------------------------------------------------------
```

---

## remove.py

Deletes documents that match one or more filter conditions. Always shows a preview of matching documents and asks for confirmation before deleting.

### Conditions

| Flag | What it removes |
|---|---|
| `--no-coords` | Listings where `coordenadas` is null |
| `--fuente SOURCE` | All listings from a given source (`argenprop` / `zonaprop` / `remax`) |
| `--barrio NAME` | Listings with a matching barrio (partial, case-insensitive) |
| `--price-below USD` | Listings with `precioUsd < USD` |
| `--price-above USD` | Listings with `precioUsd > USD` |
| `--flag FLAG` | Listings where a boolean flag is `true` |
| `--yes` / `-y` | Skip the confirmation prompt (useful for scripting) |

Multiple conditions are combined with **AND** logic.

### Usage
```bash
# Remove listings with no coordinates
python db/remove.py --no-coords

# Remove all ZonaProp listings
python db/remove.py --fuente zonaprop

# Remove listings outside the target price range
python db/remove.py --price-below 150000
python db/remove.py --price-above 180000

# Remove a specific neighbourhood
python db/remove.py --barrio "Belgrano C"

# Combine conditions (AND)
python db/remove.py --fuente zonaprop --no-coords

# Skip confirmation (for scripting / CI)
python db/remove.py --no-coords --yes
```

### Sample output
```
  Matched 5 document(s). Showing up to 5:

  [zonaprop  ] id=57692306  barrio=Belgrano C      precio=159000.0  coords=(None, None)
  [zonaprop  ] id=57072235  barrio=Villa Urquiza   precio=160000.0  coords=(None, None)
  ...

  Delete 5 document(s)? [y/N] y
21:26:49 [INFO] Deleted 5 document(s).
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
| `ubicacion` | object | `{ barrio, direccion, ciudad, coordenadas: { latitude, longitude } }` |
| `detalles` | object | `{ ambientes, dormitorios, banos, superficieTotal, superficieCubierta }` |
| `flags` | object | `{ porEscalera, balcon, enConstruccion, aptoCredito, cochera }` |
