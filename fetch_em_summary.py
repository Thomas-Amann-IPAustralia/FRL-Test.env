"""
fetch_em_summary.py
--------------------
Given a legislation.gov.au URL and a compilation number, finds every Act that
amended that compilation, retrieves its Explanatory Memorandum summary from
ParlInfo, and writes a plain-English report.

Usage:
    python fetch_em_summary.py <legislation_url> <compilation_number>

Amending Act discovery (three layers, all run, results deduplicated):

    Layer 1 — registerId check
        If the compilation's registerId is C####A##### (Act series), that IS
        the amending Act. Common for single-amendment compilations.

    Layer 2 — reasons array (two sub-passes)
        2a. Walk reasons[].amendedByTitle / affectedByTitle for C####A##### titleIds.
        2b. Scan reasons[].markdown for embedded C####A##### patterns.
            The markdown field contains the Act ID even when the titleId fields
            hold a compilation register ID (C####C#####) instead.

    Layer 3 — _AffectsSearch API endpoint
        Query GET /v1/_AffectsSearch with the principal Act's titleId.
        Fallback if Layers 1 and 2 yield nothing.

ParlInfo summary retrieval:
    Scrape the amending Act's legislation.gov.au /latest/versions page for
    the "Originating Bill and Explanatory Memorandum" ParlInfo link, then:
    1. Extract the <summary> element or b.bills-delimited content.
    2. If < 100 words, fall back to Bills Digest Key Points.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FRL_API = "https://api.prod.legislation.gov.au/v1"
LEGISLATION_BASE = "https://www.legislation.gov.au"
PARLINFO_DISPLAY = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"

TITLE_ID_RE = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")

# Strictly Acts: C + 4 digits + "A" + digits  (C2023A00074, C2016A00004)
# Excludes compilation register IDs (C####C#####) and instruments
ACT_SERIES_RE = re.compile(r"^C\d{4}A\d+$")

# Same pattern but for scanning free text / markdown
ACT_IN_TEXT_RE = re.compile(r"\bC\d{4}A\d+\b")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}

MIN_SUMMARY_WORDS = 100


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_section(title: str) -> None:
    log("")
    log("─" * 60)
    log(f"  {title}")
    log("─" * 60)


# ---------------------------------------------------------------------------
# FRL API helpers
# ---------------------------------------------------------------------------
def extract_title_id(url: str) -> str:
    match = TITLE_ID_RE.search(url)
    if not match:
        raise ValueError(
            f"Could not extract a Title ID from: {url}\n"
            "Expected a URL like https://www.legislation.gov.au/C2015A00040/latest/versions"
        )
    return match.group(1)


def get_compilation(title_id: str, compilation_number: str) -> dict:
    """Fetch a specific compilation via the FRL API Versions/Find() endpoint."""
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


# ---------------------------------------------------------------------------
# Amending Act discovery
# ---------------------------------------------------------------------------
def discover_amending_acts(version_data: dict) -> list[dict]:
    """
    Identify all Acts that amended this compilation using three layers.
    All layers run; results are deduplicated by titleId.
    """
    seen: set[str] = set()
    results: list[dict] = []

    def add(tid: str, name: str, affect: str, source: str) -> None:
        if tid and tid not in seen and ACT_SERIES_RE.match(tid):
            seen.add(tid)
            results.append({"titleId": tid, "name": name, "affect": affect, "source": source})
            log(f"    Added: {tid} (via {source})")

    title_id = version_data.get("titleId", "")
    register_id = version_data.get("registerId", "")
    start = version_data.get("start", "")

    # --- Layer 1: registerId ---
    if ACT_SERIES_RE.match(register_id):
        log(f"  Layer 1: registerId {register_id} is an amending Act")
        add(register_id, "", "Amend", "registerId")
    else:
        log(f"  Layer 1: registerId {register_id} is not an Act series ID")

    # --- Layer 2: reasons array ---
    reasons = version_data.get("reasons", [])
    log(f"  Layer 2: {len(reasons)} reason(s) in API response")
    for i, reason in enumerate(reasons):
        affect = reason.get("affect", "Amend")
        log(f"    reason[{i}]: affect={affect!r} keys={list(reason.keys())}")

        # 2a — check titleId fields directly
        for key in ("amendedByTitle", "affectedByTitle"):
            obj = reason.get(key) or {}
            if isinstance(obj, dict):
                tid = obj.get("titleId", "")
                name = obj.get("name", "")
                log(f"      {key}: titleId={tid!r} matches={bool(ACT_SERIES_RE.match(tid)) if tid else False}")
                if tid and ACT_SERIES_RE.match(tid):
                    add(tid, name, affect, f"reasons[{i}].{key}")
                    break
            else:
                log(f"      {key}: not a dict -> {type(obj).__name__}: {str(obj)[:80]!r}")

        # 2b — scan markdown field for embedded Act IDs
        markdown = reason.get("markdown", "") or ""
        for tid in ACT_IN_TEXT_RE.findall(markdown):
            # Extract name from markdown if formatted as [Name](TitleId)
            name_match = re.search(
                r"\[([^\]]+)\]\(" + re.escape(tid) + r"\)", markdown
            )
            name = name_match.group(1) if name_match else ""
            add(tid, name, affect, f"reasons[{i}].markdown")

    # --- Layer 3: _AffectsSearch API ---
    # Only run if Layers 1 and 2 found nothing, to avoid unnecessary API calls
    if not results:
        log(f"  Layer 3: Layers 1+2 empty — querying _AffectsSearch for {title_id}")
        affects_acts = _query_affects_search(title_id, start)
        for act in affects_acts:
            add(act["titleId"], act.get("name", ""), act.get("affect", "Amend"), "affects_search")
    else:
        log(f"  Layer 3: skipped (already found {len(results)} Act(s) in Layers 1+2)")

    return results


def _query_affects_search(title_id: str, compilation_start: str) -> list[dict]:
    """
    Query the FRL _AffectsSearch endpoint for Acts affecting the given title.
    Falls back to the Affect entity set if _AffectsSearch is unavailable.
    """
    comp_date = compilation_start[:10] if compilation_start else ""
    results: list[dict] = []

    endpoints = [
        f"{FRL_API}/_AffectsSearch",
        f"{FRL_API}/Affect",
    ]
    filter_fields = [
        f"affectedTitleId eq '{title_id}'",
        f"affectedTitle/titleId eq '{title_id}'",
    ]

    for endpoint in endpoints:
        for filter_expr in filter_fields:
            url = f"{endpoint}?$filter={quote(filter_expr)}&$top=50"
            log(f"    Trying: {url}")
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 404:
                    log(f"    404 — skipping")
                    break  # try next endpoint
                resp.raise_for_status()
                data = resp.json()
                entries = data.get("value", data if isinstance(data, list) else [])
                log(f"    Got {len(entries)} entries")

                seen_in_response: set[str] = set()
                for entry in entries:
                    # Try multiple field names for the affecting Act ID
                    affecting_id = (
                        entry.get("affectingTitleId")
                        or (entry.get("affectingTitle") or {}).get("titleId", "")
                    )
                    if not affecting_id or not ACT_SERIES_RE.match(affecting_id):
                        continue

                    # Optionally filter by date
                    if comp_date:
                        entry_date = str(
                            entry.get("dateChanged", "")
                            or entry.get("start", "")
                            or ""
                        )[:10]
                        if entry_date and entry_date != comp_date:
                            continue

                    if affecting_id not in seen_in_response:
                        seen_in_response.add(affecting_id)
                        title_obj = entry.get("affectingTitle") or {}
                        name = title_obj.get("name", "") if isinstance(title_obj, dict) else ""
                        results.append({
                            "titleId": affecting_id,
                            "name": name,
                            "affect": entry.get("affect", "Amend"),
                        })

                if results:
                    return results
                # No date-matched results — retry without date constraint
                if comp_date and not results:
                    log("    No date-matched results — retrying without date filter")
                    for entry in entries:
                        affecting_id = (
                            entry.get("affectingTitleId")
                            or (entry.get("affectingTitle") or {}).get("titleId", "")
                        )
                        if affecting_id and ACT_SERIES_RE.match(affecting_id):
                            title_obj = entry.get("affectingTitle") or {}
                            name = title_obj.get("name", "") if isinstance(title_obj, dict) else ""
                            results.append({
                                "titleId": affecting_id,
                                "name": name,
                                "affect": entry.get("affect", "Amend"),
                            })
                    if results:
                        return results

            except Exception as exc:
                log(f"    Error: {exc}")
                continue

    return results


# ---------------------------------------------------------------------------
# ParlInfo URL discovery
# ---------------------------------------------------------------------------
def find_parlinfo_url(amending_act_id: str) -> str | None:
    """
    Scrape the amending Act's legislation.gov.au page to find the
    "Originating Bill and Explanatory Memorandum" ParlInfo link.
    """
    candidate_paths = [
        f"/{amending_act_id}/latest/versions",
        f"/{amending_act_id}/asmade/versions",
        f"/{amending_act_id}/latest/text",
    ]

    for path in candidate_paths:
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

        # Strategy A: anchor href contains parlinfo + billhome
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "parlinfo.aph.gov.au" in href and "billhome" in href.lower():
                log(f"    Found: {href}")
                return href

        # Strategy B: anchor text contains "Originating Bill"
        for a in soup.find_all("a", href=True):
            if (
                "originating bill" in a.get_text(strip=True).lower()
                and "parlinfo" in a["href"].lower()
            ):
                log(f"    Found via link text: {a['href']}")
                return a["href"]

        # Strategy C: raw regex scan
        matches = re.findall(
            r'https?://parlinfo\.aph\.gov\.au/parlInfo/search/display/[^\s\'"<>]+billhome[^\s\'"<>]+',
            html,
            re.IGNORECASE,
        )
        if matches:
            log(f"    Found via regex: {matches[0]}")
            return matches[0]

        log("    No ParlInfo link found")

    return None


# ---------------------------------------------------------------------------
# ParlInfo bill home scraping
# ---------------------------------------------------------------------------
def _fetch_with_stealth(url: str) -> str:
    """
    Fetch a page using selenium-stealth + headless Chromium.

    selenium-stealth patches navigator.webdriver and other automation
    fingerprints that Azure WAF JS Challenge detects. Playwright does NOT
    patch these properties, which is why it still receives 403.
    """
    import time
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium_stealth import stealth

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-AU")

    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)

    stealth(
        driver,
        languages=["en-AU", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    log(f"    stealth: navigating to {url[:80]}")
    try:
        driver.get(url)
        # Poll until the WAF JS challenge resolves and the real page loads.
        for _ in range(20):  # up to 10 seconds
            time.sleep(0.5)
            if "Azure WAF" not in driver.page_source:
                break
        html = driver.page_source
        log(f"    stealth: got {len(html)} chars")
        return html
    finally:
        driver.quit()


def _fetch_parlinfo_html(parlinfo_url: str) -> str:
    """
    Fetch HTML from a ParlInfo URL.

    parlinfo.aph.gov.au is protected by an Azure WAF JS Challenge that
    blocks all plain HTTP clients (requests, vanilla playwright) because
    they expose navigator.webdriver=true. selenium-stealth patches this
    and other automation fingerprints so the challenge passes normally.
    """
    # Primary: selenium-stealth
    try:
        html = _fetch_with_stealth(parlinfo_url)
        if len(html) > 500 and "Azure WAF" not in html:
            return html
        preview = html[:300].replace("\n", " ")
        log(f"    stealth: still got WAF page — preview: {preview!r}")
    except ImportError:
        log("    selenium-stealth not installed — falling back to requests")
    except Exception as exc:
        log(f"    stealth failed: {exc} — falling back to requests")

    # Fallback: requests (useful for local testing against non-WAF sites)
    url_variants = [parlinfo_url]
    if ";" in parlinfo_url:
        url_variants.append(parlinfo_url.replace(";", "?", 1))

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en-GB;q=0.9,en;q=0.8",
        "Referer": "https://www.legislation.gov.au/",
    }

    last_status = None
    for url in url_variants:
        log(f"    requests GET {url[:100]}")
        try:
            resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            last_status = resp.status_code
            if resp.status_code == 200 and len(resp.text) > 200:
                log(f"    HTTP 200 ({len(resp.text)} chars)")
                return resp.text
            preview = resp.text[:200].replace("\n", " ").strip()
            log(f"    HTTP {resp.status_code}: {preview!r}")
        except Exception as exc:
            log(f"    Request error: {exc}")

    raise RuntimeError(
        f"Could not retrieve ParlInfo page (last status: {last_status}). "
        f"URL: {parlinfo_url}"
    )


def _extract_summary_from_html(html: str) -> tuple[str, str]:
    """
    Extract (bill_title, summary_text) from a ParlInfo bill home HTML page.

    ParlInfo uses <summary> as a plain container element (NOT an HTML5
    details/summary accordion). The XPATH is:
      /html/body/div[2]/div[2]/div/div[2]/div/div[1]/summary

    The full bill summary text sits directly inside this <summary> element.
    soup.find("summary").get_text() retrieves it correctly.

    Three strategies are tried in order:
      1. Direct <summary> element — the primary ParlInfo structure.
      2. b.bills marker layout — legacy/alternate ParlInfo layout where
         content sits between <b class="bills">Summary</b> and
         <b class="bills">Progress of bill</b>.
      3. Broad text scan — last resort for unexpected page structures.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Log a diagnostic snippet so structure issues are visible in the log
    body_text = soup.get_text(separator=" ", strip=True)
    log(f"    Page text length: {len(body_text)} chars, word count: {len(body_text.split())}")
    summary_els = soup.find_all("summary")
    log(f"    <summary> elements found: {len(summary_els)}")
    for i, el in enumerate(summary_els[:3]):
        preview = el.get_text(separator=" ", strip=True)[:120]
        log(f"      summary[{i}]: {preview!r}")

    # Bill title
    bill_title = ""
    for selector in ["h1", "h2.bills", ".billTitle", "title"]:
        el = soup.select_one(selector)
        if el:
            candidate = el.get_text(strip=True)
            if len(candidate) > 10 and "parlinfo" not in candidate.lower():
                bill_title = candidate
                break

    # -----------------------------------------------------------------------
    # Strategy 1: <summary> element — ParlInfo's primary structure.
    #
    # Two variants exist across different bill pages:
    #   A) <summary> as a standalone container (e.g. r7042):
    #      The full summary text is directly inside <summary>.
    #      → get_text() on <summary> returns everything.
    #
    #   B) <summary> as the heading of a <details> accordion (e.g. r7127):
    #      <summary> contains only the opening clause; the rest of the text
    #      is in sibling elements inside <details> after <summary>.
    #      → get_text() on <summary> returns a truncated fragment.
    #      → Fix: walk up to <details> and collect all child text.
    #
    # XPATH: /html/body/div[2]/div[2]/div/div[2]/div/div[1]/summary
    # -----------------------------------------------------------------------
    summary_el = soup.find("summary")
    if summary_el:
        summary_text = summary_el.get_text(separator=" ", strip=True)

        # Check if this <summary> is inside a <details> (variant B)
        parent_details = summary_el.find_parent("details")
        if parent_details:
            # Collect ALL text inside <details>: summary heading + sibling content
            chunks = []
            for child in parent_details.children:
                if hasattr(child, "get_text"):
                    t = child.get_text(separator=" ", strip=True)
                    if t:
                        chunks.append(t)
            full_text = " ".join(chunks)
            log(f"    Strategy 1B (<details>+<summary>): {len(full_text.split())} words")
            if len(full_text.split()) >= 10:
                return bill_title, full_text

        # Variant A: standalone <summary> container
        if len(summary_text.split()) >= 10:
            log(f"    Strategy 1A (<summary> standalone): {len(summary_text.split())} words")
            return bill_title, summary_text

        log(f"    Strategy 1: <summary> found but only {len(summary_text.split())} words — continuing")

    # -----------------------------------------------------------------------
    # Strategy 2: b.bills marker layout
    # Content between <b class="bills">Summary</b> and
    # <b class="bills">Progress of bill</b>
    # -----------------------------------------------------------------------
    summary_b = None
    for b_tag in soup.find_all("b", class_="bills"):
        if re.search(r"\bSummary\b", b_tag.get_text(), re.IGNORECASE):
            summary_b = b_tag
            break

    if summary_b:
        chunks = []
        for sibling in summary_b.parent.next_siblings:
            if hasattr(sibling, "find_all"):
                stop = any(
                    re.search(r"Progress of bill", b.get_text(), re.IGNORECASE)
                    for b in sibling.find_all("b", class_="bills")
                )
                if stop:
                    break
                chunk = sibling.get_text(separator=" ", strip=True)
                if chunk:
                    chunks.append(chunk)
            elif hasattr(sibling, "strip"):
                text = str(sibling).strip()
                if text and not text.startswith("<"):
                    chunks.append(text)
        if chunks:
            text = " ".join(chunks)
            log(f"    Strategy 2 (b.bills markers): {len(text.split())} words")
            return bill_title, text

    # -----------------------------------------------------------------------
    # Strategy 3: any element with substantial text near "Summary" heading
    # Last resort for unexpected page structures.
    # -----------------------------------------------------------------------
    for tag in soup.find_all(["div", "section", "article", "p"]):
        attrs_str = " ".join(str(v) for v in tag.attrs.values()).lower()
        if "summary" in attrs_str:
            text = tag.get_text(separator=" ", strip=True)
            if len(text.split()) >= 20:
                log(f"    Strategy 3 (summary attr): {len(text.split())} words")
                return bill_title, text

    log("    WARNING: No extraction strategy succeeded")
    # Dump the first 1000 chars of page text to help diagnose
    log(f"    Page preview: {body_text[:500]!r}")
    return bill_title, ""


