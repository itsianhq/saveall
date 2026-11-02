import os
import re
import shutil
import tempfile
import threading
import uuid
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
# Work area for in-progress and not-kept files; lives inside the container, not in ./downloads.
WORK_DIR = os.path.join(tempfile.gettempdir(), "saveall")
shutil.rmtree(WORK_DIR, ignore_errors=True)
# Keep a copy of every download in DOWNLOAD_DIR (set KEEP_FILES=true). Off = delete after the browser saves it.
KEEP_FILES = os.environ.get("KEEP_FILES", "false").strip().lower() in ("1", "true", "yes", "on")
# Files that aren't kept are removed after this long if the browser never fetches them.
UNFETCHED_TTL = 15 * 60

app = Flask(__name__, static_folder="static")

VIDEO_FORMATS = {"mp4", "webm", "mkv"}
AUDIO_FORMATS = {"mp3", "m4a", "wav", "opus"}

PLATFORMS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "twitter.com": "X / Twitter", "x.com": "X / Twitter",
    "reddit.com": "Reddit", "redd.it": "Reddit",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
    "twitch.tv": "Twitch",
    "soundcloud.com": "SoundCloud",
    "pinterest.com": "Pinterest",
    "snapchat.com": "Snapchat",
    "threads.net": "Threads",
    "bilibili.com": "Bilibili",
}

# Skip formats yt-dlp marks as watermarked (e.g. TikTok's "download" format).
NO_WM = "[format_note!*=watermark]"

jobs = {}


def detect_platform(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def clean_url(url):
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/info")
def info():
    url = clean_url(request.json.get("url"))
    if not url:
        return jsonify(error="Paste a link first."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=friendly_error(e)), 400

    if data.get("_type") == "playlist" and data.get("entries"):
        data = next(iter(data["entries"]))

    heights = sorted(
        {f["height"] for f in data.get("formats") or [] if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return jsonify(
        platform=detect_platform(url) or data.get("extractor_key") or "Unknown",
        title=data.get("title") or "Untitled",
        thumbnail=data.get("thumbnail"),
        duration=data.get("duration"),
        uploader=data.get("uploader") or data.get("channel"),
        heights=heights,
    )


@app.post("/api/download")
def download():
    body = request.json or {}
    url = clean_url(body.get("url"))
    kind = body.get("kind", "video")
    fmt = body.get("format", "mp4")
    quality = body.get("quality", "best")

    allowed = {"video": VIDEO_FORMATS, "audio": AUDIO_FORMATS}.get(kind, set())
    if not url or fmt not in allowed:
        return jsonify(error="Unsupported format."), 400

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "starting", "percent": 0, "speed": None, "eta": None, "file": None, "error": None}
    threading.Thread(target=run_job, args=(job_id, url, kind, fmt, quality), daemon=True).start()
    return jsonify(id=job_id)


def run_job(job_id, url, kind, fmt, quality):
    job = jobs[job_id]
    out_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            job.update(
                status="downloading",
                percent=round(d["downloaded_bytes"] / total * 100, 1) if total else job["percent"],
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            job.update(status="processing", percent=100)

    opts = {
        "quiet": True,
        "noplaylist": True,
        "outtmpl": os.path.join(out_dir, "%(title).150B.%(ext)s"),
        "restrictfilenames": False,
        "progress_hooks": [hook],
        "postprocessor_hooks": [lambda d: job.update(status="processing")],
    }

    if kind == "audio":
        opts["format"] = f"bestaudio{NO_WM}/bestaudio/best{NO_WM}/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        h = f"[height<={int(quality)}]" if str(quality).isdigit() else ""
        opts["format"] = (
            f"bv*{h}{NO_WM}+ba/b{h}{NO_WM}/bv*{h}+ba/b{h}/b"
        )
        opts["merge_output_format"] = fmt
        # Make mp4 play everywhere (QuickTime, iPhone, Windows) by preferring H.264/AAC.
        if fmt == "mp4":
            opts["format_sort"] = ["vcodec:h264", "acodec:aac", "res", "fps"]
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": fmt}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(out_dir) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Nothing was downloaded.")
        name = max(files, key=lambda f: os.path.getsize(os.path.join(out_dir, f)))
        if KEEP_FILES:
            final = unique_path(os.path.join(DOWNLOAD_DIR, name))
            shutil.move(os.path.join(out_dir, name), final)
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            final = os.path.join(out_dir, name)
            threading.Timer(UNFETCHED_TTL, shutil.rmtree, args=(out_dir, True)).start()
        job.update(status="done", percent=100, file=final)
    except Exception as e:
        job.update(status="error", error=friendly_error(e))
        shutil.rmtree(out_dir, ignore_errors=True)


def unique_path(path):
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def friendly_error(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    if "Unsupported URL" in msg:
        return "That link isn't supported."
    if "login" in msg.lower() or "private" in msg.lower():
        return "This video is private or needs a login."
    return msg[:300]


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify({**job, "file": os.path.basename(job["file"]) if job["file"] else None})


@app.get("/api/file/<job_id>")
def file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not os.path.exists(job["file"]):
        return jsonify(error="File not ready"), 404
    path = job["file"]
    if KEEP_FILES:
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))

    # Not kept: stream it, then delete once the browser has the whole file.
    def stream():
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    resp = Response(stream(), mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(os.path.getsize(path))
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(os.path.basename(path))}"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
