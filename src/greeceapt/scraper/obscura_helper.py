"""
Obscura engine helper — shared by GreeceApt code paths that need a real browser (XE cookie bootstrap).

Starts Obscura as a CDP server subprocess, connects Playwright via connect_over_cdp.
Startup waits up to ``GREECEAPT_OBSCURA_CDP_WAIT_SEC`` (default 45s) per port and tries
``GREECEAPT_OBSCURA_PORTS`` or built-in falloffs if the first port is slow or busy.

When ``./obscura`` exists, **Chromium fallback is off by default** so sessions stay on Obscura
(usually faster / less WAF-prone). Set ``GREECEAPT_CHROMIUM_FALLBACK=1`` if Obscura fails to
start and you need stock Chromium. If the binary is absent, Chromium is used automatically.

XE cookie bootstrap passes ``allow_chromium_escape=True`` so repeated WAF blocks can switch
to headed Chromium without that env var; disable with ``GREECEAPT_XE_COOKIE_NO_CHROMIUM_ESCAPE=1``.

Usage:
    async with engine_session(workers=20) as sess:
        ctx = await new_stealth_context(sess.browser)
        page = await ctx.new_page()
        ...
        sess.record_blocked()   # call on 403 / 405
        sess.record_ok()        # call on 200
        if sess.consecutive_errors >= FALLBACK_THRESHOLD:
            await sess.switch_to_chromium()  # rotates Obscura when fallback is disabled
"""
import asyncio
import logging
import os
import stat
import subprocess
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncIterator

import httpx
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from greeceapt.db_helpers.paths import OBSCURA_PATH

STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['el-GR', 'el', 'en-US'] });
    window.chrome = { runtime: {} };