def scrape_bill_summary(parlinfo_url: str) -> tuple[str, str]:
    """
    Fetch and parse a ParlInfo bill home page.
    Returns (bill_title, summary_text).
    Raises RuntimeError if the page cannot be fetched.
    """
    log(f"  Scraping ParlInfo -> {parlinfo_url}")
    html = _fetch_parlinfo_html(parlinfo_url)
    return _extract_summary_from_html(html)


# ---------------------------------------------------------------------------
# Bills Digest fallback
# ---------------------------------------------------------------------------
def extract_bill_id(parlinfo_url: str) -> str | None:
    match = re.search(r"billhome[/%2F]+([a-zA-Z][0-9]+)", parlinfo_url, re.IGNORECASE)
    return match.group(1) if match else None


def scrape_bills_digest(bill_id: str) -> str:
    """Fetch Bills Digest and extract Key Points section."""
    search_url = (
        f"{PARLINFO_DISPLAY}"
        f";query=BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs;rec=0"
    )
    log(f"  Bills Digest -> {search_url}")
    try:
        # Use stealth fetch — Bills Digest is on the same WAF-protected domain
        html = _fetch_parlinfo_html(search_url)
    except Exception as exc:
        log(f"    Bills Digest failed: {exc}")
        return ""

    soup = BeautifulSoup(html, "html.parser")

    key_points_marker = None
    for tag in soup.find_all(["p", "h2", "h3", "strong", "b"]):
        if re.search(r"key\s+points", tag.get_text(), re.IGNORECASE):
            key_points_marker = tag
            break

    if not key_points_marker:
        log("    'Key points' marker not found")
        return ""

    chunks = []
    for sibling in key_points_marker.next_siblings:
        if hasattr(sibling, "get_text"):
            text = sibling.get_text(strip=True)
            if re.search(r"^\s*Contents?\s*$", text, re.IGNORECASE):
                break
            if text:
                chunks.append(text)
        elif str(sibling).strip():
            chunks.append(str(sibling).strip())

    if chunks:
        text = " ".join(chunks)
        log(f"    Bills Digest key points: {len(text.split())} words")
        return text

    return ""


