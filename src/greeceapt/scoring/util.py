"""Shared numeric helpers for the scoring package."""


def trimmed_mean(values: list[float], trim_pct: float) -> float:
    """Trim ``trim_pct`` from each tail (at least one element per tail when n > 3)."""
    n = len(values)
    if n == 0:
        return 0.0
    if n <= 3:
        return sum(values) / n
    cut = max(1, int(n * trim_pct))
    trimmed = sorted(values)[cut:-cut]
    return sum(trimmed) / len(trimmed)
