#!/usr/bin/env node
/**
 * Development launcher for FinIntel. Owns the Python backend and Tauri frontend.
 * Production installers continue to use the packaged, Tauri-managed sidecar.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const isWin = process.platform === "win32";
const venvPython = join(root, ".venv", isWin ? "Scripts" : "bin", isWin ? "python.exe" : "python");
const python = process.env.FININTEL_PYTHON || (existsSync(venvPython) ? venvPython : isWin ? "python" : "python3");
const npm = isWin ? "npm.cmd" : "npm";
const cargoHome = process.env.CARGO_HOME || join(process.env.HOME || process.env.USERPROFILE || "", ".cargo");
const cargoBin = join(cargoHome, "bin");
if (existsSync(join(cargoBin, isWin ? "cargo.exe" : "cargo"))) {
  process.env.PATH = cargoBin + (isWin ? ";" : ":") + (process.env.PATH || "");
}
const children = new Set();
let stopping = false;

function child(command, args) {
  const proc = spawn(command, args, { cwd: root, stdio: "inherit", shell: isWin && command === npm, detached: !isWin, env: process.env });
  children.add(proc);
  proc.on("exit", () => children.delete(proc));
  proc.on("error", error => { console.error(`Could not start ${command}: ${error.message}`); stop(1); });
  return proc;
}

function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const proc of children) {
    if (!proc.killed) {
      // Windows does not forward POSIX signals; taskkill includes descendants.
      if (isWin) {
        spawn("taskkill", ["/pid", String(proc.pid), "/t", "/f"], { stdio: "ignore" });
      } else {
        try { process.kill(-proc.pid, "SIGTERM"); } catch { proc.kill("SIGTERM"); }
      }
    }
  }
  process.exitCode = code;
}
process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));

const health = "http://127.0.0.1:8765/api/v1/health";
try {
  // Do not start Tauri against an unrelated backend that already owns the port.
  try {
    const response = await fetch(health, { signal: AbortSignal.timeout(900) });
    if (response.status) throw new Error("Port 8765 already has a listener. Stop it before using the combined launcher.");
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("Port 8765")) throw error;
  }

  const backend = child(python, ["-m", "uvicorn", "finintel.main:app", "--host", "127.0.0.1", "--port", "8765"]);
  backend.on("exit", (code) => { if (!stopping) stop(code || 1); });
  let ready = false;
  for (let attempt = 0; attempt < 80 && !stopping; attempt++) {
    if (backend.exitCode !== null) break;
    try {
      const res = await fetch(health, { signal: AbortSignal.timeout(700) });
      if (res.ok) {
        const body = await res.json();
        if (body.service === "finintel-backend") { ready = true; break; }
      }
    } catch { /* backend still starting */ }
    await sleep(250);
  }
  if (!ready) throw new Error("Python backend failed to become ready. Install it with: python -m pip install -e './backend[dev]'");
  console.log("\nFinIntel backend ready. Launching Tauri desktop...\n");
  const frontend = child(npm, ["run", "tauri:dev"]);
  frontend.on("exit", code => { if (!stopping) stop(code || 0); });
} catch (error) {
  console.error("\nFinIntel startup failed:", error instanceof Error ? error.message : error);
  stop(1);
}
