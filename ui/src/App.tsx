import { useEffect, useState, type ReactElement } from "react";
import EvalsView from "./views/EvalsView";
import EvalView from "./views/EvalView";
import SessionView from "./views/SessionView";
import SessionsView from "./views/SessionsView";

// Hash-based routing so the StaticFiles html=True mount needs no SPA fallback.
// Routes: `#/sessions` (default), `#/sessions/:runId`, `#/evals`, and
// `#/evals/:reportId`.

type Route =
  | { kind: "sessions" }
  | { kind: "session"; runId: string }
  | { kind: "evals" }
  | { kind: "eval"; reportId: string };

function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "");
  const parts = path.split("/").filter((part) => part.length > 0);
  if (parts.length === 2 && parts[0] === "sessions") {
    return { kind: "session", runId: decodeURIComponent(parts[1]) };
  }
  if (parts.length === 1 && parts[0] === "evals") {
    return { kind: "evals" };
  }
  if (parts.length === 2 && parts[0] === "evals") {
    return { kind: "eval", reportId: decodeURIComponent(parts[1]) };
  }
  return { kind: "sessions" };
}

export function navigateToSession(runId: string): void {
  window.location.hash = `#/sessions/${encodeURIComponent(runId)}`;
}

export function navigateToSessions(): void {
  window.location.hash = "#/sessions";
}

export function navigateToEvals(): void {
  window.location.hash = "#/evals";
}

export function navigateToEval(reportId: string): void {
  window.location.hash = `#/evals/${encodeURIComponent(reportId)}`;
}

export default function App(): ReactElement {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    if (window.location.hash === "") {
      window.location.replace("#/sessions");
    }
    const onHashChange = (): void => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <a className="app-title" href="#/sessions">
          ⬡ loopforge operator console
        </a>
      </header>
      <main className="app-main">
        {route.kind === "sessions" ? (
          <SessionsView />
        ) : route.kind === "session" ? (
          // Keyed by runId: switching sessions must remount the view so no
          // events, detail, artifacts, or fatal banners leak across runs.
          <SessionView key={route.runId} runId={route.runId} />
        ) : route.kind === "evals" ? (
          <EvalsView />
        ) : (
          // Keyed by reportId for the same remount guarantee as sessions.
          <EvalView key={route.reportId} reportId={route.reportId} />
        )}
      </main>
    </div>
  );
}
