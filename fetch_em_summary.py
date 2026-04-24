"""
fetch_em_summary.py
--------------------
Given a legislation.gov.au URL and a compilation number, finds every Act that
amended that compilation, retrieves its Explanatory Memorandum summary from
ParlInfo, and prints a plain-English explanation to stdout (and to
$GITHUB_STEP_SUMMARY if running in GitHub Actions).

Usage:
    python fetch_em_summary.py <legislation_url> <compilation_number>

Examples:
    python fetch_em_summary.py https://www.legislation.gov.au/C2015A00040/latest/versions C09
    python fetch_em_summary.py https://www.legislation.gov.au/C2004A04969/latest/versions C47

Logic:
    1. Extract the Title ID from the URL.
    2. Call FRL API Versions/Find() to get the compilation and its 'reasons'.
    3. Filter reasons to Acts only (titleId starts with 'C2').
    4. For each amending Act, call FRL API to get its 'parliamentaryInformation'
       or fall back to scraping the legislation.gov.au "as made" page to find
       the ParlInfo bill home URL.
    5. Scrape the ParlInfo bill home page for the <summary> element.
    6. If the summary is < 100 words, find and scrape the Bills Digest instead.
    7. Write a structured plain-English report.
"""

from __future__ import annotations

import os
import re
import sys
import json
import textwrap
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FRL_API = "https://api.prod.legislation.gov.au/v1"
PARLINFO_BASE = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"
BILLS_DIGEST_SEARCH = "https://parlinfo.aph.gov.au/parlInfo/search/display/display.w3p"

TITLE_ID_RE = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")

# Acts have titleIds that begin with C (C2015A00040, C2026A00001, etc.)
ACT_ID_RE = re.compile(r"^C\d{4}[A-Z]\d+$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TripwireBot/1.0; "
        "+https://github.com/ipaventures/tripwire)"
    )
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
    """Fetch a specific compilation via the FRL API."""
    # Strip leading C/c from compilation number (C09 → 9)
    comp_num = re.sub(r"^[Cc]", "", compilation_number).strip()
    url = (
        f"{FRL_API}/Versions/Find("
        f"titleId='{title_id}',"
        f"compilationNumber='{comp_num}')"
    )
    log(f"FRL API → GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError(
            f"Compilation '{compilation_number}' not found for '{title_id}'.\n"
            "Check the URL and compilation number are correct."
        )
    resp.raise_for_status()
    return resp.json()


