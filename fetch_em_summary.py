"""
fetch_em_summary.py
--------------------
Given a legislation.gov.au URL and a compilation number, finds every Act that
amended that compilation, retrieves its Explanatory Memorandum summary from
ParlInfo, and prints a plain-English explanation to stdout (and to
$GITHUB_STEP_SUMMARY if running in GitHub Actions).

Usage:
    python fetch_em_summary.py <legislation_url> <compilation_number>

How amending Acts are discovered (three-layer approach):

    Layer 1 — registerId check
        If the compilation's own registerId matches C####A##### (Act series),
        that ID IS the amending Act. Common for single-amendment compilations
        e.g. C2016A00004, C2023A00074.

    Layer 2 — reasons array
        Walk Version.reasons for amendedByTitle / affectedByTitle entries
        whose titleId matches C####A#####.
        Note: this array is sometimes empty even when amendments exist.

    Layer 3 — Affect API endpoint
        Call GET /v1/Affect?$filter=affectedTitleId eq '{titleId}' and match
        entries whose start date aligns with this compilation's start date.
        This is the dedicated FRL endpoint for amendment relationships and is
        the most reliable source when reasons is empty.

How the ParlInfo EM summary is retrieved:
    Scrape the amending Act's own legislation.gov.au /latest/versions page
    for the "Originating Bill and Explanatory Memorandum" ParlInfo link, then:
    1. Extract the <summary> element or b.bills-delimited content.
    2. If < 100 words, fall back to the Bills Digest Key Points section.
"""

from __future__ import annotations

import os
import re
import sys
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FRL_API = "https://api.prod.legislation.gov.au/v1"
LEGISLATION_BASE = "https://www.legislation.gov.au"
PARLINFO_DISPLAY = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"

TITLE_ID_RE = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")

