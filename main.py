import os
import tempfile
import json
import re
import requests
import pysubs2
import subprocess
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github, InputFileContent
from bs4 import BeautifulSoup

app = FastAPI()

# --- CLOUD CONFIGURATION ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
GIST_ID = os.getenv("GIST_ID", "26c5299781dfbc13015947ebff5a6e66")

# --- PLATFORM PRE-ROLL OFFSETS (in Milliseconds) ---
PLATFORM_OFFSETS = {
    "com.wbd.stream": 7000,                     # Max / HBO Max
    "com.hbo.hbonow": 7000,                     # Legacy HBO
    "com.hulu.livingroomplus": 4000,            # Hulu
    "com.disney.disneyplus": 5000,              # Disney+
    "com.amazon.amazonvideo.livingroom": 2000,  # Prime Video
    "com.netflix.ninja": 0                      # Netflix (Standard)
}

# --- TRIGGER LISTS ---
EXPLICIT_WORDS = [
    "fuck", "fucking", "fucked", "fucker", "motherfucker",
    "shit", "shitty", "bullshit", "bitch", "bitches", "bitching",
    "asshole", "assholes", "ass", "damn", "goddamn", "dammit",
    "cunt", "dick", "cock", "pussy", "whore", "slut", "bastard"
]
PROFANITY_REGEX = re.compile(r'\b(?:' + '|'.join(EXPLICIT_WORDS) + r')\b', re.IGNORECASE)

VISUAL_CUES = [
    "moan", "moaning", "moans", "panting", "pants",
    "breathing heavily", "heavy breathing", "grunting", "grunts",
    "bed springs", "squeaking", "flesh slapping", "slapping",
    "kissing", "smooching"
]
EXPLICIT_REGEX = re.compile(r'[\[\(].*?\b(?:' + '|'.join(VISUAL_CUES) + r')\b.*?[\]\)]', re.IGNORECASE)

BUFFER_MS = 500

class ReportRequest(BaseModel):
    tmdb_id: int = 0
    media_type: str = "movie"
    title: str
    season: int = None
    episode: int = None
    package_name: str = ""

def merge_zones(zones):
    if not zones: return []
    zones.sort(key=lambda x: x["startTimeMs"])
    merged = [zones[0]]
    for current in zones[1:]:
        prev = merged[-1]
        if current["startTimeMs"] <= prev["endTimeMs"]:
            prev["endTimeMs"] = max(prev["endTimeMs"], current["endTimeMs"])
        else:
            merged.append(current)
    return merged

def fetch_imdb_advisories(title, tmdb_api_key):
    """Scrapes exact Nudity/Drugs/Violence severities from IMDb Parents Guide."""
    if not tmdb_api_key: return {}
    try:
        # 1. Use TMDB to securely find the official IMDb ID
        clean_title = re.sub(r'\s*S\d+E\d+.*', '', title, flags=re.IGNORECASE).strip()
        tmdb_url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_api_key}&query={clean_title}"
        results = requests.get(tmdb_url).json().get("results", [])
        if not results: return {}
        
        media_item = results[0]
        media_id = media_item["id"]
        media_type = media_item.get("media_type", "movie")
        
        ext_url = f"https://api.themoviedb.org/3/{media_type}/{media_id}/external_ids?api_key={tmdb_api_key}"
        imdb_id = requests.get(ext_url).json().get("imdb_id")
        if not imdb_id: return {}
        
        # 2. Scrape the live IMDb HTML 
        pg_url = f"https://www.imdb.com/title/{imdb_id}/parentalguide"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(pg_url, headers=headers)
        
        advisories = {}
        categories = ["Sex & Nudity", "Violence & Gore", "Profanity", "Alcohol, Drugs & Smoking", "Frightening & Intense Scenes"]
        
        for cat in categories:
            # Dynamically scan the HTML for the community severity tags
            pattern = re.compile(fr'{re.escape(cat)}.{'{0,500}'}?(None|Mild|Moderate|Severe)', re.IGNORECASE | re.DOTALL)
            match = pattern.search(res.text)
            if match:
                advisories[cat] = match.group(1).title()
                
        return advisories
    except Exception as e:
        print(f"IMDb Scraper Error: {e}")
        return {}

