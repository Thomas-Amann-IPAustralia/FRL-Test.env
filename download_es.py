import os
import sys
import re
import requests

# Root API URL
BASE_URL = "https://api.prod.legislation.gov.au/v1"

def extract_title_id(url):
    """Extracts the 11-character Title ID from a legislation URL."""
    match = re.search(r'/(?P<id>[A-Z0-9]{11})', url)
    if not match:
        raise ValueError(f"Could not extract Title ID from URL: {url}")
    return match.group('id')

def download_es():
    if len(sys.argv) < 3:
        print("Usage: python download_es.py <url> <compilation_number>")
        return

    url_input = sys.argv[1]
    comp_input = sys.argv[2]
    
    # Standardize the compilation number (e.g., 'C51' becomes '51')
    clean_comp = comp_input.upper().lstrip('C').strip()
    title_id = extract_title_id(url_input)

    print(f"--- Processing Request ---")
    print(f"Title ID: {title_id} | Compilation: {clean_comp}")

    # Step 1: Get the version details using the specialized Find() function.
    # Note: We do NOT use $expand here because the server prohibits it.
    version_url = f"{BASE_URL}/Versions/Find(titleId='{title_id}',compilationNumber='{clean_comp}')"
    
    print(f"Fetching compilation details...")
    response = requests.get(version_url)
    
    if response.status_code != 200:
        print(f"CRITICAL: API could not retrieve version. Status: {response.status_code}")
        print(f"Check if Compilation '{clean_comp}' actually exists for this title.")
        return

    version_data = response.json()
    
    # The 'reasons' field is provided by default in the Find() response.
    reasons = version_data.get('reasons', [])
    print(f"Found {len(reasons)} amendment reasons.")

    amending_ids = set()
    for reason in reasons:
        # 'amendedByTitle' is the string field containing the ID of the amending law.
        aid = reason.get('amendedByTitle')
        if isinstance(aid, str) and len(aid) == 11:
            amending_ids.add(aid)
        
        # Fallback: Check if the 'affect' field itself is a string ID
        affect = reason.get('affect')
        if isinstance(affect, str) and len(affect) == 11:
            amending_ids.add(affect)

    print(f"Amending IDs to process: {amending_ids}")

    if not amending_ids:
        print("No amending IDs were identified. This compilation may not have amending documents.")
        return

    os.makedirs("downloads", exist_ok=True)

    # Step 2: Download the Explanatory Statement for each amending ID.
    for amd_id in amending_ids:
        # We fetch the ES in Word format as originally requested.
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
            filename = f"downloads/ES_{amd_id}_for_{comp_input}.docx"
            with open(filename, "wb") as f:
                f.write(doc_resp.content)
            print(f"SUCCESS: Saved {filename}")
        else:
            print(f"FAILED: No Word ES found for {amd_id} (Status {doc_resp.status_code}).")

if __name__ == "__main__":
    download_es()
