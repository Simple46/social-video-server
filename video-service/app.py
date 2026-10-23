from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.request
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
    version="1.2.0",
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
# Geo / egress configuration
# ---------------------------------------------------------------------------

def _split_env_list(value: str | None) -> list[str]:
    """Parse a comma-separated environment variable into a clean list."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def mask_proxy(proxy: str) -> str:
    """Hide credentials when reporting a proxy in API responses."""
    parsed = urlparse(proxy)
    if parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return proxy


_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def normalize_proxy(raw: str) -> str | None:
    """Repair and validate a proxy URL, or return None if unusable.

    Handles the two most common paste mistakes: a missing scheme
    ("host:8080" parses as scheme="host") and a stray trailing slash.
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        return None

    if "://" not in value:
        value = "http://" + value

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()

    if scheme not in _PROXY_SCHEMES:
        logging.warning(
            "Ignoring proxy with unsupported scheme %r: %s", scheme, value)
        return None

    if not parsed.hostname:
        logging.warning("Ignoring proxy without a hostname: %s", value)
        return None

    try:
        parsed.port
    except ValueError:
        logging.warning("Ignoring proxy with an invalid port: %s", value)
        return None

    return value


# Proxies are tried in order, after a direct attempt. Put the most reliable
# one first. Accepts http://, https://, socks5:// (socks5 needs pysocks).
_raw_proxies = _split_env_list(os.getenv("CLIPNEST_PROXY_URLS"))
_single_proxy = os.getenv("CLIPNEST_PROXY_URL", "").strip()
if _single_proxy:
    _raw_proxies.insert(0, _single_proxy)

PROXY_URLS: list[str] = []
for _raw in _raw_proxies:
    _normalized = normalize_proxy(_raw)
    if _normalized and _normalized not in PROXY_URLS:
        PROXY_URLS.append(_normalized)

# Optional: route only the tiny geo-verification request through a proxy and
# let the heavy media download go direct. Saves proxy bandwidth. Only used
# when a route has no full proxy configured.
GEO_VERIFICATION_PROXY = normalize_proxy(
    os.getenv("CLIPNEST_GEO_VERIFICATION_PROXY", ""))

# Spoofed X-Forwarded-For countries, used when no proxy is configured or when
# every configured proxy has been exhausted.
GEO_BYPASS_COUNTRIES: list[str] = [
    code.upper() for code in _split_env_list(
        os.getenv(
            "CLIPNEST_GEO_BYPASS_COUNTRIES",
            "IE,ES,IT,SE,NO,FI,PT,CZ,EE,HU,BG,IS,MT,CY,LU",
        )
    )
]

# Optional override for the YouTube client list. Left empty, yt-dlp picks its
# own defaults. Example: "web_safari,tv".
YOUTUBE_PLAYER_CLIENTS: list[str] = _split_env_list(
    os.getenv("CLIPNEST_YOUTUBE_CLIENTS"))

# How long to trust a proxy hostname's DNS result before re-checking.
DNS_CACHE_TTL = 60.0
_dns_cache: dict[str, tuple[float, bool]] = {}
_dns_lock = threading.Lock()


