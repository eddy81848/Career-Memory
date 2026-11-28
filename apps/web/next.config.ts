import type { NextConfig } from "next";
import path from "node:path";

const liveModeEnabled =
  process.env.NEXT_PUBLIC_API_MODE === "live" ||
  Boolean(process.env.CAREER_MEMORY_API_ORIGIN);

const apiOrigin = (
  process.env.CAREER_MEMORY_API_ORIGIN || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    if (!liveModeEnabled) {
      return [];
    }
    return [
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
    ];
  },
};

export default nextConfig;
