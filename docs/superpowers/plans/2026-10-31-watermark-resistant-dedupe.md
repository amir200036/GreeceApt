# Watermark-Resistant Dedupe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a conservative, fast, local dedupe pipeline that resists border watermarks, never auto-merges from fewer than three matched photos, and prevents unsafe transitive merges.

**Architecture:** Represent every photo with versioned full-image and center-crop pHashes, index near hashes with five Hamming bands, and generate candidates from the union of photo, geo, and metadata sources. Score each pair with explicit evidence, then form groups with deterministic complete-link clustering so every automatic group is supported by direct `HIGH` evidence between all members.

**Tech Stack:** Python 3, Pillow, ImageHash, asyncio/httpx, dataclasses, JSON, pytest

**Spec:** `docs/superpowers/specs/2026-10-31-watermark-resistant-dedupe-design.md`

## Global Constraints

- Treat a false merge as more damaging than a missed duplicate; ambiguous evidence must become `REVIEW`, not `HIGH`.
- Automatic merge requires at least 3 one-to-one matched photos.
- A photo match is valid only when `full↔full` or `center↔center` Hamming distance is `< 5`; never cross-compare variants.
- Center fingerprints crop exactly 12.5% from each edge, retaining 75% of image width and height in memory only.
- Do not remove watermarks, reconstruct pixels, rewrite originals, or introduce OCR, embeddings, or another model.
- Treat near hashes within Hamming distance `4` as one boilerplate family; exclude a family seen in at least `5` distinct listings.
- Candidate tolerances are exact: geo `≤ 1,000 m`, area relative delta `≤ 3%`, and price relative delta `≤ 25%`.
- Preserve old string cache entries and legacy `photo_url_hashes`/`image_hashes` for one compatibility period; cache replacement must remain atomic.
- Do not change survivor selection, field merge behavior, apartment/Qwen scoring, scraper filters, or database schemas.
- Do not delete or overwrite production JSON while developing or evaluating.
- Preserve unrelated dirty worktree changes. Every commit must stage only the files and, where necessary, the hunks named by its task.
- Deliver on `codex/dedupe-hardening` through a pull request.

## Review Focus

- Tiny, narrow, or portrait images must always produce a non-empty valid center crop; pin this in Task 2.
- Repeated URLs or repeated variants inside one listing must count only once toward the five-listing boilerplate threshold; pin this in Task 3.
- Legacy full-only fingerprints must remain matchable without accidentally cross-comparing them to center hashes; pin this in Tasks 2 and 5.
- A pair discovered by multiple candidate sources must be scored and emitted only once while retaining all source labels; pin this in Task 4.
- Group membership and review output must be identical for any input listing or edge order; pin this in Task 6.

---

### Task 1: Freeze the Evaluation Inputs and Baseline Gates

**Files:**
- Adopt existing untracked baseline: `src/greeceapt/scraper/dedupe.py`
- Adopt existing untracked baseline: `tests/test_dedupe.py`
- Create: `src/greeceapt/scraper/dedupe_eval.py`
- Create: `tests/test_dedupe_eval.py`
- Create: `tests/fixtures/dedupe_live_2026-10-31.json`
- Create: `tests/fixtures/dedupe_historical_positives_2026-08.json`

**Interfaces:**
- Consumes: current `dedupe.evaluate_pair()`, `dedupe.candidate_pairs()`, the two local `data/_archive_2026-08-*/deleted_listings.json` files, and `/tmp/greeceapt-scraper-check.DHzsOo/{xe_detail_listings,spitogatos_listings,merged_pairs,review_pairs}.json` only while creating the fixtures.
- Produces: `PositivePair(kept: dict, absorbed: dict)`, `load_positive_pairs(paths: Sequence[Path]) -> list[PositivePair]`, `load_live_fixture(path: Path) -> dict`, frozen reference functions `legacy_candidate_pairs(listings: Sequence[dict]) -> set[tuple[int, int]]`, `legacy_evaluate_pair(pair: PositivePair) -> dict`, and `legacy_find_duplicate_groups(listings: Sequence[dict]) -> dict`, `evaluate_legacy_pair_policy(pairs: Sequence[PositivePair]) -> dict[str, int]`, and CLI entry point `python -m greeceapt.scraper.dedupe_eval`.

