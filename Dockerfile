# Riffdle web server image for Render (or any Docker host).
# The heavy separation work runs on Modal's GPU, so this image stays lean:
# Python + ffmpeg + a handful of light deps. No torch/demucs here.
FROM python:3.11-slim

# ffmpeg: yt-dlp uses it to extract/trim audio before shipping to Modal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt \
    && pip install --no-cache-dir -U --pre "yt-dlp[default]"

# Only the app code (see .dockerignore for what's excluded)
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Render injects $PORT; app.py reads it. This is just a local-run fallback.
ENV PORT=10000
EXPOSE 10000

CMD ["python", "backend/app.py"]
