import os
import re
import sys
import time
import uuid
import json
import random
import shutil
import subprocess
import threading
from difflib import SequenceMatcher
from pathlib import Path
import string
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, join_room as sio_join, leave_room as sio_leave, emit
from dotenv import load_dotenv
import boto3
from botocore.exceptions import ClientError
import yt_dlp

load_dotenv()

app = Flask(__name__, static_folder="../frontend", static_url_path="")
CORS(app)
# threading async mode → no eventlet/gevent dependency, plays nicely with the
# existing threading.Thread-based Demucs workers.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BASE_DIR = Path(__file__).parent.parent
AUDIO_DIR = BASE_DIR / "audio"
UPLOADS_DIR = AUDIO_DIR / "uploads"
STEMS_DIR = AUDIO_DIR / "stems"
CACHE_DIR = AUDIO_DIR / "cache"   # keyed by video ID — persists across restarts
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── R2 client (optional — gracefully disabled if .env is missing) ─────────────
def _make_r2_client():
    try:
        account_id = os.environ["R2_ACCOUNT_ID"].strip()
        access_key = os.environ["R2_ACCESS_KEY_ID"].strip()
        secret_key = os.environ["R2_SECRET_ACCESS_KEY"].strip()
        if not all([account_id, access_key, secret_key]):
            return None, None
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        return client, os.environ.get("R2_BUCKET", "riffdle-cache").strip()
    except (KeyError, Exception):
        return None, None

r2, R2_BUCKET = _make_r2_client()


def r2_upload(local_path: Path, key: str):
    """Upload a file to R2 in a background thread (non-blocking)."""
    if not r2:
        return
    content_type = "audio/mpeg" if local_path.suffix == ".mp3" else "application/json"
    def _upload():
        try:
            r2.upload_file(str(local_path), R2_BUCKET, key,
                           ExtraArgs={"ContentType": content_type})
        except Exception:
            pass
    threading.Thread(target=_upload, daemon=True).start()


def r2_download(key: str, local_path: Path) -> bool:
    """Download a file from R2. Returns True on success."""
    if not r2:
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        r2.download_file(R2_BUCKET, key, str(local_path))
        return True
    except ClientError:
        return False


def r2_key_exists(key: str) -> bool:
    if not r2:
        return False
    try:
        r2.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def r2_get_json(key: str, default):
    """Read a small JSON object straight from R2 (no temp file). Returns `default`
    if the key is missing, R2 is disabled, or the body can't be parsed."""
    if not r2:
        return default
    try:
        obj = r2.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except (ClientError, ValueError, KeyError):
        return default


def r2_put_json(key: str, obj) -> bool:
    """Write a small JSON object straight to R2. Returns True on success."""
    if not r2:
        return False
    try:
        r2.put_object(Bucket=R2_BUCKET, Key=key,
                      Body=json.dumps(obj, indent=2).encode("utf-8"),
                      ContentType="application/json")
        return True
    except ClientError:
        return False


# ── Cached-song catalog (drives search autocomplete) ──────────────────────────
# A flat list [{video_id, title, artist}] of every song cached in R2 — kept in
# catalog.json so the search box can suggest ANY cached song, not just ones that
# also happen to be in the songs.json seed list. Updated whenever a song is
# cached (here and in cache_queue.py).
CATALOG_KEY = "catalog.json"
_catalog_lock = threading.Lock()


def add_to_catalog(video_id: str, title: str, artist: str):
    """Upsert a song into catalog.json (deduped by video_id)."""
    if not r2 or not video_id:
        return
    with _catalog_lock:
        cat = r2_get_json(CATALOG_KEY, [])
        if not isinstance(cat, list):
            cat = []
        cat = [c for c in cat if c.get("video_id") != video_id]
        cat.append({"video_id": video_id, "title": title, "artist": artist})
        r2_put_json(CATALOG_KEY, cat)


# In-memory game state: { session_id: { ... } }
games = {}


# ── yt-dlp cookies ────────────────────────────────────────────────────────────
# Datacenter IPs (Render, etc.) get YouTube's "confirm you're not a bot" challenge.
# Supplying a Netscape-format cookies.txt authenticates the requests. Point
# YT_COOKIES_FILE at one, or drop it at /etc/secrets/cookies.txt (Render secret
# file). Empty locally on your Mac (residential IP doesn't need it).
def _cookie_opts() -> dict:
    src = os.environ.get("YT_COOKIES_FILE") or "/etc/secrets/cookies.txt"
    if not os.path.exists(src):
        return {}
    # Render mounts secret files read-only, but yt-dlp rewrites the cookie jar
    # after each request. Copy to a writable temp path so that write succeeds.
    dst = "/tmp/yt_cookies.txt"
    try:
        shutil.copyfile(src, dst)
        return {"cookiefile": dst}
    except Exception:
        return {"cookiefile": src}

YT_COOKIES = _cookie_opts()


def fuzzy_match(a: str, b: str, threshold: float = 0.75) -> bool:
    """True if strings are similar enough to count as a match (handles typos)."""
    if len(a) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def is_match(guess: str, target: str) -> bool:
    """
    True if guess meaningfully matches target.
    Parentheticals are stripped from the target before comparing so that
    "(From '8 Mile' Soundtrack)" or "(Radio Edit)" don't inflate the length
    and block correct guesses.
    Substring matches require the guess to cover at least 60% of the stripped
    target so that single words from long titles don't count.
    The reverse direction (target inside a longer guess) is always fine.
    """
    clean = re.sub(r"\s*[\(\[].*?[\)\]]", "", target).strip()
    if guess in clean:
        return len(guess) >= len(clean) * 0.6
    return clean in guess or fuzzy_match(guess, clean)


