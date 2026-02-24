"""
api/db.py — Async MongoDB connection via Motor.

The client is initialised once at app startup (lifespan) and shared
across all requests through app.state.
"""

import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

DB_NAME         = os.getenv("MONGODB_DB_NAME", "earthbnb")
COLLECTION_NAME = "properties"


def create_client() -> AsyncIOMotorClient:
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI not set in .env")
    return AsyncIOMotorClient(uri)


def get_collection(client: AsyncIOMotorClient) -> AsyncIOMotorCollection:
    return client[DB_NAME][COLLECTION_NAME]
