#!/usr/bin/env python3
"""
CER REGDOCS Document Downloader
=================================
Automatically downloads PDF documents from a Canada Energy Regulator
REGDOCS Advanced Search results page.

PURPOSE
-------
The Canada Energy Regulator (CER) publishes regulatory filings in their
REGDOCS database. When you run an Advanced Search and filter by date range
or document type, you get a paginated list of documents. This tool takes
that filtered search URL and downloads every PDF from those results into
a folder on your computer — no clicking through pages manually.

HOW IT WORKS
------------
The REGDOCS site loads its search results using JavaScript, so a plain
HTTP request only returns an empty page shell. This tool uses a lightweight
headless browser (Playwright/Chromium) just for the part that requires
JavaScript — clicking through result pages and collecting document links.
Once it has all the links, it downloads every PDF using plain HTTP requests,
which is fast and doesn't need the browser at all.

  Step 1 — Open the search URL in a headless browser.
  Step 2 — Collect all document download links from the results.
  Step 3 — Click "Next 20 Results" and repeat until no more pages.
  Step 4 — For each collected link, download the PDF via HTTP.
  Step 5 — Save progress after each file so interrupted runs can resume.

REQUIREMENTS
------------
  Python 3.10+, internet access.

  Install dependencies (one time only):
    pip install playwright requests
    playwright install chromium

USAGE
-----
  python cer_regdocs_scraper.py
  python cer_regdocs_scraper.py --url "https://apps.cer-rec.gc.ca/REGDOCS/Search/AdvancedResults?..."
  python cer_regdocs_scraper.py --output my_folder
"""

import argparse
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  — edit these as needed
# ─────────────────────────────────────────────────────────────────────────────

SEARCH_URL: str = (
    "https://apps.cer-rec.gc.ca/REGDOCS/Search/AdvancedResults"
    "?sd=2026-02-01&ed=2026-03-01"
    "&rds=82%2C83%2C84%2C85%2C86%2C87%2C88%2C89%2C90%2C91"
    "%2C92%2C93%2C94%2C95%2C96%2C97%2C98%2C99%2C100%2C101"
    "%2C102%2C103%2C104%2C105"
)

BASE_URL: str = "https://apps.cer-rec.gc.ca"
OUTPUT_DIR: Path = Path("regdocs_downloads")

# Seconds to wait between download requests (avoids hammering the server)
DOWNLOAD_DELAY: float = 1.0
DOWNLOAD_TIMEOUT: int = 60
MAX_RETRIES: int = 3

# How long to wait (ms) after clicking "Next page" for results to reload
NEXT_PAGE_WAIT_MS: int = 3000


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("regdocs")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


