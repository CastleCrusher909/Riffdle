# Riffdle — Handoff / Developer Guide

A music "guess the song" game (Heardle-style, but with **stems**). A song is split
into 4 instrument stems (drums / bass / melody / vocals) via Demucs; players guess
the title (and artist) as the stems reveal one at a time. Single-player **and** full
real-time multiplayer, plus a daily challenge.

- **Live:** https://riffdle.onrender.com
- **Repo:** github.com/CastleCrusher909/Riffdle (branch `main`, auto-deploys on push)
- **Local:** `cd` into the project, then `./run.sh` → http://localhost:5001

---

## Architecture (3 pieces)

1. **Web app** — Flask + Flask-SocketIO (`backend/app.py`). Single process, 1 worker,
   threading async mode (no eventlet/gevent). Serves the static frontend + the API +
   the multiplayer sockets. Port 5001 locally, `$PORT` on Render.
2. **Storage** — Cloudflare R2 bucket `riffdle-cache` holds all stem MP3s plus a few
   JSON index/catalog/stats objects (see "R2 objects" below). ~130 songs cached.
3. **Separation** — local Mac **Demucs** (MPS GPU) is the active path for adding songs.
   A **Modal** GPU function (`modal_separate.py`) is deployed but dormant.

The hosted site runs **cache-only** (`RIFFDLE_CACHE_ONLY=1`): it never contacts YouTube
at all (datacenter IPs get bot-blocked). On a cache miss it returns `not_cached`
immediately after the direct video-id→R2 lookup — see the cache-only quirk below. New
songs are added by the owner running locally on a Mac (residential IP), which uploads to
R2 → instantly live on the host.

---

## Files

```
backend/app.py        Flask routes, SocketIO multiplayer, R2, yt-dlp, cache, matching, daily, stats
backend/songs.json    ~120-song seed list (title/artist/decade/genre) — used for Random filters
frontend/index.html   All screens (landing, mp setup/lobby/result, loading, game, result)
frontend/app.js       Single-player + Web Audio engine + autocomplete + share + daily + stats
frontend/mp.js        Multiplayer client (rooms, streaks, skip, breaks, leaderboard)
frontend/style.css    Dark-purple theme (redesigned: frosted cards, SVG icons, animations)
frontend/favicon.svg  Purple guitar (browser tab); favicon.ico + PNGs for Google/app icon
frontend/og-image.png Social share preview (1200x630)
frontend/robots.txt, sitemap.xml, google…​.html   SEO + Search Console verification
Dockerfile            Render image (python:3.11-slim + ffmpeg + light deps + nightly yt-dlp)
requirements-server.txt  LIGHT hosting deps (no torch/demucs)
requirements.txt      Full local deps (flask, demucs, torchcodec, modal, etc.)
modal_separate.py     Modal GPU separation function (dormant)
precache.py           Batch-cache backend/songs.json locally (no R2 upload by itself)
cache_queue.py        Cache the curated R2 queue → R2 (the main "add songs" tool)
manage_requests.py    Review user song requests + build the cache queue (interactive CLI)
upload_to_r2.py       One-time R2 seeding from local cache
repair_cache.py       Clean messy cached titles/artists
run.sh                Local launch (port 5001)
MANAGING_SONGS.md     Owner guide for the request→queue→cache workflow (gitignored)
```

---

## Running & deploying

- **Local:** `./run.sh`. Restart manually after any `backend/app.py` change (`use_reloader=False`).
- **Deploy:** push to `main` → Render auto-deploys. (If auto-deploy stops firing, the
  GitHub→Render webhook is likely missing — reconnect the repo via the Render GitHub App.)
- **Render env vars:** `RIFFDLE_CACHE_ONLY=1`, `RIFFDLE_DEBUG=0`, `R2_*` (4 vars),
  plus `RIFFDLE_USE_MODAL=1` + `MODAL_TOKEN_*` (these are dead under cache-only — harmless).
