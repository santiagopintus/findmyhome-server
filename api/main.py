"""
api/main.py — FastAPI application entry point.

Run with:
    uvicorn api.main:app --reload

Interactive docs available at:
    http://localhost:8000/docs   (Swagger UI)
    http://localhost:8000/redoc  (ReDoc)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db import create_client, get_collection
from api.routes.properties import router as properties_router
from api.routes.scrape import router as scrape_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — create Motor client and attach collection to app state
    client = create_client()
    app.state.col = get_collection(client)
    yield
    # Shutdown — close the connection
    client.close()


app = FastAPI(
    title="Encontremos Casa API",
    description="Real estate listings from ArgenProp, ZonaProp and RE/MAX.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this when deploying
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(properties_router)
app.include_router(scrape_router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
