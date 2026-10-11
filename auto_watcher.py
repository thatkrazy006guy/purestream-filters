import os
import tempfile
import requests
import json
import re
import pysubs2
from datetime import datetime, timedelta
from github import Github

# --- API & ENVIRONMENT CONFIGURATION ---
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
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
rating_cache = {}

def get_gist_db():
    gh = Github(GITHUB_TOKEN)
    gist = gh.get_gist(GIST_ID)
    filename = list(gist.files.keys())[0]
    data = json.loads(gist.files[filename].content)
    return gh, gist, filename, data

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

def is_tv_mature(show_name):
    if not TMDB_API_KEY: return True
    if show_name in rating_cache: return rating_cache[show_name]

    try:
        res = requests.get("https://api.themoviedb.org/3/search/tv", params={"api_key": TMDB_API_KEY, "query": show_name}, timeout=5).json()
        if not res.get("results"): return False
        show_id = res["results"][0]["id"]
        rate_res = requests.get(f"https://api.themoviedb.org/3/tv/{show_id}/content_ratings", params={"api_key": TMDB_API_KEY}, timeout=5).json()
        for country in rate_res.get("results", []):
            if country.get("iso_3166_1") == "US":
                is_mature = country.get("rating", "") in ["TV-14", "TV-MA"]
                rating_cache[show_name] = is_mature
                return is_mature
        return False
    except Exception:
        return False

def fetch_recent_tv_episodes():
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        response = requests.get("https://api.tvmaze.com/schedule/full", timeout=10)
        if response.status_code != 200: return []
    except Exception:
        return []

    new_drops = []
    for ep in response.json():
        if ep.get("airdate") in [today, yesterday]:
            show_data = ep.get("_embedded", {}).get("show", {})
            if show_data.get("language") == "English" and show_data.get("type") not in ["News", "Sports", "Award Show"]:
                show_name = show_data.get("name", "")
                season, number = ep.get("season"), ep.get("number")
                if show_name and season and number and is_tv_mature(show_name):
                    new_drops.append(f"{show_name} S{season:02d}E{number:02d}")
    return list(set(new_drops))

def fetch_recent_movies():
    if not TMDB_API_KEY: return []
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    params = {
        "api_key": TMDB_API_KEY,
        "primary_release_date.gte": yesterday,
        "primary_release_date.lte": today,
        "with_original_language": "en",
        "region": "US",
        "certification_country": "US",
        "certification": "PG-13|R|NC-17" 
    }
    try:
        response = requests.get("https://api.themoviedb.org/3/discover/movie", params=params, timeout=10)
        if response.status_code != 200: return []
    except Exception:
        return []

    new_movies = []
    for movie in response.json().get("results", []):
        title = movie.get("title")
        release_date = movie.get("release_date", "")
        if title:
            year = release_date.split("-")[0] if release_date else datetime.now().year
            new_movies.append(f"{title} {year}")
    return list(set(new_movies))

def process_single_title(title):
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_path = os.path.join(temp_dir, f"{title}.mp4")
        open(dummy_path, "w").close()

        os.system(f"subliminal download -l en '{dummy_path}'")

        found_subs = []
        for ext in [".srt", ".ass", ".ssa", ".sub"]:
            sub_file = os.path.join(temp_dir, f"{title}{ext}")
            if os.path.exists(sub_file):
                found_subs.append(sub_file)

        if not found_subs: return None

        try:
            subs = pysubs2.load(found_subs[0], encoding='utf-8')
        except Exception:
            try:
                subs = pysubs2.load(found_subs[0], encoding='iso-8859-1')
            except Exception:
                return None

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
        return all_zones

if __name__ == "__main__":
    gh, gist, filename, db_data = get_gist_db()
    existing_titles = set(k.lower() for k in db_data.keys())

    recent_tv = fetch_recent_tv_episodes()
    recent_movies = fetch_recent_movies()
    all_to_check = list(set(recent_tv + recent_movies))

    missing_titles = [t for t in all_to_check if t.lower() not in existing_titles]
    print(f"Found {len(missing_titles)} new releases to check.")

    updated = False
    for title in missing_titles:
        print(f"Scrubbing: {title}")
        zones = process_single_title(title)
        if zones is not None:
            db_data[title] = {"skipZones": zones, "advisories": {}}
            updated = True
            print(f"Successfully added {title} to Gist!")

    if updated:
        gist.edit(files={filename: {"content": json.dumps(db_data, indent=2)}})
        print("Updated GitHub Gist Database successfully.")