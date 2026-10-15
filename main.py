import os
import time
import tempfile
import json
import re
import requests
import pysubs2
import subprocess
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github, InputFileContent

app = FastAPI()

# --- CLOUD CONFIGURATION ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GIST_ID = os.getenv("GIST_ID", "26c5299781dfbc13015947ebff5a6e66").strip()

PLATFORM_OFFSETS = {
    "com.wbd.stream": 0,
    "com.hbo.hbonow": 0,
    "com.hulu.livingroomplus": 0,
    "com.disney.disneyplus": 0,
    "com.amazon.amazonvideo.livingroom": 0,
    "com.netflix.ninja": 0
}

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

def fetch_tmdb_rating(media_id, media_type, api_key):
    if not api_key or not media_id: return ""
    try:
        if media_type == "movie":
            url = f"https://api.themoviedb.org/3/movie/{media_id}/release_dates?api_key={api_key}"
            res = requests.get(url, timeout=5).json()
            for result in res.get("results", []):
                if result.get("iso_3166_1") == "US":
                    dates = result.get("release_dates", [])
                    if dates: return dates[0].get("certification", "")
        else:
            url = f"https://api.themoviedb.org/3/tv/{media_id}/content_ratings?api_key={api_key}"
            res = requests.get(url, timeout=5).json()
            for result in res.get("results", []):
                if result.get("iso_3166_1") == "US":
                    return result.get("rating", "")
    except: pass
    return ""

def fetch_community_edl(imdb_id):
    if not imdb_id: return []
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
                community_zones.append({
                    "startTimeMs": int(float(parts[0]) * 1000), 
                    "endTimeMs": int(float(parts[1]) * 1000), 
                    "skipType": "profanity" if parts[2] == "1" else "explicit"
                })
        return community_zones
    except: return []

@app.get("/")
def health_check():
    return {"status": "PureStream Universal Cloud Service Running"}

