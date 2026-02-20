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
| `fuente`     | string  | `argenprop` \| `zonaprop` \| `remax` \|`meli`            |
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

**Timing:** This endpoint runs all 4 scrapers **in parallel** and then does the DB upsert. Expect **60–120 seconds** depending on how many detail pages need fetching. Always show a loading state.

---

### 2.4 `PATCH /properties/{fuente}/{id}/favourite` — Mark / unmark favourite

```
PATCH /properties/zonaprop/507260105/favourite
Content-Type: application/json

{ "favorito": true }
```

- `favorito: true` → marks the property as a favourite.
- `favorito: false` → removes it from favourites.

Returns the updated full `Property` document or `404`.

---

### 2.4 `POST /scrape` — On-demand scrape

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
  source: "argenprop" | "zonaprop" | "remax" | "meli";  // API: fuente
  scrapedAt: string;             // API: extraidoEn (ISO 8601)
  location: Location;
  details: Details;
  flags: Flags;
  description: string | null;    // API: descripcion
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
}

export interface Flags {
  porEscalera: boolean;     // no elevator — stairs only
  balcon: boolean;          // has balcony
  enConstruccion: boolean;  // under construction
  aptoCredito: boolean;     // mortgage-eligible
  cochera: boolean;         // parking / garage included
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

---

### 4.4 Flag Pills — Fixed Colours & Fixed Order

Flags are displayed **left → right** in this fixed order. A pill only renders when its value is `true`.

| # | Flag key        | Label            | Pill colour (bg)  | Icon suggestion     |
|---|-----------------|------------------|-------------------|---------------------|
| 1 | `aptoCredito`   | Apto crédito     | `#2563EB` (blue)  | 🏦 bank / credit card |
| 2 | `cochera`       | Cochera          | `#7C3AED` (violet)| 🚗 car / parking P   |
| 3 | `balcon`        | Balcón           | `#059669` (green) | 🌿 leaf / terrace    |
| 4 | `enConstruccion`| En construcción  | `#D97706` (amber) | 🔨 hard hat / hammer |
| 5 | `porEscalera`   | Sin ascensor     | `#6B7280` (gray)  | 🪜 stairs            |

Pill anatomy: `rounded-full px-2 py-0.5 text-xs font-medium text-white flex items-center gap-1`.
Container: `flex flex-wrap gap-1 min-h-[1.75rem]` (keeps card height consistent even with no flags).

---

## 5. Route: `/add` — Add Properties

### 5.1 Navigation to this route

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

### 5.2 Page Layout — Two Tabs

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

### 5.3 Tab 1 — URL (primary, default active)

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
│    remax · mercadolibre         │
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
5. On `400`: show inline error "URL no soportada. Usá links de argenprop, zonaprop, remax o mercadolibre."
6. On `500`: show inline error "No se pudo scrapear la propiedad. Verificá que la URL sea correcta."
7. After success, offer a "Ver todas las propiedades" link back to `/`.

---

### 5.4 Tab 2 — Búsqueda masiva (secondary)

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
│  ┄ LOADING STATE (see §5.5) ┄  │
│                                 │
│  ┄ RESULTS (see §5.6) ┄        │
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

### 5.5 Loading State (batch only)

The batch call takes 60–120 seconds. Show a **full-section animated loader** — not just a spinner — to communicate that real work is happening:

```
┌─────────────────────────────────┐
│                                 │
│   [animated house / search      │
│    illustration]                │
│                                 │
│   Buscando propiedades...       │  ← h2
│                                 │
│   Consultando 4 sitios en       │
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

### 5.6 Results (batch only)

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

## 6. Header

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

## 7. Footer

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

## 8. UX Details & Guidelines

- **Mobile-first:** design for 375px viewport first; scale up with media queries.
- **Loading states:** skeleton cards while fetching the list (match card dimensions).
- **Empty state:** if `results` is empty, show a friendly illustration and "No se encontraron propiedades".
- **Date formatting:** render `scrapedAt` / `extraidoEn` as a relative time (e.g. "hace 3 días") or short date ("12 feb").
- **Prices:** format `priceUsd` as `USD 150,000` and `expenses` as `$ 45.000 ARS`.
- **Null fields:** never show a label for a field whose value is null/undefined — omit the row/element entirely.
- **External links:** the "Ver →" card button must open `property.url` in a new tab (`target="_blank" rel="noopener noreferrer"`).
- **Accessibility:** all interactive elements must have accessible labels; images must have meaningful `alt` text.

---

## 9. Quick Reference — Field Mapping (API → UI)

| API field (Spanish)          | UI label / usage         |
|------------------------------|--------------------------|
| `titulo`                     | Card title               |
| `precioUsd`                  | `USD X,XXX`              |
| `expensas`                   | `$ X.XXX ARS / mes`      |
| `imagenes`                   | Carousel                 |
| `url`                        | "Ver en sitio" link      |
| `fuente`                     | Source badge             |
| `extraidoEn`                 | Scraped date             |
| `descripcion`                | 2-line truncated text    |
| `ubicacion.barrio`           | Neighbourhood            |
| `ubicacion.direccion`        | Street address           |
| `ubicacion.ciudad`           | City                     |
| `ubicacion.coordenadas`      | (Optional map pin)       |
| `detalles.ambientes`         | Rooms icon + number      |
| `detalles.dormitorios`       | Bed icon + number        |
| `detalles.banos`             | Shower/bath icon + number|
| `detalles.superficieTotal`   | Total m²                 |
| `detalles.superficieCubierta`| Covered m²               |
| `flags.aptoCredito`          | Blue pill "Apto crédito" |
| `flags.cochera`              | Violet pill "Cochera"    |
| `flags.balcon`               | Green pill "Balcón"      |
| `flags.enConstruccion`       | Amber pill "En construcción" |
| `flags.porEscalera`          | Gray pill "Sin ascensor" |
| `favorito`                   | Heart button on card (filled = favourite) |


## Color Palette
| Role           | Color (hex)  | Usage                          |
|----------------|--------------|--------------------------------|
| Primary        | `#FF385C`   | Buttons, links, accents        |
| Secondary      | `#222222`   | Text, icons, secondary elements |
| Background     | `#FFFFFF`   | Page background                |
| Card Background| `#f7f6f2`   | Property cards                 |