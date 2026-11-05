import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    // Store-level tests only: no DOM needed, keeps devDeps light.
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
