import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import Playwright, async_playwright

from greeceapt.cookies import util as cookie_util
from greeceapt.db_helpers.paths import COOKIES_JSON

DEFAULT_START_URL = "https://www.xe.gr/en/property/results"


def load_cookies(cookies_path: Path) -> list[dict[str, Any]]:
    """Load cookies from disk (supports list or {'cookies': [...]} formats)."""
    if not cookies_path.exists():
        raise FileNotFoundError(f"cookies.json not found at: {cookies_path}")

    with cookies_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    try:
        cookies = cookie_util.parse_cookie_json_root(data)
    except ValueError as e:
        raise ValueError(str(e)) from e

    n_exp = cookie_util.count_expired_cookies(cookies)
    if n_exp:
        print(
            f"[WARN] {n_exp} of {len(cookies)} cookies are expired. "
            "Consider re-running cookie capture."
        )

    return cookies


def save_cookies(cookies_path: Path, cookies: list[dict[str, Any]]) -> None:
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    with cookies_path.open("w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


async def capture_cookies_interactive(
    playwright: Playwright,
    cookies_path: Path,
    start_url: str = DEFAULT_START_URL,
) -> list[dict[str, Any]]:
    """
    Open a browser window, let the user pass verification/login,
    then persist context cookies to cookies_path.
    """
    browser = await playwright.chromium.launch(headless=False)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[WARN] Initial navigation failed ({e}). Continue manually in opened browser.")

        print("\n[COOKIES] Browser opened for cookie capture.")
        print("[COOKIES] Complete login/verification steps in the browser.")
        await asyncio.to_thread(input, "[COOKIES] Press Enter here when ready to save cookies: ")

        cookies = await context.cookies()
        if not cookies:
            raise RuntimeError("No cookies captured from browser context.")

        save_cookies(cookies_path, cookies)
        print(f"[COOKIES] Saved {len(cookies)} cookies -> {cookies_path}")
        return cookies
    finally:
        await browser.close()


async def ensure_cookies(
    playwright: Playwright,
    cookies_path: Path,
    start_url: str = DEFAULT_START_URL,
    auto_capture: bool = True,
) -> list[dict[str, Any]]:
    """
    Return valid cookies from disk. If missing/invalid and auto_capture=True,
    launch browser flow to capture and persist them automatically.
    """
    try:
        return load_cookies(cookies_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        if not auto_capture:
            raise
        print(f"[COOKIES] Existing cookies unavailable ({e}). Starting auto-capture flow.")
        return await capture_cookies_interactive(
            playwright=playwright,
            cookies_path=cookies_path,
            start_url=start_url,
        )


async def run_cli() -> None:
    async with async_playwright() as p:
        await ensure_cookies(
            playwright=p,
            cookies_path=COOKIES_JSON,
            start_url=DEFAULT_START_URL,
            auto_capture=True,
        )


if __name__ == "__main__":
    from greeceapt.logging_config import configure_root_logging

    configure_root_logging()
    asyncio.run(run_cli())
