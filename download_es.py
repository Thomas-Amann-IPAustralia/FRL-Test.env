import os      # Standard library for creating folders and handling file paths.
import sys     # Used to grab command-line arguments (URL and Compilation Number).
import re      # Regular expressions to pick out specific text patterns from strings.
import requests # The "engine" that sends requests to the FRL API and gets data back.

# This is the root address for all API calls as defined in the index.
# We use 'v1' as it is the current version for public read operations.
BASE_URL = "https://api.prod.legislation.gov.au/v1"

def extract_title_id(url):
    """
    Legislation URLs contain a unique 11-character ID (e.g., F1996B00084).
    This function uses a Regex pattern to find and return that ID.
    """
    # Look for a pattern of uppercase letters and numbers that is 11 chars long.
    # This matches common patterns like 'C2004A01224' or 'F2021L00100'.
    match = re.search(r'/(?P<id>[A-Z0-9]{11})', url)
    if not match:
        # If the user provides a broken URL, we stop the script early with a clear error.
        raise ValueError(f"Could not extract Title ID from URL: {url}")
    return match.group('id')

def download_es():
    # Verify the user actually provided the 2 required inputs in the GitHub UI.
    if len(sys.argv) < 3:
        print("Usage: python download_es.py <url> <compilation_number>")
        return

    # Assign inputs to easy-to-read variables.
    url_input = sys.argv[1]
    comp_number = sys.argv[2] # Example: 'C51'
    
    # The API expects 'compilationNumber' as an incrementing string (e.g., '51').
    # We strip the 'C' prefix just in case the API search is strict about numeric-only strings.
    clean_comp = comp_number.lstrip('C')
    
    # Step 0: Get the ID from the URL (e.g., 'F1996B00084').
    title_id = extract_title_id(url_input)

    print(f"Targeting Title: {title_id} | Compilation: {comp_number}")

    # --- STEP 1: Find the Compilation Version ---
    # We need to look at a specific 'Version' to see the 'reasons' it was created.
    # These 'reasons' link back to the amending documents that changed the law.
    # Endpoint used: GET /v1/Versions/Find.
    version_url = f"{BASE_URL}/Versions/Find(titleId='{title_id}',compilationNumber='{clean_comp}')"
    
    print(f"Searching for compilation details at: {version_url}")
    response = requests.get(version_url)
    
    if response.status_code != 200:
        print(f"Error: Could not find compilation {comp_number}. API said: {response.status_code}")
        return

    # Convert the JSON response into a Python Dictionary.
    version_data = response.json()
    
    # The 'reasons' field contains the 'ReasonForVersion' objects.
    reasons = version_data.get('reasons', [])

    if not reasons:
        print("No amending reasons/documents found for this specific compilation.")
        return

    # --- STEP 2: Identify Amending Titles ---
    # We iterate through every 'reason' to find 'affectingTitleId'.
    # This ID tells us which new Law or Instrument caused the change.
    amending_ids = set() # Use a 'set' to avoid downloading the same file twice.
    for reason in reasons:
        # Each reason has an 'affect' object which describes the relationship.
        affect = reason.get('affect', {})
        amending_id = affect.get('affectingTitleId')
        if amending_id:
            amending_ids.add(amending_id)

    if not amending_ids:
        print("No amending Title IDs found in the version reasons.")
        return

    # Create a 'downloads' folder in the GitHub workspace if it doesn't exist.
    os.makedirs("downloads", exist_ok=True)

    # --- STEP 3: Fetch the Explanatory Statement (ES) ---
    # Now we loop through every amending document we found.
    for amd_id in amending_ids:
        print(f"Fetching ES for amending title: {amd_id}...")
        
        # We use the /documents/find endpoint with specific filters.
        # type='ES': We want the Explanatory Statement.
        # format='Word': We want the .DOCX version for easy editing.
        # asAtSpecification='AsMade': Amending documents are usually retrieved in their original 'AsMade' state.
        doc_find_url = (
            f"{BASE_URL}/documents/find("
            f"titleid='{amd_id}',"
            f"asatspecification='AsMade',"
            f"type='ES',"
            f"format='Word')"
        )
        
        doc_resp = requests.get(doc_find_url)
        
        # If the request is successful, the 'content' of the response is the actual binary file.
        if doc_resp.status_code == 200:
            # We save it using the amending ID and compilation number for clarity.
            filename = f"downloads/ES_{amd_id}_for_{comp_number}.docx"
            with open(filename, "wb") as f:
                f.write(doc_resp.content)
            print(f"Successfully saved: {filename}")
        else:
            # Some older documents might only have PDFs. The API will return 404 for Word in those cases.
            print(f"Could not find Word ES for {amd_id}. (It may only exist as a PDF).")

if __name__ == "__main__":
    # This ensures the script only runs if called directly, not if imported.
    download_es()
