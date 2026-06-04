// Origin-relative so the app works both on localhost and from another device on
// the LAN (e.g. http://172.16.x.x:5001) — needed for multiplayer across devices.
const API = `${location.origin}/api`;
window.mpActive = false;   // true while a multiplayer game owns the shared UI/audio

const ALL_STEMS = ["drums", "bass", "melody", "vocals"];
let activeStemOrder = [];

let sessionId = null;
let currentVideoId = null;   // video id of the song in play (for sharing)
let playerSolveStems = null; // stems revealed when the player got the title (or null)
let challengeScore = null;   // a friend's score to beat (from a shared link)
let challengeForVid = null;  // the video id that challenge score applies to
let dailyMode = false;       // true while playing the daily challenge
let dailyNumber = null;      // today's daily number
let dailyCountdownId = null;
let stemsRevealed = 0;
let score = 0;
let gameOver = false;
let titleGuessed = false;
let artistGuessed = false;
let resultTitle = null;
let resultArtist = null;
let pollToken = 0;       // incremented to cancel stale poll loops
let revealLocked = false; // debounce guard — prevents double-firing the reveal button

// ── Screen helpers ────────────────────────────────────────────
function showScreen(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}

function setError(el, msg) {
  el.textContent = msg;
  el.classList.remove("hidden");
}

// ── Landing ───────────────────────────────────────────────────
document.getElementById("btn-start").addEventListener("click", handleSearchOrStart);
document.getElementById("yt-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") handleSearchOrStart();
});

// Cached-song catalog for autocomplete. On the hosted site (cache_only) the
// search box only suggests songs that are actually playable.
let cachedCatalog = [];
let cacheOnly = false;

async function loadCatalog() {
  try {
    const data = await fetch(`${API}/catalog`).then((r) => r.json());
    cachedCatalog = data.songs || [];
    cacheOnly = !!data.cache_only;
    if (cacheOnly) {
      document.getElementById("yt-url").placeholder = "Search songs…";
    }
  } catch (_) {}
}
loadCatalog();

function catalogMatches(query) {
  const q = query.toLowerCase().trim();
  if (!q) return [];
  return cachedCatalog
    .filter((s) => s.title.toLowerCase().includes(q) || s.artist.toLowerCase().includes(q))
    .slice(0, 8);
}

function watchUrl(videoId) {
  return `https://www.youtube.com/watch?v=${videoId}`;
}

function renderCatalogSuggestions(query) {
  const el = document.getElementById("search-results");
  const matches = catalogMatches(query);
  el.innerHTML = "";
  if (!matches.length) { el.classList.add("hidden"); return; }
  matches.forEach((s) => {
    const card = document.createElement("button");
    card.className = "search-card";
    card.innerHTML = `
      <img class="search-thumb" src="https://img.youtube.com/vi/${s.video_id}/mqdefault.jpg" alt="" />
      <div class="search-meta">
        <span class="search-title">${escapeHtml(s.title)}</span>
        <span class="search-artist">${escapeHtml(s.artist)}</span>
      </div>`;
    card.addEventListener("click", () => startGame(watchUrl(s.video_id)));
    el.appendChild(card);
  });
  el.classList.remove("hidden");
}

// Live autocomplete from the cached catalog as the user types.
document.getElementById("yt-url").addEventListener("input", (e) => {
  if (isUrl(e.target.value)) { document.getElementById("search-results").classList.add("hidden"); return; }
  renderCatalogSuggestions(e.target.value);
});

function isUrl(str) {
  return str.startsWith("http://") || str.startsWith("https://") || str.startsWith("www.");
}

async function handleSearchOrStart() {
  const input = document.getElementById("yt-url").value.trim();
  const errEl = document.getElementById("landing-error");
  errEl.classList.add("hidden");
  if (!input) { setError(errEl, "Type a song name."); return; }

  if (isUrl(input)) { startGame(input); return; }

  // Cache-only host: play the best cached match (no YouTube search).
  if (cacheOnly) {
    const matches = catalogMatches(input);
    if (matches.length) startGame(watchUrl(matches[0].video_id));
    else setError(errEl, "🎵 No song matches that yet — try 🎲 Random or 🎮 Play with friends!");
    return;
  }

  // Local Mac: fall back to a YouTube search so new songs can be cached.
  await searchSongs(input);
}

