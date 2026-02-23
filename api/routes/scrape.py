"""
api/routes/scrape.py — Scrape endpoints.

POST /scrape
  201 → Property document (newly inserted)
  200 → Property document (already existed, updated in place)
  400 → URL domain not in allowed list
  422 → URL missing or malformed (handled automatically by FastAPI/Pydantic)
  500 → Scraping failed or returned no data

POST /scrape/batch
  200 → { inserted: Property[], updated: Property[], total_inserted, total_updated, errors }
      Runs all scrapers in parallel with the given search config,
      parses the results, upserts to MongoDB, and returns new vs refreshed properties.
"""

import asyncio
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel, Field
from pymongo import ReturnDocument, UpdateOne

from parser.parser import compute_flags, transform_listing
from scrapers.single import VALID_DOMAINS, scrape_url
import scrapers.argenprop_scraper as _ap
import scrapers.zonaprop_scraper  as _zp
import scrapers.remax_scraper     as _rm
import scrapers.meli_scraper      as _ml

router = APIRouter(prefix="/scrape", tags=["scrape"])

# Fields set by the user — must never be overwritten when re-scraping a listing
_USER_FIELDS = {"favorito", "visitado", "oculto"}
# Defaults applied only when a property is first inserted
_USER_DEFAULTS = {"favorito": False, "visitado": False, "oculto": False}


# ── SHARED DEPENDENCY ─────────────────────────────────────────────────────────

def col(request: Request) -> AsyncIOMotorCollection:
    return request.app.state.col


# ══════════════════════════════════════════════════════════════════════════════
# POST /scrape  — single URL
# ══════════════════════════════════════════════════════════════════════════════