@app.get("/")
def health_check():
    return {"status": "PureStream Universal Cloud Service Running"}

@app.post("/api/report-title")
def report_title(req: ReportRequest):
    if not GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN environment variable not set.")

    formatted_title = req.title
    if req.media_type == "tv" and req.season and req.episode:
        formatted_title = f"{req.title} S{req.season:02d}E{req.episode:02d}"

    # Determine studio logo delay for the active app
    platform_offset = PLATFORM_OFFSETS.get(req.package_name, 0)

    search_queries = [formatted_title]
    base_title = re.sub(r'\s*S\d+E\d+.*', '', formatted_title, flags=re.IGNORECASE).strip()
    if base_title != formatted_title and base_title not in search_queries:
        search_queries.append(base_title)

    if TMDB_API_KEY:
        try:
            res = requests.get("https://api.themoviedb.org/3/search/multi", params={"api_key": TMDB_API_KEY, "query": base_title}).json()
            for item in res.get("results", [])[:3]:
                official = item.get("title") or item.get("name")
                if official and official not in search_queries:
                    search_queries.append(official)
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as temp_dir:
        found_subs = []
        
        for query in search_queries:
            clean_query = re.sub(r'[\\/:*?"<>|]', '', query).strip()
            dummy_mp4 = os.path.join(temp_dir, f"{clean_query}.1080p.WEB-DL.mp4")
            open(dummy_mp4, "w").close()
            
            subprocess.run(["subliminal", "download", "-l", "en", dummy_mp4])

            for file in os.listdir(temp_dir):
                if file.startswith(clean_query) and not file.endswith(".mp4"):
                    found_subs.append(os.path.join(temp_dir, file))
            
            if found_subs:
                print(f"Success! Found subs using alias: {query}")
                break

        if not found_subs:
            return {"status": "pending", "message": f"No subtitles found online for any alias of {formatted_title}."}

        try:
            subs = pysubs2.load(found_subs[0], encoding='utf-8')
        except Exception:
            try:
                subs = pysubs2.load(found_subs[0], encoding='iso-8859-1')
            except Exception:
                return {"status": "error", "message": "Failed to parse subtitle file."}

        profanity_zones = []
        explicit_zones = []

        for sub in subs:
            # Apply platform-specific intro offset + buffer
            start_ms = max(0, sub.start - BUFFER_MS + platform_offset)
            end_ms = sub.end + BUFFER_MS + platform_offset
            text = sub.plaintext

            if EXPLICIT_REGEX.search(text):
                explicit_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "explicit"})

            spoken_text = re.sub(r'[\[\(].*?[\]\)]', '', text)
            if PROFANITY_REGEX.search(spoken_text):
                profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})

        all_zones = merge_zones(profanity_zones) + merge_zones(explicit_zones)
        all_zones.sort(key=lambda x: x["startTimeMs"])

        # Layer 1: Subtitle-based Advisories
        advisories_map = {}
        if any(z["skipType"] == "profanity" for z in all_zones):
            advisories_map["Profanity"] = "Severe"
        if any(z["skipType"] == "explicit" for z in all_zones):
            advisories_map["Nudity & Explicit Content"] = "Moderate"

        # Layer 2: Scrape IMDb Parents Guide for community severities
        imdb_advisories = fetch_imdb_advisories(formatted_title, TMDB_API_KEY)

        # Merge them (IMDb community data overwrites the basic subtitle scan if there are duplicates)
        final_advisories = {**advisories_map, **imdb_advisories}

        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        
        raw_url = gist.files[gist_file].raw_url
        db_data = requests.get(raw_url).json()

        db_data[formatted_title] = {
            "skipZones": all_zones,
            "advisories": final_advisories
        }

        new_content = json.dumps(db_data, separators=(',', ':'))
        gist.edit(files={gist_file: InputFileContent(content=new_content)})

    return {"status": "success", "title": formatted_title, "zones_added": len(all_zones), "platform_offset_ms": platform_offset}

