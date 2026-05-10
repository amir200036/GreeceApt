"""Tests for greeceapt.pipeline.util."""

import json

import pytest

from greeceapt.pipeline.util import load_listings_json_list


def test_load_listings_json_list_ok(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps([{"a": 1}]), encoding="utf-8")
    assert load_listings_json_list(p) == [{"a": 1}]


def test_load_listings_json_list_not_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_listings_json_list(p)
