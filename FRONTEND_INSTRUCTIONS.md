# Frontend Instructions — Encontremos Casa

> **Author:** Santiago Pintus
> **Stack:** Your choice (React / Next.js recommended). Mobile-first. Use the libraries you already know.

---

## 1. Overview

Build a mobile-first web app that:
1. Lists all scraped properties in a responsive card grid (`/` — home route).
2. Lets users submit a property URL to scrape on-demand (`/add` — add route).
3. Provides a consistent header + footer across both routes.

---

## 2. API Reference

**Base URL:** `http://localhost:8000`

CORS is fully open in development (`allow_origins: ["*"]`).

---

### 2.1 `GET /properties` — Paginated property list

```
GET /properties
```

**Query parameters:**

| Param        | Type    | Description                                          |
|--------------|---------|------------------------------------------------------|
| `page`       | integer | Page number (default: 1)                             |
| `pageSize`   | integer | Results per page (default: 20, max: 100)             |
| `barrio`     | string  | Neighbourhood partial match (case-insensitive)       |
| `fuente`     | string  | `argenprop` \| `zonaprop` \| `remax` \| `meli` \| `properati` |
| `precio_min` | float   | Min price in USD                                     |
| `precio_max` | float   | Max price in USD                                     |
| `ambientes`  | integer | Number of rooms (exact match)                        |
| `dormitorios`| integer | Number of bedrooms (exact match)                     |
| `flags`      | string  | Repeatable. Flag names that must be `true`. See §4.3 |
| `favorito`   | boolean | `true` → only favourites · `false` → only non-favourites |

**Response `200 OK`:**

```jsonc
{
  "total": 312,
  "page": 1,
  "pageSize": 20,
  "pages": 16,
  "results": [ /* Property[] */ ]
}
```

---

### 2.2 `GET /properties/{fuente}/{id}` — Single property

```
GET /properties/argenprop/abc123
```

Returns a single `Property` document or `404`.

---

### 2.3 `POST /scrape/batch` — Full pipeline with custom search config

```
POST /scrape/batch
Content-Type: application/json
```

**Request body** (mirrors `search_filters.json`):

```jsonc
{
  "property": { "type": "departamento", "operation": "sale" },
  "location": {
    "country": "Argentina",
    "city": "Buenos Aires",
    "neighborhoods": ["Belgrano", "Núñez", "Saavedra", "Villa Urquiza"]
  },
  "price": { "currency": "USD", "min": 100000, "max": 175000 },
  "features": { "bedrooms": [3], "parking_spots_min": 1 },
  "scraping": {
    "max_pages": 10,
    "delay_between_requests_seconds": [1.0, 2.0],
    "max_retries": 3
  }
}
```

All fields under `property`, `features`, and `scraping` have defaults and can be omitted. Only `location` and `price` are required.

**Response `200 OK`:**

```jsonc
{
  "inserted":       [ /* Property[] — brand-new listings */ ],
  "updated":        [ /* Property[] — listings that already existed and were refreshed */ ],
  "total_inserted": 24,
  "total_updated":  12,
  "errors":         [ /* string[] — per-scraper error messages, if any */ ]
}
```

**Timing:** This endpoint runs all 5 scrapers **in parallel** and then does the DB upsert. Expect **60–120 seconds** depending on how many detail pages need fetching. Always show a loading state.

---

### 2.4 `PATCH /properties/{fuente}/{id}/notes` — Save user notes

```
PATCH /properties/zonaprop/507260105/notes
Content-Type: application/json
```

Saves the user's free-text comment and/or manual boolean flags. Only the fields present in the body are updated; omitted fields are left unchanged.

**Request body** (all fields optional — send only what changed):

```jsonc
{
  "comentarios": "Tiene buen precio pero el baño es chico.",
  "flagsManual": {
    "cocinaGrande":      true,
    "necesitaRemodelar": false,
    "tienePlazaCerca":   true
  }
}
```

- Send `"comentarios": ""` or `"comentarios": null` to clear the comment.
- `flagsManual` is always replaced in full — send all three booleans.

