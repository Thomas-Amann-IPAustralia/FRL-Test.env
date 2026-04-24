import os
import sys
import re
import requests

BASE_URL = "https://api.prod.legislation.gov.au/v1"

def extract_title_id(url):
    match = re.search(r'/(?P<id>[A-Z0-9]{11})', url)
    if not match:
        raise ValueError(f"Could not extract Title ID from URL: {url}")
    return match.group('id')

def download_es():
    if len(sys.argv) < 3:
        print("Usage: python download_es.py <url> <compilation_number>")
        return

    url_input = sys.argv[1]
    comp_number = sys.argv[2]
    clean_comp = comp_number.lstrip('C') # Strip 'C' to get just the number string
    title_id = extract_title_id(url_input)

    print(f"--- Diagnostic Start ---")
    print(f"Targeting Title: {title_id}")
    print(f"Targeting Compilation: {clean_comp}")

    # FIX: Instead of Versions/Find(...), we query the Versions collection with a $filter.
    # This is the standard OData way to allow for $expand functionality.
    version_query_url = (
        f"{BASE_URL}/Versions?"
        f"$filter=titleId eq '{title_id}' and compilationNumber eq '{clean_comp}'"
        f"&$expand=reasons($expand=affect)"
    )
    
    print(f"Querying: {version_query_url}")
    response = requests.get(version_query_url)
    
    if response.status_code != 200:
        print(f"CRITICAL: API query failed. Status: {response.status_code}")
        print(f"Response: {response.text}")
        return

    # Data from collection queries is returned inside a 'value' array.
    results = response.json().get('value', [])
    if not results:
        print(f"ERROR: Compilation {clean_comp} not found for title {title_id}.")
        return

    # Take the first matching version
    version_data = results[0]
    reasons = version_data.get('reasons', [])
    print(f"Found {len(reasons)} reason entries.")

    amending_ids = set()
    for reason in reasons:
        # Check 'amendedByTitle' - often a direct ID string
        direct_id = reason.get('amendedByTitle')
        if direct_id and isinstance(direct_id, str):
            amending_ids.add(direct_id)
        
        # Check the 'affect' object which we expanded
        affect = reason.get('affect')
        if isinstance(affect, dict):
            aid = affect.get('affectingTitleId')
            if aid:
                amending_ids.add(aid)
        elif isinstance(affect, str) and len(affect) == 11:
            # Fallback if the API returns just the ID string instead of an object
            amending_ids.add(affect)

    print(f"Unique Amending IDs to check: {amending_ids}")

    if not amending_ids:
        print("No amending Title IDs found.")
        return

    os.makedirs("downloads", exist_ok=True)

    for amd_id in amending_ids:
        # Requesting the ES in Word format
        doc_find_url = (
            f"{BASE_URL}/documents/find("
            f"titleid='{amd_id}',"
            f"asatspecification='AsMade',"
            f"type='ES',"
            f"format='Word')"
        )
        
        print(f"Attempting download for {amd_id}...")
        doc_resp = requests.get(doc_find_url)
        
        if doc_resp.status_code == 200:
            filename = f"downloads/ES_{amd_id}_for_{comp_number}.docx"
            with open(filename, "wb") as f:
                f.write(doc_resp.content)
            print(f"SUCCESS: Saved {filename}")
        else:
            print(f"FAILED: No Word ES for {amd_id} (Status {doc_resp.status_code}).")

if __name__ == "__main__":
    download_es()