class ScrapeRequest(BaseModel):
    url: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def scrape_property(
    body: ScrapeRequest,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    # 1. Validate URL scheme and domain — 400 if not supported
    parsed_url = urlparse(body.url)
    if parsed_url.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid URL scheme {parsed_url.scheme!r}. Only http/https are allowed.",
        )
    domain = parsed_url.netloc
    if domain not in VALID_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"Domain not supported: {domain!r}. "
                   f"Allowed: {', '.join(sorted(VALID_DOMAINS))}",
        )

    # 2. Scrape in a thread pool (scraping is synchronous/blocking)
    try:
        raw = await asyncio.to_thread(scrape_url, body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Scraping error: {exc}")

    if raw is None:
        raise HTTPException(
            status_code=500,
            detail="Scraper returned no data for the given URL.",
        )

    # 3. Transform + compute flags
    listing = transform_listing(raw)
    listing["flags"] = compute_flags(listing)

    if not listing.get("id") or not listing.get("fuente"):
        raise HTTPException(
            status_code=500,
            detail="Scraper could not determine property id or source.",
        )

    # 4. Check existence (for response status)
    existing = await collection.find_one(
        {"id": listing["id"], "fuente": listing["fuente"]}
    )

    # 5. Upsert and return — strip user-managed fields from $set so re-scraping
    #    never overwrites favorito / visitado / oculto; set defaults on insert only.
    scrape_data = {k: v for k, v in listing.items() if k not in _USER_FIELDS}
    result = await collection.find_one_and_update(
        {"id": listing["id"], "fuente": listing["fuente"]},
        {"$set": scrape_data, "$setOnInsert": _USER_DEFAULTS},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    result.pop("_id", None)

    http_status = status.HTTP_200_OK if existing else status.HTTP_201_CREATED
    return JSONResponse(content=result, status_code=http_status)


# ══════════════════════════════════════════════════════════════════════════════
# POST /scrape/batch  — full pipeline with custom search config
# ══════════════════════════════════════════════════════════════════════════════

# ── Request models ────────────────────────────────────────────────────────────

class _PropertyConfig(BaseModel):
    type: str = "departamento"
    operation: str = "sale"


class _LocationConfig(BaseModel):
    country: str = "Argentina"
    city: str = "Buenos Aires"
    neighborhoods: list[str]


class _PriceConfig(BaseModel):
    currency: str = "USD"
    min: float
    max: float


class _FeaturesConfig(BaseModel):
    bedrooms: list[int] = []
    dormitorios_min: int = 0
    parking_spots_min: int = 0
    superficie_cubierta_min: float = 0


class _ScrapingConfig(BaseModel):
    max_pages: int = 10
    delay_between_requests_seconds: list[float] = [1.0, 2.0]
    max_retries: int = 3


class BatchScrapeRequest(BaseModel):
    # "property" shadows the Python built-in inside the class — use alias
    prop:     _PropertyConfig = Field(default_factory=_PropertyConfig, alias="property")
    location: _LocationConfig
    price:    _PriceConfig
    features: _FeaturesConfig = Field(default_factory=_FeaturesConfig)
    scraping: _ScrapingConfig = Field(default_factory=_ScrapingConfig)

    model_config = {"populate_by_name": True}

    def to_config(self) -> dict:
        """Return a dict in the same shape as search_filters.json."""
        return self.model_dump(by_alias=True)


# ── Per-scraper pipeline runners (blocking — called via asyncio.to_thread) ────

def _run_argenprop(config: dict) -> tuple[list[dict], str | None]:
    try:
        delay   = config.get("scraping", {}).get("delay_between_requests_seconds", [1.0, 2.0])
        session = _ap.make_session()
        raw, _  = _ap.scrape_all_pages(session, config)
        filtered = [l for l in raw if _ap.filter_listing(l, config)]
        unique   = _ap.deduplicate(filtered)
        if _ap.FETCH_COORDINATES or _ap.FETCH_DETAIL_PAGES:
            for i, listing in enumerate(unique, 1):
                unique[i - 1] = _ap.fetch_detail_page(session, listing, delay)
        return unique, None
    except Exception as exc:
        return [], f"argenprop: {exc}"


def _run_zonaprop(config: dict) -> tuple[list[dict], str | None]:
    try:
        delay   = config.get("scraping", {}).get("delay_between_requests_seconds", [1.0, 2.0])
        scraper = _zp.make_scraper()
        raw, _  = _zp.scrape_all_pages(scraper, config)
        filtered = [l for l in raw if _zp.filter_listing(l, config)]
        unique   = _zp.deduplicate(filtered)
        if _zp.FETCH_COORDINATES or _zp.FETCH_DETAIL_PAGES:
            for i, listing in enumerate(unique, 1):
                unique[i - 1] = _zp.fetch_detail_page(scraper, listing, delay)
        return unique, None
    except Exception as exc:
        return [], f"zonaprop: {exc}"


def _run_remax(config: dict) -> tuple[list[dict], str | None]:
    try:
        session  = _rm.make_session()
        raw, _   = _rm.scrape_all_pages(session, config)
        filtered = [l for l in raw if _rm.filter_listing(l, config)]
        unique   = _rm.deduplicate(filtered)
        return unique, None
    except Exception as exc:
        return [], f"remax: {exc}"


def _run_meli(config: dict) -> tuple[list[dict], str | None]:
    try:
        delay   = config.get("scraping", {}).get("delay_between_requests_seconds", [1.0, 2.0])
        scraper = _ml.make_scraper()
        raw, _  = _ml.scrape_all_pages(scraper, config)
        filtered = [l for l in raw if _ml.filter_listing(l, config)]
        unique   = _ml.deduplicate(filtered)
        if _ml.FETCH_COORDINATES or _ml.FETCH_DETAIL_PAGES:
            for i, listing in enumerate(unique, 1):
                unique[i - 1] = _ml.fetch_detail_page(scraper, listing, delay)
        return unique, None
    except Exception as exc:
        return [], f"meli: {exc}"


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/batch")
async def batch_scrape(
    body:       BatchScrapeRequest,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    config = body.to_config()

    # 1. Run all scrapers concurrently in the thread pool
    results = await asyncio.gather(
        asyncio.to_thread(_run_argenprop, config),
        asyncio.to_thread(_run_zonaprop,  config),
        asyncio.to_thread(_run_remax,     config),
        asyncio.to_thread(_run_meli,      config),
    )

    all_raw: list[dict] = []
    errors:  list[str]  = []
    for listings, error in results:
        all_raw.extend(listings)
        if error:
            errors.append(error)

    # 2. Transform + compute flags, skip invalid docs
    all_listings: list[dict] = []
    for raw in all_raw:
        listing = transform_listing(raw)
        listing["flags"] = compute_flags(listing)
        if listing.get("id") and listing.get("fuente"):
            all_listings.append(listing)

    if not all_listings:
        return {
            "inserted": [], "updated": [],
            "total_inserted": 0, "total_updated": 0,
            "errors": errors,
        }

    # 3. Determine which already exist (single query)
    or_filter = [{"id": l["id"], "fuente": l["fuente"]} for l in all_listings]
    existing_keys: set[tuple[str, str]] = set()
    async for doc in collection.find({"$or": or_filter}, {"id": 1, "fuente": 1}):
        existing_keys.add((doc["id"], doc["fuente"]))

    # 4. Bulk upsert — strip user-managed fields from $set so re-scraping
    #    never overwrites favorito / visitado / oculto; set defaults on insert only.
    ops = [
        UpdateOne(
            {"id": l["id"], "fuente": l["fuente"]},
            {
                "$set": {k: v for k, v in l.items() if k not in _USER_FIELDS},
                "$setOnInsert": _USER_DEFAULTS,
            },
            upsert=True,
        )
        for l in all_listings
    ]
    await collection.bulk_write(ops, ordered=False)

    # 5. Fetch the stored documents and split into inserted / updated
    result_docs: dict[tuple[str, str], dict] = {}
    async for doc in collection.find({"$or": or_filter}):
        doc.pop("_id", None)
        result_docs[(doc["id"], doc["fuente"])] = doc

    inserted: list[dict] = []
    updated:  list[dict] = []
    for l in all_listings:
        key = (l["id"], l["fuente"])
        doc = result_docs.get(key)
        if doc:
            (updated if key in existing_keys else inserted).append(doc)

    return {
        "inserted":       inserted,
        "updated":        updated,
        "total_inserted": len(inserted),
        "total_updated":  len(updated),
        "errors":         errors,
    }
