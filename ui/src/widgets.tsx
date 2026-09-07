import { useState, type ReactElement } from "react";
import type { PolicyLifecycle, RunStatus } from "./api";

const STATUS_CLASS: Record<RunStatus, string> = {
  created: "badge badge-muted",
  planning: "badge badge-active",
  ready: "badge badge-active",
  acting: "badge badge-active",
  verifying: "badge badge-active",
  reflecting: "badge badge-active",
  waiting_for_approval: "badge badge-amber",
  succeeded: "badge badge-green",
  failed: "badge badge-red",
  stalled: "badge badge-red",
  budget_exhausted: "badge badge-red",
  cancelled: "badge badge-muted",
};

export function StatusBadge({ status }: { status: RunStatus }): ReactElement {
  return <span className={STATUS_CLASS[status] ?? "badge"}>{status}</span>;
}

const LIFECYCLE_CLASS: Record<PolicyLifecycle, string> = {
  candidate: "badge badge-muted",
  shadowed: "badge badge-active",
  benchmarked: "badge badge-amber",
  promoted: "badge badge-green",
  retired: "badge badge-muted",
};

/** Lifecycle badge for the operator-owned policy registry (PACS-017 M6). */
export function LifecycleBadge({ lifecycle }: { lifecycle: PolicyLifecycle }): ReactElement {
  return <span className={LIFECYCLE_CLASS[lifecycle] ?? "badge"}>{lifecycle}</span>;
}

/** A small ⓘ toggle that expands an inline explanation — the standard way
 *  the console documents a field without hiding meaning in a title attr. */
export function InfoPill({ text }: { text: string }): ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <span className="info-pill-wrap">
      <button
        type="button"
        className="info-pill"
        aria-label="more info"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        ⓘ
      </button>
      {open && <span className="info-pill-text">{text}</span>}
    </span>
  );
}

/** Driving/managed flags as badges. "unmanaged" means the server did not
 *  create this run and will not drive it — control happens via the CLI. */
export function RunFlags({ driving, managed }: { driving: boolean; managed: boolean }): ReactElement {
  return (
    <span className="flags">
      {driving && (
        <span className="badge badge-active" title="the server is actively driving this run">
          driving
        </span>
      )}
      <span
        className="badge badge-muted"
        title={
          managed
            ? "created by this server — it can drive and control the run"
            : "not owned by this server — it will not drive the run; control it via the CLI"
        }
      >
        {managed ? "managed" : "unmanaged"}
      </span>
    </span>
  );
}

export function ErrorBanner({ message, onDismiss }: { message: string; onDismiss?: () => void }): ReactElement {
  return (
    <div className="error-banner" role="alert">
      <span>{message}</span>
      {onDismiss !== undefined && (
        <button type="button" className="link-button" onClick={onDismiss}>
          dismiss
        </button>
      )}
    </div>
  );
}
