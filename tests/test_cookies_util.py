"""Tests for greeceapt.cookies.util."""

import pytest

from greeceapt.cookies.util import count_expired_cookies, parse_cookie_json_root


def test_parse_cookie_json_root_list():
    assert parse_cookie_json_root([{"name": "a"}]) == [{"name": "a"}]


def test_parse_cookie_json_root_wrapped():
    assert parse_cookie_json_root({"cookies": [{"name": "b"}]}) == [{"name": "b"}]


def test_parse_cookie_json_root_invalid():
    with pytest.raises(ValueError):
        parse_cookie_json_root({"wrong": True})


def test_count_expired():
    now = 1_000_000.0
    cookies = [
        {"expires": now - 1},
        {"expires": now + 100},
        {"expires": -1},
    ]
    assert count_expired_cookies(cookies, now=now) == 1
