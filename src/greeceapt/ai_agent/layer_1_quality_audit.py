"""
Layer 1 — Visual auditor (Moondream + llama3 via Ollama).

Pipeline per image:
  1. Moondream describes the room in free text (the only prompt type it handles reliably).
  2. llama3 reads the description and returns a precise JSON score (1.0–10.0),
     renovation flag, and view type.

Scores from interior-like views are aggregated (top-K mean) for ``visual_score``;
full structured output is stored in ``layer_1_features`` for downstream scoring.

Images are resized before inference. Multiple images per listing are scored in
parallel (``LAYER1_MOONDREAM_CONCURRENCY``) while each listing still runs in the
outer pool from ``ai_conductor``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests as http_requests
from PIL import Image

logger = logging.getLogger(__name__)

_thread_sessions = threading.local()


def _http_session() -> http_requests.Session:
    """Thread-local Session — ``requests.Session`` is not thread-safe shared."""
    s = getattr(_thread_sessions, "session", None)
    if s is None:
        s = http_requests.Session()
        s.headers.setdefault("User-Agent", "GreeceApt/1.0 (AI-Layer1)")
        _thread_sessions.session = s
    return s


def _ollama_base_url() -> str:
    raw = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").strip().rstrip("/")
    if not raw.lower().startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


OLLAMA_BASE_URL = _ollama_base_url()
MOONDREAM_MODEL = os.environ.get("OLLAMA_MODEL", os.environ.get("MOONDREAM_MODEL", "moondream"))
SCORE_MODEL     = os.environ.get("OLLAMA_SCORE_MODEL", "llama3")
OLLAMA_TIMEOUT_S = int(os.environ.get("OLLAMA_TIMEOUT", "60"))
IMAGE_SIZE = (384, 384)
TOP_K = 5
JPEG_QUALITY = int(os.environ.get("LAYER1_JPEG_QUALITY", "85"))
LAYER1_MAX_IMAGES = int(os.environ.get("LAYER1_MAX_IMAGES", "0"))
LAYER1_MOONDREAM_CONCURRENCY = max(1, int(os.environ.get("LAYER1_MOONDREAM_CONCURRENCY", "3")))

# ── Prompts ───────────────────────────────────────────────────────────────────
# Moondream 1B only reliably answers open-ended description questions.
# llama3 scores the description as structured JSON with decimal precision.

_Q_DESCRIBE = "Describe the condition of this room in one sentence."

_SCORE_PROMPT = """\
An apartment photo was described as: "{desc}"

You are a real estate quality analyst. Based on this description, return a JSON object:
{{"score": <FLOAT>, "is_renovated": <BOOL>, "view_type": "<TYPE>"}}

score: a decimal from 1.0 to 10.0
  1.0-2.0 = derelict, severe structural damage
  2.5-3.5 = very poor, crumbling walls or severe neglect
  4.0-4.9 = poor, very old and dated finishes
  5.0-5.9 = average, liveable but clearly aged
  6.0-6.9 = above average, acceptable condition
  7.0-7.9 = good, clean and well maintained
  8.0-8.9 = very good, recently updated or renovated
  9.0-9.9 = excellent, high-quality modern renovation
  10.0    = brand new or luxury grade
Use one decimal place. Be precise and critical — most Athens budget apartments score 4–6.

is_renovated: true only if description clearly implies modern finishes or recent renovation.
view_type: interior | kitchen | bathroom | bedroom | living | balcony | exterior | floor_plan | other

