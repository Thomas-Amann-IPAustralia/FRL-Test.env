import os
import sys
import re
import requests

BASE_URL = "https://api.prod.legislation.gov.au/v1"

def extract_title_id(url):
    # Regex to pull the Title ID (e.g., F1996B00084) from the URL
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
    clean_comp = comp_number.lstrip('C')
    title_id = extract_title_id(url_input)

    print(f"Targeting Title: {title_id} | Compilation: {comp_number}")

    # --- UPDATED URL ---
    # Added ?$expand=reasons($expand=affect) to force the API to send full objects
    # Ref: GET /v1/Versions (with $expand=Reasons)
    version_url = (
        f"{BASE_URL}/Versions/Find(titleId='{title_id}',compilationNumber='{clean_comp}')"
        f"?$expand=reasons($expand=affect)"
    )
    
    print(f"Searching for compilation details at: {version_url}")
    response = requests.get(version_url)
    
    if response.status_code != 200:
        print(f"Error fetching version: {response.status_code}")
        return

    version_data = response.json()
    reasons = version_data.get('reasons', [])

    amending_ids = set()
    for reason in reasons:
        # Check 'amendedByTitle' first, which is often a direct ID string
        direct_id = reason.get('amendedByTitle')
        if isinstance(direct_id, str):
            amending_ids.add(direct_id)
        
        # Then check the 'affect' object
        affect = reason.get('affect')
        # DEFENSIVE CHECK: Only call .get() if affect is actually a dictionary
        if isinstance(affect, dict):
            amending_id = affect.get('affectingTitleId')
            if amending_id:
                amending_ids.add(amending_id)
        elif isinstance(affect, str) and len(affect) == 11:
            # If 'affect' came back as a string ID instead of an object
            amending_ids.add(affect)

    if not amending_ids:
        print("No amending Title IDs found in version reasons.")
        return

    os.makedirs("downloads", exist_ok=True)

    for amd_id in amending_ids:
        print(f"Fetching ES for amending title: {amd_id}...")
        
        # Ref: GET /v1/documents/find with type='ES'
        doc_find_url = (
            f"{BASE_URL}/documents/find("
            f"titleid='{amd_id}',"
            f"asatspecification='AsMade',"
            f"type='ES',"
            f"format='Word')"
        )
        
        doc_resp = requests.get(doc_find_url)
        
        if doc_resp.status_code == 200:
            filename = f"downloads/ES_{amd_id}_for_{comp_number}.docx"
            with open(filename, "wb") as f:
                f.write(doc_resp.content)
            print(f"Successfully saved: {filename}")
        else:
            print(f"Could not find Word ES for {amd_id} (Status: {doc_resp.status_code})")

if __name__ == "__main__":
    download_es()
