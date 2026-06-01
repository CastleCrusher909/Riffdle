#!/usr/bin/env python3
"""
precache.py — warm the Riffdle audio cache from songs.json overnight.

Usage:
    python precache.py                        # all songs
    python precache.py --decades 80s 90s      # filter by decade
    python precache.py --genres pop rock      # filter by genre
    python precache.py --decades 80s --genres pop  # both filters

Progress is appended to precache.log so you can review what happened.
Safe to interrupt and re-run — already-cached songs are skipped instantly.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from difflib import SequenceMatcher
from pathlib import Path

import yt_dlp

# ── Paths (mirrors app.py) ────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
AUDIO_DIR = BASE_DIR / "audio"
CACHE_DIR = AUDIO_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

INDEX_FILE = CACHE_DIR / "song_index.json"
SONGS_FILE = BASE_DIR / "backend" / "songs.json"
LOG_FILE   = BASE_DIR / "precache.log"

MODEL        = "htdemucs_ft"
TRIM_SECONDS = 90
SILENCE_DB   = -50.0

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Helpers (copied from app.py, no Flask dependency) ────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def core_title(title: str) -> str:
    t = re.sub(r"[\(\[].*?[\)\]]", "", title)
    if " - " in t:
        t = t.split(" - ", 1)[1]
    return _norm(t)


def clean_title(title: str, artist: str) -> str:
    for sep in (" - ", ": "):
        if title.lower().startswith(artist.lower() + sep):
            return title[len(artist) + len(sep):]
    return title


def _load_index() -> dict:
    return json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else {}


def _save_index(index: dict):
    INDEX_FILE.write_text(json.dumps(index, indent=2))


def rebuild_index() -> dict:
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


def is_cached(title: str, artist: str) -> bool:
    """Check if a song is already in the cache via index lookup."""
    query_ct = core_title(title)
    idx = _load_index()
    if query_ct in idx:
        vid = idx[query_ct]
        cache_path = CACHE_DIR / vid
        meta_file  = cache_path / "meta.json"
        if meta_file.exists():
            return True
    # Fuzzy fallback
    for key, vid in idx.items():
        if SequenceMatcher(None, query_ct, key).ratio() >= 0.9:
            cache_path = CACHE_DIR / vid
            if (cache_path / "meta.json").exists():
                return True
    return False


def search_youtube(title: str, artist: str) -> str | None:
    """Return the best YouTube URL for a song."""
    query = f"{title} {artist} official audio"
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            return None
        return f"https://www.youtube.com/watch?v={entries[0]['id']}"


def download_audio(url: str, work_dir: Path) -> dict:
    out_template = str(work_dir / "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id  = info.get("id")
        raw_title = info.get("track") or info.get("title", "Unknown Title")
        artist    = (info.get("artist") or info.get("uploader") or
                     info.get("channel") or "Unknown Artist")
        if artist.endswith(" - Topic"):
            artist = artist[:-len(" - Topic")]
        title = re.sub(
            r"\s*[\(\[][^\)\]]*(?:official|lyric|remaster|4k|hd|ft\.|feat\.|\d{4})[^\)\]]*[\)\]]",
            "", raw_title, flags=re.IGNORECASE,
        ).strip().rstrip("-–—").strip()
        title = clean_title(title, artist)
    return {"video_id": video_id, "title": title, "artist": artist,
            "wav_path": work_dir / "audio.wav"}


def is_silent(mp3_path: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-i", mp3_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"max_volume: ([-\d.]+) dB", result.stderr)
    if not match:
        return False
    return float(match.group(1)) < SILENCE_DB


def separate_stems(wav_path: Path, work_dir: Path) -> dict:
    stems_out = work_dir / "stems"
    stems_out.mkdir(parents=True, exist_ok=True)

    trimmed_path = str(wav_path.parent / "trimmed.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), "-t", str(TRIM_SECONDS), trimmed_path],
        check=True, capture_output=True,
    )

    cmd = [
        sys.executable, "-m", "demucs",
        "-n", MODEL, "-d", "mps",
        "-o", str(stems_out),
        trimmed_path,
    ]
    subprocess.run(cmd, check=True)

    model_out = stems_out / MODEL / "trimmed"
    stem_map  = {
        "drums":  model_out / "drums.wav",
        "bass":   model_out / "bass.wav",
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
            stem_files[name] = mp3_file

    if not stem_files:
        stem_files = {name: wf.with_suffix(".mp3") for name, wf in stem_map.items()}

    return stem_files


def save_to_cache(video_id: str, title: str, artist: str, stem_files: dict):
    cache_path = CACHE_DIR / video_id
    cache_path.mkdir(parents=True, exist_ok=True)
    for stem_name, src_path in stem_files.items():
        dst = cache_path / f"{stem_name}.mp3"
        if not dst.exists():
            shutil.copy2(src_path, dst)
    (cache_path / "meta.json").write_text(
        json.dumps({"title": title, "artist": artist, "stems": list(stem_files.keys())})
    )
    index = _load_index()
    index[core_title(title)] = video_id
    _save_index(index)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-cache Riffdle songs overnight.")
    parser.add_argument("--decades", nargs="*", help="e.g. 80s 90s")
    parser.add_argument("--genres",  nargs="*", help="e.g. pop rock")
    args = parser.parse_args()

    songs = json.loads(SONGS_FILE.read_text())
    if args.decades:
        songs = [s for s in songs if s["decade"] in args.decades]
    if args.genres:
        songs = [s for s in songs if s["genre"] in args.genres]

    log.info("=" * 60)
    log.info(f"Starting precache run — {len(songs)} songs to process")
    if args.decades:
        log.info(f"  decades filter: {args.decades}")
    if args.genres:
        log.info(f"  genres filter:  {args.genres}")
    log.info("=" * 60)

    log.info("Rebuilding index from existing cache…")
    rebuild_index()

    done = skipped = failed = 0

    for i, song in enumerate(songs, 1):
        title  = song["title"]
        artist = song["artist"]
        label  = f"[{i}/{len(songs)}] {title} — {artist}"

        if is_cached(title, artist):
            log.info(f"SKIP     {label}")
            skipped += 1
            continue

        log.info(f"START    {label}")

        work_dir = Path(tempfile.mkdtemp(prefix="riffdle_"))
        try:
            url = search_youtube(title, artist)
            if not url:
                log.warning(f"NOTFOUND {label}  (no YouTube result)")
                failed += 1
                continue

            log.info(f"         URL: {url}")
            dl = download_audio(url, work_dir)
            log.info(f"         Downloaded: {dl['title']} — {dl['artist']}")

            stem_files = separate_stems(dl["wav_path"], work_dir)
            log.info(f"         Stems: {list(stem_files.keys())}")

            save_to_cache(dl["video_id"], dl["title"], dl["artist"], stem_files)
            log.info(f"OK       {label}")
            done += 1

        except Exception as exc:
            log.error(f"FAIL     {label}  → {exc}")
            failed += 1

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    log.info("=" * 60)
    log.info(f"Done.  cached={done}  skipped={skipped}  failed={failed}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
