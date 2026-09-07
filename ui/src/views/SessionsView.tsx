import { useCallback, useEffect, useState, type FormEvent, type ReactElement } from "react";
import {
  ApiError,
  createSessionByProfile,
  createSessionInline,
  detectHarness,
  listProfiles,
  listSessions,
  type InlineProfile,
  type ProfileInfo,
  type SessionEntry,
} from "../api";
import { navigateToEvals, navigateToPolicies, navigateToSession } from "../App";
import { formatAge, formatCost, formatTime, shortRunId, truncate } from "../format";
import RepoBrowser from "../RepoBrowser";
import { ErrorBanner, InfoPill, RunFlags, StatusBadge } from "../widgets";

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
  noProgressLimit: string;
  containerImage: string;
}

function defaultInlineForm(): InlineForm {
  return {
    repository: "",
    objective: "",
    checks: [{ name: "tests", kind: "test", argv: "{python} -m pytest", timeout_seconds: "120" }],
    // No trailing slashes: the domain patch-constraint contract rejects
    // empty path segments, so "src/ tests/" would 422 on creation.
    required: "tests",
    allowedPrefixes: "src tests",
    requireChange: false,
    gateFileWrites: false,
    provider: "scripted",
    modelName: "",
    tier: "standard",
    maxCostUsd: "1.00",
    maxIterations: "10",
    maxTotalTokens: "",
    maxElapsedSeconds: "",
    noProgressLimit: "",
    containerImage: "",
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
  const progressTrim = form.noProgressLimit.trim();
  const noProgressLimit = progressTrim === "" ? null : Number(progressTrim);
  if (noProgressLimit !== null && (!Number.isInteger(noProgressLimit) || noProgressLimit <= 0)) {
    return { error: "no_progress_limit must be a positive integer" };
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
        container_image: form.containerImage.trim() === "" ? null : form.containerImage.trim(),
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
        no_progress_limit: noProgressLimit,
      },
      // The write tools are the local repair stack's only mutation surface;
      // gating them pauses the run for durable operator approval (PACS-014).
      approval: form.gateFileWrites ? { required_for: ["write_file", "edit_file"] } : null,
    },
  };
}

const RECENT_REPOS_KEY = "loopforge.recentRepositories";
const RECENT_REPOS_MAX = 5;

function loadRecentRepos(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENT_REPOS_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return []; // storage unavailable or corrupted — recents are a convenience only
  }
}

