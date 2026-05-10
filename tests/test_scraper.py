"""Tests for greeceapt.scraper (imports and pure helpers; no live scrape)."""

from greeceapt.scraper.obscura_helper import (
    Engine,
    FALLBACK_THRESHOLD,
    _obscura_port_candidates,
    allow_chromium_fallback,
    is_obscura_available,
)
from greeceapt.scraper.scrape_spitogatos import SEARCH_API
from greeceapt.scraper.scrape_xe import ATHENS_CENTER_ID as ATHENS_FROM_XE_MODULE
from greeceapt.scraper.xe import xe_config
from greeceapt.scraper.xe.xe_session import _chromium_headless_for_xe_cookie_bootstrap
from greeceapt.scraper.util import (
    ATHENS_CENTER_ID,
    BASE_URL_XE,
    build_xe_url,
    upsert_listing_in_url_store,
)


def test_spitogatos_search_api_url():
    assert "spitogatos" in SEARCH_API.lower()


def test_xe_athens_center_id_shape():
    assert ATHENS_CENTER_ID.startswith("ChIJ")
    assert ATHENS_FROM_XE_MODULE == ATHENS_CENTER_ID


def test_build_xe_url_contains_core_params():
    u = build_xe_url(min_price=50_000, max_price=100_000, page=1)
    assert u.startswith(BASE_URL_XE)
    assert "minimum_price=50000" in u
    assert "maximum_price=100000" in u
    assert "page=1" in u
    assert "country=GR" in u
    assert ATHENS_CENTER_ID in u
    assert "building_type_options" not in u


def test_build_xe_url_has_photos_flag():
    u = build_xe_url(has_photos=True)
    assert "has_photos=true" in u


def test_build_xe_url_optional_building_type_filter():
    u = build_xe_url(building_type="apartment")
    assert "building_type_options%5B%5D=apartment" in u or "building_type_options[]=apartment" in u


def test_obscura_engine_enum():
    assert Engine.OBSCURA.value == "obscura"
    assert isinstance(FALLBACK_THRESHOLD, int)


def test_is_obscura_available_boolean():
    assert is_obscura_available() in (True, False)


def test_allow_chromium_fallback_boolean():
    assert isinstance(allow_chromium_fallback(), bool)


def test_xe_cookie_bootstrap_headless_env(monkeypatch):
    monkeypatch.setenv("GREECEAPT_XE_COOKIE_HEADLESS", "1")
    assert _chromium_headless_for_xe_cookie_bootstrap() is True
    monkeypatch.delenv("GREECEAPT_XE_COOKIE_HEADLESS", raising=False)
    monkeypatch.setenv("GREECEAPT_XE_COOKIE_HEADFUL", "1")
    assert _chromium_headless_for_xe_cookie_bootstrap() is False


def test_obscura_port_candidates_env_override(monkeypatch):
    monkeypatch.setenv("GREECEAPT_OBSCURA_PORTS", "1111, 2222")
    assert _obscura_port_candidates(9224) == [1111, 2222]


def test_obscura_port_candidates_default_starts_requested(monkeypatch):
    monkeypatch.delenv("GREECEAPT_OBSCURA_PORTS", raising=False)
    ports = _obscura_port_candidates(9224)
    assert ports[0] == 9224
    assert 9244 in ports


def test_xe_obscura_bootstrap_port_in_valid_range():
    assert 1024 <= xe_config.OBSCURA_BOOTSTRAP_PORT <= 65535


def test_xe_stealth_mode_flag_exists():
    assert isinstance(getattr(xe_config, "XE_STEALTH_MODE", None), bool)


def test_upsert_listing_in_url_store_touch_vs_insert():
    store: dict = {}
    u = "https://example.com/a"
    assert upsert_listing_in_url_store(store, u, {"url": u, "price_eur": 1, "scraped_at": "t0"}) == "inserted"
    assert store[u]["price_eur"] == 1
    first_updated = store[u].get("updated_at")
    assert first_updated
    assert upsert_listing_in_url_store(store, u, {"url": u, "price_eur": 99, "scraped_at": "t1"}) == "touched"
    assert store[u]["price_eur"] == 1
    assert store[u]["updated_at"] != first_updated
