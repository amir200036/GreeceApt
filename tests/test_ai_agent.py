"""Tests for greeceapt.ai_agent (no Ollama / network)."""

from greeceapt.ai_agent import layer_1_quality_audit as l1
from greeceapt.ai_agent.layer_2_aesthetic_filter import MIN_AESTHETIC_GRADE, MIN_NEIGHBORHOOD_SIZE


def test_layer_2_thresholds():
    assert MIN_AESTHETIC_GRADE == 3.0
    assert MIN_NEIGHBORHOOD_SIZE == 10


def test_layer_1_top_k_and_model_constants():
    assert l1.TOP_K == 5
    assert l1.MOONDREAM_MODEL == "moondream"
    assert "11434" in l1.OLLAMA_BASE_URL
