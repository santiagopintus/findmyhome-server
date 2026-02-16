"""
db/read.py — Inspect the `properties` collection in MongoDB.

Prints a summary and spot-checks a few documents to verify the upload.

Usage:
    python db/read.py              # summary + 3 samples per source
    python db/read.py --full       # print full JSON for each sample
    python db/read.py --id 18059878 --fuente argenprop   # look up one property
"""

import argparse
import json
import logging
import os
import sys

# Force UTF-8 output so Spanish characters (ñ, á, etc.) render correctly
# on Windows consoles that default to cp1252.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── PATHS / CONFIG ────────────────────────────────────────────────────────────
ROOT_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_NAME         = "earthbnb"
COLLECTION_NAME = "properties"


# ── CONNECTION ────────────────────────────────────────────────────────────────

def get_collection():
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
    uri = os.getenv("MONGODB_URI")
    if not uri:
        log.error("MONGODB_URI not found in .env")
        sys.exit(1)
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        client.admin.command("ping")
        log.info("Connected to MongoDB.")
    except ConnectionFailure as exc:
        log.error("Could not connect: %s", exc)
        sys.exit(1)
    return client[DB_NAME][COLLECTION_NAME]


# ── DISPLAY HELPERS ───────────────────────────────────────────────────────────

def _doc_to_str(doc: dict, full: bool = False) -> str:
    """Return a readable string for one document."""
    doc.pop("_id", None)
    if full:
        return json.dumps(doc, ensure_ascii=False, indent=2)

    flags = doc.get("flags", {})
    active_flags = [k for k, v in flags.items() if v]
    ub = doc.get("ubicacion", {})
    det = doc.get("detalles", {})
    coords = ub.get("coordenadas") or {}

    return (
        f"  id        : {doc.get('id')}\n"
        f"  fuente    : {doc.get('fuente')}\n"
        f"  titulo    : {doc.get('titulo', '')[:80]}\n"
        f"  precioUsd : {doc.get('precioUsd')}\n"
        f"  ubicacion : {ub.get('barrio')}, {ub.get('ciudad')}\n"
        f"  detalles  : {det.get('ambientes')} amb | {det.get('dormitorios')} dorm | "
        f"{det.get('banos')} baños | {det.get('superficieCubierta')} m²\n"
        f"  coordenadas: lat={coords.get('latitude')} lng={coords.get('longitude')}\n"
        f"  flags     : {active_flags if active_flags else '(none)'}\n"
        f"  url       : {doc.get('url', '')[:80]}"
    )


# ── VIEWS ─────────────────────────────────────────────────────────────────────

def print_summary(col) -> None:
    total = col.count_documents({})
    print(f"\n{'-'*60}")
    print(f"  Collection : {DB_NAME}.{COLLECTION_NAME}")
    print(f"  Total docs : {total}")

    # Breakdown by source
    pipeline = [{"$group": {"_id": "$fuente", "count": {"$sum": 1}}}]
    for row in sorted(col.aggregate(pipeline), key=lambda r: r["_id"] or ""):
        print(f"  {row['_id']:12s}: {row['count']} properties")

    # Price range
    pipeline2 = [
        {"$match": {"precioUsd": {"$ne": None}}},
        {"$group": {
            "_id": None,
            "min": {"$min": "$precioUsd"},
            "max": {"$max": "$precioUsd"},
            "avg": {"$avg": "$precioUsd"},
        }},
    ]
    for row in col.aggregate(pipeline2):
        print(
            f"  Price range: USD {row['min']:,.0f} – {row['max']:,.0f} "
            f"(avg {row['avg']:,.0f})"
        )

    # Flags breakdown
    flag_keys = ["porEscalera", "balcon", "enConstruccion", "aptoCredito", "cochera"]
    print(f"\n  Flags:")
    for flag in flag_keys:
        count = col.count_documents({f"flags.{flag}": True})
        print(f"    {flag:16s}: {count}")

    print(f"{'-'*60}\n")


def print_samples(col, full: bool = False, per_source: int = 3) -> None:
    sources = col.distinct("fuente")
    for source in sorted(sources):
        docs = list(col.find({"fuente": source}).limit(per_source))
        print(f"\n{'='*60}")
        print(f"  Samples from: {source}  ({len(docs)} shown)")
        print(f"{'='*60}")
        for doc in docs:
            print(_doc_to_str(doc, full=full))
            print()


def lookup_one(col, prop_id: str, fuente: str, full: bool = False) -> None:
    doc = col.find_one({"id": prop_id, "fuente": fuente})
    if not doc:
        print(f"No document found for id={prop_id!r} fuente={fuente!r}")
        return
    print(_doc_to_str(doc, full=True))


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the properties collection.")
    parser.add_argument("--full",   action="store_true", help="Print full JSON for each sample")
    parser.add_argument("--id",     help="Look up a specific property by id")
    parser.add_argument("--fuente", help="Source for --id lookup (e.g. argenprop)")
    args = parser.parse_args()

    col = get_collection()

    if args.id:
        if not args.fuente:
            print("--fuente is required when using --id")
            sys.exit(1)
        lookup_one(col, args.id, args.fuente, full=True)
        return

    print_summary(col)
    print_samples(col, full=args.full)


if __name__ == "__main__":
    main()