- **Render secret file:** `cookies.txt` at `/etc/secrets/cookies.txt` (app copies to
  `/tmp/yt_cookies.txt` since the mount is read-only). Needed for yt-dlp search/metadata.
- **Diagnostics:** `GET /api/health`.

---

## R2 objects

```
{video_id}/{stem}.mp3   the 4 stems for a song
{video_id}/meta.json    {title, artist, stems}
song_index.json         {core_title: video_id}  (boot index; resilience)
catalog.json            [{video_id, title, artist, decade, genre}]  ← drives search + Random
song_requests.json      [{key, query, count, first, last}]  user song requests
song_queue.json         [{query, decade, genre, added}]  owner's curated to-cache list
song_stats.json         {video_id: {plays, solves, stem_sum}}  per-song difficulty
catalog/                (none — historical note: stems live under {video_id}/)
```

---

## Adding songs (owner workflow)

All on the Mac, from the project dir (full deps installed in `venv/`):

1. **Curate:** `./venv/bin/python manage_requests.py`
   - shows user requests (sorted by popularity) + the queue
   - `add <#...> <decade> <genre>` promote requests; `q <decade> <genre> <song>` add your own;
     `tag <#> <decade> <genre>`; `del`, `qdel`, `refresh`, `quit`
   - decades: 70s 80s 90s 00s 10s 20s · genres: pop rock hiphop randb electronic
2. **Cache:** `./venv/bin/python cache_queue.py` (`--dry-run`, `--limit N` available)
   - searches YouTube → downloads → Demucs → uploads stems+meta to R2 → updates
     index + catalog → removes from queue. Resumable. ~1 min/song.

Songs go live on the host within ~30s (catalog has a 30s server cache) + a client refresh.
Also works to add a one-off by running the app locally (no CACHE_ONLY) and searching it.

> Note: yt-dlp sometimes resolves the artist to the uploader/channel name. Fix the
> entry's title/artist directly in R2 (`catalog.json` + `{vid}/meta.json`) if needed.

---

## Features (what's built)

- **Single-player:** search/autocomplete (cached catalog), Random (filterable), stem-by-stem
  reveal, scrubbable custom audio player (Web Audio API on one AudioContext).
- **Multiplayer:** rooms (4-letter codes + `?room=CODE` links), host-only lobby settings,
  N-round matches with auto-advance intermissions, **3s silent break between stems**,
  last-call grace, skip-stem voting, streak/order bonuses, live leaderboard, host abort.
- **Daily Challenge:** same song for everyone each day (deterministic by UTC date from
  `DAILY_LAUNCH` 2026-06-03; no repeats until the catalog cycles). One play/day (locked,
  re-opening shows your saved result). Heardle-style emoji-grid share + countdown.
- **Daily streak:** localStorage `riffdle-streak` `{current,best,lastDay}` (lastDay = the
  daily *number* last completed). Flame badge on the home screen; "🔥 X day streak!" + best
  on the daily result; once played today the daily button shows "Come back tomorrow" + a
  live countdown (stays clickable to review the saved result). Consecutive-day increment,
  resets after a missed day. Frontend-only (`app.js`).
- **Hint system (single-player only):** up to 3 hints/round — 1st free, 2nd −75 pts, 3rd
  −150 pts. Reveals least→most helpful: title word count → artist initial → title initial.
  First hint is instant w/ a "Free hint!" toast; paid hints show a confirm popup. Hints
  stack in styled boxes above the guess input; the result screen lists hints used + total
  deducted. Backend `GET /api/hint/<session>/<n>` returns only a derivative (count / first
  letter) — **never** the full title or artist. Hidden in multiplayer (server-side scoring).
