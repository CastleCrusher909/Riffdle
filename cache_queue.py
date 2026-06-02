#!/usr/bin/env python3
"""
cache_queue.py — cache every song in the R2 queue, then clear what succeeds.

Reads song_queue.json from R2 and, for each entry, searches YouTube,
downloads (residential Mac IP), runs local Demucs, and uploads the stems +
meta to R2 so the song goes live on the hosted site. Songs that cache
successfully are removed from the queue; failures stay in the queue (logged)
so you can fix and re-run.

Run this on your Mac (it needs the full local deps: demucs, ffmpeg, yt-dlp):
    ./venv/bin/python cache_queue.py
    ./venv/bin/python cache_queue.py --limit 5     # only the first 5
    ./venv/bin/python cache_queue.py --dry-run     # list what would run, no work

Curate the queue first with manage_requests.py.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import yt_dlp

# Reuse the battle-tested download + separation pipeline from precache.py
from precache import download_audio, separate_stems, core_title

load_dotenv()

QUEUE_KEY = "song_queue.json"
INDEX_KEY = "song_index.json"


def make_client():
    try:
        account_id = os.environ["R2_ACCOUNT_ID"].strip()
        access_key = os.environ["R2_ACCESS_KEY_ID"].strip()
        secret_key = os.environ["R2_SECRET_ACCESS_KEY"].strip()
    except KeyError as e:
        sys.exit(f"Missing env var {e}. Make sure .env has your R2 credentials.")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    return client, os.environ.get("R2_BUCKET", "riffdle-cache").strip()


s3, BUCKET = make_client()


def get_json(key, default):
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except (ClientError, ValueError, KeyError):
        return default


def put_json(key, obj):
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=json.dumps(obj, indent=2).encode("utf-8"),
                  ContentType="application/json")


def upload_file(path: Path, key: str):
    ctype = "audio/mpeg" if path.suffix == ".mp3" else "application/json"
    s3.upload_file(str(path), BUCKET, key, ExtraArgs={"ContentType": ctype})


def search_youtube_url(query: str) -> str | None:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query} official audio", download=False)
        entries = info.get("entries") or []
        return f"https://www.youtube.com/watch?v={entries[0]['id']}" if entries else None


def cache_one(query: str, index: dict) -> str:
    """Download + separate + upload one song. Returns one of:
    'cached', 'exists', or 'fail:<reason>'. Mutates `index` on success."""
    url = search_youtube_url(query)
    if not url:
        return "fail:no YouTube result"

    work_dir = Path(tempfile.mkdtemp(prefix="riffdle_q_"))
    try:
        dl = download_audio(url, work_dir)
        video_id, title, artist = dl["video_id"], dl["title"], dl["artist"]
        print(f"         → {title} — {artist}  ({video_id})")

        if core_title(title) in index:
            return "exists"

        stem_files = separate_stems(dl["wav_path"], work_dir)
        if not stem_files:
            return "fail:no stems produced"

        # Upload each stem to R2 under {video_id}/{stem}.mp3
        for name, src in stem_files.items():
            upload_file(Path(src), f"{video_id}/{name}.mp3")

        # meta.json
        meta_path = work_dir / "meta.json"
        meta_path.write_text(json.dumps({
            "title": title, "artist": artist, "stems": list(stem_files.keys()),
        }))
        upload_file(meta_path, f"{video_id}/meta.json")

        # Update the canonical R2 index immediately so it's never lost on a crash
        index[core_title(title)] = video_id
        put_json(INDEX_KEY, index)
        return "cached"

    except Exception as exc:
        return f"fail:{exc}"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Cache the Riffdle song queue from R2.")
    parser.add_argument("--limit", type=int, help="only process the first N queued songs")
    parser.add_argument("--dry-run", action="store_true", help="list the queue, do nothing")
    args = parser.parse_args()

    queue = get_json(QUEUE_KEY, [])
    if not isinstance(queue, list):
        queue = []
    if not queue:
        print("Queue is empty. Add songs with manage_requests.py first.")
        return

    todo = queue[: args.limit] if args.limit else list(queue)

    print(f"Queue has {len(queue)} song(s); processing {len(todo)}.")
    if args.dry_run:
        for i, q in enumerate(todo, 1):
            print(f"  [{i}] {q.get('query', '?')}")
        print("(dry run — nothing cached)")
        return

    index = get_json(INDEX_KEY, {})
    if not isinstance(index, dict):
        index = {}

    cached = exists = failed = 0
    for i, entry in enumerate(todo, 1):
        query = entry.get("query", "").strip()
        if not query:
            continue
        print(f"\n[{i}/{len(todo)}] {query}")
        result = cache_one(query, index)

        if result in ("cached", "exists"):
            # Remove this entry from the live queue and persist (resumable)
            queue = [q for q in queue if q.get("query") != entry.get("query")]
            put_json(QUEUE_KEY, queue)
            if result == "cached":
                print("   ✅ cached & live")
                cached += 1
            else:
                print("   ↩ already cached — removed from queue")
                exists += 1
        else:
            print(f"   ❌ {result}  (left in queue)")
            failed += 1

    print("\n" + "=" * 50)
    print(f"Done.  cached={cached}  already-cached={exists}  failed={failed}")
    print(f"Queue now has {len(queue)} song(s) remaining.")
    print("=" * 50)


if __name__ == "__main__":
    main()
