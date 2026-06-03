// ══════════════════════════════════════════════════════════════════════════════
// Riffdle — Multiplayer client
// ══════════════════════════════════════════════════════════════════════════════
//
// Reuses the Web Audio engine + helpers from app.js via shared script scope
// (loadStemBuffer, masterPlay/Pause/Stop, addStemPlaying, showScreen, addGuessEntry,
//  escapeHtml, and the shared state vars sessionId/activeStemOrder/stemsRevealed/…).
// Server reveals stems on a timer; each client plays its own audio locally.

let socket = null;

const mpRoom = {
  code: null,
  sid: null,        // our own server-side socket id (from the 'joined' payload)
  isHost: false,
  status: "lobby",
  timer: 60,
  rounds: 3,
  filters: { decades: [], genres: [] },
  filtersLocked: false,
  players: [],
};

let mpTotalStems = 0;
let mpClipLength = 30;       // seconds; the looping clip length = reveal interval
let mpRoundNum = 1;          // current round number within the match
let mpTotalRounds = 1;       // rounds in this match
let mpCountdownId = null;
let mpCountdownLeft = 0;
let mpIntermissionId = null; // between-round countdown on the result screen
let mpGraceId = null;        // last-call countdown
let mpBreakId = null;        // between-stem silent break countdown
let mpResumeAfterBreak = false; // was music playing when the break started?
let mpSkipVoted = false;     // whether we've voted to skip the current stem
let mpAutoplayWanted = false;
let mpMyScore = 0;
let mpMyStreak = 0;
let mpGotTitle = false;
let mpGotArtist = false;
let mpLastGuess = "";

// ── Small helpers ─────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function chipValues(containerId) {
  return [...document.querySelectorAll(`#${containerId} .chip.active`)].map((c) => c.dataset.value);
}

function singleChipValue(containerId, fallback) {
  const el = document.querySelector(`#${containerId} .chip.active`);
  return el ? el.dataset.value : fallback;
}

function setChips(containerId, values) {
  document.querySelectorAll(`#${containerId} .chip`).forEach((c) => {
    c.classList.toggle("active", values.includes(c.dataset.value));
  });
}

function mpError(elId, msg) {
  const el = $(elId);
  el.textContent = msg;
  el.classList.remove("hidden");
}

// ── Connection ────────────────────────────────────────────────────────────────
function mpConnect() {
  if (socket && socket.connected) return socket;
  socket = io();   // same origin
  registerSocketHandlers();
  return socket;
}

function registerSocketHandlers() {
  socket.on("joined", (d) => {
    mpRoom.code = d.code;
    mpRoom.sid = d.sid;
    mpRoom.isHost = d.you_are_host;
    $("lobby-code").textContent = d.code;
    showScreen("screen-mp-lobby");
  });

  socket.on("room_update", (d) => {
    if (!mpRoom.code) return;
    mpRoom.status = d.status;
    mpRoom.timer = d.timer;
    mpRoom.rounds = d.rounds;
    mpRoom.filters = d.filters;
    mpRoom.filtersLocked = d.filters_locked;
    mpRoom.players = d.players;
    // Authoritative host check using server-side ids (handles host reassignment).
    if (mpRoom.sid) mpRoom.isHost = d.host_sid === mpRoom.sid;
    renderLobby();
    // Return to lobby after a 'play again'
    if (d.status === "lobby" && !isScreenActive("screen-mp-lobby")) {
      showScreen("screen-mp-lobby");
      document.body.classList.remove("mp");
      window.mpActive = false;
    }
  });

  socket.on("room_error", (d) => {
    const where = isScreenActive("screen-mp-setup") ? "mp-setup-error" : "lobby-error";
    mpError(where, d.message);
  });

  socket.on("game_started", (d) => mpStartGame(d));
  socket.on("stem_break", onStemBreak);
  socket.on("stem_revealed", (d) => {
    clearInterval(mpBreakId);
    mpTotalStems = d.total_stems;
    mpRevealStem(d.stems_revealed);
    mpStartCountdown(d.timer);
    mpResetSkip();
    // Resume from the top so everyone hears all revealed stems together.
    if (mpResumeAfterBreak || isPlaying) {
      mpResumeAfterBreak = false;
      startOffset = 0;
      mpTryAutoplay();
    }
  });
  socket.on("skip_update", onSkipUpdate);
  socket.on("last_call", onLastCall);
  socket.on("game_aborted", onGameAborted);
  socket.on("player_guessed", onPlayerGuessed);
  socket.on("scores_update", (d) => renderMpScores(d.leaderboard));
  socket.on("guess_result", onGuessResult);
  socket.on("round_over", onRoundOver);

  socket.on("disconnect", () => {
    if (window.mpActive || mpRoom.code) {
      // Server/connection dropped mid-session
      mpError("lobby-error", "Disconnected from server.");
    }
  });
}

