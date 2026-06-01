#!/usr/bin/env python3
"""
repair_cache.py — clean up messy titles and channel-name artists in meta.json files.

Uses songs.json as ground truth: if a cached song can be matched back to the
seed list, the title and artist are replaced with the canonical version.
For unmatched entries, best-effort cleaning is applied (strip junk from titles).

Safe to run multiple times. Run it then restart the Flask server.
"""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR   = Path(__file__).parent
CACHE_DIR  = BASE_DIR / "audio" / "cache"
INDEX_FILE = CACHE_DIR / "song_index.json"
SONGS_FILE = BASE_DIR / "backend" / "songs.json"


# ── Helpers (mirrors app.py) ──────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def core_title(title: str) -> str:
    t = re.sub(r"[\(\[].*?[\)\]]", "", title)
    if " - " in t:
        t = t.split(" - ", 1)[1]
    return _norm(t)


def strip_junk(title: str) -> str:
    """Strip all (…) / […] suffixes and common audio-quality tags from a title."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    t = t.rstrip("-–—†").strip()
    # Strip trailing pipe-separated junk  "Title | Something"
    if " | " in t:
        t = t.split(" | ")[0].strip()
    return t


def extract_artist_from_title(title: str) -> tuple[str | None, str]:
    """
    If title is 'Artist - Song Title', return (artist, song_title).
    Otherwise return (None, title).
    Guards against false splits like 'D.A.N.C.E. - †'.
    """
    separators = [" - ", "- "]
    for sep in separators:
        if sep in title:
            left, right = title.split(sep, 1)
            left  = left.strip()
            right = right.strip()
            # Right side must be at least 2 meaningful chars
            if len(_norm(right)) >= 2:
                return left, right
    return None, title


def best_effort_clean(title: str, artist: str) -> tuple[str, str]:
    """Apply heuristics to produce a cleaner (title, artist) pair."""
    embedded_artist, remaining = extract_artist_from_title(title)
    if embedded_artist:
        clean_t = strip_junk(remaining)
        return clean_t, embedded_artist
    return strip_junk(title), artist


# ── Build ground-truth lookup from songs.json ─────────────────────────────────

songs = json.loads(SONGS_FILE.read_text())
ground_truth: dict[str, dict] = {}   # core_title_key → {title, artist}
for s in songs:
    key = core_title(s["title"])
    ground_truth[key] = {"title": s["title"], "artist": s["artist"]}


def find_ground_truth(title: str) -> dict | None:
    """Try exact core_title match, then fuzzy fallback."""
    key = core_title(title)
    if key in ground_truth:
        return ground_truth[key]
    # Fuzzy — handles minor spelling differences
    best_score, best_match = 0.0, None
    for gt_key, gt_val in ground_truth.items():
        score = SequenceMatcher(None, key, gt_key).ratio()
        if score > best_score:
            best_score, best_match = score, gt_val
    if best_score >= 0.85:
        return best_match
    return None


# ── Process each cache directory ──────────────────────────────────────────────

fixed = skipped = errors = 0

for cache_path in sorted(CACHE_DIR.iterdir()):
    meta_file = cache_path / "meta.json"
    if not cache_path.is_dir() or not meta_file.exists():
        continue

    try:
        meta = json.loads(meta_file.read_text())
    except json.JSONDecodeError:
        print(f"ERROR  {cache_path.name}  (bad JSON)")
        errors += 1
        continue

    old_title  = meta.get("title", "")
    old_artist = meta.get("artist", "")

    gt = find_ground_truth(old_title)
    if gt:
        new_title  = gt["title"]
        new_artist = gt["artist"]
        source = "songs.json"
    else:
        new_title, new_artist = best_effort_clean(old_title, old_artist)
        source = "heuristic"

    if new_title == old_title and new_artist == old_artist:
        skipped += 1
        continue

    print(f"FIX [{source}]  {cache_path.name}")
    print(f"  title:  {old_title!r}  →  {new_title!r}")
    if new_artist != old_artist:
        print(f"  artist: {old_artist!r}  →  {new_artist!r}")

    meta["title"]  = new_title
    meta["artist"] = new_artist
    meta_file.write_text(json.dumps(meta))
    fixed += 1


# ── Rebuild index ─────────────────────────────────────────────────────────────

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

INDEX_FILE.write_text(json.dumps(index, indent=2))
print(f"\nIndex rebuilt: {len(index)} entries")
print(f"Done.  fixed={fixed}  unchanged={skipped}  errors={errors}")
