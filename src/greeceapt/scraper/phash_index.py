"""Subquadratic near-pHash indexing and corpus boilerplate analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from greeceapt.scraper.fingerprints import PhotoFingerprint


HashVariant = Literal["full", "center"]


@dataclass(frozen=True)
class PhotoRef:
    photo_key: str
    fingerprint: PhotoFingerprint


@dataclass(frozen=True, order=True)
class HashRef:
    listing_index: int
    photo_key: str
    variant: HashVariant
    value: int


class HammingBandIndex:
    """Propose exact Hamming-radius neighbors through ``radius + 1`` bands."""

    def __init__(self, radius: int = 4, bands: int = 5) -> None:
        if radius != 4 or bands != 5:
            raise ValueError("the dedupe index contract requires radius=4 and bands=5")
        self.radius = radius
        self.bands = bands
        self._buckets: dict[tuple[HashVariant, int, int], set[HashRef]] = {}

    @staticmethod
    def band_layout() -> tuple[tuple[int, int], ...]:
        return ((0, 13), (13, 13), (26, 13), (39, 13), (52, 12))

    @classmethod
    def from_refs(cls, refs: Sequence[HashRef]) -> HammingBandIndex:
        index = cls()
        for ref in refs:
            index.add(ref)
        return index

    @classmethod
    def _keys(cls, ref: HashRef) -> tuple[tuple[HashVariant, int, int], ...]:
        return tuple(
            (ref.variant, band_index, (ref.value >> start) & ((1 << width) - 1))
            for band_index, (start, width) in enumerate(cls.band_layout())
        )

    def add(self, ref: HashRef) -> None:
        for key in self._keys(ref):
            self._buckets.setdefault(key, set()).add(ref)

    def neighbors(self, ref: HashRef) -> tuple[HashRef, ...]:
        proposed: set[HashRef] = set()
        for key in self._keys(ref):
            proposed.update(self._buckets.get(key, ()))
        return tuple(sorted(
            candidate
            for candidate in proposed
            if candidate.variant == ref.variant
            and (candidate.value ^ ref.value).bit_count() <= self.radius
        ))


@dataclass(frozen=True)
class PhotoIndexAnalysis:
    usable_photos_by_listing: tuple[tuple[PhotoRef, ...], ...]
    photo_candidate_pairs: frozenset[tuple[int, int]]
    blacklisted_refs: frozenset[tuple[int, str]]
    blacklist_family_count: int


class _UnionFind:
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


def _parse_listing_photos(listing: dict) -> tuple[PhotoRef, ...]:
    photos: dict[str, PhotoFingerprint] = {}
    for key, value in (listing.get("photo_fingerprints") or {}).items():
        fingerprint = value if isinstance(value, PhotoFingerprint) else PhotoFingerprint.from_json(value)
        if fingerprint is not None:
            photos[str(key)] = fingerprint
    if not photos:
        for key, value in (listing.get("photo_url_hashes") or {}).items():
            fingerprint = PhotoFingerprint.from_json(value)
            if fingerprint is not None:
                photos[str(key)] = fingerprint
    if not photos:
        for index, value in enumerate(listing.get("image_hashes") or []):
            fingerprint = PhotoFingerprint.from_json(value)
            if fingerprint is not None:
                photos[f"legacy-{index:04d}"] = fingerprint
    return tuple(PhotoRef(key, photos[key]) for key in sorted(photos))


def _hash_refs(photos_by_listing: Sequence[Sequence[PhotoRef]]) -> tuple[HashRef, ...]:
    refs: set[HashRef] = set()
    for listing_index, photos in enumerate(photos_by_listing):
        for photo in photos:
            if photo.fingerprint.full is not None:
                refs.add(HashRef(listing_index, photo.photo_key, "full", photo.fingerprint.full))
            if photo.fingerprint.center is not None:
                refs.add(HashRef(listing_index, photo.photo_key, "center", photo.fingerprint.center))
    return tuple(sorted(refs))


def analyze_photo_index(
    listings: Sequence[dict],
    min_common_listings: int = 5,
) -> PhotoIndexAnalysis:
    photos_by_listing = tuple(_parse_listing_photos(listing) for listing in listings)
    refs = _hash_refs(photos_by_listing)

    union_find = _UnionFind(len(refs))
    ref_positions = {ref: index for index, ref in enumerate(refs)}
    cluster_index = HammingBandIndex()
    for position, ref in enumerate(refs):
        for neighbor in cluster_index.neighbors(ref):
            union_find.union(position, ref_positions[neighbor])
        cluster_index.add(ref)

    families: dict[int, list[HashRef]] = {}
    for position, ref in enumerate(refs):
        families.setdefault(union_find.find(position), []).append(ref)
    blacklisted: set[tuple[int, str]] = set()
    family_count = 0
    for family in families.values():
        if len({ref.listing_index for ref in family}) >= min_common_listings:
            family_count += 1
            blacklisted.update((ref.listing_index, ref.photo_key) for ref in family)

    usable = tuple(
        tuple(photo for photo in photos if (listing_index, photo.photo_key) not in blacklisted)
        for listing_index, photos in enumerate(photos_by_listing)
    )

    candidates: set[tuple[int, int]] = set()
    candidate_index = HammingBandIndex()
    for ref in _hash_refs(usable):
        for neighbor in candidate_index.neighbors(ref):
            if ref.listing_index != neighbor.listing_index:
                candidates.add(tuple(sorted((ref.listing_index, neighbor.listing_index))))
        candidate_index.add(ref)

    return PhotoIndexAnalysis(
        usable_photos_by_listing=usable,
        photo_candidate_pairs=frozenset(candidates),
        blacklisted_refs=frozenset(blacklisted),
        blacklist_family_count=family_count,
    )
