"""
download_es.py
--------------
Downloads the Explanatory Statement (ES) Word document for every amending
instrument associated with a specific compilation of a Federal Register of
Legislation (FRL) title.


Usage:
    python download_es.py <legislation_url> <compilation_number>


Examples:
    python download_es.py https://www.legislation.gov.au/F1996B00084/latest/text C51


The script:
  1. Extracts the Title ID from the supplied legislation URL.
  2. Calls the FRL API Versions/Find() endpoint to retrieve the compilation,
     including the embedded 'reasons' array that lists every amending instrument.
  3. For each amending instrument, attempts to download its ES in Word format.
     If no ES is found it falls back to SupplementaryES before giving up.
  4. Saves each file to downloads/<titleId>/<compilationNumber>/ and writes a
     plain-text manifest of every outcome to the same folder.
  5. Exits with code 1 if the compilation cannot be found, or if zero files
     were successfully downloaded (so the CI step fails visibly).
"""


import os
import re
import sys
import json
import textwrap
from pathlib import Path
from datetime import datetime, timezone


import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://api.prod.legislation.gov.au/v1"


# Document types to try, in priority order
ES_TYPE_PRIORITY = ["ES", "SupplementaryES"]


# Regex that matches the 11-character series identifier embedded in any
# legislation.gov.au URL, e.g. F1996B00084, C2004A01224, F2024L01299
TITLE_ID_RE = re.compile(r"\b([A-Z][0-9]{4}[A-Z][0-9]{5,6})\b")




# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Print a timestamped log line and flush immediately (important in CI)."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)




def extract_title_id(url: str) -> str:
    """
    Extract the 11-character FRL Title ID from a legislation.gov.au URL.


    Raises ValueError if nothing matching is found.
    """
    match = TITLE_ID_RE.search(url)
    if not match:
        raise ValueError(
            f"Could not extract a Title ID from URL: {url}\n"
            "Expected a URL like https://www.legislation.gov.au/F1996B00084/latest/text"
        )
    return match.group(1)




def get_compilation(title_id: str, compilation_number: str) -> dict:
    """
    Call the Versions/Find() endpoint for the given title ID and compilation
    number.  Returns the parsed JSON response dict.


    Raises RuntimeError on non-200 response.
    """
    url = (
        f"{BASE_URL}/Versions/Find("
        f"titleId='{title_id}',"
        f"compilationNumber='{compilation_number}')"
    )
    log(f"GET {url}")
    resp = requests.get(url, timeout=30)


    if resp.status_code == 404:
        raise RuntimeError(
            f"Compilation '{compilation_number}' not found for title '{title_id}'.\n"
            "Check that the compilation number exists (e.g. C51 → pass '51', not 'C51')."
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Unexpected API response {resp.status_code} when fetching compilation.\n"
            f"Body: {resp.text[:500]}"
        )


    return resp.json()




def extract_amending_ids(version_data: dict) -> list[dict]:
    """
    Walk the 'reasons' array of a Version object and collect every unique
    amending Title ID.


    The API schema defines ReasonForVersion.amendedByTitle as an
    AffectedByTitle object with a 'titleId' string property — NOT a raw
    string.  We also check affectedByTitle as a secondary source.


    Returns a list of dicts:
        [{"titleId": "F2024L01299", "name": "...", "affect": "Amend"}, ...]
    """
    seen: set[str] = set()
    results: list[dict] = []


    reasons = version_data.get("reasons", [])
    if not reasons:
        log("  Warning: 'reasons' array is empty or absent in the version response.")


    for reason in reasons:
        affect_type = reason.get("affect", "Unknown")


        # Primary source: amendedByTitle.titleId
        amended_by = reason.get("amendedByTitle") or {}
        tid = amended_by.get("titleId") if isinstance(amended_by, dict) else None


        # Fallback: affectedByTitle.titleId
        if not tid:
            affected_by = reason.get("affectedByTitle") or {}
            tid = affected_by.get("titleId") if isinstance(affected_by, dict) else None


        if tid and tid not in seen:
            seen.add(tid)
            name = (amended_by or {}).get("name") or ""
            results.append({"titleId": tid, "name": name, "affect": affect_type})


    return results




