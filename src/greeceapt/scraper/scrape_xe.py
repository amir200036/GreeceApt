"""CLI entry for the XE scraper (implementation in ``greeceapt.scraper.xe``)."""

from __future__ import annotations

import asyncio

from greeceapt.scraper.xe import ATHENS_CENTER_ID, run_scrape_cycle

__all__ = ["ATHENS_CENTER_ID", "run_scrape_cycle"]


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    asyncio.run(run_scrape_cycle())
