"""CLI entry for the Spitogatos scraper (implementation in ``greeceapt.scraper.spitogatos``)."""

from __future__ import annotations

import asyncio

from greeceapt.scraper.spitogatos import config
from greeceapt.scraper.spitogatos.scrape import run

SEARCH_API = config.SEARCH_API
LISTINGS_JSON = config.LISTINGS_JSON

__all__ = ["LISTINGS_JSON", "SEARCH_API", "run"]


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    asyncio.run(run())
