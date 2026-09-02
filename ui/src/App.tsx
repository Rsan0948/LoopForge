import { useEffect, useState, type ReactElement } from "react";
import SessionView from "./views/SessionView";
import SessionsView from "./views/SessionsView";

// Hash-based routing so the StaticFiles html=True mount needs no SPA fallback.
// Routes: `#/sessions` (default) and `#/sessions/:runId`.

type Route = { kind: "sessions" } | { kind: "session"; runId: string };

function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "");
  const parts = path.split("/").filter((part) => part.length > 0);
  if (parts.length === 2 && parts[0] === "sessions") {
    return { kind: "session", runId: decodeURIComponent(parts[1]) };
  }
  return { kind: "sessions" };
}

export function navigateToSession(runId: string): void {
  window.location.hash = `#/sessions/${encodeURIComponent(runId)}`;
}

export function navigateToSessions(): void {
  window.location.hash = "#/sessions";
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
        {route.kind === "sessions" ? <SessionsView /> : <SessionView runId={route.runId} />}
      </main>
    </div>
  );
}