Reply with the JSON object only."""

# Interior detection keyword fallback (used when llama3 is unavailable)
_INTERIOR_KW = frozenset({"room", "wall", "floor", "ceiling", "kitchen", "bathroom",
                           "bedroom", "countertop", "table", "chair", "bed", "sofa",
                           "shelf", "lamp", "corridor", "hallway", "interior"})

_INTERIOR_VIEWS = frozenset(
    {"interior", "kitchen", "bathroom", "bedroom", "living", "dining"},
)


# ── Keyword fallback scorer (used if llama3 call fails) ──────────────────────

def _fallback_parse(desc: str) -> tuple[float, bool, str]:
    t = desc.lower()
    _excellent = {"renovated", "newly", "modern", "luxury", "elegant", "pristine"}
    _good      = {"clean", "bright", "spacious", "tidy", "well-maintained"}
    _poor      = {"old", "dated", "worn", "cluttered", "dark", "dingy", "cramped", "shabby"}
    _very_poor = {"dirty", "filthy", "stained", "damaged", "cracked", "peeling", "dilapidated"}

    if any(w in t for w in _excellent):
        score = 8.0
    elif any(w in t for w in _good):
        score = 6.5
    elif any(w in t for w in _very_poor):
        score = 2.5
    elif any(w in t for w in _poor):
        score = 3.5
    else:
        score = 5.0

    is_renovated = any(w in t for w in _excellent)
    view_type = "interior" if any(w in t for w in _INTERIOR_KW) else "exterior"
    return score, is_renovated, view_type


# ── llama3 scoring ────────────────────────────────────────────────────────────

def _score_with_llm(desc: str) -> tuple[float, bool, str] | None:
    """Send description to llama3; return (score, is_renovated, view_type) or None."""
    prompt = _SCORE_PROMPT.format(desc=desc)
    try:
        resp = _http_session().post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": SCORE_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"num_predict": 120, "temperature": 0.1},
            },
            timeout=OLLAMA_TIMEOUT_S,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()

        # Try direct parse, then regex fallback
        obj = None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    obj = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if not isinstance(obj, dict):
            logger.warning("llama3 score parse failed. raw=%r", raw[:200])
            return None

        score_raw = obj.get("score")
        try:
            score = round(float(score_raw), 1)
        except (TypeError, ValueError):
            logger.warning("llama3 invalid score=%r", score_raw)
            return None
        score = max(1.0, min(10.0, score))

        ren = obj.get("is_renovated")
        if isinstance(ren, str):
            is_renovated = ren.strip().lower() in ("true", "1", "yes")
        else:
            is_renovated = bool(ren)

        vt = str(obj.get("view_type") or "other").strip().lower().replace(" ", "_")
        if not vt:
            vt = "other"

        return score, is_renovated, vt
    except Exception as e:
        logger.warning("llama3 scoring error: %s", e)
        return None


# ── Main image scorer ─────────────────────────────────────────────────────────

def download_and_resize(url: str) -> str | None:
    """Download image URL, resize to ``IMAGE_SIZE``, save to temp JPEG. Returns path or None."""
    try:
        r = _http_session().get(url.strip(), timeout=15)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB").resize(IMAGE_SIZE, Image.LANCZOS)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            img.save(f, format="JPEG", quality=JPEG_QUALITY)
            return f.name
    except Exception:
        logger.debug("Image fetch/resize failed: %s", url, exc_info=True)
        return None


def _run_moondream(image_path: str) -> dict | None:
    """Describe image with Moondream, then score the description with llama3."""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        resp = _http_session().post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": MOONDREAM_MODEL,
                "prompt": _Q_DESCRIBE,
                "images": [img_b64],
                "stream": False,
                "options": {"num_predict": 80},
            },
            timeout=OLLAMA_TIMEOUT_S,
        )
        resp.raise_for_status()
        desc = resp.json().get("response", "").strip()
        if not desc:
            logger.warning("Moondream empty response for %s", image_path)
            return None

        result = _score_with_llm(desc)
        if result is None:
            logger.info("llama3 unavailable, using keyword fallback for: %r", desc)
            result = _fallback_parse(desc)

        score, is_renovated, view_type = result
        logger.debug("desc=%r → score=%.1f renovated=%s view=%s", desc, score, is_renovated, view_type)
        return {
            "interior_quality_score": score,
            "is_renovated": is_renovated,
            "view_type": view_type,
        }
    except Exception as e:
        logger.error("Moondream error: %s", e)
        return None


def analyze_listing(image_paths: list[str]) -> tuple[float | None, dict]:
    """
    Run Moondream+llama3 on each image path; return (visual_score, layer_1_features).

    ``visual_score`` is the mean of the top-K interior-like ``interior_quality_score`` values.
    """
    if not image_paths:
        return None, {}

    paths_slice = image_paths[:LAYER1_MAX_IMAGES] if LAYER1_MAX_IMAGES > 0 else list(image_paths)

    n_workers = min(LAYER1_MOONDREAM_CONCURRENCY, len(paths_slice))
    per_image: list[dict] = []
    interior_scores: list[float] = []
    any_renovated = False

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        future_map = {pool.submit(_run_moondream, p): (i, p) for i, p in enumerate(paths_slice, 1)}
        for fut in as_completed(future_map):
            i, path = future_map[fut]
            try:
                result = fut.result()
            except Exception as exc:
                logger.warning("Moondream task failed img %s: %s", path, exc)
                continue

            if not result:
                logger.info("  img %d/%d  FAILED", i, len(paths_slice))
                continue

            per_image.append({"index": i, **result})
            if result.get("is_renovated"):
                any_renovated = True
            vt = result.get("view_type") or "other"
            if vt in _INTERIOR_VIEWS:
                interior_scores.append(float(result["interior_quality_score"]))
                logger.info(
                    "  img %d/%d KEEP | score=%.1f view=%s renovated=%s",
                    i, len(paths_slice),
                    result["interior_quality_score"], vt, result.get("is_renovated"),
                )
            else:
                logger.info(
                    "  img %d/%d SKIP view | score=%.1f view=%s",
                    i, len(paths_slice),
                    result["interior_quality_score"], vt,
                )

    if not interior_scores:
        interior_scores = [float(r["interior_quality_score"]) for r in per_image]

    if not interior_scores:
        return None, {"per_image": per_image, "is_renovated": any_renovated}

    top_k = sorted(interior_scores, reverse=True)[:TOP_K]
    avg = round(sum(top_k) / len(top_k), 2)
    features = {
        "interior_quality_avg": avg,
        "is_renovated": any_renovated,
        "view_types": [r.get("view_type") for r in per_image],
        "per_image": per_image,
    }
    return avg, features