async function searchSongs(query) {
  const errEl = document.getElementById("landing-error");
  const btn = document.getElementById("btn-start");
  const resultsEl = document.getElementById("search-results");

  btn.textContent = "Searching…";
  btn.disabled = true;
  resultsEl.classList.add("hidden");
  resultsEl.innerHTML = "";

  try {
    const res = await fetch(`${API}/search?q=${encodeURIComponent(query)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Search failed");
    renderSearchResults(data.results);
  } catch (err) {
    setError(errEl, `Search error: ${err.message}`);
  } finally {
    btn.textContent = "Search";
    btn.disabled = false;
  }
}

function renderSearchResults(results) {
  const el = document.getElementById("search-results");
  el.innerHTML = "";
  if (!results.length) {
    el.innerHTML = "<p class='no-results'>No results found.</p>";
    el.classList.remove("hidden");
    return;
  }
  results.forEach((r) => {
    const card = document.createElement("button");
    card.className = "search-card";
    card.innerHTML = `
      <img class="search-thumb" src="${r.thumbnail}" alt="" />
      <div class="search-meta">
        <span class="search-title">${escapeHtml(r.title)}</span>
        <span class="search-artist">${escapeHtml(r.artist)}${r.duration ? ` · ${r.duration}` : ""}</span>
      </div>
    `;
    card.addEventListener("click", () => startGame(r.url));
    el.appendChild(card);
  });
  el.classList.remove("hidden");
}

function extractVideoId(url) {
  const m = String(url).match(/(?:v=|youtu\.be\/|\/shorts\/|\/embed\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : null;
}

async function startGame(url, opts = {}) {
  currentVideoId = extractVideoId(url);   // remembered for the Share button
  playerSolveStems = null;
  dailyMode = !!opts.daily;
  dailyNumber = opts.daily ? opts.number : null;
  const errEl = document.getElementById("landing-error");
  errEl.classList.add("hidden");
  document.getElementById("search-results").classList.add("hidden");

  showScreen("screen-loading");
  setLoadingLabel("Sending link to server…");

  try {
    const res = await fetch(`${API}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Server error");
    sessionId = data.session_id;
    pollToken++;
    pollStatus(pollToken);
  } catch (err) {
    showScreen("screen-landing");
    setError(errEl, `Error: ${err.message}`);
  }
}

function setLoadingLabel(msg) {
  document.getElementById("loading-label").textContent = msg;
}

async function pollStatus(token) {
  if (token !== pollToken) return;  // stale loop — bail out

  try {
    const res = await fetch(`${API}/status/${sessionId}`);
    const data = await res.json();

    if (token !== pollToken) return;  // cancelled while fetch was in flight

    if (data.status === "checking_cache") setLoadingLabel("Checking cache…");
    else if (data.status === "downloading") setLoadingLabel("Downloading audio from YouTube…");
    else if (data.status === "separating") setLoadingLabel("Separating stems with Demucs… (this takes a minute)");
    else if (data.status === "queued") setLoadingLabel("Queued…");
    else if (data.status === "ready") { initGame(data.stems_available); return; }
    else if (data.status === "error") {
      showScreen("screen-landing");
      const msg = data.error === "not_cached"
        ? "🎵 That song isn't in Riffdle yet — try 🎲 Random, 🎮 multiplayer, or another song!"
        : `Error: ${data.error}`;
      setError(document.getElementById("landing-error"), msg);
      return;
    }
  } catch (_) {}

  setTimeout(() => pollStatus(token), 2000);
}

// ── Random ────────────────────────────────────────────────────
// Scope to the landing-page filter chips only. A bare ".chip" selector would
// also bind every multiplayer chip, double-toggling them (app.js + mp.js both
// fire) and silently cancelling the user's clicks.
document.querySelectorAll("#chips-decade .chip, #chips-genre .chip").forEach((chip) => {
  chip.addEventListener("click", () => chip.classList.toggle("active"));
});

document.getElementById("btn-random").addEventListener("click", async () => {
  const btn = document.getElementById("btn-random");
  const errEl = document.getElementById("landing-error");
  errEl.classList.add("hidden");

  const decades = [...document.querySelectorAll("#chips-decade .chip.active")].map((c) => c.dataset.value);
  const genres  = [...document.querySelectorAll("#chips-genre .chip.active")].map((c) => c.dataset.value);

  const params = new URLSearchParams();
  if (decades.length) params.set("decades", decades.join(","));
  if (genres.length)  params.set("genres",  genres.join(","));

  btn.textContent = "Finding song…";
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/random?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "No songs found");
    startGame(data.url);
  } catch (err) {
    setError(errEl, `Random error: ${err.message}`);
  } finally {
    btn.textContent = "🎲 Random";
    btn.disabled = false;
  }
});