def clean_title(title: str, artist: str) -> str:
    """Strip leading 'Artist - ' or 'Artist: ' from titles that YouTube includes."""
    for sep in (" - ", ": "):
        if title.lower().startswith(artist.lower() + sep):
            return title[len(artist) + len(sep):]
    return title


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def core_title(title: str) -> str:
    """
    Reduce a YouTube title to just the song name so the same song matches
    regardless of how the artist is credited.
      'Gotye, Kimbra - Somebody That I Used To Know (Lyrics)' -> 'somebodythatiusedtoknow'
      'Somebody That I Used To Know'                          -> 'somebodythatiusedtoknow'
    """
    t = re.sub(r"[\(\[].*?[\)\]]", "", title)        # drop (Lyrics), [Official Video], etc.
    if " - " in t:
        t = t.split(" - ", 1)[1]                      # drop the 'Artist - ' prefix
    return _norm(t)


INDEX_FILE = CACHE_DIR / "song_index.json"

def _load_index() -> dict:
    return json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else {}

def _save_index(index: dict):
    INDEX_FILE.write_text(json.dumps(index))

def rebuild_index():
    """Scan local cache folders and build a core_title -> video_id map. Local-only."""
    index = {}
    for cache_path in CACHE_DIR.iterdir():
        meta_file = cache_path / "meta.json"
        if not cache_path.is_dir() or not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            index[core_title(meta["title"])] = cache_path.name
        except (json.JSONDecodeError, KeyError):
            continue
    _save_index(index)
    return index


def bootstrap_index():
    """
    Boot-time index restore. When R2 is configured the canonical index lives in
    the bucket (it covers songs whose stems were offloaded and deleted locally),
    so we pull it down rather than rebuilding from local folders — a local scan
    would wipe entries for any song not currently cached on disk.
    Falls back to a local rebuild only when R2 is unavailable.
    """
    if r2 and r2_download("song_index.json", INDEX_FILE):
        try:
            return json.loads(INDEX_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return rebuild_index()


def get_video_meta(url: str) -> dict | None:
    """Extract video ID, title, and artist without downloading."""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, **YT_COOKIES}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        vid = info.get("id")
        if not vid:
            return None
        raw = info.get("track") or info.get("title", "Unknown Title")
        artist = (info.get("artist") or info.get("uploader") or
                  info.get("channel") or "Unknown Artist")
        if artist.endswith(" - Topic"):
            artist = artist[:-len(" - Topic")]
        title = re.sub(
            r"\s*[\(\[][^\)\]]*(?:official|lyric|remaster|4k|hd|ft\.|feat\.|\d{4})[^\)\]]*[\)\]]",
            "", raw, flags=re.IGNORECASE,
        ).strip().rstrip("-–—").strip()
        title = clean_title(title, artist)
        return {"id": vid, "title": title, "artist": artist}


def load_cache(video_id: str) -> dict | None:
    """Return cached stem paths + metadata. Falls back to R2 if not found locally."""
    cache_path = CACHE_DIR / video_id
    meta_file  = cache_path / "meta.json"

    # Try to pull from R2 if missing locally
    if not meta_file.exists():
        if not r2_download(f"{video_id}/meta.json", meta_file):
            return None

    meta = json.loads(meta_file.read_text())
    stem_names = meta.get("stems", ["drums", "bass", "melody", "vocals"])
    stem_files = {}
    for name in stem_names:
        local = cache_path / f"{name}.mp3"
        if not local.exists():
            # Try to pull this stem from R2
            if not r2_download(f"{video_id}/{name}.mp3", local):
                return None   # R2 doesn't have it either
        stem_files[name] = str(local)

    artist = meta["artist"]
    title  = clean_title(meta["title"], artist)
    return {"title": title, "artist": artist, "stem_files": stem_files}


def save_cache(video_id: str, title: str, artist: str, stem_files: dict):
    """Copy active stems into the cache directory, update index, and sync to R2."""
    cache_path = CACHE_DIR / video_id
    cache_path.mkdir(parents=True, exist_ok=True)
    for stem_name, src_path in stem_files.items():
        dst = cache_path / f"{stem_name}.mp3"
        if not dst.exists():
            shutil.copy2(src_path, dst)
        r2_upload(dst, f"{video_id}/{stem_name}.mp3")

    meta_file = cache_path / "meta.json"
    meta_file.write_text(
        json.dumps({"title": title, "artist": artist, "stems": list(stem_files.keys())})
    )
    r2_upload(meta_file, f"{video_id}/meta.json")

    # Update core_title → video_id index so different uploads of the same song reuse this cache
    index = _load_index()
    index[core_title(title)] = video_id
    _save_index(index)
    r2_upload(INDEX_FILE, "song_index.json")

    # Add to the search catalog so it's instantly suggestable on the live site
    add_to_catalog(video_id, clean_title(title, artist), artist)


def download_audio(url: str, session_id: str) -> dict:
    out_dir = UPLOADS_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "audio.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Force the android_vr client: it returns directly-downloadable audio
        # formats. With cookies present yt-dlp otherwise prefers the web client,
        # whose formats need PO tokens and fail with "format is not available".
        "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        **YT_COOKIES,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "192",
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        raw_title = info.get("track") or info.get("title", "Unknown Title")
        # Prefer music metadata artist over channel name (avoids cover/tribute channels)
        artist = (info.get("artist")
                  or info.get("uploader")
                  or info.get("channel")
                  or "Unknown Artist")
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")]
        # Strip "(Official Video)", "(4K Remaster)", "[Lyrics]", etc.
        title = re.sub(
            r"\s*[\(\[][^\)\]]*(?:official|lyric|remaster|4k|hd|ft\.|feat\.|\d{4})[^\)\]]*[\)\]]",
            "", raw_title, flags=re.IGNORECASE,
        ).strip().rstrip("-–—").strip()
        title = clean_title(title, artist)

    wav_path = out_dir / "audio.wav"
    return {"title": title, "artist": artist, "wav_path": str(wav_path)}


