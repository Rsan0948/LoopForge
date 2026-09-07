import { useCallback, useEffect, useState, type FormEvent, type ReactElement } from "react";
import { ApiError, browseDirectories, type FsBrowseResult } from "./api";
import { ErrorBanner } from "./widgets";

// VS Code open-folder style directory picker for the inline session form.
// Read-only (the backend lists directory names only); selecting requires a
// git worktree because session creation demands one — the affordance is
// honest rather than letting the operator pick a path the server will deny.

function joinPath(parent: string, name: string): string {
  return parent === "/" ? `/${name}` : `${parent}/${name}`;
}

export default function RepoBrowser({
  onSelect,
  onClose,
}: {
  onSelect: (path: string) => void;
  onClose: () => void;
}): ReactElement {
  const [result, setResult] = useState<FsBrowseResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pathDraft, setPathDraft] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(true);

  const navigate = useCallback((path?: string) => {
    setLoading(true);
    setError(null);
    browseDirectories(path)
      .then((fresh) => {
        setResult(fresh);
        setPathDraft(fresh.path);
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.detail : String(err)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    navigate();
  }, [navigate]);

  const onJump = (event: FormEvent): void => {
    event.preventDefault();
    const target = pathDraft.trim();
    if (target !== "") navigate(target);
  };

  const visible = (result?.entries ?? []).filter((entry) => showHidden || !entry.is_hidden);
  const hiddenCount = (result?.entries ?? []).length - visible.length;

  return (
    <div className="repo-browser">
      {error !== null && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
      {result !== null && result.shortcuts.length > 0 && (
        <div className="repo-shortcuts">
          {result.shortcuts.map((shortcut) => (
            <button
              key={shortcut}
              type="button"
              className="link-button"
              onClick={() => navigate(shortcut)}
            >
              {shortcut}
            </button>
          ))}
        </div>
      )}
      <form className="repo-path-form" onSubmit={onJump}>
        {result !== null && result.parent !== null && (
          <button type="button" title="parent directory" onClick={() => navigate(result.parent ?? undefined)}>
            ↑
          </button>
        )}
        <input
          className="mono"
          value={pathDraft}
          onChange={(event) => setPathDraft(event.target.value)}
          placeholder="/absolute/path — Enter to jump"
        />
      </form>
      <div className="repo-list">
        {loading ? (
          <p className="muted">loading…</p>
        ) : visible.length === 0 ? (
          <p className="muted">
            {result !== null && result.entries.length > 0
              ? "only hidden directories here"
              : "no subdirectories"}
          </p>
        ) : (
          visible.map((entry) => (
            <button
              key={entry.name}
              type="button"
              className="repo-row"
              onClick={() => navigate(joinPath(result?.path ?? "/", entry.name))}
            >
              <span className="repo-name mono">{entry.name}/</span>
              {entry.is_git_worktree && <span className="badge badge-muted">git</span>}
            </button>
          ))
        )}
      </div>
      {result !== null && result.notes.map((note) => (
        <p key={note} className="muted repo-note">
          {note}
        </p>
      ))}
      <div className="repo-footer">
        <label className="follow-toggle">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(event) => setShowHidden(event.target.checked)}
          />
          show hidden{hiddenCount > 0 && !showHidden ? ` (${hiddenCount})` : ""}
        </label>
        {result !== null && result.truncated && (
          <span className="muted">listing truncated (500 entries)</span>
        )}
        <span className="repo-footer-actions">
          {result !== null && !result.is_git_worktree && (
            <span className="muted">not a git worktree (no .git)</span>
          )}
          <button
            type="button"
            className="primary"
            disabled={result === null || !result.is_git_worktree}
            title={
              result !== null && result.is_git_worktree
                ? "use this directory as the session repository"
                : "session creation requires a git worktree — descend into one"
            }
            onClick={() => {
              if (result !== null) onSelect(result.path);
            }}
          >
            select this directory
          </button>
          <button type="button" onClick={onClose}>
            close
          </button>
        </span>
      </div>
    </div>
  );
}
