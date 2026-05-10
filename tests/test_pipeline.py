"""Tests for greeceapt.pipeline."""

import json

import pytest

from greeceapt.pipeline import ingest


def test_load_json_list(tmp_path):
    path = tmp_path / "listings.json"
    path.write_text(json.dumps([{"url": "https://a/1", "price_eur": 1}]), encoding="utf-8")
    rows = ingest.load_json(str(path))
    assert len(rows) == 1
    assert rows[0]["url"] == "https://a/1"


def test_load_json_rejects_non_list(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        ingest.load_json(str(path))


def test_load_json_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest.load_json(str(tmp_path / "nope.json"))