MODEL = "htdemucs_ft"  # fine-tuned model — better stem separation than htdemucs
TRIM_SECONDS = 90     # only separate the first 90s; game rarely needs more
SILENCE_DB = -50.0    # stems quieter than this are considered empty


def is_silent(mp3_path: str) -> bool:
    """Return True if the stem has no meaningful audio content."""
    result = subprocess.run(
        ["ffmpeg", "-i", mp3_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"max_volume: ([-\d.]+) dB", result.stderr)
    if not match:
        return False
    return float(match.group(1)) < SILENCE_DB


def separate_stems(wav_path: str, session_id: str) -> dict:
    stems_out = STEMS_DIR / session_id
    stems_out.mkdir(parents=True, exist_ok=True)

    # Trim to TRIM_SECONDS before separation — cuts Demucs time proportionally
    # Use a no-dot filename so Demucs output dir is predictable
    trimmed_path = str(Path(wav_path).parent / "trimmed.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path, "-t", str(TRIM_SECONDS), trimmed_path],
        check=True, capture_output=True,
    )

    wav_stem = "trimmed"

    cmd = [
        sys.executable, "-m", "demucs",
        "-n", MODEL,
        "-d", "mps",
        "-o", str(stems_out),
        trimmed_path,
    ]
    subprocess.run(cmd, check=True)

    # Demucs outputs to: stems_out/<model>/<trimmed_stem>/{drums,bass,other,vocals}.wav
    model_out = stems_out / MODEL / wav_stem

    # Convert each WAV to MP3 via ffmpeg for clean browser playback.
    # Demucs/torchaudio WAV encoding has quality issues; ffmpeg is reliable.
    stem_map = {
        "drums": model_out / "drums.wav",
        "bass":  model_out / "bass.wav",
        "melody": model_out / "other.wav",
        "vocals": model_out / "vocals.wav",
    }
    stem_files = {}
    for name, wav_file in stem_map.items():
        mp3_file = wav_file.with_suffix(".mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_file), "-q:a", "0", str(mp3_file)],
            check=True, capture_output=True,
        )
        if not is_silent(str(mp3_file)):
            stem_files[name] = str(mp3_file)

    # Safety: if everything read as silent (shouldn't happen), return all stems
    if not stem_files:
        stem_files = {name: str(wf.with_suffix(".mp3")) for name, wf in stem_map.items()}

    return stem_files


# ── Modal offload (optional) ──────────────────────────────────────────────────
# When RIFFDLE_USE_MODAL is set, cache misses are separated on a Modal GPU instead
# of locally. Keeps the hosted server light while still supporting "search any
# song". Local dev (your Mac) leaves it unset and uses local Demucs above.
USE_MODAL = os.environ.get("RIFFDLE_USE_MODAL", "").lower() in ("1", "true", "yes")

# When RIFFDLE_CACHE_ONLY is set (the hosted deploy), the server only plays songs
# already cached in R2 — it never tries to download new ones. YouTube blocks
# datacenter IPs from downloading, so new songs are added by the owner running
# Riffdle locally (residential IP) which auto-populates R2.
CACHE_ONLY = os.environ.get("RIFFDLE_CACHE_ONLY", "").lower() in ("1", "true", "yes")


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char YouTube video id straight from a URL (no network call),
    so cached songs can be served without ever hitting YouTube."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


REQUESTS_KEY = "song_requests.json"   # user-submitted song requests (R2)
_requests_lock = threading.Lock()


def log_song_request(query: str):
    """Append a user's song request to song_requests.json in R2 (deduped by a
    normalized key; re-requests bump a count so popular asks bubble up). Runs in a
    background thread so the request path never blocks on R2."""
    query = (query or "").strip()
    if not query or not r2:
        return

    def _write():
        key = _norm(query)
        if not key:
            return
        with _requests_lock:
            reqs = r2_get_json(REQUESTS_KEY, [])
            if not isinstance(reqs, list):
                reqs = []
            now = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
            for entry in reqs:
                if entry.get("key") == key:
                    entry["count"] = entry.get("count", 1) + 1
                    entry["last"] = now
                    break
            else:
                reqs.append({"key": key, "query": query, "count": 1,
                             "first": now, "last": now})
            r2_put_json(REQUESTS_KEY, reqs)

    threading.Thread(target=_write, daemon=True).start()


def separate_via_modal(url: str, session_id: str) -> dict:
    """Download here (residential IP avoids YouTube's bot challenge), trim to 90s,
    then run Demucs on Modal's GPU. Returns {title, artist, stem_files}."""
    import modal

    # 1. Download on the server (yt-dlp stays off datacenter IPs)
    dl = download_audio(url, session_id)

    # 2. Trim to 90s so we only ship ~16MB of wav to Modal
    out_dir = STEMS_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    trimmed = out_dir / "trimmed.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", dl["wav_path"], "-t", str(TRIM_SECONDS), str(trimmed)],
        check=True, capture_output=True,
    )

    # 3. Separate on the GPU
    fn = modal.Function.from_name("riffdle-demucs", "separate_audio")
    result = fn.remote(trimmed.read_bytes())   # blocks ~30-60s

    # 4. Write the returned stems locally
    stem_files = {}
    for name, data in result["stems"].items():
        path = out_dir / f"{name}.mp3"
        path.write_bytes(data)
        stem_files[name] = str(path)

    return {"title": dl["title"], "artist": dl["artist"], "stem_files": stem_files}