function isScreenActive(id) {
  return $(id).classList.contains("active");
}

// ── Landing → setup ───────────────────────────────────────────────────────────
$("btn-mp").addEventListener("click", () => {
  $("mp-setup-error").classList.add("hidden");
  const saved = localStorage.getItem("riffdle-name");
  if (saved) $("mp-name").value = saved;
  showScreen("screen-mp-setup");
});

$("btn-mp-back").addEventListener("click", () => showScreen("screen-landing"));

// Decade/genre chips: multi-select
["mp-chips-decade", "mp-chips-genre"].forEach((id) => {
  document.querySelectorAll(`#${id} .chip`).forEach((chip) => {
    chip.addEventListener("click", () => chip.classList.toggle("active"));
  });
});

// Clamp a numeric input to a range (server clamps too).
function readNum(inputId, lo, hi, fallback) {
  const v = parseInt($(inputId).value, 10);
  if (isNaN(v)) return fallback;
  return Math.max(lo, Math.min(hi, v));
}
const readClipLen = (id) => readNum(id, 3, 90, 15);
const readRounds = (id) => readNum(id, 1, 20, 3);

function getName() {
  const name = $("mp-name").value.trim();
  if (name) localStorage.setItem("riffdle-name", name);
  return name;
}

$("btn-create-room").addEventListener("click", () => {
  const name = getName();
  if (!name) { mpError("mp-setup-error", "Enter your name first."); return; }
  mpConnect();
  socket.emit("create_room", {
    name,
    timer: readClipLen("mp-timer-input"),
    rounds: readRounds("mp-rounds-input"),
    decades: chipValues("mp-chips-decade"),
    genres: chipValues("mp-chips-genre"),
  });
});

$("btn-join-room").addEventListener("click", () => {
  const name = getName();
  const code = $("mp-code").value.trim().toUpperCase();
  if (!name) { mpError("mp-setup-error", "Enter your name first."); return; }
  if (code.length !== 4) { mpError("mp-setup-error", "Room codes are 4 letters."); return; }
  mpConnect();
  socket.emit("join_room_req", { code, name });
});

// ── Lobby rendering ───────────────────────────────────────────────────────────
function renderLobby() {
  $("lobby-code").textContent = mpRoom.code || "————";

  // Players
  const list = $("lobby-player-list");
  list.innerHTML = "";
  mpRoom.players.forEach((p) => {
    const li = document.createElement("li");
    li.innerHTML = `${p.is_host ? "👑 " : ""}${escapeHtml(p.name)}`;
    list.appendChild(li);
  });

  // Host sees editable controls; everyone else sees a read-only summary.
  $("lobby-settings").classList.toggle("hidden", !mpRoom.isHost);
  $("lobby-settings-readonly").classList.toggle("hidden", mpRoom.isHost);
  $("btn-start-game").classList.toggle("hidden", !mpRoom.isHost);
  $("lobby-wait-msg").classList.toggle("hidden", mpRoom.isHost);

  if (mpRoom.isHost) {
    // Reflect authoritative state. Don't clobber an input while it's being edited.
    if (document.activeElement !== $("lobby-timer-input")) {
      $("lobby-timer-input").value = mpRoom.timer;
    }
    if (document.activeElement !== $("lobby-rounds-input")) {
      $("lobby-rounds-input").value = mpRoom.rounds;
    }
    setChips("lobby-chips-decade", mpRoom.filters.decades);
    setChips("lobby-chips-genre", mpRoom.filters.genres);
  } else {
    // Fill the read-only summary
    const fmtList = (arr) => (arr && arr.length ? arr.join(", ") : "Any");
    $("ro-timer").textContent = `${mpRoom.timer}s`;
    $("ro-rounds").textContent = `${mpRoom.rounds}`;
    $("ro-decades").textContent = fmtList(mpRoom.filters.decades);
    $("ro-genres").textContent = fmtList(mpRoom.filters.genres);
  }

  if (mpRoom.status === "loading") {
    $("lobby-wait-msg").textContent = "Loading song…";
    $("lobby-wait-msg").classList.remove("hidden");
  } else {
    $("lobby-wait-msg").textContent = "Waiting for the host to start…";
  }
}