# ---------------------------------------------------------------------------
# Per-Act orchestration
# ---------------------------------------------------------------------------
def process_amending_act(act: dict) -> dict:
    tid = act["titleId"]
    result = {
        "titleId": tid,
        "name": act.get("name", ""),
        "affect": act.get("affect", ""),
        "discovery_source": act.get("source", ""),
        "parlinfo_url": None,
        "bill_id": None,
        "bill_title": "",
        "summary": "",
        "summary_source": "",
        "status": "not_found",
    }

    parlinfo_url = find_parlinfo_url(tid)
    if not parlinfo_url:
        log(f"  Could not find a ParlInfo URL for {tid}")
        result["status"] = "no_parlinfo_url"
        return result

    result["parlinfo_url"] = parlinfo_url
    result["bill_id"] = extract_bill_id(parlinfo_url)

    try:
        bill_title, summary = scrape_bill_summary(parlinfo_url)
    except Exception as exc:
        import traceback
        log(f"  ParlInfo scrape failed: {exc}")
        log(f"  Traceback: {traceback.format_exc()}")
        result["status"] = "scrape_error"
        return result

    result["bill_title"] = bill_title
    word_count = len(summary.split())
    log(f"  Summary word count: {word_count}")

    if word_count >= MIN_SUMMARY_WORDS:
        result["summary"] = summary
        result["summary_source"] = "parlinfo_bill_home"
        result["status"] = "success"
        return result

    log(f"  < {MIN_SUMMARY_WORDS} words — trying Bills Digest ...")
    digest = scrape_bills_digest(result["bill_id"]) if result["bill_id"] else ""

    if digest and len(digest.split()) >= 30:
        result["summary"] = digest
        result["summary_source"] = "bills_digest"
        result["status"] = "success"
        return result

    if summary:
        result["summary"] = summary
        result["summary_source"] = "parlinfo_bill_home_short"
        result["status"] = "success_short"
        return result

    result["status"] = "no_summary_found"
    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_report(
    principal_title_id: str,
    compilation_label: str,
    results: list[dict],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# EM Summary Report",
        "",
        f"**Principal Act:** [{principal_title_id}]({LEGISLATION_BASE}/{principal_title_id}/latest/versions)  ",
        f"**Compilation:** {compilation_label}  ",
        f"**Generated:** {now}  ",
        "",
        "---",
        "",
    ]

    if not results:
        lines.append("No amending Acts were identified for this compilation.")
        return "\n".join(lines)

    success_count = sum(1 for r in results if r["status"].startswith("success"))
    lines.append(
        f"Found **{len(results)}** amending Act(s). "
        f"EM summaries retrieved for **{success_count}**."
    )
    lines.append("")

    for i, res in enumerate(results, 1):
        tid = res["titleId"]
        name = res.get("name") or tid
        bill_title = res.get("bill_title") or name
        parlinfo_url = res.get("parlinfo_url") or ""
        summary = res.get("summary", "").strip()
        source = res.get("summary_source", "")
        status = res.get("status", "")

        lines.append(f"## {i}. {name}")
        lines.append("")
        lines.append(f"- **Amending Act:** [{tid}]({LEGISLATION_BASE}/{tid}/latest/versions)")
        lines.append(f"- **Discovered via:** {res.get('discovery_source', '-')}")
        if bill_title and bill_title != name:
            lines.append(f"- **Bill:** {bill_title}")
        if parlinfo_url:
            lines.append(f"- **ParlInfo:** [{parlinfo_url}]({parlinfo_url})")
        if res.get("bill_id"):
            lines.append(f"- **Bill ID:** {res['bill_id']}")
        source_label = {
            "parlinfo_bill_home": "ParlInfo Bill Home – Summary",
            "parlinfo_bill_home_short": "ParlInfo Bill Home – Summary (< 100 words)",
            "bills_digest": "Bills Digest – Key Points",
        }.get(source, source or "-")
        lines.append(f"- **Summary source:** {source_label}")
        lines.append("")

        if summary:
            lines.append("### Plain-English Summary")
            lines.append("")
            lines.append(summary)
        elif status == "no_parlinfo_url":
            lines.append(
                "> ⚠️ No ParlInfo bill home link found on the legislation.gov.au page for this Act. "
                "It may be a commencement or administrative instrument with no EM."
            )
        elif status == "scrape_error":
            lines.append(
                "> ⚠️ Found the ParlInfo URL but could not retrieve summary content. "
                "Check the link above manually."
            )
        else:
            lines.append("> ⚠️ No summary text could be extracted. Check the ParlInfo link above.")

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def write_step_summary(report_md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(report_md)
    log("GitHub Step Summary written.")


def write_output_file(report_md: str, title_id: str, compilation_label: str) -> str:
    from pathlib import Path
    out_dir = Path("em_summaries") / title_id
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"EM_summary_{title_id}_{compilation_label}.md"
    out_path = out_dir / filename
    out_path.write_text(report_md, encoding="utf-8")
    log(f"Report written -> {out_path}")
    return str(out_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: python fetch_em_summary.py <legislation_url> <compilation_number>\n"
            "Example: python fetch_em_summary.py "
            "https://www.legislation.gov.au/C2004A04014/latest/versions C50"
        )
        sys.exit(1)

    url_input = sys.argv[1].strip()
    comp_input = sys.argv[2].strip().upper()

    log_section("FRL EM Summary Fetcher")
    log(f"Input URL   : {url_input}")
    log(f"Compilation : {comp_input}")

    try:
        title_id = extract_title_id(url_input)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    log(f"Title ID    : {title_id}")

    log_section("Fetching compilation from FRL API")
    try:
        version_data = get_compilation(title_id, comp_input)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)

    register_id = version_data.get("registerId", "unknown")
    start = version_data.get("start", "")
    log(f"Register ID : {register_id}")
    log(f"Start date  : {start[:10] if start else 'unknown'}")

    version_data.setdefault("titleId", title_id)

    log_section("Discovering amending Acts")
    amending_acts = discover_amending_acts(version_data)

    if not amending_acts:
        log("No amending Acts found via any discovery method.")
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
        result = process_amending_act(act)
        results.append(result)

    log_section("Generating report")
    report_md = generate_report(title_id, comp_input, results)

    print("\n" + "=" * 60)
    print(report_md)
    print("=" * 60)

    out_path = write_output_file(report_md, title_id, comp_input)
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
