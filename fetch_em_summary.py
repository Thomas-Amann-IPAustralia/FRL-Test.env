"""
fetch_em_summary.py
====================
Tripwire sub-pipeline: given a legislation.gov.au URL and a compilation number,
identifies every Act that introduced amendments into that compilation, then
retrieves a plain-English explanation of what changed from ParlInfo.

OUTPUT
------
A markdown report saved to:
    em_summaries/<titleId>/EM_summary_<titleId>_<compilationNumber>.md

The report is also written to $GITHUB_STEP_SUMMARY so it renders directly
in the GitHub Actions run UI without needing to download the artifact.

USAGE
-----
    python fetch_em_summary.py <legislation_url> <compilation_number>

    e.g. python fetch_em_summary.py \\
             https://www.legislation.gov.au/C2004A04014/latest/versions C50

CONTENT SOURCE PRIORITY
------------------------
ParlInfo exposes bill information across several page types. We prefer the most
comprehensive plain-English source and fall back progressively:

    1. Bills Digest  — Key Points section (most readable, written for a lay audience)
    2. Summary ≥100w — Bill home page Summary section (author's own précis)
    3. Explan. Memo  — General Outline / Outline section (technical but complete)
    4. Summary <100w — Same as #2 but accepted even when short (last resort)

All marker strings are matched case-insensitively; each marker occupies its own
line/heading on the page.

AMENDING ACT DISCOVERY
-----------------------
The FRL API's Version object has a `reasons` array that SHOULD identify the Acts
that triggered each new compilation. In practice this array is inconsistently
populated, so three discovery layers run in sequence:

    Layer 1 — registerId check
        A compilation's registerId sometimes IS the amending Act's titleId
        (pattern C####A#####). This is the simplest and fastest path.

    Layer 2 — reasons array
        Walk Version.reasons checking both `amendedByTitle` and `affectedByTitle`.
        Both fields are checked independently because `amendedByTitle.titleId` is
        often empty while `affectedByTitle.titleId` is populated (or vice versa).
        The `markdown` field is also scanned as a last resort.

    Layer 3 — Affect API
        GET /v1/_AffectsSearch (falling back to /v1/Affect) with a filter on
        affectedTitleId. This is the most reliable source when reasons is empty,
        but it's slower so it runs last.

WAF BYPASS
----------
parlinfo.aph.gov.au is protected by an Azure WAF JS Challenge that returns
HTTP 403 to all plain HTTP clients regardless of headers. The challenge works
by injecting JavaScript that sets a cookie, then redirecting — a flow that
requires a real browser engine. We use selenium-stealth + headless Chromium,
which patches navigator.webdriver and other automation fingerprints that the
WAF uses to detect bots. Playwright does NOT patch these properties, which is
why it also receives 403.
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


# ===========================================================================
# Constants
# ===========================================================================

FRL_API          = "https://api.prod.legislation.gov.au/v1"
LEGISLATION_BASE = "https://www.legislation.gov.au"
PARLINFO_DISPLAY = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"

# Matches any FRL series identifier embedded in a URL or string.
# FRL IDs follow the pattern: one uppercase letter, 4 digits, one uppercase
# letter, 5-6 digits  (e.g. C2004A04014, F2021L01234).
TITLE_ID_RE = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")

# Restricts to Acts only: C + 4 digits + "A" + digits.
# The middle letter encodes the series type:
#   A = Act          (e.g. C2023A00074)  ← what we want
#   C = Compilation  (e.g. C2023C00385)  ← register ID, not an Act
#   L = Legislative Instrument           ← not relevant here
# Without this restriction, compilation register IDs would be mistaken for
# amending Acts — a bug that caused "No amending Acts found" in early versions.
ACT_SERIES_RE = re.compile(r"^C\d{4}A\d+$")

# Detects Explanatory Memorandum links on a bill home page.
# EM links contain the pattern legislation%2Fems%2F (URL-encoded path segment)
# followed by a bill-ID and UUID, e.g.:
#   ...legislation%2Fems%2Fr7096_ems_0ce6b86e-94e4-49ab-91c5-a1d2f9f0a608...
# The UUID is unique per EM upload and cannot be predicted — it must be
# discovered by scraping the bill home page.
EM_LINK_RE = re.compile(
    r'https?://parlinfo\.aph\.gov\.au/parlInfo/search/display/display\.w3p'
    r'[^\s"\'<>]*legislation%2Fems%2F[^\s"\'<>]+',
    re.IGNORECASE,
)

# Standard browser-like headers for requests to legislation.gov.au.
# parlinfo.aph.gov.au ignores these (WAF blocks regardless), but
# legislation.gov.au serves the amending Act's versions page with them.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}

# Threshold for preferring the full summary over the EM.
# Under this word count the Summary is considered too brief to be useful
# on its own and we try the Explanatory Memorandum instead.
MIN_SUMMARY_WORDS = 100


# ===========================================================================
# Logging helpers
# ===========================================================================

def log(msg: str) -> None:
    """Timestamped stdout log line — visible in GitHub Actions run logs."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_section(title: str) -> None:
    log("")
    log("─" * 60)
    log(f"  {title}")
    log("─" * 60)