def proxy_host_resolves(proxy: str) -> bool:
    """Cached DNS check so a bogus proxy hostname is skipped, not retried."""
    host = urlparse(proxy).hostname
    if not host:
        return False

    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(host)
        if cached and now - cached[0] < DNS_CACHE_TTL:
            return cached[1]

    try:
        socket.getaddrinfo(host, None)
        resolves = True
    except socket.gaierror:
        resolves = False

    with _dns_lock:
        _dns_cache[host] = (now, resolves)

    if not resolves:
        logging.warning(
            "Proxy host %r does not resolve; skipping that route for now", host)

    return resolves


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
    route: dict[str, Any] | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> None:
    """Append a compact JSONL outcome record; do not save the full URL."""
    record = {
        "timestamp": utc_now(),
        "operation": operation,
        "platform": platform,
        "success": success,
        "quality": quality,
        "host": urlparse(url).hostname,
        "route": route,
        "attempts": attempts,
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


# ---------------------------------------------------------------------------
# yt-dlp options and egress routing
# ---------------------------------------------------------------------------

class EgressFailure(RuntimeError):
    """Every egress route failed. Carries a per-route failure report."""

    def __init__(self, failures: list[dict[str, Any]],
                 permanent: bool = False) -> None:
        self.failures = failures
        self.permanent = permanent
        self.last_error = failures[-1]["error"] if failures else None
        summary = " | ".join(
            f"{item['attempt']}:"
            f"{item['proxy'] or item['geo_bypass_country'] or 'direct'}"
            f" -> {item['error']}"
            for item in failures
        )
        super().__init__(summary or "All egress routes failed.")


def build_ydl_options(
    *,
    proxy: str | None = None,
    geo_country: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build yt-dlp options for a single egress route."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "geo_bypass": True,
        # Do not force a JavaScript runtime: yt-dlp should detect configured
        # runtimes and report its own warnings if one is required.
    }

    if geo_country:
        options["geo_bypass_country"] = geo_country

    if proxy:
        options["proxy"] = proxy
    elif GEO_VERIFICATION_PROXY:
        options["geo_verification_proxy"] = GEO_VERIFICATION_PROXY

    if YOUTUBE_PLAYER_CLIENTS:
        options["extractor_args"] = {
            "youtube": {"player_client": YOUTUBE_PLAYER_CLIENTS}
        }

    if extra:
        options.update(extra)

    return options


def _attempt_plan(*, include_unresolvable: bool = False) -> list[dict[str, Any]]:
    """Ordered egress routes: direct, then proxies, then spoofed countries.

    Proxies whose hostname fails DNS are dropped, unless that would leave no
    proxy routes at all (in which case they are kept so the failure is
    reported rather than silently ignored).
    """
    plan: list[dict[str, Any]] = [{"proxy": None, "geo_country": None}]

    usable = [proxy for proxy in PROXY_URLS
              if include_unresolvable or proxy_host_resolves(proxy)]
    if not usable and PROXY_URLS:
        usable = list(PROXY_URLS)

    plan += [{"proxy": proxy, "geo_country": None} for proxy in usable]
    plan += [{"proxy": None, "geo_country": code}
             for code in GEO_BYPASS_COUNTRIES]
    return plan


def _route_label(route: dict[str, Any]) -> dict[str, Any]:
    """Safe, serialisable description of a route."""
    return {
        "proxy": mask_proxy(route["proxy"]) if route.get("proxy") else None,
        "geo_bypass_country": route.get("geo_country"),
    }


_GEO_MARKERS = (
    "not available in your country",
    "geo restricted",
    "geo-restricted",
    "available in your country",
    "not available in this country",
)

# Only these genuinely mean "another route will not help". Everything else
# (DNS failures, TLS resets, timeouts, 403s, bot checks) is worth retrying.
_PERMANENT_MARKERS = (
    "video unavailable",
    "this video is private",
    "private video",
    "has been removed by the uploader",
    "does not exist",
    "not a valid url",
    "unsupported url",
    "incomplete youtube id",
)


def _is_geo_error(exc: Exception) -> bool:
    if isinstance(exc, yt_dlp.utils.GeoRestrictedError):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _GEO_MARKERS)


def _is_permanent(exc: Exception) -> bool:
    """True when retrying through another egress route cannot help."""
    if _is_geo_error(exc):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _PERMANENT_MARKERS)


