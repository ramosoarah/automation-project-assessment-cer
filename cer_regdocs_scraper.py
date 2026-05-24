#!/usr/bin/env python3
"""
CER REGDOCS Scraper
====================
Downloads all documents (PDF or HTML-printed-to-PDF) from a pre-filtered
Canada Energy Regulator REGDOCS Advanced Search URL.

Requires: Python 3.10+, Google Chrome, internet access.
See README for pip install commands.

Usage:
  python cer_regdocs_scraper.py
  python cer_regdocs_scraper.py --url "https://apps.cer-rec.gc.ca/REGDOCS/Search/AdvancedResults?sd=2026-03-01&ed=2026-04-01&rds=..."
"""

import argparse
import base64
import hashlib
import json
import logging
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import requests as _requests

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


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

# Timeouts (seconds)
PAGE_LOAD_TIMEOUT: int = 45
ELEMENT_WAIT_TIMEOUT: int = 20
DOWNLOAD_POLL_INTERVAL: float = 1.5
DOWNLOAD_TIMEOUT: int = 180
JS_RENDER_PAUSE: float = 2.5   # extra pause after page load for JS rendering

# How many times to retry a failed page before skipping
MAX_RETRIES: int = 3
RETRY_BACKOFF: float = 4.0   # seconds between retries (doubles each attempt)

HEADLESS: bool = False   # set True for server/CI environments


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

    # File handler — verbose
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler — info and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ─────────────────────────────────────────────────────────────────────────────
# FILENAME / PATH UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str, max_len: int = 180) -> str:
    """Strip characters that are illegal on Windows or Linux filesystems."""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = name.strip("._")
    return name[:max_len] or "unnamed"


