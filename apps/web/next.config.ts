import type { NextConfig } from "next";

/**
 * Every screen here reads a live review queue, so pages render on demand rather
 * than from a component cache.
 */
const nextConfig: NextConfig = {
  cacheComponents: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
