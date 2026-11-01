"""Reproducible, sanitized evaluation helpers for listing dedupe.

The checked-in corpora contain only fields consumed by entity resolution. Raw
listing text and remote URLs deliberately remain outside the repository.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Sequence

from PIL import Image


_FORBIDDEN_FIELDS = frozenset({"description", "address", "address_raw"})
_EARTH_RADIUS_M = 6_371_000.0
_DEG_LAT_M = 111_320.0
_GEO_CELL_M = 250.0


@dataclass(frozen=True)
class PositivePair:
    kept: dict
    absorbed: dict


def load_positive_pairs(paths: Sequence[Path]) -> list[PositivePair]:
    """Load archived ``kept``/``absorbed`` records without modifying source data."""
    pairs: list[PositivePair] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"positive-pair archive must be a list: {path}")
        for record in payload:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("kept"), dict)
                or not isinstance(record.get("absorbed"), dict)
            ):
                raise ValueError(f"invalid positive-pair record in {path}")
            pairs.append(
                PositivePair(
                    kept=copy.deepcopy(record["kept"]),
                    absorbed=copy.deepcopy(record["absorbed"]),
                )
            )
    return pairs


def _validate_sanitized(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_FIELDS:
                raise ValueError(f"forbidden field in evaluation fixture: {key}")
            _validate_sanitized(child)
        return
    if isinstance(value, list):
        for child in value:
            _validate_sanitized(child)
        return
    if isinstance(value, str) and (
        "http://" in value.lower() or "https://" in value.lower()
    ):
        raise ValueError("remote URL in evaluation fixture")


def load_live_fixture(path: Path) -> dict:
    """Load a frozen live corpus after enforcing its privacy/stability contract."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("live fixture must be a JSON object")
    _validate_sanitized(payload)
    return payload


def _legacy_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _legacy_coords(listing: dict) -> tuple[float, float] | None:
    lat = _legacy_float(listing.get("latitude"))
    lon = _legacy_float(listing.get("longitude"))
    return None if lat is None or lon is None else (lat, lon)


def _legacy_hashes(listing: dict, blacklist: frozenset[int] = frozenset()) -> list[int]:
    hashes: list[int] = []
    for value in listing.get("image_hashes") or []:
        try:
            parsed = int(str(value), 16)
        except (TypeError, ValueError):
            continue
        if parsed not in blacklist:
            hashes.append(parsed)
    return hashes