- [ ] **Step 1: Verify and preserve the existing measured baseline**

  Run: `venv/bin/pytest -q tests/test_dedupe.py tests/test_scraper.py`

  Expected: `52 passed`. Treat the current untracked `dedupe.py` and `test_dedupe.py` as the measured baseline already reviewed with the user; do not rewrite them while freezing fixtures, but include them in this first implementation commit so every later commit is reproducible from Git alone.

- [ ] **Step 2: Write failing evaluation-contract tests**

  Add tests asserting that the live fixture has `64` listings, synthetic IDs and local photo keys only, contains no `description`, `address`, `http://`, or `https://` text, and declares expected outcomes of `6` automatic pairs and `1` review pair. Assert the historical fixture contains exactly `1,224` positive pairs and uses the same sanitized schema. Add a synthetic loader test that reads a minimal `deleted_listings.json` entry into `PositivePair` without mutating it.

- [ ] **Step 3: Run the focused tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_dedupe_eval.py`

  Expected: FAIL because `dedupe_eval` and the fixture do not exist.

- [ ] **Step 4: Implement the loader and create the sanitized fixtures**

  Both fixtures must contain only `id`, dedupe-consumed numeric/enum fields, source, local photo keys such as `p0001`, and legacy hashes/fingerprints required to reproduce the decisions. Map every original listing/photo URL to a stable synthetic token, store explicit pair IDs rather than relying on list order, and deduplicate any pair repeated across the two archives. The module must reject a fixture containing a remote URL or forbidden free-text field. Raw archives and `/tmp` payloads must not be added to Git.

- [ ] **Step 5: Add and run the historical baseline tests**

  Assert against the checked-in historical fixture that there are exactly `1,224` known-positive pairs, all `1,224` are proposed by a frozen copy of the current candidate rules, the isolated legacy pair policy yields `1,064` automatic pairs, and exactly `53` currently automatic pairs have only one or two matches and therefore belong in the future review path. Keep these small reference functions inside `dedupe_eval.py` unchanged by later production refactors; the isolated policy metric has no corpus-wide blacklist/grouping, while `legacy_find_duplicate_groups` preserves the complete pre-V2 CPU stage for the later benchmark. This makes both correctness and runtime comparisons reproducible in a clean clone.

  Run: `venv/bin/pytest -q tests/test_dedupe_eval.py`

  Expected: PASS with the exact `1224 / 1064 / 53` gates.

- [ ] **Step 6: Commit the baseline, evaluation harness, and fixtures**

  ```bash
  git add src/greeceapt/scraper/dedupe.py tests/test_dedupe.py src/greeceapt/scraper/dedupe_eval.py tests/test_dedupe_eval.py tests/fixtures/dedupe_live_2026-10-31.json tests/fixtures/dedupe_historical_positives_2026-08.json
  git commit -m "test: freeze dedupe evaluation corpus"
  ```

### Task 2: Add Versioned Full and Center Photo Fingerprints

**Files:**
- Create: `src/greeceapt/scraper/fingerprints.py`
- Create: `tests/test_fingerprints.py`
- Modify: `src/greeceapt/scraper/util.py:205-283`
- Modify: `tests/test_scraper.py:105-145`

**Interfaces:**
- Consumes: Pillow `Image.Image`, `imagehash.phash`, URL strings, and legacy cache values shaped as hex strings.
- Produces: frozen `PhotoFingerprint(version: str, full: int | None, center: int | None)`, `PhotoFingerprint.from_json(value: object) -> PhotoFingerprint | None`, `PhotoFingerprint.to_json() -> dict[str, str | None]`, `fingerprint_image(image: Image.Image) -> PhotoFingerprint`, `load_standalone_phash_cache(cache_json: Path | None = None) -> dict[str, PhotoFingerprint]`, and `compute_listing_photo_fingerprints(photo_urls: list[str], url_cache: dict[str, PhotoFingerprint], sem: asyncio.Semaphore, client: httpx.AsyncClient) -> dict[str, PhotoFingerprint]`.

- [ ] **Step 1: Write failing fingerprint tests**

  Cover deterministic `phash-v2` serialization, old hex-string parsing as full-only, malformed record rejection, a synthetic outer-border overlay whose full hash changes while its center hash still matches below distance `5`, and the Review Focus case of images as small/narrow as `1×8`, `8×1`, and portrait `17×101` producing non-empty crops.

- [ ] **Step 2: Run the focused tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_fingerprints.py tests/test_scraper.py -k "fingerprint or phash_cache"`

  Expected: FAIL because the versioned fingerprint API is absent.

