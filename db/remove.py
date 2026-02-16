"""
db/remove.py — Remove documents from the `properties` collection.

Always prints a preview of matching documents and asks for confirmation
before deleting anything.

Usage examples:
    # Remove listings with no coordinates
    python db/remove.py --no-coords

    # Remove by source
    python db/remove.py --fuente zonaprop

    # Remove below or above a price
    python db/remove.py --price-below 150000
    python db/remove.py --price-above 180000

    # Remove by neighborhood (partial match, case-insensitive)
    python db/remove.py --barrio "Belgrano C"

    # Remove by flag
    python db/remove.py --flag enConstruccion

    # Combine conditions (AND logic)
    python db/remove.py --fuente zonaprop --no-coords

    # Skip confirmation prompt (for scripting)
    python db/remove.py --no-coords --yes
"""

import argparse
import logging
import os
import sys

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

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ────────────────────────────────────────────────────────────────────
ROOT_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_NAME         = "earthbnb"
COLLECTION_NAME = "properties"

FLAG_KEYS = ["porEscalera", "balcon", "enConstruccion", "aptoCredito", "cochera"]


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


# ── FILTER BUILDER ────────────────────────────────────────────────────────────

def build_filter(args: argparse.Namespace) -> dict:
    """Combine all CLI conditions into a single MongoDB filter (AND logic)."""
    conditions = []

    if args.no_coords:
        conditions.append({
            "$or": [
                {"ubicacion.coordenadas": None},
                {"ubicacion.coordenadas": {"$exists": False}},
            ]
        })

    if args.fuente:
        conditions.append({"fuente": args.fuente})

    if args.barrio:
        conditions.append({
            "ubicacion.barrio": {"$regex": args.barrio, "$options": "i"}
        })

    if args.price_below is not None:
        conditions.append({"precioUsd": {"$lt": args.price_below}})

    if args.price_above is not None:
        conditions.append({"precioUsd": {"$gt": args.price_above}})

    if args.flag:
        conditions.append({f"flags.{args.flag}": True})

    if not conditions:
        log.error("No filter conditions specified. Pass at least one condition.")
        log.error("Run with --help to see available options.")
        sys.exit(1)

    return {"$and": conditions} if len(conditions) > 1 else conditions[0]


# ── PREVIEW ───────────────────────────────────────────────────────────────────

def print_preview(col, query: dict, limit: int = 5) -> int:
    """Print up to `limit` matching docs and return the total match count."""
    total = col.count_documents(query)
    if total == 0:
        print("No documents match the given conditions.")
        return 0

    print(f"\n  Matched {total} document(s). Showing up to {limit}:\n")
    for doc in col.find(query).limit(limit):
        ub     = doc.get("ubicacion", {})
        coords = ub.get("coordenadas") or {}
        print(
            f"  [{doc.get('fuente'):10s}] id={doc.get('id'):12s} "
            f"barrio={ub.get('barrio', '?'):20s} "
            f"precio={doc.get('precioUsd')} "
            f"coords=({coords.get('latitude')}, {coords.get('longitude')})"
        )
    if total > limit:
        print(f"  ... and {total - limit} more.")
    print()
    return total


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove documents from the properties collection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--no-coords",    action="store_true",
                        help="Remove listings where coordenadas is null")
    parser.add_argument("--fuente",       metavar="SOURCE",
                        help="Remove by source (argenprop | zonaprop | remax)")
    parser.add_argument("--barrio",       metavar="NAME",
                        help="Remove by barrio (partial match, case-insensitive)")
    parser.add_argument("--price-below",  type=float, metavar="USD",
                        help="Remove listings with precioUsd < USD")
    parser.add_argument("--price-above",  type=float, metavar="USD",
                        help="Remove listings with precioUsd > USD")
    parser.add_argument("--flag",         choices=FLAG_KEYS, metavar="FLAG",
                        help=f"Remove listings where a flag is true: {FLAG_KEYS}")
    parser.add_argument("--yes", "-y",    action="store_true",
                        help="Skip confirmation prompt")

    args = parser.parse_args()
    query = build_filter(args)

    col   = get_collection()
    total = print_preview(col, query)

    if total == 0:
        sys.exit(0)

    if not args.yes:
        answer = input(f"  Delete {total} document(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    result = col.delete_many(query)
    log.info("Deleted %d document(s).", result.deleted_count)


if __name__ == "__main__":
    main()
