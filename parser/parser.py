"""
parser.py — Merges, remaps, and enriches scraper output files.

Input:  output/*.json  (produced by any scraper in this project)
Output: output/parsed_listings_YYYY-MM-DD_HH-MM-SS.json

Key transformations
  - Remap snake_case English keys → camelCase Spanish
  - Add a `flags` object with boolean enrichment fields
  - Deduplicate by (id, fuente) — first occurrence wins
"""

import glob
import json
import os
import re
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_GLOB = os.path.join(ROOT_DIR, "output", "*.json")
OUTPUT_DIR = os.path.join(ROOT_DIR, "parsed")


# ---------------------------------------------------------------------------
# Key mapping helpers
# ---------------------------------------------------------------------------

_LOCATION_MAP = {
    "neighborhood": "barrio",
    "street_address": "direccion",
    "city": "ciudad",
    "coordinates": "coordenadas",
}

_DETAILS_MAP = {
    "rooms": "ambientes",
    "bedrooms": "dormitorios",
    "bathrooms": "banos",
    "surface_total_m2": "superficieTotal",
    "surface_covered_m2": "superficieCubierta",
    "floor": "piso",
    "age_years": "antiguedad",
}


def _remap_location(raw: dict | None) -> dict:
    if not raw:
        return {}
    return {_LOCATION_MAP.get(k, k): v for k, v in raw.items()}


def _remap_details(raw: dict | None) -> dict:
    if not raw:
        return {}
    return {_DETAILS_MAP.get(k, k): v for k, v in raw.items()}


def transform_listing(raw: dict) -> dict:
    """Remap one raw scraper listing to the camelCase Spanish schema."""
    return {
        "id": raw.get("id"),
        "titulo": raw.get("title"),
        "precioUsd": raw.get("price_usd"),
        "moneda": raw.get("price_currency"),
        "descripcion": raw.get("description"),
        "imagenes": raw.get("images", []),
        "url": raw.get("url"),
        "fuente": raw.get("source"),
        "extraidoEn": raw.get("scraped_at"),
        "caracteristicas": raw.get("features", []),
        "ubicacion": _remap_location(raw.get("location")),
        "detalles": _remap_details(raw.get("property_details")),
    }


# ---------------------------------------------------------------------------
# Boolean flag computation
# ---------------------------------------------------------------------------

def _build_search_text(listing: dict) -> str:
    """
    Concatenate all text fields of a transformed listing into one lowercase
    string for flag matching.
    """
    parts: list[str] = []

    for key in ("titulo", "descripcion"):
        val = listing.get(key)
        if isinstance(val, str):
            parts.append(val)

    caracteristicas = listing.get("caracteristicas")
    if isinstance(caracteristicas, list):
        parts.extend(str(c) for c in caracteristicas if c)
    elif isinstance(caracteristicas, str):
        parts.append(caracteristicas)

    # Include location strings for completeness
    ubicacion = listing.get("ubicacion", {})
    for key in ("barrio", "direccion", "ciudad"):
        val = ubicacion.get(key)
        if isinstance(val, str):
            parts.append(val)

    return " ".join(parts).lower()


def compute_flags(listing: dict) -> dict:
    text = _build_search_text(listing)
    cochera_opcional = bool(re.search(r"(cochera|guardacoche)\s+(es\s+)?opcional", text)) or bool(re.search(r"alquilar (cochera|guardacoche)\s", text))
    no_credito = bool(re.search(r"no\s+(es\s+)?apto\s+(al\s+|para\s+)?cr[eé]dito|sin\s+cr[eé]dito", text))

    return {
        "porEscalera": "por escalera" in text,
        "balcon": bool(re.search(r"balc[oó]n", text)) and not re.search(r"sin balc[oó]n", text) and not re.search(r"balc[oó]n franc[eé]s", text),
        "enConstruccion": (
            "de pozo" in text
            or "emprendimiento" in text
            or "a construir" in text
        ),
        "aptoCredito": not no_credito and bool(re.search(r"apto\s+cr[eé]dito|cr[eé]dito", text)),
        "cocheraOpcional": cochera_opcional,
        "cochera": not cochera_opcional and (("cochera" in text or "coche" in text) and "sin cochera" not in text and "sin guardacoche" not in text and "no posee cochera" not in text),
        "reservado": bool(re.search(r"reservad[ao]", text)) and not re.search(r"derechos\s+reservados", text),
        "patio": bool(re.search(r"\bpatio\b", text)) and not re.search(r"sin patio", text),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    input_files = sorted(glob.glob(INPUT_GLOB))

    # Exclude previously parsed output files so we don't re-parse them
    input_files = [f for f in input_files if not os.path.basename(f).startswith("parsed_")]

    if not input_files:
        print("No input files found. Make sure the scrapers have run first.")
        return

    seen: set[tuple] = set()
    listings: list[dict] = []
    sources: set[str] = set()

    for filepath in input_files:
        print(f"Reading {os.path.basename(filepath)} …")
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)

        raw_listings = data.get("listings", [])
        for raw in raw_listings:
            dedup_key = (raw.get("id"), raw.get("source"))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            listing = transform_listing(raw)
            listing["flags"] = compute_flags(listing)

            listings.append(listing)
            if listing.get("fuente"):
                sources.add(listing["fuente"])

    now = datetime.now(tz=timezone.utc)
    output = {
        "meta": {
            "totalListings": len(listings),
            "fuentes": sorted(sources),
            "generadoEn": now.isoformat(),
        },
        "listings": listings,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    output_path = os.path.join(OUTPUT_DIR, f"parsed_listings_{timestamp}.json")

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    print(f"\nDone. {len(listings)} listings written to {output_path}")
    print(f"Sources: {', '.join(sorted(sources))}")


if __name__ == "__main__":
    main()