def url_to_stem(url: str) -> str:
    """Return a clean filename stem derived from the last path segment of a URL."""
    path = urlparse(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if "/" in path else path
    segment = segment.split("?")[0].split("#")[0]
    if not segment:
        segment = hashlib.md5(url.encode()).hexdigest()[:12]
    return sanitize_filename(segment)


def make_pdf_filename(url: str, suffix: str = "") -> str:
    """Build a .pdf filename from a URL, with an optional descriptive suffix."""
    stem = url_to_stem(url)
    if suffix:
        safe_suffix = sanitize_filename(suffix)[:60]
        return f"{stem}__{safe_suffix}.pdf"
    name = stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"
    return name


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER SETUP
# ─────────────────────────────────────────────────────────────────────────────

def build_driver(download_dir: Path) -> webdriver.Chrome:
    """
    Create a configured Chrome WebDriver.
    Forces PDFs to download (not open in-browser) via Chrome preferences.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    download_path_str = str(download_dir.resolve())

    prefs: dict = {
        # Download silently to our folder
        "download.default_directory": download_path_str,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        # Force PDFs to download instead of being rendered in the browser
        "plugins.always_open_pdf_externally": True,
        "plugins.plugins_disabled": ["Chrome PDF Viewer"],
        "safebrowsing.enabled": True,
    }

    options = ChromeOptions()
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    return driver


# ─────────────────────────────────────────────────────────────────────────────
# CHROME DEVTOOLS PROTOCOL  (CDP) — HTML → PDF
# ─────────────────────────────────────────────────────────────────────────────

def cdp_print_to_pdf(driver: webdriver.Chrome, output_path: Path) -> bool:
    """
    Print the currently loaded page to a PDF file using CDP Page.printToPDF.
    This is the only reliable way to save HTML pages as PDFs from Selenium.
    Returns True on success.
    """
    try:
        params = {
            "printBackground": True,
            "paperWidth": 8.5,      # inches (US Letter)
            "paperHeight": 11.0,
            "marginTop": 0.4,
            "marginBottom": 0.4,
            "marginLeft": 0.4,
            "marginRight": 0.4,
            "preferCSSPageSize": False,
            "transferMode": "ReturnAsBase64",
        }
        result = driver.execute_cdp_cmd("Page.printToPDF", params)
        pdf_bytes = base64.b64decode(result["data"])
        output_path.write_bytes(pdf_bytes)
        return True
    except Exception as exc:
        logging.getLogger("regdocs").error(
            f"CDP printToPDF failed for {output_path.name}: {exc}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_pdfs(directory: Path) -> set[str]:
    """Return the current set of .pdf filenames in a directory."""
    return {f.name for f in directory.glob("*.pdf")}


def wait_for_download_completion(directory: Path, timeout: int = DOWNLOAD_TIMEOUT) -> bool:
    """
    Block until all in-progress downloads (.crdownload / .tmp) finish.
    Returns True if the directory is clean before the timeout.
    """
    logger = logging.getLogger("regdocs")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        in_progress = (
            list(directory.glob("*.crdownload"))
            + list(directory.glob("*.tmp"))
            + list(directory.glob("*.partial"))
        )
        if not in_progress:
            return True
        logger.debug(
            f"Waiting for download — in-progress files: "
            f"{[f.name for f in in_progress]}"
        )
        time.sleep(DOWNLOAD_POLL_INTERVAL)
    logger.warning("Download did not complete within timeout.")
    return False


def trigger_browser_download(
    driver: webdriver.Chrome,
    url: str,
    download_dir: Path,
    visited_urls: set[str],
    downloaded_names: set[str],
    logger: logging.Logger,
) -> bool:
    """
    Download a PDF, trying two methods in order:
      1. requests.get() with the browser's live session cookies (fast, reliable).
      2. Browser navigation fallback — for servers that reject non-browser clients.

    Returns True if a new file was confirmed in the download directory.
    """
    filename = make_pdf_filename(url)

    if url in visited_urls:
        logger.info(f"  [skip-url]  Already attempted: {url}")
        return False

    if filename in downloaded_names:
        logger.info(f"  [skip-file] Already downloaded: {filename}")
        visited_urls.add(url)
        return False

    visited_urls.add(url)
    output_path = download_dir / filename
    logger.info(f"  [download]  {filename}")
    logger.debug(f"             URL: {url}")

    # ── Primary: requests download with browser session cookies ──────────────
    try:
        session_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        user_agent = driver.execute_script("return navigator.userAgent;")
        resp = _requests.get(
            url,
            cookies=session_cookies,
            headers={"User-Agent": user_agent, "Referer": BASE_URL},
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            output_path.write_bytes(resp.content)
            downloaded_names.add(output_path.name)
            logger.info(
                f"  [saved]     {output_path.name} "
                f"({output_path.stat().st_size:,} bytes)"
            )
            return True
        logger.debug(
            f"  [non-pdf]   content-type={content_type!r} — trying browser fallback"
        )
    except Exception as exc:
        logger.debug(f"  [req-fail]  requests failed ({exc}) — trying browser fallback")

    # ── Fallback: navigate the browser to the URL and let Chrome download it ───
    before = snapshot_pdfs(download_dir)
    try:
        driver.get(url)
    except TimeoutException:
        # Normal: Chrome starts a download and navigation "times out"
        pass
    except WebDriverException as exc:
        # ERR_ABORTED is also normal for direct-download URLs
        if "net::ERR_ABORTED" not in str(exc) and "timeout" not in str(exc).lower():
            logger.warning(f"  [warn]      WebDriver error navigating to {url}: {exc}")

    ok = wait_for_download_completion(download_dir)
    after = snapshot_pdfs(download_dir)
    new_files = after - before

    if new_files:
        downloaded_names.update(new_files)
        logger.info(f"  [saved]     {sorted(new_files)}")
        return True

    logger.warning(f"  [no-file]   No new PDF appeared for {url}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# PAGINATION
# ─────────────────────────────────────────────────────────────────────────────

def detect_total_pages(driver: webdriver.Chrome, logger: logging.Logger) -> int:
    """
    Detect the total number of result pages from the REGDOCS search UI.
    Tries several selector strategies and falls back to 1.
    """
    strategies = [
        # PagedList "skip to last" link — href ends with &p=N
        "//li[contains(@class,'PagedList-skipToLast')]/a",
        "//li[contains(@class,'last')]/a[contains(@href,'p=')]",
        # Generic "last page" links
        "//a[@rel='last']",
        "//a[contains(@class,'last-page')]",
        # Any pagination link — we'll take the maximum page number
        "//ul[contains(@class,'pagination')]//a[contains(@href,'p=')]",
        "//nav[contains(@aria-label,'age')]//a[contains(@href,'p=')]",
        "//div[contains(@class,'pager')]//a[contains(@href,'p=')]",
    ]

    max_page = 1
    for xpath in strategies:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                href = el.get_attribute("href") or ""
                m = re.search(r"[?&]p=(\d+)", href)
                if m:
                    max_page = max(max_page, int(m.group(1)))
                text = el.text.strip()
                if text.isdigit():
                    max_page = max(max_page, int(text))
        except Exception:
            continue

    if max_page > 1:
        logger.info(f"Detected {max_page} result pages.")
    else:
        # Try reading a "showing X of Y" counter
        try:
            counter_el = driver.find_element(
                By.XPATH,
                "//*[contains(text(),' of ') and "
                "(contains(text(),'result') or contains(text(),'Result'))]",
            )
            m = re.search(r"of\s+(\d+)", counter_el.text.replace(",", ""))
            if m:
                total_results = int(m.group(1))
                # REGDOCS defaults to 20 results per page
                max_page = max(1, -(-total_results // 20))
                logger.info(
                    f"Inferred {max_page} pages from '{counter_el.text.strip()}'"
                )
        except Exception:
            logger.info("Could not determine page count — assuming 1 page.")

    return max_page


def paginated_url(base: str, page: int) -> str:
    """Append ?p=N or &p=N to the search URL."""
    if page == 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}p={page}"


# ─────────────────────────────────────────────────────────────────────────────
# RESULT LINK EXTRACTION  (from search results page)
# ─────────────────────────────────────────────────────────────────────────────

# REGDOCS result items consistently link to /REGDOCS/Item/<id> or /REGDOCS/File/<id>
_RESULT_HREF_PATTERNS: list[str] = [
    "/REGDOCS/Item/",
    "/REGDOCS/File/",
    "/REGDOCS/Document/",
]

def extract_search_result_links(
    driver: webdriver.Chrome, logger: logging.Logger
) -> list[str]:
    """
    Collect all unique document/item links from the current search results page.
    Returns absolute URLs.
    """
    links: set[str] = set()

    try:
        all_anchors = driver.find_elements(By.TAG_NAME, "a")
        for anchor in all_anchors:
            try:
                href = (anchor.get_attribute("href") or "").strip()
            except StaleElementReferenceException:
                continue

            if not href:
                continue

            # Resolve relative URLs
            if href.startswith("/"):
                href = BASE_URL + href
            elif not href.startswith("http"):
                continue

            if any(p in href for p in _RESULT_HREF_PATTERNS):
                # Strip fragment identifiers
                href = href.split("#")[0]
                links.add(href)

    except Exception as exc:
        logger.error(f"Error collecting result links: {exc}", exc_info=True)

    logger.info(f"  Found {len(links)} result links on page.")
    return sorted(links)


# ─────────────────────────────────────────────────────────────────────────────
# PDF LINK EXTRACTION  (from a document/item result page)
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that strongly suggest a direct PDF file or download endpoint
_PDF_HREF_PATTERNS: list[tuple[str, str]] = [
    # (pattern_in_href, description)
    (".pdf",          "direct .pdf extension"),
    ("/PDF/",         "CER /PDF/ path segment"),
    ("/Download/",    "CER /Download/ endpoint"),
    ("/GetFile",      "CER GetFile handler"),
    ("filetype=pdf",  "filetype query param"),
    ("format=pdf",    "format query param"),
    ("type=pdf",      "type query param"),
    ("/File/",        "REGDOCS /File/ segment"),
]

def extract_pdf_links(
    driver: webdriver.Chrome,
    page_url: str,
    logger: logging.Logger,
) -> list[str]:
    """
    Find all probable PDF download links on the currently loaded result page.
    Returns a deduplicated list of absolute URLs.
    """
    pdf_urls: set[str] = set()

    try:
        anchors = driver.find_elements(By.TAG_NAME, "a")
        for anchor in anchors:
            try:
                href = (anchor.get_attribute("href") or "").strip()
                link_text = (anchor.text or "").strip().lower()
            except StaleElementReferenceException:
                continue

            if not href:
                continue

            # Resolve relative / protocol-relative URLs
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BASE_URL + href
            elif not href.startswith("http"):
                href = urljoin(page_url, href)

            href_lower = href.lower()

            # Skip mailto / javascript / anchor-only
            if href_lower.startswith(("mailto:", "javascript:", "#")):
                continue

            matched = any(pat in href_lower for pat, _ in _PDF_HREF_PATTERNS)

            # Also treat links whose visible text contains "pdf" or "download"
            if not matched and any(
                kw in link_text for kw in ("pdf", "download", "télécharger")
            ):
                matched = True

            if matched:
                pdf_urls.add(href.split("#")[0])

    except Exception as exc:
        logger.error(f"Error extracting PDF links from {page_url}: {exc}", exc_info=True)

    logger.debug(f"  PDF candidates found: {len(pdf_urls)}")
    return sorted(pdf_urls)


# ─────────────────────────────────────────────────────────────────────────────
# INTERMEDIATE PAGE HANDLING
# Some REGDOCS items open a listing page that itself links to documents.
# We detect this and recurse one level.
# ─────────────────────────────────────────────────────────────────────────────

def is_document_listing_page(driver: webdriver.Chrome) -> bool:
    """
    Return True if the current page appears to be a listing of sub-documents
    rather than a leaf document page.
    """
    try:
        # A listing page typically has a table or list of multiple document rows
        rows = driver.find_elements(
            By.XPATH,
            "//table//tr[.//a[contains(@href,'/REGDOCS/')]]",
        )
        return len(rows) > 1
    except Exception:
        return False


def collect_sub_document_links(
    driver: webdriver.Chrome,
    page_url: str,
    logger: logging.Logger,
) -> list[str]:
    """Extract sub-document links from an intermediate listing page."""
    links: set[str] = set()
    try:
        anchors = driver.find_elements(
            By.XPATH,
            "//table//a[contains(@href,'/REGDOCS/')]"
            "| //ul//a[contains(@href,'/REGDOCS/')]",
        )
        for anchor in anchors:
            try:
                href = (anchor.get_attribute("href") or "").strip()
            except StaleElementReferenceException:
                continue
            if not href:
                continue
            if href.startswith("/"):
                href = BASE_URL + href
            if any(p in href for p in _RESULT_HREF_PATTERNS):
                links.add(href.split("#")[0])
    except Exception as exc:
        logger.warning(f"Error collecting sub-document links: {exc}")

    logger.debug(f"  Sub-document links found on listing page: {len(links)}")
    return sorted(links)


# ─────────────────────────────────────────────────────────────────────────────
# STATE PERSISTENCE  (resumable runs)
# ─────────────────────────────────────────────────────────────────────────────

def load_visited(path: Path) -> set[str]:
    """Load previously visited URLs from disk (survives restarts)."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("visited", []))
        except Exception:
            pass
    return set()


def save_visited(path: Path, visited: set[str]) -> None:
    """Persist visited URLs atomically to avoid corruption on crash."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"visited": sorted(visited)}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────────
# PROCESSING A SINGLE RESULT PAGE
# ─────────────────────────────────────────────────────────────────────────────

def safe_navigate(
    driver: webdriver.Chrome,
    url: str,
    wait: WebDriverWait,
    logger: logging.Logger,
) -> bool:
    """
    Navigate to url with retry logic. Returns True if the page loaded.
    Marks the page as loaded once <body> is present.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            driver.get(url)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(JS_RENDER_PAUSE)
            return True
        except TimeoutException:
            logger.warning(
                f"  [timeout]   Attempt {attempt}/{MAX_RETRIES} loading {url}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
        except WebDriverException as exc:
            logger.error(
                f"  [error]     Attempt {attempt}/{MAX_RETRIES} — {exc}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

    return False


def process_result_page(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    result_url: str,
    download_dir: Path,
    visited_urls: set[str],
    downloaded_names: set[str],
    logger: logging.Logger,
    depth: int = 0,
) -> None:
    """
    Open one REGDOCS result/item page and:
      1. If it is an intermediate listing, recurse into each sub-document.
      2. If PDF links are found, download each one via the browser session.
      3. If no PDFs are found, print the page to PDF using CDP.

    depth is used to prevent infinite recursion on unexpected page structures.
    """
    if depth > 2:
        logger.warning(f"  [depth]     Max recursion depth reached for {result_url}")
        return

    if result_url in visited_urls:
        logger.debug(f"  [visited]   Already processed: {result_url}")
        return

    visited_urls.add(result_url)
    indent = "  " * depth

    logger.info(f"{indent}→ {result_url}")

    if not safe_navigate(driver, result_url, wait, logger):
        logger.error(f"{indent}[failed]    Could not load {result_url}")
        return

    # ── Check for intermediate listing page ──────────────────────────────────
    if depth == 0 and is_document_listing_page(driver):
        sub_links = collect_sub_document_links(driver, result_url, logger)
        logger.info(
            f"{indent}  [listing]  Found {len(sub_links)} sub-documents — recursing"
        )
        for sub_url in sub_links:
            process_result_page(
                driver, wait, sub_url, download_dir,
                visited_urls, downloaded_names, logger, depth + 1,
            )
            # Navigate back / reload current page after each sub-document
            if not safe_navigate(driver, result_url, wait, logger):
                break
        return

    # ── Look for PDF download links ───────────────────────────────────────────
    pdf_links = extract_pdf_links(driver, result_url, logger)

    if pdf_links:
        logger.info(f"{indent}  Found {len(pdf_links)} PDF link(s)")
        for pdf_url in pdf_links:
            trigger_browser_download(
                driver, pdf_url, download_dir,
                visited_urls, downloaded_names, logger,
            )
            # After a download the browser may be on a blank/download page.
            # Navigate back to the result page to continue with remaining PDFs.
            if len(pdf_links) > 1:
                if not safe_navigate(driver, result_url, wait, logger):
                    logger.warning(
                        f"{indent}  [warn]  Could not reload {result_url} "
                        "after download — skipping remaining PDFs on this page"
                    )
                    break

    else:
        # ── No PDF links — print page to PDF via CDP ─────────────────────────
        page_title = driver.title or ""
        filename = make_pdf_filename(result_url, suffix=page_title)
        output_path = download_dir / filename

        if output_path.exists():
            logger.info(f"{indent}  [exists]   HTML-PDF already saved: {filename}")
            downloaded_names.add(filename)
        else:
            logger.info(f"{indent}  [html→pdf] Printing page to PDF: {filename}")
            success = cdp_print_to_pdf(driver, output_path)
            if success:
                downloaded_names.add(filename)
                logger.info(f"{indent}  [saved]    {filename} ({output_path.stat().st_size:,} bytes)")
            else:
                logger.error(f"{indent}  [fail]     CDP print failed for {result_url}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    download_dir = OUTPUT_DIR / "pdfs"
    download_dir.mkdir(parents=True, exist_ok=True)
    visited_file = OUTPUT_DIR / "visited_urls.json"
    log_file = OUTPUT_DIR / "scraper.log"

    logger = setup_logging(log_file)
    logger.info("=" * 70)
    logger.info("CER REGDOCS Scraper  —  starting run")
    logger.info(f"Output directory : {OUTPUT_DIR.resolve()}")
    logger.info(f"Download directory: {download_dir.resolve()}")
    logger.info(f"Log file          : {log_file.resolve()}")
    logger.info("=" * 70)

    # Load resumption state
    visited_urls: set[str] = load_visited(visited_file)
    downloaded_names: set[str] = {f.name for f in download_dir.glob("*.pdf")}
    logger.info(
        f"Resuming: {len(visited_urls)} URLs already visited, "
        f"{len(downloaded_names)} PDFs already downloaded."
    )

    driver = build_driver(download_dir)
    wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

    try:
        # ── PHASE 1: collect all result links from every search page ──────────
        logger.info(f"\n[Phase 1] Loading search URL:\n  {SEARCH_URL}\n")
        if not safe_navigate(driver, SEARCH_URL, wait, logger):
            logger.critical("Could not load the search URL — aborting.")
            return

        total_pages = detect_total_pages(driver, logger)
        logger.info(f"Total result pages: {total_pages}")

        all_result_links: list[str] = []

        for page_num in range(1, total_pages + 1):
            if page_num > 1:
                page_url = paginated_url(SEARCH_URL, page_num)
                logger.info(f"\n[Page {page_num}/{total_pages}]  {page_url}")
                if not safe_navigate(driver, page_url, wait, logger):
                    logger.warning(
                        f"Could not load page {page_num} — skipping."
                    )
                    continue
            else:
                logger.info(f"\n[Page 1/{total_pages}]")

            page_links = extract_search_result_links(driver, logger)
            new_links = [l for l in page_links if l not in visited_urls]
            all_result_links.extend(new_links)
            logger.info(
                f"  Page {page_num}: {len(page_links)} links "
                f"({len(new_links)} new)  —  running total: {len(all_result_links)}"
            )

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_links: list[str] = []
        for link in all_result_links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        logger.info(
            f"\n[Phase 1 complete] "
            f"{len(unique_links)} unique result links to process.\n"
        )

        # ── PHASE 2: process each result link ─────────────────────────────────
        total = len(unique_links)
        for idx, result_url in enumerate(unique_links, 1):
            logger.info(f"\n[{idx:>4}/{total}] Processing result page")
            process_result_page(
                driver, wait, result_url,
                download_dir, visited_urls, downloaded_names, logger,
            )
            # Persist progress after every result so a crash loses minimal work
            save_visited(visited_file, visited_urls)

    except KeyboardInterrupt:
        logger.info("\nInterrupted by user (Ctrl+C) — saving progress.")
    except Exception as exc:
        logger.critical(f"Unhandled exception: {exc}", exc_info=True)
    finally:
        save_visited(visited_file, visited_urls)
        driver.quit()
        logger.info(
            f"\n{'=' * 70}\n"
            f"Run finished.\n"
            f"  PDFs downloaded : {len(downloaded_names)}\n"
            f"  URLs visited    : {len(visited_urls)}\n"
            f"  Output folder   : {download_dir.resolve()}\n"
            f"{'=' * 70}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download all documents from a CER REGDOCS Advanced Search URL."
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "The filtered REGDOCS Advanced Search URL to scrape. "
            "If omitted, uses the SEARCH_URL constant defined at the top of this file."
        ),
    )
    args = parser.parse_args()

    if args.url:
        SEARCH_URL = args.url

    run()
