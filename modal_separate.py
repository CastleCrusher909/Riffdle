"""
modal_separate.py — serverless GPU stem separation for Riffdle.

Runs the same pipeline as backend/app.py's local path (yt-dlp download → trim to
90s → Demucs htdemucs_ft → mp3 → drop silent stems) but on a Modal T4 GPU, so a
hosted server can separate *any* song on demand in ~30-60s instead of 20+ minutes
on a CPU box.

Setup (one time):
    pip install modal
    modal token new                 # browser auth against your Modal account
    modal deploy modal_separate.py  # build the image + deploy the function

The Flask server downloads the audio (yt-dlp runs on its residential IP, so it
isn't hit by YouTube's "confirm you're not a bot" datacenter challenge), trims
to 90s, and sends the wav bytes here. This function only runs Demucs on the GPU
and returns the stem mp3 bytes; the server saves them to R2 via save_cache().
"""

import modal

app = modal.App("riffdle-demucs")

# Image: ffmpeg + demucs. The default Linux torch wheel is CUDA-enabled, so
# Demucs runs on the GPU with `-d cuda`. We bake the model weights into the
# image so they aren't re-downloaded on every cold start.
def _prefetch_model():
    from demucs.pretrained import get_model
    get_model("htdemucs_ft")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    # torchcodec is required by newer torchaudio to write Demucs' WAV output
    # (the same dep your local requirements.txt pins).
    .pip_install("demucs", "soundfile", "torchcodec")
    .run_function(_prefetch_model)
)

MODEL = "htdemucs_ft"
TRIM_SECONDS = 90
SILENCE_DB = -50.0


def _is_silent(mp3_path: str) -> bool:
    import re
    import subprocess
    result = subprocess.run(
        ["ffmpeg", "-i", mp3_path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume: ([-\d.]+) dB", result.stderr)
    return bool(m) and float(m.group(1)) < SILENCE_DB


@app.function(image=image, gpu="T4", timeout=900)
def separate_audio(wav_bytes: bytes) -> dict:
    """Separate already-downloaded (and 90s-trimmed) wav audio on the GPU.
    Returns {stems: {name: mp3_bytes}}. No yt-dlp here — the server downloads."""
    import sys
    import subprocess
    import tempfile
    from pathlib import Path

    work = Path(tempfile.mkdtemp())
    # No-dot name → predictable Demucs output dir
    trimmed = work / "trimmed.wav"
    trimmed.write_bytes(wav_bytes)

    # ── Demucs on GPU ────────────────────────────────────────────────────────
    stems_out = work / "stems"
    subprocess.run(
        [sys.executable, "-m", "demucs", "-n", MODEL, "-d", "cuda",
         "-o", str(stems_out), str(trimmed)],
        check=True,
    )

    model_out = stems_out / MODEL / "trimmed"
    stem_map = {
        "drums":  model_out / "drums.wav",
        "bass":   model_out / "bass.wav",
        "melody": model_out / "other.wav",
        "vocals": model_out / "vocals.wav",
    }

    # ── Convert to mp3, drop silent stems, collect bytes ─────────────────────
    stems = {}
    for name, wav_file in stem_map.items():
        mp3_file = wav_file.with_suffix(".mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_file), "-q:a", "0", str(mp3_file)],
            check=True, capture_output=True,
        )
        if not _is_silent(str(mp3_file)):
            stems[name] = mp3_file.read_bytes()

    if not stems:   # safety: everything read as silent (shouldn't happen)
        for name, wav_file in stem_map.items():
            stems[name] = wav_file.with_suffix(".mp3").read_bytes()

    return {"stems": stems}
