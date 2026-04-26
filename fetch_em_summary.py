"""
fetch_em_summary.py
--------------------
Given a legislation.gov.au URL and a compilation number, finds every Act that
amended that compilation, retrieves a plain-English explanation from ParlInfo,
and prints a report to stdout and $GITHUB_STEP_SUMMARY.

Usage:
    python fetch_em_summary.py <legislation_url> <compilation_number>

Content source priority (each case-insensitive, markers on their own line):
    1. Bills Digest     — between 'Key Points' and 'Contents'
    2. Summary >100w    — between 'Summary' and 'Progress of bill'
    3. Explan. Memo     — between 'General Outline'|'Outline' and
                          'Financial Impact'|'Financial Impact Statement'
    4. Summary <100w    — same extraction as #2 (best-effort fallback)

How amending Acts are discovered (three layers):
    1. registerId check — if compilationregisterId is C####A##### it IS the Act
    2. reasons array    — Version.reasons amendedByTitle/affectedByTitle
    3. Affect API       — GET /v1/Affect?$filter=affectedTitleId eq '{titleId}'

ParlInfo is protected by an Azure WAF JS Challenge. selenium-stealth patches
navigator.webdriver and other automation fingerprints so the challenge passes.
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FRL_API          = "https://api.prod.legislation.gov.au/v1"
LEGISLATION_BASE = "https://www.legislation.gov.au"
PARLINFO_DISPLAY = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"

TITLE_ID_RE  = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")
ACT_SERIES_RE = re.compile(r"^C\d{4}A\d+$")

# Regex to find EM links in bill home page HTML
EM_LINK_RE = re.compile(
    r'https?://parlinfo\.aph\.gov\.au/parlInfo/search/display/display\.w3p'
    r'[^\s"\'<>]*legislation%2Fems%2F[^\s"\'<>]+',
    re.IGNORECASE,
)

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
        raise ValueError(f"Could not extract a Title ID from: {url}")
    return match.group(1)


def get_compilation(title_id: str, compilation_number: str) -> dict:
    comp_num = re.sub(r"^[Cc]", "", compilation_number).strip()
    url = (
        f"{FRL_API}/Versions/Find("
        f"titleId='{title_id}',"
        f"compilationNumber='{comp_num}')"
    )
    log(f"FRL API -> {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError(f"Compilation '{compilation_number}' not found for '{title_id}'.")
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Amending Act discovery (three layers)
# ---------------------------------------------------------------------------
def _add_act(seen: set, results: list, tid: str, name: str, affect: str, source: str) -> bool:
    """Add an amending Act to results if valid. Returns True if added."""
    if tid and tid not in seen and ACT_SERIES_RE.match(tid):
        seen.add(tid)
        results.append({"titleId": tid, "name": name, "affect": affect, "source": source})
        return True
    return False


def discover_amending_acts(version_data: dict) -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []

    title_id    = version_data.get("titleId", "")
    register_id = version_data.get("registerId", "")
    start       = version_data.get("start", "")

    # Layer 1: registerId
    if ACT_SERIES_RE.match(register_id):
        log(f"  Layer 1 (registerId): {register_id} is an amending Act")
        _add_act(seen, results, register_id, "", "Amend", "registerId")
    else:
        log(f"  Layer 1 (registerId): {register_id} is not an Act series ID")

    # Layer 2: reasons array
    # Check BOTH amendedByTitle AND affectedByTitle for each reason —
    # do NOT break after the first one, as amendedByTitle.titleId is sometimes
    # empty while affectedByTitle has the Act ID (or vice versa).
    # Also scan the markdown field for Act IDs as a last resort.
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
        # Fallback: scan markdown field for embedded Act IDs (C####A##### pattern)
        if not found_via_title:
            markdown = reason.get("markdown", "") or ""
            for tid in re.findall(r'C\d{4}A\d+', markdown) if markdown else []:
                log(f"      markdown scan: found {tid!r}")
                _add_act(seen, results, tid, "", affect, f"reasons[{i}].markdown")

    # Layer 3: Affect API
    comp_date = start[:10] if start else ""
    log(f"  Layer 3 (Affect API): affectedTitleId={title_id}, date={comp_date}")
    filter_expr = f"affectedTitleId eq '{title_id}'"
    # Try both endpoint names — /v1/Affect (EntitySet) and /v1/_AffectsSearch (search context)
    affect_endpoints = [
        f"{FRL_API}/_AffectsSearch?$filter={quote(filter_expr)}&$top=50",
        f"{FRL_API}/Affect?$filter={quote(filter_expr)}&$top=50",
    ]
    for url in affect_endpoints:
        log(f"  Affect API -> {url}")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                log(f"  404 — trying next endpoint")
                continue
            resp.raise_for_status()
            entries = resp.json().get("value", [])
            log(f"  Affect API returned {len(entries)} entries")
            matched = [e for e in entries if str(e.get("dateChanged", ""))[:10] == comp_date] if comp_date else entries
            if not matched and comp_date:
                log("  No date-matched entries — using all entries")
                matched = entries
            for entry in matched:
                tid = entry.get("affectingTitleId", "")
                obj = entry.get("affectingTitle") or {}
                name = obj.get("name", "") if isinstance(obj, dict) else ""
                _add_act(seen, results, tid, name, entry.get("affect", "Amend"), "affect_api")
            break  # success — don't try next endpoint
        except Exception as exc:
            log(f"  Affect API failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Stealth browser fetch (passes Azure WAF JS Challenge)
# ---------------------------------------------------------------------------
def _fetch_with_stealth(url: str) -> str:
    """Drive headless Chromium via selenium-stealth to bypass Azure WAF."""
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
    driver  = webdriver.Chrome(service=service, options=options)
    stealth(
        driver,
        languages=["en-AU", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )
    log(f"    stealth: navigating to {url[:90]}")
    try:
        driver.get(url)
        for _ in range(20):          # wait up to 10 s for WAF challenge
            time.sleep(0.5)
            if "Azure WAF" not in driver.page_source:
                break
        html = driver.page_source
        log(f"    stealth: got {len(html)} chars")
        return html
    finally:
        driver.quit()


def fetch_parlinfo(url: str) -> str:
    """
    Fetch any parlinfo.aph.gov.au page.
    Primary: selenium-stealth (bypasses Azure WAF JS Challenge).
    Fallback: requests (for local testing only; will 403 on the live site).
    """
    # Primary: stealth
    try:
        html = _fetch_with_stealth(url)
        if len(html) > 500 and "Azure WAF" not in html:
            return html
        preview = html[:200].replace("\n", " ")
        log(f"    stealth: still WAF page — {preview!r}")
    except ImportError:
        log("    selenium-stealth not installed — falling back to requests")
    except Exception as exc:
        log(f"    stealth failed: {exc}")

    # Fallback: requests
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


# ---------------------------------------------------------------------------
# Generic marker-based text extractor
# ---------------------------------------------------------------------------
def extract_between_markers(
    soup: BeautifulSoup,
    start_patterns: list[str],
    end_patterns: list[str],
) -> str:
    """
    Find the first element whose full text matches any start_pattern
    (case-insensitive, must be a short 'label' element ≤80 chars),
    then collect text from following elements until one matches an end_pattern.

    Returns the extracted text, or "" if the start marker is not found.
    """
    # Find start marker
    start_el = None
    for tag in soup.find_all(True):
        tag_text = tag.get_text(strip=True)
        if len(tag_text) > 80:
            continue
        for pat in start_patterns:
            if re.fullmatch(pat, tag_text, re.IGNORECASE):
                start_el = tag
                break
        if start_el:
            break

    if not start_el:
        return ""

    # Collect leaf text until end marker
    chunks: list[str] = []
    seen_texts: set[str] = set()

    for tag in start_el.find_all_next():
        tag_text = tag.get_text(strip=True)

        # Check end marker (short label elements only)
        if len(tag_text) <= 80:
            for pat in end_patterns:
                if re.fullmatch(pat, tag_text, re.IGNORECASE):
                    return " ".join(chunks).strip()

        # Collect leaf elements (no block children — avoids duplication)
        if tag.name in ("p", "li", "td", "span", "dd", "summary") and \
                not tag.find(["p", "li", "td", "dd"]):
            text = tag.get_text(separator=" ", strip=True)
            if text and text not in seen_texts:
                seen_texts.add(text)
                chunks.append(text)

    return " ".join(chunks).strip()


# ---------------------------------------------------------------------------
# Source-specific scrapers
# ---------------------------------------------------------------------------
def scrape_bills_digest(bill_id: str) -> str:
    """
    Location 1: Bills Digest.
    URL: BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs
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
    Location 2/4: Bill home Summary.
    Extract: between 'Summary' and 'Progress of bill' (case-insensitive, own line).
    """
    soup = BeautifulSoup(bill_home_html, "html.parser")

    # Log diagnostics
    summary_els = soup.find_all("summary")
    log(f"    <summary> elements on page: {len(summary_els)}")

    text = extract_between_markers(
        soup,
        start_patterns=[r"summary"],
        end_patterns=[r"progress\s+of\s+bill"],
    )

    # Fallback: if marker extraction got nothing, try direct <summary> element
    if not text:
        for el in summary_els:
            parent_details = el.find_parent("details")
            if parent_details:
                # <details>/<summary> accordion — get full details content
                chunks = [
                    c.get_text(separator=" ", strip=True)
                    for c in parent_details.children
                    if hasattr(c, "get_text")
                ]
                text = " ".join(filter(None, chunks))
            else:
                text = el.get_text(separator=" ", strip=True)
            if len(text.split()) >= 5:
                break

    log(f"    Bill summary: {len(text.split())} words extracted")
    return text


def find_em_url(bill_home_html: str) -> str | None:
    """
    Scan the bill home page HTML for a link to the Explanatory Memorandum.
    EM links contain 'legislation%2Fems%2F' in the parlinfo query.
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
    Location 3: Explanatory Memorandum.
    Extract: between 'General Outline'|'Outline' and
             'Financial Impact'|'Financial Impact Statement'
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