- [ ] **Step 3: Implement fingerprint creation and JSON conversion**

  Convert to RGB, compute `full`, crop 12.5% per edge using integer bounds clamped so width and height remain at least one pixel, then compute `center`. Store hashes as 16-character lowercase hexadecimal strings in JSON and integers in memory. A decode failure returns no fingerprint; it must not synthesize a hash.

- [ ] **Step 4: Migrate the cache without breaking old data**

  Load both legacy strings and v2 objects. Reuse a cache entry only when it has both v2 variants; a legacy/full-only entry remains readable for archive matching but is placed on the next network download list for lazy upgrade. Write new records to a sibling temporary file, flush valid JSON, and replace atomically. Keep `load_url_hash_cache_from_listings_json()` able to seed from both `photo_fingerprints` and old `photo_url_hashes`.

- [ ] **Step 5: Run fingerprint and scraper compatibility tests**

  Run: `venv/bin/pytest -q tests/test_fingerprints.py tests/test_scraper.py -k "fingerprint or phash_cache or url_hash_cache"`

  Expected: PASS, including legacy load, lazy upgrade, corrupt-cache fallback, and atomic replacement tests.

- [ ] **Step 6: Commit only fingerprint/cache changes**

  ```bash
  git add src/greeceapt/scraper/fingerprints.py tests/test_fingerprints.py
  git add -p src/greeceapt/scraper/util.py tests/test_scraper.py
  git commit -m "feat: add versioned watermark-resistant fingerprints"
  ```

### Task 3: Index Near Hashes and Remove Similar Boilerplate

**Files:**
- Create: `src/greeceapt/scraper/phash_index.py`
- Create: `tests/test_phash_index.py`
- Modify: `src/greeceapt/scraper/dedupe.py:59-108`
- Modify: `tests/test_dedupe.py:37-85`

**Interfaces:**
- Consumes: `PhotoFingerprint` from Task 2 and listing-index/photo-key references.
- Produces: frozen `PhotoRef(photo_key: str, fingerprint: PhotoFingerprint)`, frozen `HashRef(listing_index: int, photo_key: str, variant: Literal["full", "center"], value: int)`, `HammingBandIndex(radius: int = 4, bands: int = 5)`, `HammingBandIndex.add(ref: HashRef) -> None`, `HammingBandIndex.neighbors(ref: HashRef) -> tuple[HashRef, ...]`, and `analyze_photo_index(listings: Sequence[dict]) -> PhotoIndexAnalysis`. `PhotoIndexAnalysis.usable_photos_by_listing` is `tuple[tuple[PhotoRef, ...], ...]`, `photo_candidate_pairs` is `frozenset[tuple[int, int]]`, `blacklisted_refs` is `frozenset[tuple[int, str]]`, and `blacklist_family_count` is `int`.

- [ ] **Step 1: Write failing band-index property tests**

  Assert that every pair at Hamming distance `0..4` is proposed and exactly confirmed, a distance-`5` pair is excluded, full and center variants never share buckets, and results are independent of insertion order. Use fixed hashes plus a deterministic seeded sample over all five bands.

