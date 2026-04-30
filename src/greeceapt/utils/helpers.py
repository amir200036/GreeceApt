"""Shared URL and neighborhood utilities used across the pipeline."""

from urllib.parse import urlsplit, urlunsplit


def normalize_listing_url(url: str | None) -> str | None:
    """Strip query/fragment to keep a stable canonical URL."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return None


# Words that indicate a compound neighborhood name split across API fields.
# Used only to recombine e.g. municipality="Nea", area="Smyrni" → "Nea Smyrni".
_COMPOUND_PREFIXES = frozenset({"ano", "kato", "nea", "neo", "palaio"})


def resolve_neighborhood(
    municipality: str | None, area_raw: str | None
) -> str | None:
    """
    Return the full neighborhood name, combining split API fields when needed.
    Examples:
      ("Nea Smyrni", None)   → "Nea Smyrni"
      ("Nea", "Smyrni")      → "Nea Smyrni"
      ("Athens", "Pagkrati") → "Pagkrati"
      ("Pagkrati", "Gouva")  → "Pagkrati"
    """
    mun = (municipality or "").strip() or None
    ar = (area_raw or "").strip() or None
    if not mun and not ar:
        return None
    if mun and mun.lower() == "athens":
        return ar or None
    if mun:
        mun_parts = mun.split()
        # API sometimes splits compound names: municipality="Nea", area="Smyrni"
        if len(mun_parts) == 1 and mun_parts[0].lower() in _COMPOUND_PREFIXES and ar:
            return mun + " " + ar
        return mun
    return ar
