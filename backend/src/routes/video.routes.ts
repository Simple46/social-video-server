import { Router, Request, Response } from "express";
import { Readable } from "node:stream";
import { randomUUID } from "node:crypto";
import {
  detectPlatform,
  validateVideoUrl,
} from "../services/platform.service.js";

const router = Router();

const VIDEO_SERVICE_URL =
  process.env.VIDEO_SERVICE_URL ?? "http://127.0.0.1:8000";

router.post("/inspect", async (req, res) => {
  const result = validateVideoUrl(req.body?.url);

  if (!result.valid || !result.url) {
    res.status(400).json({
      success: false,
      message: result.error,
    });
    return;
  }

  try {
    const response = await fetch(`${VIDEO_SERVICE_URL}/inspect`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ url: result.url }),
      signal: AbortSignal.timeout(25_000),
    });

    const data = await response.json();

    if (!response.ok) {
      res.status(response.status).json({
        success: false,
        message: data.detail ?? "Unable to inspect this video.",
      });
      return;
    }

    res.json({
      success: true,
      platform: detectPlatform(result.url),
      video: data.video,
    });
  } catch (error) {
    console.error("Video service request failed:", error);

    res.status(503).json({
      success: false,
      message: "Video inspection service is unavailable. Please try again.",
    });
  }
});

// const VIDEO_SERVICE_URL =
//   process.env.VIDEO_SERVICE_URL ?? "http://127.0.0.1:8000";

// Start a download
router.post("/download", async (req, res) => {
  try {
    const { url, quality = "best" } = req.body ?? {};

    if (typeof url !== "string" || !url.trim()) {
      res.status(400).json({
        success: false,
        message: "A video URL is required.",
      });
      return;
    }

    const allowedQualities = ["best", "720p", "480p", "360p"];

    if (!allowedQualities.includes(quality)) {
      res.status(400).json({
        success: false,
        message: "Unsupported video quality.",
      });
      return;
    }

    const response = await fetch(`${VIDEO_SERVICE_URL}/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url.trim(), quality }),
      signal: AbortSignal.timeout(15000),
    });

    const data = await response.json();

    res.status(response.status).json(data);
  } catch (error) {
    console.error("Start download error:", error);

    res.status(502).json({
      success: false,
      message: "The video service is unavailable. Please try again.",
    });
  }
});

// Check download progress
router.get("/download/:jobId", async (req, res) => {
  try {
    const { jobId } = req.params;

    if (!/^[0-9a-f-]{36}$/i.test(jobId)) {
      res.status(400).json({
        success: false,
        message: "Invalid download ID.",
      });
      return;
    }

    const response = await fetch(`${VIDEO_SERVICE_URL}/download/${jobId}`, {
      signal: AbortSignal.timeout(10000),
    });

    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    console.error("Download status error:", error);

    res.status(502).json({
      success: false,
      message: "Unable to retrieve download status.",
    });
  }
});

// Stream the completed file through Node
router.get("/download/:jobId/file", async (req, res) => {
  try {
    const { jobId } = req.params;

    if (!/^[0-9a-f-]{36}$/i.test(jobId)) {
      res.status(400).json({
        success: false,
        message: "Invalid download ID.",
      });
      return;
    }

    const response = await fetch(
      `${VIDEO_SERVICE_URL}/download/${jobId}/file`,
      { signal: AbortSignal.timeout(30000) },
    );

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({
        detail: "The video file is not ready.",
      }));

      res.status(response.status || 502).json(data);
      return;
    }

    res.setHeader(
      "Content-Type",
      response.headers.get("content-type") ?? "application/octet-stream",
    );

    const disposition = response.headers.get("content-disposition");
    if (disposition) {
      res.setHeader("Content-Disposition", disposition);
    }

    const contentLength = response.headers.get("content-length");
    if (contentLength) {
      res.setHeader("Content-Length", contentLength);
    }

    Readable.fromWeb(
      response.body as import("node:stream/web").ReadableStream,
    ).pipe(res);
  } catch (error) {
    console.error("File download error:", error);

    if (!res.headersSent) {
      res.status(502).json({
        success: false,
        message: "Unable to retrieve the video file.",
      });
    } else {
      res.destroy(error instanceof Error ? error : undefined);
    }
  }
});
export default router;