- [ ] **Step 2: Write failing boilerplate tests**

  Assert that near hashes across `5` distinct listings blacklist the entire family, `4` listings do not, either a full or center family invalidates that photo, and repeated URLs/duplicate values within one listing count once. Assert blacklisted photos cannot generate photo candidates or pair evidence.

- [ ] **Step 3: Run tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_phash_index.py tests/test_dedupe.py -k "band or boilerplate or blacklist"`

  Expected: FAIL because near-hash indexing and similarity-aware blacklisting are missing.

- [ ] **Step 4: Implement the five-band index and photo analysis**

  Partition 64 bits deterministically into five uneven bands whose widths sum to 64. Use shared bands only to propose comparisons, confirm with exact Hamming distance `≤ 4`, cluster boilerplate refs independently per variant, and count distinct listing indices. Deduplicate listing pairs and photo keys before returning the analysis.

- [ ] **Step 5: Run index and dedupe tests**

  Run: `venv/bin/pytest -q tests/test_phash_index.py tests/test_dedupe.py -k "band or boilerplate or blacklist"`

  Expected: PASS.

- [ ] **Step 6: Commit the index and blacklist change**

  ```bash
  git add src/greeceapt/scraper/phash_index.py tests/test_phash_index.py src/greeceapt/scraper/dedupe.py tests/test_dedupe.py
  git commit -m "feat: detect near-duplicate boilerplate photos"
  ```

### Task 4: Broaden Candidate Generation With Source Attribution

**Files:**
- Modify: `src/greeceapt/scraper/dedupe.py:110-198`
- Modify: `tests/test_dedupe.py:153-179`

**Interfaces:**
- Consumes: `PhotoIndexAnalysis.photo_candidate_pairs` from Task 3.
- Produces: `CandidateMap = dict[tuple[int, int], frozenset[Literal["photo", "geo", "metadata"]]]` and `candidate_pairs(listings: Sequence[dict], photo_pairs: Iterable[tuple[int, int]] = ()) -> CandidateMap`.

- [ ] **Step 1: Write failing candidate-boundary tests**

  Pin geo inclusion at `1,000 m` and exclusion just above it, including listings separated across four 250 m grid cells; pin area at exactly `3%`; pin symmetric price eligibility at exactly `25%`; and prove a photo pair survives when both geo and metadata miss.

- [ ] **Step 2: Add the multi-source Review Focus test**

  Build one pair found by all three sources and assert the map contains one canonical `(min_index, max_index)` key with `frozenset({"photo", "geo", "metadata"})`, so later scoring/review happens once.

- [ ] **Step 3: Run tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_dedupe.py -k "candidate or geo or metadata_block or source"`

  Expected: FAIL on the 1,000 m, full-tolerance, photo-only, and attribution cases.

- [ ] **Step 4: Implement candidate indexes**

  Expand the existing 250 m geo grid to the necessary cell radius and confirm each proposed pair with Haversine `≤ 1,000`. Replace fixed area/€5,000 adjacency with sorted range indexes that propose every pair capable of satisfying symmetric `3%` area and `25%` price deltas. Union sources in the canonical map; candidate generation remains a superset and makes no merge decision.

- [ ] **Step 5: Run candidate and legacy corpus gates**

  Run: `venv/bin/pytest -q tests/test_dedupe.py -k "candidate or geo or metadata_block or source" tests/test_dedupe_eval.py -k candidate`

  Expected: PASS and all `1,224` historical positives remain candidates.

- [ ] **Step 6: Commit candidate generation**

  ```bash
  git add src/greeceapt/scraper/dedupe.py tests/test_dedupe.py tests/test_dedupe_eval.py
  git commit -m "feat: cover full dedupe candidate tolerances"
  ```

### Task 5: Enforce Conservative Pair Scoring and Explainable Evidence

**Files:**
- Modify: `src/greeceapt/scraper/dedupe.py:200-337`
- Modify: `tests/test_dedupe.py:48-151`

