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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # AI Context Engine Key
GIST_ID = os.getenv("GIST_ID", "26c5299781dfbc13015947ebff5a6e66")

PLATFORM_OFFSETS = {
    "com.wbd.stream": 7000,
    "com.hbo.hbonow": 7000,
    "com.hulu.livingroomplus": 4000,
    "com.disney.disneyplus": 5000,
    "com.amazon.amazonvideo.livingroom": 2000,
    "com.netflix.ninja": 0
}

# --- REGEX FALLBACK LISTS ---
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
    """Scrapes Nudity/Drugs/Violence severities from IMDb Parents Guide."""
    if not tmdb_api_key: return {}, None
    try:
        clean_title = re.sub(r'\s*S\d+E\d+.*', '', title, flags=re.IGNORECASE).strip()
        tmdb_url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_api_key}&query={clean_title}"
        results = requests.get(tmdb_url).json().get("results", [])
        if not results: return {}, None
        
        media_item = results[0]
        media_id = media_item["id"]
        media_type = media_item.get("media_type", "movie")
        
        ext_url = f"https://api.themoviedb.org/3/{media_type}/{media_id}/external_ids?api_key={tmdb_api_key}"
        imdb_id = requests.get(ext_url).json().get("imdb_id")
        if not imdb_id: return {}, None
        
        pg_url = f"https://www.imdb.com/title/{imdb_id}/parentalguide"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(pg_url, headers=headers)
        
        advisories = {}
        categories = ["Sex & Nudity", "Violence & Gore", "Profanity", "Alcohol, Drugs & Smoking", "Frightening & Intense Scenes"]
        
        for cat in categories:
            pattern = re.compile(fr'{re.escape(cat)}.{'{0,500}'}?(None|Mild|Moderate|Severe)', re.IGNORECASE | re.DOTALL)
            match = pattern.search(res.text)
            if match:
                advisories[cat] = match.group(1).title()
                
        return advisories, imdb_id
    except Exception as e:
        print(f"IMDb Scraper Error: {e}")
        return {}, None

