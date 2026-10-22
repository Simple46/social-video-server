
import asyncio
import re
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware

import shutil
import threading
import uuid

from pathlib import Path
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(
    title="Social Video Saver",
    version="1.0.0",
    description="Video metadata inspection service",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://frontend-virid-five-97.vercel.app/",
        "http://127.0.0.1:5173",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

download_jobs = {}
jobs_lock = threading.Lock()

SUPPORTED_QUALITIES = {"best", "720p", "480p", "360p"}


def update_job(job_id: str, **changes):
    with jobs_lock:
        if job_id in download_jobs:
            download_jobs[job_id].update(changes)


def get_job(job_id: str):
    with jobs_lock:
        job = download_jobs.get(job_id)
        return dict(job) if job else None


def get_format_selector(quality: str) -> str:
    if quality == "best":
        return "bestvideo+bestaudio/best"

    height = int(quality.removesuffix("p"))

    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )



def run_download(job_id: str, url: str, quality: str):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(data):
        if data["status"] == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)

            percent = round(downloaded / total * 100, 1) if total else 0

            update_job(
                job_id,
                status="downloading",
                progress=percent,
            )

        elif data["status"] == "finished":
            update_job(
                job_id,
                status="processing",
                progress=100,
            )

    try:
        update_job(job_id, status="downloading", progress=0)

        options = {
            "format": get_format_selector(quality),
            "outtmpl": str(job_dir / "%(title).80s-%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "js_runtimes": {"node": {}},
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        files = [
            path for path in job_dir.iterdir()
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl"))
        ]

        if not files:
            raise RuntimeError("The downloaded video file was not found.")

        output_file = max(files, key=lambda path: path.stat().st_size)

        update_job(
            job_id,
            status="completed",
            progress=100,
            filename=output_file.name,
            filesize=output_file.stat().st_size,
            error=None,
        )

    except Exception as exc:
        print(f"Download failed ({job_id}): {type(exc).__name__}: {exc}")

        shutil.rmtree(job_dir, ignore_errors=True)

        update_job(
    job_id,
    status="failed",
    error=f"{type(exc).__name__}: {str(exc)[:500]}",
)

ALLOWED_HOSTS = {
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
}

MAX_URL_LENGTH = 2048


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)


def validate_video_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url.strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL.")

    if parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail="Only HTTPS video links are accepted.",
        )

    if not hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid URL.")

    allowed = any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in ALLOWED_HOSTS
    )

    if not allowed:
        raise HTTPException(
            status_code=400,
            detail="This video platform is not supported.",
        )

    if parsed.port not in (None, 443):
        raise HTTPException(status_code=400, detail="Invalid URL port.")

    return raw_url.strip()


def get_platform(url: str) -> str:
    hostname = urlparse(url).hostname.lower().rstrip(".")

    if hostname == "youtu.be" or hostname.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ) or hostname in {"youtube.com", "youtube-nocookie.com"}:
        return "youtube"

    if hostname == "tiktok.com" or hostname.endswith(".tiktok.com"):
        return "tiktok"

    if hostname == "instagram.com" or hostname.endswith(".instagram.com"):
        return "instagram"

    return "facebook"


def inspect_video(url: str) -> dict:
    options = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "socket_timeout": 20,
    "ignoreerrors": False,
    "js_runtimes": {
        "node": {}
    },
}

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                raise ValueError("No video information was returned.")

            formats = []
            for item in info.get("formats", []):
                if not item.get("vcodec") or item.get("vcodec") == "none":
                    continue

                formats.append({
                    "formatId": item.get("format_id"),
                    "ext": item.get("ext"),
                    "height": item.get("height"),
                    "width": item.get("width"),
                    "filesize": item.get("filesize")
                    or item.get("filesize_approx"),
                })

            return {
                "title": info.get("title") or "Untitled video",
                "platform": get_platform(url),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "formats": formats[:30],
            }

    except Exception as exc:
        # Log detailed errors on the server, but do not expose
        # internal URLs, cookies, or extractor details to clients.
        print(f"Video inspection failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=422,
            detail=(
                "We couldn't inspect this video. It may be unavailable, "
                "unsupported, or require access that this service doesn't have."
            ),
        ) from exc


@app.get("/health")
def health():
    return {"success": True, "service": "video-service"}


@app.post("/inspect")
async def inspect(request: InspectRequest):
    url = validate_video_url(request.url)

    # Keep the blocking extractor off the asynchronous event loop.
    result = await asyncio.to_thread(inspect_video, url)

    return {
        "success": True,
        "video": result,
    }


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    quality: str = "best"


@app.post("/download", status_code=202)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    url = validate_video_url(request.url)
    quality = request.quality.lower()

    if quality not in SUPPORTED_QUALITIES:
        raise HTTPException(
            status_code=400,
            detail="Quality must be best, 720p, 480p, or 360p.",
        )

    job_id = str(uuid.uuid4())

    with jobs_lock:
        download_jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "quality": quality,
            "filename": None,
            "filesize": None,
            "error": None,
        }

    background_tasks.add_task(run_download, job_id, url, quality)

    return {
        "success": True,
        "job": {
            "id": job_id,
            "status": "queued",
            "statusUrl": f"/download/{job_id}",
        },
    }


@app.get("/download/{job_id}")
def download_status(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Download job not found.")

    return {"success": True, "job": job}


@app.get("/download/{job_id}/file")
def get_download_file(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Download job not found.")

    if job["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail="The download is not ready yet.",
        )

    file_path = DOWNLOAD_DIR / job_id / job["filename"]

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Downloaded file no longer exists.")

    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        filename=job["filename"],
        background=BackgroundTask(
            shutil.rmtree,
            str(file_path.parent),
            ignore_errors=True,
        ),
    )