- **Guess matching** (`is_match` / `artist_match` in app.py): forgiving of `&`↔`and`,
  punctuation, spacing, `D.A.N.C.E.`-style acronyms; accepts **any one credited artist**
  for featured collabs (e.g. "jay z" for "Beyoncé ft. Jay-Z") but NOT `&`-joined bands.
  Title + artist score **independently** in one guess; the wrong half of a combined guess
  is shown as an ✗.
- **Share:** every result has explicit **Copy** + **Share** buttons; song links carry your
  score (`?song=<base64 vid>&s=<score>`) so a friend sees a "beat it" challenge.
- **Per-song stats:** result + in-game line "players usually solve after X stems · Y% guess it".
- **Request a song:** "Don't see your song?" → `POST /api/request` → `song_requests.json`.
- **SEO:** title/description/OG tags, JSON-LD, sitemap, robots, Google Search Console
  verified. Favicon + app icons.
- **iOS:** plays through the silent/ringer switch (`navigator.audioSession.type="playback"`).
- **UI:** full redesign — frosted gradient cards, inline SVG icons (no emojis), per-stem
  colors (drums red / bass blue / melody yellow / vocals green) with reveal animations,
  custom gradient slider, subtle animated game background, micro-animations.

---

## API quick reference

`/` · `POST /api/start` · `/api/status/<id>` · `/api/stem/<id>/<stem>` ·
`POST /api/guess` · `/api/answer/<id>` · `/api/hint/<id>/<n>` · `/api/search` · `/api/random` ·
`/api/songs` · `/api/catalog` · `/api/daily` · `/api/stats/<video_id>` · `POST /api/request` · `/api/health`

**SocketIO** (client→) create_room, join_room_req, update_filters, set_timer, set_rounds,
start_game, guess, skip_request, abort_game, play_again ·
(server→) joined, room_update, game_started, stem_revealed, stem_break, skip_update,
last_call, player_guessed, scores_update, guess_result, round_over, game_aborted, room_error

---

## Critical quirks

- Port 5001 locally (AirPlay takes 5000).
- yt-dlp goes stale every few months (403/format errors). Fix: `./venv/bin/pip install -U --pre "yt-dlp[default]"`. Dockerfile installs nightly.
- Downloads force the `android_vr` player client (cookies make yt-dlp prefer `web`, whose formats fail).
- Web Audio only — never reintroduce `<audio>` elements (they drift).
- In-memory multiplayer state (`rooms` dict) → must stay **1 worker**.
- **Cache-only must short-circuit before any YouTube call.** `process_game` checks
  `CACHE_ONLY` right after the direct `video_id`→R2 `load_cache` lookup and returns
  `not_cached` — *before* `get_video_meta()`. Earlier this guard ran one step too late, so
  a cache miss still made a yt-dlp metadata call → 403 bot-block from Render. Don't move it.
- `bootstrap_index()` pulls `song_index.json` from R2 on boot (a local rebuild would wipe it).
- Render free tier sleeps after ~15 min idle (~50s cold start). A full month of uptime is
  ~730–744 hrs vs the ~750-hr free budget, so a single uptime pinger can keep it warm.
- The play button toggles a `.playing` class (SVG icons), not text. Copy/Random buttons
  restore via `innerHTML` so their SVG icons survive feedback states.

---

## Open items / ideas

- Multiplayer in-game HUD (scoreboard/feed) got light frosting — could use a deeper visual pass.
- Hints are single-player only — a multiplayer version would need server-side scoring + a
  socket event (bigger change; MP scoring is authoritative on the server).
- Auto-cleanup of the yt-dlp "artist = channel name" quirk in the cache pipeline.
- `VALID_DECADES` starts at 70s (no 60s bucket) — `add <#> 60s <genre>` silently promotes
  *untagged*. Add `"60s"` to `manage_requests.py` + a 60s Random filter chip if wanted.
- `songs.json` has a duplicate "Crazy in Love" (cosmetic).
- Optionally wire `song_queue.json` filters so Random can draw from non-songs.json cached songs by genre (catalog already carries decade/genre).