# Background worker so the client can poll
def process_game(session_id: str, url: str):
    try:
        # ── Cache check ───────────────────────────────────────────
        games[session_id]["status"] = "checking_cache"

        # 1. Exact video-id match straight from the URL — no YouTube call needed,
        #    so cached songs (and Random) work even if cookies/downloads don't.
        video_id = extract_video_id(url)
        cached = load_cache(video_id) if video_id else None

        # 2. Miss → fetch metadata and match the same song from any other upload.
        meta = None
        if not cached:
            meta = get_video_meta(url)
            if meta and not video_id:
                video_id = meta["id"]
            if meta:
                ct = core_title(meta["title"])
                idx = _load_index()
                existing_id = idx.get(ct)
                if not existing_id:   # fuzzy fallback for minor spelling diffs
                    for key, vid in idx.items():
                        if SequenceMatcher(None, ct, key).ratio() >= 0.9:
                            existing_id = vid
                            break
                if existing_id:
                    cached = load_cache(existing_id)

        if cached:
            games[session_id].update({
                "title": cached["title"],
                "artist": cached["artist"],
                "stem_files": cached["stem_files"],
                "status": "ready",
            })
            return

        # ── Cache miss ────────────────────────────────────────────
        # Hosted deploy can't download new songs (YouTube blocks datacenter IPs),
        # so fail gracefully and note the request for later.
        if CACHE_ONLY:
            log_song_request(meta["title"] if meta else url)
            games[session_id]["status"] = "error"
            games[session_id]["error"] = "not_cached"
            return

        # ── Full pipeline (local Mac / Modal) ─────────────────────
        games[session_id]["status"] = "separating"

        if USE_MODAL:
            # Modal does the download + GPU separation and returns the stems.
            info = separate_via_modal(url, session_id)
        else:
            # Local: download here, then separate with local Demucs.
            dl = download_audio(url, session_id)
            stem_files = separate_stems(dl["wav_path"], session_id)
            info = {"title": dl["title"], "artist": dl["artist"], "stem_files": stem_files}

        games[session_id].update({
            "title": info["title"],
            "artist": info["artist"],
            "stem_files": info["stem_files"],
            "status": "ready",
        })

        # Save to cache (local + R2) for next time
        if video_id:
            save_cache(video_id, info["title"], info["artist"], info["stem_files"])

    except Exception as e:
        games[session_id]["status"] = "error"
        games[session_id]["error"] = str(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/start", methods=["POST"])
def start_game():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    session_id = str(uuid.uuid4())
    games[session_id] = {
        "session_id": session_id,
        "url": url,
        "status": "queued",
        "title": None,
        "artist": None,
        "stem_files": None,
        "guesses": [],
        "artist_guessed": False,
    }

    thread = threading.Thread(target=process_game, args=(session_id, url), daemon=True)
    thread.start()

    return jsonify({"session_id": session_id})


@app.route("/api/status/<session_id>")
def game_status(session_id):
    game = games.get(session_id)
    if not game:
        return jsonify({"error": "Session not found"}), 404

    payload = {
        "status": game["status"],
        "session_id": session_id,
    }
    if game["status"] == "error":
        payload["error"] = game.get("error", "Unknown error")
    if game["status"] == "ready":
        payload["stems_available"] = list(game["stem_files"].keys())
    return jsonify(payload)


@app.route("/api/stem/<session_id>/<stem_name>")
def serve_stem(session_id, stem_name):
    game = games.get(session_id)
    if not game or game["status"] != "ready":
        return jsonify({"error": "Not ready"}), 404

    allowed = {"drums", "bass", "melody", "vocals"}
    if stem_name not in allowed:
        return jsonify({"error": "Invalid stem"}), 400

    file_path = Path(game["stem_files"][stem_name])
    if not file_path.exists():
        return jsonify({"error": "Stem file missing"}), 404

    return send_from_directory(str(file_path.parent), file_path.name)


@app.route("/api/guess", methods=["POST"])
def submit_guess():
    data = request.get_json()
    session_id = data.get("session_id")
    guess = data.get("guess", "").strip()
    stems_revealed = data.get("stems_revealed", 1)

    game = games.get(session_id)
    if not game or game["status"] != "ready":
        return jsonify({"error": "Session not ready"}), 400

    title = game["title"].lower()
    artist = game["artist"].lower()
    guess_lower = guess.lower()

    matches_title  = is_match(guess_lower, title)
    matches_artist = is_match(guess_lower, artist)

    base_points = max(0, 1000 - (stems_revealed - 1) * 200)

    if matches_title:
        result = "correct"
        points = base_points
    elif matches_artist and not game["artist_guessed"]:
        result = "artist"
        points = base_points // 2
        game["artist_guessed"] = True
    else:
        result = "wrong"
        points = 0

    game["guesses"].append({
        "guess": guess,
        "result": result,
        "stems_revealed": stems_revealed,
        "points": points,
    })

    return jsonify({
        "result": result,
        "points": points,
        "title":  game["title"]  if result in ("correct", "artist") else None,
        "artist": game["artist"] if result in ("correct", "artist") else None,
    })


SONGS = json.loads((BASE_DIR / "backend" / "songs.json").read_text())


@app.route("/api/songs")
def get_songs():
    return jsonify([{"title": s["title"], "artist": s["artist"]} for s in SONGS])


_catalog_cache = {"t": 0.0, "songs": None}
CATALOG_TTL = 30   # seconds — keep R2 reads cheap without going stale for long


@app.route("/api/catalog")
def catalog():
    """Every song currently cached in R2 — used for search autocomplete so the
    box can suggest any playable song. Reads catalog.json (kept fresh as songs
    are cached); falls back to the songs.json-gated pool if it's missing."""
    now = time.time()
    if _catalog_cache["songs"] is None or now - _catalog_cache["t"] > CATALOG_TTL:
        songs = r2_get_json(CATALOG_KEY, None)
        if not isinstance(songs, list):
            songs = playable_pool([], [])   # fallback: songs.json ∩ cache index
        songs.sort(key=lambda s: s.get("title", "").lower())
        _catalog_cache.update(t=now, songs=songs)
    return jsonify({"cache_only": CACHE_ONLY, "songs": _catalog_cache["songs"]})


@app.route("/api/request", methods=["POST"])
def request_song():
    """Log a user's request to add a song. The owner reviews these later
    (manage_requests.py) and caches the good ones from their Mac."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()[:120]
    if not query:
        return jsonify({"ok": False, "error": "empty"}), 400
    if not r2:
        return jsonify({"ok": False, "error": "unavailable"}), 503
    log_song_request(query)
    return jsonify({"ok": True})


@app.route("/api/random")
def random_song():
    decades = [d.strip() for d in request.args.get("decades", "").split(",") if d.strip()]
    genres  = [g.strip() for g in request.args.get("genres",  "").split(",") if g.strip()]

    # Hosted (cache-only): pick from songs already cached in R2 and return the
    # cached video id directly — no YouTube call, so Random always works.
    if CACHE_ONLY:
        pool = playable_pool(decades, genres)
        if not pool:
            return jsonify({"error": "No cached songs match those filters"}), 404
        song = random.choice(pool)
        return jsonify({"url": f"https://www.youtube.com/watch?v={song['video_id']}"})

    pool = SONGS
    if decades:
        pool = [s for s in pool if s["decade"] in decades]
    if genres:
        pool = [s for s in pool if s["genre"] in genres]
    if not pool:
        return jsonify({"error": "No songs match those filters"}), 404

    song = random.choice(pool)
    query = f"{song['title']} {song['artist']} official audio"

    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, **YT_COOKIES}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = info.get("entries") or []
            if not entries:
                return jsonify({"error": "Could not find song on YouTube"}), 404
            vid = entries[0]["id"]
    except Exception as e:
        return jsonify({"error": f"YouTube lookup failed: {e}"}), 502

    return jsonify({"url": f"https://www.youtube.com/watch?v={vid}"})


@app.route("/api/search")
def search_songs():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, **YT_COOKIES}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch3:{query}", download=False)
            results = []
            for entry in (info.get("entries") or []):
                vid = entry.get("id", "")
                dur = entry.get("duration") or 0
                artist = (entry.get("uploader") or entry.get("channel") or "")
                if artist.endswith(" - Topic"):
                    artist = artist[: -len(" - Topic")]
                results.append({
                    "id": vid,
                    "title": entry.get("title", "Unknown"),
                    "artist": artist,
                    "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
                    "duration": f"{int(dur) // 60}:{int(dur) % 60:02d}" if dur else "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })
    except Exception as e:
        return jsonify({"error": f"YouTube search failed: {e}"}), 502
    return jsonify({"results": results})


@app.route("/api/health")
def health():
    """Diagnostics for the hosted deploy — confirms R2, Modal, and cookies wiring."""
    cookie_path = os.environ.get("YT_COOKIES_FILE") or "/etc/secrets/cookies.txt"
    cookie_found = os.path.exists(cookie_path)
    first_line = ""
    if cookie_found:
        try:
            with open(cookie_path) as f:
                first_line = f.readline().strip()[:60]
        except Exception as e:
            first_line = f"(unreadable: {e})"
    return jsonify({
        "r2_connected": bool(r2),
        "use_modal": USE_MODAL,
        "modal_token_set": bool(os.environ.get("MODAL_TOKEN_ID")),
        "cookies_path": cookie_path,
        "cookies_found": cookie_found,
        "cookies_first_line": first_line,
        "cookies_active": bool(YT_COOKIES),
        "cached_songs": len(_load_index()),
    })


@app.route("/api/answer/<session_id>")
def reveal_answer(session_id):
    game = games.get(session_id)
    if not game:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"title": game["title"], "artist": game["artist"]})


# ══════════════════════════════════════════════════════════════════════════════
# MULTIPLAYER  (Flask-SocketIO)
# ══════════════════════════════════════════════════════════════════════════════
#
# Rooms live entirely in memory. Each room runs an auto-timer reveal loop as a
# SocketIO background task; players guess independently and everyone who gets it
# right scores (more points for earlier stems). Audio reuse: when a round starts
# we create a normal `games[session_id]` entry from the cached song, so the
# existing /api/stem/<session_id>/<stem> endpoint serves stems unchanged.

rooms = {}        # room_code -> room dict
sid_room = {}     # socket id -> room_code

# Clip length (seconds) is host-configurable. It doubles as the looping clip
# length AND the reveal interval. Bounded by the 90s trimmed cache audio.
MIN_CLIP_LEN = 3
MAX_CLIP_LEN = TRIM_SECONDS   # 90
DEFAULT_CLIP_LEN = 15
ROUND_END_GRACE = True        # give one final timer window after the last stem


def clamp_clip_len(value, default=DEFAULT_CLIP_LEN):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_CLIP_LEN, min(MAX_CLIP_LEN, v))


# Match length (number of rounds) is host-configurable.
MIN_ROUNDS = 1
MAX_ROUNDS = 20
DEFAULT_ROUNDS = 3
INTERMISSION = 6      # seconds between rounds in a multi-round match
GRACE_SECONDS = 5    # "last call" window for final guesses before a round closes
STEM_BREAK = 3       # silent pause between stem reveals (music stops, countdown shows)
TICK = 0.5           # reveal-loop poll interval (lets skips interrupt the wait)

# Competitive scoring (multiplayer)
ORDER_BONUS = [300, 200, 100]   # podium bonus for the 1st/2nd/3rd to title-solve
STREAK_STEP = 100               # extra points per streak level beyond the first
STREAK_CAP = 5                  # streak levels past which the bonus stops growing


def clamp_rounds(value, default=DEFAULT_ROUNDS):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_ROUNDS, min(MAX_ROUNDS, v))


def gen_room_code() -> str:
    """4 unambiguous uppercase letters, unique across active rooms."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I/O to avoid 1/0 confusion
    while True:
        code = "".join(random.choice(alphabet) for _ in range(4))
        if code not in rooms:
            return code


def playable_pool(decades, genres):
    """
    Cached songs (instant-start, no Demucs) that match the filters.
    Joins songs.json (for decade/genre) against the cache index (for video_id).
    """
    idx = _load_index()
    pool = []
    for s in SONGS:
        if decades and s["decade"] not in decades:
            continue
        if genres and s["genre"] not in genres:
            continue
        vid = idx.get(core_title(s["title"]))
        if vid:
            pool.append({"video_id": vid, "title": s["title"], "artist": s["artist"]})
    return pool


def leaderboard(room):
    """Sorted [{name, score, is_host, streak}] for the room."""
    rows = [
        {
            "name": p["name"],
            "score": p["score"],
            "is_host": sid == room["host_sid"],
            "streak": p.get("streak", 0),
        }
        for sid, p in room["players"].items()
    ]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def room_public(room):
    """Serializable lobby/room state pushed to every client."""
    host = room["players"].get(room["host_sid"])
    return {
        "code": room["code"],
        "status": room["status"],
        "timer": room["timer"],
        "filters": room["filters"],
        "filters_locked": room["filters_locked"],
        "host_sid": room["host_sid"],
        "host_name": host["name"] if host else None,
        "rounds": room["rounds_total"],
        "players": leaderboard(room),
    }


def broadcast_room(code):
    room = rooms.get(code)
    if room:
        socketio.emit("room_update", room_public(room), room=code)


def cleanup_room(code):
    room = rooms.pop(code, None)
    if room and room.get("session_id"):
        games.pop(room["session_id"], None)


def end_round(code):
    room = rooms.get(code)
    if not room or room["status"] == "ended":
        return   # idempotent — early-finish and the timer can both try to end it
    room["status"] = "ended"

    # Break the streak of anyone who didn't solve the title this round.
    solvers = set(room.get("title_solvers", []))
    for s, p in room["players"].items():
        if s not in solvers:
            p["streak"] = 0

    room["rounds_played"] = room.get("rounds_played", 0) + 1
    match_over = room["rounds_played"] >= room["rounds_total"]
    session = games.get(room.get("session_id"), {})

    socketio.emit("round_over", {
        "title": session.get("title"),
        "artist": session.get("artist"),
        "leaderboard": leaderboard(room),
        "round": room["rounds_played"],
        "total": room["rounds_total"],
        "match_over": match_over,
        "intermission": INTERMISSION,
    }, room=code)

    # Auto-advance to the next round after a short intermission (unless the match is done).
    if not match_over:
        socketio.start_background_task(intermission_then_next, code, room["match_token"])


def intermission_then_next(code, match_token):
    """Wait out the intermission, then start the next round of the same match."""
    socketio.sleep(INTERMISSION)
    room = rooms.get(code)
    if room and room.get("match_token") == match_token and room["status"] == "ended":
        begin_round(code)


def everyone_finished(room):
    """True if every current player has nailed both the title AND the artist."""
    players = room["players"]
    if not players:
        return False
    for sid in players:
        g = room["round_guessed"].get(sid)
        if not g or not (g["title"] and g["artist"]):
            return False
    return True


def eligible_skippers(room):
    """Players who haven't gotten the title yet — the ones more stems could help."""
    out = []
    for sid in room["players"]:
        g = room["round_guessed"].get(sid)
        if not (g and g["title"]):
            out.append(sid)
    return out


def skip_status(room):
    """{requested, needed} — how many unsolved players have asked to skip."""
    elig = eligible_skippers(room)
    reqs = room.get("skips", set())
    return {"requested": sum(1 for s in elig if s in reqs), "needed": len(elig)}


def skip_satisfied(room):
    """True when every still-guessing player has voted to skip (and there's ≥1)."""
    elig = eligible_skippers(room)
    if not elig:
        return False   # all solved → the early-finish path handles ending
    reqs = room.get("skips", set())
    return all(s in reqs for s in elig)


def wait_interval(code, token):
    """
    Sleep up to one reveal interval, breaking early if everyone votes to skip.
    Resets the skip votes for the new interval. Returns False if the round was
    aborted (room gone / new round / ended), True otherwise.
    """
    room = rooms.get(code)
    if not room:
        return False
    room["skips"] = set()
    socketio.emit("skip_update", skip_status(room), room=code)
    waited = 0.0
    while waited < room["timer"]:
        socketio.sleep(TICK)
        waited += TICK
        room = rooms.get(code)
        if not room or room.get("round_token") != token or room["status"] != "playing":
            return False
        if skip_satisfied(room):
            break
    return True


def finish_round_with_grace(code, token):
    """A short 'last call' window where guesses still count, then close the round."""
    room = rooms.get(code)
    if not room or room.get("round_token") != token or room["status"] != "playing":
        return
    room["status"] = "grace"
    is_final = (room.get("rounds_played", 0) + 1) >= room.get("rounds_total", 1)
    socketio.emit("last_call", {"seconds": GRACE_SECONDS, "is_final": is_final}, room=code)
    waited = 0.0
    while waited < GRACE_SECONDS:
        socketio.sleep(TICK)
        waited += TICK
        room = rooms.get(code)
        if not room or room.get("round_token") != token or room["status"] != "grace":
            return   # ended early (everyone finished) or aborted
    end_round(code)


def stem_break_pause(code, token):
    """A short silent break between stems (clients stop the music + count down).
    Returns False if the round was aborted during the pause."""
    socketio.emit("stem_break", {"seconds": STEM_BREAK}, room=code)
    waited = 0.0
    while waited < STEM_BREAK:
        socketio.sleep(TICK)
        waited += TICK
        room = rooms.get(code)
        if not room or room.get("round_token") != token or room["status"] != "playing":
            return False
    return True


def run_round(code, token):
    """Drip-feed stems (timer or all-skip), give a last-call grace, then end."""
    room = rooms.get(code)
    if not room:
        return
    total = len(room["stems_available"])

    # Reveal stems 2..total
    for i in range(2, total + 1):
        if not wait_interval(code, token):
            return
        # Silent break before the next stem drops
        if not stem_break_pause(code, token):
            return
        room = rooms.get(code)
        if not room or room.get("round_token") != token or room["status"] != "playing":
            return
        room["stems_revealed"] = i
        socketio.emit("stem_revealed", {
            "stems_revealed": i,
            "total_stems": total,
            "timer": room["timer"],
        }, room=code)
        socketio.emit("skip_update", skip_status(room), room=code)

    # Final full-mix window (timer or all-skip), then the last-call grace
    if not wait_interval(code, token):
        return
    finish_round_with_grace(code, token)


def begin_round(code):
    """Pick a cached song, build a games session for audio, kick off the reveal loop."""
    room = rooms.get(code)
    if not room:
        return

    pool = playable_pool(room["filters"]["decades"], room["filters"]["genres"])
    if not pool:
        socketio.emit("room_error",
                      {"message": "No cached songs match those filters. Try removing some."},
                      room=code)
        room["status"] = "lobby"
        broadcast_room(code)
        return

    # No repeats within a session: skip already-played songs. Once the whole
    # filtered pool is exhausted, reshuffle (clear history) so play can continue.
    played = room.setdefault("played", set())
    fresh = [s for s in pool if s["video_id"] not in played]
    if not fresh:
        played.clear()
        fresh = pool

    song = random.choice(fresh)
    played.add(song["video_id"])
    cached = load_cache(song["video_id"])   # pulls stems from R2 if needed
    if not cached:
        socketio.emit("room_error", {"message": "Failed to load that song's audio."}, room=code)
        room["status"] = "lobby"
        broadcast_room(code)
        return

    # Drop the previous round's audio session if any
    if room.get("session_id"):
        games.pop(room["session_id"], None)

    session_id = str(uuid.uuid4())
    games[session_id] = {
        "session_id": session_id,
        "status": "ready",
        "title": cached["title"],
        "artist": cached["artist"],
        "stem_files": cached["stem_files"],
        "guesses": [],
        "artist_guessed": False,
    }

    stems = list(cached["stem_files"].keys())
    room["round_token"] = room.get("round_token", 0) + 1
    token = room["round_token"]
    room.update({
        "session_id": session_id,
        "title": cached["title"],
        "artist": cached["artist"],
        "stems_available": stems,
        "stems_revealed": 1,
        "round_guessed": {},
        "skips": set(),
        "title_solvers": [],   # sids in the order they solved the title (for order bonus)
        "status": "playing",
    })

    socketio.emit("game_started", {
        "session_id": session_id,
        "total_stems": len(stems),
        "stems_revealed": 1,
        "timer": room["timer"],
        "round": room["rounds_played"] + 1,   # 1-based number of the round now starting
        "total_rounds": room["rounds_total"],
        "leaderboard": leaderboard(room),      # carry cumulative scores into the new round
    }, room=code)

    socketio.start_background_task(run_round, code, token)


# ── Socket event handlers ─────────────────────────────────────────────────────

@socketio.on("create_room")
def on_create_room(data):
    sid = request.sid
    name = (data.get("name") or "Host").strip()[:20] or "Host"
    timer = clamp_clip_len(data.get("timer"))
    rounds = clamp_rounds(data.get("rounds"))
    decades = list(data.get("decades") or [])
    genres = list(data.get("genres") or [])

    code = gen_room_code()
    rooms[code] = {
        "code": code,
        "host_sid": sid,
        "players": {sid: {"name": name, "score": 0, "streak": 0}},
        "timer": timer,
        "rounds_total": rounds,
        "rounds_played": 0,
        "match_token": 0,
        "filters": {"decades": decades, "genres": genres},
        "filters_locked": False,
        "status": "lobby",
        "session_id": None,
        "round_token": 0,
        "played": set(),   # video_ids played this session (no repeats)
    }
    sid_room[sid] = code
    sio_join(code)
    emit("joined", {"code": code, "sid": sid, "you_are_host": True})
    broadcast_room(code)


@socketio.on("join_room_req")
def on_join_room(data):
    sid = request.sid
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "Player").strip()[:20] or "Player"

    room = rooms.get(code)
    if not room:
        emit("room_error", {"message": f"Room {code} not found."})
        return

    room["players"][sid] = {"name": name, "score": 0, "streak": 0}
    sid_room[sid] = code
    sio_join(code)
    emit("joined", {"code": code, "sid": sid, "you_are_host": room["host_sid"] == sid})
    broadcast_room(code)