def extract_info_with_fallback(
    raw_url: str,
    *,
    download: bool,
    extra: dict[str, Any] | None = None,
    on_route: Any = None,
) -> dict[str, Any]:
    """Extract or download, walking the route plan until one succeeds.

    Raises EgressFailure with a per-route report if every route fails, or if
    a permanent error is hit.
    """
    plan = _attempt_plan()
    failures: list[dict[str, Any]] = []
    last_exc: Exception | None = None

    for index, route in enumerate(plan):
        options = build_ydl_options(**route, extra=extra)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(raw_url, download=download)
            if on_route:
                on_route(route, index, plan)
            return info
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            failures.append({
                "attempt": index + 1,
                "total_routes": len(plan),
                **_route_label(route),
                "geo": _is_geo_error(exc),
                "error_type": type(exc).__name__,
                "error": str(exc).splitlines()[0][:200],
            })

            logging.warning(
                "Route failed (attempt %s/%s, proxy=%s, country=%s): %s",
                index + 1,
                len(plan),
                mask_proxy(route["proxy"]) if route["proxy"] else None,
                route["geo_country"],
                str(exc).splitlines()[0][:200],
            )

            if _is_permanent(exc):
                raise EgressFailure(failures, permanent=True) from exc

            if index < len(plan) - 1:
                time.sleep(0.5)

    raise EgressFailure(failures) from last_exc


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
        "supported_platforms": sorted(
            {"youtube", "tiktok", "instagram", "facebook", "linkedin", "x"}),
        # --- egress / geo configuration ---
        "egress_routes": len(_attempt_plan()),
        "proxies_configured": len(PROXY_URLS),
        "proxy_hosts": [
            {
                "proxy": mask_proxy(proxy),
                "host": urlparse(proxy).hostname,
                "resolves": proxy_host_resolves(proxy),
            }
            for proxy in PROXY_URLS
        ],
        "geo_bypass_countries": GEO_BYPASS_COUNTRIES,
        "geo_verification_proxy": (
            mask_proxy(GEO_VERIFICATION_PROXY)
            if GEO_VERIFICATION_PROXY else None
        ),
        "youtube_player_clients": YOUTUBE_PLAYER_CLIENTS or None,
    }


@app.get("/routes")
def routes() -> dict[str, Any]:
    """The exact order yt-dlp will try egress routes in."""
    return {
        "total_routes": len(_attempt_plan()),
        "routes": [
            {"attempt": index + 1, **_route_label(route)}
            for index, route in enumerate(_attempt_plan())
        ],
    }


@app.get("/proxy-check")
def proxy_check() -> dict[str, Any]:
    """DNS, TCP and HTTP reachability for every configured proxy."""
    results: list[dict[str, Any]] = []

    for proxy in PROXY_URLS:
        parsed = urlparse(proxy)
        host = parsed.hostname or ""
        port = parsed.port
        entry: dict[str, Any] = {
            "proxy": mask_proxy(proxy),
            "host": host,
            "port": port,
        }

        try:
            socket.getaddrinfo(host, None)
            entry["dns"] = "ok"
        except socket.gaierror as exc:
            entry["dns"] = f"failed: {exc}"

        if port and entry["dns"] == "ok":
            try:
                with socket.create_connection((host, port), timeout=5):
                    entry["tcp"] = "ok"
            except OSError as exc:
                entry["tcp"] = f"failed: {exc}"

        # urllib only understands http/https proxies, not socks.
        if entry.get("tcp") == "ok" and parsed.scheme.lower() in {"http", "https"}:
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                with opener.open(
                    "https://www.youtube.com/generate_204", timeout=10
                ) as response:
                    entry["http"] = f"ok ({response.status})"
            except Exception as exc:  # noqa: BLE001
                entry["http"] = f"failed: {type(exc).__name__}: {exc}"

        results.append(entry)

    return {
        "proxies": results,
        "note": (
            "dns=ok and tcp=ok are the minimum. http=ok confirms the proxy "
            "actually forwards HTTPS."
        ),
    }


