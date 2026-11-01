"""Reproducible corpus gates for the conservative dedupe rollout."""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"
LIVE_FIXTURE = FIXTURES / "dedupe_live_2026-10-31.json"
HISTORICAL_FIXTURE = FIXTURES / "dedupe_historical_positives_2026-08.json"
FORBIDDEN_TEXT = ("description", "address", "http://", "https://")


def _read_fixture(path: Path) -> dict:
    assert path.exists(), f"missing frozen fixture: {path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_sanitized(payload: dict) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert not any(token in serialized for token in FORBIDDEN_TEXT)


def test_live_fixture_is_sanitized_and_pins_expected_outcomes():
    payload = _read_fixture(LIVE_FIXTURE)

    assert len(payload["listings"]) == 64
    assert len(payload["expected_high_pairs"]) == 6
    assert len(payload["expected_review_pairs"]) == 1
    assert all(row["id"].startswith("listing-") for row in payload["listings"])
    assert all(
        photo_key.startswith("p")
        for row in payload["listings"]
        for photo_key in row.get("photo_url_hashes", {})
    )
    _assert_sanitized(payload)


def test_historical_fixture_has_all_sanitized_known_positive_pairs():
    payload = _read_fixture(HISTORICAL_FIXTURE)

    assert len(payload["positive_pairs"]) == 1_224
    assert all(pair["kept"]["id"].startswith("listing-") for pair in payload["positive_pairs"])
    assert all(pair["absorbed"]["id"].startswith("listing-") for pair in payload["positive_pairs"])
    _assert_sanitized(payload)


def test_positive_pair_loader_does_not_mutate_source(tmp_path):
    assert importlib.util.find_spec("greeceapt.scraper.dedupe_eval") is not None, (
        "dedupe_eval module is missing"
    )
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    source = [{"kept": {"url": "kept", "image_hashes": ["00"]},
               "absorbed": {"url": "absorbed", "image_hashes": ["00"]}}]
    path = tmp_path / "deleted_listings.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    pairs = dedupe_eval.load_positive_pairs([path])

    assert len(pairs) == 1
    assert pairs[0].kept == source[0]["kept"]
    assert pairs[0].absorbed == source[0]["absorbed"]
    assert json.loads(path.read_text(encoding="utf-8")) == source


def test_live_fixture_loader_rejects_remote_urls(tmp_path):
    assert importlib.util.find_spec("greeceapt.scraper.dedupe_eval") is not None, (
        "dedupe_eval module is missing"
    )
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    path = tmp_path / "unsafe.json"
    path.write_text(
        json.dumps({"listings": [{"id": "listing-0001", "photo": "https://example.test/a"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="remote URL"):
        dedupe_eval.load_live_fixture(path)


def test_live_fixture_loader_rejects_free_text_fields(tmp_path):
    assert importlib.util.find_spec("greeceapt.scraper.dedupe_eval") is not None, (
        "dedupe_eval module is missing"
    )
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    path = tmp_path / "unsafe.json"
    path.write_text(
        json.dumps({"listings": [{"id": "listing-0001", "description": "private text"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden field"):
        dedupe_eval.load_live_fixture(path)


def test_isolated_pair_policy_pins_candidate_and_confidence_counts():
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    payload = _read_fixture(HISTORICAL_FIXTURE)
    pairs = [
        dedupe_eval.PositivePair(kept=record["kept"], absorbed=record["absorbed"])
        for record in payload["positive_pairs"]
    ]

    metrics = dedupe_eval.evaluate_legacy_pair_policy(pairs)

    assert metrics == {
        "positive_pairs": 1_224,
        "candidate_pairs": 1_224,
        "high": 1_117,
        "review": 103,
        "none": 4,
        "high_with_fewer_than_three_matches": 53,
        "future_automatic": 1_064,
    }


def test_legacy_full_pipeline_reproduces_frozen_live_outcomes():
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    payload = _read_fixture(LIVE_FIXTURE)
    id_by_index = {index: row["id"] for index, row in enumerate(payload["listings"])}

    result = dedupe_eval.legacy_find_duplicate_groups(payload["listings"])
    merged_pairs = sorted(
        sorted((id_by_index[group[0]], id_by_index[index]))
        for group in result["groups"]
        for index in group[1:]
    )
    review_pairs = sorted(
        sorted((record["a_id"], record["b_id"]))
        for record in result["review_pairs"]
    )

    assert merged_pairs == payload["expected_high_pairs"]
    assert review_pairs == payload["expected_review_pairs"]


def test_v2_candidate_generation_keeps_all_historical_positives():
    from greeceapt.scraper import dedupe

    payload = _read_fixture(HISTORICAL_FIXTURE)

    proposed = sum(
        (0, 1) in dedupe.candidate_pairs([record["kept"], record["absorbed"]])
        for record in payload["positive_pairs"]
    )

    assert proposed == 1_224


def test_final_evaluation_meets_corpus_and_hard_negative_gates():
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")

    report = dedupe_eval.evaluate_corpora(
        HISTORICAL_FIXTURE,
        LIVE_FIXTURE,
        benchmark_repeats=3,
    )

    assert report.historical_positive_pairs == 1_224
    assert report.historical_positive_candidates == 1_224
    assert report.isolated_automatic == 1_064
    assert report.demoted_low_photo == 53
    assert report.live_high_pairs == 6
    assert report.live_review_pairs == 1
    assert report.hard_negative_cases == 6
    assert report.hard_negative_automatic == 0
    assert report.runtime_ratio <= 2.0


def test_evaluation_report_uses_historical_positive_coverage_language():
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")
    report = dedupe_eval.EvaluationReport.empty()

    rendered = dedupe_eval.render_markdown(report).lower()

    assert "historical-positive coverage" in rendered
    assert "accuracy" not in rendered
    assert "precision" not in rendered


def test_legacy_benchmark_parses_each_listing_hash_set_once():
    dedupe_eval = importlib.import_module("greeceapt.scraper.dedupe_eval")

    class CountingListing(dict):
        hash_reads = 0

        def get(self, key, default=None):
            if key == "image_hashes":
                type(self).hash_reads += 1
            return super().get(key, default)

    listings = [
        CountingListing({
            "id": f"listing-{index}",
            "image_hashes": [f"{index + offset:016x}" for offset in range(3)],
            "latitude": 37.9838,
            "longitude": 23.7275,
            "area_sqm": 50.0,
            "price_eur": 100_000.0,
        })
        for index in range(12)
    ]

    dedupe_eval.legacy_find_duplicate_groups(listings)

    assert CountingListing.hash_reads == len(listings)