**Interfaces:**
- Consumes: per-listing usable `PhotoFingerprint` records and candidate source labels.
- Produces: frozen `PhotoMatch(a_photo_key: str, b_photo_key: str, distance: int, variant: Literal["full", "center"])`; `greedy_photo_matching(photos_a: Sequence[PhotoRef], photos_b: Sequence[PhotoRef], threshold: int = 5) -> tuple[PhotoMatch, ...]`; expanded frozen `PairEvidence(candidate_sources: frozenset[str], matches: tuple[PhotoMatch, ...], full_matches: int, center_matches: int, photo_match_frac: float, matched_photos: int, min_hamming: int, geo_distance_m: float | None, price_rel_delta: float | None, agreeing_fields: int, conflicting_fields: tuple[str, ...], reason_code: str, confidence: Literal["HIGH", "REVIEW", "NONE"])`; and `evaluate_pair(a: dict, b: dict, photos_a: Sequence[PhotoRef], photos_b: Sequence[PhotoRef], candidate_sources: Iterable[str] = ()) -> PairEvidence`.

- [ ] **Step 1: Write failing decision-table tests**

  Assert: area conflict is `NONE`; price delta `>25%` with matches is `REVIEW`; zero matches is `NONE`; one or two matches is always `REVIEW`; three matches with overlap `≥60%` is `HIGH`; three matches with overlap `≥34%` plus one corroborator is `HIGH`; other matched cases are `REVIEW`. Pin a machine-readable reason code for every branch.

- [ ] **Step 2: Write failing variant and one-to-one evidence tests**

  Assert full↔full and center↔center are eligible, cross-variant similarity is ignored, the lower valid distance selects the winning variant, one photo cannot match twice, and legacy full-only inputs still match full-only without crashing. Assert evidence reports exact full/center match counts and photo keys.

- [ ] **Step 3: Run tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_dedupe.py -k "photo or evidence or confidence or price or area"`

  Expected: FAIL because the current fraction-only policy can mark one-photo pairs `HIGH` and lacks variant evidence/reason codes.

- [ ] **Step 4: Implement the ordered policy and evidence model**

  Generate all eligible photo edges, sort by `(distance, photo_key_a, photo_key_b, variant)` for deterministic greedy one-to-one matching, and apply the seven policy rules in spec order. Keep floor/energy excluded, area as the only hard metadata veto, and year/bedrooms as soft corroborators.

- [ ] **Step 5: Run scoring and isolated historical gates**

  Run: `venv/bin/pytest -q tests/test_dedupe.py -k "photo or evidence or confidence or price or area" tests/test_dedupe_eval.py -k pair_policy`

  Expected: PASS; isolated legacy scoring reports `1,064` automatic and the same `53` formerly automatic one/two-photo pairs as `REVIEW`.

- [ ] **Step 6: Commit pair-policy changes**

  ```bash
  git add src/greeceapt/scraper/dedupe.py tests/test_dedupe.py tests/test_dedupe_eval.py
  git commit -m "feat: require three photos for automatic dedupe"
  ```

### Task 6: Replace Transitive Union With Deterministic Complete-Link Groups

**Files:**
- Modify: `src/greeceapt/scraper/dedupe.py:339-440`
- Modify: `tests/test_dedupe.py:181-230`

**Interfaces:**
- Consumes: all scored `PairEvidence` values keyed by canonical listing pair.
- Produces: `complete_link_groups(listings: Sequence[dict], evidence: Mapping[tuple[int, int], PairEvidence]) -> GroupingResult`; `GroupingResult` contains sorted groups and bridge review records. `find_duplicate_groups()` exposes the final groups plus scored evidence and review records in `DedupeResult`.

- [ ] **Step 1: Write failing bridge and clique tests**

  Assert `A↔B=HIGH`, `B↔C=HIGH`, `A↔C=NONE` never yields `[A,B,C]`; a three-way all-`HIGH` clique does; and a failed bridge emits review evidence without discarding the safe cluster already formed.

- [ ] **Step 2: Add deterministic-order tests**

  Shuffle listings and high edges repeatedly, normalize output by stable URL/ID, and assert identical groups and review reason codes. Pin edge preference order: matched count descending, overlap descending, minimum Hamming ascending, then stable endpoint IDs.

- [ ] **Step 3: Run tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_dedupe.py -k "bridge or complete_link or deterministic or transitive"`

  Expected: FAIL because Union-Find still creates unsafe transitive components.

