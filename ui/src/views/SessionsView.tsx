import { useCallback, useEffect, useState, type FormEvent, type ReactElement } from "react";
import {
  ApiError,
  createSessionByProfile,
  createSessionInline,
  listProfiles,
  listSessions,
  type InlineProfile,
  type ProfileInfo,
  type SessionEntry,
} from "../api";
import { navigateToSession } from "../App";
import { formatAge, formatCost, formatTime, shortRunId, truncate } from "../format";
import { ErrorBanner, StatusBadge } from "../widgets";

const REFRESH_MS = 5000;

const CHECK_KINDS = ["test", "lint", "typecheck", "build"] as const;
const MODEL_PROVIDERS = ["scripted", "ollama", "deepseek"] as const;
const MODEL_TIERS = ["economy", "standard", "advanced"] as const;

interface CheckRow {
  name: string;
  kind: string;
  argv: string;
  timeout_seconds: string;
}

interface InlineForm {
  repository: string;
  objective: string;
  checks: CheckRow[];
  required: string;
  allowedPrefixes: string;
  requireChange: boolean;
  gateFileWrites: boolean;
  provider: string;
  modelName: string;
  tier: string;
  maxCostUsd: string;
  maxIterations: string;
  maxTotalTokens: string;
  maxElapsedSeconds: string;
}

function defaultInlineForm(): InlineForm {
  return {
    repository: "",
    objective: "",
    checks: [{ name: "tests", kind: "test", argv: "{python} -m pytest", timeout_seconds: "120" }],
    required: "tests",
    allowedPrefixes: "src/ tests/",
    requireChange: false,
    gateFileWrites: false,
    provider: "scripted",
    modelName: "",
    tier: "standard",
    maxCostUsd: "1.00",
    maxIterations: "10",
    maxTotalTokens: "",
    maxElapsedSeconds: "",
  };
}

/** Client-side validation; returns an error message or the built request. */
function buildInlineProfile(form: InlineForm): { error: string } | { profile: InlineProfile } {
  if (!form.repository.trim()) return { error: "repository is required" };
  if (!form.objective.trim()) return { error: "objective is required" };
  if (form.checks.length === 0) return { error: "at least one check is required" };
  const checks = [];
  for (const row of form.checks) {
    if (!row.name.trim()) return { error: "every check needs a name" };
    const argv = row.argv.trim().split(/\s+/).filter(Boolean);
    if (argv.length === 0) return { error: `check ${row.name}: argv is required` };
    const timeout = Number(row.timeout_seconds);
    if (!Number.isFinite(timeout) || timeout <= 0) {
      return { error: `check ${row.name}: timeout must be a positive number` };
    }
    checks.push({
      name: row.name.trim(),
      kind: row.kind.toUpperCase(),
      argv,
      timeout_seconds: timeout,
      cpu_seconds: null,
    });
  }
  const required = form.required.trim().split(/\s+/).filter(Boolean);
  const allowedPrefixes = form.allowedPrefixes.trim().split(/\s+/).filter(Boolean);
  if (required.length === 0) return { error: "acceptance.required must list at least one check name" };
  if (allowedPrefixes.length === 0) return { error: "acceptance.allowed_prefixes is required" };
  if (form.provider !== "scripted" && !form.modelName.trim()) {
    return { error: `model.name is required for provider ${form.provider}` };
  }
  const maxCostUsd = Number(form.maxCostUsd);
  const maxIterations = Number(form.maxIterations);
  if (!Number.isFinite(maxCostUsd) || maxCostUsd <= 0) return { error: "max_cost_usd must be positive" };
  if (!Number.isInteger(maxIterations) || maxIterations <= 0) {
    return { error: "max_iterations must be a positive integer" };
  }
  const tokensTrim = form.maxTotalTokens.trim();
  const maxTotalTokens = tokensTrim === "" ? null : Number(tokensTrim);
  // NaN/Infinity would serialize as null over JSON — silently becoming
  // "no limit" — so non-finite input is a form error, never a payload.
  if (maxTotalTokens !== null && (!Number.isInteger(maxTotalTokens) || maxTotalTokens <= 0)) {
    return { error: "max_total_tokens must be a positive integer" };
  }
  const elapsedTrim = form.maxElapsedSeconds.trim();
  const maxElapsedSeconds = elapsedTrim === "" ? null : Number(elapsedTrim);
  if (maxElapsedSeconds !== null && (!Number.isFinite(maxElapsedSeconds) || maxElapsedSeconds <= 0)) {
    return { error: "max_elapsed_seconds must be positive" };
  }
  return {
    profile: {
      repository: form.repository.trim(),
      objective: form.objective.trim(),
      checks,
      acceptance: {
        required,
        allowed_prefixes: allowedPrefixes,
        require_change: form.requireChange,
        max_changed_files: null,
      },
      sandbox: {
        container_image: null,
        environment: {},
        max_memory_bytes: null,
        local_python: null,
      },
      model: {
        provider: form.provider,
        name: form.modelName.trim() === "" ? null : form.modelName.trim(),
        tier: form.tier,
      },
      budget: {
        max_cost_usd: maxCostUsd,
        max_iterations: maxIterations,
        max_total_tokens: maxTotalTokens,
        max_elapsed_seconds: maxElapsedSeconds,
      },
      // The write tools are the local repair stack's only mutation surface;
      // gating them pauses the run for durable operator approval (PACS-014).
      approval: form.gateFileWrites ? { required_for: ["write_file", "edit_file"] } : null,
    },
  };
}