@socketio.on("update_filters")
def on_update_filters(data):
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["status"] != "lobby":
        return
    if room["filters_locked"] and room["host_sid"] != sid:
        return
    room["filters"] = {
        "decades": list(data.get("decades") or []),
        "genres": list(data.get("genres") or []),
    }
    broadcast_room(room["code"])


@socketio.on("set_timer")
def on_set_timer(data):
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["host_sid"] != sid:
        return
    room["timer"] = clamp_clip_len(data.get("timer"), room["timer"])
    broadcast_room(room["code"])


@socketio.on("set_rounds")
def on_set_rounds(data):
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["host_sid"] != sid:
        return
    room["rounds_total"] = clamp_rounds(data.get("rounds"), room["rounds_total"])
    broadcast_room(room["code"])


@socketio.on("toggle_lock")
def on_toggle_lock():
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["host_sid"] != sid:
        return
    room["filters_locked"] = not room["filters_locked"]
    broadcast_room(room["code"])


@socketio.on("start_game")
def on_start_game():
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["host_sid"] != sid or room["status"] != "lobby":
        return
    # Begin a fresh match: reset round counter, cumulative scores, and streaks.
    room["rounds_played"] = 0
    room["match_token"] = room.get("match_token", 0) + 1
    for p in room["players"].values():
        p["score"] = 0
        p["streak"] = 0
    room["status"] = "loading"
    broadcast_room(room["code"])
    socketio.start_background_task(begin_round, room["code"])


