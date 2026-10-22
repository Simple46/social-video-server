import express from "express";
import cors from "cors";
import videoRoutes from "./routes/video.routes.js";

const app = express();

app.use(
  cors({
    origin:
      process.env.FRONTEND_ORIGIN ??
      "https://frontend-virid-five-97.vercel.app/",
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

export default app;
