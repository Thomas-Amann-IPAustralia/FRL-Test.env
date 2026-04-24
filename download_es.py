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
    clean_comp = comp_number.lstrip('C')
    title_id = extract_title_id(url_input)

    print(f"--- Diagnostic Start ---")
    print(f"Targeting Title: {title_id}")
    print(f"Targeting Compilation: {clean_comp}")

    # Use $expand to get the 'affect' details
    version_url = (
        f"{BASE_URL}/Versions/Find(titleId='{title_id}',compilationNumber='{clean_comp}')"
        f"?$expand=reasons($expand=affect)"
    )
    
    response = requests.get(version_url)
    if response.status_code != 200:
        print(f"CRITICAL: API could not find this version. Status: {response.status_code}")
        return

    version_data = response.json()
    reasons = version_data.get('reasons', [])
    print(f"Found {len(reasons)} reason entries for this compilation.")

    amending_ids = set()
    for reason in reasons:
        # Checking multiple fields for the amending ID
        aid = reason.get('amendedByTitle')
        if aid:
            amending_ids.add(aid)
        
        affect = reason.get('affect')
        if isinstance(affect, dict):
            affect_id = affect.get('affectingTitleId')
            if affect_id:
                amending_ids.add(affect_id)

    print(f"Unique Amending IDs found: {amending_ids}")

    if not amending_ids:
        print("No amending Title IDs were identified from the reasons.")
        return

    os.makedirs("downloads", exist_ok=True)

    for amd_id in amending_ids:
        # We look for the ES in Word format
        # Note: If 'Word' fails, it might only exist as 'Pdf'
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
            print(f"FAILED: No Word ES found for {amd_id} (Status {doc_resp.status_code}).")
            print(f"TIP: This document might only have a PDF version on the register.")

if __name__ == "__main__":
    download_es()
