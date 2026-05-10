"""Playwright cookie bootstrap and curl-cffi session for XE.gr."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Any

from curl_cffi import requests

from greeceapt.cookies.cookie_manager import load_cookies
from greeceapt.scraper.obscura_helper import Engine, engine_session, new_stealth_context, safe_title
from . import xe_config as cfg

logger = logging.getLogger(__name__)


def _chromium_headless_for_xe_cookie_bootstrap() -> bool:
    """
    Headed Chromium usually receives XE session cookies (_rodeo_session, etc.);
    headless often yields none and map_search returns 403.

    ``GREECEAPT_XE_COOKIE_HEADLESS=1`` — force headless (e.g. CI).
    ``GREECEAPT_XE_COOKIE_HEADFUL=1`` — force headed (overrides headless default on Linux CI).
    """
    if os.getenv("GREECEAPT_XE_COOKIE_HEADLESS", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.getenv("GREECEAPT_XE_COOKIE_HEADFUL", "").strip().lower() in ("1", "true", "yes"):
        return False
    if sys.platform == "darwin":
        return False
    return not bool(os.environ.get("DISPLAY"))


def _cookies_have_valid_entry(cookies: list[dict[str, Any]]) -> bool:
    if not cookies:
        return False
    now = time.time()
    for cookie in cookies:
        expires = cookie.get("expires")
        if not isinstance(expires, (int, float)):
            return True
        if expires <= 0 or expires > now:
            return True
    return False


def is_dns_or_connection_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "could not resolve host" in msg
        or "name or service not known" in msg
        or "temporary failure in name resolution" in msg
        or "connection" in msg
        or "failed to perform" in msg
    )


async def _page_full_html(page) -> str:
    """
    Obscura/CDP can make ``page.content()`` fail with ``value: expected string, got object``.
    Fall back to ``document.documentElement.outerHTML``.
    """
    try:
        raw = await page.content()
        if isinstance(raw, str):
            return raw
        if raw is not None:
            return str(raw)
    except Exception as exc:
        logger.debug("page.content() unavailable (%s); trying outerHTML.", exc)
    try:
        out = await page.evaluate(
            "() => document.documentElement ? document.documentElement.outerHTML : ''"
        )
        if isinstance(out, str):
            return out
        if out is not None:
            return str(out)
    except Exception as exc:
        logger.debug("outerHTML fallback failed: %s", exc)
    return ""


async def _bootstrap_poll_for_larger_html(
    page,
    *,
    max_total_ms: int,
    chunk_ms: int,
) -> tuple[str, str, int, str]:
    """
    Poll DOM in short chunks so Ctrl+C cancels promptly and we do not sleep 35s on a dead WAF shell.
    Stops early when HTML looks like a full SERP (>=18k) or WAF markers appear.
    """
    deadline = time.monotonic() + max_total_ms / 1000.0
    while time.monotonic() < deadline:
        title = await safe_title(page)
        html = await _page_full_html(page)
        html_size = len(html)
        content_sample = html[:8000].lower()
        if html_size >= 18_000:
            return title, html, html_size, content_sample
        if "human verification" in title.lower() or "awswafcookiedomainlist" in content_sample:
            return title, html, html_size, content_sample
        remaining_s = deadline - time.monotonic()
        step_ms = min(chunk_ms, int(max(0.0, remaining_s) * 1000))
        if step_ms < 500:
            break
        await page.wait_for_timeout(step_ms)
    title = await safe_title(page)
    html = await _page_full_html(page)
    return title, html, len(html), html[:8000].lower()


async def _try_cdp_cookies(ctx, page) -> list[dict[str, Any]]:
    """Get ALL cookies including HttpOnly ones. Tries CDP, then storage_state, then ctx.cookies()."""
    try:
        cdp = await ctx.new_cdp_session(page)
        result = await cdp.send("Network.getAllCookies")
        cookies = result.get("cookies", [])
        if cookies:
            return cookies
    except Exception:
        pass
    try:
        state = await ctx.storage_state()
        cookies = state.get("cookies", [])
        if cookies:
            return cookies
    except Exception:
        pass
    return await ctx.cookies()


async def _poll_for_session_cookie(ctx, page, *, max_wait_s: int = 25) -> list[dict[str, Any]]:
    """
    Poll cookies every 2s until _rodeo_session appears (or max_wait_s).
    Returns the latest cookie list regardless.
    """
    # Let XE's JS-triggered requests complete before we start checking.
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    cookies: list[dict[str, Any]] = []
    steps = max(1, max_wait_s // 2)
    for i in range(steps):
        cookies = await _try_cdp_cookies(ctx, page)
        names = {c.get("name") for c in cookies}
        if "_rodeo_session" in names or "rodeo_session" in names:
            logger.info("_rodeo_session captured after %ss of polling.", i * 2)
            return cookies
        if i < steps - 1:
            await page.wait_for_timeout(2000)

    names = {c.get("name") for c in cookies}
    if "_rodeo_session" not in names and "rodeo_session" not in names:
        logger.warning(
            "Bootstrap: _rodeo_session not found after %ss. Cookies present: %s",
            max_wait_s,
            sorted(names),
        )
    return cookies


async def _ensure_cookies_with_optional_force(force: bool) -> list[dict[str, Any]]:
    if force:
        # Back up existing cookies before wiping — restored if new bootstrap fails.
        backup: list[dict[str, Any]] = []
        if cfg.COOKIES_PATH.exists():
            try:
                backup = json.loads(cfg.COOKIES_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
            cfg.COOKIES_PATH.unlink()

    # Obscura CDP port for XE cookie bootstrap only (Spitogatos is curl-only).
    ch_headless = _chromium_headless_for_xe_cookie_bootstrap()
    logger.info(
        "Cookie bootstrap chromium_headless=%s (macOS defaults to headed; "
        "CI: GREECEAPT_XE_COOKIE_HEADLESS=1).",
        ch_headless,
    )
    allow_escape = os.getenv("GREECEAPT_XE_COOKIE_NO_CHROMIUM_ESCAPE", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    )
    async with engine_session(
        workers=1,
        port=cfg.OBSCURA_BOOTSTRAP_PORT,
        chromium_headless=ch_headless,
        allow_chromium_escape=allow_escape,
        skip_obscura=True,
    ) as sess:
        logger.info("Cookie bootstrap using %s engine.", sess.engine.value)
        ctx = await new_stealth_context(sess.browser, locale="en-US")
        page = await ctx.new_page()
        try:
            for attempt in range(1, 4):
                # Navigate directly to the full search results URL — this triggers the
                # csrf_token cookie and a full page render (needed for API auth).
                nav_ok = True
                try:
                    await page.goto(cfg.BOOTSTRAP_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("load", timeout=20000)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.warning("Bootstrap nav failed (attempt=%s): %s", attempt, exc)
                    nav_ok = False

                if not nav_ok:
                    # Page is in broken state after timeout — recycle immediately to avoid hangs.
                    sess.record_blocked()
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                    if sess.consecutive_errors >= 2 and sess.engine is Engine.OBSCURA:
                        logger.warning(
                            "Obscura blocked %s times — rotating / switching browser (see obscura_helper).",
                            sess.consecutive_errors,
                        )
                        await sess.switch_to_chromium()
                    ctx = await new_stealth_context(sess.browser, locale="en-US")
                    page = await ctx.new_page()
                    continue

                # Wait for page JS (including WAF challenge) to complete.
                wait_ms = 20000 + attempt * 5000
                try:
                    await page.wait_for_timeout(wait_ms)
                    title = await safe_title(page)
                    html = await _page_full_html(page)
                    content_sample = html[:8000].lower()
                    html_size = len(html)
                except Exception as exc:
                    logger.warning("Bootstrap page read failed (attempt=%s): %s", attempt, exc)
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                    ctx = await new_stealth_context(sess.browser, locale="en-US")
                    page = await ctx.new_page()
                    continue

                definitely_waf = (
                    "human verification" in title.lower()
                    or "awswafcookiedomainlist" in content_sample
                )
                # Full SERP is large; WAF/interstitials often stay ~4–8k. Poll in short slices so
                # we do not block 35s on a shell that never grows (and Ctrl+C can interrupt sooner).
                if not definitely_waf and html_size < 18_000:
                    total_ms = 16_000 if sess.engine is Engine.OBSCURA else 10_000
                    chunk_ms = 4_000
                    logger.info(
                        "Bootstrap: HTML still small (%s bytes) without WAF title markers — "
                        "polling up to %sms in %sms steps for client render.",
                        html_size,
                        total_ms,
                        chunk_ms,
                    )
                    try:
                        title, html, html_size, content_sample = await _bootstrap_poll_for_larger_html(
                            page,
                            max_total_ms=total_ms,
                            chunk_ms=chunk_ms,
                        )
                    except Exception as exc:
                        logger.warning("Bootstrap extended wait failed (attempt=%s): %s", attempt, exc)

                blocked = (
                    "human verification" in title.lower()
                    or "awswafcookiedomainlist" in content_sample
                    or html_size < 6000
                )
                logger.info("Bootstrap attempt=%s  html=%d  blocked=%s  engine=%s",
                            attempt, html_size, blocked, sess.engine.value)

                if blocked:
                    sess.record_blocked()
                    # Obscura often serves a ~4.5k WAF shell that never grows — switch to Chromium
                    # on first small-page block instead of burning multiple full attempts.
                    need_chromium = sess.engine is Engine.OBSCURA and (
                        html_size < 8000 or sess.consecutive_errors >= 2
                    )
                    if need_chromium:
                        logger.warning(
                            "Obscura cookie bootstrap stuck (html=%s consecutive_errors=%s) — "
                            "switching browser for capture.",
                            html_size,
                            sess.consecutive_errors,
                        )
                        await sess.switch_to_chromium()
                        ctx = await new_stealth_context(sess.browser, locale="en-US")
                        page = await ctx.new_page()
                    continue

                # Wait for networkidle and poll for _rodeo_session (set by XE's AJAX on page load).
                cookies = await _poll_for_session_cookie(ctx, page)

                # Inject CSRF token as cookie if not already present — the token lives in
                # the HTML meta tag and must be sent as x-csrf-token header on API calls.
                cookie_names = {c.get("name") for c in cookies}
                if "csrf_token" not in cookie_names:
                    csrf_match = re.search(
                        r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)',
                        html,
                    )
                    if csrf_match:
                        cookies.append({
                            "name": "csrf_token",
                            "value": csrf_match.group(1),
                            "domain": "www.xe.gr",
                            "path": "/",
                            "httpOnly": False,
                            "secure": True,
                        })
                        logger.info("Injected CSRF token from HTML meta tag.")

                if cookies:
                    cfg.COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with cfg.COOKIES_PATH.open("w", encoding="utf-8") as fh:
                        json.dump(cookies, fh, ensure_ascii=False, indent=2)
                    logger.info("Captured %s cookies via %s (attempt=%s).",
                                len(cookies), sess.engine.value, attempt)
                    return cookies

                logger.warning("No cookies captured despite unblocked page (attempt=%s).", attempt)

            # All attempts failed — restore backup if available, otherwise return empty.
            if force and backup:
                logger.warning("Bootstrap failed; restoring %s backup cookies.", len(backup))
                with cfg.COOKIES_PATH.open("w", encoding="utf-8") as fh:
                    json.dump(backup, fh, ensure_ascii=False, indent=2)
                return backup

            logger.error("Cookie bootstrap failed after all attempts. Proceeding with empty session.")
            return []
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


async def get_valid_cookies(force: bool = False) -> list[dict[str, Any]]:
    if force:
        logger.info("Forced cookie refresh requested.")
        return await _ensure_cookies_with_optional_force(force=True)

    try:
        cookies = load_cookies(cfg.COOKIES_PATH)
    except FileNotFoundError:
        logger.info("cookies.json missing. Capturing fresh cookies via Playwright.")
        return await _ensure_cookies_with_optional_force(force=False)
    except Exception as exc:
        logger.warning("cookies.json unreadable (%s). Re-capturing cookies.", exc)
        return await _ensure_cookies_with_optional_force(force=False)

    if _cookies_have_valid_entry(cookies):
        logger.info("Reusing existing cookies from %s", cfg.COOKIES_PATH)
        return cookies

    # Session hygiene: keep using existing cookie jar until blocked errors explicitly trigger force refresh.
    logger.warning("cookies.json appears expired; reusing until blocked response triggers forced refresh.")
    return cookies


def _cookie_dict_for_xe_session(cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Prefer ``www.xe.gr`` cookie values when CDP returns duplicate names across domains."""
    def _domain_rank(domain: str) -> int:
        d = (domain or "").lower()
        if "www.xe.gr" in d:
            return 2
        if "xe.gr" in d:
            return 1
        return 0

    out: dict[str, str] = {}
    ordered = sorted(
        (c for c in cookies if c.get("name")),
        key=lambda c: _domain_rank(str(c.get("domain", ""))),
    )
    for c in ordered:
        v = c.get("value")
        if v is not None:
            out[str(c["name"])] = str(v)
    return out


def build_impersonated_session(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session(impersonate="chrome131")
    cookie_dict = _cookie_dict_for_xe_session(cookies)
    session.cookies.update({k: v for k, v in cookie_dict.items() if v is not None})
    csrf_token = str(cookie_dict.get("csrf_token", "") or "")
    session.headers.update(
        {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "referer": cfg.RESULTS_REFERER,
            "x-requested-with": "XMLHttpRequest",
            "x-csrf-token": csrf_token,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


async def get_impersonated_session(force_cookies: bool = False) -> requests.Session:
    cookies = await get_valid_cookies(force=force_cookies)
    return build_impersonated_session(cookies)

