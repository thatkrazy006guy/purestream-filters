# Store the last scraped title globally so we can block the TV from acting on old data
last_scraped_title = ""

# Add live or weekly broadcast titles that Netflix permanently pins in "Continue Watching"
LIVE_EXCLUSIONS = [
    "wwe raw", 
    "wwe smackdown", 
    "wwe nxt", 
    "wwe", 
    "netflix live", 
    "live from"
]

@app.get("/api/netflix-sync")
def netflix_sync():
    global last_scraped_title
    import os
    import requests
    import re
    from bs4 import BeautifulSoup

    cookie_val = os.environ.get("NETFLIX_RAW_COOKIE") or os.environ.get("SECURE_NETFLIX_ID")
    
    if not cookie_val:
        return {"status": "error", "message": "No cookie found in Render. Set NETFLIX_RAW_COOKIE."}

    cookie_header = f"SecureNetflixId={cookie_val}" if "=" not in cookie_val else cookie_val

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie_header
    }

    latest_title = None

    # METHOD 1: Check the Official Viewing Activity Ledger
    try:
        url_activity = "https://www.netflix.com/viewingactivity"
        response = requests.get(url_activity, headers=headers, allow_redirects=False)
        
        if response.status_code in [301, 302] and 'login' in response.headers.get('Location', ''):
            return {"status": "error", "message": "Cookie expired. Grab a fresh cookie from Chrome."}

        soup = BeautifulSoup(response.text, 'html.parser')
        first_title_tag = soup.select_one('.col.title a')

        if first_title_tag:
            activity_title = first_title_tag.text.strip()
            # Ignore live show exclusions from activity ledger as well
            if not any(ex in activity_title.lower() for ex in LIVE_EXCLUSIONS):
                latest_title = activity_title
    except Exception as e:
        print(f"Ledger error: {e}")

    # METHOD 2: The "Continue Watching" Fallback (with Exclusion Filter)
    if latest_title == last_scraped_title or not latest_title:
        try:
            url_browse = "https://www.netflix.com/browse"
            res_browse = requests.get(url_browse, headers=headers)
            
            # Isolate the continueWatching context array block
            cw_chunk_match = re.search(r'"listContext":"continueWatching".*?\]', res_browse.text)
            if cw_chunk_match:
                cw_chunk = cw_chunk_match.group(0)
                # Extract all titles in order from the array
                raw_titles = re.findall(r'"title":"(.*?)"', cw_chunk)
                
                for raw_t in raw_titles:
                    cw_title = raw_t.encode('utf-8').decode('unicode_escape').strip()
                    
                    # Check if title matches any pinned live show keyword
                    if any(exclusion in cw_title.lower() for exclusion in LIVE_EXCLUSIONS):
                        print(f"Skipping pinned live show: {cw_title}")
                        continue
                    
                    if cw_title and cw_title != last_scraped_title:
                        print(f"🔥 RESCUED FROM CONTINUE WATCHING: {cw_title}")
                        latest_title = cw_title
                        break
        except Exception as e:
            print(f"Continue Watching fallback failed: {e}")

    # STATEFUL LOCK: Reject the request if Netflix still hasn't updated either source
    if latest_title and latest_title != last_scraped_title:
        print(f"New Netflix Title Detected: {latest_title}")
        last_scraped_title = latest_title
        return {"status": "success", "title": latest_title}
    else:
        return {"status": "pending", "message": "Netflix has not updated yet. TV will poll again in 20s..."}