# ===========================================================================
# FRL API helpers
# ===========================================================================

def extract_title_id(url: str) -> str:
    """
    Pull the FRL Title ID (e.g. C2004A04014) out of any legislation.gov.au URL.

    WHY: Users paste URLs in many formats (/latest/versions, /asmade/text,
    direct compilation URLs like /C2004A04014/2023-10-18) but the Title ID is
    always present as a distinct token. Regex extraction is more robust than
    URL parsing.
    """
    match = TITLE_ID_RE.search(url)
    if not match:
        raise ValueError(
            f"Could not extract a Title ID from: {url}\n"
            "Expected a URL like https://www.legislation.gov.au/C2015A00040/latest/versions"
        )
    return match.group(1)


def get_compilation(title_id: str, compilation_number: str) -> dict:
    """
    Fetch a specific compiled version of a title via the FRL OData API.

    WHY Versions/Find() rather than a list + filter:
    Find() is a bound function that accepts both titleId and compilationNumber
    as parameters and returns a single Version object directly. Using a filtered
    list ($filter=titleId eq '...' and compilationNumber eq '...') requires
    encoding OData syntax in the query string and returns an array wrapper —
    more fragile with no benefit for a single known target.

    WHY strip the leading 'C': The user inputs "C50" or "c50" (matching the
    FRL website display), but the API compilationNumber field stores the bare
    integer ("50"). Strip the prefix before passing it.
    """
    comp_num = re.sub(r"^[Cc]", "", compilation_number).strip()
    url = (
        f"{FRL_API}/Versions/Find("
        f"titleId='{title_id}',"
        f"compilationNumber='{comp_num}')"
    )
    log(f"FRL API -> {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError(
            f"Compilation '{compilation_number}' not found for '{title_id}'."
        )
    resp.raise_for_status()
    return resp.json()


# ===========================================================================
# Amending Act discovery
# ===========================================================================

def _add_act(
    seen: set, results: list,
    tid: str, name: str, affect: str, source: str,
) -> bool:
    """
    Add an amending Act to the results list if it passes validation.
    Returns True if added (used by Layer 2 to track whether a reason resolved).

    WHY a seen set: the three discovery layers may independently find the same
    amending Act. De-duplication here prevents duplicate ParlInfo fetches
    and duplicate report sections downstream.

    WHY ACT_SERIES_RE guard: compilation register IDs (C####C#####) appear in
    several API fields alongside Act IDs. Without the 'A' series check, a
    compilation ID would be passed to find_parlinfo_url(), which would then
    scrape a compiled version's page rather than an as-made Act's page and
    fail to find any ParlInfo EM link.
    """
    if tid and tid not in seen and ACT_SERIES_RE.match(tid):
        seen.add(tid)
        results.append({"titleId": tid, "name": name, "affect": affect, "source": source})
        return True
    return False


