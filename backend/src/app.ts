import express from "express";
import cors from "cors";
import videoRoutes from "./routes/video.routes.js";

const app = express();

const frontendOrigin = (
  process.env.FRONTEND_ORIGIN ?? "http://localhost:5173"
).replace(/\/+$/, "");

app.use(
  cors({
    origin: frontendOrigin,
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization"],
  }),
);

app.use(express.json({ limit: "10kb" }));

app.get("/api/health", (_req, res) => {
  res.json({
    success: true,
    service: "social-video-saver-api",
  });
});

app.use("/api/videos", videoRoutes);

app.get("/api/debug/routes", (_req, res) => {
  res.json({
    message: "Latest backend app is running",
    routes: ["/api/health", "/api/videos/inspect", "/api/videos/download"],
  });
});

export default app;