// Lobby controls (host only). The server broadcast re-renders authoritatively.
$("lobby-timer-input").addEventListener("change", () => {
  if (!mpRoom.isHost) return;
  const v = readClipLen("lobby-timer-input");
  $("lobby-timer-input").value = v;
  socket.emit("set_timer", { timer: v });
});
$("lobby-rounds-input").addEventListener("change", () => {
  if (!mpRoom.isHost) return;
  const v = readRounds("lobby-rounds-input");
  $("lobby-rounds-input").value = v;
  socket.emit("set_rounds", { rounds: v });
});
["lobby-chips-decade", "lobby-chips-genre"].forEach((id) => {
  document.querySelectorAll(`#${id} .chip`).forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!mpRoom.isHost) return;
      chip.classList.toggle("active");   // optimistic; server echo confirms
      socket.emit("update_filters", {
        decades: chipValues("lobby-chips-decade"),
        genres: chipValues("lobby-chips-genre"),
      });
    });
  });
});

$("btn-start-game").addEventListener("click", () => {
  if (mpRoom.isHost) socket.emit("start_game");
});

$("btn-copy-link").addEventListener("click", async () => {
  const link = `${location.origin}/?room=${mpRoom.code}`;
  try {
    await navigator.clipboard.writeText(link);
    $("btn-copy-link").textContent = "✓ Copied!";
    setTimeout(() => { $("btn-copy-link").textContent = "🔗 Copy invite link"; }, 1500);
  } catch (_) {
    prompt("Copy this invite link:", link);
  }
});

$("btn-leave-lobby").addEventListener("click", mpLeave);
$("btn-mp-leave-result").addEventListener("click", mpLeave);

function mpLeave() {
  if (socket) socket.disconnect();
  socket = null;
  mpRoom.code = null;
  mpRoom.isHost = false;
  window.mpActive = false;
  document.body.classList.remove("mp");
  clearInterval(mpCountdownId);
  clearInterval(mpIntermissionId);
  clearInterval(mpGraceId);
  clearInterval(mpBreakId);
  if (typeof masterStop === "function") masterStop();
  showScreen("screen-landing");
}

// ── Game ──────────────────────────────────────────────────────────────────────
async function mpStartGame(d) {
  window.mpActive = true;
  document.body.classList.add("mp");

  clearInterval(mpIntermissionId);
  clearInterval(mpGraceId);
  clearInterval(mpBreakId);
  mpResetSkip();
  $("mp-timer").classList.remove("last-call");
  sessionId = d.session_id;
  mpTotalStems = d.total_stems;
  mpRoom.timer = d.timer;
  mpClipLength = d.timer;   // the looping clip length
  mpRoundNum = d.round || 1;
  mpTotalRounds = d.total_rounds || 1;

  // Reset the shared Web Audio engine. loopRegion is set once the first buffer
  // decodes (we need the song duration to centre the clip).
  stopAllSources();
  isPlaying = false;
  mpResumeAfterBreak = false;
  startOffset = 0;
  startCtxTime = 0;
  loopRegion = null;
  for (const k in buffers) delete buffers[k];
  for (const k in bufferPromises) delete bufferPromises[k];
  cancelAnimationFrame(rafId);

  // Fetch the ordered stem names for this song
  let stems;
  try {
    const status = await fetch(`${API}/status/${sessionId}`).then((r) => r.json());
    stems = status.stems_available || ["drums", "bass", "melody", "vocals"];
  } catch (_) {
    stems = ["drums", "bass", "melody", "vocals"];
  }
  activeStemOrder = stems;
  stemsRevealed = 0;

  // Stem cards
  ALL_STEMS.forEach((stem) => {
    const card = $(`stem-${stem}`);
    card.classList.remove("unlocked");
    card.style.display = activeStemOrder.includes(stem) ? "" : "none";
  });

  // Reset UI
  $("guess-log").innerHTML = "";
  $("mp-feed").innerHTML = "";
  $("guess-input").value = "";
  $("score-display").textContent = "Score: 0";
  $("btn-play-all").textContent = "▶";
  $("btn-play-all").disabled = false;
  $("seek-bar").disabled = true;   // loop mode: no scrubbing, bar shows clip progress
  $("seek-bar").value = 0;
  $("time-current").textContent = "0:00";
  $("time-total").textContent = "0:00";
  $("artist-bonus").classList.add("hidden");
  // Score + streak are cumulative across a match; only zero them at round 1.
  if (mpRoundNum === 1) { mpMyScore = 0; mpMyStreak = 0; }
  mpGotTitle = false;
  mpGotArtist = false;
  updateMpScoreDisplay();
  renderMpScores(d.leaderboard || mpRoom.players);   // cumulative standings

  // Round badge in the HUD ("Round 2 of 5", or hidden for a single-round match)
  $("mp-round-badge").textContent = mpTotalRounds > 1 ? `Round ${mpRoundNum} of ${mpTotalRounds}` : "";
  $("mp-round-badge").style.display = mpTotalRounds > 1 ? "" : "none";

  // Host-only "End game" button
  $("btn-mp-abort").classList.toggle("hidden", !mpRoom.isHost);

  showScreen("screen-game");

  mpAutoplayWanted = true;
  mpRevealStem(d.stems_revealed);   // reveal stem 1
  mpStartCountdown(d.timer);
}

