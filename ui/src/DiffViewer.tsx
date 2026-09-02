import { useState, type FormEvent, type ReactElement } from "react";

// Parser + renderer for `workspace_snapshot` artifacts. The content format is
// produced by WorkspaceArtifactCollector (src/loopforge/workloads/repair.py):
//
//   workspace_id=<id>
//   base_revision=<rev>
//   changed_files=<comma-separated paths or ->
//   untracked_files=<comma-separated paths or ->
//   <blank line>
//   <unified diff from git diff, possibly empty>
//
// Anything malformed or truncated falls back to a raw <pre> — the artifact is
// evidence, so it must never be hidden by a parser bug.

export interface SnapshotHeader {
  workspaceId: string;
  baseRevision: string;
  changedFiles: string[];
  untrackedFiles: string[];
}

export type DiffLineKind = "add" | "del" | "hunk" | "context" | "meta";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
}

export interface DiffFile {
  path: string;
  additions: number;
  deletions: number;
  lines: DiffLine[];
}

export interface ParsedSnapshot {
  header: SnapshotHeader;
  files: DiffFile[];
}

function parsePathList(value: string): string[] {
  const trimmed = value.trim();
  if (trimmed === "-" || trimmed === "") return [];
  return trimmed.split(",").filter((entry) => entry.length > 0);
}