@socketio.on("skip_request")
def on_skip_request(data):
    """A player votes to skip to the next stem. When everyone still guessing
    has voted, the reveal loop advances early (handled in wait_interval)."""
    sid = request.sid
    code = sid_room.get(sid)
    room = rooms.get(code)
    if not room or room["status"] != "playing":
        return
    skips = room.setdefault("skips", set())
    if data.get("skip"):
        skips.add(sid)
    else:
        skips.discard(sid)
    socketio.emit("skip_update", skip_status(room), room=code)


@socketio.on("guess")
def on_guess(data):
    sid = request.sid
    code = sid_room.get(sid)
    room = rooms.get(code)
    # Guesses count during play AND the last-call grace window.
    if not room or room["status"] not in ("playing", "grace"):
        return
    session = games.get(room["session_id"])
    if not session:
        return

    guess = (data.get("guess") or "").strip()
    if not guess:
        return

    g = guess.lower()
    title = session["title"].lower()
    artist = session["artist"].lower()
    stems_revealed = room["stems_revealed"]
    base = max(0, 1000 - (stems_revealed - 1) * 200)

    player = room["players"].get(sid)
    if not player:
        return
    got = room["round_guessed"].setdefault(sid, {"title": False, "artist": False})

    result, points = "wrong", 0
    order_bonus = streak_bonus = 0
    rank = -1
    if is_match(g, title) and not got["title"]:
        got["title"] = True
        result = "correct"

        # Order bonus: reward beating others to the title (multiplayer only).
        rank = len(room["title_solvers"])
        room["title_solvers"].append(sid)
        if len(room["players"]) > 1 and rank < len(ORDER_BONUS):
            order_bonus = ORDER_BONUS[rank]

        # Streak bonus: consecutive rounds in which you've solved the title.
        player["streak"] = player.get("streak", 0) + 1
        streak_bonus = min(player["streak"] - 1, STREAK_CAP) * STREAK_STEP

        points = base + order_bonus + streak_bonus
    elif is_match(g, artist) and not got["artist"]:
        result, points = "artist", base // 2
        got["artist"] = True

    if points:
        player["score"] += points
        first = result == "correct" and rank == 0 and len(room["players"]) > 1
        # Announce that someone scored — never leak the actual guess text
        socketio.emit("player_guessed", {
            "name": player["name"],
            "what": "title" if result == "correct" else "artist",
            "first": first,
            "streak": player.get("streak", 0) if result == "correct" else 0,
        }, room=code)
        socketio.emit("scores_update", {"leaderboard": leaderboard(room)}, room=code)

    # Private result back to just this guesser
    emit("guess_result", {
        "result": result,
        "points": points,
        "base": base if result != "wrong" else 0,
        "order_bonus": order_bonus,
        "streak_bonus": streak_bonus,
        "streak": player.get("streak", 0) if result == "correct" else 0,
        "title": session["title"] if result != "wrong" else None,
        "artist": session["artist"] if result != "wrong" else None,
    })

    # Early finish: if everyone has both title + artist, end now instead of
    # waiting out the timer. (end_round is idempotent vs. the reveal-loop timer.)
    if points and everyone_finished(room):
        end_round(code)