def fetch_community_edl(imdb_id):
    """Scrapes open-source EDL repositories for community visual skips."""
    if not imdb_id: return []
    # Placeholder for a community repo URL. You can map this to any raw GitHub repository hosting .edl files
    edl_repo_url = f"https://raw.githubusercontent.com/PureStreamCommunity/EDL-Database/main/movies/{imdb_id}.edl"
    try:
        res = requests.get(edl_repo_url, timeout=3)
        if res.status_code != 200: return []
            
        community_zones = []
        for line in res.text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            parts = line.split()
            if len(parts) >= 3:
                start_ms = int(float(parts[0]) * 1000)
                end_ms = int(float(parts[1]) * 1000)
                action = "profanity" if parts[2] == "1" else "explicit"
                community_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": action})
        return community_zones
    except:
        return []

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

    platform_offset = PLATFORM_OFFSETS.get(req.package_name, 0)
    search_queries = [formatted_title]
    base_title = re.sub(r'\s*S\d+E\d+.*', '', formatted_title, flags=re.IGNORECASE).strip()
    if base_title != formatted_title: search_queries.append(base_title)

    if TMDB_API_KEY:
        try:
            res = requests.get("https://api.themoviedb.org/3/search/multi", params={"api_key": TMDB_API_KEY, "query": base_title}).json()
            for item in res.get("results", [])[:3]:
                official = item.get("title") or item.get("name")
                if official and official not in search_queries:
                    search_queries.append(official)
        except: pass

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
            if found_subs: break

        if not found_subs:
            return {"status": "pending", "message": f"No subtitles found for {formatted_title}."}

        try:
            subs = pysubs2.load(found_subs[0], encoding='utf-8')
        except:
            try: subs = pysubs2.load(found_subs[0], encoding='iso-8859-1')
            except: return {"status": "error", "message": "Failed to parse subtitle file."}

        profanity_zones = []
        explicit_zones = []
        
        # --- LAYER 1: GROQ AI CONTEXT ENGINE ---
        ai_success = False
        if GROQ_API_KEY:
            try:
                print("Attempting Groq AI analysis...")
                sub_lines = []
                for i, sub in enumerate(subs):
                    clean_text = sub.plaintext.replace('\n', ' ').strip()
                    sub_lines.append(f"{i}|{clean_text}")
                
                full_script = "\n".join(sub_lines)
                
                prompt = (
                    "You are a strict parental media filter. Read the following numbered movie subtitle lines. "
                    "Identify lines containing severe profanity, explicit sexual innuendo, or graphic visual descriptions in brackets. "
                    "Ignore mild words (like hell, damn) or non-explicit words (like pounding on a door). "
                    "Return ONLY a raw JSON array of the integers matching the line numbers that violate this policy. "
                    "Do not return any other text, markdown, or explanation. Example: [15, 89, 102]\n\n"
                    f"{full_script}"
                )

                headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
                payload = {"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
                
                response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                
                ai_text = response.json()["choices"][0]["message"]["content"]
                match = re.search(r'\[.*?\]', ai_text, re.DOTALL)
                if match:
                    bad_indices = json.loads(match.group(0))
                    print(f"Groq AI successfully found {len(bad_indices)} violating lines.")
                    for idx in bad_indices:
                        if 0 <= idx < len(subs):
                            start_ms = max(0, subs[idx].start - BUFFER_MS + platform_offset)
                            end_ms = subs[idx].end + BUFFER_MS + platform_offset
                            profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})
                    ai_success = True
            except Exception as e:
                print(f"Groq AI Failed ({e}). Falling back to Regex Engine!")

        # --- LAYER 2: REGEX FALLBACK ENGINE ---
        if not ai_success:
            print("Running Regex fallback engine...")
            for sub in subs:
                start_ms = max(0, sub.start - BUFFER_MS + platform_offset)
                end_ms = sub.end + BUFFER_MS + platform_offset
                text = sub.plaintext

                if EXPLICIT_REGEX.search(text):
                    explicit_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "explicit"})

                spoken_text = re.sub(r'[\[\(].*?[\]\)]', '', text)
                if PROFANITY_REGEX.search(spoken_text):
                    profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})

        # --- LAYER 3: IMDb & COMMUNITY EDL ---
        imdb_advisories, imdb_id = fetch_imdb_advisories(formatted_title, TMDB_API_KEY)
        community_zones = fetch_community_edl(imdb_id)
        
        all_zones = merge_zones(profanity_zones) + merge_zones(explicit_zones) + merge_zones(community_zones)
        all_zones.sort(key=lambda x: x["startTimeMs"])

        advisories_map = {}
        if any(z["skipType"] == "profanity" for z in all_zones): advisories_map["Profanity"] = "Severe"
        if any(z["skipType"] == "explicit" for z in all_zones): advisories_map["Nudity & Explicit Content"] = "Moderate"

        final_advisories = {**advisories_map, **imdb_advisories}

        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        
        db_data = requests.get(gist.files[gist_file].raw_url).json()

        db_data[formatted_title] = {
            "skipZones": all_zones,
            "advisories": final_advisories
        }

        new_content = json.dumps(db_data, separators=(',', ':'))
        gist.edit(files={gist_file: InputFileContent(content=new_content)})

    return {"status": "success", "title": formatted_title, "zones_added": len(all_zones), "platform_offset_ms": platform_offset, "ai_used": ai_success}

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
    except Exception: pass

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
                    if any(exclusion in cw_title.lower() for exclusion in LIVE_EXCLUSIONS): continue
                    if cw_title and cw_title != last_scraped_title:
                        latest_title = cw_title
                        break
        except Exception: pass

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
            for k in keys_to_delete: del db_data[k]
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
