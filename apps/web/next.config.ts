import type { NextConfig } from "next";
import path from "node:path";

const apiMode = (process.env.NEXT_PUBLIC_API_MODE || "mock")
  .trim()
  .toLowerCase();

const hostedModeEnabled = apiMode === "hosted";

// Preserve Track 1 local fullstack PG18 rewrites when NEXT_PUBLIC_API_MODE=live
// (or when CAREER_MEMORY_API_ORIGIN is set outside hosted mode).
// Hosted mode MUST NEVER expose /local-dev/:path* or unauthenticated /v1/:path* rewrites.
const liveModeEnabled =
  !hostedModeEnabled &&
  (apiMode === "live" || Boolean(process.env.CAREER_MEMORY_API_ORIGIN));

const apiOrigin = (
  process.env.CAREER_MEMORY_API_ORIGIN || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    if (!liveModeEnabled) {
      return [];
    }
    return {
      beforeFiles: [
        {
          source: "/v1/:path*",
          destination: `${apiOrigin}/v1/:path*`,
        },
        {
          source: "/healthz",
          destination: `${apiOrigin}/healthz`,
        },
        {
          source: "/local-dev/:path*",
          destination: `${apiOrigin}/local-dev/:path*`,
        },
      ],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default nextConfig;