- [ ] **Step 4: Implement complete-link agglomeration**

  Start with singleton clusters and process sorted `HIGH` edges. Merge two clusters only when every cross-cluster pair exists and is directly `HIGH`; otherwise retain both clusters and append one deduplicated `incomplete_high_clique` review record containing the blocked edge/component.

- [ ] **Step 5: Run grouping and full unit tests**

  Run: `venv/bin/pytest -q tests/test_dedupe.py`

  Expected: PASS with no group whose member pair lacks direct `HIGH` evidence.

- [ ] **Step 6: Commit grouping changes**

  ```bash
  git add src/greeceapt/scraper/dedupe.py tests/test_dedupe.py
  git commit -m "fix: prevent transitive dedupe bridge merges"
  ```

### Task 7: Integrate V2 Dedupe Into the Scraper Merge Pipeline

**Files:**
- Modify: `src/greeceapt/scraper/run_all.py:1-230`
- Modify: `src/greeceapt/scraper/util.py:205-283,335-390`
- Modify: `tests/test_scraper.py`
- Modify: `src/greeceapt/db_helpers/paths.py:20-25` only if a report/audit path constant is required

**Interfaces:**
- Consumes: Tasks 2–6 fingerprint, candidate, scoring, grouping, and evidence APIs.
- Produces: listing fields `photo_fingerprints`, compatibility fields `photo_url_hashes` and `image_hashes`, versioned review JSON, and merge-audit records containing `dedupe_policy_version="phash-v2-conservative"` plus authorizing pair evidence.

- [ ] **Step 1: Write failing merge-pipeline integration tests**

  With temporary per-site JSON and a fake async image client, assert the merge creates v2 fingerprints, retains full legacy hash fields, writes the cache atomically, and merges only a three-photo `HIGH` pair. Assert one/two-photo, price-gap, area-conflict, and bridge fixtures are never absorbed.

- [ ] **Step 2: Write failing audit/review tests**

  Assert review records include URLs/IDs, all candidate sources once, full/center counts, per-photo winning variants, overlap, minimum Hamming, geo/price values, corroborators/conflicts, and reason code. Assert every absorbed row has policy version and the exact direct evidence that authorized its group; survivor/date/photo preference behavior remains unchanged.

