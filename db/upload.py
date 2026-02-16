"""
db/upload.py — Upload parsed property listings to MongoDB.

Reads all JSON files from the `parsed/` directory and upserts each listing
into the `properties` collection. Duplicates are avoided via upsert on the
compound key (id, fuente).

Usage:
    pip install -r requirements.txt
    python db/upload.py

    # Upload a specific file:
    python db/upload.py parsed/parsed_listings_2026-02-15_19-37-29.json
"""

import glob
import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, ConnectionFailure

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── PATHS ─────────────────────────────────────────────────────────────────────
ROOT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSED_GLOB = os.path.join(ROOT_DIR, "parsed", "*.json")

DB_NAME         = "earthbnb"
COLLECTION_NAME = "properties"


# ── CONNECTION ────────────────────────────────────────────────────────────────

def get_collection():
    """
    Load MONGODB_URI from .env and return the `properties` collection handle.
    Raises SystemExit if the variable is missing or the connection fails.
    """
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    uri = os.getenv("MONGODB_URI")
    if not uri:
        log.error("MONGODB_URI not found. Add it to your .env file.")
        sys.exit(1)

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        # Trigger an actual connection attempt
        client.admin.command("ping")
        log.info("Connected to MongoDB.")
    except ConnectionFailure as exc:
        log.error("Could not connect to MongoDB: %s", exc)
        sys.exit(1)

    return client[DB_NAME][COLLECTION_NAME]


# ── UPLOAD ────────────────────────────────────────────────────────────────────

def upload_file(collection, filepath: str) -> tuple[int, int]:
    """
    Upsert all listings from one parsed JSON file.

    Deduplication key: (id, fuente) — matches the parser's own dedup strategy.
    Returns (upserted_count, modified_count).
    """
    with open(filepath, encoding="utf-8") as fh:
        data = json.load(fh)

    listings = data.get("listings", [])
    if not listings:
        log.warning("%s has no listings — skipping", os.path.basename(filepath))
        return 0, 0

    operations = [
        UpdateOne(
            filter={"id": lst["id"], "fuente": lst["fuente"]},
            update={"$set": lst},
            upsert=True,
        )
        for lst in listings
        if lst.get("id") and lst.get("fuente")
    ]

    if not operations:
        log.warning("%s: no valid listings to upload", os.path.basename(filepath))
        return 0, 0

    try:
        result = collection.bulk_write(operations, ordered=False)
        return result.upserted_count, result.modified_count
    except BulkWriteError as exc:
        # Log write errors but don't abort — partial success is still success
        log.error("Bulk write error for %s: %s", os.path.basename(filepath), exc.details)
        return 0, 0


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Allow passing a specific file path as an argument
    if len(sys.argv) > 1:
        input_files = sys.argv[1:]
    else:
        input_files = sorted(glob.glob(PARSED_GLOB))

    if not input_files:
        log.error("No parsed files found. Run parser/parser.py first.")
        sys.exit(1)

    collection = get_collection()

    # Ensure the compound index exists (idempotent — safe to run every time)
    collection.create_index(
        [("id", 1), ("fuente", 1)],
        unique=True,
        name="id_fuente_unique",
        background=True,
    )

    total_upserted = 0
    total_modified = 0

    for filepath in input_files:
        log.info("Uploading %s …", os.path.basename(filepath))
        upserted, modified = upload_file(collection, filepath)
        log.info("  → %d inserted, %d updated", upserted, modified)
        total_upserted += upserted
        total_modified += modified

    log.info(
        "=== Done. %d inserted, %d updated across %d file(s) ===",
        total_upserted, total_modified, len(input_files),
    )


if __name__ == "__main__":
    main()