# ─────────────────────────────────────────────────────────────────────────────
# FILENAME UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 180) -> str:
    """Remove characters that are invalid in file names on Windows and Linux."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name.strip()).strip("._")
    return (name or "unnamed")[:max_len]


def filename_from_url(url: str, fallback_id: str = "") -> str:
    """Derive a .pdf filename from a URL, using the last path segment."""
    segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    stem = safe_filename(segment) or fallback_id or "document"
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1: collect links using a headless browser
#
# The search results page loads its content via JavaScript. We launch a
# headless Chromium browser (bundled with Playwright — no system browser
# install needed), navigate to the search URL, and click through every
# results page to collect all file download links.
# ─────────────────────────────────────────────────────────────────────────────

def collect_all_links(search_url: str, logger: logging.Logger) -> tuple[set[str], set[str]]:
    """
    Open the search URL in a headless browser, paginate through every page
    of results, and return two sets of REGDOCS URLs:
      - download_links : /REGDOCS/File/Download/{id}  (direct PDF links)
      - item_links     : /REGDOCS/Item/View/{id}       (document detail pages)
    """
    download_links: set[str] = set()
    item_links: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_default_timeout(30_000)

        logger.info(f"  Opening search URL in headless browser...")
        page.goto(search_url)

        # Wait for the results container to be populated by JavaScript
        try:
            page.wait_for_selector("#divSearchResults a[href*='/REGDOCS/']", timeout=30_000)
        except PlaywrightTimeout:
            logger.warning("  Timed out waiting for results — page may have no results.")
            browser.close()
            return download_links, item_links

        page_num = 0
        while True:
            page_num += 1

            # Grab all relevant hrefs from the current results view
            hrefs: list[str] = page.eval_on_selector_all(
                "#divSearchResults a[href*='/REGDOCS/']",
                "els => els.map(e => e.getAttribute('href'))",
            )

            page_downloads = set()
            page_items = set()
            for href in hrefs:
                if not href:
                    continue
                full = BASE_URL + href if href.startswith("/") else href
                if "/REGDOCS/File/Download/" in full:
                    page_downloads.add(full)
                elif "/REGDOCS/Item/View/" in full:
                    page_items.add(full)

            download_links |= page_downloads
            item_links |= page_items

            logger.info(
                f"  Results page {page_num}: "
                f"{len(page_downloads)} download link(s), "
                f"{len(page_items)} item page(s)"
            )

            # Click "Next 20 Results" if the button exists
            next_btn = page.query_selector("a.next-page")
            if not next_btn:
                logger.info("  No more result pages.")
                break

            next_btn.click()
            page.wait_for_timeout(NEXT_PAGE_WAIT_MS)

        browser.close()

    return download_links, item_links


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1b: resolve Item/View pages to their File/Download links
#
# Some search results link to a document detail page (/REGDOCS/Item/View/N)
# rather than directly to a file. We call the companion API endpoint
# (/REGDOCS/Item/LoadResult/N) which returns the actual file list for that
# document — this works with plain HTTP, no browser needed.
# ─────────────────────────────────────────────────────────────────────────────

def resolve_item_pages(
    item_links: set[str],
    session: requests.Session,
    logger: logging.Logger,
) -> set[str]:
    """
    For each Item/View URL, call the LoadResult API to get its File/Download links.
    Returns the combined set of all resolved File/Download URLs.
    """
    resolved: set[str] = set()
    total = len(item_links)

    for idx, item_url in enumerate(sorted(item_links), 1):
        item_id = item_url.rstrip("/").rsplit("/", 1)[-1]
        load_url = f"{BASE_URL}/REGDOCS/Item/LoadResult/{item_id}"
        logger.debug(f"  [{idx}/{total}] LoadResult {item_id}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                time.sleep(DOWNLOAD_DELAY)
                resp = session.get(load_url, timeout=30)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                logger.warning(f"    Attempt {attempt} failed: {exc}")
                if attempt == MAX_RETRIES:
                    logger.error(f"    Giving up on {load_url}")
                    resp = None
                else:
                    time.sleep(3 * attempt)

        if resp is None:
            continue

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/REGDOCS/File/Download/" in href:
                full = BASE_URL + href if href.startswith("/") else href
                resolved.add(full)

    logger.info(f"  Resolved {len(resolved)} download link(s) from {total} item page(s).")
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: download each PDF via HTTP
# ─────────────────────────────────────────────────────────────────────────────

def download_pdf(
    session: requests.Session,
    url: str,
    dest: Path,
    logger: logging.Logger,
) -> bool:
    """Download one PDF. Skips if already saved. Returns True on success."""
    if dest.exists():
        logger.info(f"  [exists]  {dest.name}")
        return True

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(DOWNLOAD_DELAY)
            resp = session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            logger.info(f"  [saved]   {dest.name}  ({dest.stat().st_size:,} bytes)")
            return True
        except requests.RequestException as exc:
            logger.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(3 * attempt)

    logger.error(f"  [fail]    Could not download {url}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS TRACKING
# Saves completed download URLs to disk so re-running the script skips
# files that are already downloaded.
# ─────────────────────────────────────────────────────────────────────────────

def load_visited(path: Path) -> set[str]:
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")).get("visited", []))
        except Exception:
            pass
    return set()


def save_visited(path: Path, visited: set[str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"visited": sorted(visited)}, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run(search_url: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_dir = OUTPUT_DIR / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    visited_file = OUTPUT_DIR / "visited_urls.json"
    log_file = OUTPUT_DIR / "downloader.log"

    logger = setup_logging(log_file)
    logger.info("=" * 70)
    logger.info("CER REGDOCS Document Downloader")
    logger.info(f"Search URL : {search_url}")
    logger.info(f"Saving to  : {pdf_dir.resolve()}")
    logger.info("=" * 70)

    visited = load_visited(visited_file)
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": BASE_URL + "/",
        "Upgrade-Insecure-Requests": "1",
    })

    # ── Phase 1: collect all download links from search results ───────────────
    logger.info("\n[Phase 1] Collecting document links from search results...")
    download_links, item_links = collect_all_links(search_url, logger)
    logger.info(
        f"\n[Phase 1 complete] "
        f"{len(download_links)} direct download link(s), "
        f"{len(item_links)} item page(s) to resolve."
    )

    # ── Phase 1b: resolve Item/View pages to their download links ─────────────
    if item_links:
        logger.info("\n[Phase 1b] Resolving item pages to file links...")
        extra = resolve_item_pages(item_links, session, logger)
        download_links |= extra

    new_links = sorted(download_links - visited)
    logger.info(
        f"\n{len(download_links)} total download link(s) found. "
        f"{len(new_links)} not yet downloaded.\n"
    )

    # ── Phase 2: download every PDF ───────────────────────────────────────────
    logger.info("[Phase 2] Downloading PDFs...")
    downloaded = 0
    total = len(new_links)

    for idx, url in enumerate(new_links, 1):
        file_id = url.rstrip("/").rsplit("/", 1)[-1]
        dest = pdf_dir / filename_from_url(url, fallback_id=file_id)
        logger.info(f"\n[{idx:>4}/{total}]  {url}")
        ok = download_pdf(session, url, dest, logger)
        if ok:
            downloaded += 1
            visited.add(url)
            save_visited(visited_file, visited)

    logger.info(
        f"\n{'=' * 70}\n"
        f"Finished.\n"
        f"  PDFs saved  : {downloaded}\n"
        f"  Saved to    : {pdf_dir.resolve()}\n"
        f"  Full log    : {log_file.resolve()}\n"
        f"{'=' * 70}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download PDF documents from a CER REGDOCS Advanced Search URL."
    )
    parser.add_argument(
        "--url",
        default=SEARCH_URL,
        help="The REGDOCS Advanced Search URL to download from.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Folder to save PDFs into (default: regdocs_downloads/pdfs).",
    )
    args = parser.parse_args()

    if args.output:
        OUTPUT_DIR = Path(args.output)

    run(args.url)