@app.post("/api/report-title")
def report_title(req: ReportRequest):
    if not GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN not set.")

    formatted_title = req.title
    if req.media_type == "tv" and req.season and req.episode:
        formatted_title = f"{req.title} S{req.season:02d}E{req.episode:02d}"

    platform_offset = PLATFORM_OFFSETS.get(req.package_name, 0)
    search_queries = [formatted_title]
    base_title = re.sub(r'\s*S\d+E\d+.*', '', formatted_title, flags=re.IGNORECASE).strip()
    if base_title != formatted_title: search_queries.append(base_title)

    tmdb_media_id, tmdb_media_type, imdb_id = None, req.media_type, None

    if TMDB_API_KEY:
        try:
            res = requests.get("https://api.themoviedb.org/3/search/multi", params={"api_key": TMDB_API_KEY, "query": base_title}).json()
            results = res.get("results", [])
            if results:
                tmdb_media_id = results[0]["id"]
                tmdb_media_type = results[0].get("media_type", "movie")
                official = results[0].get("title") or results[0].get("name")
                if official and official not in search_queries: search_queries.append(official)
                
                ext_url = f"https://api.themoviedb.org/3/{tmdb_media_type}/{tmdb_media_id}/external_ids?api_key={TMDB_API_KEY}"
                imdb_id = requests.get(ext_url, timeout=5).json().get("imdb_id")
        except: pass

    with tempfile.TemporaryDirectory() as temp_dir:
        found_subs = []
        for query in search_queries:
            clean_query = re.sub(r'[\\/:*?"<>|]', '', query).strip()
            dummy_mp4 = os.path.join(temp_dir, f"{clean_query}.1080p.WEB-DL.mp4")
            open(dummy_mp4, "w").close()
            subprocess.run(["subliminal", "download", "-l", "en", dummy_mp4], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        profanity_zones, explicit_zones, ai_success = [], [], False
        
        suspicious_lines = []
        for i, sub in enumerate(subs):
            text = sub.plaintext.replace('\n', ' ').strip()
            if EXPLICIT_REGEX.search(text) or PROFANITY_REGEX.search(re.sub(r'[\[\(].*?[\]\)]', '', text)):
                suspicious_lines.append(f"{i}|{text}")

        # --- LAYER 1: GROQ AI ENGINE (Sanity-Checked & Llama-Only) ---
        if GROQ_API_KEY and suspicious_lines:
            print(f"🧠 Booting Groq AI Engine on {len(suspicious_lines)} flagged lines...")
            script_chunk = "\n".join(suspicious_lines)
            prompt = (
                "You are a strict parental media filter. Read these specific numbered subtitle lines. "
                "Identify ONLY the lines containing actual severe profanity, explicit sexual innuendo, or graphic visual descriptions in brackets. "
                "Ignore mild words or non-explicit action. "
                "Return ONLY a raw JSON array of the integers matching the line numbers that violate this policy. "
                "Do not return any other text. Example: [15, 89, 102]\n\n"
                f"{script_chunk}"
            )
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            
            preferred_models = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
            active_models = preferred_models.copy()
            try:
                models_res = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=5)
                if models_res.status_code == 200:
                    fetched_models = [m["id"] for m in models_res.json().get("data", []) if "whisper" not in m["id"].lower() and "llama" in m["id"].lower()]
                    for m in fetched_models:
                        if m not in active_models: active_models.append(m)
            except: pass
            
            for model in active_models:
                try:
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
                    response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=20)
                    
                    if response.status_code == 200:
                        ai_text = response.json()["choices"][0]["message"]["content"]
                        match = re.search(r'\[.*?\]', ai_text, re.DOTALL)
                        if match:
                            bad_indices = json.loads(match.group(0))
                            
                            if len(bad_indices) == 0 and len(suspicious_lines) > 0:
                                print(f"⚠️ Groq ({model}) hallucinated 0 violations. Rejecting AI response.")
                                ai_success = False 
                                break 
                                
                            print(f"✅ AI SUCCESS: Groq ({model}) validated {len(bad_indices)} true violations.")
                            for idx in bad_indices:
                                if 0 <= idx < len(subs):
                                    start_ms = max(0, subs[idx].start - BUFFER_MS + platform_offset)
                                    end_ms = subs[idx].end + BUFFER_MS + platform_offset
                                    profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})
                            ai_success = True
                            break
                except: pass
            
            if not ai_success:
                print("⚠️ AI failed sanity check or network limits. Bypassing AI Engine.")
        
        # --- LAYER 2: REGEX FALLBACK ---
        if not ai_success:
            print("⚙️ Running standard Regex Engine...")
            for idx_str in suspicious_lines:
                idx = int(idx_str.split("|")[0])
                sub = subs[idx]
                start_ms = max(0, sub.start - BUFFER_MS + platform_offset)
                end_ms = sub.end + BUFFER_MS + platform_offset
                text = sub.plaintext
                if EXPLICIT_REGEX.search(text): explicit_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "explicit"})
                spoken_text = re.sub(r'[\[\(].*?[\]\)]', '', text)
                if PROFANITY_REGEX.search(spoken_text): profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})

        # --- LAYER 3: OFFICIAL RATING & COMMUNITY MERGE ---
        tmdb_certification = fetch_tmdb_rating(tmdb_media_id, tmdb_media_type, TMDB_API_KEY)
        community_zones = fetch_community_edl(imdb_id)
        
        all_zones = merge_zones(profanity_zones) + merge_zones(explicit_zones) + merge_zones(community_zones)
        all_zones.sort(key=lambda x: x["startTimeMs"])

        final_advisories = {}
        if tmdb_certification: final_advisories["Official Rating"] = tmdb_certification
        if any(z["skipType"] == "profanity" for z in all_zones): final_advisories["Profanity"] = "Severe"
        if any(z["skipType"] == "explicit" for z in all_zones): final_advisories["Nudity & Explicit Content"] = "Moderate"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                gh = Github(GITHUB_TOKEN)
                gist = gh.get_gist(GIST_ID)
                gist_file = list(gist.files.keys())[0]
                db_data = requests.get(gist.files[gist_file].raw_url).json()

                existing_zones = db_data.get(formatted_title, {}).get("skipZones", [])
                if len(all_zones) == 0 and len(existing_zones) > 0:
                    print("🛡️ Anti-Overwrite Protocol: Prevented an empty scan from wiping out existing database zones.")
                    break

                db_data[formatted_title] = {"skipZones": all_zones, "advisories": final_advisories}
                gist.edit(files={gist_file: InputFileContent(content=json.dumps(db_data, separators=(',', ':')))})
                print(f"✅ GitHub Save Success (Attempt {attempt + 1})")
                break
            except Exception as e:
                if "409" in str(e) and attempt < max_retries - 1:
                    time.sleep(2)
                else: break

    return {"status": "success", "title": formatted_title, "zones_added": len(all_zones)}

