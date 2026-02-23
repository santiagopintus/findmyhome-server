"""
api/models.py — Pydantic schemas for request validation and response serialisation.
"""

from pydantic import BaseModel


# ── NESTED MODELS ─────────────────────────────────────────────────────────────

class Coordenadas(BaseModel):
    latitude:  float | None = None
    longitude: float | None = None


class Ubicacion(BaseModel):
    barrio:      str | None = None
    direccion:   str | None = None
    ciudad:      str | None = None
    coordenadas: Coordenadas | None = None


class Detalles(BaseModel):
    ambientes:          int   | None = None
    dormitorios:        int   | None = None
    banos:              int   | None = None
    superficieTotal:    float | None = None
    superficieCubierta: float | None = None
    piso:               int   | None = None
    antiguedad:         int   | None = None


class Flags(BaseModel):
    porEscalera:     bool = False
    balcon:          bool = False
    patio:           bool = False
    enConstruccion:  bool = False
    aptoCredito:     bool = False
    cochera:         bool = False
    cocheraOpcional: bool = False
    reservado:       bool = False


class FlagsManual(BaseModel):
    """Manually set booleans — filled in by the user via the table UI."""
    cocinaGrande:         bool = False
    necesitaRemodelar:    bool = False
    tienePlazaCerca:      bool = False


# ── MAIN DOCUMENT MODELS ──────────────────────────────────────────────────────

class Property(BaseModel):
    """Full property document as stored in MongoDB (read responses)."""
    id:              str
    fuente:          str
    titulo:          str | None = None
    precioUsd:       float | None = None
    moneda:          str | None = None
    descripcion:     str | None = None
    imagenes:        list[str] = []
    url:             str | None = None
    extraidoEn:      str | None = None
    caracteristicas: list[str] = []
    ubicacion:       Ubicacion    | None = None
    detalles:        Detalles     | None = None
    flags:           Flags        | None = None
    flagsManual:     FlagsManual  | None = None
    comentarios:     str | None = None
    favorito:        bool = False
    visitado:        bool = False
    oculto:          bool = False


class PropertyCreate(BaseModel):
    """Body for POST /properties. id and fuente are required."""
    id:              str
    fuente:          str
    titulo:          str | None = None
    precioUsd:       float | None = None
    moneda:          str | None = None
    descripcion:     str | None = None
    imagenes:        list[str] = []
    url:             str | None = None
    extraidoEn:      str | None = None
    caracteristicas: list[str] = []
    ubicacion:       Ubicacion    | None = None
    detalles:        Detalles     | None = None
    flags:           Flags        | None = None
    flagsManual:     FlagsManual  | None = None
    comentarios:     str | None = None
    favorito:        bool = False
    visitado:        bool = False
    oculto:          bool = False


class PropertyUpdate(BaseModel):
    """Body for PUT /properties/{fuente}/{id}. All fields optional."""
    titulo:          str | None = None
    precioUsd:       float | None = None
    moneda:          str | None = None
    descripcion:     str | None = None
    imagenes:        list[str] | None = None
    url:             str | None = None
    extraidoEn:      str | None = None
    caracteristicas: list[str] | None = None
    ubicacion:       Ubicacion | None = None
    detalles:        Detalles  | None = None
    flags:           Flags     | None = None


class FavouriteUpdate(BaseModel):
    """Body for PATCH /properties/{fuente}/{id}/favourite."""
    favorito: bool


class VisitadoUpdate(BaseModel):
    """Body for PATCH /properties/{fuente}/{id}/visited."""
    visitado: bool


class OcultoUpdate(BaseModel):
    """Body for PATCH /properties/{fuente}/{id}/hidden."""
    oculto: bool


class NotesUpdate(BaseModel):
    """Body for PATCH /properties/{fuente}/{id}/notes."""
    comentarios: str | None = None
    flagsManual: FlagsManual | None = None


# ── PAGINATED RESPONSE ────────────────────────────────────────────────────────

class PaginatedProperties(BaseModel):
    total:    int
    page:     int
    pageSize: int
    pages:    int
    results:  list[Property]