function mpRevealStem(n) {
  while (stemsRevealed < n && stemsRevealed < activeStemOrder.length) {
    const stem = activeStemOrder[stemsRevealed];
    stemsRevealed++;
    $(`stem-${stem}`).classList.add("unlocked");
    loadStemBuffer(stem)
      .then((buf) => {
        mpEnsureLoopRegion(buf);   // centre + size the clip from the song duration
        if (isPlaying) {
          addStemPlaying(stem);
        } else if (mpAutoplayWanted) {
          mpAutoplayWanted = false;
          mpTryAutoplay();
        }
      })
      .catch((err) => console.error(`stem ${stem} load failed`, err));
  }
  updateMpProgress();
}

// Define the looping clip: mpClipLength seconds taken from the exact middle of
// the song. Set once, from the first decoded buffer (all stems share its length).
function mpEnsureLoopRegion(buf) {
  if (loopRegion || !buf) return;
  const L = Math.min(mpClipLength, buf.duration);
  const start = Math.max(0, (buf.duration - L) / 2);
  loopRegion = { start, length: L };
}

function mpTryAutoplay() {
  const ctx = ensureCtx();
  if (ctx.state === "suspended") {
    ctx.resume().then(() => { if (ctx.state === "running") masterPlay(); }).catch(() => {});
  } else {
    masterPlay();
  }
}

function updateMpProgress() {
  const total = activeStemOrder.length || 1;
  $("progress-fill").style.width = `${(stemsRevealed / total) * 100}%`;
  $("reveal-label").textContent = `Stem ${stemsRevealed} of ${total} revealed`;
}

function mpStartCountdown(seconds) {
  clearInterval(mpCountdownId);
  mpCountdownLeft = seconds;
  renderCountdown();
  mpCountdownId = setInterval(() => {
    mpCountdownLeft = Math.max(0, mpCountdownLeft - 1);
    renderCountdown();
    if (mpCountdownLeft === 0) clearInterval(mpCountdownId);
  }, 1000);
}

// Silent break between stems: stop the music, count down, then stem_revealed resumes it.
function onStemBreak(d) {
  clearInterval(mpCountdownId);
  mpResumeAfterBreak = isPlaying;
  if (typeof masterStop === "function") masterStop();
  $("btn-mp-skip").disabled = true;
  let left = d.seconds || 3;
  const el = $("mp-timer");
  el.classList.remove("last-call");
  const render = () => { el.innerHTML = `Next stem in <strong>${left}s</strong>`; };
  render();
  clearInterval(mpBreakId);
  mpBreakId = setInterval(() => {
    left = Math.max(0, left - 1);
    render();
    if (left === 0) clearInterval(mpBreakId);
  }, 1000);
}

function renderCountdown() {
  const el = $("mp-timer");
  if (stemsRevealed >= mpTotalStems) {
    el.innerHTML = mpCountdownLeft > 0
      ? `Last chance — <strong>${mpCountdownLeft}s</strong>`
      : `Time's up!`;
  } else {
    el.innerHTML = `🎧 Listen — <strong>${mpCountdownLeft}s</strong>`;
  }
}

// ── Guessing ──────────────────────────────────────────────────────────────────
function mpSubmitGuess() {
  if (!window.mpActive) return;
  const input = $("guess-input");
  const guess = input.value.trim();
  if (!guess) return;
  mpLastGuess = guess;
  input.value = "";
  socket.emit("guess", { guess });
}

