"""Controlled Playwright profile. One temporary user-data directory, no extensions."""

from __future__ import annotations

from playwright.sync_api import sync_playwright


def render_controlled_page(url: str, *, user_data_dir: str) -> str:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir,
            headless=True,
            accept_downloads=False,
            args=["--disable-extensions", "--disable-sync", "--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            return page.locator("body").inner_text()
        finally:
            context.close()