def legacy_candidate_pairs(listings: Sequence[dict]) -> set[tuple[int, int]]:
    """Frozen copy of the pre-V2 geo and fixed metadata blocking rules."""
    geo_cells: dict[tuple[int, int], list[int]] = {}
    meta_cells: dict[tuple[int, int], list[int]] = {}
    for index, listing in enumerate(listings):
        coords = _legacy_coords(listing)
        if coords is not None:
            lat, lon = coords
            cell_lat = int(lat * _DEG_LAT_M // _GEO_CELL_M)
            lon_m = _DEG_LAT_M * math.cos(math.radians(lat))
            cell_lon = int(lon * lon_m // _GEO_CELL_M)
            geo_cells.setdefault((cell_lat, cell_lon), []).append(index)
        area = _legacy_float(listing.get("area_sqm"))
        price = _legacy_float(listing.get("price_eur"))
        if area is not None and area > 0 and price is not None and price > 0:
            meta_cells.setdefault((int(round(area)), int(price // 5_000)), []).append(index)

    pairs: set[tuple[int, int]] = set()
    for (cell_lat, cell_lon), bucket in geo_cells.items():
        nearby = [
            index
            for delta_lat in (-1, 0, 1)
            for delta_lon in (-1, 0, 1)
            for index in geo_cells.get((cell_lat + delta_lat, cell_lon + delta_lon), ())
        ]
        for left in bucket:
            for right in nearby:
                if left != right:
                    pairs.add(tuple(sorted((left, right))))
    for (area_key, price_key), bucket in meta_cells.items():
        nearby = [
            index
            for area_delta in (-1, 0, 1)
            for price_delta in (-1, 0, 1)
            for index in meta_cells.get((area_key + area_delta, price_key + price_delta), ())
        ]
        for left in bucket:
            for right in nearby:
                if left != right:
                    pairs.add(tuple(sorted((left, right))))
    return pairs


def _legacy_photo_matching(hashes_a: Sequence[int], hashes_b: Sequence[int]) -> tuple[int, int]:
    if not hashes_a or not hashes_b:
        return 0, 64
    edges: list[tuple[int, int, int]] = []
    minimum = 64
    for left, hash_a in enumerate(hashes_a):
        for right, hash_b in enumerate(hashes_b):
            distance = (hash_a ^ hash_b).bit_count()
            minimum = min(minimum, distance)
            if distance < 5:
                edges.append((distance, left, right))
    used_a: set[int] = set()
    used_b: set[int] = set()
    matches = 0
    for _distance, left, right in sorted(edges):
        if left not in used_a and right not in used_b:
            used_a.add(left)
            used_b.add(right)
            matches += 1
    return matches, minimum


def _legacy_metadata(a: dict, b: dict) -> tuple[int, tuple[str, ...]]:
    agreeing = 0
    conflicts: list[str] = []
    area_a = _legacy_float(a.get("area_sqm"))
    area_b = _legacy_float(b.get("area_sqm"))
    if area_a is not None and area_b is not None and max(area_a, area_b) > 0:
        if abs(area_a - area_b) / max(area_a, area_b) > 0.03:
            conflicts.append("area_sqm")
        else:
            agreeing += 1
    for field in ("year_built", "bedrooms"):
        value_a = _legacy_float(a.get(field))
        value_b = _legacy_float(b.get(field))
        if value_a is not None and value_b is not None and int(value_a) == int(value_b):
            agreeing += 1
    return agreeing, tuple(conflicts)


def _legacy_evaluate_rows(a: dict, b: dict, blacklist: frozenset[int] = frozenset()) -> dict:
    hashes_a = _legacy_hashes(a, blacklist)
    hashes_b = _legacy_hashes(b, blacklist)
    matched, minimum = _legacy_photo_matching(hashes_a, hashes_b)
    denominator = min(len(hashes_a), len(hashes_b))
    overlap = matched / denominator if denominator else 0.0
    agreeing, conflicts = _legacy_metadata(a, b)
    confidence = "NONE"
    if conflicts:
        confidence = "NONE"
    elif overlap >= 0.60:
        confidence = "HIGH"
    elif overlap >= 0.34 and agreeing >= 1:
        confidence = "HIGH"
    elif overlap >= 0.34 or matched > 0:
        confidence = "REVIEW"
    price_a = _legacy_float(a.get("price_eur"))
    price_b = _legacy_float(b.get("price_eur"))
    price_delta = None
    if price_a and price_b and max(price_a, price_b) > 0:
        price_delta = abs(price_a - price_b) / max(price_a, price_b)
        if confidence != "NONE" and price_delta > 0.25:
            confidence = "REVIEW"
    return {
        "confidence": confidence,
        "matched_photos": matched,
        "photo_match_frac": round(overlap, 4),
        "min_hamming": minimum,
        "agreeing_fields": agreeing,
        "conflicting_fields": conflicts,
        "price_rel_delta": None if price_delta is None else round(price_delta, 4),
    }


def legacy_evaluate_pair(pair: PositivePair) -> dict:
    return _legacy_evaluate_rows(pair.kept, pair.absorbed)


def evaluate_legacy_pair_policy(pairs: Sequence[PositivePair]) -> dict[str, int]:
    metrics = {
        "positive_pairs": len(pairs),
        "candidate_pairs": 0,
        "high": 0,
        "review": 0,
        "none": 0,
        "high_with_fewer_than_three_matches": 0,
        "future_automatic": 0,
    }
    for pair in pairs:
        metrics["candidate_pairs"] += (0, 1) in legacy_candidate_pairs(
            [pair.kept, pair.absorbed]
        )
        evidence = legacy_evaluate_pair(pair)
        confidence = str(evidence["confidence"]).lower()
        metrics[confidence] += 1
        if evidence["confidence"] == "HIGH":
            if int(evidence["matched_photos"]) < 3:
                metrics["high_with_fewer_than_three_matches"] += 1
            else:
                metrics["future_automatic"] += 1
    return metrics


class _LegacyUnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self.parent[max(root_left, root_right)] = min(root_left, root_right)


def legacy_find_duplicate_groups(listings: Sequence[dict]) -> dict:
    """Frozen pre-V2 CPU pipeline used only as a behavior/runtime reference."""
    counts: dict[int, int] = {}
    for listing in listings:
        for value in set(_legacy_hashes(listing)):
            counts[value] = counts.get(value, 0) + 1
    blacklist = frozenset(value for value, count in counts.items() if count >= 5)
    union_find = _LegacyUnionFind(len(listings))
    review_pairs: list[dict] = []
    for left, right in sorted(legacy_candidate_pairs(listings)):
        if not _legacy_hashes(listings[left], blacklist) or not _legacy_hashes(
            listings[right], blacklist
        ):
            continue
        evidence = _legacy_evaluate_rows(listings[left], listings[right], blacklist)
        if evidence["confidence"] == "HIGH":
            union_find.union(left, right)
        elif evidence["confidence"] == "REVIEW":
            review_pairs.append(
                {
                    "a_id": listings[left].get("id"),
                    "b_id": listings[right].get("id"),
                    **evidence,
                }
            )

    components: dict[int, list[int]] = {}
    for index in range(len(listings)):
        components.setdefault(union_find.find(index), []).append(index)
    groups: list[list[int]] = []
    for members in components.values():
        valid = all(
            not _legacy_metadata(listings[members[i]], listings[members[j]])[1]
            for i in range(len(members))
            for j in range(i + 1, len(members))
        )
        groups.extend([sorted(members)] if valid else [[member] for member in sorted(members)])
    return {"groups": sorted(groups, key=lambda group: group[0]), "review_pairs": review_pairs}


@dataclass(frozen=True)
class EvaluationReport:
    historical_positive_pairs: int = 0
    historical_positive_candidates: int = 0
    isolated_automatic: int = 0
    demoted_low_photo: int = 0
    isolated_review: int = 0
    isolated_none: int = 0
    end_to_end_grouped_positives: int = 0
    end_to_end_review: int = 0
    end_to_end_none_or_missing: int = 0
    end_to_end_demotion_reasons: dict[str, int] = field(default_factory=dict)
    live_high_pairs: int = 0
    live_review_pairs: int = 0
    hard_negative_cases: int = 0
    hard_negative_automatic: int = 0
    candidate_pairs_by_source: dict[str, int] = field(default_factory=dict)
    fingerprint_seconds: float = 0.0
    pair_scoring_seconds: float = 0.0
    total_v2_seconds: float = 0.0
    legacy_seconds: float = 0.0
    runtime_ratio: float = 0.0
    peak_candidate_pairs: int = 0

    @classmethod
    def empty(cls) -> EvaluationReport:
        return cls()


def _production_pair_evidence(pair: PositivePair):
    from greeceapt.scraper import dedupe
    from greeceapt.scraper.phash_index import analyze_photo_index

    listings = [pair.kept, pair.absorbed]
    analysis = analyze_photo_index(listings)
    candidates = dedupe.candidate_pairs(listings, analysis.photo_candidate_pairs)
    sources = candidates.get((0, 1), frozenset())
    evidence = dedupe.evaluate_pair(
        pair.kept,
        pair.absorbed,
        analysis.usable_photos_by_listing[0],
        analysis.usable_photos_by_listing[1],
        sources,
    )
    return (0, 1) in candidates, evidence


def _hard_negative_violations() -> tuple[int, int]:
    from greeceapt.scraper import dedupe
    from greeceapt.scraper.fingerprints import PhotoFingerprint

    def far(label: str) -> int:
        import hashlib
        return int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)

    def listing(name: str, photos: list[tuple[int, int | None]], **overrides) -> dict:
        row = {
            "url": name,
            "photo_fingerprints": {
                f"p{index}": PhotoFingerprint(
                    "phash-v2" if center is not None else "phash-v1",
                    full,
                    center,
                ).to_json()
                for index, (full, center) in enumerate(photos)
            },
            "latitude": 37.9838,
            "longitude": 23.7275,
            "area_sqm": 50.0,
            "price_eur": 100_000.0,
            "year_built": 1980,
            "bedrooms": 2,
        }
        row.update(overrides)
        return row

    shared_watermark = [
        listing(f"watermark-{index}", [(far(f"full-{index}"), 0)])
        for index in range(5)
    ]
    stock_photo = [
        listing(f"stock-{index}", [(0, far(f"center-{index}"))])
        for index in range(5)
    ]
    one_photo = [listing("one-a", [(1, 2)]), listing("one-b", [(1, 2)])]
    bridge = [
        listing("bridge-a", [(far(label), None) for label in ("a1", "a2", "a3")]),
        listing("bridge-b", [(far(label), None) for label in ("a1", "a2", "a3", "b1", "b2", "b3")]),
        listing("bridge-c", [(far(label), None) for label in ("b1", "b2", "b3")]),
    ]
    shared = [(far(label), None) for label in ("s1", "s2", "s3")]
    area_conflict = [
        listing("area-a", shared, area_sqm=50.0),
        listing("area-b", shared, area_sqm=80.0),
    ]
    price_gap = [
        listing("price-a", shared, price_eur=100_000.0),
        listing("price-b", shared, price_eur=140_000.0),
    ]

    violations = 0
    for case_name, rows in (
        ("shared-watermark", shared_watermark),
        ("stock-photo", stock_photo),
        ("one-photo", one_photo),
        ("bridge", bridge),
        ("area-conflict", area_conflict),
        ("price-gap", price_gap),
    ):
        result = dedupe.find_duplicate_groups(rows)
        if case_name == "bridge":
            violations += any(len(group) == 3 for group in result.groups)
        else:
            violations += any(len(group) > 1 for group in result.groups)
    return 6, violations


def _fingerprint_benchmark() -> float:
    from greeceapt.scraper.fingerprints import fingerprint_image

    images = [Image.new("RGB", (128, 96), color=(index * 7 % 256, index * 13 % 256, index * 19 % 256))
              for index in range(32)]
    started = time.perf_counter()
    for image in images:
        fingerprint_image(image)
    return time.perf_counter() - started


def evaluate_corpora(
    positive_fixture: Path,
    live_fixture: Path,
    *,
    benchmark_repeats: int = 3,
) -> EvaluationReport:
    from greeceapt.scraper import dedupe

    positive_payload = load_live_fixture(positive_fixture)
    live_payload = load_live_fixture(live_fixture)
    pairs = [
        PositivePair(record["kept"], record["absorbed"])
        for record in positive_payload["positive_pairs"]
    ]
    legacy_metrics = evaluate_legacy_pair_policy(pairs)

    candidate_count = 0
    isolated_counts = Counter()
    for pair in pairs:
        is_candidate, evidence = _production_pair_evidence(pair)
        candidate_count += is_candidate
        isolated_counts[evidence.confidence] += 1

    historical_rows = [row for pair in pairs for row in (pair.kept, pair.absorbed)]
    legacy_times: list[float] = []
    v2_times: list[float] = []
    legacy_find_duplicate_groups(historical_rows[:64])
    dedupe.find_duplicate_groups(historical_rows[:64])
    v2_result = None
    for _ in range(max(1, benchmark_repeats)):
        started = time.perf_counter()
        legacy_find_duplicate_groups(historical_rows)
        legacy_times.append(time.perf_counter() - started)
        started = time.perf_counter()
        v2_result = dedupe.find_duplicate_groups(historical_rows)
        v2_times.append(time.perf_counter() - started)
    assert v2_result is not None

    member_group = {
        member: group_index
        for group_index, group in enumerate(v2_result.groups)
        for member in group
    }
    grouped_positives = 0
    end_review = 0
    end_none = 0
    demotion_reasons: Counter[str] = Counter()
    for pair_index in range(len(pairs)):
        left, right = pair_index * 2, pair_index * 2 + 1
        if member_group[left] == member_group[right]:
            grouped_positives += 1
            continue
        evidence = v2_result.pair_evidence.get((left, right))
        if evidence is not None and evidence.confidence == "REVIEW":
            end_review += 1
            demotion_reasons[evidence.reason_code] += 1
        else:
            end_none += 1
            demotion_reasons["blacklisted_or_no_direct_evidence"] += 1

    live_result = dedupe.find_duplicate_groups(live_payload["listings"])
    live_high = sum(max(0, len(group) - 1) for group in live_result.groups)
    live_reviews = sum(
        review.get("reason_code") != "incomplete_high_clique"
        for review in live_result.review_pairs
    )
    hard_cases, hard_violations = _hard_negative_violations()

    legacy_seconds = statistics.median(legacy_times)
    v2_seconds = statistics.median(v2_times)
    return EvaluationReport(
        historical_positive_pairs=len(pairs),
        historical_positive_candidates=candidate_count,
        isolated_automatic=isolated_counts["HIGH"],
        demoted_low_photo=legacy_metrics["high_with_fewer_than_three_matches"],
        isolated_review=isolated_counts["REVIEW"],
        isolated_none=isolated_counts["NONE"],
        end_to_end_grouped_positives=grouped_positives,
        end_to_end_review=end_review,
        end_to_end_none_or_missing=end_none,
        end_to_end_demotion_reasons=dict(sorted(demotion_reasons.items())),
        live_high_pairs=live_high,
        live_review_pairs=live_reviews,
        hard_negative_cases=hard_cases,
        hard_negative_automatic=hard_violations,
        candidate_pairs_by_source=v2_result.candidate_pairs_by_source,
        fingerprint_seconds=_fingerprint_benchmark(),
        pair_scoring_seconds=v2_result.pair_scoring_seconds,
        total_v2_seconds=v2_seconds,
        legacy_seconds=legacy_seconds,
        runtime_ratio=v2_seconds / legacy_seconds if legacy_seconds else float("inf"),
        peak_candidate_pairs=v2_result.candidate_pairs,
    )


def render_markdown(report: EvaluationReport) -> str:
    reasons = "\n".join(
        f"- `{reason}`: {count}"
        for reason, count in report.end_to_end_demotion_reasons.items()
    ) or "- none"
    sources = ", ".join(
        f"{source}={count}" for source, count in report.candidate_pairs_by_source.items()
    ) or "none"
    return f"""# Watermark-Resistant Dedupe Evaluation

## Historical-positive coverage

- Known positive records: {report.historical_positive_pairs}
- Candidate records: {report.historical_positive_candidates}
- Isolated policy: {report.isolated_automatic} automatic, {report.demoted_low_photo} formerly automatic one/two-photo records demoted
- Isolated final states: HIGH={report.isolated_automatic}, REVIEW={report.isolated_review}, NONE={report.isolated_none}
- End-to-end grouped positives: {report.end_to_end_grouped_positives}
- End-to-end review: {report.end_to_end_review}
- End-to-end none/missing: {report.end_to_end_none_or_missing}

Demotion reasons:
{reasons}

## Live and safety fixtures

- Frozen live sample: {report.live_high_pairs} HIGH / {report.live_review_pairs} REVIEW
- Hard-negative violations: {report.hard_negative_automatic} / {report.hard_negative_cases}

## Runtime

- Candidate pairs by source: {sources}
- Peak candidate pairs: {report.peak_candidate_pairs}
- 32-image fingerprint time: {report.fingerprint_seconds:.4f}s
- Pair-scoring time: {report.pair_scoring_seconds:.4f}s
- Legacy total: {report.legacy_seconds:.4f}s
- V2 total: {report.total_v2_seconds:.4f}s
- V2 / legacy ratio: {report.runtime_ratio:.3f}
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate conservative listing dedupe")
    parser.add_argument("--positive-fixture", type=Path, required=True)
    parser.add_argument("--live-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark-repeats", type=int, default=3)
    args = parser.parse_args(argv)
    report = evaluate_corpora(
        args.positive_fixture,
        args.live_fixture,
        benchmark_repeats=args.benchmark_repeats,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(report), encoding="utf-8")
    gates = (
        report.historical_positive_pairs == 1_224,
        report.historical_positive_candidates == 1_224,
        report.isolated_automatic == 1_064,
        report.demoted_low_photo == 53,
        report.live_high_pairs == 6,
        report.live_review_pairs == 1,
        report.hard_negative_automatic == 0,
        report.runtime_ratio <= 2.0,
    )
    return 0 if all(gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
