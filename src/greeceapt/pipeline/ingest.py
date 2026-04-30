import json
import logging
import sys
from pathlib import Path
from typing import Any

from greeceapt.db_helpers import insert_listings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_json(path: str | None = None) -> list[dict[str, Any]]:
    if path is None:
        default_path = PROJECT_ROOT / "data" / "scraper_listings.json"
        logger.info("Using default JSON: %s", default_path)
        path = str(default_path)

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"JSON file not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON must contain a list")

    logger.info("Loaded %s listings from %s", len(data), p)
    return data


def main() -> None:
    json_path = sys.argv[1] if len(sys.argv) > 1 else None
    listings = load_json(json_path)
    insert_listings(listings)
    logger.info("Ingestion complete. Database updated.")


if __name__ == "__main__":
    main()
