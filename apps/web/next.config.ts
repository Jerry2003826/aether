import type { NextConfig } from "next";

const apiBase = process.env.AETHER_API_BASE ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${apiBase}/v1/:path*` },
      { source: "/health/:path*", destination: `${apiBase}/health/:path*` },
    ];
  },
};

export default nextConfig;