// ── Share a song with a friend ────────────────────────────────
// Builds a link like  https://riffdle.onrender.com/?song=<token>  where token is
// the base64'd video id (so the answer isn't one click away in the URL). Opening
// that link auto-starts the same cached song for the friend to play.
function songShareUrl(videoId, score) {
  const token = encodeURIComponent(btoa(videoId));
  let url = `${location.origin}/?song=${token}`;
  if (score > 0) url += `&s=${score}`;   // friend will see this as a score to beat
  return url;
}

// Copy and Share are distinct: Copy always writes to the clipboard (with
// confirmation); Share opens the native sheet and does nothing if dismissed.
async function copyToClipboard(text, btn) {
  const original = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "✓ Copied!";
    setTimeout(() => (btn.textContent = original), 2000);
  } catch (_) {
    window.prompt("Copy this:", text);
  }
}
async function nativeShare(text) {
  try { await navigator.share({ title: "Riffdle", text }); }
  catch (_) { /* dismissed or unsupported — do nothing */ }
}

function songShareMessage() {
  const url = songShareUrl(currentVideoId, score);
  let text = score > 0
    ? `I scored ${score} on this Riffdle song — can you beat it?`
    : "Can you guess this Riffdle song?";
  if (gameOver) {   // include the Heardle-style grid once finished
    const total = activeStemOrder.length || 4;
    text += `\n${emojiGrid(playerSolveStems !== null, playerSolveStems, total)}`;
  }
  return `${text}\n${url}`;
}

// Header (mid-game) button: share on mobile, copy on desktop
document.getElementById("btn-share").addEventListener("click", (e) => {
  if (!currentVideoId) return;
  const msg = songShareMessage();
  if (navigator.share) nativeShare(msg); else copyToClipboard(msg, e.currentTarget);
});
// Result screen: explicit Copy + Share
document.getElementById("btn-copy-result").addEventListener("click", (e) => {
  if (currentVideoId) copyToClipboard(songShareMessage(), e.currentTarget);
});
document.getElementById("btn-share-result").addEventListener("click", (e) => {
  if (!currentVideoId) return;
  const msg = songShareMessage();
  if (navigator.share) nativeShare(msg); else copyToClipboard(msg, e.currentTarget);
});

// Clicking the in-game Riffdle logo returns to the main menu
document.getElementById("logo-home").addEventListener("click", () => {
  if (window.mpActive && !confirm("Leave the game and go back to the menu?")) return;
  window.location.href = location.origin + location.pathname;   // home, dropping any ?song/?daily
});

// ── Daily Challenge ───────────────────────────────────────────
const dailyKey = (n) => `riffdle-daily-${n}`;

