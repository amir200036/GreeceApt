"""
Runs both scrapers (XE + Spitogatos) in parallel and merges the results
into data/listings.json.

Each scraper runs in its **own subprocess** (``python -m greeceapt.scraper.scrape_xe`` and
``python -m greeceapt.scraper.scrape_spitogatos``). That avoids sharing one asyncio event loop
between Playwright (XE cookie bootstrap) and Spitogatos' heavy HTTP fan-out, which otherwise
made the two jobs interfere and look like only one was progressing.

XE cookie bootstrap uses **Obscura** (first port from ``xe_config.OBSCURA_BOOTSTRAP_PORT``,
default 9244; override with ``GREECEAPT_XE_OBSCURA_PORT``) when ``./obscura`` exists (no Chromium
fallback by default; set ``GREECEAPT_CHROMIUM_FALLBACK=1`` if needed). Spitogatos is **curl-only**
to the public JSON APIs and does not use a browser or Obscura.

Merge phase (``greeceapt.scraper.util``): subprocess outputs are merged into one JSON with
**Quad-Lock** dedupe (pHash + metadata). Site-side search filters apply on each portal; merge
still moves rows with parseable publication older than ``STALE_DAYS`` to
``stale_listings.json`` (see ``is_listing_stale_by_publication``).
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone

import httpx
import imagehash

from greeceapt.db_helpers import paths
from greeceapt.scraper import util as scraper_util

# Aliased from ``paths`` so tests can ``monkeypatch.setattr(run_all, "DATA_DIR", tmp)``.
PROJECT_ROOT = paths.PROJECT_ROOT
DATA_DIR = paths.DATA_DIR
OUTPUT_JSON = paths.LISTINGS_JSON
DELETED_JSON = paths.DELETED_LISTINGS_JSON
STALE_JSON = paths.STALE_LISTINGS_JSON
XE_JSON = paths.XE_LISTINGS_JSON
SPITOGATOS_JSON = paths.SPITOGATOS_LISTINGS_JSON

logger = logging.getLogger(__name__)


async def _run_scraper_subprocess(module: str, label: str) -> None:
    """Run a scraper module in a child process so it owns its own event loop (esp. Playwright vs curl)."""
    logger.info("Subprocess %s: %s -m %s", label, sys.executable, module)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        cwd=str(PROJECT_ROOT),
    )
    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"{label} scraper subprocess exited with code {rc}")
    logger.info("%s subprocess finished OK (exit 0).", label)


async def _merge() -> tuple[list[dict], list[dict], list[dict]]:
    all_raw: list[dict] = []
    for path, source in [(XE_JSON, "xe"), (SPITOGATOS_JSON, "spitogatos")]:
        if not path.exists():
            logger.warning("Output file missing, skipping: %s", path)
            continue
        with path.open(encoding="utf-8") as f:
            rows = json.load(f)
        for row in rows:
            row.setdefault("source", source)
        all_raw.extend(rows)
        logger.info("Loaded %s listings from %s", len(rows), path.name)

    url_cache = scraper_util.load_url_hash_cache_from_listings_json(OUTPUT_JSON)
    total_urls = sum(len(r.get("photo_urls") or []) for r in all_raw)
    cached_hits = sum(
        1 for r in all_raw for u in (r.get("photo_urls") or []) if u in url_cache
    )
    logger.info(
        "Computing hashes for %s listings — %s/%s image URLs already cached",
        len(all_raw), cached_hits, total_urls,
    )

    sem = asyncio.Semaphore(scraper_util.IMAGE_CONCURRENCY)
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        hash_results = await asyncio.gather(*(
            scraper_util.compute_listing_photo_hashes(
                r.get("photo_urls") or [], url_cache, sem, client,
            )
            for r in all_raw
        ))

    for listing, url_hash_map in zip(all_raw, hash_results):
        listing["photo_url_hashes"] = url_hash_map
        listing["image_hashes"] = sorted(set(url_hash_map.values()))
        listing.setdefault("source_urls", [listing.get("url", "")])

    logger.info(
        "Deduplicating %s listings — Quad-Lock (pHash D_H<%s + area/floor/year/energy)...",
        len(all_raw), scraper_util.HASH_DH_THRESHOLD,
    )
    deduped: list[dict] = []
    deduped_hash_objs: list[list[imagehash.ImageHash]] = []
    merged_pairs: list[dict] = []
    quad_lock_rejections = 0

    for listing in all_raw:
        new_hash_strs: list[str] = listing.get("image_hashes") or []
        new_hash_objs = [imagehash.hex_to_hash(h) for h in new_hash_strs]

        matched_idx: int | None = None
        if new_hash_objs:
            for i, existing_objs in enumerate(deduped_hash_objs):
                if not existing_objs:
                    continue
                is_phash_dup, phash_sim, min_h = scraper_util.phash_duplicate_metrics(
                    new_hash_objs, existing_objs,
                )
                if not is_phash_dup:
                    continue
                if scraper_util.quad_lock_metadata_match(deduped[i], listing):
                    matched_idx = i
                    break
                quad_lock_rejections += 1
                logger.info(
                    "Quad-Lock rejected duplicate candidate (pHash would merge): "
                    "candidate=%s existing=%s — pHash similarity score = %.4f, min_hamming=%s, "
                    "D_H threshold=%s; metadata mismatch (area=%s/%s floor=%s/%s year=%s/%s energy=%s/%s)",
                    listing.get("url"),
                    deduped[i].get("url"),
                    phash_sim,
                    min_h,
                    scraper_util.HASH_DH_THRESHOLD,
                    listing.get("area_sqm"),
                    deduped[i].get("area_sqm"),
                    listing.get("floor"),
                    deduped[i].get("floor"),
                    listing.get("year_built"),
                    deduped[i].get("year_built"),
                    listing.get("energy_class"),
                    deduped[i].get("energy_class"),
                )

        if matched_idx is not None:
            kept = deduped[matched_idx]
            kept_objs = deduped_hash_objs[matched_idx]
            _is_dup, phash_sim, min_h = scraper_util.phash_duplicate_metrics(
                new_hash_objs, kept_objs,
            )
            kept_snapshot = dict(kept)
            scraper_util.merge_duplicate_listing_into_kept(kept, listing)
            deduped_hash_objs[matched_idx] = [
                imagehash.hex_to_hash(h)
                for h in (kept.get("image_hashes") or [])
            ]
            merged_pairs.append({"kept": kept_snapshot, "absorbed": dict(listing)})
            logger.info(
                "Merged listing %s into %s: pHash similarity score = %.4f, min_hamming=%s "
                "(threshold D_H<%s); Quad-Lock metadata aligned (area/floor/year/energy).",
                listing.get("url"),
                kept.get("url"),
                phash_sim,
                min_h,
                scraper_util.HASH_DH_THRESHOLD,
            )
        else:
            deduped.append(dict(listing))
            deduped_hash_objs.append(new_hash_objs)

    logger.info(
        "Deduplication: %s raw → %s unique listings (%s merged, %s visual-only false positives blocked)",
        len(all_raw), len(deduped), len(merged_pairs), quad_lock_rejections,
    )

    fresh = [r for r in deduped if not scraper_util.is_listing_stale_by_publication(r)]
    stale = [r for r in deduped if scraper_util.is_listing_stale_by_publication(r)]
    logger.info(
        "Date filter: %s fresh (within %s days), %s stale removed",
        len(fresh), scraper_util.STALE_DAYS, len(stale),
    )

    fresh.sort(key=lambda r: (r.get("price_eur") is None, r.get("price_eur") or 0))
    return fresh, stale, merged_pairs


async def main() -> None:
    logger.info("=== run_all: XE + Spitogatos as two parallel subprocesses ===")
    started_at = datetime.now(timezone.utc)

    results = await asyncio.gather(
        _run_scraper_subprocess("greeceapt.scraper.scrape_xe", "XE"),
        _run_scraper_subprocess("greeceapt.scraper.scrape_spitogatos", "Spitogatos"),
        return_exceptions=True,
    )

    for name, result in zip(("XE", "Spitogatos"), results):
        if isinstance(result, Exception):
            logger.error("%s scraper failed: %s", name, result)
        else:
            logger.info("%s scraper finished OK.", name)

    logger.info("Merging results → %s", OUTPUT_JSON)
    listings, stale_listings, merged_pairs = await _merge()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(listings, f, indent=2, ensure_ascii=False)

    with DELETED_JSON.open("w", encoding="utf-8") as f:
        json.dump(merged_pairs, f, indent=2, ensure_ascii=False)

    with STALE_JSON.open("w", encoding="utf-8") as f:
        json.dump(stale_listings, f, indent=2, ensure_ascii=False)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info(
        "Done. %s fresh listings → %s  |  %s stale → %s  |  %s merged pairs → %s  (%.0fs)",
        len(listings), OUTPUT_JSON,
        len(stale_listings), STALE_JSON,
        len(merged_pairs), DELETED_JSON,
        elapsed,
    )


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    asyncio.run(main())
