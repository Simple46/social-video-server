export type VideoPlatform =
  | "youtube"
  | "tiktok"
  | "instagram"
  | "facebook"
  | "linkedin"
  | "x"
  | "unknown";

export function detectPlatform(url: string): VideoPlatform {
  let hostname: string;

  try {
    hostname = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return "unknown";
  }

  // Normalize www and mobile subdomains through domain matching.
  const host = hostname.replace(/^www\./, "");

  if (
    host === "youtube.com" ||
    host.endsWith(".youtube.com") ||
    host === "youtu.be" ||
    host === "youtube-nocookie.com" ||
    host.endsWith(".youtube-nocookie.com")
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
    host === "fb.watch"
  ) {
    return "facebook";
  }

  if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
    return "linkedin";
  }

  if (
    host === "x.com" ||
    host.endsWith(".x.com") ||
    host === "twitter.com" ||
    host.endsWith(".twitter.com")
  ) {
    return "x";
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

  if (parsed.protocol !== "https:") {
    return {
      valid: false,
      error: "Please use a secure HTTPS video link.",
    };
  }

  if (
    parsed.username ||
    parsed.password ||
    (parsed.port && parsed.port !== "443")
  ) {
    return { valid: false, error: "This URL is not allowed." };
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