# ---------------------------------------------------------------------------
# ParlInfo link discovery (on legislation.gov.au)
# ---------------------------------------------------------------------------
def find_parlinfo_url(amending_act_id: str) -> str | None:
    """Scrape the amending Act's legislation.gov.au page for its ParlInfo bill home link."""
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
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "parlinfo.aph.gov.au" in href and "billhome" in href.lower():
                log(f"    Found: {href}")
                return href
        for a in soup.find_all("a", href=True):
            if "originating bill" in a.get_text(strip=True).lower() and "parlinfo" in a["href"].lower():
                log(f"    Found via link text: {a['href']}")
                return a["href"]
        matches = re.findall(
            r'https?://parlinfo\.aph\.gov\.au/parlInfo/search/display/[^\s\'"<>]+billhome[^\s\'"<>]+',
            html, re.IGNORECASE,
        )
        if matches:
            log(f"    Found via regex: {matches[0]}")
            return matches[0]
        log("    No ParlInfo link found")
    return None


def extract_bill_id(parlinfo_url: str) -> str | None:
    match = re.search(r"billhome[/%2F]+([a-zA-Z][0-9]+)", parlinfo_url, re.IGNORECASE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Per-Act orchestration
# ---------------------------------------------------------------------------
def process_amending_act(act: dict) -> dict:
    """
    Priority pipeline for one amending Act:
      1. Bills Digest      — Key Points → Contents
      2. Summary >100 w    — Summary → Progress of bill
      3. Explan. Memo      — General Outline/Outline → Financial Impact
      4. Summary <100 w    — same extraction as #2 (best-effort fallback)
    """
    tid = act["titleId"]
    result = {
        "titleId": tid,
        "name": act.get("name", ""),
        "affect": act.get("affect", ""),
        "discovery_source": act.get("source", ""),
        "parlinfo_url": None,
        "bill_id": None,
        "bill_title": "",
        "em_url": None,
        "summary": "",
        "summary_source": "",
        "status": "not_found",
    }

    # --- Find ParlInfo bill home URL ---
    parlinfo_url = find_parlinfo_url(tid)
    if not parlinfo_url:
        log(f"  Could not find a ParlInfo URL for {tid}")
        result["status"] = "no_parlinfo_url"
        return result

    result["parlinfo_url"] = parlinfo_url
    bill_id = extract_bill_id(parlinfo_url)
    result["bill_id"] = bill_id

    # --- Fetch bill home page once (reused for summary + EM URL discovery) ---
    log(f"  Fetching bill home page for {tid} ...")
    try:
        bill_home_html = fetch_parlinfo(parlinfo_url)
    except Exception as exc:
        log(f"  Bill home fetch failed: {exc}")
        result["status"] = "scrape_error"
        return result

    # Extract bill title from page
    soup_home = BeautifulSoup(bill_home_html, "html.parser")
    for selector in ["h1", "h2.bills", ".billTitle"]:
        el = soup_home.select_one(selector)
        if el:
            t = el.get_text(strip=True)
            if len(t) > 10 and "parlinfo" not in t.lower():
                result["bill_title"] = t
                break

    # -----------------------------------------------------------------------
    # Priority 1: Bills Digest
    # -----------------------------------------------------------------------
    if bill_id:
        digest = scrape_bills_digest(bill_id)
        if digest and len(digest.split()) >= 10:
            result["summary"]        = digest
            result["summary_source"] = "bills_digest"
            result["status"]         = "success"
            return result
        log("  [1] Bills Digest: no usable content — trying next source")

    # -----------------------------------------------------------------------
    # Priority 2: Summary > 100 words
    # -----------------------------------------------------------------------
    summary = scrape_bill_summary(bill_home_html)
    if len(summary.split()) >= MIN_SUMMARY_WORDS:
        result["summary"]        = summary
        result["summary_source"] = "bill_summary"
        result["status"]         = "success"
        return result
    log(f"  [2] Summary: {len(summary.split())} words (< {MIN_SUMMARY_WORDS}) — trying next source")

    # -----------------------------------------------------------------------
    # Priority 3: Explanatory Memorandum
    # -----------------------------------------------------------------------
    em_url = find_em_url(bill_home_html)
    result["em_url"] = em_url
    if em_url:
        em_text = scrape_em(em_url)
        if em_text and len(em_text.split()) >= 10:
            result["summary"]        = em_text
            result["summary_source"] = "explanatory_memorandum"
            result["status"]         = "success"
            return result
    log("  [3] EM: no usable content — using summary fallback")

    # -----------------------------------------------------------------------
    # Priority 4: Summary < 100 words (best-effort fallback)
    # -----------------------------------------------------------------------
    if summary:
        result["summary"]        = summary
        result["summary_source"] = "bill_summary_short"
        result["status"]         = "success_short"
        return result

    log("  [4] No content found from any source")
    result["status"] = "no_summary_found"
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def generate_report(principal_title_id: str, compilation_label: str, results: list[dict]) -> str:
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
    lines.append(f"Found **{len(results)}** amending Act(s). Summaries retrieved for **{success_count}**.")
    lines.append("")

    source_labels = {
        "bills_digest":             "Bills Digest — Key Points",
        "bill_summary":             "Bill Home — Summary (≥100 words)",
        "explanatory_memorandum":   "Explanatory Memorandum — General Outline",
        "bill_summary_short":       "Bill Home — Summary (<100 words)",
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


def write_step_summary(report_md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(report_md)
    log("GitHub Step Summary written.")


def write_output_file(report_md: str, title_id: str, compilation_label: str) -> str:
    from pathlib import Path
    out_dir  = Path("em_summaries") / title_id
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
        print("Usage: python fetch_em_summary.py <legislation_url> <compilation_number>")
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
            f"**Principal Act:** {title_id}  \n**Compilation:** {comp_input}  \n\n"
            f"No amending Acts were found for this compilation.\n"
        )
        write_step_summary(report)
        write_output_file(report, title_id, comp_input)
        sys.exit(0)

    log(f"Found {len(amending_acts)} amending Act(s):")
    for act in amending_acts:
        log(f"  * {act['titleId']}  (via {act['source']})  {act.get('name','')}")

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
        log("WARNING: No summaries retrieved.")
        sys.exit(1)


if __name__ == "__main__":
    main()
