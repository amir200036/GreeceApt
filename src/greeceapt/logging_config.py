"""Single place to configure the root logger for CLI entrypoints."""

from __future__ import annotations

import logging

DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def configure_root_logging(
    *,
    level: int = logging.INFO,
    fmt: str | None = None,
    force: bool = False,
) -> None:
    """Configure the root logger once per process (set ``force=True`` to replace handlers)."""
    logging.basicConfig(level=level, format=fmt or DEFAULT_FORMAT, force=force)
