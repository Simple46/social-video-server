import express from "express";
import cors from "cors";
import videoRoutes from "./routes/video.routes.js";

const app = express();

app.use(
  cors({
    origin: (process.env.FRONTEND_ORIGIN ?? "http://localhost:5173").replace(
      /\/+$/,
      "",
    ),
  }),
);

app.use(express.json());

app.use("/api/videos", videoRoutes);
app.use(express.json({ limit: "10kb" }));

app.get("/api/health", (_req, res) => {
  res.json({
    success: true,
    service: "social-video-saver-api",
  });
});

app.use("/api/videos", videoRoutes);

export default app;
