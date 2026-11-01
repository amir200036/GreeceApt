# Watermark-Resistant, Conservative Listing Dedupe Design

## Intent

Strengthen listing entity resolution so a wrong merge is substantially less likely than a missed duplicate, while keeping the scraper local, fast, explainable, and free of an additional vision model. Watermarks are neutralized only for fingerprint computation; downloaded originals and listing photo URLs are never altered or republished.

## Current evidence

The current pipeline downloads listing photos, calculates one full-image 64-bit pHash per URL, creates candidate listing pairs from a 250 m geo grid plus narrow area/price buckets, scores photo overlap and metadata, and joins all `HIGH` edges through Union-Find.

The live one-page check produced 64 raw listings, six automatic merges, and one review pair. All six automatic merges were exceptionally strong: 6–25 matched photos, 100% overlap on the smaller photo set, identical area and price, and 0.1–28.3 m coordinate distance.

The existing relevant suite has 52 passing tests. The historical positive archives contain 1,224 previously confirmed duplicate pairs. The current code proposes all 1,224 as candidates. In an isolated pair-policy measurement using each archived `kept`/`absorbed` pair's legacy full hashes, without corpus-wide boilerplate removal or grouping, requiring at least three matched photos would keep 1,064 historical pairs eligible for automatic merging and move 53 pairs that are currently `HIGH` but have only one or two matches out of the automatic path. The remaining historical pairs are already `REVIEW` or `NONE`.

Three reproducible hazards motivate the change:

- one matched photo against a one-photo listing produces `photo_match_frac=1.0` and `HIGH`, even with no metadata;
- `A↔B=HIGH` and `B↔C=HIGH` merges all three even when `A↔C=NONE`;
- two identical-photo listings can be omitted from candidate scoring when coordinates are far apart and their still-valid relative price gap crosses more than one €5,000 bucket.

## Goals

- Require sufficient absolute photo evidence before any automatic merge.
- Make photo matching resistant to common corner, border, and portal watermarks without removing or reconstructing image content.
- Detect near-identical boilerplate photos, not only byte/ pHash-identical ones.
- Guarantee that transitive graph bridges cannot merge listings lacking direct evidence.
- Generate candidate pairs across the full declared geo, area, and relative-price tolerances, and directly from photo similarity.
- Preserve deterministic, explainable `HIGH`, `REVIEW`, and `NONE` decisions.
- Keep existing JSON stores and the old pHash cache readable during migration.
- Add an evaluation report that exposes automatic-merge recall on known positives, review volume, hard-negative behavior, candidate counts, and runtime.

## Non-goals

- Do not remove watermarks from stored or displayed images.
- Do not use generative inpainting, OCR, logo detection, CLIP, DINO, or another model.
- Do not change apartment scoring, Qwen condition scoring, scraper search filters, or database schemas.
- Do not promise a measured false-positive rate until a labeled hard-negative set exists.
- Do not automatically merge weak one- or two-photo pairs merely to preserve historical recall.

## Constraints

- A false merge is more damaging than a missed duplicate; ambiguous cases go to review.
- The full scan may contain roughly 1,000 listings and around 10,000–15,000 photos.
- Fingerprint creation remains deterministic and local after image download.
- No original image file is rewritten. Crops exist only in memory.
- Cache writes remain atomic, and an interrupted migration must leave a readable cache.
- Existing unrelated dirty worktree changes must not be overwritten or bundled into the PR.
- Delivery will be through a dedicated `codex/` branch and pull request.

## Approaches considered

### 1. Conservative multi-fingerprint pHash hardening — selected

Keep the current local pHash architecture, add full and center-crop fingerprints, stronger absolute evidence, similarity-aware boilerplate removal, complete-link grouping, and broader candidate generation. This gives the best reliability/speed balance and preserves explainability.

### 2. Explicit watermark/logo detection

Detect text or logos, construct masks, and hash only unmasked pixels. This can handle variable watermark placement but introduces detector errors, per-site maintenance, and substantially more implementation complexity.

