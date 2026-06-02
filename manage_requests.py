#!/usr/bin/env python3
"""
manage_requests.py — review user song requests and curate the cache queue.

Two lists live in R2:
  • song_requests.json — raw user submissions (deduped, with a count)
  • song_queue.json    — songs YOU picked to cache next

Typical flow (run on your Mac):
  ./venv/bin/python manage_requests.py            # interactive menu
Promote the requests you like into the queue, then cache them however you
normally do (search them locally, or feed the queue into precache).

Interactive commands:
  add <nums...>     move those requests into the queue
  del <nums...>     delete those requests (ignore them)
  q <text>          add an arbitrary song straight to the queue (your own pick)
  qdel <nums...>    remove items from the queue
  refresh           re-read both lists from R2
  quit              exit
"""

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

REQUESTS_KEY = "song_requests.json"
QUEUE_KEY = "song_queue.json"


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


def norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Must match the chips in songs.json / the frontend
VALID_DECADES = {"70s", "80s", "90s", "00s", "10s", "20s"}
VALID_GENRES = {"pop", "rock", "hiphop", "randb", "electronic"}


def tags_str(q):
    d, g = q.get("decade"), q.get("genre")
    return f"  [{d or '?'}/{g or '?'}]" if (d or g) else "  [untagged]"


def show(requests, queue):
    print("\n" + "=" * 60)
    print(f"USER REQUESTS ({len(requests)})  — sorted by popularity")
    print("=" * 60)
    if not requests:
        print("  (none)")
    for i, r in enumerate(requests, 1):
        cnt = r.get("count", 1)
        star = f"  ×{cnt}" if cnt > 1 else ""
        print(f"  [{i:2}] {r.get('query', '?')}{star}   (last {r.get('last', '?')})")

    print("\n" + "-" * 60)
    print(f"CACHE QUEUE ({len(queue)})  — songs you've picked to add")
    print("-" * 60)
    if not queue:
        print("  (empty)")
    for i, q in enumerate(queue, 1):
        print(f"  [{i:2}] {q.get('query', '?')}{tags_str(q)}")
    print()


def pick(nums, items):
    """Map 1-based number strings to list items, skipping out-of-range."""
    out = []
    for n in nums:
        if n.isdigit():
            idx = int(n) - 1
            if 0 <= idx < len(items):
                out.append(items[idx])
    return out


def main():
    requests = get_json(REQUESTS_KEY, [])
    queue = get_json(QUEUE_KEY, [])
    if not isinstance(requests, list):
        requests = []
    if not isinstance(queue, list):
        queue = []
    queue_keys = {norm(q.get("query", "")) for q in queue}

    show(requests, queue)
    print("Commands:")
    print("  add <#...> <decade> <genre>   promote requests to the queue, tagged")
    print("  del <#...>                    delete user requests")
    print("  q <decade> <genre> <song>     add your own song to the queue")
    print("  tag <#> <decade> <genre>      set decade/genre on a queued song")
    print("  qdel <#...>                   remove songs from the queue")
    print("  refresh | quit")
    print(f"  decades: {' '.join(sorted(VALID_DECADES))}   genres: {' '.join(sorted(VALID_GENRES))}")
    print("  (decade/genre are optional — untagged songs still cache, just won't")
    print("   show under Random filters)")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("quit", "exit", "q!"):
            break

        elif cmd == "refresh":
            requests = get_json(REQUESTS_KEY, []) or []
            queue = get_json(QUEUE_KEY, []) or []
            queue_keys = {norm(q.get("query", "")) for q in queue}
            show(requests, queue)

        elif cmd == "add":
            # Trailing "<decade> <genre>" tokens tag every promoted song.
            decade = genre = None
            nums = args
            if len(args) >= 2 and args[-2] in VALID_DECADES and args[-1] in VALID_GENRES:
                decade, genre, nums = args[-2], args[-1], args[:-2]
            chosen = pick(nums, requests)
            if not chosen:
                print("  nothing matched. usage: add <#...> <decade> <genre>")
                continue
            if not decade:
                print("  ⚠ no decade/genre given — promoting untagged "
                      "(won't show under Random filters).")
            added = 0
            now = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
            for r in chosen:
                k = norm(r.get("query", ""))
                if k and k not in queue_keys:
                    item = {"query": r["query"], "added": now}
                    if decade:
                        item["decade"] = decade
                    if genre:
                        item["genre"] = genre
                    queue.append(item)
                    queue_keys.add(k)
                    added += 1
            # remove the promoted requests from the request list
            chosen_keys = {norm(r.get("query", "")) for r in chosen}
            requests = [r for r in requests if norm(r.get("query", "")) not in chosen_keys]
            put_json(QUEUE_KEY, queue)
            put_json(REQUESTS_KEY, requests)
            print(f"  → promoted {added} to the queue.")
            show(requests, queue)

        elif cmd == "tag":
            # tag <#> <decade> <genre>
            if len(args) != 3 or not args[0].isdigit():
                print("  usage: tag <#> <decade> <genre>")
                continue
            d, g = args[1], args[2]
            if d not in VALID_DECADES or g not in VALID_GENRES:
                print(f"  invalid. decades: {' '.join(sorted(VALID_DECADES))} | "
                      f"genres: {' '.join(sorted(VALID_GENRES))}")
                continue
            chosen = pick([args[0]], queue)
            if not chosen:
                print("  no such queue item.")
                continue
            chosen[0]["decade"] = d
            chosen[0]["genre"] = g
            put_json(QUEUE_KEY, queue)
            print(f"  → tagged '{chosen[0]['query']}' as {d}/{g}.")
            show(requests, queue)

        elif cmd == "del":
            chosen = pick(args, requests)
            chosen_keys = {norm(r.get("query", "")) for r in chosen}
            requests = [r for r in requests if norm(r.get("query", "")) not in chosen_keys]
            put_json(REQUESTS_KEY, requests)
            print(f"  → deleted {len(chosen)} request(s).")
            show(requests, queue)

        elif cmd == "q":
            # Optional leading "<decade> <genre>" tags the song.
            decade = genre = None
            rest = args
            if len(args) >= 3 and args[0] in VALID_DECADES and args[1] in VALID_GENRES:
                decade, genre, rest = args[0], args[1], args[2:]
            text = " ".join(rest).strip()
            if not text:
                print("  usage: q <decade> <genre> <song name & artist>")
                continue
            if not decade:
                print("  ⚠ no decade/genre given — adding untagged "
                      "(won't show under Random filters).")
            k = norm(text)
            if k and k not in queue_keys:
                item = {"query": text, "added": time.strftime("%Y-%m-%d %H:%M", time.gmtime())}
                if decade:
                    item["decade"] = decade
                if genre:
                    item["genre"] = genre
                queue.append(item)
                queue_keys.add(k)
                put_json(QUEUE_KEY, queue)
                print(f"  → added '{text}' to the queue.")
            else:
                print("  (already in queue)")
            show(requests, queue)

        elif cmd == "qdel":
            chosen = pick(args, queue)
            chosen_keys = {norm(q.get("query", "")) for q in chosen}
            queue = [q for q in queue if norm(q.get("query", "")) not in chosen_keys]
            queue_keys = {norm(q.get("query", "")) for q in queue}
            put_json(QUEUE_KEY, queue)
            print(f"  → removed {len(chosen)} from the queue.")
            show(requests, queue)

        else:
            print("  unknown command. try: add | del | q | tag | qdel | refresh | quit")

    print("Bye.")


if __name__ == "__main__":
    main()