last_scraped_title = ""
LIVE_EXCLUSIONS = ["wwe raw", "wwe smackdown", "wwe nxt", "wwe", "netflix live", "live from"]

@app.get("/api/netflix-sync")
def netflix_sync():
    global last_scraped_title
    import os, requests, re
    from bs4 import BeautifulSoup
    cookie_val = os.environ.get("NETFLIX_RAW_COOKIE") or os.environ.get("SECURE_NETFLIX_ID")
    if not cookie_val: return {"status": "error", "message": "No cookie found."}
    headers = {"User-Agent": "Mozilla/5.0", "Cookie": f"SecureNetflixId={cookie_val}" if "=" not in cookie_val else cookie_val}
    latest_title = None
    try:
        res = requests.get("https://www.netflix.com/viewingactivity", headers=headers, allow_redirects=False)
        if res.status_code not in [301, 302]:
            tag = BeautifulSoup(res.text, 'html.parser').select_one('.col.title a')
            if tag and not any(ex in tag.text.lower() for ex in LIVE_EXCLUSIONS): latest_title = tag.text.strip()
    except: pass
    if latest_title == last_scraped_title or not latest_title:
        try:
            res_browse = requests.get("https://www.netflix.com/browse", headers=headers)
            cw = re.search(r'"listContext":"continueWatching".*?\]', res_browse.text)
            if cw:
                for raw_t in re.findall(r'"title":"(.*?)"', cw.group(0)):
                    cw_title = raw_t.encode('utf-8').decode('unicode_escape').strip()
                    if not any(ex in cw_title.lower() for ex in LIVE_EXCLUSIONS) and cw_title != last_scraped_title:
                        latest_title = cw_title
                        break
        except: pass
    if latest_title and latest_title != last_scraped_title:
        last_scraped_title = latest_title
        return {"status": "success", "title": latest_title}
    return {"status": "pending", "message": "Waiting on Netflix"}

@app.get("/api/delete-movie")
def delete_movie(title: str):
    try:
        gh, gist = Github(GITHUB_TOKEN), Github(GITHUB_TOKEN).get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        db_data = requests.get(gist.files[gist_file].raw_url).json()
        keys = [k for k in db_data.keys() if title.lower() in k.lower()]
        if keys:
            for k in keys: del db_data[k]
            gist.edit(files={gist_file: InputFileContent(content=json.dumps(db_data, separators=(',', ':')))})
            return {"status": "success", "message": f"Deleted: {keys}"}
        return {"status": "error", "message": f"'{title}' not found."}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/shift-sync")
def shift_sync(title: str, offset_ms: int):
    try:
        gh, gist = Github(GITHUB_TOKEN), Github(GITHUB_TOKEN).get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        db_data = requests.get(gist.files[gist_file].raw_url).json()
        keys = [k for k in db_data.keys() if title.lower() in k.lower()]
        if not keys: return {"status": "error", "message": f"'{title}' not found."}
        for k in keys:
            for z in db_data[k].get("skipZones", []):
                z["startTimeMs"] += offset_ms
                z["endTimeMs"] += offset_ms
        gist.edit(files={gist_file: InputFileContent(content=json.dumps(db_data, separators=(',', ':')))})
        return {"status": "success", "message": f"Shifted {keys} by {offset_ms}ms"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/db")
def view_database():
    try:
        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        return requests.get(gist.files[gist_file].raw_url).json()
    except Exception as e:
        return {"error": str(e)}