### 3. Local image embeddings

Add CLIP/DINO-style embeddings alongside pHash. This is more transformation-resistant but adds another model, memory/runtime cost, and a new calibration surface. It conflicts with the current single-model, fast-local direction.

## Fingerprint contract

Each successfully decoded photo receives a versioned fingerprint:

```text
PhotoFingerprint(
  version="phash-v2",
  full=<64-bit pHash of RGB image>,
  center=<64-bit pHash after cropping 12.5% from every edge>,
)
```

The center crop retains 75% of width and height. It removes the common corner/border watermark region without inventing pixels. Matching compares `full↔full` and `center↔center`; it never cross-compares variants. A photo-pair distance is the smaller valid distance. A pair is similar when that distance is below the existing Hamming threshold of 5.

The persistent pHash cache becomes a versioned URL-to-record mapping. The loader accepts both the old string value and the new record. A legacy string supplies only `full`; because it lacks `center`, the next network-enabled scrape lazily re-downloads and upgrades that URL. The cache is atomically replaced only after valid JSON has been written.

Listing JSON gains `photo_fingerprints`, keyed by photo URL. The existing `photo_url_hashes` and `image_hashes` remain for one compatibility period and continue to contain full hashes. New dedupe code prefers fingerprints and falls back to legacy full hashes when reading old archives.

## Similarity-aware boilerplate detection

The current blacklist counts only exact pHash integers. V2 groups near hashes within Hamming distance 4 separately for the `full` and `center` variants, counts distinct listing IDs per group, and marks a photo unusable when either of its variants belongs to a group appearing in at least five listings.

To avoid quadratic comparison, 64-bit hashes are partitioned into five deterministic bands. Any two hashes within four bit differences must share at least one unchanged band. A band index proposes hash pairs; exact Hamming distance confirms them. Union-Find is acceptable here because the output is only a conservative boilerplate exclusion, not a listing merge.

The same confirmed near-hash index also proposes listing candidate pairs, after boilerplate fingerprints have been removed.

## Candidate generation

Candidate pairs are the union of three independent sources:

1. **Photo candidates:** listings with at least one non-blacklisted photo pair below Hamming 5, proposed by the five-band index.
2. **Geo candidates:** listings within 1,000 m, found through the existing 250 m grid expanded to the required cell radius and confirmed with Haversine distance.
3. **Metadata candidates:** listings whose area and price ranges can satisfy the actual scoring tolerances. Area uses the symmetric 3% relative range; price uses the symmetric 25% relative range rather than fixed adjacent €5,000 buckets.

Candidate generation is deliberately a superset. It never proves a duplicate; only pair evaluation can do that. Pair counts and source attribution are logged so performance regressions are visible.

## Pair evaluation policy

One-to-one photo matching remains, but a listing pair cannot be `HIGH` with fewer than three matched photos.

Decision order:

1. A hard area conflict produces `NONE`.
2. A relative price gap above 25% can never be automatic; evidence is `REVIEW` when photos match.
3. Zero matches produces `NONE`.
4. One or two matches produces `REVIEW`, regardless of overlap fraction or metadata.
5. Three or more matches with overlap at least 60% produces `HIGH` even when metadata is sparse.
6. Three or more matches with overlap at least 34% plus at least one corroborator produces `HIGH`.
7. Remaining matched pairs produce `REVIEW`.

The existing area veto, soft year/bedroom corroboration, and exclusion of noisy floor/energy disagreements remain unchanged until new labeled evidence supports different rules.

Every review record includes URLs, candidate sources, full/center match counts, the variant that won each photo match, overlap, minimum distance, geo distance, price delta, corroborators, conflicts, and a machine-readable reason code.

## Grouping policy

Raw transitive Union-Find is replaced by deterministic complete-link agglomeration:

