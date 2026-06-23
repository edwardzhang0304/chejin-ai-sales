import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(packageRoot, "../..");
const outputDir = resolve(repoRoot, "worker-client/chejin_worker_client/web_assets");
const esbuildBin = resolve(repoRoot, "frontend/node_modules/.bin/esbuild");

mkdirSync(outputDir, { recursive: true });

copyFileSync(resolve(packageRoot, "static/index.html"), resolve(outputDir, "index.html"));
copyFileSync(resolve(packageRoot, "src/worker-ui.css"), resolve(outputDir, "worker-ui.css"));
copyFileSync(resolve(packageRoot, "src/worker-ui.tokens.css"), resolve(outputDir, "worker-ui.tokens.css"));

const result = spawnSync(
  esbuildBin,
  [
    resolve(packageRoot, "src/WorkerClientRuntimeApp.tsx"),
    "--bundle",
    "--format=esm",
    "--platform=browser",
    "--jsx=automatic",
    `--outfile=${resolve(outputDir, "worker-web-app.js")}`,
  ],
  {
    cwd: repoRoot,
    env: {
      ...process.env,
      NODE_PATH: resolve(repoRoot, "frontend/node_modules"),
    },
    stdio: "inherit",
  },
);

if (result.status !== 0) {
  process.exit(result.status ?? 1);
}
