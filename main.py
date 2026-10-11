import os
import tempfile
import json
import re
import requests
import pysubs2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github, InputFileContent

app = FastAPI()

# --- CLOUD CONFIGURATION ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
GIST_ID = os.getenv("GIST_ID", "26c5299781dfbc13015947ebff5a6e66")

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

BUFFER_MS = 1500

class ReportRequest(BaseModel):
    tmdb_id: int = 0
    media_type: str = "movie"
    title: str
    season: int = None
    episode: int = None

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

@app.get("/")
def health_check():
    return {"status": "PureStream Cloud Service Running"}

@app.post("/api/report-title")
def report_title(req: ReportRequest):
    if not GITHUB_TOKEN:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN environment variable not set.")

    # 1. The literal title Netflix sends
    formatted_title = req.title
    if req.media_type == "tv" and req.season and req.episode:
        formatted_title = f"{req.title} S{req.season:02d}E{req.episode:02d}"

    # 2. Build Smart Fallback Queries
    search_queries = [formatted_title]
    
    # Strip fake TV show tags (Turns "Downfall S01E01" into "Downfall")
    base_title = re.sub(r'\s*S\d+E\d+.*', '', formatted_title, flags=re.IGNORECASE).strip()
    if base_title != formatted_title and base_title not in search_queries:
        search_queries.append(base_title)

    # Ping TMDB for the official alias (Turns "Downfall" into "Downfall: The Case Against Boeing")
    if TMDB_API_KEY:
        try:
            res = requests.get("https://api.themoviedb.org/3/search/multi", params={"api_key": TMDB_API_KEY, "query": base_title}).json()
            for item in res.get("results", [])[:3]: # Grab the top 3 matches
                official = item.get("title") or item.get("name")
                if official and official not in search_queries:
                    search_queries.append(official)
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as temp_dir:
        found_subs = []
        
        # 3. Try searching each alias until we find subtitles
        for query in search_queries:
            clean_query = re.sub(r'[\\/:*?"<>|]', '', query).strip()
            dummy_mp4 = os.path.join(temp_dir, f"{clean_query}.mp4")
            open(dummy_mp4, "w").close()
            
            os.system(f"subliminal download -l en '{dummy_mp4}'")

            for ext in [".srt", ".ass", ".ssa", ".sub"]:
                sub_file = os.path.join(temp_dir, f"{clean_query}{ext}")
                if os.path.exists(sub_file):
                    found_subs.append(sub_file)
            
            if found_subs:
                print(f"Success! Found subs using alias: {query}")
                break # Stop searching, we found it!

        if not found_subs:
            return {"status": "pending", "message": f"No subtitles found online for any alias of {formatted_title}."}

        # 4. Process the subtitles
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
            start_ms = max(0, sub.start - BUFFER_MS)
            end_ms = sub.end + BUFFER_MS
            text = sub.plaintext

            if PROFANITY_REGEX.search(text):
                profanity_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "profanity"})
            if EXPLICIT_REGEX.search(text):
                explicit_zones.append({"startTimeMs": start_ms, "endTimeMs": end_ms, "skipType": "explicit"})

        all_zones = merge_zones(profanity_zones) + merge_zones(explicit_zones)
        all_zones.sort(key=lambda x: x["startTimeMs"])

        # 5. Update Gist Database
        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        
        raw_url = gist.files[gist_file].raw_url
        db_data = requests.get(raw_url).json()

        # CRITICAL: We save the zones under the EXACT Netflix title so the TV app instantly recognizes it
        db_data[formatted_title] = {
            "skipZones": all_zones,
            "advisories": {}
        }

        new_content = json.dumps(db_data, separators=(',', ':'))
        gist.edit(files={gist_file: InputFileContent(content=new_content)})

    return {"status": "success", "title": formatted_title, "zones_added": len(all_zones)}