function NewSessionPanel(): ReactElement {
  const [mode, setMode] = useState<"profile" | "inline">("profile");
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [profilePath, setProfilePath] = useState("");
  const [form, setForm] = useState<InlineForm>(defaultInlineForm);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listProfiles()
      .then((list) => {
        setProfiles(list);
        if (list.length > 0) setProfilePath(list[0].path);
      })
      .catch(() => setProfiles([]));
  }, []);

  const patch = (partial: Partial<InlineForm>): void => setForm((prev) => ({ ...prev, ...partial }));

  const patchCheck = (index: number, partial: Partial<CheckRow>): void =>
    setForm((prev) => ({
      ...prev,
      checks: prev.checks.map((row, i) => (i === index ? { ...row, ...partial } : row)),
    }));

  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    void (async () => {
      try {
        let runId: string;
        if (mode === "profile") {
          if (!profilePath) {
            setError("select a profile first");
            return;
          }
          runId = await createSessionByProfile(profilePath);
        } else {
          const built = buildInlineProfile(form);
          if ("error" in built) {
            setError(built.error);
            return;
          }
          runId = await createSessionInline(built.profile);
        }
        navigateToSession(runId);
      } catch (err) {
        setError(err instanceof ApiError ? err.detail : String(err));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>New session</h2>
        <div className="mode-toggle">
          <button
            type="button"
            className={mode === "profile" ? "active" : ""}
            onClick={() => setMode("profile")}
          >
            from profile
          </button>
          <button
            type="button"
            className={mode === "inline" ? "active" : ""}
            onClick={() => setMode("inline")}
          >
            inline
          </button>
        </div>
      </div>
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      <form onSubmit={onSubmit} className="new-session-form">
        {mode === "profile" ? (
          <label className="field">
            <span>profile</span>
            {profiles.length === 0 ? (
              <em className="muted">no profiles found (data-dir profiles or ./.loopforge/*.toml)</em>
            ) : (
              <select value={profilePath} onChange={(e) => setProfilePath(e.target.value)}>
                {profiles.map((p) => (
                  <option key={p.path} value={p.path}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </label>
        ) : (
          <>
            <label className="field">
              <span>repository</span>
              <input
                value={form.repository}
                onChange={(e) => patch({ repository: e.target.value })}
                placeholder="/absolute/path/to/repo"
              />
            </label>
            <label className="field">
              <span>objective</span>
              <textarea
                rows={2}
                value={form.objective}
                onChange={(e) => patch({ objective: e.target.value })}
                placeholder="what the agent should accomplish"
              />
            </label>
            <fieldset className="checks">
              <legend>checks</legend>
              {form.checks.map((row, index) => (
                <div className="check-row" key={index}>
                  <input
                    className="check-name"
                    value={row.name}
                    onChange={(e) => patchCheck(index, { name: e.target.value })}
                    placeholder="name"
                  />
                  <select value={row.kind} onChange={(e) => patchCheck(index, { kind: e.target.value })}>
                    {CHECK_KINDS.map((kind) => (
                      <option key={kind} value={kind}>
                        {kind}
                      </option>
                    ))}
                  </select>
                  <input
                    className="check-argv"
                    value={row.argv}
                    onChange={(e) => patchCheck(index, { argv: e.target.value })}
                    placeholder="argv (space-separated; argv[0] absolute or {python})"
                  />
                  <input
                    className="check-timeout"
                    value={row.timeout_seconds}
                    onChange={(e) => patchCheck(index, { timeout_seconds: e.target.value })}
                    placeholder="timeout s"
                  />
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => patch({ checks: form.checks.filter((_, i) => i !== index) })}
                  >
                    ✕
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="link-button"
                onClick={() =>
                  patch({
                    checks: [...form.checks, { name: "", kind: "test", argv: "", timeout_seconds: "120" }],
                  })
                }
              >
                + add check
              </button>
            </fieldset>
            <div className="field-grid">
              <label className="field">
                <span>acceptance.required</span>
                <input
                  value={form.required}
                  onChange={(e) => patch({ required: e.target.value })}
                  placeholder="check names, space-separated"
                />
              </label>
              <label className="field">
                <span>acceptance.allowed_prefixes</span>
                <input
                  value={form.allowedPrefixes}
                  onChange={(e) => patch({ allowedPrefixes: e.target.value })}
                  placeholder="src/ tests/"
                />
              </label>
              <label className="field field-inline">
                <span>require_change</span>
                <input
                  type="checkbox"
                  checked={form.requireChange}
                  onChange={(e) => patch({ requireChange: e.target.checked })}
                />
              </label>
              <label className="field field-inline" title="write_file/edit_file pause the run for durable operator approval">
                <span>gate file writes</span>
                <input
                  type="checkbox"
                  checked={form.gateFileWrites}
                  onChange={(e) => patch({ gateFileWrites: e.target.checked })}
                />
              </label>
              <label className="field">
                <span>model.provider</span>
                <select value={form.provider} onChange={(e) => patch({ provider: e.target.value })}>
                  {MODEL_PROVIDERS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>model.name</span>
                <input
                  value={form.modelName}
                  onChange={(e) => patch({ modelName: e.target.value })}
                  placeholder={form.provider === "scripted" ? "(optional)" : "required"}
                />
              </label>
              <label className="field">
                <span>model.tier</span>
                <select value={form.tier} onChange={(e) => patch({ tier: e.target.value })}>
                  {MODEL_TIERS.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>budget.max_cost_usd</span>
                <input value={form.maxCostUsd} onChange={(e) => patch({ maxCostUsd: e.target.value })} />
              </label>
              <label className="field">
                <span>budget.max_iterations</span>
                <input
                  value={form.maxIterations}
                  onChange={(e) => patch({ maxIterations: e.target.value })}
                />
              </label>
              <label className="field">
                <span>budget.max_total_tokens</span>
                <input
                  value={form.maxTotalTokens}
                  onChange={(e) => patch({ maxTotalTokens: e.target.value })}
                  placeholder="(optional)"
                />
              </label>
              <label className="field">
                <span>budget.max_elapsed_seconds</span>
                <input
                  value={form.maxElapsedSeconds}
                  onChange={(e) => patch({ maxElapsedSeconds: e.target.value })}
                  placeholder="(optional)"
                />
              </label>
            </div>
          </>
        )}
        <div>
          <button type="submit" disabled={busy || (mode === "profile" && profiles.length === 0)}>
            {busy ? "creating…" : "create session"}
          </button>
        </div>
      </form>
    </section>
  );
}

export default function SessionsView(): ReactElement {
  const [sessions, setSessions] = useState<SessionEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setSessions(await listSessions());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      setNowMs(Date.now());
      void refresh();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  return (
    <div className="stack">
      <section className="panel">
        <div className="panel-header">
          <h2>Sessions</h2>
          <button type="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
        {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
        {sessions === null ? (
          <p className="muted">loading…</p>
        ) : sessions.length === 0 ? (
          <p className="muted">no sessions yet</p>
        ) : (
          <table className="sessions-table">
            <thead>
              <tr>
                <th>run</th>
                <th>objective</th>
                <th>status</th>
                <th>started</th>
                <th>last event</th>
                <th>cost</th>
                <th>flags</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.run_id}>
                  <td>
                    <a href={`#/sessions/${encodeURIComponent(s.run_id)}`} className="mono">
                      {shortRunId(s.run_id)}
                    </a>
                  </td>
                  <td className="objective-cell" title={s.objective}>
                    {truncate(s.objective, 80)}
                  </td>
                  <td>
                    <StatusBadge status={s.status} />
                  </td>
                  <td className="mono">{formatTime(s.started_at)}</td>
                  <td className="mono">{formatAge(s.last_occurred_at, nowMs)}</td>
                  <td className="mono">{formatCost(s.cost_usd)}</td>
                  <td className="muted mono">
                    {s.driving ? "driving " : ""}
                    {s.managed ? "managed" : "unmanaged"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <NewSessionPanel />
    </div>
  );
}
