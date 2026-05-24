#!/usr/bin/env python3
"""
CER REGDOCS Document Downloader
=================================
Automatically downloads PDF documents from a Canada Energy Regulator
REGDOCS Advanced Search results page.

PURPOSE
-------
The Canada Energy Regulator (CER) publishes regulatory filings in their
REGDOCS database. When you run an Advanced Search on the REGDOCS website
and filter by date range or document type, you get a list of documents.
This tool takes that filtered search URL and downloads every PDF from
those results to a folder on your computer — instead of you clicking
each one manually.

HOW IT WORKS
------------
  1. Loads the search results page (handles multiple pages automatically)
  2. Collects links to each individual document listed in the results
  3. Opens each document page and finds the PDF download link
  4. Downloads each PDF into a local folder
  5. Saves progress as it goes — if interrupted, re-running it skips
     files already downloaded

REQUIREMENTS
------------
  Python 3.10+, internet access.
  Install dependencies: pip install requests beautifulsoup4

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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


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

# Seconds to pause between HTTP requests (avoids overloading the server)
REQUEST_DELAY: float = 1.0
REQUEST_TIMEOUT: int = 30
MAX_RETRIES: int = 3
RETRY_BACKOFF: float = 5.0     # seconds between retries (multiplied by attempt number)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP SESSION
# Sets browser-like headers so the server accepts our requests.
# ─────────────────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    })
    return session


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_page(
    session: requests.Session,
    url: str,
    logger: logging.Logger,
) -> BeautifulSoup | None:
    """
    Load a web page and return its parsed HTML.
    Retries up to MAX_RETRIES times on failure.
    Returns None if the page could not be loaded.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            logger.warning(f"  Attempt {attempt}/{MAX_RETRIES} failed for {url}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    logger.error(f"  Could not load {url} after {MAX_RETRIES} attempts — skipping.")
    return None


def download_pdf(
    session: requests.Session,
    url: str,
    dest: Path,
    logger: logging.Logger,
) -> bool:
    """
    Download a PDF file and save it to dest.
    Skips if the file already exists.
    Returns True on success.
    """
    if dest.exists():
        logger.info(f"  [exists]  {dest.name}")
        return True
    try:
        time.sleep(REQUEST_DELAY)
        resp = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info(f"  [saved]   {dest.name}  ({dest.stat().st_size:,} bytes)")
        return True
    except requests.RequestException as exc:
        logger.error(f"  [fail]    {url}: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# FILENAME UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 180) -> str:
    """Strip characters that are invalid in file names on Windows and Linux."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name.strip()).strip("._")
    return (name or "unnamed")[:max_len]


def pdf_filename_from_url(url: str) -> str:
    """Derive a .pdf filename from the last segment of a URL."""
    segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    stem = safe_filename(segment) or "document"
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


# ─────────────────────────────────────────────────────────────────────────────
# LINK EXTRACTION
# These functions read parsed HTML and pull out the URLs we care about.
# ─────────────────────────────────────────────────────────────────────────────

# URL path segments that indicate a REGDOCS document or item page
_RESULT_PATTERNS = ["/REGDOCS/Item/", "/REGDOCS/File/", "/REGDOCS/Document/"]

# URL patterns that suggest a direct PDF download link
_PDF_PATTERNS = [
    ".pdf", "/PDF/", "/Download/", "/GetFile",
    "filetype=pdf", "format=pdf", "type=pdf", "/File/",
]


def find_document_links(soup: BeautifulSoup) -> list[str]:
    """
    Find links to individual REGDOCS document pages within a search results page.
    Returns a list of absolute URLs.
    """
    links: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if href.startswith("/"):
            href = BASE_URL + href
        if any(pattern in href for pattern in _RESULT_PATTERNS):
            links.add(href.split("#")[0])
    return sorted(links)


def find_pdf_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """
    Find PDF download links on a document detail page.
    Returns a list of absolute URLs.
    """
    links: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        link_text = tag.get_text(strip=True).lower()

        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = BASE_URL + href
        elif not href.startswith("http"):
            href = urljoin(page_url, href)

        if href.lower().startswith(("mailto:", "javascript:", "#")):
            continue

        href_lower = href.lower()
        is_pdf_link = any(p in href_lower for p in _PDF_PATTERNS)
        is_download_text = any(kw in link_text for kw in ("pdf", "download", "télécharger"))

        if is_pdf_link or is_download_text:
            links.add(href.split("#")[0])

    return sorted(links)


def detect_total_pages(soup: BeautifulSoup) -> int:
    """
    Read the search results pagination to find how many result pages there are.
    Falls back to 1 if pagination cannot be detected.
    """
    max_page = 1
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        m = re.search(r"[?&]p=(\d+)", href)
        if m:
            max_page = max(max_page, int(m.group(1)))
        text = tag.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))
    return max_page


def paginated_url(base: str, page: int) -> str:
    """Append the page number parameter to a search URL."""
    if page == 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}p={page}"


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS TRACKING
# Saves visited URLs to disk so interrupted runs can resume without
# re-downloading files that are already complete.
# ─────────────────────────────────────────────────────────────────────────────

def load_visited(path: Path) -> set[str]:
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8"))["visited"])
        except Exception:
            pass
    return set()


def save_visited(path: Path, visited: set[str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"visited": sorted(visited)}, indent=2), encoding="utf-8")
    tmp.replace(path)


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
    session = make_session()

    # ── Phase 1: Collect all document page links from every results page ──────
    logger.info("\n[Phase 1] Reading search results...")

    first_page = fetch_page(session, search_url, logger)
    if first_page is None:
        logger.critical("Could not load the search URL. Check your internet connection or the URL.")
        return

    total_pages = detect_total_pages(first_page)
    logger.info(f"  Found {total_pages} page(s) of results.")

    all_doc_links: list[str] = list(find_document_links(first_page))
    logger.info(f"  Page 1: {len(all_doc_links)} document link(s)")

    for page_num in range(2, total_pages + 1):
        page_url = paginated_url(search_url, page_num)
        logger.info(f"  Page {page_num}: {page_url}")
        soup = fetch_page(session, page_url, logger)
        if soup is None:
            logger.warning(f"  Could not load page {page_num} — skipping.")
            continue
        links = find_document_links(soup)
        all_doc_links.extend(links)
        logger.info(f"  Page {page_num}: {len(links)} document link(s)")

    # Deduplicate while preserving order
    seen: set[str] = set()
    doc_links: list[str] = []
    for link in all_doc_links:
        if link not in seen:
            seen.add(link)
            doc_links.append(link)

    new_doc_links = [l for l in doc_links if l not in visited]
    logger.info(
        f"\n[Phase 1 complete] {len(doc_links)} total documents found, "
        f"{len(new_doc_links)} not yet downloaded.\n"
    )

    # ── Phase 2: Download PDFs from each document page ────────────────────────
    logger.info("[Phase 2] Downloading PDFs...")
    total = len(new_doc_links)
    downloaded = 0

    for idx, doc_url in enumerate(new_doc_links, 1):
        logger.info(f"\n[{idx:>4}/{total}]  {doc_url}")

        soup = fetch_page(session, doc_url, logger)
        if soup is None:
            continue

        pdf_links = find_pdf_links(soup, doc_url)

        if not pdf_links:
            logger.info("  No PDF links found on this page.")
        else:
            logger.info(f"  Found {len(pdf_links)} PDF link(s)")
            for pdf_url in pdf_links:
                if pdf_url in visited:
                    logger.info(f"  [skip]  Already downloaded.")
                    continue
                dest = pdf_dir / pdf_filename_from_url(pdf_url)
                ok = download_pdf(session, pdf_url, dest, logger)
                if ok:
                    downloaded += 1
                    visited.add(pdf_url)

        visited.add(doc_url)
        save_visited(visited_file, visited)

    logger.info(
        f"\n{'=' * 70}\n"
        f"Finished.\n"
        f"  PDFs saved    : {downloaded}\n"
        f"  Saved to      : {pdf_dir.resolve()}\n"
        f"  Full log      : {log_file.resolve()}\n"
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