def get_asmade_date(amd_id: str) -> str | None:
    """
    Fetch the as-made registration date for an amending instrument by calling
    Versions/Find(titleId=..., asAtSpecification='AsMade').


    Returns the date portion of the 'start' field (yyyy-mm-dd), or None on failure.
    The website's direct download URL requires this date segment.
    """
    url = (
        f"{BASE_URL}/Versions/Find("
        f"titleId='{amd_id}',"
        f"asAtSpecification='AsMade')"
    )
    log(f"  Fetching as-made date → GET {url}")
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            start = data.get("start", "")
            # 'start' is datetime format: "2024-10-14T00:00:00" — take date part only
            date_part = start[:10] if start else None
            if date_part:
                log(f"  As-made date: {date_part}")
            return date_part
    except Exception as exc:
        log(f"  Could not fetch as-made date for {amd_id}: {exc}")
    return None




def download_via_web_url(
    amd_id: str,
    asmade_date: str,
    compilation_label: str,
    out_dir: Path,
    result: dict,
) -> dict:
    """
    Fallback: download the ES Word document directly from the legislation.gov.au
    website using the known URL pattern:


        https://www.legislation.gov.au/{titleId}/asmade/{date}/es/original/word


    This mirrors what the Downloads tab on the website serves and works even when
    the API's documents/find() endpoint returns 404 (e.g. for instruments whose ES
    was lodged as a direct file rather than through the standard API pathway).


    Also tries SupplementaryES variant if ES returns 404.
    """
    WEB_BASE = "https://www.legislation.gov.au"
    es_paths = [
        ("ES",              f"{WEB_BASE}/{amd_id}/asmade/{asmade_date}/es/original/word"),
        ("SupplementaryES", f"{WEB_BASE}/{amd_id}/asmade/{asmade_date}/supplementaryes/original/word"),
    ]


    for doc_type, web_url in es_paths:
        log(f"  Web fallback [{doc_type}] → GET {web_url}")
        try:
            resp = requests.get(web_url, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            log(f"  ✗ Web fallback network error: {exc}")
            continue


        result["http_status"] = resp.status_code


        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            # Reject HTML error pages masquerading as 200
            if "html" in content_type and len(resp.content) < 50_000:
                log(f"  ✗ Web fallback returned HTML (likely error page), skipping.")
                continue


            filename = f"ES_{amd_id}_{compilation_label}.docx"
            filepath = out_dir / filename
            filepath.write_bytes(resp.content)


            result["status"] = "success"
            result["type"] = f"{doc_type} (web)"
            result["filename"] = filename
            result["source"] = "web"
            size_kb = len(resp.content) / 1024
            log(f"  ✓ {doc_type} (web) saved → {filepath} ({size_kb:.1f} KB)")
            return result


        elif resp.status_code == 404:
            log(f"  ✗ Web fallback {doc_type}: 404 Not Found.")
        else:
            log(f"  ✗ Web fallback {doc_type}: status {resp.status_code}.")


    return result




def download_es_for_title(
    amd_id: str,
    compilation_label: str,
    out_dir: Path,
) -> dict:
    """
    Attempt to download an ES Word document for *amd_id*.


    Strategy:
      1. Try the FRL API documents/find() endpoint (ES, then SupplementaryES).
      2. If both return 404, fetch the instrument's as-made date via Versions/Find()
         and attempt the direct web download URL used by the legislation.gov.au
         Downloads tab.


    Returns a result dict:
        {
            "titleId": str,
            "status": "success" | "not_found" | "error",
            "type": str | None,
            "source": "api" | "web" | None,
            "filename": str | None,
            "http_status": int | None,
            "error": str | None,
        }
    """
    result = {
        "titleId": amd_id,
        "status": "not_found",
        "type": None,
        "source": None,
        "filename": None,
        "http_status": None,
        "error": None,
    }


    # --- Pass 1: API endpoint ---
    api_404_count = 0
    for doc_type in ES_TYPE_PRIORITY:
        find_url = (
            f"{BASE_URL}/documents/find("
            f"titleid='{amd_id}',"
            f"asatspecification='AsMade',"
            f"type='{doc_type}',"
            f"format='Word')"
        )
        log(f"  API [{doc_type}] → GET {find_url}")


        try:
            resp = requests.get(find_url, timeout=60)
        except requests.RequestException as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            log(f"  ✗ Network error for {amd_id}: {exc}")
            return result


        result["http_status"] = resp.status_code


        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            if "json" in content_type:
                log(f"  ✗ API {doc_type}: returned JSON metadata (no file).")
                api_404_count += 1
                continue


            filename = f"ES_{amd_id}_{compilation_label}.docx"
            filepath = out_dir / filename
            filepath.write_bytes(resp.content)


            result["status"] = "success"
            result["type"] = doc_type
            result["source"] = "api"
            result["filename"] = filename
            size_kb = len(resp.content) / 1024
            log(f"  ✓ API {doc_type} saved → {filepath} ({size_kb:.1f} KB)")
            return result


        elif resp.status_code == 404:
            log(f"  ✗ API {doc_type}: 404 Not Found.")
            api_404_count += 1
        else:
            log(f"  ✗ API {doc_type}: status {resp.status_code}.")


    # --- Pass 2: Web URL fallback (triggered when all API attempts returned 404) ---
    if api_404_count == len(ES_TYPE_PRIORITY):
        log(f"  All API attempts 404'd — trying direct web download fallback …")
        asmade_date = get_asmade_date(amd_id)
        if asmade_date:
            result = download_via_web_url(
                amd_id, asmade_date, compilation_label, out_dir, result
            )
        else:
            log(f"  Could not determine as-made date; web fallback skipped.")


    if result["status"] != "success":
        log(f"  — No ES document found for {amd_id} via any method.")


    return result




def write_manifest(
    out_dir: Path,
    title_id: str,
    compilation_label: str,
    amending_docs: list[dict],
    download_results: list[dict],
) -> None:
    """Write a human-readable manifest.txt and a machine-readable manifest.json."""
    # Index results by titleId for easy lookup
    results_by_id = {r["titleId"]: r for r in download_results}


    lines = [
        "ES Download Manifest",
        "=" * 60,
        f"Principal Title ID : {title_id}",
        f"Compilation        : {compilation_label}",
        f"Run at (UTC)       : {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Amending instruments found : {len(amending_docs)}",
        f"Documents downloaded       : {sum(1 for r in download_results if r['status'] == 'success')}",
        f"Not found                  : {sum(1 for r in download_results if r['status'] == 'not_found')}",
        f"Errors                     : {sum(1 for r in download_results if r['status'] == 'error')}",
        "",
        "-" * 60,
    ]


    for doc in amending_docs:
        tid = doc["titleId"]
        res = results_by_id.get(tid, {})
        status_icon = {"success": "✓", "not_found": "✗", "error": "!"}.get(
            res.get("status", ""), "?"
        )
        source = res.get("source") or ""
        lines.append(
            f"{status_icon}  {tid:<14}  affect={doc['affect']:<12}  "
            f"status={res.get('status','unknown'):<10}  "
            f"source={source:<4}  file={res.get('filename') or '—'}"
        )
        if doc.get("name"):
            lines.append(f"   name: {doc['name']}")


    manifest_txt = out_dir / "manifest.txt"
    manifest_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Manifest written → {manifest_txt}")


    manifest_json = out_dir / "manifest.json"
    manifest_json.write_text(
        json.dumps(
            {
                "titleId": title_id,
                "compilation": compilation_label,
                "amendingInstruments": amending_docs,
                "downloads": download_results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"JSON manifest written → {manifest_json}")




def write_step_summary(
    title_id: str,
    compilation_label: str,
    amending_docs: list[dict],
    download_results: list[dict],
    out_dir: Path,
) -> None:
    """
    Write a GitHub Actions Step Summary (Markdown) if the GITHUB_STEP_SUMMARY
    environment variable is set.  Silently skips if running locally.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return


    results_by_id = {r["titleId"]: r for r in download_results}
    success_count = sum(1 for r in download_results if r["status"] == "success")


    rows = []
    for doc in amending_docs:
        tid = doc["titleId"]
        res = results_by_id.get(tid, {})
        status = res.get("status", "unknown")
        icon = {"success": "✅", "not_found": "❌", "error": "⚠️"}.get(status, "❓")
        frl_link = f"[{tid}](https://www.legislation.gov.au/{tid}/latest/text)"
        rows.append(
            f"| {icon} | {frl_link} | {doc.get('name', '—')} "
            f"| {doc.get('affect', '—')} | {res.get('filename') or '—'} |"
        )


    summary = textwrap.dedent(f"""\
        ## ES Download — {title_id} / {compilation_label}


        | | Amending Instrument | Name | Affect | Output File |
        |---|---|---|---|---|
        {chr(10).join(rows)}


        **{success_count} of {len(amending_docs)} documents downloaded** → `{out_dir}/`
    """)


    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(summary)
    log("GitHub Step Summary written.")




# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: python download_es.py <legislation_url> <compilation_number>\n"
            "Example: python download_es.py "
            "https://www.legislation.gov.au/F1996B00084/latest/text C51"
        )
        sys.exit(1)


    url_input = sys.argv[1].strip()
    comp_input = sys.argv[2].strip()


    # Normalise compilation number: strip leading 'C' (C51 → 51)
    comp_number = re.sub(r"^[Cc]", "", comp_input).strip()
    # Keep original label for filenames/display
    comp_label = comp_input.upper()


    log("=" * 60)
    log("FRL Explanatory Statement Downloader")
    log("=" * 60)
    log(f"Input URL        : {url_input}")
    log(f"Compilation input: {comp_input}  (normalised → '{comp_number}')")


    # Step 1 — Extract Title ID
    try:
        title_id = extract_title_id(url_input)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    log(f"Title ID         : {title_id}")


    # Step 2 — Fetch the compilation from the API
    log("")
    log("Fetching compilation details from FRL API …")
    try:
        version_data = get_compilation(title_id, comp_number)
    except RuntimeError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)


    register_id = version_data.get("registerId", "unknown")
    log(f"Register ID      : {register_id}")


    # Step 3 — Extract amending instrument IDs from the 'reasons' array
    log("")
    log("Extracting amending instruments …")
    amending_docs = extract_amending_ids(version_data)


    if not amending_docs:
        log(
            "No amending instruments found in this compilation's 'reasons' array.\n"
            "This may be the as-made version (C0/compilation 0) or the API did not\n"
            "return reasons for this title."
        )
        sys.exit(0)


    log(f"Found {len(amending_docs)} amending instrument(s):")
    for doc in amending_docs:
        log(f"  • {doc['titleId']}  ({doc['affect']})  {doc.get('name', '')}")


    # Step 4 — Set up output directory
    out_dir = Path("downloads") / title_id / comp_label
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"\nOutput directory : {out_dir}")


    # Step 5 — Download ES for each amending instrument
    log("")
    log("Downloading Explanatory Statements …")
    log("-" * 60)


    download_results: list[dict] = []
    for doc in amending_docs:
        log(f"Processing {doc['titleId']} …")
        result = download_es_for_title(doc["titleId"], comp_label, out_dir)
        download_results.append(result)


    # Step 6 — Write manifests and summary
    log("")
    log("Writing manifests …")
    write_manifest(out_dir, title_id, comp_label, amending_docs, download_results)
    write_step_summary(title_id, comp_label, amending_docs, download_results, out_dir)


    # Step 7 — Final status
    success_count = sum(1 for r in download_results if r["status"] == "success")
    log("")
    log("=" * 60)
    log(f"Complete: {success_count}/{len(amending_docs)} ES documents downloaded.")
    log("=" * 60)


    if success_count == 0:
        log("WARNING: No documents were downloaded. Exiting with code 1.")
        sys.exit(1)




if __name__ == "__main__":
    main()