def discover_amending_acts(version_data: dict) -> list[dict]:
    """
    Identify all Acts that amended this compiled version.

    Runs three layers in sequence, accumulating results across all of them.
    Layers are not short-circuited — all three always run — because the same
    Act can appear in Layer 1 (registerId) and also in the reasons array, and
    we want the debug logging from all layers visible in the run log to aid
    diagnosis when something goes wrong.
    """
    seen: set[str] = set()
    results: list[dict] = []

    title_id    = version_data.get("titleId", "")
    register_id = version_data.get("registerId", "")
    start       = version_data.get("start", "")

    # ------------------------------------------------------------------
    # Layer 1: registerId check
    #
    # For compilations triggered by a single Act, the compilation's own
    # registerId is sometimes the amending Act's titleId rather than a
    # compiled-version ID. Example:
    #   C2016A00004  → registerId of the C10 compilation of C2004A01214
    #
    # WHY this happens: When only one Act amends a title and the compilation
    # is registered immediately after royal assent, the FRL register assigns
    # the compilation an ID in the Act series rather than the C####C##### 
    # compilation series.
    # ------------------------------------------------------------------
    if ACT_SERIES_RE.match(register_id):
        log(f"  Layer 1 (registerId): {register_id} is an amending Act")
        _add_act(seen, results, register_id, "", "Amend", "registerId")
    else:
        log(f"  Layer 1 (registerId): {register_id} is not an Act series ID")

    # ------------------------------------------------------------------
    # Layer 2: reasons array
    #
    # Version.reasons is the canonical source for amendment relationships.
    # Each reason has two potential title-reference fields:
    #   amendedByTitle   — the Act that made the amendment
    #   affectedByTitle  — the Act or instrument that affected this title
    #
    # WHY check both independently (no break after first):
    # In practice, `amendedByTitle.titleId` is frequently an empty string
    # while `affectedByTitle.titleId` holds the correct Act ID (and vice
    # versa). Early versions broke out of the loop after `amendedByTitle`,
    # silently skipping `affectedByTitle` and producing "No amending Acts
    # found" even when the data was present.
    #
    # WHY scan markdown as a last resort:
    # The `markdown` field is a human-readable description generated by the
    # FRL system, e.g. "Amended by [C2018A00099](link) Some Amendment Act".
    # When both structured fields have empty titleIds, the Act ID is still
    # recoverable from this text via regex. It is a last resort because the
    # markdown format is not part of the API contract and could change.
    # ------------------------------------------------------------------
    reasons = version_data.get("reasons", [])
    log(f"  Layer 2 (reasons): {len(reasons)} reason(s)")
    for i, reason in enumerate(reasons):
        affect = reason.get("affect", "Amend")
        log(f"    reason[{i}]: affect={affect!r} keys={list(reason.keys())}")
        found_via_title = False
        for key in ("amendedByTitle", "affectedByTitle"):
            obj = reason.get(key) or {}
            if not isinstance(obj, dict):
                log(f"      {key}: not a dict ({type(obj).__name__})")
                continue
            tid  = obj.get("titleId", "")
            name = obj.get("name", "")
            log(f"      {key}: titleId={tid!r} matches={bool(ACT_SERIES_RE.match(tid)) if tid else False}")
            if tid and _add_act(seen, results, tid, name, affect, f"reasons[{i}].{key}"):
                found_via_title = True
        # Markdown fallback
        if not found_via_title:
            markdown = reason.get("markdown", "") or ""
            for tid in re.findall(r"C\d{4}A\d+", markdown):
                log(f"      markdown scan: found {tid!r}")
                _add_act(seen, results, tid, "", affect, f"reasons[{i}].markdown")

    # ------------------------------------------------------------------
    # Layer 3: Affect API
    #
    # The FRL API exposes affect relationships via two endpoints:
    #   /v1/_AffectsSearch  — dedicated search context (preferred per API docs)
    #   /v1/Affect          — EntitySet (fallback; sometimes returns 404)
    #
    # WHY try both: the API documentation recommends _AffectsSearch for
    # targeted affect queries, but in testing /v1/Affect was sometimes the
    # one that responded. We try _AffectsSearch first and fall back to Affect.
    #
    # WHY filter by date: a broad query for all Acts that ever affected this
    # title could return dozens of historical entries. We filter by the
    # compilation's start date to narrow to only the Acts that triggered THIS
    # specific compilation. If date-filtering yields nothing (e.g. the API
    # stores dates in a different timezone or format), we retry without the
    # date filter as a safety net.
    #
    # WHY OData $filter uses quote() not urlencode():
    # urlencode() encodes the dollar sign in "$filter" as "%24filter", which
    # the FRL OData layer does not recognise — it expects a literal "$".
    # quote() is applied only to the filter EXPRESSION (the value after "="),
    # leaving the "$filter" key name unencoded.
    # ------------------------------------------------------------------
    comp_date = start[:10] if start else ""
    log(f"  Layer 3 (Affect API): affectedTitleId={title_id}, date={comp_date}")
    filter_expr = f"affectedTitleId eq '{title_id}'"
    affect_endpoints = [
        f"{FRL_API}/_AffectsSearch?$filter={quote(filter_expr)}&$top=50",
        f"{FRL_API}/Affect?$filter={quote(filter_expr)}&$top=50",
    ]
    for url in affect_endpoints:
        log(f"  Affect API -> {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                log("  404 — trying next endpoint")
                continue
            resp.raise_for_status()
            entries = resp.json().get("value", [])
            log(f"  Affect API returned {len(entries)} entries")
            matched = (
                [e for e in entries if str(e.get("dateChanged", ""))[:10] == comp_date]
                if comp_date else entries
            )
            if not matched and comp_date:
                log("  No date-matched entries — using all entries")
                matched = entries
            for entry in matched:
                tid = entry.get("affectingTitleId", "")
                obj = entry.get("affectingTitle") or {}
                name = obj.get("name", "") if isinstance(obj, dict) else ""
                _add_act(seen, results, tid, name, entry.get("affect", "Amend"), "affect_api")
            break  # one endpoint succeeded — don't try the other
        except Exception as exc:
            log(f"  Affect API failed: {exc}")

    return results


# ===========================================================================
# Stealth browser fetch
# ===========================================================================

def _fetch_with_stealth(url: str) -> str:
    """
    Fetch a page using selenium-stealth + headless Chromium.

    WHY selenium-stealth instead of requests or Playwright:
    parlinfo.aph.gov.au is behind an Azure WAF JS Challenge. The challenge
    works by serving a 403 response containing a JavaScript payload that:
      1. Reads browser properties (navigator.webdriver, plugins, languages…)
      2. Computes a challenge token from those properties
      3. Sets a cookie with the token
      4. Redirects to the original URL

    Plain HTTP clients (requests, httpx) cannot execute JavaScript at all,
    so they never pass step 1.

    Playwright runs real Chromium but does NOT patch the automation flags
    that the WAF checks. In particular, navigator.webdriver remains `true`
    in a Playwright-driven browser, which the WAF detects as a bot and
    returns 403 after the JS runs.

    selenium-stealth explicitly patches:
      • navigator.webdriver → false
      • navigator.plugins → non-empty array
      • navigator.languages → ["en-AU", "en"]
      • window.chrome → present (mimics a real Chrome install)
      • WebGL vendor/renderer → realistic Intel strings

    These patches make the WAF's JS challenge compute a valid token, the
    redirect happens, and the real page is served.

    WHY poll for "Azure WAF" disappearance rather than using a fixed sleep:
    The WAF challenge resolution time varies (typically 0.5–3 s). Polling
    every 500 ms up to 10 s avoids unnecessary delay on fast resolutions
    while still handling slow ones.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium_stealth import stealth

    options = Options()
    options.add_argument("--headless=new")        # new headless mode (Chrome 112+)
    options.add_argument("--no-sandbox")           # required in Docker/GHA containers
    options.add_argument("--disable-dev-shm-usage")  # /dev/shm is small in GHA; use /tmp
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-AU")

    # /usr/bin/chromedriver is installed by the workflow's apt-get step.
    # We specify the path explicitly rather than relying on PATH lookup to
    # avoid version mismatch errors on runners where multiple Chrome versions
    # might be present.
    service = Service("/usr/bin/chromedriver")
    driver  = webdriver.Chrome(service=service, options=options)

    stealth(
        driver,
        languages=["en-AU", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,    # patches a canvas hairline fingerprint
    )

    log(f"    stealth: navigating to {url[:90]}")
    try:
        driver.get(url)
        # Poll until the WAF challenge resolves (Azure WAF disappears from source)
        for _ in range(20):
            time.sleep(0.5)
            if "Azure WAF" not in driver.page_source:
                break
        html = driver.page_source
        log(f"    stealth: got {len(html)} chars")
        return html
    finally:
        driver.quit()   # always release the browser process


def fetch_parlinfo(url: str) -> str:
    """
    Fetch any parlinfo.aph.gov.au page, bypassing the Azure WAF.

    WHY two-stage with requests fallback:
    The stealth browser adds ~3–5 s per page fetch (browser launch + WAF wait).
    Having a requests fallback allows local development and unit testing against
    non-WAF-protected mock servers without needing a Chrome install. On the
    live site from GitHub Actions, requests will always 403 and the stealth
    path is the only viable route.

    WHY try both ; and ? URL variants:
    ParlInfo uses semicolons as path parameter separators (a legacy CGI
    convention). Some HTTP intermediaries strip or rewrite semicolons. Trying
    the ? form as well costs one extra attempt and covers edge cases.
    """
    # Primary: stealth browser
    try:
        html = _fetch_with_stealth(url)
        if len(html) > 500 and "Azure WAF" not in html:
            return html
        preview = html[:200].replace("\n", " ")
        log(f"    stealth: still WAF page after polling — {preview!r}")
    except ImportError:
        log("    selenium-stealth not installed — falling back to requests")
    except Exception as exc:
        log(f"    stealth failed: {exc}")

    # Fallback: plain requests (will 403 on the live site)
    url_variants = [url, url.replace(";", "?", 1)] if ";" in url else [url]
    last_status = None
    for u in url_variants:
        log(f"    requests GET {u[:100]}")
        try:
            resp = requests.get(u, headers=HEADERS, timeout=30, allow_redirects=True)
            last_status = resp.status_code
            if resp.status_code == 200 and len(resp.text) > 200:
                return resp.text
            log(f"    HTTP {resp.status_code}")
        except Exception as exc:
            log(f"    Request error: {exc}")

    raise RuntimeError(
        f"Could not retrieve ParlInfo page (last status: {last_status}). URL: {url}"
    )


# ===========================================================================
# Generic marker-based text extractor
# ===========================================================================

def extract_between_markers(
    soup: BeautifulSoup,
    start_patterns: list[str],
    end_patterns: list[str],
) -> str:
    """
    Extract page content between two section markers.

    WHY a generic extractor rather than source-specific functions:
    The Bills Digest, bill home Summary, and Explanatory Memorandum pages all
    use the same structural idiom: a short heading labels the section start,
    followed by content paragraphs, followed by another short heading for the
    next section. A single parametrised function avoids three near-identical
    implementations and makes it easy to add new sources later.

    HOW marker detection works:
    Markers are identified by their full text matching a pattern (re.fullmatch,
    case-insensitive). The 80-character cap filters out content paragraphs
    that happen to mention "Summary" — only genuine section headings (which
    are brief labels) will match.

    HOW content collection works:
    After the start marker, we walk all following elements with find_all_next()
    and collect text from "leaf" elements — those with no block-level children.
    This avoids collecting the same text multiple times when container divs wrap
    paragraph elements: we get the <p> text once, not the <p> text plus the
    parent <div> text.

    WHY re.fullmatch not re.search:
    We want "Key Points" to match a heading that says exactly "Key Points", not
    a paragraph that happens to contain the phrase "Key Points" in the middle of
    a sentence. fullmatch anchors the pattern to the entire string.
    """
    # Find the start marker element
    start_el = None
    for tag in soup.find_all(True):
        tag_text = tag.get_text(strip=True)
        if len(tag_text) > 80:      # too long to be a section heading
            continue
        for pat in start_patterns:
            if re.fullmatch(pat, tag_text, re.IGNORECASE):
                start_el = tag
                break
        if start_el:
            break

    if not start_el:
        return ""

    # Collect leaf-element text until the end marker
    chunks: list[str]    = []
    seen_texts: set[str] = set()

    for tag in start_el.find_all_next():
        tag_text = tag.get_text(strip=True)

        # End marker check (same short-element heuristic as start marker)
        if len(tag_text) <= 80:
            for pat in end_patterns:
                if re.fullmatch(pat, tag_text, re.IGNORECASE):
                    return " ".join(chunks).strip()

        # Leaf element content collection
        # "Leaf" = no block-level descendants (p, li, td, dd)
        if tag.name in ("p", "li", "td", "span", "dd", "summary") and \
                not tag.find(["p", "li", "td", "dd"]):
            text = tag.get_text(separator=" ", strip=True)
            if text and text not in seen_texts:
                seen_texts.add(text)
                chunks.append(text)

    return " ".join(chunks).strip()


# ===========================================================================
# Source-specific scrapers
# ===========================================================================

def scrape_bills_digest(bill_id: str) -> str:
    """
    Priority 1: Bills Digest — Key Points section.

    WHY preferred first:
    The Bills Digest is written by the Parliamentary Library specifically for
    a non-specialist audience. The Key Points section is a concise, accurate
    summary of the bill's purpose and effect — exactly what Tripwire needs to
    explain a legislative change in plain English.

    URL pattern:
        BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs
    The bill_id (e.g. "r7042") is extracted from the bill home ParlInfo URL.
    Dataset%3Abillsdgs scopes the search to the Bills Digest dataset.

    Extract: between 'Key Points' and 'Contents' (case-insensitive, own line).
    """
    url = (
        f"{PARLINFO_DISPLAY}"
        f";query=BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs;rec=0"
    )
    log(f"  [1] Bills Digest -> {url}")
    try:
        html = fetch_parlinfo(url)
    except Exception as exc:
        log(f"    Bills Digest fetch failed: {exc}")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    text = extract_between_markers(
        soup,
        start_patterns=[r"key\s+points"],
        end_patterns=[r"contents?"],
    )
    log(f"    Bills Digest: {len(text.split())} words extracted")
    return text


def scrape_bill_summary(bill_home_html: str) -> str:
    """
    Priority 2 and 4: Bill home page Summary section.

    WHY two page variants exist on ParlInfo:
    Older bills use a legacy layout with <b class="bills"> as section markers.
    Newer bills use <summary> as either:
      A) A standalone container (the full summary is directly inside <summary>)
      B) The heading of a <details> accordion (the rest of the content is in
         sibling elements after <summary> inside <details>)

    The generic extract_between_markers handles the b.bills layout.
    The <summary> fallback handles both A and B:
      - For variant A: get_text() on <summary> returns everything
      - For variant B: we walk up to find_parent("details") and collect all
        child text, which includes both the <summary> heading text and the
        sibling paragraphs that contain the rest of the content

    Extract: between 'Summary' and 'Progress of bill' (case-insensitive, own line).
    """
    soup = BeautifulSoup(bill_home_html, "html.parser")

    summary_els = soup.find_all("summary")
    log(f"    <summary> elements on page: {len(summary_els)}")

    # Primary: marker-based extraction (handles b.bills legacy layout)
    text = extract_between_markers(
        soup,
        start_patterns=[r"summary"],
        end_patterns=[r"progress\s+of\s+bill"],
    )

    # Fallback: direct <summary> element (handles both ParlInfo <summary> variants)
    if not text:
        for el in summary_els:
            parent_details = el.find_parent("details")
            if parent_details:
                # Variant B: collect the full <details> content
                chunks = [
                    c.get_text(separator=" ", strip=True)
                    for c in parent_details.children
                    if hasattr(c, "get_text")
                ]
                text = " ".join(filter(None, chunks))
            else:
                # Variant A: standalone container
                text = el.get_text(separator=" ", strip=True)
            if len(text.split()) >= 5:
                break

    log(f"    Bill summary: {len(text.split())} words extracted")
    return text


def find_em_url(bill_home_html: str) -> str | None:
    """
    Scan the bill home page HTML for a link to the Explanatory Memorandum.

    WHY scan raw HTML rather than parsing with BeautifulSoup:
    The EM link is deeply nested in the page's DOM and its surrounding
    elements vary by bill. A regex against the raw HTML string is both simpler
    and more resilient to DOM structure variation than navigating the tree.

    WHY the UUID cannot be predicted:
    The EM URL contains a UUID component (e.g. 0ce6b86e-1206-484c-a5be...)
    that is assigned at upload time by the ParlInfo document management system.
    It is not derivable from the bill ID or any other known field. The bill
    home page is the only reliable place to discover it.
    """
    match = EM_LINK_RE.search(bill_home_html)
    if match:
        url = match.group(0).rstrip("\"'")
        log(f"    EM link found: {url[:90]}")
        return url
    log("    No EM link found on bill home page")
    return None


def scrape_em(em_url: str) -> str:
    """
    Priority 3: Explanatory Memorandum — General Outline or Outline section.

    WHY used when Bills Digest is absent and Summary is short:
    Not all bills have a Parliamentary Library digest (particularly minor
    technical amendment bills). The EM is always available and its General
    Outline / Outline section provides a structured, legally accurate
    description of the bill's purpose and mechanism. It is more technical
    than the Bills Digest but still readable plain English.

    WHY 'General Outline' and 'Outline' as alternate start markers:
    The heading varies by drafting convention. Older EMs use "Outline";
    newer ones use "General Outline". Both are tried as alternatives via
    re.fullmatch so the first one present on the page is used.

    WHY 'Financial Impact' and 'Financial Impact Statement' as end markers:
    The Financial Impact section immediately follows the Outline in all
    standard EM structures. It is a reliable stop point that prevents
    capturing unrelated later sections.

    Extract: 'General Outline'|'Outline' → 'Financial Impact'|'Financial Impact Statement'
    (case-insensitive, own line).
    """
    log(f"  [3] Explanatory Memorandum -> {em_url}")
    try:
        html = fetch_parlinfo(em_url)
    except Exception as exc:
        log(f"    EM fetch failed: {exc}")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    text = extract_between_markers(
        soup,
        start_patterns=[r"general\s+outline", r"outline"],
        end_patterns=[r"financial\s+impact(?:\s+statement)?"],
    )
    log(f"    EM: {len(text.split())} words extracted")
    return text


# ===========================================================================
# ParlInfo URL discovery (on legislation.gov.au)
# ===========================================================================

def find_parlinfo_url(amending_act_id: str) -> str | None:
    """
    Scrape the amending Act's legislation.gov.au page to find its ParlInfo
    "Originating Bill and Explanatory Memorandum" link.

    WHY scrape legislation.gov.au rather than constructing the ParlInfo URL:
    The ParlInfo bill home URL contains a bill register ID (e.g. "r7042") that
    is not derivable from the FRL Act titleId (e.g. "C2023A00074"). The two
    systems use independent identifiers. The only reliable mapping is the link
    that the legislation.gov.au website exposes on the Act's versions page
    under "Originating Bill and Explanatory Memorandum".

    WHY three detection strategies:
    The link appears as a standard anchor in most cases (Strategy A), but the
    page is a React SPA and the anchor's surrounding markup varies. Strategy B
    catches cases where the text content is "Originating Bill..." but the href
    structure differs. Strategy C is a raw regex fallback that catches links
    in data attributes or non-standard anchor positions.

    WHY try /latest/versions before /asmade/versions:
    For an as-made Act, both paths resolve to the same page. /latest/versions
    is the canonical URL shown in browser navigation and the more likely to be
    stable long-term.
    """
    for path in [
        f"/{amending_act_id}/latest/versions",
        f"/{amending_act_id}/asmade/versions",
    ]:
        url = f"{LEGISLATION_BASE}{path}"
        log(f"  Scraping -> {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        except Exception as exc:
            log(f"    Request failed: {exc}")
            continue
        if resp.status_code != 200:
            log(f"    HTTP {resp.status_code}")
            continue
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Strategy A: anchor href contains both parlinfo domain and billhome path
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "parlinfo.aph.gov.au" in href and "billhome" in href.lower():
                log(f"    Found: {href}")
                return href

        # Strategy B: anchor text mentions "Originating Bill"
        for a in soup.find_all("a", href=True):
            if (
                "originating bill" in a.get_text(strip=True).lower()
                and "parlinfo" in a["href"].lower()
            ):
                log(f"    Found via link text: {a['href']}")
                return a["href"]

        # Strategy C: raw regex scan of the full HTML source
        matches = re.findall(
            r'https?://parlinfo\.aph\.gov\.au/parlInfo/search/display/[^\s\'"<>]+billhome[^\s\'"<>]+',
            html, re.IGNORECASE,
        )
        if matches:
            log(f"    Found via regex: {matches[0]}")
            return matches[0]

        log("    No ParlInfo link found on this page")

    return None


def extract_bill_id(parlinfo_url: str) -> str | None:
    """
    Extract the bill register ID (e.g. 'r7042') from a ParlInfo bill home URL.

    The bill ID appears after 'billhome/' in the URL, sometimes URL-encoded as
    'billhome%2F'. It is needed to construct the Bills Digest search URL.
    """
    match = re.search(r"billhome[/%2F]+([a-zA-Z][0-9]+)", parlinfo_url, re.IGNORECASE)
    return match.group(1) if match else None


# ===========================================================================
# Per-Act orchestration
# ===========================================================================

def process_amending_act(act: dict) -> dict:
    """
    Run the full content-retrieval pipeline for one amending Act.

    The four-priority waterfall is designed to maximise the chance of getting
    a useful, readable plain-English explanation:

    1. Bills Digest (Key Points)
       Best for readability — written for non-specialists. Attempted first
       regardless of length, as even a short digest is more accessible than
       an EM general outline.

    2. Summary ≥ 100 words
       The bill author's own précis. Good when present and substantive.
       The 100-word threshold filters out placeholder text like "Introduced
       with the [other bill], this bill..." that some bills use when the
       full details are in a companion bill's summary.

    3. Explanatory Memorandum (General Outline)
       Always available for government bills. More technical but reliable.
       Attempted when the summary is too short — this usually means the bill
       is a companion to another bill (e.g. consequential amendments) and the
       EM has the actual operational detail.

    4. Summary < 100 words
       Accepted as a last resort. Short summaries do exist for genuinely minor
       bills (e.g. statute law revision Acts that make only technical fixes).

    WHY fetch the bill home page once and reuse:
    The bill home page HTML serves double duty: it is scraped for the Summary
    (Priority 2/4) AND scanned for the EM URL (Priority 3). Fetching it once
    saves a second stealth browser launch (~4 s) and reduces the chance of the
    page being served differently on a second request.
    """
    tid = act["titleId"]
    result = {
        "titleId":          tid,
        "name":             act.get("name", ""),
        "affect":           act.get("affect", ""),
        "discovery_source": act.get("source", ""),
        "parlinfo_url":     None,
        "bill_id":          None,
        "bill_title":       "",
        "em_url":           None,
        "summary":          "",
        "summary_source":   "",
        "status":           "not_found",
    }

    # Locate the ParlInfo bill home URL via legislation.gov.au
    parlinfo_url = find_parlinfo_url(tid)
    if not parlinfo_url:
        log(f"  Could not find a ParlInfo URL for {tid}")
        result["status"] = "no_parlinfo_url"
        return result

    result["parlinfo_url"] = parlinfo_url
    result["bill_id"]      = extract_bill_id(parlinfo_url)

    # Fetch the bill home page once; reuse for summary + EM URL discovery
    log(f"  Fetching bill home page ...")
    try:
        bill_home_html = fetch_parlinfo(parlinfo_url)
    except Exception as exc:
        log(f"  Bill home fetch failed: {exc}")
        result["status"] = "scrape_error"
        return result

    # Extract bill title for the report
    soup_home = BeautifulSoup(bill_home_html, "html.parser")
    for selector in ["h1", "h2.bills", ".billTitle"]:
        el = soup_home.select_one(selector)
        if el:
            t = el.get_text(strip=True)
            if len(t) > 10 and "parlinfo" not in t.lower():
                result["bill_title"] = t
                break

    # ----------------------------------------------------------------
    # Priority 1: Bills Digest — Key Points
    # ----------------------------------------------------------------
    if result["bill_id"]:
        digest = scrape_bills_digest(result["bill_id"])
        if digest and len(digest.split()) >= 10:
            result.update(summary=digest, summary_source="bills_digest", status="success")
            return result
    log("  [1] Bills Digest: no usable content — trying next source")

    # ----------------------------------------------------------------
    # Priority 2: Summary ≥ 100 words
    # ----------------------------------------------------------------
    summary = scrape_bill_summary(bill_home_html)
    if len(summary.split()) >= MIN_SUMMARY_WORDS:
        result.update(summary=summary, summary_source="bill_summary", status="success")
        return result
    log(f"  [2] Summary: {len(summary.split())} words (< {MIN_SUMMARY_WORDS}) — trying next source")

    # ----------------------------------------------------------------
    # Priority 3: Explanatory Memorandum — General Outline
    # ----------------------------------------------------------------
    em_url = find_em_url(bill_home_html)
    result["em_url"] = em_url
    if em_url:
        em_text = scrape_em(em_url)
        if em_text and len(em_text.split()) >= 10:
            result.update(summary=em_text, summary_source="explanatory_memorandum", status="success")
            return result
    log("  [3] EM: no usable content — using summary fallback")

    # ----------------------------------------------------------------
    # Priority 4: Summary < 100 words (best-effort fallback)
    # ----------------------------------------------------------------
    if summary:
        result.update(summary=summary, summary_source="bill_summary_short", status="success_short")
        return result

    log("  [4] No content found from any source")
    result["status"] = "no_summary_found"
    return result


# ===========================================================================
# Report generation
# ===========================================================================

def generate_report(
    principal_title_id: str,
    compilation_label: str,
    results: list[dict],
) -> str:
    """
    Build the markdown report that is both committed to the repo and rendered
    in the GitHub Actions Step Summary.

    WHY markdown: GitHub renders markdown natively in the Step Summary UI and
    in the repository file browser, making the report immediately readable
    without any tooling. The same file is also machine-parseable if Tripwire
    needs to ingest it downstream.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# EM Summary Report", "",
        f"**Principal Act:** [{principal_title_id}]({LEGISLATION_BASE}/{principal_title_id}/latest/versions)  ",
        f"**Compilation:** {compilation_label}  ",
        f"**Generated:** {now}  ",
        "", "---", "",
    ]

    if not results:
        lines.append("No amending Acts were identified for this compilation.")
        return "\n".join(lines)

    success_count = sum(1 for r in results if r["status"].startswith("success"))
    lines.append(
        f"Found **{len(results)}** amending Act(s). "
        f"Summaries retrieved for **{success_count}**."
    )
    lines.append("")

    source_labels = {
        "bills_digest":           "Bills Digest — Key Points",
        "bill_summary":           "Bill Home — Summary (≥100 words)",
        "explanatory_memorandum": "Explanatory Memorandum — General Outline",
        "bill_summary_short":     "Bill Home — Summary (<100 words, fallback)",
    }

    for i, res in enumerate(results, 1):
        tid     = res["titleId"]
        name    = res.get("name") or tid
        summary = res.get("summary", "").strip()
        status  = res.get("status", "")
        source  = res.get("summary_source", "")

        lines += [f"## {i}. {name}", ""]
        lines.append(f"- **Amending Act:** [{tid}]({LEGISLATION_BASE}/{tid}/latest/versions)")
        lines.append(f"- **Discovered via:** {res.get('discovery_source', '-')}")
        if res.get("bill_title") and res["bill_title"] != name:
            lines.append(f"- **Bill:** {res['bill_title']}")
        if res.get("parlinfo_url"):
            lines.append(f"- **Bill home:** [{res['parlinfo_url']}]({res['parlinfo_url']})")
        if res.get("em_url"):
            lines.append(f"- **EM:** [{res['em_url']}]({res['em_url']})")
        if res.get("bill_id"):
            lines.append(f"- **Bill ID:** {res['bill_id']}")
        lines.append(f"- **Summary source:** {source_labels.get(source, source or '-')}")
        lines.append("")

        if summary:
            lines += ["### Plain-English Summary", "", summary]
        elif status == "no_parlinfo_url":
            lines.append("> ⚠️ No ParlInfo link found on the legislation.gov.au page.")
        elif status == "scrape_error":
            lines.append("> ⚠️ Could not fetch the bill home page.")
        else:
            lines.append("> ⚠️ No summary content extracted from any source.")

        lines += ["", "---", ""]

    return "\n".join(lines)