@socketio.on("abort_game")
def on_abort_game():
    """Host ends the in-progress game/match and returns everyone to the lobby."""
    sid = request.sid
    code = sid_room.get(sid)
    room = rooms.get(code)
    if not room or room["host_sid"] != sid or room["status"] == "lobby":
        return
    # Invalidate any running round / intermission / grace background tasks.
    room["round_token"] = room.get("round_token", 0) + 1
    room["match_token"] = room.get("match_token", 0) + 1
    if room.get("session_id"):
        games.pop(room["session_id"], None)
        room["session_id"] = None
    room["status"] = "lobby"
    room["rounds_played"] = 0
    socketio.emit("game_aborted", {}, room=code)
    broadcast_room(code)


@socketio.on("play_again")
def on_play_again():
    sid = request.sid
    room = rooms.get(sid_room.get(sid))
    if not room or room["host_sid"] != sid:
        return
    if room.get("session_id"):
        games.pop(room["session_id"], None)
        room["session_id"] = None
    room["status"] = "lobby"
    broadcast_room(room["code"])


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    code = sid_room.pop(sid, None)
    room = rooms.get(code)
    if not room:
        return
    was_host = room["host_sid"] == sid
    room["players"].pop(sid, None)
    sio_leave(code)

    if not room["players"]:
        cleanup_room(code)
        return
    if was_host:
        room["host_sid"] = next(iter(room["players"]))
    broadcast_room(code)


if __name__ == "__main__":
    bootstrap_index()  # restore the canonical core_title -> video_id map (R2 or local)
    port = int(os.environ.get("PORT", 5001))   # Render injects $PORT; 5001 locally
    debug = os.environ.get("RIFFDLE_DEBUG", "1") != "0"
    socketio.run(app, host="0.0.0.0", port=port, debug=debug,
                 use_reloader=False, allow_unsafe_werkzeug=True)
