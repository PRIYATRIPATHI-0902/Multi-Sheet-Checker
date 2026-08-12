// Build / dev-serve without Vite. esbuild only.
import * as esbuild from "esbuild";
import { cp, mkdir } from "node:fs/promises";

const serve = process.argv.includes("--serve");

const options = {
  entryPoints: ["src/index.tsx"],
  bundle: true,
  outdir: "dist",
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  loader: { ".css": "css" },
  sourcemap: serve,
  minify: !serve,
  logLevel: "info",
};

await mkdir("dist", { recursive: true });
await cp("public/index.html", "dist/index.html");

if (serve) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  const { host, port } = await ctx.serve({ servedir: "dist", port: 3000 });
  console.log(`\n  UI running at http://${host === "0.0.0.0" ? "localhost" : host}:${port}`);
  console.log("  Expecting the API on http://localhost:8000\n");
} else {
  await esbuild.build(options);
  console.log("Built to frontend/dist - the FastAPI server will serve it from /");
}
