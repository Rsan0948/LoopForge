import type { ReactElement } from "react";
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
