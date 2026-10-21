import { useCallback, useEffect, useState } from "react";
import { fetchBackendHealth } from "./api";
import type { BackendHealth } from "./health";

type Connection =
  | { kind: "loading" }
  | { kind: "connected"; health: BackendHealth }
  | { kind: "disconnected"; message: string };

const pending = [{ number: "02", label: "Public history acquisition" }, { number: "03", label: "SQLite persistence and APIs" }, { number: "04", label: "Market charts and data grids" }];

export default function App() {
  const [connection, setConnection] = useState<Connection>({ kind: "loading" });

  const checkHealth = useCallback(async (signal?: { cancelled: boolean }) => {
    setConnection({ kind: "loading" });
    try {
      const health = await fetchBackendHealth();
      if (!signal?.cancelled) setConnection({ kind: "connected", health });
    } catch (error: unknown) {
      if (!signal?.cancelled) {
        setConnection({
          kind: "disconnected",
          message: error instanceof Error ? error.message : "Backend unavailable"
        });
      }
    }
  }, []);

  useEffect(() => {
    const signal = { cancelled: false };
    void checkHealth(signal);
    return () => {
      signal.cancelled = true;
    };
  }, [checkHealth]);

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand-mark" aria-hidden="true">FI</div>
        <span className="side-label">Workspace</span>
        <nav aria-label="Sections">
          <a href="#overview" className="nav-item current" aria-current="page">Overview</a>
          <span className="nav-item disabled" aria-disabled="true">Markets <small>PR #4</small></span>
          <span className="nav-item disabled" aria-disabled="true">Research <small>Later</small></span>
          <span className="nav-item disabled" aria-disabled="true">AI Desk <small>Later</small></span>
        </nav>
        <div className="sidebar-bottom">PRIVATE · LOCAL FIRST</div>
      </aside>

      <main id="overview" className="main">
        <header className="topbar">
          <div><span className="eyebrow">FININTEL / OVERVIEW</span><h1>Private Financial Intelligence</h1></div>
          <span className="release">BOOTSTRAP v0.1</span>
        </header>

        <section className="intro">
          <span className="eyebrow">FOUNDATION / PR #1</span>
          <h2>Ready for market intelligence.</h2>
          <p>The desktop shell and local Python service are the foundation. Public market data, persistence, and charts arrive in upcoming pull requests.</p>
        </section>

        <div className="panel-grid">
          <section className="panel" aria-labelledby="backend-title">
            <div className="panel-head"><h3 id="backend-title">Python backend</h3><span className="panel-tag">LOCAL API</span></div>
            <p className="description">Health check via typed Tauri command or Vite development proxy.</p>
            <div role="status" aria-live="polite" className="status-row">
              <span className={`status-dot ${connection.kind}`} aria-hidden="true" />
              <strong>{connection.kind === "loading" ? "Checking…" : connection.kind === "connected" ? "Connected" : "Unavailable"}</strong>
            </div>
            <p className="details">
              {connection.kind === "connected"
                ? `finintel-backend v${connection.health.version}`
                : connection.kind === "disconnected"
                  ? `Start the Python service on 127.0.0.1:8765. ${connection.message}`
                  : "Contacting local service…"}
            </p>
            <button type="button" onClick={() => void checkHealth()} disabled={connection.kind === "loading"}>
              Recheck connection
            </button>
          </section>

          <section className="panel" aria-labelledby="plan-title">
            <div className="panel-head"><h3 id="plan-title">Implementation roadmap</h3><span className="panel-tag">M1</span></div>
            <p className="description">Data first, visualization second, intelligence later.</p>
            <ol className="roadmap">
              {pending.map((item) => <li key={item.number}><span>{item.number}</span>{item.label}</li>)}
            </ol>
          </section>
        </div>

        <footer>FinIntel · No cloud backend · No synthetic market data</footer>
      </main>
    </div>
  );
}