Returns the updated full `Property` document or `404`.

---

### 2.5 `PATCH /properties/{fuente}/{id}/favourite` — Mark / unmark favourite

```
PATCH /properties/zonaprop/507260105/favourite
Content-Type: application/json

{ "favorito": true }
```

- `favorito: true` → marks the property as a favourite.
- `favorito: false` → removes it from favourites.

Returns the updated full `Property` document or `404`.

---

### 2.6 `POST /scrape` — On-demand scrape

```
POST /scrape
Content-Type: application/json

{ "url": "https://www.argenprop.com/departamento-..." }
```

**Supported domains:**
- `www.argenprop.com`
- `www.zonaprop.com.ar`
- `www.remax.com.ar`
- `inmuebles.mercadolibre.com.ar`
- `departamento.mercadolibre.com.ar`
- `www.properati.com.ar`

**Responses:**
| Status | Meaning                                      |
|--------|----------------------------------------------|
| `201`  | Property scraped and stored for the first time |
| `200`  | Property already existed — refreshed in DB   |
| `400`  | Unsupported domain or malformed URL          |
| `500`  | Scraping failed or returned no data          |

Returns the full `Property` document on `200`/`201`.

---

## 3. TypeScript Types

Map the API's Spanish camelCase fields to these interfaces:

```typescript
export interface PropertyCard {
  id: string;
  title: string | null;          // API: titulo
  priceUsd: number | null;       // API: precioUsd
  expenses: number | null;       // API: expensas (monthly ARS) — may be null
  images: string[];              // API: imagenes
  url: string;                   // API: url
  source: "argenprop" | "zonaprop" | "remax" | "meli" | "properati";  // API: fuente
  scrapedAt: string;             // API: extraidoEn (ISO 8601)
  location: Location;
  details: Details;
  flags: Flags;
  flagsManual: FlagsManual;      // API: flagsManual — user-editable
  description: string | null;    // API: descripcion
  comentarios: string | null;    // API: comentarios — user-editable free text
  favorito: boolean;             // API: favorito
}

export interface Location {
  barrio: string | null;         // API: ubicacion.barrio
  direccion: string | null;      // API: ubicacion.direccion
  ciudad: string | null;         // API: ubicacion.ciudad
  coordenadas: {
    latitude: number;
    longitude: number;
  } | null;                      // API: ubicacion.coordenadas
}

export interface Details {
  ambientes: number | null;           // API: detalles.ambientes
  dormitorios: number | null;         // API: detalles.dormitorios
  banos: number | null;               // API: detalles.banos
  superficieTotal: number | null;     // API: detalles.superficieTotal (m²)
  superficieCubierta: number | null;  // API: detalles.superficieCubierta (m²)
  piso: number | null;                // API: detalles.piso — floor number (may be null)
  antiguedad: number | null;          // API: detalles.antiguedad — building age in years (may be null)
}

export interface Flags {
  porEscalera: boolean;      // no elevator — stairs only
  balcon: boolean;           // has balcony
  patio: boolean;            // has a patio
  enConstruccion: boolean;   // under construction
  aptoCredito: boolean;      // mortgage-eligible
  cochera: boolean;          // parking / garage included
  cocheraOpcional: boolean;  // parking available at extra cost (cochera is false when this is true)
  reservado: boolean;        // property is reserved / under offer
}

/** Manually toggled by the user in the table view. Never overwritten by scraping. */
export interface FlagsManual {
  cocinaGrande: boolean;         // kitchen is large / open plan
  necesitaRemodelar: boolean;    // property needs renovation
  tienePlazaCerca: boolean;      // near a park or plaza
}
```

---

## 4. Route: `/` — Property Listing

### 4.1 Layout

```
┌─────────────────────────────────┐
│           HEADER                │
├─────────────────────────────────┤
│  (optional filter bar)          │
├─────────────────────────────────┤
│  PROPERTY GRID (cards)          │
│                                 │
│  [card] [card]                  │
│  [card] [card]                  │
│  [card] [card]                  │
│         ...                     │
├─────────────────────────────────┤
│  pagination controls            │
├─────────────────────────────────┤
│           FOOTER                │
└─────────────────────────────────┘
         [FAB: ➕🏠]   ← bottom-right floating button
```

