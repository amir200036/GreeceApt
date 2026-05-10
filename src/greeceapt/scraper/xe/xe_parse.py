"""JSON → listing dict parsing for XE.gr (pure helpers, no I/O)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from greeceapt.db_helpers.util import normalize_listing_url, resolve_neighborhood
from .xe_config import DISCOVERY_ITEM_KEYS

logger = logging.getLogger(__name__)

def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _deep_first(node: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        for key in keys:
            if key in node and node[key] not in (None, ""):
                return node[key]
        for value in node.values():
            found = _deep_first(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_first(item, keys)
            if found not in (None, ""):
                return found
    return None


def clean_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


_FLOOR_TEXT_MAP: dict[str, float] = {
    "basement":               -1.0,
    "lower ground":           -1.0,
    "semi-basement":          -0.5,
    "semi basement":          -0.5,
    "ground floor":            0.0,
    "ground":                  0.0,
    "elevated ground floor":   0.5,
    "elevated ground":         0.5,
    "raised ground floor":     0.5,
    "mezzanine":               0.5,
}
_ORDINAL_RE = re.compile(r"(\d+)\s*(?:st|nd|rd|th)\b", re.IGNORECASE)


def clean_floor(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Plain integer string ("3") or negative ("-1")
    try:
        return float(int(text))
    except ValueError:
        pass
    # Ordinal: "1st", "2nd floor", "3rd", "4th floor"
    m = _ORDINAL_RE.search(text)
    if m:
        return float(int(m.group(1)))
    # Text keyword match (longest key first to avoid "ground" shadowing "ground floor")
    lower = text.lower()
    for key in sorted(_FLOOR_TEXT_MAP, key=len, reverse=True):
        if key in lower:
            return _FLOOR_TEXT_MAP[key]
    return None


def clean_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    token_match = re.search(r"\d[\d.,]*", str(value).strip())
    if not token_match:
        return None
    token = token_match.group(0)

    if "." in token and "," in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        parts = token.split(",")
        token = "".join(parts) if len(parts[-1]) == 3 else token.replace(",", ".")
    elif "." in token:
        parts = token.split(".")
        if len(parts[-1]) == 3 and len(parts) > 1:
            token = "".join(parts)

    try:
        return float(token)
    except ValueError:
        return None


def clean_price_eur(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return float(int(digits))
    except ValueError:
        return None


def normalize_publication_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def flatten_characteristics(node: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    characteristics = node.get("characteristics_list")
    if not isinstance(characteristics, list):
        return flattened
    for entry in characteristics:
        if not isinstance(entry, dict):
            continue
        label_raw = _first_present(entry, "text", "label", "name", "title")
        if label_raw is None:
            continue
        label = str(label_raw).strip().lower()
        if not label:
            continue
        value = _first_present(entry, "value", "display_value", "text_value", "description")
        flattened[label] = value
    return flattened


def find_characteristics_container(node: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node.get("characteristics_list"), list):
        return node
    for key in ("result", "t", "listing", "property", "details"):
        child = node.get(key)
        if isinstance(child, dict) and isinstance(child.get("characteristics_list"), list):
            return child
    return node


def _extract_photo_urls(node: dict[str, Any]) -> list[str]:
    photo_urls: list[str] = []
    photos = node.get("photos")
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, str) and photo:
                photo_urls.append(photo)
            elif isinstance(photo, dict):
                url = _first_present(photo, "url", "src", "image_url", "large", "original")
                if url:
                    photo_urls.append(str(url))

    image_gallery = node.get("image_gallery")
    if isinstance(image_gallery, list):
        for image in image_gallery:
            if isinstance(image, str) and image:
                photo_urls.append(image)
                continue
            if not isinstance(image, dict):
                continue
            for branch in ("fullscreen", "big", "medium", "small", "thumbnail"):
                branch_value = image.get(branch)
                if isinstance(branch_value, dict):
                    url = _first_present(branch_value, "jpeg", "webp", "jpg", "url")
                    if url:
                        photo_urls.append(str(url))
                        break
    return list(dict.fromkeys(photo_urls))


def extract_discovery_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in DISCOVERY_ITEM_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            if key != "items":
                logger.info("Discovery items extracted from response key '%s'.", key)
            return [item for item in value if isinstance(item, dict)]
    return []


def extract_property_id(item: dict[str, Any]) -> str | None:
    raw_id = _first_present(item, "id", "result_id", "ad_id", "listing_id")
    if raw_id is None:
        return None
    text = str(raw_id).strip()
    return text or None

def extract_address_parts(node: dict[str, Any]) -> tuple[str | None, str | None]:
    separated = node.get("address_separated")
    if isinstance(separated, list):
        municipality = str(separated[0]).strip() if len(separated) > 0 and separated[0] else None
        area_raw = str(separated[1]).strip() if len(separated) > 1 and separated[1] else None
        return municipality, area_raw
    if isinstance(separated, dict):
        municipality = _first_present(separated, "municipality", "city", "region")
        area_raw = _first_present(separated, "area", "neighborhood", "district")
        return (
            str(municipality).strip() if municipality else None,
            str(area_raw).strip() if area_raw else None,
        )
    return None, None

def get_listing_payload(detail_node: dict[str, Any], chars: dict[str, Any]) -> dict[str, Any] | None:
    url = _first_present(detail_node, "url", "seo_url")
    canonical_url = normalize_listing_url(url)
    if not canonical_url:
        return None

    municipality, area_raw = extract_address_parts(detail_node)

    # Fallback for map_search preview: parse "City (Neighborhood)" from the address string.
    # Use both _first_present and _deep_first so nested address fields are found.
    if area_raw is None:
        addr_str = str(
            _first_present(detail_node, "address", "address_full")
            or _deep_first(detail_node, ("address", "address_full"))
            or ""
        ).strip()
        m = re.search(r"\(([^)]+)\)", addr_str)
        if m:
            area_raw = m.group(1).strip()
            if municipality is None:
                municipality = addr_str[: addr_str.index("(")].strip() or None

    neighborhood = resolve_neighborhood(municipality, area_raw)
    photo_urls = _extract_photo_urls(detail_node)

    # Floor: prefer characteristics_list; fall back to top-level and deep keys in detail_node.
    # Must check for None explicitly (ground floor = 0.0 is falsy).
    _floor = clean_floor(_first_present(chars, "floor"))
    if _floor is None:
        _floor = clean_floor(
            _first_present(detail_node, "floor", "floor_number")
            or _deep_first(detail_node, ("floor", "floor_number"))
        )

    listing = {
        "url": str(url).strip(),
        "title": _first_present(detail_node, "title", "headline")
        or _deep_first(detail_node, ("title", "headline")),
        "description": _first_present(detail_node, "description", "body", "description_text")
        or _deep_first(detail_node, ("description", "body", "description_text")),
        "price_eur": clean_price_eur(
            _first_present(detail_node, "price", "price_value") or _deep_first(detail_node, ("price", "price_value"))
        ),
        "price_per_sqm": clean_float(
            _first_present(detail_node, "price_per_sqm", "price_sqm", "price_per_square_meter", "price_per_unit_area")
            or _deep_first(detail_node, ("price_per_sqm", "price_sqm", "price_per_square_meter", "price_per_unit_area"))
        ),
        "area_sqm": clean_float(
            _first_present(detail_node, "size_with_square_meter", "size", "sqm")
            or _deep_first(detail_node, ("size_with_square_meter", "size", "sqm"))
        ),
        "municipality": municipality,
        "neighborhood": neighborhood,
        "address_raw": _first_present(detail_node, "address", "address_full")
        or _deep_first(detail_node, ("address", "address_full")),
        "bedrooms": clean_int(
            _first_present(detail_node, "bedrooms", "bedrooms_count")
            or _deep_first(detail_node, ("bedrooms", "bedrooms_count"))
        ),
        "bathrooms": clean_int(
            _first_present(detail_node, "bathrooms", "bathrooms_count")
            or _deep_first(detail_node, ("bathrooms", "bathrooms_count"))
        ),
        "floor": _floor,
        "year_built": clean_int(
            _first_present(chars, "year built", "year of construction", "construction year")
            or _first_present(detail_node, "construction_year")
            or _deep_first(detail_node, ("construction_year",))
        ),
        "renovation_year": clean_int(_first_present(chars, "renovation year")),
        "energy_class": _first_present(chars, "energy class"),
        "heating_type": _first_present(chars, "heating", "heating system", "heat type")
        or _deep_first(detail_node, ("heating_type", "heating_system", "heating")),
        "photos_count": clean_int(_first_present(detail_node, "photos_count") or _deep_first(detail_node, ("photos_count",)))
        or len(photo_urls),
        "photo_urls": photo_urls,
        "publication_date": normalize_publication_date(
            _first_present(detail_node, "published_at", "publication_date", "publication_start_date", "date")
            or _deep_first(detail_node, ("published_at", "publication_date", "publication_start_date", "date"))
        ),
        "latitude": clean_float(
            _first_present(detail_node, "geo_lat", "latitude") or _deep_first(detail_node, ("geo_lat", "latitude"))
        ),
        "longitude": clean_float(
            _first_present(detail_node, "geo_lng", "longitude") or _deep_first(detail_node, ("geo_lng", "longitude"))
        ),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    # Post-hoc: recover neighborhood from address_raw for cases where the address was
    # only discoverable via _deep_first but the earlier fallback used _first_present.
    if not listing["neighborhood"] and listing.get("address_raw"):
        _addr = str(listing["address_raw"]).strip()
        _m = re.search(r"\(([^)]+)\)", _addr)
        if _m:
            _area = _m.group(1).strip()
            _before = _addr[: _addr.index("(")].strip() or None
            listing["neighborhood"] = resolve_neighborhood(
                _before or listing.get("municipality"), _area
            )
            if not listing["municipality"] and _before:
                listing["municipality"] = _before

    return listing


def listing_from_mapsearch_preview(item: dict[str, Any]) -> dict[str, Any] | None:
    """
    When map_search rows embed the same shape as (or nest) a property card, build a listing
    without calling ``single_result``. Returns None if we cannot derive a canonical URL.
    """
    merged: dict[str, Any] = dict(item)
    for key in ("ad", "result", "listing", "t", "property"):
        sub = item.get(key)
        if isinstance(sub, dict):
            merged.update(sub)
    url_raw = _first_present(merged, "url", "seo_url", "link")
    if url_raw and str(url_raw).startswith("/"):
        merged["url"] = "https://www.xe.gr" + str(url_raw)
    elif not url_raw:
        rid = extract_property_id(item)
        if rid:
            merged["url"] = f"https://www.xe.gr/en/property/d/property-for-sale/{rid}"
    if not normalize_listing_url(_first_present(merged, "url", "seo_url")):
        return None
    chars = flatten_characteristics(find_characteristics_container(merged))
    return get_listing_payload(merged, chars)