last_scraped_title = ""
LIVE_EXCLUSIONS = ["wwe raw", "wwe smackdown", "wwe nxt", "wwe", "netflix live", "live from"]

@app.get("/api/netflix-sync")
def netflix_sync():
    global last_scraped_title
    import os
    import requests
    import re
    from bs4 import BeautifulSoup

    cookie_val = os.environ.get("NETFLIX_RAW_COOKIE") or os.environ.get("SECURE_NETFLIX_ID")
    if not cookie_val:
        return {"status": "error", "message": "No cookie found."}

    cookie_header = f"SecureNetflixId={cookie_val}" if "=" not in cookie_val else cookie_val
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": cookie_header}

    latest_title = None

    try:
        url_activity = "https://www.netflix.com/viewingactivity"
        response = requests.get(url_activity, headers=headers, allow_redirects=False)
        if response.status_code not in [301, 302]:
            soup = BeautifulSoup(response.text, 'html.parser')
            first_title_tag = soup.select_one('.col.title a')
            if first_title_tag:
                activity_title = first_title_tag.text.strip()
                if not any(ex in activity_title.lower() for ex in LIVE_EXCLUSIONS):
                    latest_title = activity_title
    except Exception:
        pass

    if latest_title == last_scraped_title or not latest_title:
        try:
            url_browse = "https://www.netflix.com/browse"
            res_browse = requests.get(url_browse, headers=headers)
            cw_chunk_match = re.search(r'"listContext":"continueWatching".*?\]', res_browse.text)
            if cw_chunk_match:
                cw_chunk = cw_chunk_match.group(0)
                raw_titles = re.findall(r'"title":"(.*?)"', cw_chunk)
                for raw_t in raw_titles:
                    cw_title = raw_t.encode('utf-8').decode('unicode_escape').strip()
                    if any(exclusion in cw_title.lower() for exclusion in LIVE_EXCLUSIONS):
                        continue
                    if cw_title and cw_title != last_scraped_title:
                        latest_title = cw_title
                        break
        except Exception:
            pass

    if latest_title and latest_title != last_scraped_title:
        last_scraped_title = latest_title
        return {"status": "success", "title": latest_title}
    else:
        return {"status": "pending", "message": "Netflix has not updated yet."}

@app.get("/api/delete-movie")
def delete_movie(title: str):
    try:
        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        db_data = requests.get(gist.files[gist_file].raw_url).json()
        
        keys_to_delete = [k for k in db_data.keys() if title.lower() in k.lower()]
        if keys_to_delete:
            for k in keys_to_delete:
                del db_data[k]
            gist.edit(files={gist_file: InputFileContent(content=json.dumps(db_data, separators=(',', ':')))})
            return {"status": "success", "message": f"Deleted: {keys_to_delete}"}
        return {"status": "error", "message": f"No entry containing '{title}' found."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/shift-sync")
def shift_sync(title: str, offset_ms: int):
    try:
        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        db_data = requests.get(gist.files[gist_file].raw_url).json()
        
        matched_keys = [k for k in db_data.keys() if title.lower() in k.lower()]
        if not matched_keys:
            return {"status": "error", "message": f"No title containing '{title}' found."}
            
        for k in matched_keys:
            for zone in db_data[k].get("skipZones", []):
                zone["startTimeMs"] += offset_ms
                zone["endTimeMs"] += offset_ms
                
        gist.edit(files={gist_file: InputFileContent(content=json.dumps(db_data, separators=(',', ':')))})
        return {"status": "success", "message": f"Shifted {matched_keys} by {offset_ms}ms"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
