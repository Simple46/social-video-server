export type VideoPlatform =
  | "youtube"
  | "tiktok"
  | "instagram"
  | "facebook"
  | "unknown";

export function detectPlatform(url: string): VideoPlatform {
  let hostname: string;

  try {
    hostname = new URL(url).hostname.toLowerCase();
  } catch {
    return "unknown";
  }

  const host = hostname.replace(/^www\./, "");

  if (
    host === "youtube.com" ||
    host === "m.youtube.com" ||
    host === "youtu.be" ||
    host === "youtube-nocookie.com"
  ) {
    return "youtube";
  }

  if (host === "tiktok.com" || host.endsWith(".tiktok.com")) {
    return "tiktok";
  }

  if (host === "instagram.com" || host.endsWith(".instagram.com")) {
    return "instagram";
  }

  if (
    host === "facebook.com" ||
    host.endsWith(".facebook.com") ||
    host === "fb.watch" ||
    host.endsWith(".fb.watch")
  ) {
    return "facebook";
  }

  return "unknown";
}

export function validateVideoUrl(input: unknown): {
  valid: boolean;
  url?: string;
  error?: string;
} {
  if (typeof input !== "string" || !input.trim()) {
    return { valid: false, error: "Please provide a video URL." };
  }

  let parsed: URL;

  try {
    parsed = new URL(input.trim());
  } catch {
    return { valid: false, error: "Please enter a valid URL." };
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { valid: false, error: "Only HTTP and HTTPS links are supported." };
  }

  const platform = detectPlatform(parsed.toString());

  if (platform === "unknown") {
    return {
      valid: false,
      error: "This platform is not supported yet.",
    };
  }

  return { valid: true, url: parsed.toString() };
}