# ===========================================================================
# Output helpers
# ===========================================================================

def write_step_summary(report_md: str) -> None:
    """
    Append the report to $GITHUB_STEP_SUMMARY.

    WHY append rather than write: other steps in the workflow may also write
    to the Step Summary. Appending ensures we don't overwrite their output.
    The path is provided by GitHub Actions as an environment variable; when
    running locally it is not set and this function is a no-op.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(report_md)
    log("GitHub Step Summary written.")


def write_output_file(report_md: str, title_id: str, compilation_label: str) -> str:
    """
    Write the report to em_summaries/<titleId>/EM_summary_<titleId>_<comp>.md

    WHY this directory structure:
    Organising by titleId keeps all compilations of the same Act together.
    The workflow's git commit step then pushes the whole em_summaries/ tree,
    building up a queryable history of all EM summaries over time.
    """
    from pathlib import Path
    out_dir  = Path("em_summaries") / title_id
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"EM_summary_{title_id}_{compilation_label}.md"
    out_path = out_dir / filename
    out_path.write_text(report_md, encoding="utf-8")
    log(f"Report written -> {out_path}")
    return str(out_path)


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: python fetch_em_summary.py <legislation_url> <compilation_number>\n"
            "Example: python fetch_em_summary.py "
            "https://www.legislation.gov.au/C2004A04014/latest/versions C50"
        )
        sys.exit(1)

    url_input  = sys.argv[1].strip()
    comp_input = sys.argv[2].strip().upper()

    log_section("FRL EM Summary Fetcher")
    log(f"Input URL   : {url_input}")
    log(f"Compilation : {comp_input}")

    try:
        title_id = extract_title_id(url_input)
    except ValueError as exc:
        log(f"ERROR: {exc}"); sys.exit(1)
    log(f"Title ID    : {title_id}")

    log_section("Fetching compilation from FRL API")
    try:
        version_data = get_compilation(title_id, comp_input)
    except RuntimeError as exc:
        log(f"ERROR: {exc}"); sys.exit(1)

    register_id = version_data.get("registerId", "unknown")
    start       = version_data.get("start", "")
    log(f"Register ID : {register_id}")
    log(f"Start date  : {start[:10] if start else 'unknown'}")
    version_data.setdefault("titleId", title_id)

    log_section("Discovering amending Acts")
    amending_acts = discover_amending_acts(version_data)

    if not amending_acts:
        log("No amending Acts found.")
        report = (
            f"# EM Summary Report\n\n"
            f"**Principal Act:** {title_id}  \n"
            f"**Compilation:** {comp_input}  \n\n"
            f"No amending Acts were found for this compilation.\n"
        )
        write_step_summary(report)
        write_output_file(report, title_id, comp_input)
        sys.exit(0)

    log(f"Found {len(amending_acts)} amending Act(s):")
    for act in amending_acts:
        log(f"  * {act['titleId']}  (via {act['source']})  {act.get('name', '')}")

    log_section("Retrieving EM summaries from ParlInfo")
    results = []
    for act in amending_acts:
        log(f"\nProcessing {act['titleId']} ...")
        results.append(process_amending_act(act))

    log_section("Generating report")
    report_md = generate_report(title_id, comp_input, results)

    print("\n" + "=" * 60)
    print(report_md)
    print("=" * 60)

    out_path      = write_output_file(report_md, title_id, comp_input)
    write_step_summary(report_md)
    success_count = sum(1 for r in results if r["status"].startswith("success"))

    log_section("Complete")
    log(f"{success_count}/{len(results)} summaries retrieved.")
    log(f"Report saved -> {out_path}")

    if success_count == 0:
        log("WARNING: No summaries retrieved. Exiting with code 1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
