FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    nodejs \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip yt-dlp faster-whisper pytubefix

COPY . /app

RUN mkdir -p /tmp/clipforge_exports /tmp/clipforge_uploads /tmp/hf_cache && \
    chmod -R 777 /app /tmp/clipforge_exports /tmp/clipforge_uploads /tmp/hf_cache

ENV PORT=7860
ENV HOME=/tmp
ENV HF_HOME=/tmp/hf_cache
ENV PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["sh", "-c", "if [ -f /app/clipforge-pro.py ]; then exec python -u /app/clipforge-pro.py; elif [ -f /app/clipforge_app.py ]; then exec python -u /app/clipforge_app.py; else exec python -u /app/app.py; fi"]
