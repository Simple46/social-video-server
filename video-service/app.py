from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from starlette.background import BackgroundTask


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.getenv("CLIPNEST_DOWNLOAD_DIR", BASE_DIR / "downloads"))
DATA_DIR = Path(os.getenv("CLIPNEST_DATA_DIR", BASE_DIR / "data"))
EVENT_LOG = DATA_DIR / "extraction_events.jsonl"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "linkedin.com",
    "x.com",
    "twitter.com",
}

CORS_ORIGINS = [
    "https://frontend-virid-five-97.vercel.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app = FastAPI(
    title="Clipnest Python Video Service",
    description="Inspect and download publicly accessible videos using yt-dlp.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Download work runs outside the request thread. Keep the pool small so one
# local development machine is not overloaded by simultaneous downloads.
executor = ThreadPoolExecutor(max_workers=2)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InspectRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    quality: str = Field(
        default="best", description="best, 1080p, 720p, 480p, 360p, or audio")


# ---------------------------------------------------------------------------
# URL validation and common helpers
# ---------------------------------------------------------------------------

def validate_video_url(raw_url: str) -> str:
    """Accept HTTPS URLs only from the supported platform domains."""
    parsed = urlparse(raw_url)

    if parsed.scheme.lower() != "https":
        raise HTTPException(status_code=400, detail="Use an HTTPS video URL.")

    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="The URL is invalid.")

    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400, detail="URLs containing credentials are not allowed.")

    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(
            status_code=400, detail="The URL contains an invalid port.")

    if port not in (None, 443):
        raise HTTPException(
            status_code=400, detail="Only standard HTTPS URLs are allowed.")

    host = parsed.hostname.lower().rstrip(".")
    allowed = any(host == domain or host.endswith("." + domain)
                  for domain in ALLOWED_DOMAINS)

    if not allowed:
        raise HTTPException(
            status_code=400,
            detail="This platform is not supported. Try YouTube, TikTok, Instagram, Facebook, LinkedIn, or X.",
        )

    return raw_url


def get_platform(raw_url: str) -> str:
    host = (urlparse(raw_url).hostname or "").lower().rstrip(".")

    if host == "youtu.be" or host.endswith(("youtube.com", "youtube-nocookie.com")):
        return "youtube"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith("instagram.com"):
        return "instagram"
    if host == "fb.watch" or host.endswith("facebook.com"):
        return "facebook"
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host.endswith(("x.com", "twitter.com")):
        return "x"
    return "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(
    *,
    operation: str,
    platform: str,
    success: bool,
    url: str,
    error_type: str | None = None,
    error_message: str | None = None,
    quality: str | None = None,
) -> None:
    """Append a compact JSONL outcome record; do not save the full URL."""
    record = {
        "timestamp": utc_now(),
        "operation": operation,
        "platform": platform,
        "success": success,
        "quality": quality,
        "host": urlparse(url).hostname,
        "error_type": error_type,
        "error_message": (error_message or "")[:500] or None,
    }
    try:
        with EVENT_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logging.exception("Could not write extraction event log")


def package_version(package_name: str) -> str | None:
    try:
        return version(package_name)
    except PackageNotFoundError:
        return None


def base_ydl_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        # Do not force a JavaScript runtime: yt-dlp should detect configured
        # runtimes and report its own warnings if one is required.
    }


def public_metadata(info: dict[str, Any], platform: str) -> dict[str, Any]:
    duration = info.get("duration")
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "platform": platform,
        "duration": duration,
        "duration_seconds": duration,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url"),
        "ext": info.get("ext"),
        "availability": info.get("availability"),
    }


def choose_format(quality: str) -> tuple[str, str]:
    quality = quality.strip().lower()
    if quality == "audio":
        return "bestaudio/best", "mp3"
    if quality == "best":
        return "bestvideo*+bestaudio/best", "mp4"
    if quality in {"1080p", "720p", "480p", "360p"}:
        height = quality[:-1]
        return (
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "mp4",
        )
    raise HTTPException(
        status_code=400,
        detail="Invalid quality. Choose best, 1080p, 720p, 480p, 360p, or audio.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Clipnest Python Video Service",
        "status": "running",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "clipnest-python", "timestamp": utc_now()}


@app.get("/diagnostics")
def diagnostics() -> dict[str, Any]:
    ffmpeg_path = shutil.which("ffmpeg")
    deno_path = shutil.which("deno")
    return {
        "status": "ok",
        "python_service": "running",
        "yt_dlp_version": package_version("yt-dlp"),
        "fastapi_version": package_version("fastapi"),
        "ffmpeg_available": ffmpeg_path is not None,
        "ffmpeg_path": ffmpeg_path,
        "deno_available": deno_path is not None,
        "deno_path": deno_path,
        "download_directory_exists": DOWNLOAD_DIR.exists(),
        "event_log": str(EVENT_LOG),
        "supported_platforms": sorted({"youtube", "tiktok", "instagram", "facebook", "linkedin", "x"}),
    }


@app.post("/inspect")
def inspect_video(payload: InspectRequest) -> dict[str, Any]:
    raw_url = validate_video_url(str(payload.url))
    platform = get_platform(raw_url)

    try:
        options = base_ydl_options()
        options["extract_flat"] = False

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(raw_url, download=False)

        if not info:
            raise RuntimeError("The extractor returned no video information.")

        result = public_metadata(info, platform)
        log_event(operation="inspect", platform=platform,
                  success=True, url=raw_url)
        return {"success": True, "video": result}

    except HTTPException:
        raise
    except Exception as exc:
        log_event(
            operation="inspect",
            platform=platform,
            success=False,
            url=raw_url,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        logging.exception("Video inspection failed for platform=%s", platform)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Could not inspect this video. It may be private, restricted, unsupported, or the platform may require an extractor update.",
                "error_type": type(exc).__name__,
            },
        )


