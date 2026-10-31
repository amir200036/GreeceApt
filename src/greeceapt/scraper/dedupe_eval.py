"""Reproducible, sanitized evaluation helpers for listing dedupe.

The checked-in corpora contain only fields consumed by entity resolution. Raw
listing text and remote URLs deliberately remain outside the repository.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


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