/** Strip the a/ or b/ prefix git adds to diff paths. */
function stripGitPrefix(path: string): string {
  return path.replace(/^[ab]\//, "");
}

export function parseWorkspaceSnapshot(content: string): ParsedSnapshot | null {
  try {
    const lines = content.split("\n");
    if (lines.length < 5) return null;
    const [workspaceLine, revisionLine, changedLine, untrackedLine, blankLine] = lines;
    if (
      !workspaceLine.startsWith("workspace_id=") ||
      !revisionLine.startsWith("base_revision=") ||
      !changedLine.startsWith("changed_files=") ||
      !untrackedLine.startsWith("untracked_files=") ||
      blankLine.trim() !== ""
    ) {
      return null;
    }
    const header: SnapshotHeader = {
      workspaceId: workspaceLine.slice("workspace_id=".length),
      baseRevision: revisionLine.slice("base_revision=".length),
      changedFiles: parsePathList(changedLine.slice("changed_files=".length)),
      untrackedFiles: parsePathList(untrackedLine.slice("untracked_files=".length)),
    };

    const diffLines = lines.slice(5);
    // Drop the trailing empty element produced by a final newline.
    while (diffLines.length > 0 && diffLines[diffLines.length - 1] === "") {
      diffLines.pop();
    }
    if (diffLines.length === 0) {
      return { header, files: [] };
    }

    const files: DiffFile[] = [];
    let current: DiffFile | null = null;
    let oldPath: string | null = null;
    let newPath: string | null = null;

    const finishFile = (): void => {
      if (current === null) return;
      // Prefer the +++ (new) path; fall back to --- (old) for deletions,
      // then to whatever the diff --git line carried.
      current.path = newPath ?? oldPath ?? current.path;
      files.push(current);
      current = null;
      oldPath = null;
      newPath = null;
    };

    for (const line of diffLines) {
      if (line.startsWith("diff --git ")) {
        finishFile();
        const match = /^diff --git (\S+) (\S+)/.exec(line);
        const bSide = match?.[2];
        current = {
          path: bSide !== undefined ? stripGitPrefix(bSide) : "(unknown)",
          additions: 0,
          deletions: 0,
          lines: [{ kind: "meta", text: line }],
        };
        continue;
      }
      if (current === null) {
        // Diff content before the first `diff --git` boundary: treat the
        // whole body as unparseable rather than guessing.
        return null;
      }
      if (line.startsWith("--- ")) {
        const path = line.slice(4).trim();
        oldPath = path === "/dev/null" ? null : stripGitPrefix(path);
        current.lines.push({ kind: "meta", text: line });
      } else if (line.startsWith("+++ ")) {
        const path = line.slice(4).trim();
        newPath = path === "/dev/null" ? null : stripGitPrefix(path);
        current.lines.push({ kind: "meta", text: line });
      } else if (line.startsWith("@@")) {
        current.lines.push({ kind: "hunk", text: line });
      } else if (line.startsWith("+")) {
        current.additions += 1;
        current.lines.push({ kind: "add", text: line });
      } else if (line.startsWith("-")) {
        current.deletions += 1;
        current.lines.push({ kind: "del", text: line });
      } else if (line.startsWith(" ")) {
        current.lines.push({ kind: "context", text: line });
      } else {
        // index / mode / similarity / rename / binary / "\ No newline" lines.
        current.lines.push({ kind: "meta", text: line });
      }
    }
    finishFile();

    if (files.length === 0) return null;
    return { header, files };
  } catch {
    return null;
  }
}

const LINE_CLASS: Record<DiffLineKind, string> = {
  add: "diff-add",
  del: "diff-del",
  hunk: "diff-hunk",
  context: "diff-context",
  meta: "diff-meta",
};

interface DiffViewerProps {
  content: string;
  /** True while the run is driving (server denies rollback with 409). */
  rollbackDisabled: boolean;
  onRollbackPaths: (paths: string[]) => void;
  onRollbackAll: () => void;
}

export default function DiffViewer({
  content,
  rollbackDisabled,
  onRollbackPaths,
  onRollbackAll,
}: DiffViewerProps): ReactElement {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [confirmAll, setConfirmAll] = useState(false);

  const parsed = parseWorkspaceSnapshot(content);
  if (parsed === null) {
    return <pre className="artifact-content">{content}</pre>;
  }
  const { header, files } = parsed;

  const toggle = (path: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const rollbackTitle = rollbackDisabled ? "run is driving — pause it before rollback" : undefined;

  const onConfirmAll = (event: FormEvent): void => {
    event.preventDefault();
    setConfirmAll(false);
    onRollbackAll();
  };

  return (
    <div className="diff-viewer">
      <dl className="snapshot-header mono">
        <div>
          <dt>workspace</dt>
          <dd>{header.workspaceId}</dd>
        </div>
        <div>
          <dt>base revision</dt>
          <dd>{header.baseRevision}</dd>
        </div>
        <div>
          <dt>changed files</dt>
          <dd>{header.changedFiles.length > 0 ? header.changedFiles.join(", ") : "—"}</dd>
        </div>
        <div>
          <dt>untracked files</dt>
          <dd>{header.untrackedFiles.length > 0 ? header.untrackedFiles.join(", ") : "—"}</dd>
        </div>
      </dl>

      <div className="rollback-toolbar">
        <button
          type="button"
          disabled={rollbackDisabled || selected.size === 0}
          title={rollbackTitle}
          onClick={() => {
            const paths = [...selected];
            setSelected(new Set());
            onRollbackPaths(paths);
          }}
        >
          rollback selected ({selected.size})
        </button>
        {confirmAll ? (
          <form onSubmit={onConfirmAll} className="inline-form">
            <span className="muted">reset entire workspace to base revision?</span>
            <button type="submit" className="danger">
              confirm full rollback
            </button>
            <button type="button" onClick={() => setConfirmAll(false)}>
              cancel
            </button>
          </form>
        ) : (
          <button
            type="button"
            className="danger"
            disabled={rollbackDisabled}
            title={rollbackTitle}
            onClick={() => setConfirmAll(true)}
          >
            rollback entire workspace…
          </button>
        )}
      </div>

      {files.length === 0 ? (
        <p className="muted">no changes against the base revision</p>
      ) : (
        files.map((file) => (
          <details key={file.path} className="diff-file" open>
            <summary>
              <input
                type="checkbox"
                checked={selected.has(file.path)}
                disabled={rollbackDisabled}
                title={rollbackTitle ?? "select for selective rollback"}
                onChange={() => toggle(file.path)}
                onClick={(event) => event.stopPropagation()}
              />
              <span className="diff-path mono">{file.path}</span>
              <span className="diff-counts mono">
                <span className="diff-add-text">+{file.additions}</span>{" "}
                <span className="diff-del-text">−{file.deletions}</span>
              </span>
            </summary>
            <div className="diff-lines mono">
              {file.lines.map((line, index) => (
                <div key={index} className={LINE_CLASS[line.kind]}>
                  {line.text}
                </div>
              ))}
            </div>
          </details>
        ))
      )}
    </div>
  );
}
