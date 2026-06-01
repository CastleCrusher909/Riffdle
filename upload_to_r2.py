#!/usr/bin/env python3
"""
upload_to_r2.py — upload the local audio cache to Cloudflare R2.

Safe to re-run: already-uploaded files are skipped.
Run this once to seed R2, then app.py keeps it in sync automatically.
"""

import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

BASE_DIR  = Path(__file__).parent
CACHE_DIR = BASE_DIR / "audio" / "cache"

ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"].strip()
ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"].strip()
SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"].strip()
BUCKET     = os.environ["R2_BUCKET"].strip()

s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto",
)


def already_uploaded(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


uploaded = skipped = failed = 0

for cache_path in sorted(CACHE_DIR.iterdir()):
    if not cache_path.is_dir():
        continue

    meta_file = cache_path / "meta.json"
    if not meta_file.exists():
        continue

    video_id = cache_path.name
    files = list(cache_path.glob("*.mp3")) + [meta_file]

    print(f"\n{video_id}/")
    for f in files:
        key = f"{video_id}/{f.name}"
        if already_uploaded(key):
            print(f"  SKIP  {f.name}")
            skipped += 1
            continue
        try:
            content_type = "audio/mpeg" if f.suffix == ".mp3" else "application/json"
            s3.upload_file(str(f), BUCKET, key, ExtraArgs={"ContentType": content_type})
            print(f"  OK    {f.name}")
            uploaded += 1
        except Exception as e:
            print(f"  FAIL  {f.name}  → {e}")
            failed += 1

# Also upload song_index.json
index_file = CACHE_DIR / "song_index.json"
if index_file.exists():
    s3.upload_file(str(index_file), BUCKET, "song_index.json",
                   ExtraArgs={"ContentType": "application/json"})
    print("\nUploaded song_index.json")

print(f"\nDone.  uploaded={uploaded}  skipped={skipped}  failed={failed}")
