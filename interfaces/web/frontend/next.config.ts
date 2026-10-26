import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

const apiUrl = (process.env.AGENT_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export default function nextConfig(phase: string): NextConfig {
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      // Development uses Next; production serves the original static export via FastAPI.
      async rewrites() {
        return [{ source: "/api/agent/:path*", destination: `${apiUrl}/api/:path*` }];
      },
    };
  }
  return { output: "export", images: { unoptimized: true }, trailingSlash: true };
}
