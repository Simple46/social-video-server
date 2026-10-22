import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clapperboard,
  Clipboard,
  Download,
  ExternalLink,
  FileVideo,
  Film,
  LoaderCircle,
  Moon,
  ShieldCheck,
  Sun,
  Video,
  X,
  PlayCircle,
} from "lucide-react";
import "./index.css";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:4000/api";

type VideoFormat = {
  formatId: string;
  ext?: string;
  height?: number | null;
  width?: number | null;
  filesize?: number | null;
};

type VideoInfo = {
  title: string;
  platform?: string;
  duration?: number | null;
  thumbnail?: string | null;
  uploader?: string | null;
  formats?: VideoFormat[];
};

type DownloadJob = {
  id: string;
  status: "queued" | "downloading" | "completed" | "failed";
  progress?: number;
  filename?: string;
  filesize?: number;
  error?: string;
  message?: string;
};

const qualities = [
  { value: "best", label: "Best quality", detail: "Highest available" },
  { value: "720p", label: "720p HD", detail: "HD video" },
  { value: "480p", label: "480p", detail: "Standard quality" },
  { value: "360p", label: "360p", detail: "Smaller file" },
];

function formatDuration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) {
    return "Duration unavailable";
  }

  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);

  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatBytes(bytes?: number) {
  if (bytes == null || !Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit++;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function getPlatform(url: string) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    if (host.includes("youtube") || host === "youtu.be") return "YouTube";
    if (host.includes("tiktok")) return "TikTok";
    if (host.includes("instagram")) return "Instagram";
    if (host === "fb.watch" || host.includes("facebook")) return "Facebook";
    return "Social video";
  } catch {
    return "Social video";
  }
}

