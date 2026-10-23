import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { dedupe: ["react", "react-dom", "lucide-react"] },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