"""

CHROMIUM_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]

DEFAULT_PORT = 9222
DEFAULT_WORKERS = 20
FALLBACK_THRESHOLD = 5
# Obscura cold-start can exceed 10s; port 9224 may be busy — see _start_obscura_resolving_port.
OBSCURA_CDP_DEFAULT_WAIT_SEC = 45.0
OBSCURA_CDP_POLL_INTERVAL_SEC = 0.35

logger = logging.getLogger(__name__)


class Engine(Enum):
    OBSCURA = "obscura"
    CHROMIUM = "chromium"


def is_obscura_available() -> bool:
    if not OBSCURA_PATH.exists():
        return False
    mode = OBSCURA_PATH.stat().st_mode
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def allow_chromium_fallback() -> bool:
    """
    Explicit ``GREECEAPT_CHROMIUM_FALLBACK=0/1`` wins. Otherwise: if ``./obscura`` exists,
    default **no** Chromium fallback so Playwright stays on Obscura.
    """
    raw = os.getenv("GREECEAPT_CHROMIUM_FALLBACK")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in ("1", "true", "yes")
    return not is_obscura_available()


def _obscura_port_candidates(requested: int) -> list[int]:
    """
    Ports to try for ``obscura serve`` (first free / working wins).

    Override with ``GREECEAPT_OBSCURA_PORTS`` (comma-separated), e.g. ``9224,9244,9264``.
    """
    raw = os.getenv("GREECEAPT_OBSCURA_PORTS", "").strip()
    if raw:
        ports: list[int] = []
        for part in raw.split(","):
            p = part.strip()
            if p.isdigit():
                ports.append(int(p))
        if ports:
            return ports
    return [requested] + [requested + 20 * i for i in range(1, 8)]


async def _try_obscura_on_port(port: int, workers: int, max_wait_sec: float) -> subprocess.Popen | None:
    """Start Obscura on one port and wait until CDP answers or the process exits / times out."""
    try:
        proc = subprocess.Popen(
            [
                str(OBSCURA_PATH),
                "serve",
                "--port",
                str(port),
                "--stealth",
                "--workers",
                str(workers),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("Obscura spawn failed port=%s: %s", port, exc)
        return None

    cdp_url = f"http://localhost:{port}/json/version"
    deadline = time.monotonic() + max_wait_sec
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                logger.warning(
                    "Obscura process exited before CDP was ready (port=%s exit_code=%s). "
                    "Try another port via GREECEAPT_OBSCURA_PORTS or run `./obscura serve --port %s` for errors.",
                    port,
                    proc.returncode,
                    port,
                )
                return None
            try:
                if (await client.get(cdp_url, timeout=0.5)).status_code == 200:
                    logger.info("Obscura CDP ready on port=%s after startup wait.", port)
                    return proc
            except Exception:
                pass
            await asyncio.sleep(OBSCURA_CDP_POLL_INTERVAL_SEC)

    logger.warning(
        "Obscura did not expose CDP on port=%s within %.0fs — trying next candidate or giving up.",
        port,
        max_wait_sec,
    )
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    return None


async def _start_obscura_resolving_port(requested_port: int, workers: int) -> tuple[subprocess.Popen | None, int]:
    """
    Try Obscura on ``requested_port`` then fallbacks until CDP responds.

    ``GREECEAPT_OBSCURA_CDP_WAIT_SEC`` — max seconds to wait per port (default 45, clamped 5–120).
    """
    raw_wait = os.getenv("GREECEAPT_OBSCURA_CDP_WAIT_SEC", "").strip()
    try:
        max_wait = float(raw_wait) if raw_wait else OBSCURA_CDP_DEFAULT_WAIT_SEC
    except ValueError:
        max_wait = OBSCURA_CDP_DEFAULT_WAIT_SEC
    max_wait = max(5.0, min(max_wait, 120.0))

    for cand in _obscura_port_candidates(requested_port):
        proc = await _try_obscura_on_port(cand, workers, max_wait)
        if proc is not None:
            return proc, cand
    return None, requested_port


async def new_stealth_context(browser: Browser, locale: str = "el-GR") -> BrowserContext:
    """Create a new browser context with stealth JS and Greek locale."""
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale=locale,
        timezone_id="Europe/Athens",
        viewport={"width": 1280, "height": 800},
    )
    await ctx.add_init_script(STEALTH_JS)
    return ctx


async def safe_title(page) -> str:
    """Get page title safely — page.title() fails on Obscura CDP, evaluate is universal."""
    return (await page.evaluate("document.title") or "").strip()


class EngineSession:
    """
    Long-lived browser + subprocess handle with error tracking and Chromium fallback.

    Callers drive the fallback by calling record_blocked() / record_ok() after each
    page visit, then checking consecutive_errors against FALLBACK_THRESHOLD.
    """

    def __init__(
        self,
        browser: Browser,
        engine: Engine,
        proc: "subprocess.Popen | None",
        pw: Playwright,
        port: int,
        workers: int,
        *,
        chromium_headless: bool = True,
        allow_chromium_escape: bool = False,
    ) -> None:
        self.browser = browser
        self.engine = engine
        self._proc = proc
        self._pw = pw
        self._port = port
        self._workers = workers
        self._consecutive_errors = 0
        self._chromium_headless = chromium_headless
        self._allow_chromium_escape = allow_chromium_escape

    def record_blocked(self) -> None:
        self._consecutive_errors += 1
        logger.debug(
            "Obscura session: record_blocked consecutive_errors=%s engine=%s port=%s",
            self._consecutive_errors,
            self.engine.value,
            self._port,
        )
        if self._consecutive_errors == FALLBACK_THRESHOLD:
            logger.info(
                "Obscura session: scrape health — consecutive_errors reached FALLBACK_THRESHOLD=%s "
                "(engine=%s port=%s); caller may rotate or switch_to_chromium on next policy check.",
                FALLBACK_THRESHOLD,
                self.engine.value,
                self._port,
            )

    def record_ok(self) -> None:
        self._consecutive_errors = 0

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    async def switch_to_chromium(self) -> None:
        """
        Tear down the current Obscura session and reconnect via standard Chromium.
        Safe to call when engine is already CHROMIUM (becomes a no-op).

        When ``allow_chromium_fallback()`` is false and ``allow_chromium_escape`` was not set
        on this session (XE cookie bootstrap passes it true), this calls ``rotate()`` instead
        of launching stock Chromium.
        """
        if self.engine is Engine.CHROMIUM:
            return
        if not (allow_chromium_fallback() or self._allow_chromium_escape):
            logger.info(
                "Obscura session: switch_to_chromium — reason=chromium_fallback_disabled "
                "(GREECEAPT_CHROMIUM_FALLBACK not enabled); performing Obscura rotate instead "
                "consecutive_errors=%s port=%s",
                self._consecutive_errors,
                self._port,
            )
            await self.rotate()
            return
        reason = (
            "xe_cookie_bootstrap_escape"
            if self._allow_chromium_escape and not allow_chromium_fallback()
            else "chromium_fallback_env"
        )
        logger.info(
            "Obscura session: switch_to_chromium — reason=%s headless=%s consecutive_errors=%s port=%s",
            reason,
            self._chromium_headless,
            self._consecutive_errors,
            self._port,
        )
        try:
            await self.browser.close()
        except Exception:
            pass
        if self._proc:
            self._proc.kill()
            self._proc = None
        self.browser = await self._pw.chromium.launch(
            headless=self._chromium_headless,
            args=CHROMIUM_STEALTH_ARGS,
        )
        self.engine = Engine.CHROMIUM
        self._consecutive_errors = 0

    async def rotate(self) -> None:
        """
        Restart the browser process to flush accumulated memory (call every N listings).
        Restarts Obscura subprocess if available; falls back to a fresh Chromium launch.
        """
        logger.info(
            "Obscura session: rotate — reason=browser_process_restart engine=%s port=%s",
            self.engine.value,
            self._port,
        )
        try:
            await self.browser.close()
        except Exception:
            pass

        if self.engine is Engine.OBSCURA:
            if self._proc:
                self._proc.kill()
                self._proc = None
            proc, bound = await _start_obscura_resolving_port(self._port, self._workers)
            if proc is not None:
                try:
                    self.browser = await self._pw.chromium.connect_over_cdp(
                        f"http://localhost:{bound}"
                    )
                    self._proc = proc
                    self._port = bound
                    self._consecutive_errors = 0
                    return
                except Exception:
                    proc.kill()
            if not (allow_chromium_fallback() or self._allow_chromium_escape):
                raise RuntimeError(
                    "Obscura restart failed (rotate). Set GREECEAPT_CHROMIUM_FALLBACK=1 to use Chromium."
                )
            self.engine = Engine.CHROMIUM

        self.browser = await self._pw.chromium.launch(
            headless=self._chromium_headless,
            args=CHROMIUM_STEALTH_ARGS,
        )
        self._consecutive_errors = 0

    async def close(self) -> None:
        try:
            await self.browser.close()
        except Exception:
            pass
        if self._proc:
            self._proc.kill()
            self._proc = None


@asynccontextmanager
async def engine_session(
    workers: int = DEFAULT_WORKERS,
    port: int = DEFAULT_PORT,
    fallback_threshold: int = FALLBACK_THRESHOLD,
    chromium_headless: bool = True,
    allow_chromium_escape: bool = False,
    skip_obscura: bool = False,
) -> AsyncIterator[EngineSession]:
    """
    Async context manager: yields an EngineSession backed by Obscura when ``./obscura``
    exists and CDP starts, otherwise Chromium. When Obscura is present, Chromium fallback
    is off unless ``GREECEAPT_CHROMIUM_FALLBACK=1``. Callers may invoke ``switch_to_chromium()``
    on repeated blocks; with fallback off it becomes an Obscura ``rotate()`` instead.

    ``chromium_headless`` applies when launching stock Chromium (Obscura absent, fallback,
    escape hatch, or rotate fallback).

    ``allow_chromium_escape`` (XE cookie bootstrap): after repeated blocks, ``switch_to_chromium``
    may tear down Obscura and use Chromium with ``chromium_headless`` even when
    ``GREECEAPT_CHROMIUM_FALLBACK`` is unset.

    ``skip_obscura``: start with stock Chromium only (e.g. XE forced cookie refresh after Obscura
    WAF shell — avoids another slow Obscura bootstrap).
    """
    async with async_playwright() as pw:
        proc = None
        browser = None
        engine = Engine.CHROMIUM
        session_port = port

        if skip_obscura and is_obscura_available():
            logger.info("Obscura skipped for this session (skip_obscura=True).")
        elif is_obscura_available():
            proc, bound_port = await _start_obscura_resolving_port(port, workers)
            if proc is None:
                logger.warning(
                    "Obscura at %s did not become ready on any candidate port (starting from %s). "
                    "Tried GREECEAPT_OBSCURA_PORTS or built-in fallbacks; see GREECEAPT_OBSCURA_CDP_WAIT_SEC. "
                    "Run `./obscura serve --port %s` manually to inspect errors.",
                    OBSCURA_PATH,
                    port,
                    port,
                )
            else:
                try:
                    browser = await pw.chromium.connect_over_cdp(f"http://localhost:{bound_port}")
                    engine = Engine.OBSCURA
                    session_port = bound_port
                    logger.info(
                        "Playwright connected to Obscura CDP (port=%s, workers=%s).",
                        bound_port,
                        workers,
                    )
                except Exception as exc:
                    logger.warning("Obscura CDP connect on port %s failed: %s", bound_port, exc)
                    proc.kill()
                    proc = None
        else:
            logger.info("Obscura not used: no executable at %s", OBSCURA_PATH)

        if browser is None:
            if is_obscura_available() and not allow_chromium_fallback() and not skip_obscura:
                raise RuntimeError(
                    "Obscura binary exists but CDP did not start. Fix ./obscura or port, "
                    "or set GREECEAPT_CHROMIUM_FALLBACK=1 to use Chromium."
                )
            if is_obscura_available() and allow_chromium_fallback():
                logger.warning(
                    "Falling back to stock Chromium (Obscura failed; remove GREECEAPT_CHROMIUM_FALLBACK=1 "
                    "once Obscura works to stay on Obscura only).",
                )
            browser = await pw.chromium.launch(
                headless=chromium_headless,
                args=CHROMIUM_STEALTH_ARGS,
            )
            engine = Engine.CHROMIUM

        session = EngineSession(
            browser=browser,
            engine=engine,
            proc=proc,
            pw=pw,
            port=session_port,
            workers=workers,
            chromium_headless=chromium_headless,
            allow_chromium_escape=allow_chromium_escape,
        )
        try:
            yield session
        finally:
            await session.close()
