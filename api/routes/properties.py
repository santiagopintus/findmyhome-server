"""
api/routes/properties.py — CRUD endpoints for the `properties` collection.

GET    /properties                          List with filters + pagination
GET    /properties/{fuente}/{id}            Single property
POST   /properties                          Insert one
PUT    /properties/{fuente}/{id}            Partial update
DELETE /properties/{fuente}/{id}            Delete one
PATCH  /properties/{fuente}/{id}/favourite  Mark / unmark as favourite
PATCH  /properties/{fuente}/{id}/hidden     Mark / unmark as hidden
"""

import math
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from motor.motor_asyncio import AsyncIOMotorCollection

from api.models import (
    FavouriteUpdate,
    NotesUpdate,
    OcultoUpdate,
    PaginatedProperties,
    Property,
    PropertyCreate,
    PropertyUpdate,
    VisitadoUpdate,
)

router = APIRouter(prefix="/properties", tags=["properties"])

FLAG_KEYS = {
    "porEscalera", "balcon", "patio", "enConstruccion",
    "aptoCredito", "cochera", "cocheraOpcional", "reservado",
}
FUENTES   = {"argenprop", "zonaprop", "remax"}


# ── DEPENDENCY ────────────────────────────────────────────────────────────────

def col(request: Request) -> AsyncIOMotorCollection:
    return request.app.state.col


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _clean(doc: dict) -> dict:
    """Strip the MongoDB internal _id before returning a document."""
    doc.pop("_id", None)
    return doc


def _build_filter(
    barrio:        str | None,
    fuente:        str | None,
    precio_min:    float | None,
    precio_max:    float | None,
    ambientes:     int | None,
    dormitorios:   int | None,
    flags:         list[str],
    exclude_flags: list[str],
    favorito:      bool | None,
    oculto:        bool | None,
) -> dict:
    conditions: list[dict] = []

    if barrio:
        conditions.append({"ubicacion.barrio": {"$regex": re.escape(barrio), "$options": "i"}})

    if fuente:
        conditions.append({"fuente": fuente})

    price_filter: dict = {}
    if precio_min is not None:
        price_filter["$gte"] = precio_min
    if precio_max is not None:
        price_filter["$lte"] = precio_max
    if price_filter:
        conditions.append({"precioUsd": price_filter})

    if ambientes is not None:
        conditions.append({"detalles.ambientes": ambientes})

    if dormitorios is not None:
        conditions.append({"detalles.dormitorios": dormitorios})

    for flag in flags:
        if flag in FLAG_KEYS:
            conditions.append({f"flags.{flag}": True})

    for flag in exclude_flags:
        if flag in FLAG_KEYS:
            conditions.append({f"flags.{flag}": {"$ne": True}})

    if favorito is not None:
        conditions.append({"favorito": favorito})

    if oculto is not None:
        if oculto:
            conditions.append({"oculto": True})
        else:
            # Exclude hidden properties; also matches docs where the field doesn't exist
            conditions.append({"oculto": {"$ne": True}})

    return {"$and": conditions} if conditions else {}


# ── LIST ──────────────────────────────────────────────────────────────────────

_SORT_FIELDS = {
    "precio":              "precioUsd",
    "superficie_cubierta": "detalles.superficieCubierta",
    "superficie_total":    "detalles.superficieTotal",
}


