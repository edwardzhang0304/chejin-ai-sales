import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(packageRoot, "../..");
const esbuildBin = resolve(repoRoot, "frontend/node_modules/.bin/esbuild");

mkdirSync(resolve(packageRoot, "dist"), { recursive: true });

const result = spawnSync(
  esbuildBin,
  [
    resolve(packageRoot, "preview.tsx"),
    "--bundle",
    "--format=iife",
    "--platform=browser",
    "--jsx=automatic",
    `--outfile=${resolve(packageRoot, "dist/preview.js")}`,
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
