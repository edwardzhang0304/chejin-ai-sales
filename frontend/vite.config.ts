import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";

const contract = JSON.parse(readFileSync(new URL("../contracts/c2_contract_v3.json", import.meta.url), "utf8"));

export default defineConfig({
  plugins: [react()],
  resolve: { dedupe: ["react", "react-dom", "lucide-react"] },
  define: { "import.meta.env.VITE_PRODUCT_VERSION": JSON.stringify(contract.contract_revision) },
  server: {
    host: "127.0.0.1",
    port: 5173,
    fs: {
      allow: [fileURLToPath(new URL("..", import.meta.url))],
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
});