# Strictly Acts: C + 4 digits + "A" + digits  (e.g. C2023A00074)
# Excludes compilation register IDs (C####C#####) and instruments (F####L#####)
ACT_SERIES_RE = re.compile(r"^C\d{4}A\d+$")

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
# Layer 3: Affect API endpoint
# ---------------------------------------------------------------------------
def get_amending_acts_via_affect_api(title_id: str, compilation_start: str) -> list[dict]:
    """
    Query the FRL Affect endpoint for all Acts that amended this title,
    then filter to those whose dateChanged aligns with the compilation start date.

    The Affect endpoint is the dedicated FRL API resource for amendment
    relationships. It is more reliably populated than Version.reasons.

    Args:
        title_id: The principal Act's title ID (e.g. 'C2004A04014')
        compilation_start: The compilation's start date from the API
                           (ISO format, e.g. '2023-10-18T00:00:00')

    Returns:
        List of dicts with titleId, name, affect, source='affect_api'
    """
    # Extract date portion for comparison (2023-10-18T00:00:00 -> 2023-10-18)
    comp_date = compilation_start[:10] if compilation_start else ""

    params = urlencode({
        "$filter": f"affectedTitleId eq '{title_id}'",
        "$top": "50",
    })
    url = f"{FRL_API}/Affect?{params}"
    log(f"  Affect API -> {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"  Affect API failed: {exc}")
        return []

    results = []
    seen: set[str] = set()
    entries = data.get("value", data if isinstance(data, list) else [])

    for entry in entries:
        affecting_id = entry.get("affectingTitleId", "")
        if not affecting_id or not ACT_SERIES_RE.match(affecting_id):
            continue

        # Match by date if we have one
        if comp_date:
            entry_date = str(entry.get("dateChanged", "") or "")[:10]
            if entry_date and entry_date != comp_date:
                continue

        if affecting_id not in seen:
            seen.add(affecting_id)
            affecting_title = entry.get("affectingTitle") or {}
            name = (
                affecting_title.get("name", "")
                if isinstance(affecting_title, dict)
                else str(affecting_title)
            )
            results.append({
                "titleId": affecting_id,
                "name": name,
                "affect": entry.get("affect", "Amend"),
                "source": "affect_api",
            })
            log(f"  Found via Affect API: {affecting_id} ({name})")

    # If date filtering returned nothing, retry without date constraint
    # (handles cases where the API date field differs slightly from compilation start)
    if not results and comp_date:
        log("  No date-matched results — retrying Affect API without date filter ...")
        for entry in entries:
            affecting_id = entry.get("affectingTitleId", "")
            if not affecting_id or not ACT_SERIES_RE.match(affecting_id):
                continue
            if affecting_id not in seen:
                seen.add(affecting_id)
                affecting_title = entry.get("affectingTitle") or {}
                name = (
                    affecting_title.get("name", "")
                    if isinstance(affecting_title, dict)
                    else str(affecting_title)
                )
                results.append({
                    "titleId": affecting_id,
                    "name": name,
                    "affect": entry.get("affect", "Amend"),
                    "source": "affect_api_undated",
                })
                log(f"  Found (undated): {affecting_id} ({name})")

    return results


# ---------------------------------------------------------------------------
# Amending Act discovery — all three layers
# ---------------------------------------------------------------------------
def discover_amending_acts(version_data: dict) -> list[dict]:
    """
    Identify all Acts that amended this compilation using three layers.
    Results are deduplicated by titleId.
    """
    seen: set[str] = set()
    results: list[dict] = []

    def add(act: dict) -> None:
        tid = act.get("titleId", "")
        if tid and tid not in seen and ACT_SERIES_RE.match(tid):
            seen.add(tid)
            results.append(act)

    title_id = version_data.get("titleId", "")
    register_id = version_data.get("registerId", "")
    start = version_data.get("start", "")

    # --- Layer 1: registerId ---
    if ACT_SERIES_RE.match(register_id):
        log(f"  Layer 1 (registerId): {register_id} is an amending Act")
        add({"titleId": register_id, "name": "", "affect": "Amend", "source": "registerId"})
    else:
        log(f"  Layer 1 (registerId): {register_id} is not an Act series ID")

    # --- Layer 2: reasons array ---
    reasons = version_data.get("reasons", [])
    log(f"  Layer 2 (reasons array): {len(reasons)} reason(s)")
    for reason in reasons:
        affect = reason.get("affect", "")
        for key in ("amendedByTitle", "affectedByTitle"):
            obj = reason.get(key) or {}
            if not isinstance(obj, dict):
                continue
            tid = obj.get("titleId", "")
            name = obj.get("name", "")
            if tid:
                add({"titleId": tid, "name": name, "affect": affect, "source": "reasons"})
            break

    # --- Layer 3: Affect API (always run as a safety net) ---
    log(f"  Layer 3 (Affect API): querying for affectedTitleId={title_id}")
    affect_results = get_amending_acts_via_affect_api(title_id, start)
    for act in affect_results:
        add(act)

    return results


# ---------------------------------------------------------------------------
# ParlInfo URL discovery
# ---------------------------------------------------------------------------
def find_parlinfo_url(amending_act_id: str) -> str | None:
    """
    Scrape the amending Act's legislation.gov.au page to find the
    "Originating Bill and Explanatory Memorandum" ParlInfo link.

    The link appears on the Act's own versions page, not the principal Act's page.
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

        # Strategy C: raw regex scan (catches JS-rendered or data-attribute links)
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
def scrape_bill_summary(parlinfo_url: str) -> tuple[str, str]:
    """
    Scrape bill title and summary from a ParlInfo bill home page.
    Returns (bill_title, summary_text).

    Three extraction strategies:
      1. <summary> element (XPATH: /html/body/.../summary)
      2. Content between <b class="bills">Summary</b> and
         <b class="bills">Progress of bill</b>
      3. Any div/section with 'summary' in its attributes
    """
    log(f"  Scraping ParlInfo -> {parlinfo_url}")
    resp = requests.get(parlinfo_url, headers=HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Bill title
    bill_title = ""
    for selector in ["h1", "h2.bills", ".billTitle"]:
        el = soup.select_one(selector)
        if el:
            bill_title = el.get_text(strip=True)
            break

    # Strategy 1: <summary> element
    summary_el = soup.find("summary")
    if summary_el:
        text = summary_el.get_text(separator=" ", strip=True)
        if len(text.split()) >= 15:
            log(f"    <summary> element: {len(text.split())} words")
            return bill_title, text

    # Strategy 2: b.bills markers
    summary_start = None
    for b_tag in soup.find_all("b", class_="bills"):
        if re.search(r"\bSummary\b", b_tag.get_text(), re.IGNORECASE):
            summary_start = b_tag
            break

    if summary_start:
        chunks = []
        for sibling in summary_start.parent.next_siblings:
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
            elif str(sibling).strip() and not str(sibling).startswith("<"):
                chunks.append(str(sibling).strip())
        if chunks:
            text = " ".join(chunks)
            log(f"    b.bills markers: {len(text.split())} words")
            return bill_title, text

    # Strategy 3: div/section with 'summary' in attributes
    for tag in soup.find_all(["div", "section", "article"]):
        attrs_str = " ".join(str(v) for v in tag.attrs.values()).lower()
        if "summary" in attrs_str:
            text = tag.get_text(separator=" ", strip=True)
            if len(text.split()) >= 20:
                log(f"    div/section fallback: {len(text.split())} words")
                return bill_title, text

    log("    WARNING: Could not locate summary content")
    return bill_title, ""


# ---------------------------------------------------------------------------
# Bills Digest fallback
# ---------------------------------------------------------------------------
def extract_bill_id(parlinfo_url: str) -> str | None:
    """Extract bill ID like 'r7042' from a ParlInfo URL."""
    match = re.search(r"billhome[/%2F]+([a-zA-Z][0-9]+)", parlinfo_url, re.IGNORECASE)
    return match.group(1) if match else None


def scrape_bills_digest(bill_id: str) -> str:
    """
    Fetch the Bills Digest and extract the Key Points section
    (between <p>Key points</p> and <p>Contents</p>).
    """
    search_url = (
        f"{PARLINFO_DISPLAY}"
        f";query=BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs;rec=0"
    )
    log(f"  Bills Digest -> {search_url}")
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        log(f"    Bills Digest failed: {exc}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

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
    """
    Full pipeline for one amending Act:
      1. Find the ParlInfo bill home URL (scrape legislation.gov.au)
      2. Scrape the summary from ParlInfo
      3. If < MIN_SUMMARY_WORDS, fall back to Bills Digest
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
        "summary": "",
        "summary_source": "",
        "status": "not_found",
    }

    # Step 1: find ParlInfo URL
    parlinfo_url = find_parlinfo_url(tid)
    if not parlinfo_url:
        log(f"  Could not find a ParlInfo URL for {tid}")
        result["status"] = "no_parlinfo_url"
        return result

    result["parlinfo_url"] = parlinfo_url
    result["bill_id"] = extract_bill_id(parlinfo_url)

    # Step 2: scrape ParlInfo bill summary
    try:
        bill_title, summary = scrape_bill_summary(parlinfo_url)
    except Exception as exc:
        log(f"  ParlInfo scrape failed: {exc}")
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

    # Step 3: Bills Digest fallback
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
            "parlinfo_bill_home_short": "ParlInfo Bill Home – Summary (short, < 100 words)",
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
                "It may be a commencement or administrative instrument with no EM, "
                "or the page structure was unexpected."
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

    # Inject titleId into version_data so discover_amending_acts can use it
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
