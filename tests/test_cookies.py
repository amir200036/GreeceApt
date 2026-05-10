"""Tests for greeceapt.cookies."""

import json
import time
from pathlib import Path

import pytest

from greeceapt.cookies.cookie_manager import load_cookies, save_cookies


def test_load_cookies_list_format(tmp_path: Path):
    path = tmp_path / "cookies.json"
    future = int(time.time()) + 86400
    cookies = [{"name": "sid", "value": "abc", "domain": ".xe.gr", "expires": future}]
    path.write_text(json.dumps(cookies), encoding="utf-8")
    loaded = load_cookies(path)
    assert len(loaded) == 1
    assert loaded[0]["name"] == "sid"


def test_load_cookies_wrapped_format(tmp_path: Path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"cookies": [{"name": "x", "value": "y"}]}), encoding="utf-8")
    loaded = load_cookies(path)
    assert loaded[0]["value"] == "y"


def test_load_cookies_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_cookies(tmp_path / "missing.json")


def test_load_cookies_bad_format(tmp_path: Path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"wrong": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        load_cookies(path)


def test_save_roundtrip(tmp_path: Path):
    path = tmp_path / "out.json"
    save_cookies(path, [{"name": "a", "value": "b"}])
    assert load_cookies(path)[0]["name"] == "a"
