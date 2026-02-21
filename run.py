"""
run.py - Full pipeline: scrape all sources in parallel -> parse -> upload to MongoDB.

Usage:
    python run.py
"""

import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parent
PYTHON = sys.executable

SCRAPERS = [
    ("argenprop",  ROOT / "scrapers" / "argenprop_scraper.py"),
    ("zonaprop",   ROOT / "scrapers" / "zonaprop_scraper.py"),
    ("remax",      ROOT / "scrapers" / "remax_scraper.py"),
    ("meli",       ROOT / "scrapers" / "meli_scraper.py"),
    ("properati",  ROOT / "scrapers" / "properati_scraper.py"),
]

PARSER   = ROOT / "parser" / "parser.py"
UPLOADER = ROOT / "db"     / "upload.py"


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _stream(proc: subprocess.Popen, label: str, failed: list[str]) -> None:
    """Read lines from proc stdout and print with a [label] prefix."""
    for line in proc.stdout:
        print(f"[{label}] {line}", end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        failed.append(label)


def _run_parallel() -> list[str]:
    """Launch all scrapers simultaneously and return labels of any that failed."""
    failed: list[str] = []
    threads: list[threading.Thread] = []

    for label, path in SCRAPERS:
        proc = subprocess.Popen(
            [PYTHON, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=ROOT,
        )
        t = threading.Thread(target=_stream, args=(proc, label, failed), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    return failed


def _run_step(label: str, path: Path) -> bool:
    """Run a sequential step, inherit its output, return True on success."""
    _divider(label)
    result = subprocess.run([PYTHON, str(path)], cwd=ROOT)
    return result.returncode == 0


def _divider(label: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {label}")
    print(f"{'-' * 60}\n")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  ENCONTREMOS CASA - full pipeline")
    print("=" * 60)
    print(f"\nStep 1 - Running {len(SCRAPERS)} scrapers in parallel...\n")

    failed = _run_parallel()

    if failed:
        print(f"\n[!] Scrapers that failed or returned errors: {', '.join(failed)}")
        print("    Continuing with whatever data was saved to output/...")
    else:
        print("\n[OK] All scrapers finished.")

    if not _run_step("Step 2 - Parsing", PARSER):
        print("[FAIL] Parser failed - aborting.")
        sys.exit(1)

    if not _run_step("Step 3 - Uploading to MongoDB", UPLOADER):
        print("[FAIL] Upload failed.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  Done. New properties are live in MongoDB.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
