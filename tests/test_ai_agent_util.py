"""Tests for greeceapt.ai_agent.util."""

from datetime import date

from greeceapt.ai_agent.util import parse_yyyy_mm_dd_prefix


def test_parse_yyyy_mm_dd_prefix():
    assert parse_yyyy_mm_dd_prefix("2026-03-15T12:00:00") == date(2026, 3, 15)
    assert parse_yyyy_mm_dd_prefix("") is None