function App() {
  const [url, setUrl] = useState("");
  const [quality, setQuality] = useState("720p");
  const [video, setVideo] = useState<VideoInfo | null>(null);
  const [job, setJob] = useState<DownloadJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [dark, setDark] = useState(
    () => localStorage.getItem("clipnest-theme") === "dark",
  );

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("clipnest-theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    if (!job?.id || job.status === "completed" || job.status === "failed") {
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      try {
        const response = await fetch(
          `${API_URL}/videos/download/${encodeURIComponent(job.id)}`,
        );
        const data = await response.json();

        if (!response.ok) {
          throw new Error(
            data.message || data.detail || "Could not check download progress.",
          );
        }

        const nextJob: DownloadJob = data.job ?? data;
        if (cancelled) return;

        setJob(nextJob);

        if (nextJob.status === "completed") {
          setDownloading(false);
        } else if (nextJob.status === "failed") {
          setDownloading(false);
          setError(
            nextJob.error ||
              nextJob.message ||
              "The download failed. Please try another link or quality.",
          );
        } else {
          timer = setTimeout(poll, 1200);
        }
      } catch (err) {
        if (cancelled) return;
        setDownloading(false);
        setError(
          err instanceof Error
            ? err.message
            : "Connection lost while checking progress.",
        );
      }
    };

    timer = setTimeout(poll, 700);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [job?.id, job?.status]);

  async function inspectVideo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setVideo(null);
    setJob(null);

    let parsedUrl: URL;
    try {
      parsedUrl = new URL(url.trim());
      if (!["http:", "https:"].includes(parsedUrl.protocol)) {
        throw new Error("Use a valid video link.");
      }
    } catch {
      setError("Enter a valid video URL, including https://");
      return;
    }

    setLoading(true);
    try {
      const response = await fetch(`${API_URL}/videos/inspect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: parsedUrl.toString() }),
      });

      const data = await response.json();
      if (!response.ok || data.success === false) {
        throw new Error(
          data.message || data.detail || "We couldn't retrieve this video.",
        );
      }

      const info = data.video ?? data.data ?? data;
      if (!info.title) {
        throw new Error("Video details were missing from the server response.");
      }

      setVideo(info as VideoInfo);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function startDownload() {
    if (!video || !url.trim() || downloading) return;

    setError("");
    setJob(null);
    setDownloading(true);

    try {
      const response = await fetch(`${API_URL}/videos/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), quality }),
      });

      const data = await response.json();
      if (!response.ok || data.success === false) {
        throw new Error(
          data.message || data.detail || "Unable to start the download.",
        );
      }

      const newJob: DownloadJob = data.job ?? data;
      if (!newJob.id) {
        throw new Error("The server did not return a download job ID.");
      }
      setJob(newJob);
    } catch (err) {
      setDownloading(false);
      setError(
        err instanceof Error
          ? err.message
          : "Unable to start the download. Check your connection.",
      );
    }
  }

  function downloadFile() {
    if (!job?.id) return;
    window.location.assign(
      `${API_URL}/videos/download/${encodeURIComponent(job.id)}/file`,
    );
  }

  async function pasteLink() {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        setUrl(text.trim());
        setError("");
      }
    } catch {
      setError(
        "Clipboard access isn't available. Paste your link into the field.",
      );
    }
  }

  const platform = getPlatform(url);
  const progress = Math.max(0, Math.min(100, job?.progress ?? 0));
  const isComplete = job?.status === "completed";

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" aria-label="Clipnest home">
          <span className="brand-mark">
            <Clapperboard size={21} />
          </span>
          <span>
            clipnest<span className="brand-dot">.</span>
          </span>
        </a>

        <nav className="desktop-nav" aria-label="Main navigation">
          <a href="#downloader">Downloader</a>
          <a href="#how-it-works">How it works</a>
        </nav>

        <button
          className="theme-toggle"
          onClick={() => setDark((value) => !value)}
          aria-label={`Switch to ${dark ? "light" : "dark"} mode`}
          title={`Switch to ${dark ? "light" : "dark"} mode`}
        >
          {dark ? <Sun size={18} /> : <Moon size={18} />}
        </button>
      </header>

      <section className="hero" id="downloader">
        <div className="eyebrow">
          <span className="eyebrow-icon">
            <ArrowDownToLine size={14} />
          </span>
          YOUR VIDEOS, YOUR WAY
        </div>

        <h1>
          Good videos deserve
          <br />
          <span>to be kept.</span>
        </h1>

        <p className="hero-copy">
          Paste a video link, choose your quality, and save it to your device.
          Simple as that.
        </p>

        <div className="platform-list" aria-label="Supported platforms">
          <span>
            <PlayCircle size={16} /> YouTube
          </span>
          <i />
          <span>
            <Video size={16} /> TikTok
          </span>
          <i />
          <span>
            <Film size={16} /> Instagram
          </span>
          <i />
          <span>
            <Clapperboard size={16} /> Facebook
          </span>
        </div>
      </section>

      <section className="downloader-card" aria-label="Video downloader">
        <div className="card-heading">
          <div>
            <span className="section-kicker">VIDEO DOWNLOADER</span>
            <h2>Start with a link</h2>
          </div>
          <span className="private-note">
            <ShieldCheck size={15} /> No account needed
          </span>
        </div>

        <form onSubmit={inspectVideo}>
          <label className="field-label" htmlFor="video-url">
            Video URL
          </label>
          <div className={`url-field ${error ? "has-error" : ""}`}>
            <span className="url-field-icon">
              <ExternalLink size={18} />
            </span>
            <input
              id="video-url"
              type="url"
              placeholder="Paste your video link here..."
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
                if (error) setError("");
              }}
              autoComplete="url"
              required
            />
            {url ? (
              <button
                type="button"
                className="clear-input"
                onClick={() => {
                  setUrl("");
                  setVideo(null);
                  setJob(null);
                  setError("");
                }}
                aria-label="Clear URL"
              >
                <X size={17} />
              </button>
            ) : (
              <button
                type="button"
                className="paste-button"
                onClick={pasteLink}
              >
                <Clipboard size={15} /> <span>Paste</span>
              </button>
            )}
          </div>

          <button
            className="primary-button inspect-button"
            type="submit"
            disabled={loading || !url.trim()}
          >
            {loading ? (
              <>
                <LoaderCircle className="spin" size={18} /> Finding your
                video...
              </>
            ) : (
              <>
                Find video <ArrowRight size={18} />
              </>
            )}
          </button>
        </form>

        {error && (
          <div className="alert alert-error" role="alert">
            <CircleAlert size={19} />
            <span>{error}</span>
            <button
              type="button"
              onClick={() => setError("")}
              aria-label="Dismiss error"
            >
              <X size={16} />
            </button>
          </div>
        )}

        {loading && (
          <div className="loading-panel" role="status">
            <span className="loading-orb">
              <LoaderCircle className="spin" size={24} />
            </span>
            <div>
              <strong>Looking for your video</strong>
              <p>Checking the link and available video details...</p>
            </div>
          </div>
        )}

        {video && !loading && (
          <div className="video-result">
            <div className="result-topline">
              <span className="result-label">
                <CheckCircle2 size={16} /> VIDEO FOUND
              </span>
              <span className="platform-tag">{video.platform || platform}</span>
            </div>

            <div className="video-preview">
              {video.thumbnail ? (
                <img src={video.thumbnail} alt="" className="video-thumbnail" />
              ) : (
                <div className="thumbnail-placeholder">
                  <FileVideo size={32} />
                </div>
              )}
              {video.duration != null && (
                <span className="duration-tag">
                  {formatDuration(video.duration)}
                </span>
              )}
            </div>

            <div className="video-meta">
              <h3>{video.title}</h3>
              <p>{video.uploader || "Video creator"}</p>
            </div>

            <div className="quality-heading">
              <label htmlFor="quality">Choose video quality</label>
              <span>MP4 when supported</span>
            </div>

            <div
              className="quality-options"
              role="group"
              aria-label="Video quality"
            >
              {qualities.map((option) => (
                <button
                  type="button"
                  key={option.value}
                  className={`quality-option ${quality === option.value ? "selected" : ""}`}
                  onClick={() => setQuality(option.value)}
                  aria-pressed={quality === option.value}
                >
                  <span className="quality-radio">
                    {quality === option.value && <span />}
                  </span>
                  <span className="quality-copy">
                    <strong>{option.label}</strong>
                    <small>{option.detail}</small>
                  </span>
                  {quality === option.value && (
                    <Check size={16} className="quality-check" />
                  )}
                </button>
              ))}
            </div>

            {!isComplete && (
              <button
                className="primary-button download-button"
                onClick={startDownload}
                disabled={downloading}
              >
                {downloading ? (
                  <>
                    <LoaderCircle className="spin" size={19} /> Preparing
                    download...
                  </>
                ) : (
                  <>
                    <Download size={19} /> Download video{" "}
                    <ArrowRight size={18} />
                  </>
                )}
              </button>
            )}
          </div>
        )}

        {downloading && job && !isComplete && (
          <div className="progress-panel" role="status" aria-live="polite">
            <div className="progress-title">
              <span className="progress-icon">
                <LoaderCircle className="spin" size={19} />
              </span>
              <div>
                <strong>
                  {job.status === "queued"
                    ? "Waiting to start..."
                    : "Preparing your video..."}
                </strong>
                <p>This may take a little while for larger videos.</p>
              </div>
              <span className="progress-percent">{Math.round(progress)}%</span>
            </div>
            <div
              className="progress-track"
              role="progressbar"
              aria-label="Download progress"
              aria-valuenow={progress}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <span style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        {isComplete && (
          <div className="success-panel" role="status">
            <span className="success-icon">
              <CheckCircle2 size={25} />
            </span>
            <div className="success-copy">
              <strong>Your video is ready!</strong>
              <p>
                {job.filename || "Video prepared"}
                {job.filesize ? ` · ${formatBytes(job.filesize)}` : ""}
              </p>
              <button className="text-button" onClick={downloadFile}>
                Save video to device <ArrowDownToLine size={16} />
              </button>
            </div>
          </div>
        )}

        <div className="card-footnote">
          <ShieldCheck size={15} />
          <span>
            Use this tool only for videos you have permission to save.
          </span>
        </div>
      </section>

      <section className="how-section" id="how-it-works">
        <div className="how-heading">
          <span className="section-kicker">THREE SIMPLE STEPS</span>
          <h2>From link to library.</h2>
          <p>No complicated setup. Just the video you want to keep.</p>
        </div>

        <div className="steps-grid">
          <article className="step-card">
            <span className="step-number">01</span>
            <span className="step-icon">
              <ExternalLink size={22} />
            </span>
            <h3>Paste your link</h3>
            <p>Copy a supported video URL and drop it into the box above.</p>
          </article>
          <article className="step-card">
            <span className="step-number">02</span>
            <span className="step-icon">
              <Clapperboard size={22} />
            </span>
            <h3>Choose your quality</h3>
            <p>
              Pick the resolution that makes sense for your device and
              connection.
            </p>
          </article>
          <article className="step-card">
            <span className="step-number">03</span>
            <span className="step-icon">
              <Download size={22} />
            </span>
            <h3>Save it locally</h3>
            <p>
              When processing finishes, save the video directly to your device.
            </p>
          </article>
        </div>
      </section>

      <footer className="footer">
        <a className="footer-brand" href="#">
          <Clapperboard size={17} /> clipnest.
        </a>
        <span>Made for the videos worth keeping.</span>
        <a href="#downloader" className="back-top">
          Back to top <ChevronDown size={14} />
        </a>
      </footer>
    </main>
  );
}

export default App;
