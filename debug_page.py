#!/usr/bin/env python3
"""Quick diagnostic: open the REGDOCS search page and dump what's actually there."""

from playwright.sync_api import sync_playwright

SEARCH_URL = (
    "https://apps.cer-rec.gc.ca/REGDOCS/Search/AdvancedResults"
    "?sd=2026-02-01&ed=2026-03-01"
    "&rds=82%2C83%2C84%2C85%2C86%2C87%2C88%2C89%2C90%2C91"
    "%2C92%2C93%2C94%2C95%2C96%2C97%2C98%2C99%2C100%2C101"
    "%2C102%2C103%2C104%2C105"
)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    page = browser.new_page()
    page.set_default_timeout(30_000)

    print("Navigating to search URL...")
    page.goto(SEARCH_URL)
    page.wait_for_load_state("networkidle", timeout=30_000)

    print("\n--- Screenshot saved to debug_screenshot.png ---")
    page.screenshot(path="debug_screenshot.png", full_page=True)

    print("\n--- All <a href> values on the page ---")
    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
    for h in hrefs:
        print(" ", h)

    print("\n--- IDs of all divs ---")
    div_ids = page.eval_on_selector_all("div[id]", "els => els.map(e => e.id)")
    for d in div_ids:
        print(" ", d)

    print("\n--- Page title ---")
    print(" ", page.title())

    print("\n--- #divSearchResults exists? ---")
    el = page.query_selector("#divSearchResults")
    print(" ", "YES" if el else "NO")

    print("\n--- a.next-page exists? ---")
    el = page.query_selector("a.next-page")
    print(" ", "YES" if el else "NO")

    browser.close()