def run_download(job_id: str, raw_url: str, quality: str, platform: str) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        format_selector, preferred_ext = choose_format(quality)

        def progress_hook(data: dict[str, Any]) -> None:
            status = data.get("status")
            with jobs_lock:
                if job_id not in jobs:
                    return
                if status == "downloading":
                    jobs[job_id]["status"] = "downloading"
                    jobs[job_id]["downloaded_bytes"] = data.get(
                        "downloaded_bytes", 0)
                    jobs[job_id]["total_bytes"] = data.get(
                        "total_bytes") or data.get("total_bytes_estimate")
                    jobs[job_id]["speed"] = data.get("speed")
                    jobs[job_id]["eta"] = data.get("eta")
                elif status == "finished":
                    jobs[job_id]["status"] = "processing"

        options = base_ydl_options()
        options.update({
            "format": format_selector,
            "outtmpl": str(job_dir / "%(title).150B-%(id)s.%(ext)s"),
            "merge_output_format": preferred_ext,
            "progress_hooks": [progress_hook],
            "overwrites": True,
        })
        if quality == "audio":
            options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(raw_url, download=True)
            requested_downloads = info.get("requested_downloads") or []
            candidate_paths = []

            for item in requested_downloads:
                path = item.get("filepath")
                if path:
                    candidate_paths.append(Path(path))

            # The postprocessor/merge may change the final extension. Find the
            # actual output file in the job directory rather than assuming one.
            files = [p for p in job_dir.iterdir() if p.is_file(
            ) and not p.name.endswith((".part", ".ytdl"))]
            if not files:
                for candidate in candidate_paths:
                    if candidate.exists():
                        files.append(candidate)

            if not files:
                raise RuntimeError(
                    "yt-dlp completed without producing an output file.")

            output_path = max(files, key=lambda item: item.stat().st_mtime)
            metadata = public_metadata(info, platform)

        with jobs_lock:
            jobs[job_id].update({
                "status": "completed",
                "video": metadata,
                "filename": output_path.name,
                "file_path": str(output_path),
                "completed_at": utc_now(),
            })

        log_event(
            operation="download",
            platform=platform,
            success=True,
            url=raw_url,
            quality=quality,
        )

    except Exception as exc:
        logging.exception(
            "Video download failed for job=%s platform=%s", job_id, platform)
        shutil.rmtree(job_dir, ignore_errors=True)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "failed",
                    "error": "The download failed. Check the Python terminal for the detailed extractor error.",
                    "error_type": type(exc).__name__,
                    "completed_at": utc_now(),
                })
        log_event(
            operation="download",
            platform=platform,
            success=False,
            url=raw_url,
            quality=quality,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


@app.post("/download", status_code=202)
def start_download(payload: DownloadRequest) -> dict[str, Any]:
    raw_url = validate_video_url(str(payload.url))
    quality = payload.quality.strip().lower()
    # Validate quality before scheduling background work.
    choose_format(quality)

    job_id = uuid.uuid4().hex
    platform = get_platform(raw_url)

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "platform": platform,
            "quality": quality,
            "created_at": utc_now(),
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
        }

    executor.submit(run_download, job_id, raw_url, quality, platform)

    return {
        "success": True,
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/download/{job_id}",
        "file_url": f"/download/{job_id}/file",
    }


@app.get("/download/{job_id}")
def download_status(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(
                status_code=404, detail="Download job not found.")
        # Never expose local filesystem paths through the API.
        result = {key: value for key, value in job.items() if key !=
                  "file_path"}

    if result["status"] == "completed":
        result["file_url"] = f"/download/{job_id}/file"
    return result


@app.get("/download/{job_id}/file")
def get_download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(
                status_code=404, detail="Download job not found.")
        if job.get("status") != "completed":
            raise HTTPException(
                status_code=409,
                detail=f"Download is not ready. Current status: {job.get('status')}.",
            )
        file_path = Path(job.get("file_path", ""))

    # Resolve and verify the path remains inside this service's download folder.
    try:
        resolved_path = file_path.resolve(strict=True)
        resolved_path.relative_to(DOWNLOAD_DIR.resolve())
    except (OSError, ValueError):
        raise HTTPException(
            status_code=404, detail="Downloaded file is no longer available.")

    if not resolved_path.is_file():
        raise HTTPException(
            status_code=404, detail="Downloaded file is no longer available.")

    return FileResponse(
        path=resolved_path,
        filename=resolved_path.name,
        media_type="application/octet-stream",
    )


@app.get("/events")
def recent_events(limit: int = 20) -> dict[str, Any]:
    """Return recent event records for local debugging; URLs are not stored."""
    limit = max(1, min(limit, 100))
    if not EVENT_LOG.exists():
        return {"events": []}

    try:
        lines = EVENT_LOG.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines[-limit:] if line.strip()]
        return {"events": events}
    except (OSError, json.JSONDecodeError):
        logging.exception("Could not read event log")
        raise HTTPException(
            status_code=500, detail="Could not read the event log.")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
