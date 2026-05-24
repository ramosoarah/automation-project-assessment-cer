# CER REGDOCS Scraper

A production-quality Selenium scraper for the Canada Energy Regulator REGDOCS
document portal. Downloads all PDFs (or converts HTML pages to PDF) from a
pre-filtered Advanced Search URL.

---

## Requirements

- Python 3.10 or later
- Microsoft Edge (Chromium-based) installed
- Windows 10/11 (also runs on Linux/macOS with minor path adjustments)

---

## Installation

```bash
pip install selenium webdriver-manager requests
pip install playwright
```

`webdriver-manager` automatically downloads the correct `msedgedriver.exe`
for your installed version of Edge.

---

## Quick start

```bash
python cer_regdocs_scraper.py
```

Downloads land in `regdocs_downloads/pdfs/`.
Progress is logged to `regdocs_downloads/scraper.log` and the console.
Visited URLs are persisted to `regdocs_downloads/visited_urls.json` so that
interrupted runs resume where they left off.

---

## Configuration (top of script)

| Constant | Default | Purpose |
|---|---|---|
| `SEARCH_URL` | CER Advanced Search URL | Starting URL with all filters pre-applied |
| `OUTPUT_DIR` | `regdocs_downloads/` | Root output directory |
| `HEADLESS` | `False` | Set `True` to run without opening a browser window |
| `PAGE_LOAD_TIMEOUT` | 45 s | Hard timeout for each page load |
| `DOWNLOAD_TIMEOUT` | 180 s | Max wait for a single PDF file to download |
| `MAX_RETRIES` | 3 | Retry count for failed page loads |
| `JS_RENDER_PAUSE` | 2.5 s | Extra pause after load to let JavaScript finish |

---

## Architecture

```
run()
├── Phase 1: Pagination loop
│   ├── safe_navigate()           — load each search results page with retry
│   ├── detect_total_pages()      — parse pagination to find page count
│   └── extract_search_result_links() — collect /REGDOCS/Item/ links
│
└── Phase 2: Per-result processing
    └── process_result_page()
        ├── is_document_listing_page()  — detect intermediate listing pages
        ├── collect_sub_document_links() — recurse one level if listing
        ├── extract_pdf_links()         — find PDF download URLs
        ├── trigger_browser_download()  — navigate browser to download URL
        │   └── wait_for_download_completion() — poll for .crdownload to clear
        └── cdp_print_to_pdf()          — CDP Page.printToPDF for HTML pages
```

### Key design decisions

**Why `requests.get()` with browser cookies instead of pure browser navigation?**
`requests` is faster and more reliable for file downloads. After the browser
establishes a session with REGDOCS, its cookies are extracted via
`driver.get_cookies()` and passed to `requests`, giving it the same auth
context. Browser navigation is kept as a fallback for servers that reject
non-browser User-Agent headers or use client-side redirects.

**Why `plugins.always_open_pdf_externally: True`?**
Without this preference Edge renders PDFs inside a browser tab and nothing is
written to disk. This flag forces Edge to save PDFs as files.

**Why CDP `Page.printToPDF` for HTML pages?**
It is the only way to produce byte-perfect PDFs from a rendered page without
a third-party tool. It captures JavaScript-rendered content, background
colours, and CSS layouts exactly as displayed.

**Why poll for `.crdownload` files?**
Selenium has no native download-complete event. Watching for the
browser's in-progress marker file (`.crdownload`) is the standard reliable
approach and avoids arbitrary `time.sleep()` calls.

**Why JSON for visited-URL persistence?**
Human-readable, easy to inspect/edit between runs, and atomic-write safe (we
write to `.tmp` then rename so a crash mid-write never corrupts the file).

---

## Output structure

```
regdocs_downloads/
├── pdfs/
│   ├── 12345.pdf          ← downloaded PDF
│   ├── 67890__Title.pdf   ← HTML page printed to PDF
│   └── ...
├── visited_urls.json      ← resumption state
└── scraper.log            ← full debug log
```

---

## Resuming an interrupted run

Simply run the script again. `visited_urls.json` is loaded at startup and
any URL already in it is skipped. Existing PDFs in `pdfs/` are also detected
at startup and their names are added to the `downloaded_names` set, preventing
duplicate downloads.

---

## Troubleshooting

### `WebDriverException: 'msedgedriver' executable needs to be in PATH`
`webdriver-manager` should handle this automatically. If it fails:
```bash
pip install --upgrade webdriver-manager
```
Or download `msedgedriver.exe` manually from
https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/
and place it on your PATH.

### PDFs open in the browser instead of downloading
Check that `plugins.always_open_pdf_externally` is being applied. If you are
using a managed or enterprise Edge profile, group policy may override this
setting. Try setting `HEADLESS = True` in the config, or pass a
`--user-data-dir` pointing to a fresh temporary directory in `build_edge_driver`.

### `TimeoutException` on every page
The site may be slow or rate-limiting the scraper. Increase
`PAGE_LOAD_TIMEOUT` and `JS_RENDER_PAUSE` in the configuration block.

### No result links are collected
REGDOCS may have changed its HTML structure. Open the search URL in Edge,
right-click a result link, and inspect the `href`. Update
`_RESULT_HREF_PATTERNS` in the script to match the new path prefix.

### `CDP printToPDF` returns empty PDF or fails
- The page may have redirected to a login screen — check the browser window.
- The browser tab may have crashed — increase `JS_RENDER_PAUSE`.
- The Edge version may be too old — update Edge.

### Downloads go to the wrong folder
`pathlib.Path.resolve()` converts the path to an absolute string. If the
working directory changes between runs, delete `visited_urls.json` and
restart, or set `OUTPUT_DIR` to an absolute path.

---

## Scaling / performance improvements

| Improvement | How |
|---|---|
| **Parallel processing** | Run multiple browser instances with `ThreadPoolExecutor`. Each worker needs its own `download_dir` sub-folder; merge results afterward. |
| **Rate limiting** | Add a configurable `REQUEST_DELAY` between navigations to avoid being blocked. |
| **Incremental date ranges** | Parameterise `sd`/`ed` in `SEARCH_URL` and loop over monthly windows. |
| **Database-backed state** | Replace `visited_urls.json` with SQLite for faster lookups on large runs. |
| **Headless CI execution** | Set `HEADLESS = True` and run in GitHub Actions or Azure Pipelines. |
| **Content-based deduplication** | Hash each downloaded PDF and skip if the hash already exists — catches the same document under different URLs. |
| **Retry queue** | Collect failed URLs and retry them in a second pass at the end of each run. |
