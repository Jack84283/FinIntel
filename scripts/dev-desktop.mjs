#!/usr/bin/env node
/**
 * Development launcher for FinIntel. Owns the Python backend and Tauri frontend.
 * Production installers continue to use the packaged, Tauri-managed sidecar.
 */
import { spawn, execFileSync } from "node:child_process";
import { randomBytes, createHash } from "node:crypto";
import { existsSync, openSync, closeSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { connect } from "node:net";
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
// Shared only with the launched Python and native Tauri processes, never Vite/client code.
process.env.FININTEL_PROCESS_TOKEN = randomBytes(32).toString("hex");
const lockPath = join(tmpdir(), "finintel-dev-" + createHash("sha256").update(root).digest("hex").slice(0, 24) + ".lock");
const instanceId = randomBytes(16).toString("hex");
let ownLock = false;
const children = new Set();

function alive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error?.code === "EPERM"; }
}

// PID reuse or an unrelated listener must never lead to an unrelated process being killed.
function matchesLauncher(pid) {
  try {
    const command = isWin
      ? execFileSync("powershell.exe", ["-NoProfile", "-Command", "(Get-CimInstance Win32_Process -Filter 'ProcessId = " + pid + "').CommandLine"], { encoding: "utf8", timeout: 3000 })
      : execFileSync("ps", ["-p", String(pid), "-o", "command="], { encoding: "utf8", timeout: 3000 });
    return command.includes("dev-desktop.mjs") && command.includes("node");
  } catch { return false; }
}

async function portFree(port) {
  return new Promise(resolveFree => {
    const socket = connect({ host: "127.0.0.1", port });
    socket.setTimeout(450);
    socket.once("connect", () => { socket.destroy(); resolveFree(false); });
    socket.once("timeout", () => { socket.destroy(); resolveFree(false); });
    socket.once("error", error => { socket.destroy(); resolveFree(error.code === "ECONNREFUSED"); });
  });
}

// Migrate old development sessions launched before the lock existed.
// Only stop listeners whose command line is a FinIntel backend or Vite process
// and whose working directory is within this exact checkout.
function legacyListenerPids(port) {
  if (isWin) return []; // Windows legacy ownership cannot be verified safely.
  try {
    return execFileSync("lsof", ["-nP", "-t", "-iTCP:" + port, "-sTCP:LISTEN"], { encoding: "utf8", timeout: 3000 })
      .trim().split(/\\s+/).map(Number).filter(pid => Number.isSafeInteger(pid) && pid > 0);
  } catch { return []; }
}

function verifiedLegacy(pid, port) {
  if (isWin) return false;
  try {
    const command = execFileSync("ps", ["-p", String(pid), "-o", "command="], { encoding: "utf8", timeout: 3000 }).trim();
    const cwd = execFileSync("lsof", ["-a", "-p", String(pid), "-d", "cwd", "-Fn"], { encoding: "utf8", timeout: 3000 })
      .split("\\n").find(line => line.startsWith("n"))?.slice(1);
    const inCheckout = cwd === root || cwd?.startsWith(root + "/");
    const expected = port === 8765
      ? /(?:uvicorn.*finintel\\.main:app|finintel\\.desktop_entry)/.test(command)
      : /(?:vite|tauri)/.test(command) && command.includes("FinIntel");
    return Boolean(inCheckout && expected);
  } catch { return false; }
}

async function stopLegacyListeners() {
  if (isWin) return;
  for (const port of [8765, 1420]) {
    for (const pid of legacyListenerPids(port)) {
      if (!verifiedLegacy(pid, port)) continue;
      console.log("Stopping previous FinIntel process on port " + port + " (PID " + pid + ")...");
      try { process.kill(pid, "SIGTERM"); } catch (error) { if (error?.code !== "ESRCH") throw error; }
    }
  }
}

async function waitForPorts() {
  for (let attempt = 0; attempt < 50; attempt++) {
    if (await portFree(8765) && await portFree(1420)) return;
    await sleep(200);
  }
  throw new Error("FinIntel ports 8765/1420 are still in use. Refusing to terminate unrelated processes.");
}

function lockOwner() {
  try { return JSON.parse(readFileSync(lockPath, "utf8")); }
  catch (error) { if (error?.code === "ENOENT") return null; throw new Error("Cannot inspect FinIntel launcher lock."); }
}

async function acquireLock() {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const fd = openSync(lockPath, "wx", 0o600);
      try { writeFileSync(fd, JSON.stringify({ pid: process.pid, id: instanceId, root })); }
      finally { closeSync(fd); }
      ownLock = true;
      await stopLegacyListeners();
      await waitForPorts();
      return;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
    const owner = lockOwner();
    if (!owner) continue;
    if (owner.root !== root) throw new Error("Launcher lock belongs to a different checkout.");
    if (alive(owner.pid)) {
      if (!matchesLauncher(owner.pid)) throw new Error("FinIntel launcher PID cannot be verified; refusing to stop it.");
      console.log("Stopping previous FinIntel development instance (PID " + owner.pid + ")...");
      process.kill(owner.pid, "SIGTERM");
      for (let i = 0; i < 50 && alive(owner.pid); i++) await sleep(200);
      if (alive(owner.pid)) throw new Error("Previous FinIntel instance did not stop cleanly.");
    }
    // Only remove the same lock observed above; do not unlink a new owner's lock.
    const current = lockOwner();
    if (current?.id === owner.id && current?.pid === owner.pid) unlinkSync(lockPath);
  }
  throw new Error("Could not acquire FinIntel development launcher ownership.");
}

function releaseLock() {
  if (!ownLock) return;
  try { if (lockOwner()?.id === instanceId) unlinkSync(lockPath); }
  catch { /* avoid masking shutdown */ }
  ownLock = false;
}

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
  releaseLock();
  process.exitCode = code;
}
process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));

const health = "http://127.0.0.1:8765/api/v1/health";
try {
  await acquireLock();

  const backend = child(python, ["-m", "uvicorn", "finintel.main:app", "--host", "127.0.0.1", "--port", "8765"]);
  backend.on("exit", (code) => { if (!stopping) stop(code || 1); });
  let ready = false;
  for (let attempt = 0; attempt < 80 && !stopping; attempt++) {
    if (backend.exitCode !== null) break;
    try {
      const res = await fetch(health, { headers: { "x-finintel-process-token": process.env.FININTEL_PROCESS_TOKEN }, signal: AbortSignal.timeout(700) });
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