function NewSessionPanel(): ReactElement {
  const [mode, setMode] = useState<"profile" | "inline">("profile");
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [profilePath, setProfilePath] = useState("");
  const [form, setForm] = useState<InlineForm>(defaultInlineForm);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [detectNotes, setDetectNotes] = useState<string[]>([]);
  const [recentRepos, setRecentRepos] = useState<string[]>(loadRecentRepos);

  const rememberRepo = useCallback((path: string): void => {
    setRecentRepos((prev) => {
      const next = [path, ...prev.filter((item) => item !== path)].slice(0, RECENT_REPOS_MAX);
      try {
        window.localStorage.setItem(RECENT_REPOS_KEY, JSON.stringify(next));
      } catch {
        // Storage full/unavailable — the in-memory list still works this session.
      }
      return next;
    });
  }, []);

  // Harness detection (read-only): prefill only fields the operator has not
  // touched (still at their defaults) — a suggestion never clobbers an edit,
  // and the server re-validates everything on creation regardless.
  const runDetect = useCallback(
    (path: string): void => {
      detectHarness(path)
        .then((result) => {
          setDetectNotes(result.notes);
          rememberRepo(path);
          setForm((prev) => {
            const defaults = defaultInlineForm();
            const next = { ...prev };
            if (
              result.checks.length > 0 &&
              JSON.stringify(prev.checks) === JSON.stringify(defaults.checks)
            ) {
              next.checks = result.checks.map((check) => ({
                name: check.name,
                kind: check.kind.toLowerCase(),
                argv: check.argv.join(" "),
                timeout_seconds: String(check.timeout_seconds),
              }));
            }
            if (result.required.length > 0 && prev.required === defaults.required) {
              next.required = result.required.join(" ");
            }
            if (
              result.allowed_prefixes.length > 0 &&
              prev.allowedPrefixes === defaults.allowedPrefixes
            ) {
              next.allowedPrefixes = result.allowed_prefixes.join(" ");
            }
            return next;
          });
        })
        .catch(() => setDetectNotes([])); // manual entry always still works
    },
    [rememberRepo],
  );

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
            <div className="field">
              <span>
                repository{" "}
                <InfoPill text="Absolute path to the git worktree the agent will repair. It must contain a .git directory — use browse… to pick it from your filesystem instead of typing." />
              </span>
              <div className="repo-input-row">
                <input
                  value={form.repository}
                  onChange={(e) => patch({ repository: e.target.value })}
                  onBlur={() => {
                    const value = form.repository.trim();
                    if (value.startsWith("/")) runDetect(value);
                  }}
                  placeholder="/absolute/path/to/repo"
                />
                <button type="button" onClick={() => setBrowseOpen((open) => !open)}>
                  {browseOpen ? "hide browser" : "browse…"}
                </button>
              </div>
              {recentRepos.length > 0 && (
                <div className="recent-repos">
                  <span className="muted">recent:</span>
                  {recentRepos.map((path) => (
                    <button
                      key={path}
                      type="button"
                      className="link-button mono"
                      title={path}
                      onClick={() => {
                        patch({ repository: path });
                        runDetect(path);
                      }}
                    >
                      {path.split("/").filter(Boolean).pop() ?? path}
                    </button>
                  ))}
                </div>
              )}
              {detectNotes.map((note) => (
                <p key={note} className="muted field-note">
                  {note}
                </p>
              ))}
            </div>
            {browseOpen && (
              <RepoBrowser
                onSelect={(path) => {
                  patch({ repository: path });
                  setBrowseOpen(false);
                  runDetect(path);
                }}
                onClose={() => setBrowseOpen(false)}
              />
            )}
            <label className="field">
              <span>
                objective{" "}
                <InfoPill text="What the agent should accomplish. It is written to the durable event log and the agent cannot change it — only you can, via amend objective." />
              </span>
              <textarea
                rows={2}
                value={form.objective}
                onChange={(e) => patch({ objective: e.target.value })}
                placeholder="what the agent should accomplish"
              />
            </label>
            <fieldset className="checks">
              <legend>
                checks{" "}
                <InfoPill text="The verification commands the deterministic verifier runs — the model's own claim of success means nothing. argv[0] must be absolute or {python} (the repo's .venv/bin/python) in local mode, or an in-container path when a container image is set." />
              </legend>
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
                <span>
                  acceptance.required{" "}
                  <InfoPill text="The checks that must pass for the verifier to grant success. Space-separated check names from above." />
                </span>
                <input
                  value={form.required}
                  onChange={(e) => patch({ required: e.target.value })}
                  placeholder="check names, space-separated"
                />
              </label>
              <label className="field">
                <span>
                  acceptance.allowed_prefixes{" "}
                  <InfoPill text="Path prefixes the agent's patch may touch (e.g. src tests — no trailing slashes). A change anywhere else fails verification — this is the scope boundary, and the agent cannot widen it." />
                </span>
                <input
                  value={form.allowedPrefixes}
                  onChange={(e) => patch({ allowedPrefixes: e.target.value })}
                  placeholder="src tests"
                />
              </label>
              <label className="field field-inline">
                <span>
                  require_change{" "}
                  <InfoPill text="When on, verification fails if the workspace did not change — a no-op 'success' cannot pass." />
                </span>
                <input
                  type="checkbox"
                  checked={form.requireChange}
                  onChange={(e) => patch({ requireChange: e.target.checked })}
                />
              </label>
              <label className="field field-inline">
                <span>
                  gate file writes{" "}
                  <InfoPill text="When on, write_file/edit_file pause the run for your durable approval before executing (PACS-014). Nothing is written without your explicit grant." />
                </span>
                <input
                  type="checkbox"
                  checked={form.gateFileWrites}
                  onChange={(e) => patch({ gateFileWrites: e.target.checked })}
                />
              </label>
              <label className="field">
                <span>
                  sandbox.container_image{" "}
                  <InfoPill text="Docker image for check execution (e.g. python:3.12-alpine). When set, check argv must be in-container paths and {python} is not substituted. Required on macOS, where the local sandbox fails closed." />
                </span>
                <input
                  value={form.containerImage}
                  onChange={(e) => patch({ containerImage: e.target.value })}
                  placeholder="(optional; required on macOS)"
                />
              </label>
              <label className="field">
                <span>
                  model.provider{" "}
                  <InfoPill text="scripted = deterministic offline driver (no credentials, for dry runs). ollama / deepseek = live models." />
                </span>
                <select value={form.provider} onChange={(e) => patch({ provider: e.target.value })}>
                  {MODEL_PROVIDERS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>
                  model.name{" "}
                  <InfoPill text="The provider's model id, e.g. devstral-small-2:latest. Not needed for the scripted provider." />
                </span>
                <input
                  value={form.modelName}
                  onChange={(e) => patch({ modelName: e.target.value })}
                  placeholder={form.provider === "scripted" ? "(optional)" : "required"}
                />
              </label>
              <label className="field">
                <span>
                  model.tier{" "}
                  <InfoPill text="Capability class used for routing decisions: economy / standard / advanced." />
                </span>
                <select value={form.tier} onChange={(e) => patch({ tier: e.target.value })}>
                  {MODEL_TIERS.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>
                  budget.max_cost_usd{" "}
                  <InfoPill text="Hard spending ceiling in USD. The deterministic runtime stops the run when it is exceeded — the model cannot raise its own budget." />
                </span>
                <input value={form.maxCostUsd} onChange={(e) => patch({ maxCostUsd: e.target.value })} />
              </label>
              <label className="field">
                <span>
                  budget.max_iterations{" "}
                  <InfoPill text="Hard cap on model turns for this run." />
                </span>
                <input
                  value={form.maxIterations}
                  onChange={(e) => patch({ maxIterations: e.target.value })}
                />
              </label>
              <label className="field">
                <span>
                  budget.max_total_tokens{" "}
                  <InfoPill text="Optional hard cap on total tokens (input + output). Leave empty for no token cap." />
                </span>
                <input
                  value={form.maxTotalTokens}
                  onChange={(e) => patch({ maxTotalTokens: e.target.value })}
                  placeholder="(optional)"
                />
              </label>
              <label className="field">
                <span>
                  budget.max_elapsed_seconds{" "}
                  <InfoPill text="Optional wall-clock cap for the whole run. Leave empty for no time cap." />
                </span>
                <input
                  value={form.maxElapsedSeconds}
                  onChange={(e) => patch({ maxElapsedSeconds: e.target.value })}
                  placeholder="(optional)"
                />
              </label>
              <label className="field">
                <span>
                  budget.no_progress_limit{" "}
                  <InfoPill text="Stall threshold: consecutive turns without progress before the run stops as stalled (default 3)." />
                </span>
                <input
                  value={form.noProgressLimit}
                  onChange={(e) => patch({ noProgressLimit: e.target.value })}
                  placeholder="(optional, default 3)"
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
          <a
            className="link-button"
            href="#/evals"
            onClick={(event) => {
              event.preventDefault();
              navigateToEvals();
            }}
          >
            evals →
          </a>
          <a
            className="link-button"
            href="#/policies"
            onClick={(event) => {
              event.preventDefault();
              navigateToPolicies();
            }}
          >
            policies →
          </a>
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
                <tr
                  key={s.run_id}
                  className="row-link"
                  title="open session"
                  onClick={() => navigateToSession(s.run_id)}
                >
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
                  <td>
                    <RunFlags driving={s.driving} managed={s.managed} />
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