def get_asmade_version(title_id: str) -> dict:
    """Fetch the as-made version of an amending Act."""
    url = (
        f"{FRL_API}/Versions/Find("
        f"titleId='{title_id}',"
        f"asAtSpecification='AsMade')"
    )
    log(f"FRL API (as-made) → GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_amending_acts(version_data: dict) -> list[dict]:
    """
    Extract amending instruments from the 'reasons' array.
    Returns only Acts (titleId matches ACT_ID_RE).
    """
    seen: set[str] = set()
    results: list[dict] = []

    for reason in version_data.get("reasons", []):
        affect = reason.get("affect", "")

        # Primary: amendedByTitle; fallback: affectedByTitle
        for key in ("amendedByTitle", "affectedByTitle"):
            obj = reason.get(key) or {}
            if not isinstance(obj, dict):
                continue
            tid = obj.get("titleId", "")
            name = obj.get("name", "")
            if tid and tid not in seen and ACT_ID_RE.match(tid):
                seen.add(tid)
                results.append(
                    {
                        "titleId": tid,
                        "name": name,
                        "affect": affect,
                        "registerId": obj.get("registerId", ""),
                    }
                )
            break

    return results


# ---------------------------------------------------------------------------
# ParlInfo URL discovery
# ---------------------------------------------------------------------------
def find_parlinfo_url_from_legislation_page(title_id: str) -> str | None:
    """
    Scrape the legislation.gov.au 'as made' versions page for the amending Act
    and look for an 'Originating Bill and Explanatory Memorandum' link that
    points to parlinfo.aph.gov.au.
    """
    candidate_urls = [
        f"https://www.legislation.gov.au/{title_id}/latest/versions",
        f"https://www.legislation.gov.au/{title_id}/asmade/versions",
    ]

    for page_url in candidate_urls:
        log(f"Scraping legislation page → {page_url}")
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=30, allow_redirects=True)
            if resp.status_code != 200:
                continue
        except Exception as exc:
            log(f"  Request failed: {exc}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for any anchor pointing to parlinfo and containing bill-home query
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "parlinfo.aph.gov.au" in href and "billhome" in href.lower():
                log(f"  Found ParlInfo link: {href}")
                return href

        # Also check for links with text "Originating Bill"
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if "originating bill" in text and "parlinfo" in a["href"]:
                log(f"  Found via link text: {a['href']}")
                return a["href"]

    return None


def find_parlinfo_url_from_asmade_api(version_data: dict) -> str | None:
    """
    Look for a ParlInfo URL embedded in the as-made version's documents or
    parliamentary information fields.
    """
    # Check top-level fields
    for field in ("parliamentaryInformationUrl", "billHomeUrl", "emUrl"):
        val = version_data.get(field)
        if val and "parlinfo" in str(val).lower():
            return val

    # Check documents array for EM-type links
    for doc in version_data.get("documents", []):
        url = doc.get("url", "") or doc.get("downloadUrl", "")
        if url and "parlinfo" in url.lower():
            return url

    # Check reasons for parlinfo references
    for reason in version_data.get("reasons", []):
        for key in ("emUrl", "billHomeUrl", "parliamentaryUrl"):
            val = reason.get(key)
            if val and "parlinfo" in str(val).lower():
                return val

    return None


def build_parlinfo_search_url(bill_id: str) -> str:
    """Build a ParlInfo bill home search URL from a bill ID like 'r7421'."""
    query = f'Id%3A%22legislation%2Fbillhome%2F{bill_id}%22'
    return f"{PARLINFO_BASE};query={query}"


def extract_bill_id_from_parlinfo_url(url: str) -> str | None:
    """Extract the bill ID (e.g. 'r7421') from a parlinfo URL."""
    # Matches patterns like billhome/r7421 or billhome%2Fr7421
    match = re.search(r"billhome[/%2F]+([a-z][0-9]+)", url, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# ParlInfo scraping
# ---------------------------------------------------------------------------
def scrape_parlinfo_summary(parlinfo_url: str) -> tuple[str, str]:
    """
    Scrape the bill summary from a ParlInfo bill home page.

    Returns (bill_title, summary_text).
    The summary is extracted from:
      - <summary> element (preferred, matches XPATH /html/body/.../summary)
      - or content between <b class="bills">Summary</b> and
        <b class="bills">Progress of bill</b>
    """
    log(f"Scraping ParlInfo bill home → {parlinfo_url}")
    resp = requests.get(parlinfo_url, headers=HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract bill title
    bill_title = ""
    h1 = soup.find("h1")
    if h1:
        bill_title = h1.get_text(strip=True)

    # --- Strategy 1: <summary> element (matches the XPATH in the spec) ---
    summary_el = soup.find("summary")
    if summary_el:
        text = summary_el.get_text(separator=" ", strip=True)
        if len(text.split()) >= 20:
            log(f"  Found summary via <summary> element ({len(text.split())} words)")
            return bill_title, text

    # --- Strategy 2: content between Summary and Progress of bill markers ---
    summary_marker = None
    for b in soup.find_all("b", class_="bills"):
        if "Summary" in b.get_text():
            summary_marker = b
            break

    if summary_marker:
        chunks = []
        for sibling in summary_marker.parent.next_siblings:
            # Stop when we hit the Progress of bill marker
            if hasattr(sibling, "find_all"):
                if any(
                    "Progress of bill" in b.get_text()
                    for b in sibling.find_all("b", class_="bills")
                ):
                    break
                # Also stop at another major heading
                text_chunk = sibling.get_text(separator=" ", strip=True)
                if text_chunk:
                    chunks.append(text_chunk)
            elif str(sibling).strip():
                chunks.append(str(sibling).strip())

        if chunks:
            text = " ".join(chunks)
            log(f"  Found summary via b.bills markers ({len(text.split())} words)")
            return bill_title, text

    # --- Strategy 3: broad fallback — find any div with 'summary' in class/id ---
    for tag in soup.find_all(["div", "section", "article"]):
        attrs = " ".join(str(v) for v in tag.attrs.values()).lower()
        if "summary" in attrs:
            text = tag.get_text(separator=" ", strip=True)
            if len(text.split()) >= 20:
                log(f"  Found summary via div/section fallback ({len(text.split())} words)")
                return bill_title, text

    log("  WARNING: Could not locate summary on ParlInfo page.")
    return bill_title, ""


def scrape_bills_digest(bill_id: str) -> str:
    """
    Fetch the Bills Digest for a given bill ID and extract the 'Key points'
    section (between <p>Key points</p> and <p>Contents</p>).
    """
    search_url = (
        f"{BILLS_DIGEST_SEARCH}"
        f";query=BillId_Phrase%3A%22{bill_id}%22%20Dataset%3Abillsdgs;rec=0"
    )
    log(f"Scraping Bills Digest → {search_url}")

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        log(f"  Bills Digest request failed: {exc}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find <p> with "Key points" text
    key_points_marker = None
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if re.search(r"key\s+points", text, re.IGNORECASE):
            key_points_marker = p
            break

    if not key_points_marker:
        # Try broader: find any element mentioning Key points
        for tag in soup.find_all(["h2", "h3", "strong", "b"]):
            if re.search(r"key\s+points", tag.get_text(), re.IGNORECASE):
                key_points_marker = tag
                break

    if not key_points_marker:
        log("  Could not find 'Key points' marker in Bills Digest.")
        return ""

    chunks = []
    for sibling in key_points_marker.next_siblings:
        if hasattr(sibling, "get_text"):
            sib_text = sibling.get_text(strip=True)
            # Stop at Contents or another structural marker
            if re.search(r"^Contents?\s*$", sib_text, re.IGNORECASE):
                break
            if re.search(r"^(Purpose|Background|Financial impact|Key issues)", sib_text):
                # Also stop at next major section heading
                break
            if sib_text:
                chunks.append(sib_text)
        elif str(sibling).strip():
            chunks.append(str(sibling).strip())

    if chunks:
        text = " ".join(chunks)
        log(f"  Found Bills Digest key points ({len(text.split())} words)")
        return text

    # Fallback: grab a broad section of the digest body
    body_div = soup.find("div", class_=re.compile(r"(content|body|main)", re.I))
    if body_div:
        text = body_div.get_text(separator=" ", strip=True)
        # Truncate to ~500 words
        words = text.split()[:500]
        return " ".join(words)

    return ""


# ---------------------------------------------------------------------------
# Orchestration: get EM summary for one amending Act
# ---------------------------------------------------------------------------
def get_em_summary_for_act(amending_act: dict) -> dict:
    """
    Full pipeline for one amending Act:
      1. Find the ParlInfo bill home URL.
      2. Scrape the summary.
      3. If summary < MIN_SUMMARY_WORDS, fall back to Bills Digest.

    Returns a result dict.
    """
    tid = amending_act["titleId"]
    name = amending_act.get("name", tid)

    result = {
        "titleId": tid,
        "name": name,
        "affect": amending_act.get("affect", ""),
        "parlinfo_url": None,
        "bill_id": None,
        "bill_title": "",
        "summary": "",
        "summary_source": "",
        "status": "not_found",
    }

    # --- Step A: Discover ParlInfo URL ---
    parlinfo_url = None

    # Try 1: scrape legislation.gov.au page for the amending Act
    parlinfo_url = find_parlinfo_url_from_legislation_page(tid)

    # Try 2: check the as-made API response
    if not parlinfo_url:
        try:
            asmade_data = get_asmade_version(tid)
            parlinfo_url = find_parlinfo_url_from_asmade_api(asmade_data)
        except Exception as exc:
            log(f"  As-made API call failed for {tid}: {exc}")

    if not parlinfo_url:
        log(f"  Could not find ParlInfo URL for {tid}")
        result["status"] = "no_parlinfo_url"
        return result

    result["parlinfo_url"] = parlinfo_url

    # Extract bill ID for Bills Digest fallback
    bill_id = extract_bill_id_from_parlinfo_url(parlinfo_url)
    result["bill_id"] = bill_id

    # --- Step B: Scrape ParlInfo summary ---
    try:
        bill_title, summary = scrape_parlinfo_summary(parlinfo_url)
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

    # --- Step C: Summary too short — try Bills Digest ---
    log(f"  Summary < {MIN_SUMMARY_WORDS} words — falling back to Bills Digest …")

    digest_text = ""
    if bill_id:
        digest_text = scrape_bills_digest(bill_id)

    if digest_text and len(digest_text.split()) >= 30:
        result["summary"] = digest_text
        result["summary_source"] = "bills_digest"
        result["status"] = "success"
        return result

    # Keep whatever we have if it's non-empty
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
def wrap(text: str, width: int = 80, indent: str = "  ") -> str:
    """Word-wrap a string for readable terminal/log output."""
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def generate_report(
    principal_title_id: str,
    compilation_label: str,
    amending_acts: list[dict],
    results: list[dict],
) -> str:
    """Build a plain-English markdown report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# EM Summary Report",
        f"",
        f"**Principal Act:** [{principal_title_id}](https://www.legislation.gov.au/{principal_title_id}/latest/versions)  ",
        f"**Compilation:** {compilation_label}  ",
        f"**Generated:** {now}  ",
        f"",
        "---",
        "",
    ]

    if not results:
        lines.append("No amending Acts found for this compilation.")
        return "\n".join(lines)

    success_count = sum(1 for r in results if r["status"].startswith("success"))
    lines.append(
        f"Found **{len(results)}** amending Act(s). "
        f"Successfully retrieved summaries for **{success_count}**."
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
        lines.append(f"- **FRL ID:** [{tid}](https://www.legislation.gov.au/{tid}/latest/versions)")

        if bill_title and bill_title != name:
            lines.append(f"- **Bill:** {bill_title}")

        if parlinfo_url:
            lines.append(f"- **ParlInfo:** [{parlinfo_url}]({parlinfo_url})")

        if res.get("bill_id"):
            lines.append(f"- **Bill ID:** {res['bill_id']}")

        source_label = {
            "parlinfo_bill_home": "ParlInfo Bill Home – Summary",
            "parlinfo_bill_home_short": "ParlInfo Bill Home – Summary (short)",
            "bills_digest": "Bills Digest – Key Points",
        }.get(source, source or "—")
        lines.append(f"- **Summary source:** {source_label}")
        lines.append("")

        if summary:
            lines.append("### Plain-English Summary")
            lines.append("")
            # Wrap for readability (GitHub renders markdown so wrapping is fine)
            lines.append(summary)
        elif status == "no_parlinfo_url":
            lines.append(
                "> ⚠️ Could not locate a ParlInfo bill home link for this Act. "
                "It may be a non-parliamentary amendment (e.g. commencement instrument) "
                "or the link was not discoverable via the legislation.gov.au page."
            )
        elif status == "scrape_error":
            lines.append(
                "> ⚠️ Found the ParlInfo URL but could not scrape summary content. "
                "The page may require authentication or have an unusual structure."
            )
        else:
            lines.append(
                "> ⚠️ No summary text could be extracted for this Act. "
                "Check the ParlInfo URL above manually."
            )

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def write_step_summary(report_md: str) -> None:
    """Write report to GitHub Actions Step Summary if available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(report_md)
    log("GitHub Step Summary written.")


def write_output_file(
    report_md: str,
    title_id: str,
    compilation_label: str,
) -> str:
    """Write the report to a file and return the path."""
    from pathlib import Path

    out_dir = Path("em_summaries") / title_id
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"EM_summary_{title_id}_{compilation_label}.md"
    out_path = out_dir / filename
    out_path.write_text(report_md, encoding="utf-8")
    log(f"Report written → {out_path}")
    return str(out_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: python fetch_em_summary.py <legislation_url> <compilation_number>\n"
            "Example: python fetch_em_summary.py "
            "https://www.legislation.gov.au/C2015A00040/latest/versions C09"
        )
        sys.exit(1)

    url_input = sys.argv[1].strip()
    comp_input = sys.argv[2].strip().upper()

    log_section("FRL EM Summary Fetcher")
    log(f"Input URL        : {url_input}")
    log(f"Compilation      : {comp_input}")

    # --- Step 1: Extract Title ID ---
    try:
        title_id = extract_title_id(url_input)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    log(f"Title ID         : {title_id}")

    # --- Step 2: Fetch compilation from FRL API ---
    log_section("Fetching compilation from FRL API")
    try:
        version_data = get_compilation(title_id, comp_input)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)

    register_id = version_data.get("registerId", "unknown")
    log(f"Register ID      : {register_id}")

    # --- Step 3: Extract amending Acts ---
    log_section("Extracting amending Acts")
    amending_acts = extract_amending_acts(version_data)

    if not amending_acts:
        log("No amending Acts found in this compilation's reasons array.")
        log("This may be the as-made version, or no Acts amended this compilation.")
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
        log(f"  • {act['titleId']}  ({act['affect']})  {act.get('name', '')}")

    # --- Step 4: Retrieve EM summaries ---
    log_section("Retrieving EM summaries from ParlInfo")
    results = []
    for act in amending_acts:
        log(f"\nProcessing {act['titleId']} — {act.get('name', '')} …")
        result = get_em_summary_for_act(act)
        results.append(result)

    # --- Step 5: Generate and write report ---
    log_section("Generating report")
    report_md = generate_report(title_id, comp_input, amending_acts, results)

    # Print to stdout
    print("\n" + "=" * 60)
    print(report_md)
    print("=" * 60)

    # Write to file and GitHub Step Summary
    out_path = write_output_file(report_md, title_id, comp_input)
    write_step_summary(report_md)

    # --- Step 6: Exit status ---
    success_count = sum(1 for r in results if r["status"].startswith("success"))
    log_section("Complete")
    log(f"{success_count}/{len(results)} summaries retrieved.")
    log(f"Report saved to: {out_path}")

    if success_count == 0:
        log("WARNING: No summaries could be retrieved. Exiting with code 1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
