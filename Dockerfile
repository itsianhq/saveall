FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV DOWNLOAD_DIR=/downloads
EXPOSE 8000

# On start, update yt-dlp so site changes don't break downloads, then serve.
CMD pip install -q --no-cache-dir -U "yt-dlp[default]" 2>/dev/null; \
    exec gunicorn -b 0.0.0.0:8000 --workers 1 --threads 8 --timeout 0 app:app
