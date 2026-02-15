# Parser Requirements

## 1. Inputs

- **Directory glob:** `output/*.json` (all scraper result files)
- Each file has shape `{ metadata: {...}, listings: [...] }`
- Supported sources: `argenprop`, `zonaprop` (future sources follow the same schema)

---

## 2. Key Mapping (snake_case English → camelCase Spanish)

### Top-level listing fields

| Input key        | Output key      |
|------------------|-----------------|
| id               | id              |
| title            | titulo          |
| price_usd        | precioUsd       |
| price_currency   | moneda          |
| description      | descripcion     |
| images           | imagenes        |
| url              | url             |
| source           | fuente          |
| scraped_at       | extraidoEn      |
| features         | caracteristicas |

### Nested `location` → `ubicacion`

| Input key        | Output key  |
|------------------|-------------|
| neighborhood     | barrio      |
| street_address   | direccion   |
| city             | ciudad      |
| coordinates      | coordenadas |

### Nested `property_details` → `detalles`

| Input key           | Output key       |
|---------------------|------------------|
| rooms               | ambientes        |
| bedrooms            | dormitorios      |
| bathrooms           | banos            |
| surface_total_m2    | superficieTotal  |
| surface_covered_m2  | superficieCubierta |

---

## 3. Boolean Flags

Each listing gets a `flags` object with the following keys. Search scope is a
case-insensitive match across `descripcion`, `titulo`, `caracteristicas` (array),
and any other string fields of the listing.

| Flag key       | Set to `true` when…                                                | Default |
|----------------|--------------------------------------------------------------------|---------|
| porEscalera    | text contains `"por escalera"` AND NOT `"ascensor"`                | false   |
| balcon         | text contains `"balcon"` OR `"balcón"`                             | false   |
| enConstruccion | text contains `"de pozo"` OR `"emprendimiento"` OR `"a construir"` | false   |
| aptoCredito    | text contains `"apto crédito"` OR `"apto credito"` OR `"crédito"`  | false   |
| cochera        | text contains `"cochera"` OR `"coche"`                             | false   |

All flags default to `false`; set to `true` only when a keyword match is found.

---

## 4. Output Format

- **File:** `output/parsed_listings_YYYY-MM-DD_HH-MM-SS.json`
- **Shape:**

```json
{
  "meta": {
    "totalListings": 123,
    "fuentes": ["argenprop", "zonaprop"],
    "generadoEn": "2026-02-14T12:00:00+00:00"
  },
  "listings": []
}
```

- **Deduplication:** listings with the same `id` + `fuente` combination are kept
  only once (first occurrence wins, same strategy as scrapers).

---

## 5. Out of Scope

- Any HTTP requests or re-scraping
- Modifying scraper output files
- Database insertion (parser outputs JSON only; DB layer is a separate concern)

---

## 6. Verification

After implementation:

1. Run `python parser/parser.py` against existing `output/` files.
2. Confirm the output file exists in `output/`.
3. Spot-check 3–5 listings manually: verify camelCase keys, Spanish names, and correct flag values.
4. Confirm a listing with `"cochera"` in the description has `flags.cochera = true`.
5. Confirm a listing with `"por escalera"` has `flags.porEscalera = true`.