@router.get("", response_model=PaginatedProperties)
async def list_properties(
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
    barrio:      str | None = Query(None, description="Neighbourhood, partial match"),
    fuente:      str | None = Query(None, description="argenprop | zonaprop | remax | meli"),
    precio_min:  float | None = Query(None, description="Minimum price in USD"),
    precio_max:  float | None = Query(None, description="Maximum price in USD"),
    ambientes:   int | None = Query(None, description="Number of rooms (ambientes)"),
    dormitorios: int | None = Query(None, description="Number of bedrooms"),
    flags:         list[str] = Query(default=[], description="Flag names that must be true"),
    exclude_flags: list[str] = Query(default=[], description="Flag names that must be false"),
    favorito:      bool | None = Query(None, description="true → only favourites, false → only non-favourites"),
    oculto:        bool | None = Query(None, description="true → only hidden, false → exclude hidden"),
    sort_by:       str | None = Query(None, description="precio | superficie_cubierta | superficie_total"),
    sort_order:    str | None = Query("asc", description="asc | desc"),
    page:          int = Query(1, ge=1),
    page_size:     int = Query(20, ge=1, le=100, alias="pageSize"),
):
    query  = _build_filter(barrio, fuente, precio_min, precio_max, ambientes, dormitorios, flags, exclude_flags, favorito, oculto)
    total  = await collection.count_documents(query)
    skip   = (page - 1) * page_size
    cursor = collection.find(query)
    if sort_by and sort_by in _SORT_FIELDS:
        direction = 1 if sort_order == "asc" else -1
        cursor = cursor.sort(_SORT_FIELDS[sort_by], direction)
    cursor = cursor.skip(skip).limit(page_size)
    docs   = [_clean(doc) async for doc in cursor]

    return PaginatedProperties(
        total=total,
        page=page,
        pageSize=page_size,
        pages=math.ceil(total / page_size) if total else 0,
        results=docs,
    )


# ── GET ONE ───────────────────────────────────────────────────────────────────

@router.get("/{fuente}/{id}", response_model=Property)
async def get_property(
    fuente: str,
    id:     str,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    doc = await collection.find_one({"id": id, "fuente": fuente})
    if not doc:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(doc)


# ── CREATE ────────────────────────────────────────────────────────────────────

@router.post("", response_model=Property, status_code=status.HTTP_201_CREATED)
async def create_property(
    body:       PropertyCreate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    existing = await collection.find_one({"id": body.id, "fuente": body.fuente})
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Property id={body.id} fuente={body.fuente} already exists. Use PUT to update.",
        )
    doc = body.model_dump()
    await collection.insert_one(doc)
    doc.pop("_id", None)
    return doc


# ── UPDATE ────────────────────────────────────────────────────────────────────

@router.put("/{fuente}/{id}", response_model=Property)
async def update_property(
    fuente: str,
    id:     str,
    body:   PropertyUpdate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    # Only send fields that were explicitly provided (not None)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update provided.")

    result = await collection.find_one_and_update(
        {"id": id, "fuente": fuente},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(result)


# ── DELETE ────────────────────────────────────────────────────────────────────

@router.delete("/{fuente}/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(
    fuente: str,
    id:     str,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    result = await collection.delete_one({"id": id, "fuente": fuente})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Property not found")


# ── FAVOURITE ─────────────────────────────────────────────────────────────────

@router.patch("/{fuente}/{id}/favourite", response_model=Property)
async def set_favourite(
    fuente: str,
    id:     str,
    body:   FavouriteUpdate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    result = await collection.find_one_and_update(
        {"id": id, "fuente": fuente},
        {"$set": {"favorito": body.favorito}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(result)


# ── VISITED ───────────────────────────────────────────────────────────────────

@router.patch("/{fuente}/{id}/visited", response_model=Property)
async def set_visited(
    fuente: str,
    id:     str,
    body:   VisitadoUpdate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    result = await collection.find_one_and_update(
        {"id": id, "fuente": fuente},
        {"$set": {"visitado": body.visitado}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(result)


# ── HIDDEN ────────────────────────────────────────────────────────────────────

@router.patch("/{fuente}/{id}/hidden", response_model=Property)
async def set_hidden(
    fuente: str,
    id:     str,
    body:   OcultoUpdate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    result = await collection.find_one_and_update(
        {"id": id, "fuente": fuente},
        {"$set": {"oculto": body.oculto}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(result)


# ── NOTES ─────────────────────────────────────────────────────────────────────

@router.patch("/{fuente}/{id}/notes", response_model=Property)
async def set_notes(
    fuente: str,
    id:     str,
    body:   NotesUpdate,
    collection: Annotated[AsyncIOMotorCollection, Depends(col)],
):
    """Update user-written notes: free-text comment and/or manual boolean flags."""
    data = body.model_dump(exclude_unset=True)
    updates: dict = {}

    if "comentarios" in data:
        updates["comentarios"] = data["comentarios"]
    if "flagsManual" in data:
        updates["flagsManual"] = data["flagsManual"]  # already a dict from model_dump

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update provided.")

    result = await collection.find_one_and_update(
        {"id": id, "fuente": fuente},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Property not found")
    return _clean(result)