// Hook into the shared guess controls (app.js submitGuess bails when mpActive)
$("btn-guess").addEventListener("click", mpSubmitGuess);
$("guess-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && window.mpActive) mpSubmitGuess();
});

// ── Skip voting ───────────────────────────────────────────────────────────────
$("btn-mp-skip").addEventListener("click", () => {
  if (!window.mpActive) return;
  mpSkipVoted = !mpSkipVoted;
  $("btn-mp-skip").classList.toggle("voted", mpSkipVoted);
  socket.emit("skip_request", { skip: mpSkipVoted });
});

// ── Abort game (host only) ────────────────────────────────────────────────────
$("btn-mp-abort").addEventListener("click", () => {
  if (!mpRoom.isHost) return;
  if (confirm("End the game for everyone and return to the lobby?")) {
    socket.emit("abort_game");
  }
});

function onGameAborted() {
  clearInterval(mpCountdownId);
  clearInterval(mpIntermissionId);
  clearInterval(mpGraceId);
  clearInterval(mpBreakId);
  if (typeof masterStop === "function") masterStop();
  document.body.classList.remove("mp");
  window.mpActive = false;
  $("mp-timer").classList.remove("last-call");
  showScreen("screen-mp-lobby");   // room_update will fill it in
}

function mpResetSkip() {
  mpSkipVoted = false;
  const btn = $("btn-mp-skip");
  btn.classList.remove("voted");
  btn.disabled = false;
  btn.textContent = "⏭ Skip stem";
  $("mp-skip-status").classList.add("hidden");
}

function onSkipUpdate(d) {
  // d = { requested, needed }. Show the running tally to everyone.
  const onLast = stemsRevealed >= mpTotalStems;
  const label = onLast ? "⏭ Skip ahead" : "⏭ Skip stem";
  $("btn-mp-skip").textContent = d.needed > 1 ? `${label} (${d.requested}/${d.needed})` : label;

  const status = $("mp-skip-status");
  if (d.requested > 0 && d.needed > 0) {
    const verb = onLast ? "skip ahead" : "skip this stem";
    status.textContent = `🙋 ${d.requested} of ${d.needed} want to ${verb}`;
    status.classList.remove("hidden");
  } else {
    status.classList.add("hidden");
  }
}

function onLastCall(d) {
  clearInterval(mpCountdownId);
  if (typeof masterStop === "function") masterStop();
  const btn = $("btn-mp-skip");
  btn.disabled = true;
  btn.classList.remove("voted");
  let left = d.seconds || 5;
  const el = $("mp-timer");
  el.classList.add("last-call");
  const render = () => {
    el.innerHTML = d.is_final
      ? `⏰ Last guesses — <strong>${left}s</strong>`
      : `Next round in <strong>${left}s</strong>`;
  };
  render();
  clearInterval(mpGraceId);
  mpGraceId = setInterval(() => {
    left = Math.max(0, left - 1);
    render();
    if (left === 0) clearInterval(mpGraceId);
  }, 1000);
}

function updateMpScoreDisplay() {
  const streak = mpMyStreak >= 2 ? ` 🔥${mpMyStreak}` : "";
  $("score-display").textContent = `Score: ${mpMyScore}${streak}`;
}

function onGuessResult(d) {
  // One guess can land the title, the artist, or both at once.
  if (d.title_hit) {
    mpGotTitle = true;
    mpMyStreak = d.streak || 0;
    addGuessEntry(d.title, "correct", d.title_points);
    mpMyScore += d.title_points;
    const bonuses = [];
    if (d.order_bonus) bonuses.push(`🥇 +${d.order_bonus} first`);
    if (d.streak_bonus) bonuses.push(`🔥 +${d.streak_bonus} streak ×${d.streak}`);
    if (bonuses.length) mpAddFeed(`<span class="feed-you">You: ${bonuses.join(" · ")}</span>`);
  }
  if (d.artist_hit) {
    mpGotArtist = true;
    addGuessEntry(d.artist, "artist", d.artist_points);
    mpMyScore += d.artist_points;
  }
  if (!d.title_hit && !d.artist_hit) {
    addGuessEntry(mpLastGuess || "—", "wrong", 0);
  }
  if (d.title_hit || d.artist_hit) {
    updateMpScoreDisplay();
    $("artist-bonus").classList.toggle("hidden", mpGotArtist || !mpGotTitle);
  }
}