### 4.2 Responsive Grid

| Breakpoint | Columns |
|------------|---------|
| `< 640px`  | 1       |
| `640–1023px` | 2     |
| `≥ 1024px` | 3       |
| `≥ 1280px` | 4       |

Use `gap-4` (16px) between cards. Fetch page 1 on mount; implement infinite scroll or numbered pagination.

**Favourites filter tab:** above the grid, add a simple toggle or tab bar:

```
[ Todas ]  [ ❤ Favoritas ]
```

- "Todas" → `GET /properties` (no `favorito` param).
- "Favoritas" → `GET /properties?favorito=true`.
- Highlight the active tab with the primary colour.

---

### 4.3 Property Card

Each card must display **all** fields from `PropertyCard`. Layout (top → bottom):

```
┌─────────────────────────────┐
│  ← image carousel →     [♡] │  ← carousel + favourite heart (top-right overlay)
├─────────────────────────────┤
│  [SOURCE badge]  [DATE]     │  ← fuente pill + scraped date (relative or short)
│  TITLE (1 line, truncated)  │
│  $ USD price                │
│  Expensas: ARS expenses     │  ← hide row if null
├─────────────────────────────┤
│  📍 barrio · ciudad         │
│  🏠 ambientes  🛏 dormi  🚿 baños │
│  📐 X m² tot · Y m² cub    │  ← hide if null
├─────────────────────────────┤
│  [FLAG PILLS — see §4.4]    │
├─────────────────────────────┤
│  description (2 lines max,  │
│  CSS line-clamp: 2)         │
│                  [Ver →]    │  ← external link to url
└─────────────────────────────┘
```

**Image carousel:**
- Show dot indicators at the bottom of the image area.
- If `images` is empty, show a placeholder illustration (house outline).
- Lazy-load images.
- Swipeable on mobile.

**Favourite heart button (overlaid top-right of the carousel):**
- Icon: heart outline (unfavourited) / heart filled (favourited).
- Colour when active: `#FF385C` (primary red). Outline when inactive: white with a subtle drop shadow so it's visible over any image.
- On click: call `PATCH /properties/{fuente}/{id}/favourite` with `{ "favorito": !current }`.
- Optimistic update: toggle the heart immediately, revert on API error and show a brief error toast.
- The button must sit on top of the carousel image (`absolute top-2 right-2 z-10`).

**Source badge colours:**

| fuente      | Background     | Text  |
|-------------|----------------|-------|
| argenprop   | `#E63946`      | white |
| zonaprop    | `#2563EB`      | white |
| remax       | `#DC2626`      | white |
| meli        | `#F59E0B`      | white |
| properati   | `#EE4252`      | white |

---

### 4.4 Flag Pills — Fixed Colours & Fixed Order

Flags are displayed **left → right** in this fixed order. A pill only renders when its value is `true`.

| #  | Flag key          | Label              | Pill colour (bg)   | Icon suggestion       |
|----|-------------------|--------------------|--------------------|-----------------------|
| 1  | `aptoCredito`     | Apto crédito       | `#2563EB` (blue)   | 🏦 bank / credit card |
| 2  | `cochera`         | Cochera            | `#7C3AED` (violet) | 🚗 car / parking P    |
| 3  | `cocheraOpcional` | Cochera opcional   | `#8B5CF6` (purple) | 🚗 car (lighter)      |
| 4  | `balcon`          | Balcón             | `#059669` (green)  | 🌿 leaf / terrace     |
| 5  | `patio`           | Patio              | `#10B981` (emerald)| 🌱 plant / outdoor    |
| 6  | `enConstruccion`  | En construcción    | `#D97706` (amber)  | 🔨 hard hat / hammer  |
| 7  | `porEscalera`     | Sin ascensor       | `#6B7280` (gray)   | 🪜 stairs             |
| 8  | `reservado`       | Reservado          | `#DC2626` (red)    | 🔒 lock               |