async function startDaily() {
  const btn = document.getElementById("btn-daily");
  const errEl = document.getElementById("landing-error");
  errEl.classList.add("hidden");
  btn.disabled = true;
  try {
    const d = await fetch(`${API}/daily`).then((r) => r.json());
    if (!d.video_id) throw new Error(d.error || "No daily yet");
    dailyNumber = d.number;   // so Share works even when viewing a saved result
    currentVideoId = d.video_id;   // so the stats line can load on a saved result
    const saved = localStorage.getItem(dailyKey(d.number));
    if (saved) {
      // Already played today — just show the saved result (no replay)
      enterDailyResult(JSON.parse(saved));
    } else {
      startGame(watchUrl(d.video_id), { daily: true, number: d.number });
    }
  } catch (err) {
    setError(errEl, `Couldn't load the daily: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}
document.getElementById("btn-daily").addEventListener("click", startDaily);

function finishDaily(title, artist) {
  const total = activeStemOrder.length || 4;
  const obj = {
    number: dailyNumber,
    solved: playerSolveStems !== null,
    stems: playerSolveStems,
    score,
    title,
    artist,
    total,
  };
  try { localStorage.setItem(dailyKey(dailyNumber), JSON.stringify(obj)); } catch (_) {}
  enterDailyResult(obj);
}

function emojiGrid(solved, stems, total) {
  total = total || 4;
  if (!solved) return "🟥".repeat(total);
  return "🟪".repeat(Math.max(0, stems - 1)) + "🟩" + "⬛".repeat(Math.max(0, total - stems));
}

function dailyGrid(obj) {
  return emojiGrid(obj.solved, obj.stems, obj.total);
}

function dailyShareText(obj) {
  const total = obj.total || 4;
  const res = obj.solved ? `${obj.stems}/${total} stems` : "X";
  return `🎸 Riffdle Daily #${obj.number} — Score ${obj.score} (${res})\n${dailyGrid(obj)}\n${location.origin}`;
}

function enterDailyResult(obj) {
  if (typeof masterStop === "function") masterStop();
  document.getElementById("result-heading").textContent = `🗓️ Daily Riffdle #${obj.number}`;
  document.getElementById("result-title").textContent = obj.title || "—";
  document.getElementById("result-artist").textContent = obj.artist ? `by ${obj.artist}` : "";
  document.getElementById("result-score").textContent = obj.score;
  document.getElementById("result-stats").classList.add("hidden");
  document.getElementById("result-actions").classList.add("hidden");
  document.getElementById("result-grid").classList.add("hidden");
  const dr = document.getElementById("daily-result");
  dr.classList.remove("hidden");
  document.getElementById("daily-grid").innerHTML =
    `${dailyGrid(obj)}<div class="daily-grid-sub">${obj.solved ? "Solved in " + obj.stems + "/" + obj.total + " stems" : "Not guessed"}</div>`;
  renderSongStats({ aggregateOnly: true, targetId: "daily-stats" });
  startDailyCountdown();
  showScreen("screen-result");
}

function dailyShareTextNow() {
  const saved = localStorage.getItem(dailyKey(dailyNumber));
  return saved ? dailyShareText(JSON.parse(saved)) : null;
}
document.getElementById("btn-copy-daily").addEventListener("click", (e) => {
  const t = dailyShareTextNow();
  if (t) copyToClipboard(t, e.currentTarget);
});
document.getElementById("btn-share-daily").addEventListener("click", (e) => {
  const t = dailyShareTextNow();
  if (!t) return;
  if (navigator.share) nativeShare(t); else copyToClipboard(t, e.currentTarget);
});

document.getElementById("btn-daily-home").addEventListener("click", () => {
  clearInterval(dailyCountdownId);
  showScreen("screen-landing");
});

function startDailyCountdown() {
  const el = document.getElementById("daily-countdown");
  clearInterval(dailyCountdownId);
  const tick = () => {
    const now = new Date();
    const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
    let s = Math.max(0, Math.floor((next - now) / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    el.textContent = `⏳ Next Riffdle in ${h}h ${m}m ${sec}s`;
  };
  tick();
  dailyCountdownId = setInterval(tick, 1000);
}

// If the page was opened with ?song=<token>, auto-start that cached song.
(() => {
  const params = new URLSearchParams(location.search);
  const token = params.get("song");
  if (!token || params.get("room")) return;   // ?room is handled by multiplayer
  let videoId = null;
  try { videoId = atob(decodeURIComponent(token)); } catch (_) {}
  if (videoId && /^[A-Za-z0-9_-]{11}$/.test(videoId)) {
    const s = parseInt(params.get("s"), 10);
    if (!isNaN(s) && s > 0) { challengeScore = s; challengeForVid = videoId; }
    startGame(watchUrl(videoId));
  }
})();

// ── Request a song ────────────────────────────────────────────
// Lets players ask for an uncached song; the owner reviews these later
// (manage_requests.py) and caches the good ones.
(() => {
  const toggle = document.getElementById("btn-request-toggle");
  const form = document.getElementById("request-form");
  const input = document.getElementById("request-input");
  const submit = document.getElementById("btn-request-submit");
  const msg = document.getElementById("request-msg");
  if (!toggle) return;

  toggle.addEventListener("click", () => {
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) {
      // Prefill with whatever they last typed in the search box
      const typed = document.getElementById("yt-url").value.trim();
      if (typed && !input.value) input.value = typed;
      input.focus();
    }
  });

  async function sendRequest() {
    const query = input.value.trim();
    if (!query) { input.focus(); return; }
    submit.disabled = true;
    submit.textContent = "Sending…";
    msg.classList.add("hidden");
    try {
      const res = await fetch(`${API}/request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!res.ok) throw new Error();
      input.value = "";
      form.classList.add("hidden");
      msg.textContent = "✅ Thanks! Your request was sent.";
      msg.classList.remove("hidden", "error");
    } catch (_) {
      msg.textContent = "Couldn't send that — try again later.";
      msg.classList.remove("hidden");
      msg.classList.add("error");
    } finally {
      submit.disabled = false;
      submit.textContent = "Request";
    }
  }

  submit.addEventListener("click", sendRequest);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") sendRequest(); });
})();

// ── Master player (Web Audio API — sample-accurate, drift-free) ──────────────
//
// Every revealed stem is decoded into an AudioBuffer and played through a single
// shared AudioContext clock. All sources are scheduled against the SAME clock with
// the SAME start time + offset, so they are sample-aligned and physically cannot
// drift apart. This replaces the old multi-<audio>-element approach, where each
// element had its own independent clock and the stems would slip off-beat.

let audioCtx = null;
const buffers = {};          // stem name -> decoded AudioBuffer
const bufferPromises = {};   // stem name -> in-flight decode promise (dedupes loads)
let sources = {};            // stem name -> currently playing AudioBufferSourceNode
let isPlaying = false;
let startCtxTime = 0;        // audioCtx.currentTime anchor for the current playback run
let startOffset = 0;         // position (s) that corresponds to startCtxTime.
                             // Linear mode: absolute track position.
                             // Loop mode: phase within the clip [0, loopRegion.length).
let rafId = null;
const SCHEDULE_AHEAD = 0.06; // schedule starts 60ms out so they fire precisely, not "asap"

// Loop mode (multiplayer): play a fixed clip from the middle of the song on repeat.
// null = linear playback (single-player). { start, length } = loop a segment.
let loopRegion = null;

function ensureCtx() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    // iOS mutes Web Audio when the ringer/silent switch is off. Marking the
    // session as "playback" makes it play through the switch like a music app
    // (Safari 16.4+; harmless / ignored elsewhere).
    try {
      if (navigator.audioSession) navigator.audioSession.type = "playback";
    } catch (_) {}
  }
  return audioCtx;
}

function getDuration() {
  for (const s of activeStemOrder.slice(0, stemsRevealed)) {
    if (buffers[s]) return buffers[s].duration;
  }
  return 0;
}

// Track position (seconds) right now, derived from the shared clock.
// In loop mode this is the phase within the clip, in [0, length).
function currentPosition() {
  if (!isPlaying) return startOffset;
  const raw = audioCtx.currentTime - startCtxTime + startOffset;
  if (loopRegion) {
    const L = loopRegion.length;
    return ((raw % L) + L) % L;   // wrap into [0, L)
  }
  const dur = getDuration();
  return dur ? Math.min(Math.max(raw, 0), dur) : Math.max(raw, 0);
}

// Configure an AudioBufferSourceNode for loop playback when loopRegion is set.
function applyLoop(src) {
  if (!loopRegion) return;
  src.loop = true;
  src.loopStart = loopRegion.start;
  src.loopEnd = loopRegion.start + loopRegion.length;
}

// Fetch + decode a stem's MP3 into an AudioBuffer (cached, deduped).
function loadStemBuffer(stem) {
  if (buffers[stem]) return Promise.resolve(buffers[stem]);
  if (bufferPromises[stem]) return bufferPromises[stem];
  const ctx = ensureCtx();
  bufferPromises[stem] = fetch(`${API}/stem/${sessionId}/${stem}`)
    .then((r) => r.arrayBuffer())
    .then((ab) => ctx.decodeAudioData(ab))
    .then((buf) => { buffers[stem] = buf; return buf; });
  return bufferPromises[stem];
}

function stopAllSources() {
  Object.values(sources).forEach((s) => { try { s.stop(); } catch (_) {} });
  sources = {};
}

// Translate a track position into the buffer offset to pass to start().
// Loop mode: clip start + phase (Web Audio then wraps at loopStart/loopEnd).
function bufferOffset(pos) {
  return loopRegion ? loopRegion.start + (pos % loopRegion.length) : Math.max(0, pos);
}

// (Re)start every revealed stem, anchored to a fresh clock point at `startOffset`.
function restartSources() {
  const ctx = ensureCtx();
  stopAllSources();
  const when = ctx.currentTime + SCHEDULE_AHEAD;
  startCtxTime = when;
  activeStemOrder.slice(0, stemsRevealed).forEach((stem) => {
    if (!buffers[stem]) return;
    const src = ctx.createBufferSource();
    src.buffer = buffers[stem];
    src.connect(ctx.destination);
    applyLoop(src);
    src.start(when, bufferOffset(startOffset));
    sources[stem] = src;
  });
}

// Add one newly revealed stem mid-playback, aligned to the EXISTING timeline.
function addStemPlaying(stem) {
  const ctx = audioCtx;
  if (!buffers[stem] || !isPlaying) return;
  if (sources[stem]) { try { sources[stem].stop(); } catch (_) {} }
  const when = ctx.currentTime + SCHEDULE_AHEAD;
  const pos = when - startCtxTime + startOffset; // position that must sound at `when`
  const src = ctx.createBufferSource();
  src.buffer = buffers[stem];
  src.connect(ctx.destination);
  applyLoop(src);
  src.start(when, bufferOffset(pos));
  sources[stem] = src;
}

function masterPlay() {
  const ctx = ensureCtx();
  if (ctx.state === "suspended") ctx.resume();
  if (!activeStemOrder.slice(0, stemsRevealed).some((s) => buffers[s])) return;
  isPlaying = true;
  restartSources();
  document.getElementById("btn-play-all").textContent = "⏸";
  cancelAnimationFrame(rafId);
  rafId = requestAnimationFrame(tickSeekBar);
}

function masterPause() {
  if (!isPlaying) return;
  startOffset = currentPosition();
  isPlaying = false;
  stopAllSources();
  document.getElementById("btn-play-all").textContent = "▶";
  cancelAnimationFrame(rafId);
}

function masterStop() {
  isPlaying = false;
  stopAllSources();
  startOffset = 0;
  document.getElementById("btn-play-all").textContent = "▶";
  cancelAnimationFrame(rafId);
  document.getElementById("seek-bar").value = 0;
  document.getElementById("time-current").textContent = "0:00";
}

function tickSeekBar() {
  const dur = loopRegion ? loopRegion.length : getDuration();
  if (dur) {
    const pos = currentPosition();
    document.getElementById("seek-bar").value = (pos / dur) * 100;
    document.getElementById("time-current").textContent = fmtTime(pos);
    document.getElementById("time-total").textContent = fmtTime(dur);
    // Linear mode stops at the end; loop mode just keeps cycling.
    if (!loopRegion && isPlaying && pos >= dur - 0.02) { masterStop(); return; }
  }
  if (isPlaying) rafId = requestAnimationFrame(tickSeekBar);
}

function fmtTime(s) {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

document.getElementById("btn-play-all").addEventListener("click", () => {
  if (isPlaying) masterPause(); else masterPlay();
});

document.getElementById("seek-bar").addEventListener("input", () => {
  const dur = getDuration();
  if (!dur) return;
  const target = (document.getElementById("seek-bar").value / 100) * dur;
  startOffset = target;
  if (isPlaying) restartSources();
  document.getElementById("time-current").textContent = fmtTime(target);
});

// Scroll-wheel over the seek bar scrubs the track
document.getElementById("seek-bar").addEventListener("wheel", (e) => {
  const bar = e.currentTarget;
  if (bar.disabled) return;
  e.preventDefault();
  const step = e.deltaY < 0 ? 2 : -2;   // scroll up = forward
  bar.value = Math.max(0, Math.min(100, parseFloat(bar.value || 0) + step));
  bar.dispatchEvent(new Event("input"));
}, { passive: false });

// ── Game init ─────────────────────────────────────────────────
function initGame(stems) {
  activeStemOrder = stems;
  stemsRevealed = 0;
  score = 0;
  gameOver = false;
  titleGuessed = false;
  artistGuessed = false;
  resultTitle = null;
  resultArtist = null;
  revealLocked = false;

  // Reset the Web Audio engine for a fresh song (single-player = linear, no loop)
  stopAllSources();
  isPlaying = false;
  startOffset = 0;
  startCtxTime = 0;
  loopRegion = null;
  for (const k in buffers) delete buffers[k];
  for (const k in bufferPromises) delete bufferPromises[k];
  cancelAnimationFrame(rafId);

  document.getElementById("artist-bonus").classList.add("hidden");
  document.getElementById("guess-log").innerHTML = "";
  document.getElementById("score-display").textContent = "Score: 0";
  document.getElementById("guess-input").value = "";
  document.getElementById("btn-play-all").textContent = "▶";
  document.getElementById("btn-play-all").disabled = true;
  document.getElementById("seek-bar").value = 0;
  document.getElementById("seek-bar").disabled = true;
  document.getElementById("time-current").textContent = "0:00";
  document.getElementById("time-total").textContent = "0:00";

  // Show only active stems, reset + hide the rest
  ALL_STEMS.forEach((stem) => {
    const card = document.getElementById(`stem-${stem}`);
    card.classList.remove("unlocked");
    card.style.display = activeStemOrder.includes(stem) ? "" : "none";
  });

  document.getElementById("btn-next-stem").onclick = revealNextStem;
  showScreen("screen-game");
  renderGameStats();
  renderChallengeNote();
  revealNextStem();
}

// Banner shown when you opened a friend's shared link with a score to beat.
function renderChallengeNote() {
  const el = document.getElementById("challenge-note");
  el.classList.add("hidden");
  el.innerHTML = "";
  if (challengeScore != null && challengeForVid === currentVideoId && !window.mpActive) {
    el.innerHTML = `🎯 A friend scored <strong>${challengeScore}</strong> — beat it!`;
    el.classList.remove("hidden");
  }
}

// Show the song's difficulty live while playing (aggregate only — no answer).
async function renderGameStats() {
  const el = document.getElementById("game-stats");
  el.classList.add("hidden");
  el.innerHTML = "";
  if (!currentVideoId || window.mpActive) return;
  try {
    const s = await fetch(`${API}/stats/${currentVideoId}`).then((r) => r.json());
    if (s.avg_stems != null) {
      let line = `📊 Players usually get this by stem <strong>${s.avg_stems}</strong>`;
      if (s.solve_rate != null) line += ` · <strong>${Math.round(s.solve_rate * 100)}%</strong> guess it`;
      el.innerHTML = line;
      el.classList.remove("hidden");
    }
  } catch (_) {}
}

// ── Stem reveal ───────────────────────────────────────────────
function revealNextStem() {
  if (revealLocked || stemsRevealed >= activeStemOrder.length) return;
  revealLocked = true;
  setTimeout(() => { revealLocked = false; }, 400);

  const stem = activeStemOrder[stemsRevealed];
  stemsRevealed++;

  const card = document.getElementById(`stem-${stem}`);
  card.classList.add("unlocked");

  // Enable master player on first stem
  document.getElementById("btn-play-all").disabled = false;
  document.getElementById("seek-bar").disabled = false;

  // Decode the stem; once ready, slot it into the running mix (if playing)
  loadStemBuffer(stem)
    .then(() => { if (isPlaying) addStemPlaying(stem); })
    .catch((err) => console.error(`Failed to load stem "${stem}":`, err));

  updateRevealUI();
}

function updateRevealUI() {
  const total = activeStemOrder.length;
  const pct = (stemsRevealed / total) * 100;
  document.getElementById("progress-fill").style.width = `${pct}%`;
  document.getElementById("reveal-label").textContent =
    `Stem ${stemsRevealed} of ${total} revealed`;

  const btn = document.getElementById("btn-next-stem");
  if (stemsRevealed >= total) {
    btn.textContent = "Show answer";
    btn.onclick = showAnswer;
  } else {
    btn.textContent = "Reveal next stem";
    btn.onclick = revealNextStem;
  }
}

// onclick is set dynamically in updateRevealUI — no static listener here

// ── Guessing ──────────────────────────────────────────────────
document.getElementById("btn-guess").addEventListener("click", submitGuess);
document.getElementById("guess-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitGuess();
});

document.getElementById("btn-skip").addEventListener("click", () => {
  if (gameOver) return;
  if (titleGuessed) { finishGame(); return; }
  addGuessEntry("(skipped)", "wrong", 0);
  if (stemsRevealed < activeStemOrder.length) revealNextStem();
  else showAnswer();
});

async function submitGuess() {
  if (window.mpActive) return;   // multiplayer handles its own guessing (mp.js)
  if (gameOver) return;
  const input = document.getElementById("guess-input");
  const guess = input.value.trim();
  if (!guess) return;
  input.value = "";

  try {
    const res = await fetch(`${API}/guess`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, guess, stems_revealed: stemsRevealed }),
    });
    const data = await res.json();

    // A single guess can land the title, the artist, or both at once.
    if (data.title_hit) {
      addGuessEntry(data.title, "correct", data.title_points);
      score += data.title_points;
      if (playerSolveStems === null) playerSolveStems = stemsRevealed;
      resultTitle = data.title;
      resultArtist = data.artist;
      titleGuessed = true;
    }
    if (data.artist_hit) {
      addGuessEntry(data.artist, "artist", data.artist_points);
      score += data.artist_points;
      artistGuessed = true;
      resultTitle = resultTitle || data.title;
      resultArtist = resultArtist || data.artist;
    }
    if (!data.title_hit && !data.artist_hit) {
      addGuessEntry(guess, "wrong", 0);
    } else if (data.title_hit && !data.artist_hit && !artistGuessed) {
      // Got the title but the rest of the guess (a wrong artist attempt) missed
      const left = leftoverWords(guess, data.title);
      if (left) addGuessEntry(left, "wrong", 0, "wrong artist");
    } else if (data.artist_hit && !data.title_hit && !titleGuessed) {
      const left = leftoverWords(guess, data.artist);
      if (left) addGuessEntry(left, "wrong", 0, "wrong song");
    }

    document.getElementById("score-display").textContent = `Score: ${score}`;
    if (titleGuessed && artistGuessed) {
      document.getElementById("artist-bonus").classList.add("hidden");
      finishGame();
    } else if (titleGuessed) {
      document.getElementById("artist-bonus").classList.remove("hidden");
    }
  } catch (err) {
    console.error(err);
  }
}

function finishGame() {
  gameOver = true;
  setTimeout(() => showResult(resultTitle, resultArtist), 600);
}

function addGuessEntry(text, result, pts, hint) {
  const log = document.getElementById("guess-log");
  const el = document.createElement("div");
  el.className = `guess-entry ${result}`;
  const badge = result === "correct" ? "✓" : result === "artist" ? "½" : "✗";
  const hintText = hint || (result === "artist" ? "correct artist" : "");
  const label = hintText ? `<span class="guess-hint">${hintText}</span>` : "";
  el.innerHTML = `
    <span class="guess-badge">${badge}</span>
    <span class="guess-text">${escapeHtml(text)}</span>
    ${label}
    ${pts > 0 ? `<span class="guess-pts">+${pts}</span>` : ""}
  `;
  log.prepend(el);
}

// The leftover words of a guess after removing the matched title/artist —
// i.e. the part the player got wrong in a combined guess.
function leftoverWords(guess, matched) {
  const norm = (s) => (s || "").toLowerCase().replace(/&/g, " and ")
    .replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  const m = new Set(norm(matched).split(" ").filter(Boolean));
  return norm(guess).split(" ").filter((w) => w && !m.has(w)).join(" ").trim();
}

function escapeHtml(str) {
  return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── Answer ────────────────────────────────────────────────────
async function showAnswer() {
  if (gameOver || titleGuessed) { showResult(resultTitle, resultArtist); return; }
  gameOver = true;
  try {
    const res = await fetch(`${API}/answer/${sessionId}`);
    const data = await res.json();
    showResult(data.title, data.artist);
  } catch (err) {
    showResult("Unknown", "Unknown");
  }
}

function showResult(title, artist) {
  masterStop();
  if (dailyMode) { finishDaily(title, artist); return; }
  // Normal single-player result (reset anything the daily view may have toggled)
  document.getElementById("result-heading").textContent = "🎉 Song Revealed!";
  document.getElementById("daily-result").classList.add("hidden");
  document.getElementById("result-actions").classList.remove("hidden");
  document.getElementById("result-title").textContent = title || "—";
  document.getElementById("result-artist").textContent = artist ? `by ${artist}` : "";
  document.getElementById("result-score").textContent = score;
  // Heardle-style grid of how you did this round
  const total = activeStemOrder.length || 4;
  const grid = document.getElementById("result-grid");
  grid.innerHTML = `${emojiGrid(playerSolveStems !== null, playerSolveStems, total)}` +
    `<div class="daily-grid-sub">${playerSolveStems !== null ? "Solved in " + playerSolveStems + "/" + total + " stems" : "Not guessed"}</div>`;
  grid.classList.remove("hidden");
  showScreen("screen-result");
  renderSongStats();
}

// Fetch and show how quickly people solve this song (vs. how the player did).
async function renderSongStats(opts = {}) {
  const el = document.getElementById(opts.targetId || "result-stats");
  el.classList.add("hidden");
  el.innerHTML = "";
  if (!currentVideoId) return;
  const parts = [];
  if (!opts.aggregateOnly) {
    // Challenge result vs the friend who shared this song
    if (challengeScore != null && challengeForVid === currentVideoId) {
      const verdict = score > challengeScore ? "🏆 You win!"
                    : score === challengeScore ? "🤝 Tie!"
                    : "😅 They win";
      parts.push(`🎯 Friend: <strong>${challengeScore}</strong> · You: <strong>${score}</strong> — ${verdict}`);
    }
    if (playerSolveStems !== null) {
      parts.push(`You got it after <strong>${playerSolveStems}</strong> stem${playerSolveStems === 1 ? "" : "s"}.`);
    }
  }
  try {
    // small delay so this play/solve is counted before we read the aggregate
    await new Promise((r) => setTimeout(r, 400));
    const s = await fetch(`${API}/stats/${currentVideoId}`).then((r) => r.json());
    if (s.avg_stems != null) {
      let line = `On average players solve it after <strong>${s.avg_stems}</strong> stems`;
      if (s.solve_rate != null) line += ` · <strong>${Math.round(s.solve_rate * 100)}%</strong> guess it`;
      parts.push(line + ".");
    }
  } catch (_) {}
  if (parts.length) {
    el.innerHTML = parts.join("<br>");
    el.classList.remove("hidden");
  }
}

document.getElementById("btn-play-again").addEventListener("click", () => {
  pollToken++;  // cancel any running poll loop
  sessionId = null;
  score = 0;
  stemsRevealed = 0;
  gameOver = false;
  document.getElementById("yt-url").value = "";
  document.getElementById("search-results").classList.add("hidden");
  document.getElementById("search-results").innerHTML = "";
  showScreen("screen-landing");
});
