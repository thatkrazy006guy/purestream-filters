import os
import tempfile
import json
import re
import requests
import pysubs2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from github import Github

app = FastAPI()

# --- CLOUD CONFIGURATION (REPORTS & GIST) ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID", "26c5299781dfbc13015947ebff5a6e66")
REPO_NAME = os.getenv("REPO_NAME", "thatkrazy006guy/purestream-filters")

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

    formatted_title = req.title
    if req.media_type == "tv" and req.season and req.episode:
        formatted_title = f"{req.title} S{req.season:02d}E{req.episode:02d}"

    # Process in temporary cloud directory
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_mp4 = os.path.join(temp_dir, f"{req.title}.mp4")
        open(dummy_mp4, "w").close()

        # Download subtitles using Subliminal CLI
        os.system(f"subliminal download -l en '{dummy_mp4}'")

        found_subs = []
        for ext in [".srt", ".ass", ".ssa", ".sub"]:
            sub_file = os.path.join(temp_dir, f"{req.title}{ext}")
            if os.path.exists(sub_file):
                found_subs.append(sub_file)

        if not found_subs:
            return {"status": "pending", "message": "No subtitles found online yet."}

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

        # Update Gist Database directly via Github API
        gh = Github(GITHUB_TOKEN)
        gist = gh.get_gist(GIST_ID)
        gist_file = list(gist.files.keys())[0]
        db_data = json.loads(gist.files[gist_file].content)

        db_data[formatted_title] = {
            "skipZones": all_zones,
            "advisories": {}
        }

        gist.edit(files={gist_file: {"content": json.dumps(db_data, indent=2)}})

    return {"status": "success", "title": formatted_title, "zones_added": len(all_zones)}