@app.post("/geo-test")
def geo_test(payload: InspectRequest) -> dict[str, Any]:
    """Test every configured route against a URL and report which ones work.

    Unlike /inspect this does not stop at the first success, so it tells you
    exactly which proxies and spoofed countries are usable right now.
    """
    raw_url = validate_video_url(str(payload.url))
    results: list[dict[str, Any]] = []

    for index, route in enumerate(_attempt_plan()):
        entry: dict[str, Any] = {"attempt": index + 1, **_route_label(route)}
        try:
            with yt_dlp.YoutubeDL(build_ydl_options(**route)) as ydl:
                info = ydl.extract_info(raw_url, download=False)
            entry["ok"] = True
            entry["title"] = info.get("title")
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error_type"] = type(exc).__name__
            entry["error"] = str(exc).splitlines()[0][:180]
        results.append(entry)

    return {
        "url": raw_url,
        "total_routes": len(results),
        "working_routes": sum(1 for item in results if item["ok"]),
        "results": results,
    }


@app.post("/inspect")
def inspect_video(payload: InspectRequest) -> dict[str, Any]:
    raw_url = validate_video_url(str(payload.url))
    platform = get_platform(raw_url)
    used_route: dict[str, Any] = {}

    def record_route(route: dict[str, Any], index: int,
                     plan: list[dict[str, Any]]) -> None:
        used_route.update(_route_label(route))

    try:
        info = extract_info_with_fallback(
            raw_url,
            download=False,
            extra={"extract_flat": False},
            on_route=record_route,
        )

        if not info:
            raise RuntimeError("The extractor returned no video information.")

        result = public_metadata(info, platform)
        log_event(
            operation="inspect",
            platform=platform,
            success=True,
            url=raw_url,
            route=used_route or None,
        )
        return {"success": True, "video": result}

    except HTTPException:
        raise

    except EgressFailure as exc:
        geo_blocked = any(item["geo"] for item in exc.failures)
        log_event(
            operation="inspect",
            platform=platform,
            success=False,
            url=raw_url,
            error_type="EgressFailure",
            error_message=str(exc),
            attempts=exc.failures,
        )
        logging.exception("Video inspection failed for platform=%s", platform)

        if exc.permanent:
            message = "The video is permanently unavailable: " + (
                exc.last_error or "unknown reason")
        elif geo_blocked:
            message = (
                "This video is geo-blocked and none of the configured egress "
                "routes could reach it. Check /proxy-check, then add a proxy "
                "in an allowed country."
            )
        else:
            message = (
                "Every egress route failed. See 'attempts' for the per-route "
                "error, and check /diagnostics and /proxy-check."
            )

        raise HTTPException(
            status_code=422,
            detail={
                "message": message,
                "error_type": "EgressFailure",
                "permanent": exc.permanent,
                "geo_blocked": geo_blocked,
                "attempts": exc.failures,
            },
        )

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
                "message": "Could not inspect this video.",
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

        def record_route(route: dict[str, Any], index: int,
                         plan: list[dict[str, Any]]) -> None:
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["route"] = {
                        **_route_label(route),
                        "attempt": index + 1,
                        "total_routes": len(plan),
                    }

        download_options: dict[str, Any] = {
            "format": format_selector,
            "outtmpl": str(job_dir / "%(title).150B-%(id)s.%(ext)s"),
            "merge_output_format": preferred_ext,
            "progress_hooks": [progress_hook],
            "overwrites": True,
        }
        if quality == "audio":
            download_options["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        info = extract_info_with_fallback(
            raw_url,
            download=True,
            extra=download_options,
            on_route=record_route,
        )

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
            route=jobs.get(job_id, {}).get("route"),
        )

    except Exception as exc:
        logging.exception(
            "Video download failed for job=%s platform=%s", job_id, platform)
        shutil.rmtree(job_dir, ignore_errors=True)

        failures = getattr(exc, "failures", None)
        geo_blocked = bool(failures) and any(
            item.get("geo") for item in failures)

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "failed",
                    "error": (
                        "This video is geo-blocked and no configured egress "
                        "route could reach it."
                        if geo_blocked
                        else "The download failed on every configured egress "
                             "route. See 'attempts' for details."
                    ),
                    "error_type": type(exc).__name__,
                    "geo_blocked": geo_blocked,
                    "attempts": failures,
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
            attempts=failures,
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
        background=BackgroundTask(shutil.rmtree, resolved_path.parent,
                                  ignore_errors=True),
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