- start with singleton listing clusters;
- sort `HIGH` edges by strongest evidence: matched-photo count descending, overlap descending, minimum Hamming ascending, then stable URL order;
- merge two clusters only when every cross-cluster listing pair is directly `HIGH`;
- if a bridge edge cannot merge its clusters, retain the safe clusters and emit the bridge/component for review.

This permits genuine multi-portal groups while preventing a middle listing from joining unrelated endpoints.

## Merge behavior

The survivor selection and field merge rules remain unchanged. Photo URLs continue to prefer the longer listing set for this change; changing photo-set union semantics is a separate concern. The merge audit file retains the pre-merge rows and adds the policy version plus complete pair evidence used to authorize the merge.

## Evaluation and acceptance gates

Automated tests cover:

- one photo against many is never `HIGH`;
- two matches are review-only and three strong matches can be `HIGH`;
- a watermark overlay in an outer 12.5% region changes the full hash but preserves a center-crop match;
- center-crop matching remains one-to-one and cannot double-count a photo;
- near-identical common photos are blacklisted across five or more listings;
- a three-node bridge with `A↔C=NONE` cannot become one group;
- geo candidates cover boundary cells through 1,000 m;
- metadata candidates cover the full 3% area and 25% price tolerance;
- photo similarity can propose a pair even when both geo and metadata blocks miss;
- legacy string cache entries load safely and upgrade atomically;
- existing merge provenance and date behavior remain intact.

Corpus checks run against both historical positive archives and the latest live sample:

- all 1,224 known positive pairs must remain candidate pairs;
- an isolated pair-policy check, using legacy full hashes and no corpus-wide blacklist/grouping, must classify the same 1,064 pairs as automatic and demote the same 53 currently automatic one/two-photo positives to `REVIEW`;
- a separate end-to-end corpus check applies similarity-aware boilerplate removal and complete-link grouping, reports its final automatic/review/none coverage, and must never be forced to preserve 1,064 automatic merges when a new safety rule supplies evidence to demote one;
- all six strong live pairs must remain automatic;
- the existing live borderline pair must remain review-only;
- constructed hard negatives for shared watermark/stock photos, one-photo overlap, bridge chains, area conflicts, and large price gaps must never auto-merge.

The current 64-listing live sample is frozen as a compact test fixture at `tests/fixtures/dedupe_live_2026-10-31.json` during the first implementation task, sourced from `/tmp/greeceapt-scraper-check.DHzsOo`. The fixture contains only stable synthetic listing IDs, fields consumed by dedupe, and full legacy hashes/fingerprints needed to reproduce the six automatic pairs and one review pair. It excludes descriptions, addresses, remote image URLs, and unrelated scraper payload fields. Tests must not depend on the temporary directory or on a fresh network scrape.

A benchmark records download-excluded fingerprint time, candidate count by source, pair-scoring time, peak candidate set size, and total dedupe time. The CPU-only dedupe stage must remain practical for 1,000 listings; the default acceptance limit is no more than twice the current dedupe CPU time on the same archived corpus, excluding network downloads. If that limit fails, optimize the band/range indexes rather than weakening safety rules.

Because the archives contain positives but no trustworthy negative labels, the report must call automatic recall “historical-positive coverage,” not accuracy or precision. A future manually labeled hard-negative set is required before claiming a false-positive rate.

## Failure handling and rollout

- Decode or download failures omit that photo fingerprint and never synthesize evidence.
- If a listing has too few usable photos, it can remain unique or produce review evidence; it cannot be auto-merged from one or two matches.
- Cache corruption falls back to an empty cache and a rebuild, matching current behavior.
- The PR includes a comparison report for the two archives and the latest live sample.
- The old cache and legacy listing hash fields remain readable for rollback. No production JSON or database is deleted by the migration.

## Builder decisions left open

- Exact module boundaries for the fingerprint band index and corpus-report CLI may follow existing project conventions.
- The implementation may use small frozen dataclasses or typed dictionaries for in-memory fingerprints, provided JSON serialization stays explicit and versioned.
- Logging wording and report layout are implementation details; all required evidence fields and metrics must be present.