Pill anatomy: `rounded-full px-2 py-0.5 text-xs font-medium text-white flex items-center gap-1`.
Container: `flex flex-wrap gap-1 min-h-[1.75rem]` (keeps card height consistent even with no flags).

---

## 5. Table View — `/` with view toggle

### 5.1 View Toggle

On the home listing page add a **grid / table toggle** above the results:

```
[ Todas ]  [ ❤ Favoritas ]          [⊞ Grid]  [☰ Tabla]
```

- The toggle is a two-button segmented control, right-aligned.
- Default: Grid view.
- Table view replaces the card grid with the spreadsheet table described below.
- Persist the chosen view in `localStorage` so it survives page refreshes.

---

### 5.2 Table Layout

A horizontally scrollable, dense table. Each row is one property.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ Link │ Lugar           │ Barrio  │ USD     │ Expensas│ USD/m² │ m²cub│ m²tot │ … │
├──────┼─────────────────┼─────────┼─────────┼─────────┼────────┼──────┼───────┼───┤
│  ↗   │ Av Cabildo 1234 │Belgrano │ 145,000 │ $42.000 │  2,050 │  70  │  80   │ … │
│  ↗   │ …               │ Núñez   │ 128,000 │    —    │  1,956 │  65  │  70   │ … │
└──────┴─────────────────┴─────────┴─────────┴─────────┴────────┴──────┴───────┴───┘
```

**Column definitions (fixed order):**

| # | Header           | Source field                          | Editable | Notes                                     |
|---|------------------|---------------------------------------|----------|-------------------------------------------|
| 1 | Link             | `url`                                 | —        | Icon button `↗` opens in new tab         |
| 2 | Lugar            | `ubicacion.direccion`                 | —        | Street address; show `—` if null          |
| 3 | Barrio           | `ubicacion.barrio`                    | —        |                                           |
| 4 | Precio (USD)     | `precioUsd`                           | —        | Format: `145,000`                         |
| 5 | Expensas (ARS)   | `expensas`                            | —        | Format: `$42.000`; `—` if null            |
| 6 | USD/m²           | computed                              | —        | `precioUsd / detalles.superficieCubierta`; `—` if either null |
| 7 | m² cub           | `detalles.superficieCubierta`         | —        |                                           |
| 8 | m² tot           | `detalles.superficieTotal`            | —        |                                           |
| 9 | Dorm             | `detalles.dormitorios`                | —        |                                           |
|10 | Baños            | `detalles.banos`                      | —        |                                           |
|11 | Antigüedad       | `detalles.antiguedad`                 | —        | Years; `—` if null                        |
|12 | Piso             | `detalles.piso`                       | —        | Floor number; `—` if null                 |
|13 | 🚗               | `flags.cochera`                       | —        | ✓ / — (checkmark or dash)                |
|14 | Balcón           | `flags.balcon`                        | —        | ✓ / —                                    |
|15 | Patio            | `flags.patio`                         | —        | ✓ / —                                    |
|16 | Apto Crédito     | `flags.aptoCredito`                   | —        | ✓ / —                                    |
|17 | Cocina Grande    | `flagsManual.cocinaGrande`            | **✓**    | Checkbox                                  |
|18 | Necesita Remodelar | `flagsManual.necesitaRemodelar`     | **✓**    | Checkbox                                  |
|19 | Tiene Plaza Cerca | `flagsManual.tienePlazaCerca`        | **✓**    | Checkbox                                  |
|20 | Comentarios      | `comentarios`                         | **✓**    | Inline text input (see §5.3)              |

---

### 5.3 Editable Columns

Columns 17–20 are **user-editable**. Give them a distinct visual treatment to set them apart from the scraped data:

- Background: slightly tinted (`bg-blue-50` or a subtle warm tint) on the header and cells.
- Header label: add a small pencil icon `✏` or use italic.
- On hover: show a visible border / outline on the cell.

**Checkboxes (columns 17–19):**
- Render a standard checkbox (`<input type="checkbox">`).
- On change: immediately call `PATCH /properties/{fuente}/{id}/notes` with the full `flagsManual` object (all three booleans, reflecting the new state).
- Optimistic update: update local state immediately, revert on API error.

**Comments (column 20):**
- Render as a compact single-line `<input type="text">` (min-width ~200px).
- On blur (or Enter): call `PATCH /properties/{fuente}/{id}/notes` with `{ "comentarios": "<value>" }`.
- Debounce is optional; a blur-save is simpler and sufficient.
- Show a subtle "saved ✓" indicator that fades out after 1.5s.

---

### 5.4 Table Behaviour

- **Sorting:** clicking any column header sorts by that column. Click again to reverse. Show ▲/▼ indicator.
- **Sticky columns:** columns 1–3 (Link, Lugar, Barrio) should be sticky-left so they stay visible while scrolling horizontally.
- **Row height:** compact (`h-9` / 36px). Don't wrap text — truncate with ellipsis.
- **Zebra striping:** alternate row background for readability (`bg-white` / `bg-gray-50`).
- **Pagination:** reuse the same `GET /properties` pagination as the grid view. Same filters apply.
- **Null display:** always render `—` (em dash) for any null/undefined numeric or text value.

---

## 6. Route: `/add` — Add Properties

### 6.1 Navigation to this route

In the **main listing page** (and anywhere the FAB is shown), render a floating action button at `bottom-right`:

```
┌──────────────────────┐
│  [🏠] [＋]           │  ← pill-shaped button, rounded-full
└──────────────────────┘
```

- Left side: house icon (static, decorative).
- Right side: `+` (plus) icon, clickable.
- Clicking it navigates to `/add`.
- Style: `fixed bottom-6 right-6 z-50 flex items-center gap-2 bg-primary text-white rounded-full shadow-xl px-4 py-3`.

### 6.2 Page Layout — Two Tabs

The `/add` page has two tabs. The first (URL) is the primary option, the second (Búsqueda) is secondary.

```
┌─────────────────────────────────┐
│           HEADER                │
├─────────────────────────────────┤
│                                 │
│    Agregar propiedad            │  ← h1
│                                 │
│  [ URL ]  [ Búsqueda masiva ]   │  ← tab bar (URL is default/active)
│  ─────────────────────────────  │
│                                 │
│  ┄ TAB CONTENT (see below) ┄   │
│                                 │
├─────────────────────────────────┤
│           FOOTER                │
└─────────────────────────────────┘
```

---

### 6.3 Tab 1 — URL (primary, default active)

**Endpoint:** `POST /scrape`

```
│    Pegá la URL de la propiedad  │  ← subtitle
│                                 │
│  ┌─────────────────────────┐    │
│  │ https://www.zonapr...   │    │  ← text input, full width
│  └─────────────────────────┘    │
│                                 │
│    Sitios soportados:           │
│    argenprop · zonaprop ·       │
│    remax · mercadolibre ·       │
│    properati                    │
│                                 │
│  [    Agregar propiedad    ]    │  ← primary button
│                                 │
│  (loading spinner while POST)   │
│  (result card or error msg)     │
```

**Behaviour:**
1. User pastes URL → clicks button.
2. Show loading spinner; disable button.
3. `POST /scrape` with `{ "url": "<input value>" }`.
4. On `200` or `201`: display a preview of the scraped property (reuse `PropertyCard`). Show a toast indicating whether it was new (`201`) or refreshed (`200`).
5. On `400`: show inline error "URL no soportada. Usá links de argenprop, zonaprop, remax, mercadolibre o properati."
6. On `500`: show inline error "No se pudo scrapear la propiedad. Verificá que la URL sea correcta."
7. After success, offer a "Ver todas las propiedades" link back to `/`.

---

### 6.4 Tab 2 — Búsqueda masiva (secondary)

**Endpoint:** `POST /scrape/batch`

This tab lets the user run all scrapers simultaneously with custom filters and see the results split into new vs refreshed properties.

```
│    Buscar propiedades           │  ← subtitle
│                                 │
│  Barrios (separados por coma)   │  ← label
│  ┌─────────────────────────┐    │
│  │ Belgrano, Núñez, ...    │    │
│  └─────────────────────────┘    │
│                                 │
│  Precio mínimo (USD)            │
│  ┌────────┐                     │
│  │ 100000 │                     │
│  └────────┘                     │
│                                 │
│  Precio máximo (USD)            │
│  ┌────────┐                     │
│  │ 175000 │                     │
│  └────────┘                     │
│                                 │
│  [   Iniciar búsqueda   ]       │  ← primary button
│                                 │
│  ┄ LOADING STATE (see §6.5) ┄  │
│                                 │
│  ┄ RESULTS (see §6.6) ┄        │
```

**Form fields to collect:**

| Field | Type | Default | Maps to |
|-------|------|---------|---------|
| Barrios | comma-separated text → `string[]` | `["Belgrano","Núñez","Saavedra","Villa Urquiza"]` | `location.neighborhoods` |
| Precio mínimo | number | `100000` | `price.min` |
| Precio máximo | number | `175000` | `price.max` |

All other config values (`property`, `features`, `scraping`) are sent with their defaults — do not expose them in the UI.

**Request body to send:**
```jsonc
{
  "location": {
    "city": "Buenos Aires",
    "neighborhoods": ["Belgrano", "Núñez"]   // from form
  },
  "price": {
    "currency": "USD",
    "min": 100000,   // from form
    "max": 175000    // from form
  }
}
```

---

### 6.5 Loading State (batch only)

The batch call takes 60–120 seconds. Show a **full-section animated loader** — not just a spinner — to communicate that real work is happening:

```
┌─────────────────────────────────┐
│                                 │
│   [animated house / search      │
│    illustration]                │
│                                 │
│   Buscando propiedades...       │  ← h2
│                                 │
│   Consultando 5 sitios en       │
│   simultáneo. Esto puede        │
│   tardar hasta 2 minutos.       │
│                                 │
│   [=====>         ] 30s...      │  ← optional fake progress bar
│                                 │
│   [  Cancelar  ]                │  ← abort button (just navigates away)
│                                 │
└─────────────────────────────────┘
```

- Disable all form inputs while loading.
- The progress bar can be fake/animated (e.g., fills to 90% over 90s, then waits).
- On error: replace loader with an error message and a "Reintentar" button.

---

### 6.6 Results (batch only)

After the `POST /scrape/batch` response, show two sections:

```
┌─────────────────────────────────┐
│  ✨ 24 propiedades nuevas        │  ← section heading (green accent)
├─────────────────────────────────┤
│  [card] [card]                  │
│  [card] [card]  ...             │
├─────────────────────────────────┤
│  🔄 12 propiedades actualizadas  │  ← section heading (blue accent)
├─────────────────────────────────┤
│  [card] [card]                  │
│  [card] [card]  ...             │
└─────────────────────────────────┘
```

- Both sections reuse the `PropertyCard` component.
- If `total_inserted === 0`, hide the "nuevas" section entirely (don't show an empty list).
- If `total_updated === 0`, hide the "actualizadas" section.
- If both are 0 but `errors` is non-empty, show: "No se encontraron propiedades. Algunos sitios fallaron: [errors]".
- If `errors` is non-empty but there are results, show a small non-blocking warning banner above the results.
- After results render, show a "Ver todas las propiedades" link back to `/`.

---

## 7. Header

Appears on both routes. Sticky (`position: sticky; top: 0`).

```
┌─────────────────────────────────────────┐
│  🏠 Encontremos Casa   [Propiedades] [Agregar] │
└─────────────────────────────────────────┘
```

- **Logo / Brand:** "Encontremos Casa" with a house icon. Links to `/`.
- **Nav links:**
  - `Propiedades` → `/`
  - `Agregar` → `/add`
- On mobile: collapse nav into a hamburger menu or keep both links as icon + label.

---

## 8. Footer

Appears on both routes. Same nav links as the header.

```
┌──────────────────────────────────────────────┐
│  [Propiedades]   [Agregar]                   │
│                                              │
│         © 2025 Santiago Pintus              │
└──────────────────────────────────────────────┘
```

- Nav links: same as header (`Propiedades` → `/`, `Agregar` → `/scrape`).
- Author line: `© 2025 Santiago Pintus`.
- Keep it simple, no extra content.

---

## 9. UX Details & Guidelines

- **Mobile-first:** design for 375px viewport first; scale up with media queries.
- **Loading states:** skeleton cards while fetching the list (match card dimensions).
- **Empty state:** if `results` is empty, show a friendly illustration and "No se encontraron propiedades".
- **Date formatting:** render `scrapedAt` / `extraidoEn` as a relative time (e.g. "hace 3 días") or short date ("12 feb").
- **Prices:** format `priceUsd` as `USD 150,000` and `expenses` as `$ 45.000 ARS`.
- **Null fields:** never show a label for a field whose value is null/undefined — omit the row/element entirely.
- **External links:** the "Ver →" card button must open `property.url` in a new tab (`target="_blank" rel="noopener noreferrer"`).
- **Accessibility:** all interactive elements must have accessible labels; images must have meaningful `alt` text.

---

## 10. Quick Reference — Field Mapping (API → UI)

| API field (Spanish)                  | UI label / usage                              |
|--------------------------------------|-----------------------------------------------|
| `titulo`                             | Card title                                    |
| `precioUsd`                          | `USD X,XXX`                                   |
| `expensas`                           | `$ X.XXX ARS / mes`                           |
| `imagenes`                           | Carousel                                      |
| `url`                                | "Ver en sitio" link                           |
| `fuente`                             | Source badge                                  |
| `extraidoEn`                         | Scraped date                                  |
| `descripcion`                        | 2-line truncated text                         |
| `ubicacion.barrio`                   | Neighbourhood                                 |
| `ubicacion.direccion`                | Street address / table "Lugar" column         |
| `ubicacion.ciudad`                   | City                                          |
| `ubicacion.coordenadas`              | (Optional map pin)                            |
| `detalles.ambientes`                 | Rooms icon + number                           |
| `detalles.dormitorios`               | Bed icon + number / table "Dorm" column       |
| `detalles.banos`                     | Shower/bath icon + number                     |
| `detalles.superficieTotal`           | Total m² / table "m² tot" column             |
| `detalles.superficieCubierta`        | Covered m² / table "m² cub" column; used for USD/m² |
| `detalles.piso`                      | Table "Piso" column (floor number)            |
| `detalles.antiguedad`                | Table "Antigüedad" column (years)             |
| `flags.aptoCredito`                  | Blue pill "Apto crédito" / table col          |
| `flags.cochera`                      | Violet pill "Cochera" / table 🚗 col          |
| `flags.cocheraOpcional`              | Purple pill "Cochera opcional"                |
| `flags.balcon`                       | Green pill "Balcón" / table col               |
| `flags.patio`                        | Emerald pill "Patio" / table col              |
| `flags.enConstruccion`               | Amber pill "En construcción"                  |
| `flags.porEscalera`                  | Gray pill "Sin ascensor"                      |
| `flags.reservado`                    | Red pill "Reservado"                          |
| `flagsManual.cocinaGrande`           | Table "Cocina Grande" checkbox (user editable)|
| `flagsManual.necesitaRemodelar`      | Table "Necesita Remodelar" checkbox (user editable) |
| `flagsManual.tienePlazaCerca`        | Table "Tiene Plaza Cerca" checkbox (user editable) |
| `comentarios`                        | Table "Comentarios" text input (user editable)|
| `favorito`                           | Heart button on card (filled = favourite)     |


## Color Palette
| Role           | Color (hex)  | Usage                          |
|----------------|--------------|--------------------------------|
| Primary        | `#FF385C`   | Buttons, links, accents        |
| Secondary      | `#222222`   | Text, icons, secondary elements |
| Background     | `#FFFFFF`   | Page background                |
| Card Background| `#f7f6f2`   | Property cards                 |