function onPlayerGuessed(d) {
  let line;
  if (d.what === "title") {
    line = d.first
      ? `🥇 <strong>${escapeHtml(d.name)}</strong> got the title first!`
      : `✅ <strong>${escapeHtml(d.name)}</strong> got the title`;
    if (d.streak >= 2) line += ` <span class="feed-streak">🔥${d.streak}</span>`;
  } else {
    line = `🎤 <strong>${escapeHtml(d.name)}</strong> got the artist`;
  }
  mpAddFeed(line);
}

function mpAddFeed(html) {
  const feed = $("mp-feed");
  const el = document.createElement("div");
  el.className = "mp-feed-item";
  el.innerHTML = html;
  feed.prepend(el);
  while (feed.children.length > 8) feed.removeChild(feed.lastChild);
}

function streakBadge(streak) {
  return streak >= 2 ? ` <span class="streak-badge">🔥${streak}</span>` : "";
}

function renderMpScores(leaderboard) {
  const ol = $("mp-scores");
  ol.innerHTML = "";
  (leaderboard || []).forEach((p) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="mp-score-name">${p.is_host ? "👑 " : ""}${escapeHtml(p.name)}${streakBadge(p.streak)}</span>
                    <span class="mp-score-pts">${p.score}</span>`;
    ol.appendChild(li);
  });
}

// ── Round over ────────────────────────────────────────────────────────────────
function onRoundOver(d) {
  clearInterval(mpCountdownId);
  clearInterval(mpIntermissionId);
  clearInterval(mpGraceId);
  clearInterval(mpBreakId);
  $("mp-timer").classList.remove("last-call");
  if (typeof masterStop === "function") masterStop();

  // Mirror the server: missing the title this round breaks your streak.
  if (!mpGotTitle) mpMyStreak = 0;

  const matchOver = !!d.match_over;
  const multi = (d.total || 1) > 1;

  // Heading + round indicator
  $("mp-result-heading").textContent = matchOver ? "🏆 Match Over" : "Round Over";
  $("mp-result-round").textContent = multi ? `Round ${d.round} of ${d.total}` : "";
  $("mp-lb-label").textContent = matchOver ? "Final standings" : "Standings";

  $("mp-result-title").textContent = d.title || "—";
  $("mp-result-artist").textContent = d.artist ? `by ${d.artist}` : "";

  const ol = $("mp-leaderboard");
  ol.innerHTML = "";
  (d.leaderboard || []).forEach((p, i) => {
    const li = document.createElement("li");
    const medal = ["🥇", "🥈", "🥉"][i] || `${i + 1}.`;
    li.innerHTML = `<span class="lb-rank">${medal}</span>
                    <span class="lb-name">${p.is_host ? "👑 " : ""}${escapeHtml(p.name)}${streakBadge(p.streak)}</span>
                    <span class="lb-score">${p.score}</span>`;
    ol.appendChild(li);
  });

  // Between rounds: everyone sees an auto-advance countdown, no buttons.
  // Match over: host gets "Play again", others wait.
  if (matchOver) {
    $("mp-intermission").classList.add("hidden");
    $("btn-mp-play-again").classList.toggle("hidden", !mpRoom.isHost);
    $("mp-result-wait").classList.toggle("hidden", mpRoom.isHost);
  } else {
    $("btn-mp-play-again").classList.add("hidden");
    $("mp-result-wait").classList.add("hidden");
    mpStartIntermission(d.intermission || 6);
  }

  document.body.classList.remove("mp");   // free the in-game HUD styling
  window.mpActive = false;
  showScreen("screen-mp-result");
}

function mpStartIntermission(seconds) {
  const el = $("mp-intermission");
  el.classList.remove("hidden");
  let left = seconds;
  const render = () => { el.innerHTML = `Next round in <strong>${left}s</strong>`; };
  render();
  clearInterval(mpIntermissionId);
  mpIntermissionId = setInterval(() => {
    left = Math.max(0, left - 1);
    render();
    if (left === 0) { clearInterval(mpIntermissionId); el.innerHTML = "Starting next round…"; }
  }, 1000);
}

$("btn-mp-play-again").addEventListener("click", () => {
  if (mpRoom.isHost) socket.emit("play_again");
});

// ── Shareable link (?room=CODE) ───────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(location.search);
  const code = (params.get("room") || "").trim().toUpperCase();
  if (code.length === 4) {
    showScreen("screen-mp-setup");
    $("mp-code").value = code;
    const saved = localStorage.getItem("riffdle-name");
    if (saved) $("mp-name").value = saved;
    $("mp-name").focus();
  }
});
