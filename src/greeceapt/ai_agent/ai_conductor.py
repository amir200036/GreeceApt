"""
ai_conductor.py — Manages the AI filter pipeline.

Full flow:
  Layer 0  Metadata Filter         (SQL only — neighborhood, age, photo count)
  Layer 1  Moondream Visual Audit  (Ollama, concurrent download+resize, top-5 average, 1–10 scale)
  Layer 2  Aesthetic Quality Gate  (removes listings with visual_score < MIN_AESTHETIC_GRADE)

Listings that fail Layer 0 are moved to removed_listings before any inference.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_LOG_FORMAT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

LAYER1_MAX_WORKERS = 2
DOWNLOAD_WORKERS   = 8


# ── Layer 1 worker (runs inside a thread — no DB access) ─────────────────────

def _process_listing(args: tuple[str, list[str]]) -> tuple[str, float | None]:
    """Download + resize all images for a listing, run Moondream, return top-5 average (1–10 scale)."""
    from greeceapt.ai_agent.layer_1_quality_audit import analyze_listing, download_and_resize

    listing_id, urls = args

    paths: list[str] = []
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        future_to_url = {pool.submit(download_and_resize, url): url for url in urls}
        for future in as_completed(future_to_url):
            path = future.result()
            if path:
                paths.append(path)

    logger.info("Listing %s: %d URLs → %d downloaded", listing_id, len(urls), len(paths))

    try:
        visual_score = analyze_listing(paths)
        return listing_id, visual_score
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


# ── Layer 1 orchestrator ──────────────────────────────────────────────────────

def run_layer1(max_workers: int = LAYER1_MAX_WORKERS) -> None:
    """
    Fetch unprocessed listings, run Moondream concurrently (ThreadPoolExecutor),
    write all results back in the main thread, then apply the post-audit purge.
    """
    from greeceapt.db_helpers import db_conductor

    _log_path = _DATA_DIR / "layer1.log"
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(_LOG_FORMAT)
    logging.getLogger().addHandler(_fh)

    try:
        _run_layer1_impl(db_conductor, max_workers)
    finally:
        logging.getLogger().removeHandler(_fh)
        _fh.close()


def _run_layer1_impl(db_conductor, max_workers: int) -> None:
    conn = db_conductor.connect_updated()
    try:
        work_items = db_conductor.get_listings_for_layer1(conn)
        logger.info("Layer 1: %s listings to process.", len(work_items))
        if not work_items:
            logger.info("Layer 1: nothing to process.")
            return
    finally:
        conn.close()

    # ── Concurrent Moondream inference ────────────────────────────────────────
    results: list[tuple[str, float | None]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_listing, item): item[0] for item in work_items}
        for future in as_completed(futures):
            lid = futures[future]
            try:
                lid, visual_score = future.result()
                results.append((lid, visual_score))
                score_str = f"{visual_score:.2f}" if visual_score is not None else "None"
                logger.info("Layer 1: id=%-8s  visual_score=%s", lid, score_str)
            except Exception as exc:
                logger.warning("Layer 1: id=%s worker failed — %s", lid, exc)

    # ── Write all results in main thread ─────────────────────────────────────
    if results:
        conn = db_conductor.connect_updated()
        try:
            for lid, visual_score in results:
                db_conductor.save_visual_audit_results(conn, lid, visual_score, {})
        finally:
            conn.close()

    scored  = sum(1 for _, s in results if s is not None)
    skipped = len(work_items) - len(results)
    logger.info(
        "Layer 1 done. processed=%s  scored=%s  no_result=%s  worker_errors=%s",
        len(results), scored, len(results) - scored, skipped,
    )


# ── Top-level pipeline runner ─────────────────────────────────────────────────

def run() -> None:
    logger.info("=== AI Conductor: Layer 0 — Metadata Filter ===")
    from greeceapt.ai_agent.layer_0_cleaner import run as layer0
    layer0()

    logger.info("=== AI Conductor: Layer 1 — Moondream Visual Audit ===")
    run_layer1()

    logger.info("=== AI Conductor: Layer 2 — Aesthetic Quality Gate ===")
    from greeceapt.ai_agent.layer_2_aesthetic_filter import run as layer2
    layer2()

    logger.info("AI pipeline complete.")


if __name__ == "__main__":
    run()
