"""
main.py — Top-level pipeline orchestrator.

Calls each conductor's run() in sequence:
  1. AI Conductor     — Layer 0 (CLIP), Layer 1 (Ruin Filter)
  2. Scoring Conductor — Layer 3 (Market Analytics), Final Ranking
"""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=== GreeceApt Pipeline Start ===")

    from greeceapt.ai_agent.ai_conductor import run as ai_run
    ai_run()

    from greeceapt.scoring.scoring_conductor import run as scoring_run
    scoring_run()

    logger.info("=== GreeceApt Pipeline Complete ===")


if __name__ == "__main__":
    main()