- [ ] **Step 3: Run integration tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_scraper.py -k "dedupe or fingerprint or merge or review or audit"`

  Expected: FAIL because `run_all` still writes only legacy hashes and unversioned merge records.

- [ ] **Step 4: Wire the new pipeline into `_merge()`**

  Download/upgrade fingerprints first, populate all three listing fields, persist cache, run `find_duplicate_groups`, and serialize evidence without changing scraper subprocesses or production path behavior. Keep `--merge-only` network-free when all needed v2 fingerprints exist; missing center variants provide only legacy full evidence unless a network-enabled normal run upgrades them.

- [ ] **Step 5: Preserve merge behavior and improve observability**

  Keep current survivor and field/date rules. Log candidate counts by source, blacklisted families/photos, scored/high/review counts, blocked bridge count, fingerprint time, pair-scoring time, and total CPU dedupe time. Remove stale review output when a completed run has zero reviews so an old file cannot masquerade as current output.

- [ ] **Step 6: Run integration and regression suites**

  Run: `venv/bin/pytest -q tests/test_scraper.py tests/test_dedupe.py tests/test_fingerprints.py tests/test_phash_index.py`

  Expected: PASS, including existing merge provenance and publication-date tests.

- [ ] **Step 7: Commit only dedupe-related integration hunks**

  ```bash
  git add -p src/greeceapt/scraper/run_all.py src/greeceapt/scraper/util.py src/greeceapt/db_helpers/paths.py tests/test_scraper.py
  git commit -m "feat: make conservative dedupe the scraper default"
  ```

### Task 8: Prove Corpus Coverage, Runtime, and PR Readiness

**Files:**
- Modify: `src/greeceapt/scraper/dedupe_eval.py`
- Modify: `tests/test_dedupe_eval.py`
- Create: `docs/superpowers/reports/2026-10-31-watermark-resistant-dedupe.md`

**Interfaces:**
- Consumes: final V2 pipeline plus the checked-in frozen live and historical-positive fixtures.
- Produces: `EvaluationReport` with input counts, historical-positive candidate/automatic/review/none coverage, live expected outcomes, hard-negative outcomes, candidates by source, fingerprint/scoring/total timings, peak candidate count, and baseline/runtime ratio; CLI flags `--positive-fixture`, `--live-fixture`, and `--output`.

- [ ] **Step 1: Add failing end-to-end acceptance tests**

  Assert all `1,224` historical positives remain candidates; isolated policy remains exactly `1,064` automatic and `53` demoted; all six frozen live strong pairs are automatic; the one frozen borderline pair is review-only; constructed shared-watermark, stock-photo, one-photo, bridge, area-conflict, and price-gap negatives are never automatic. Label archive metrics `historical-positive coverage`, never accuracy or precision.

- [ ] **Step 2: Run acceptance tests and verify failure**

  Run: `venv/bin/pytest -q tests/test_dedupe_eval.py`

  Expected: FAIL until the final evaluator and metrics are implemented.

- [ ] **Step 3: Implement the evaluator and benchmark**

  Measure network-excluded fingerprint time separately from candidate/pair scoring. In the same process and on the same historical fixture, time the frozen Task 1 legacy reference and the V2 implementation with identical warm-up and repeat counts; fail the acceptance command if the median V2/legacy ratio exceeds `2.0`. End-to-end historical automatic coverage may be lower than `1,064` when blacklist or complete-link safety rules explain the demotion, but the report must enumerate those reason codes.

- [ ] **Step 4: Generate the checked-in comparison report**

  Run:

  ```bash
  PYTHONPATH=src venv/bin/python -m greeceapt.scraper.dedupe_eval \
    --positive-fixture tests/fixtures/dedupe_historical_positives_2026-08.json \
    --live-fixture tests/fixtures/dedupe_live_2026-10-31.json \
    --output docs/superpowers/reports/2026-10-31-watermark-resistant-dedupe.md
  ```

  Expected: exit `0`; report shows `1,224` candidates, isolated `1,064 / 53`, live `6 HIGH / 1 REVIEW`, zero automatic hard negatives, and runtime ratio `≤ 2.0`.

- [ ] **Step 5: Run complete verification from the repository root**

  Run:

  ```bash
  venv/bin/pytest -q
  git diff --check main...HEAD
  git status --short
  ```

  Expected: all tests pass; no whitespace errors; status contains only intentional PR files plus the user's pre-existing unrelated dirty/untracked files. Do not claim those unrelated files as part of the PR.

- [ ] **Step 6: Commit the evaluator and report**

  ```bash
  git add src/greeceapt/scraper/dedupe_eval.py tests/test_dedupe_eval.py docs/superpowers/reports/2026-10-31-watermark-resistant-dedupe.md
  git commit -m "test: verify dedupe safety and corpus coverage"
  ```

- [ ] **Step 7: Perform branch review and open the PR**

  Review `git diff main...HEAD` specifically for false-merge paths, cache rollback compatibility, accidental production-data writes, and unrelated dirty hunks. Push `codex/dedupe-hardening`, open a PR summarizing the policy change and measured coverage/runtime, and attach the comparison report. Do not merge without user approval.
