"""
api/routes/scrape.py — POST /scrape endpoint.

Accepts a property URL from a supported site, scrapes it on-demand,
and upserts the result into MongoDB.

POST /scrape
  201 → Property document (newly inserted)
  200 → Property document (already existed, updated in place)
  400 → URL domain not in allowed list
  422 → URL missing or malformed (handled automatically by FastAPI/Pydantic)
  500 → Scraping failed or returned no data
"""

import asyncio
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel
from pymongo import ReturnDocument

from parser.parser import compute_flags, transform_listing
from scrapers.single import VALID_DOMAINS, scrape_url

router = APIRouter(prefix="/scrape", tags=["scrape"])


# ── REQUEST MODEL ──────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str


# ── DEPENDENCY ────────────────────────────────────────────────────────────────

def col(request: Request) -> AsyncIOMotorCollection:
    return request.app.state.col


# ── ENDPOINT ──────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
async def scrape_property(
    body: ScrapeRequest,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    # 1. Validate domain — 400 if not supported
    domain = urlparse(body.url).netloc
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

    # 3. Transform (English snake_case → camelCase Spanish) + compute boolean flags
    listing = transform_listing(raw)
    listing["flags"] = compute_flags(listing)

    if not listing.get("id") or not listing.get("fuente"):
        raise HTTPException(
            status_code=500,
            detail="Scraper could not determine property id or source.",
        )

    # 4. Check whether the document already exists (used to pick the response status)
    existing = await collection.find_one(
        {"id": listing["id"], "fuente": listing["fuente"]}
    )

    # 5. Upsert into MongoDB and return the stored document
    result = await collection.find_one_and_update(
        {"id": listing["id"], "fuente": listing["fuente"]},
        {"$set": listing},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    result.pop("_id", None)

    # 6. 201 if newly inserted, 200 if it already existed and was updated
    http_status = status.HTTP_200_OK if existing else status.HTTP_201_CREATED
    return JSONResponse(content=result, status_code=http_status)
